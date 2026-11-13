"""Frozen theme observations and small, evidence-backed priority penalties.

This ledger describes price observations, not trades, causal explanations or
model training. It performs no network I/O. All inputs retain provider times.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import mean
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
MEANING = '冻结参考价至收盘的价格观察，非成交收益；归因是证据描述而非因果结论；仅小幅调整题材优先级，不修改模型权重。'
VERSION = 'theme-feedback-v1'
BRIEF_MEANING = '市场主线重点观察股的首次有效参考价至当日收盘价格变化；仅为watch观察，非买入建议成交收益，不计入规则精选胜率、训练样本或题材降权。'


def _stamp(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.astimezone(TZ) if value.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _now(value):
    result = _stamp(value)
    if result is None:
        raise ValueError('An explicit timezone-aware observation time is required')
    return result


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _code(value):
    value = str(value or '').strip()
    return value if re.fullmatch(r'\d{6}', value) else ''


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def _calendar(start, end):
    try:
        import exchange_calendars as xcals
        return [s.date() for s in xcals.get_calendar('XSHG').sessions_in_range(start, end)]
    except Exception:
        return []


def _empty(now, status='no_history', gaps=None):
    return {'version': VERSION, 'status': status, 'asOf': now.isoformat(), 'sourceDay': None,
            'summary': '尚无可用的已结算题材反馈。', 'themes': [], 'missedLeaders': [],
            'adjustments': {}, 'gaps': gaps or [], 'meaning': MEANING,
            'modelWeightsChanged': False, 'trainingEligible': False}


def _empty_brief(now, status='no_history', gaps=None):
    return {'version': 'market-brief-feedback-v1', 'status': status, 'asOf': now.isoformat(),
            'sourceDay': None, 'recordType': 'watch_observation', 'summary': '尚无已保存的市场重点观察复盘。',
            'items': [], 'rows': [], 'watchCount': 0, 'observedCount': 0, 'pendingCount': 0,
            'coverage': {'watchCount': 0, 'observedCount': 0, 'pct': None},
            'gaps': gaps or [], 'meaning': BRIEF_MEANING, 'trainingEligible': False,
            'includedInRulePerformance': False, 'adjustments': {}}


def _quote(raw, code, now, *, close=False):
    """Reuse the public adapter's explicit post-close exception, never fetched_at."""
    from .local_observation_review import _quote as validate_quote
    raw = raw if isinstance(raw, dict) else {}
    if raw.get('stale') is True:
        raw = {**raw, 'is_stale': True}
    earliest = (now.replace(hour=14, minute=57, second=0, microsecond=0) if close
                else now - timedelta(seconds=300))
    quote, gaps = validate_quote(raw, code, now, earliest, post_close_reference=close)
    stamp = _stamp(quote.get('source_time'))
    if close and stamp and not time(14, 57) <= stamp.time() <= time(15, 5):
        gaps.append('收盘源时间不在14:57至15:05')
    if raw.get('conflict') is True:
        gaps.append('来源标记冲突')
    return {**quote, 'code': code}, list(dict.fromkeys(gaps))


class ThemeFeedbackStore:
    def __init__(self, data_dir, calendar_provider=None):
        self.path = Path(data_dir) / 'theme-feedback-v1.sqlite3'
        self.calendar_provider = calendar_provider or _calendar

    @staticmethod
    def _schema(db):
        db.executescript('''
                CREATE TABLE IF NOT EXISTS scans(
                    scan_id TEXT PRIMARY KEY, day TEXT NOT NULL, decision_at TEXT NOT NULL,
                    known_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS scan_day ON scans(day,known_at);
                CREATE TABLE IF NOT EXISTS baselines(
                    day TEXT NOT NULL, code TEXT NOT NULL, known_at TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(day,code));
                CREATE TABLE IF NOT EXISTS closes(
                    day TEXT NOT NULL, code TEXT NOT NULL, known_at TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(day,code));
                CREATE TABLE IF NOT EXISTS feedback(
                    day TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(day,as_of));
                CREATE TABLE IF NOT EXISTS brief_references(
                    day TEXT NOT NULL, code TEXT NOT NULL, known_at TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(day,code));
                CREATE TABLE IF NOT EXISTS brief_closes(
                    day TEXT NOT NULL, code TEXT NOT NULL, known_at TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(day,code));
                CREATE TABLE IF NOT EXISTS brief_feedback(
                    day TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(day,as_of));
        ''')

    @contextmanager
    def _db(self, write=True):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=1)
        else:
            db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)
        db.row_factory = sqlite3.Row
        try:
            if write:
                self._schema(db)
            with db:
                yield db
        finally:
            db.close()

    def _sessions(self, start, end):
        try:
            return sorted({d if type(d) is date else date.fromisoformat(str(d)[:10])
                           for d in self.calendar_provider(start, end)
                           if start <= date.fromisoformat(str(d)[:10]) <= end})
        except Exception:
            return []

    def _fresh(self, day, now):
        start = date.fromisoformat(day)
        sessions = self._sessions(start, now.date())
        return (timedelta(0) <= now.date() - start <= timedelta(days=7)
                and start in sessions and len([d for d in sessions if d > start]) <= 5)

    @staticmethod
    def _has_brief_tables(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brief_references'").fetchone() is not None

    @staticmethod
    def _save_brief(db, run, now, fallback_decision):
        brief = run.get('marketBrief')
        if not isinstance(brief, dict):
            return {'status': 'no_brief', 'watchCount': 0}
        decision = _stamp(brief.get('asOf')) if brief.get('asOf') else fallback_decision
        if decision is None or decision > now or decision.date() != now.date():
            return {'status': 'rejected', 'watchCount': 0, 'gaps': ['市场简报时刻无效或超前']}
        at, day = now.isoformat(), decision.date().isoformat()
        count = 0
        for raw in brief.get('focusStocks') or []:
            if not isinstance(raw, dict) or not (code := _code(raw.get('code'))):
                continue
            quote_raw = dict(raw.get('quote') or raw)
            quote_raw['source_time'] = quote_raw.get('source_time') or quote_raw.get('sourceTime')
            source_at = _stamp(quote_raw.get('provider_timestamp') or quote_raw.get('source_time'))
            session_reference = (raw.get('quoteFreshness') == 'session_reference' and source_at is not None
                and source_at.date() == decision.date() and source_at <= decision
                and (time(11, 30) <= decision.time() < time(13) and time(11, 27) <= source_at.time() < time(13)
                     or decision.time() >= time(15) and time(14, 57) <= source_at.time() <= time(15, 5)))
            # The brief already distinguishes an explicit resting-session
            # reference from live execution evidence. Preserve its provider
            # clock; validate the original quote without inventing a new price.
            quote, gaps = _quote(quote_raw, code, source_at if session_reference else decision)
            record = {'code': code, 'name': raw.get('name') or code, 'recordType': 'watch_observation',
                'firstSeenAt': decision.isoformat(), 'referenceAt': decision.isoformat() if not gaps else None,
                'referenceKnownAt': at if not gaps else None, 'referenceQuote': quote if not gaps else None,
                'watchSource': brief.get('source'), 'themeIds': raw.get('themeIds') or ([raw['themeId']] if raw.get('themeId') else []),
                'themes': raw.get('themes') or ([{'id': raw.get('themeId'), 'name': raw.get('themeName')}] if raw.get('themeId') else []),
                'eventDate': raw.get('eventDate'), 'eventSourceTime': raw.get('eventSourceTime'),
                'quoteFreshness': 'session_reference' if session_reference else raw.get('quoteFreshness'),
                'watchStatusAtDecision': raw.get('status'), 'ruleRecommendationAtDecision': raw.get('ruleRecommendation') is True,
                'gaps': gaps, 'trainingEligible': False, 'includedInRulePerformance': False}
            previous = db.execute('SELECT payload FROM brief_references WHERE day=? AND code=?', (day, code)).fetchone()
            if previous is None:
                db.execute('INSERT INTO brief_references VALUES(?,?,?,?)', (day, code, at, _json(record)))
            elif not gaps:
                prior = json.loads(previous['payload'])
                # Do not retime an unavailable original price. A later first
                # valid reference has its own explicit decision/known times.
                if not prior.get('referenceQuote'):
                    record['firstSeenAt'] = prior['firstSeenAt']
                    db.execute('UPDATE brief_references SET payload=? WHERE day=? AND code=?', (_json(record), day, code))
            count += 1
        return {'status': 'saved', 'watchCount': count}

    def _review_brief(self, db, now, quotes):
        day, at = now.date().isoformat(), now.isoformat()
        references = [json.loads(row['payload']) for row in db.execute(
            'SELECT payload FROM brief_references WHERE day=? AND known_at<=? ORDER BY code', (day, at))]
        if not references:
            return _empty_brief(now, 'no_watches')
        items = []
        for row in references:
            code = row['code']
            known = _stamp(row.get('referenceKnownAt'))
            reference = row.get('referenceQuote') if known and known <= now else None
            close, gaps = _quote(quotes.get(code), code, now, close=True)
            if reference and not gaps and _stamp(close['source_time']) >= _stamp(row['referenceAt']):
                db.execute('INSERT OR IGNORE INTO brief_closes VALUES(?,?,?,?)', (day, code, at, _json(close)))
            settled = db.execute('SELECT payload FROM brief_closes WHERE day=? AND code=? AND known_at<=?', (day, code, at)).fetchone()
            close = json.loads(settled['payload']) if settled and reference else None
            observed = bool(reference and close)
            item = {**row, 'referenceQuote': reference, 'closeQuote': close,
                'referencePrice': reference['price'] if reference else None, 'closePrice': close['price'] if close else None,
                'referenceAt': row.get('referenceAt') if reference else None,
                'referenceKnownAt': row.get('referenceKnownAt') if reference else None,
                'status': 'observed' if observed else 'pending_data',
                'observationChangePct': round((close['price']/reference['price']-1)*100, 6) if observed else None,
                'gaps': [] if observed else ['缺少当时有效参考价'] if not reference else (gaps or ['收盘证据时间早于观察参考'])}
            item['changePct'] = item['observationChangePct']
            items.append(item)
        observed = sum(row['status'] == 'observed' for row in items)
        result = _empty_brief(now, 'reviewed' if observed == len(items) else 'partial' if observed else 'pending_data')
        result.update(sourceDay=day, items=items, rows=items, watchCount=len(items), observedCount=observed,
            pendingCount=len(items)-observed, gaps=[] if observed == len(items) else ['部分重点观察股缺少原参考价或当日收盘证据'],
            coverage={'watchCount': len(items), 'observedCount': observed, 'pct': round(100*observed/len(items), 4)},
            summary=f'市场重点观察{len(items)}只，已核验{observed}只收盘价格变化，{len(items)-observed}只待补证据；不计入规则精选收益。')
        db.execute('INSERT OR REPLACE INTO brief_feedback VALUES(?,?,?)', (day, at, _json(result)))
        return result

    def _read_brief(self, db, now, include_today):
        if not self._has_brief_tables(db):
            return _empty_brief(now)
        operator = '<=' if include_today else '<'
        row = db.execute('SELECT day,payload FROM brief_feedback WHERE day'+operator+'? AND as_of<=? ORDER BY day DESC,as_of DESC LIMIT 1',
                         (now.date().isoformat(), now.isoformat())).fetchone()
        if not row:
            return _empty_brief(now)
        if not self._fresh(row['day'], now):
            return _empty_brief(now, 'expired', ['重点观察反馈超过5个交易日或7个自然日'])
        return json.loads(row['payload'])

    @staticmethod
    def _pool(run):
        channels = run.get('opportunityChannels') or {}
        selected = {_code(r.get('code')) for r in channels.get('recommendations') or []}
        pool = run.get('themeResearchUniverse')
        complete = isinstance(pool, list)
        if not complete:
            pool = [r for field in ('researchCandidates', 'observations', 'recommendations')
                    for r in channels.get(field) or []]
        result = {}
        for raw in pool:
            if not isinstance(raw, dict) or not (code := _code(raw.get('code'))):
                continue
            theme = (raw.get('opportunity') or {}).get('theme') or {}
            ids = raw.get('themeIds')
            if not isinstance(ids, list):
                ids = [theme.get('id')] if isinstance(theme, dict) and theme.get('id') else []
            result.setdefault(code, {**raw, 'code': code,
                'themeIds': sorted({str(t) for t in ids if t}),
                'selected': code in selected or raw.get('selected') is True,
                'eligibleUniverse': raw.get('eligibleUniverse') is not False,
                'researched': raw.get('researched') is True or not complete})
        return list(result.values()), complete

    def save_scan(self, run, now):
        now = _now(now)
        raw_decision = ((run.get('opportunityChannels') or {}).get('asOf') or run.get('generatedAt')
                        or run.get('asOf') or run.get('as_of') or run.get('cutoff_at'))
        decision = _stamp(raw_decision) if raw_decision else now
        if decision is None or decision > now or decision.date() != now.date() or now.date() not in self._sessions(now.date(), now.date()):
            return {'status': 'rejected', 'gaps': ['非核验交易日或扫描时间无效']}
        pool, complete = self._pool(run)
        board = run.get('themeBoard') or {}
        themes, gaps = [], []
        if not complete:
            gaps.append('未提供完整候选池，不能衡量未扫描股票的漏选')
        for raw in board.get('themes') or []:
            stamp = _stamp(raw.get('sourceTime'))
            if not raw.get('id') or stamp is None or stamp.date() != decision.date() or not 0 <= (decision-stamp).total_seconds() <= 300:
                gaps.append('题材榜缺少同日有效源时间')
                continue
            themes.append({k: raw.get(k) for k in ('id', 'name', 'rank', 'heatScore', 'changePct',
                'breadthPct', 'selected', 'sourceTime', 'memberCodes', 'coverage')})
            for key in ('rank', 'heatScore', 'changePct', 'breadthPct'):
                themes[-1][key] = _number(themes[-1][key])
        members = defaultdict(list)
        for theme in themes:
            for code in theme.get('memberCodes') or []:
                members[str(code)].append(str(theme['id']))
        frozen = []
        for row in pool:
            quote, issues = _quote(row.get('quote') or row, row['code'], decision)
            frozen.append({'code': row['code'], 'name': row.get('name') or row['code'],
                'quote': quote, 'decisionAt': decision.isoformat(), 'knownAt': now.isoformat(),
                'themeIds': row['themeIds'] or members[row['code']], 'selected': row['selected'],
                'researched': row['researched'], 'eligibleUniverse': row['eligibleUniverse'], 'gaps': issues})
        payload = {'asOf': decision.isoformat(), 'knownAt': now.isoformat(), 'sourceDay': now.date().isoformat(),
                   'themes': themes, 'universe': frozen, 'completeUniverse': complete, 'gaps': gaps,
                   'boardMeta': {k: board.get(k) for k in ('version', 'status', 'asOf', 'sourceTime', 'source')}}
        scan_id = str(run.get('run_id') or run.get('runId') or sha256(_json(payload).encode()).hexdigest())
        day, at = decision.date().isoformat(), now.isoformat()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            added = db.execute('INSERT OR IGNORE INTO scans VALUES(?,?,?,?,?)',
                (scan_id, day, decision.isoformat(), at, _json(payload))).rowcount
            if added:
                for row in frozen:
                    if not row['gaps']:
                        db.execute('INSERT OR IGNORE INTO baselines VALUES(?,?,?,?)',
                            (day, row['code'], at, _json(row)))
                brief_archive = self._save_brief(db, run, now, decision)
            else:
                brief_archive = {'status': 'already_saved'}
        return {'status': 'saved' if added else 'already_saved', 'scanId': scan_id,
                'universeCount': len(frozen), 'validReferenceCount': sum(not r['gaps'] for r in frozen),
                'marketBriefArchive': brief_archive, 'gaps': gaps}

    def pending_codes(self, now, limit=500):
        """Only today's references can settle with today's current quotes."""
        now = _now(now)
        if not self.path.is_file() or now.date() not in self._sessions(now.date(), now.date()) or now.time() < time(15):
            return []
        with self._db(write=False) as db:
            brief_codes = []
            if self._has_brief_tables(db):
                pending = db.execute('''SELECT b.code,b.payload FROM brief_references b LEFT JOIN brief_closes c
                    ON c.day=b.day AND c.code=b.code AND c.known_at<=? WHERE b.day=? AND b.known_at<=?
                    AND c.code IS NULL ORDER BY b.code LIMIT ?''',
                    (now.isoformat(), now.date().isoformat(), now.isoformat(), min(2000, max(0, int(limit))))).fetchall()
                for row in pending:
                    reference = json.loads(row['payload'])
                    known = _stamp(reference.get('referenceKnownAt'))
                    if reference.get('referenceQuote') and known and known <= now:
                        brief_codes.append(row['code'])
            rows = db.execute('''SELECT b.code FROM baselines b LEFT JOIN closes c
                ON c.day=b.day AND c.code=b.code AND c.known_at<=? WHERE b.day=? AND b.known_at<=?
                AND c.code IS NULL ORDER BY b.code LIMIT ?''',
                (now.isoformat(), now.date().isoformat(), now.isoformat(), min(2000, max(0, int(limit))))).fetchall()
        return list(dict.fromkeys([*brief_codes, *(r['code'] for r in rows)]))[:min(2000, max(0, int(limit)))]

    def _adjustments(self, db, now, *, before_day=None):
        rows = db.execute('SELECT day,as_of,payload FROM feedback WHERE as_of<=? AND day>=? ORDER BY day DESC,as_of DESC',
                          (now.isoformat(), (now.date()-timedelta(days=7)).isoformat())).fetchall()
        seen, observations = set(), defaultdict(list)
        for row in rows:
            day = row['day']
            if day in seen or (before_day and day >= before_day) or not self._fresh(day, now):
                continue
            seen.add(day)
            for theme in json.loads(row['payload']).get('themes') or []:
                if theme.get('status') == 'observed':
                    observations[str(theme['id'])].append((day, theme.get('weakened') is True))
        eligible = {key: values for key, values in observations.items()
                    if len(values) >= 3 and sum(v[1] for v in values) >= 2
                    and sum(v[1] for v in values)/len(values) >= .5}
        return ({key: round(min(.1, .02*sum(v[1] for v in values)), 4) for key, values in eligible.items()},
                {key: [v[0] for v in values] for key, values in eligible.items()})

    def review(self, now, quotes, theme_board=None):
        now = _now(now)
        day, at = now.date().isoformat(), now.isoformat()
        if now.date() not in self._sessions(now.date(), now.date()) or now.time() < time(15):
            return {**_empty(now, 'unavailable', ['尚未收盘或非核验交易日']),
                    'marketBriefFeedback': _empty_brief(now, 'unavailable', ['尚未收盘或非核验交易日'])}
        if not self.path.is_file():
            return {**_empty(now, 'no_scans', ['当日没有冻结扫描']), 'marketBriefFeedback': _empty_brief(now)}
        quotes = quotes if isinstance(quotes, dict) else {}
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            brief_feedback = self._review_brief(db, now, quotes)
            scans = [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM scans WHERE day=? AND known_at<=? ORDER BY known_at,decision_at,scan_id', (day, at))]
            if not scans:
                return {**_empty(now, 'no_scans', ['当日没有冻结扫描']), 'marketBriefFeedback': brief_feedback}
            baselines = [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM baselines WHERE day=? AND known_at<=? ORDER BY code', (day, at))]
            for row in baselines:
                code = row['code']
                quote, gaps = _quote(quotes.get(code), code, now, close=True)
                if not gaps and _stamp(quote['source_time']) >= _stamp(row['decisionAt']):
                    db.execute('INSERT OR IGNORE INTO closes VALUES(?,?,?,?)', (day, code, at, _json(quote)))
            closes = {r['code']: json.loads(r['payload']) for r in db.execute(
                'SELECT code,payload FROM closes WHERE day=? AND known_at<=?', (day, at))}
            observations = {}
            for row in baselines:
                close = closes.get(row['code'])
                observations[row['code']] = {**row, 'closeQuote': close,
                    'observationChangePct': round((close['price']/row['quote']['price']-1)*100, 6) if close else None}
            feedback = self._describe(scans, observations, now, theme_board)
            feedback['marketBriefFeedback'] = brief_feedback
            db.execute('INSERT OR REPLACE INTO feedback VALUES(?,?,?)', (day, at, _json(feedback)))
            feedback['adjustments'], feedback['adjustmentSourceDays'] = self._adjustments(db, now)
            db.execute('UPDATE feedback SET payload=? WHERE day=? AND as_of=?', (_json(feedback), day, at))
        return feedback

    @staticmethod
    def _describe(scans, observations, now, close_board):
        themes, first_board, missed = [], {}, []
        ever_selected = {r['code'] for scan in scans for r in scan['universe'] if r['selected']}
        for scan in scans:
            for theme in scan['themes']:
                theme_id = str(theme['id'])
                member_codes = set(theme.get('memberCodes') or [])
                captured = any(r['code'] in observations and observations[r['code']]['decisionAt'] == scan['asOf']
                               and (r['code'] in member_codes or theme_id in r['themeIds']) for r in scan['universe'])
                if theme.get('selected') is not True and not captured:
                    continue
                previous = first_board.get(theme_id)
                if previous is None or (theme.get('selected') is True and previous[0].get('selected') is not True):
                    first_board[theme_id] = (theme, scan)
        close_themes = {str(t.get('id')): t for t in (close_board or {}).get('themes') or []}
        for theme_id, (theme, scan) in first_board.items():
            selected = theme.get('selected') is True
            members = set(theme.get('memberCodes') or [])
            membership = theme.get('coverage') if isinstance(theme.get('coverage'), dict) else {}
            full_membership = bool(selected and members and membership.get('complete') is True
                                   and membership.get('expectedCount') == len(members))
            # An observed candidate subset never proves complete theme breadth.
            codes = members if full_membership else {r['code'] for r in scan['universe']
                if r['code'] in members or theme_id in r['themeIds']}
            rows = [observations[c] for c in sorted(codes) if c in observations
                    and observations[c]['decisionAt'] == scan['asOf']
                    and observations[c]['observationChangePct'] is not None]
            coverage = len(rows)/len(codes) if full_membership else None
            valid = bool(full_membership and len(rows) >= 3 and coverage >= .8)
            changes = [r['observationChangePct'] for r in rows]
            average = mean(changes) if changes else None
            breadth = sum(v > 0 for v in changes)/len(changes)*100 if changes else None
            weakened = bool(valid and average < 0 and breadth < 50)
            chosen = [r for r in rows if r['selected']]
            chosen_mean = mean(r['observationChangePct'] for r in chosen) if chosen else None
            attribution = []
            if weakened:
                attribution.append({'code': 'theme_weakened', 'label': '题材走弱', 'evidence': '同窗成员参考价后平均下跌且上涨占比不足50%'})
            if valid and chosen_mean is not None and chosen_mean < average-.5:
                attribution.append({'code': 'selection_lagged', 'label': '同题材首轮选股落后', 'evidence': '首轮入选组同窗价格变化落后题材成员均值超过0.5个百分点'})
            pullbacks = [r['code'] for r in chosen if r['observationChangePct'] < 0]
            if pullbacks:
                attribution.append({'code': 'reference_pullback', 'label': '首轮入场参考后回落（非实际成交）', 'codes': pullbacks})
            if valid and scan['completeUniverse']:
                benchmark = chosen_mean if chosen_mean is not None else average
                for row in rows:
                    if (row['code'] not in ever_selected and row.get('eligibleUniverse') is not False
                            and row['observationChangePct'] >= 3 and row['observationChangePct'] >= benchmark+1):
                        missed.append({'themeId': theme_id, 'code': row['code'], 'name': row['name'],
                            'observationChangePct': row['observationChangePct'], 'researched': row['researched'],
                            'decisionAt': row['decisionAt'], 'referenceQuote': row['quote'], 'closeQuote': row['closeQuote'],
                            'label': '漏选强股（已冻结候选池内）'})
                        attribution.append({'code': 'missed_leader', 'label': '漏选强股（已冻结候选池内）', 'stockCode': row['code']})
            close = close_themes.get(theme_id) or {}
            close_time = _stamp(close.get('sourceTime'))
            close_valid = bool(close_time and close_time.date() == now.date() and close_time <= now
                               and time(14, 57) <= close_time.time() <= time(15, 5))
            themes.append({'id': theme_id, 'name': theme.get('name'), 'selected': theme.get('selected') is True,
                'status': 'observed' if valid else 'insufficient' if selected else 'sample_only', 'referenceAt': scan['asOf'],
                'scope': 'full_theme' if full_membership else 'candidate_observations',
                'scopeLabel': '完整题材成员' if full_membership else '已冻结候选样本（非全题材）',
                'memberCount': len(codes), 'observedCount': len(rows),
                'coverage': round(coverage, 4) if coverage is not None else None,
                'meanObservationChangePct': round(average, 6) if average is not None else None,
                'positiveMemberPct': round(breadth, 4) if breadth is not None else None,
                'selectedMeanChangePct': round(chosen_mean, 6) if chosen_mean is not None else None,
                'selectionScope': '首轮同窗入选组；后来入选不改写首轮参考价，且不记为当日漏选',
                'laterSelectedCodes': [r['code'] for r in rows if not r['selected'] and r['code'] in ever_selected],
                'weakened': weakened, 'continuation': 'weakened' if weakened else 'held' if valid and average >= 0 else 'mixed' if valid else 'unknown',
                'initialRank': theme.get('rank'), 'closeRank': close.get('rank') if close_valid else None,
                'closeSourceTime': close_time.isoformat() if close_valid else None, 'attributions': attribution})
        all_codes = {r['code'] for s in scans for r in s['universe']}
        missing_codes = sorted(c for c in all_codes if c not in observations or observations[c]['closeQuote'] is None)
        verified = sum(r['closeQuote'] is not None for r in observations.values())
        gaps = list(dict.fromkeys(g for scan in scans for g in scan['gaps']))
        if not first_board:
            gaps.append('当日没有可核验的冻结题材榜')
        if missing_codes:
            gaps.append(f'{len(missing_codes)}只股票缺少冻结有效参考价或同日收盘证据')
        if any(t['status'] == 'insufficient' for t in themes):
            gaps.append('部分已选主题缺完整成员证据、同窗成员不足3只或覆盖不足80%，不用于调整')
        result = _empty(now, 'reviewed' if verified and not missing_codes else 'partial' if verified else 'pending_data', gaps)
        result.update(sourceDay=now.date().isoformat(), themes=themes,
            missedLeaders=sorted(missed, key=lambda r: (-r['observationChangePct'], r['code'], r['themeId'])),
            observedCount=verified, universeCount=len(all_codes), missingCodes=missing_codes,
            summary=f'已核验{verified}/{len(all_codes)}只价格观察；{sum(t["weakened"] for t in themes)}个题材走弱，{len(missed)}条候选池内漏选强股记录。')
        return result

    def read_context(self, now, *, include_today=False):
        now = _now(now)
        day, at = now.date().isoformat(), now.isoformat()
        if not self.path.is_file():
            brief = _empty_brief(now)
            return {'history': [], 'feedback': {**_empty(now), 'marketBriefFeedback': brief}, 'marketBriefFeedback': brief}
        with self._db(write=False) as db:
            day_operator = '<=' if include_today else '<'
            rows = db.execute('SELECT day,as_of,payload FROM feedback WHERE day'+day_operator+'? AND as_of<=? ORDER BY day DESC,as_of DESC', (day, at)).fetchall()
            allowed_statuses = ('reviewed', 'partial', 'pending_data', 'no_scans', 'unavailable') if include_today else ('reviewed', 'partial')
            rows = [r for r in rows if json.loads(r['payload']).get('status') in allowed_statuses]
            feedback = _empty(now)
            if rows:
                latest = rows[0]
                if self._fresh(latest['day'], now):
                    feedback = json.loads(latest['payload'])
                    evidence = feedback.get('adjustmentSourceDays') or {}
                    feedback['adjustments'] = {key: value for key, value in feedback['adjustments'].items()
                        if len(set(evidence.get(key) or [])) >= 3
                        and all((d <= day if include_today else d < day) and self._fresh(d, now) for d in evidence[key])}
                    if feedback.get('status') not in ('reviewed', 'partial'):
                        feedback['adjustments'] = {}
                else:
                    feedback = _empty(now, 'expired', ['最近反馈超过5个交易日或7个自然日'])
            scans = db.execute('SELECT day,payload FROM scans WHERE day<? AND known_at<=? AND day>=? ORDER BY day DESC,known_at DESC,scan_id',
                               (day, at, (now.date()-timedelta(days=7)).isoformat())).fetchall()
            seen, history = set(), []
            for row in scans:
                if row['day'] not in seen and self._fresh(row['day'], now):
                    seen.add(row['day'])
                    scan = json.loads(row['payload'])
                    history.append({**(scan.get('boardMeta') or {}),
                        'asOf': (scan.get('boardMeta') or {}).get('asOf') or scan['asOf'],
                        **{k: scan[k] for k in ('knownAt', 'sourceDay', 'themes')}})
            brief = self._read_brief(db, now, include_today)
            feedback['marketBriefFeedback'] = brief
        return {'history': history, 'feedback': feedback, 'marketBriefFeedback': brief}
