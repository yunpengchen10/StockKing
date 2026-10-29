"""Prospective daily reference observations, isolated from executable/ML labels.

This module never fetches prices, assumes fills, estimates costs, or writes the
minute training tables. Missing references and incomplete calendars stay unknown.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
import zlib
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
HORIZONS = (1, 3, 5)
MEANING = '未复权参考价至后续收盘的变化；非成交收益，成交及成本未知，净收益为空；不进入分钟模型训练'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def _at(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return stamp.astimezone(TZ)


def _number(value):
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def init_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS daily_observation_signals(
            observation_id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,
            trading_day TEXT NOT NULL, task_id TEXT NOT NULL, task_version TEXT NOT NULL,
            first_run_id TEXT NOT NULL, first_category TEXT NOT NULL,
            reference_price REAL, reference_at TEXT NOT NULL, reference_source_time TEXT,
            reference_source TEXT, reference_verified INTEGER NOT NULL,
            reference_gaps_json TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(code,trading_day,task_id,task_version)
        );
        CREATE TABLE IF NOT EXISTS daily_observation_appearances(
            observation_id TEXT NOT NULL REFERENCES daily_observation_signals(observation_id),
            run_id TEXT NOT NULL, observed_at TEXT NOT NULL, categories_json TEXT NOT NULL,
            PRIMARY KEY(observation_id,run_id)
        );
        CREATE TABLE IF NOT EXISTS daily_observation_outcomes(
            observation_id TEXT NOT NULL REFERENCES daily_observation_signals(observation_id),
            horizon INTEGER NOT NULL CHECK(horizon IN (1,3,5)), target_day TEXT,
            status TEXT NOT NULL, observed_close REAL, gross_reference_change_pct REAL,
            net_return REAL CHECK(net_return IS NULL), label_end_at TEXT, label_known_at TEXT,
            reviewed_at TEXT NOT NULL, details_json TEXT NOT NULL,
            PRIMARY KEY(observation_id,horizon)
        );
        CREATE INDEX IF NOT EXISTS daily_observation_appearance_run ON daily_observation_appearances(run_id);
        CREATE INDEX IF NOT EXISTS daily_observation_signal_created ON daily_observation_signals(created_at);
        CREATE INDEX IF NOT EXISTS daily_observation_signal_recent ON daily_observation_signals(trading_day DESC,reference_at,code);
        CREATE INDEX IF NOT EXISTS daily_observation_signal_version ON daily_observation_signals(task_version,trading_day);
    ''')
    from .daily_observation_queue import init_schema as init_queue
    init_queue(db)


def _records(result):
    """Merge simultaneous exposures; never relabel the first frozen cohort later."""
    records = {}
    # Only a quote captured in this scan may enrich a compact rejection row.
    # Never fetch a replacement or repair an older frozen reference here.
    quote_map = result.get('dailyObservationQuotes') or {}
    assessment = result.get('dailyAssessment') or (result.get('dataQuality') or {}).get('daily_selection') or {}
    version = assessment.get('version')
    task = assessment.get('taskId') or 'daily_structure_to_next_session'

    def add(row, category, *, fallback=False):
        if not isinstance(row, dict) or not re.fullmatch(r'\d{6}', str(row.get('code', ''))):
            return
        if not row.get('quote') and isinstance(quote_map, dict):
            quote = quote_map.get(row['code'])
            if isinstance(quote, dict):
                row = {**row, 'quote': dict(quote)}
        signal = row.get('dailySignal') or {}
        row_version = signal.get('version') or row.get('version') or version
        row_task = signal.get('taskId') or row.get('taskId') or task
        if not row_version or not (signal or fallback or row.get('observationOnly')):
            return
        if signal.get('selected') is True and not row.get('observationOnly'):
            category = 'selected'
        key = (row['code'], str(row_task), str(row_version))
        old = records.setdefault(key, {'row': row, 'categories': set()})
        old['categories'].add(category)
        # Prefer a full quote over a compact rejection/audit row in this scan.
        if row.get('quote') and not old['row'].get('quote'):
            old['row'] = row

    for row in result.get('dailyObservationResearch') or []:
        add(row, 'unselected_control', fallback=True)
    for field, category in (('candidates', 'selected'), ('dailyCandidates', 'observation_watchlist'),
                            ('dailyAttackWatchlist', 'attack_watchlist'), ('dailyRejectionLedger', 'unselected_control')):
        for row in result.get(field) or []:
            add(row, category, fallback=field in {'dailyAttackWatchlist', 'dailyRejectionLedger'})
    for field in ('profileCandidates', 'dailyProfileCandidates'):
        for rows in (result.get(field) or {}).values():
            for row in rows or []:
                add(row, 'observation_watchlist' if row.get('observationOnly') else 'profile_selected')
    channels = result.get('opportunityChannels') or {}
    selected_opportunities = {row.get('code') for row in channels.get('recommendations') or []}
    # An earlier observation is not a delivered recommendation. Separate tasks
    # freeze each first reference so promotion cannot inherit a cheaper control
    # price. Subsequent E->M changes do not reset the recommendation reference.
    for field, category, task_id in (
            ('researchCandidates', 'opportunity_control', 'opportunity_em_control'),
            ('recommendations', 'opportunity_recommendation', 'opportunity_em_recommendation')):
        for original in channels.get(field) or []:
            if not isinstance(original, dict) or not channels.get('version'):
                continue
            if category == 'opportunity_control' and original.get('code') in selected_opportunities:
                continue
            row = {**original, 'taskId': task_id, 'version': channels['version'],
                   'observationOnly': True, 'formalEligible': False}
            # Preserve the opportunity's own task, never inherit a legacy daily
            # or minute signal accidentally copied with its source evidence.
            row.pop('dailySignal', None)
            add(row, category, fallback=True)
    for value in records.values():
        categories = value['categories']
        # A selected/watchlist row is not simultaneously an unselected control.
        if categories - {'unselected_control'}:
            categories.discard('unselected_control')
        if value['row'].get('observationOnly'):
            categories.discard('selected')
            categories.discard('profile_selected')
            if not categories:
                categories.add('observation_watchlist')
    return records


def persist_scan(db, result, run_id, trading_day, published_at):
    """Only called inside the newly inserted run transaction; no historic backfill."""
    published_at = _at(published_at)
    saved = 0
    priority = ('opportunity_recommendation', 'opportunity_control', 'selected', 'profile_selected',
                'observation_watchlist', 'attack_watchlist', 'unselected_control')
    for (code, task, version), record in _records(result).items():
        row, categories = record['row'], record['categories']
        quote = row.get('quote') or {}
        price = _number(row.get('referencePrice') or row.get('signalPrice') or quote.get('price'))
        source = quote.get('source') or row.get('quoteSource')
        source_time = quote.get('provider_timestamp') or quote.get('source_time') or row.get('sourceTime') or row.get('quoteSourceTime')
        gaps = []
        try:
            decision = _at(row.get('decision_at') or published_at)
            source_at = _at(source_time)
            if decision > published_at or decision.date().isoformat() != trading_day or published_at.date().isoformat() != trading_day:
                gaps.append('decision_or_publication_outside_signal_session')
            if not 0 <= (published_at - source_at).total_seconds() <= 30:
                gaps.append('reference_source_not_fresh_at_publication')
        except (TypeError, ValueError):
            decision = published_at
            gaps.append('reference_timestamp_missing_or_invalid')
        quote_price = _number(quote.get('price'))
        if not price or not quote_price or abs(price - quote_price) > 1e-6:
            gaps.append('reference_price_or_matching_quote_missing')
        if not source:
            gaps.append('reference_source_missing')
        if quote.get('code') and str(quote['code']) != code:
            gaps.append('reference_security_mismatch')
        if quote.get('is_stale') or quote.get('corporate_action') or quote.get('quote_usable_for_current_price_check') is False:
            gaps.append('reference_quote_unusable')
        observation_id = sha256(_json([code, trading_day, task, version]).encode()).hexdigest()[:32]
        first_category = next(category for category in priority if category in categories)
        snapshot = {key: row[key] for key in ('quote', 'dailySignal', 'dailyProfileDecision', 'dailyRankEvidence',
                    'marketBreadth', 'eventAssessment', 'state', 'reasons', 'observationOnly', 'formalEligible',
                    'opportunity', 'discoveryEvidence', 'ruleRecommendation', 'dataLineage') if key in row}
        cursor = db.execute('''INSERT OR IGNORE INTO daily_observation_signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (observation_id, code, str(row.get('name') or code), trading_day, task, version, run_id, first_category,
             price, decision.isoformat(), source_time, source, int(not gaps), _json(gaps), _json(snapshot), published_at.isoformat()))
        saved += cursor.rowcount
        db.execute('INSERT OR IGNORE INTO daily_observation_appearances VALUES(?,?,?,?)',
                   (observation_id, run_id, published_at.isoformat(), _json(sorted(categories))))
    return saved


def read_verified_close(data_dir, code, day, as_of):
    """Read existing archive/independent daily anchors without creating a DB.

    Labels may use verification learned after the signal, but never verification
    learned after the review cutoff. Raw quality flags alone are insufficient.
    """
    path = Path(data_dir) / 'minute_history' / 'minute-bars-v2.sqlite3'
    if not path.is_file():
        return {'status': 'missing_close', 'gaps': ['local_minute_archive_missing']}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT s.payload,q.status,q.verification_json FROM sessions s JOIN session_quality q '
                             'ON s.code=q.code AND s.day=q.day WHERE s.code=? AND s.day=?', (code, day)).fetchone()
        if not row:
            return {'status': 'missing_close', 'gaps': ['session_or_quality_missing']}
        if row[1] != 'verified':
            return {'status': 'unverified_close', 'gaps': ['session_quality_not_verified']}
        bars, anchor = json.loads(zlib.decompress(row[0])), json.loads(row[2] or '{}')
        from .minute_integrity import inspect_session
        quality = inspect_session(code, day, bars, anchor)
        if quality['status'] != 'verified':
            return {'status': 'unverified_close', 'gaps': quality['reasonCodes']}
        observed_at, known_at = _at(anchor.get('observed_at')), _at(anchor.get('known_at'))
        close_at = datetime.combine(date.fromisoformat(day), time(15), TZ)
        if not close_at <= observed_at <= known_at <= _at(as_of):
            return {'status': 'unverified_close', 'gaps': ['verification_not_known_at_review_cutoff']}
        if anchor.get('source_time') and _at(anchor['source_time']) > observed_at:
            return {'status': 'unverified_close', 'gaps': ['verification_source_time_after_observation']}
        if any(bar.get('corporate_action') is True for bar in bars) or anchor.get('corporate_action') is True:
            return {'status': 'unverified_close', 'gaps': ['corporate_action_price_basis_unverified']}
        # A trusted complete daily anchor also covers a sparse minute archive.
        if anchor.get('kind') == 'daily_ohlc' and anchor.get('complete') is True:
            close, evidence = _number(anchor.get('close')), 'verified_complete_daily_ohlc'
        else:
            stamps = {_at(bar['end']) for bar in bars}
            expected = {datetime.combine(date.fromisoformat(day), start, TZ) + timedelta(minutes=offset)
                        for start in (time(9, 31), time(13, 1)) for offset in range(120)}
            if stamps != expected:
                return {'status': 'missing_close', 'gaps': ['complete_240_minute_session_missing']}
            last = max(bars, key=lambda bar: _at(bar['end']))
            close, evidence = _number(last.get('close')), 'verified_complete_minute_session'
        if not close:
            return {'status': 'unverified_close', 'gaps': ['positive_close_missing']}
        return {'status': 'verified', 'close': close, 'source': anchor.get('source'),
                'source_time': anchor.get('source_time'), 'known_at': known_at.isoformat(),
                'close_at': close_at.isoformat(), 'evidence': evidence, 'price_basis': 'unadjusted',
                'session_payload_sha256': sha256(row[0]).hexdigest(), 'verification': anchor, 'gaps': []}
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError, zlib.error):
        return {'status': 'unverified_close', 'gaps': ['local_close_evidence_invalid']}


def evaluate_reference(service, signal, horizon, as_of, calendars, closes):
    """Compute one label without writes, network calls, or execution assumptions."""
    start = date.fromisoformat(signal['trading_day'])
    if start not in calendars:
        calendars[start] = service._sessions(service.calendar_provider, start, start + timedelta(days=60))
    sessions = calendars[start]
    target = sessions[horizon] if sessions and sessions[0] == start and len(sessions) > horizon else None
    end = datetime.combine(target, time(15), TZ) if target else None
    status, gaps, close, gross, known, evidence = 'pending', [], None, None, None, {}
    if target is None:
        status, gaps = 'calendar_unavailable', ['exact_exchange_sessions_unavailable']
    elif end > as_of:
        status, gaps = 'not_matured', ['target_close_not_reached']
    elif not signal['reference_verified']:
        status, gaps = 'unverified_reference', json.loads(signal['reference_gaps_json'])
    else:
        key = (signal['code'], target.isoformat())
        if key not in closes:
            closes[key] = read_verified_close(service.data_dir, *key, as_of)
        evidence = closes[key]
        status, gaps = evidence['status'], evidence.get('gaps', [])
        if status == 'verified':
            status, close, known = 'observed', evidence['close'], as_of.isoformat()
            gross = (close / signal['reference_price'] - 1) * 100
    details = {'gaps': gaps, 'closeEvidence': evidence, 'meaning': MEANING,
               'is_trade_return': False, 'training_eligible': False, 'costs_known': False,
               'reference_frozen': True, 'label_kind': 'gross_reference_change', 'price_basis': 'unadjusted'}
    return (signal['observation_id'], horizon, target.isoformat() if target else None, status, close, gross,
            None, end.isoformat() if end else None, known, as_of.isoformat(), _json(details))


def review(service, as_of, *, max_labels=300, budget_seconds=4.0, retry_seconds=0):
    """Explicit bounded write operation; GET reports never call this worker."""
    from .daily_observation_queue import run
    return run(service, as_of, max_labels=max_labels, budget_seconds=budget_seconds, retry_seconds=retry_seconds)


def review_requirements(service, as_of):
    """Capture only due target sessions; today's reference needs no minute fill."""
    as_of = _at(as_of)
    with service._connect() as db:
        signals = db.execute('SELECT * FROM daily_observation_signals WHERE created_at<=? AND reference_verified=1',
                             (as_of.isoformat(),)).fetchall()
        completed = {(row[0], row[1]) for row in db.execute(
            "SELECT observation_id,horizon FROM daily_observation_outcomes WHERE status='observed' AND reviewed_at<=?",
            (as_of.isoformat(),))}
    calendars, needed = {}, defaultdict(set)
    for signal in signals:
        day = date.fromisoformat(signal['trading_day'])
        if day not in calendars:
            calendars[day] = service._sessions(service.calendar_provider, day, day + timedelta(days=60))
        sessions = calendars[day]
        for horizon in HORIZONS:
            if (sessions and sessions[0] == day and len(sessions) > horizon
                    and sessions[horizon] <= as_of.date() and (signal['observation_id'], horizon) not in completed):
                needed[signal['code']].add(sessions[horizon].isoformat())
    return {code: sorted(days) for code, days in needed.items()}


def reviews(service, *, as_of=None, limit=500, filters=None):
    """Bound detail decoding in SQL; coverage aggregates the full filtered set."""
    filters = dict(filters or {})
    cutoff = _at(as_of or service.clock()).isoformat()
    limit = max(1, min(int(filters.get('limit') or limit), 5000))
    where, args = ['s.created_at<=?'], [cutoff]
    for field, column in (('date', 'trading_day'), ('version', 'task_version')):
        if filters.get(field):
            where.append(f's.{column}=?')
            args.append(str(filters[field]))
    if filters.get('symbol'):
        code = str(filters['symbol']).split('.')[0].strip()
        where.append('s.code=?')
        args.append(code if re.fullmatch(r'\d{6}', code) else '__invalid__')
    horizons = [int(filters['horizon'])] if filters.get('horizon') in HORIZONS else list(HORIZONS)
    clause = ' WHERE ' + ' AND '.join(where)
    ordered = ' FROM daily_observation_signals s' + clause + ' ORDER BY s.trading_day DESC,s.reference_at,s.code LIMIT ?'
    chosen = 'WITH chosen AS (SELECT s.observation_id' + ordered + ') '
    horizon_sql = ','.join('?' for _ in horizons)
    outcomes, appearances, groups = defaultdict(list), defaultdict(list), {}
    with service._connect() as db:
        total = db.execute('SELECT COUNT(*) FROM daily_observation_signals s' + clause, args).fetchone()[0]
        signals = [dict(row) for row in db.execute('SELECT s.*' + ordered, (*args, limit))] if total else []
        # An empty observation DB performs no child scans or JSON decoding.
        for row in db.execute(chosen + 'SELECT o.* FROM daily_observation_outcomes o JOIN chosen c '
                              'ON c.observation_id=o.observation_id WHERE o.reviewed_at<=? AND o.horizon IN ('
                              + horizon_sql + ') ORDER BY o.horizon', (*args, limit, cutoff, *horizons)) if signals else ():
            item = dict(row)
            item['details'] = json.loads(item.pop('details_json'))
            outcomes[item['observation_id']].append(item)
        for row in db.execute(chosen + 'SELECT a.* FROM daily_observation_appearances a JOIN chosen c '
                              'ON c.observation_id=a.observation_id WHERE a.observed_at<=? ORDER BY a.observed_at',
                              (*args, limit, cutoff)) if signals else ():
            item = dict(row)
            item['categories'] = json.loads(item.pop('categories_json'))
            appearances[item['observation_id']].append(item)
        aggregate = ("WITH h(horizon) AS (SELECT 1 UNION ALL SELECT 3 UNION ALL SELECT 5) "
                     "SELECT s.trading_day,s.task_id,s.task_version,s.first_category,h.horizon,"
                     "COALESCE(o.status,'not_reviewed') status,COUNT(*) signals,"
                     "SUM(CASE WHEN o.status='observed' AND o.gross_reference_change_pct>0 THEN 1 ELSE 0 END) up,"
                     "SUM(CASE WHEN o.status='observed' AND o.gross_reference_change_pct<0 THEN 1 ELSE 0 END) down,"
                     "SUM(CASE WHEN o.status='observed' AND o.gross_reference_change_pct=0 THEN 1 ELSE 0 END) flat "
                     "FROM daily_observation_signals s CROSS JOIN h LEFT JOIN daily_observation_outcomes o "
                     "ON o.observation_id=s.observation_id AND o.horizon=h.horizon AND o.reviewed_at<=?" + clause +
                     ' AND h.horizon IN (' + horizon_sql + ') GROUP BY '
                     's.trading_day,s.task_id,s.task_version,s.first_category,h.horizon,COALESCE(o.status,\'not_reviewed\')')
        for row in db.execute(aggregate, (cutoff, *args, *horizons)) if total else ():
            key = tuple(row[field] for field in ('trading_day', 'task_id', 'task_version', 'first_category', 'horizon'))
            group = groups.setdefault(key, {'signals': 0, 'covered': 0, 'up': 0, 'down': 0, 'flat': 0, 'missing': {}})
            group['signals'] += row['signals']
            if row['status'] == 'observed':
                group['covered'] += row['signals']
                for direction in ('up', 'down', 'flat'):
                    group[direction] += row[direction]
            else:
                group['missing'][row['status']] = row['signals']
    for signal in signals:
        signal['reference_gaps'] = json.loads(signal.pop('reference_gaps_json'))
        signal['snapshot'] = json.loads(signal.pop('snapshot_json'))
        signal['outcomes'] = outcomes[signal['observation_id']]
        signal['appearances'] = appearances[signal['observation_id']]
    # The health read recalculates due dates even if the worker stopped; it does
    # not create queue rows, run reviews, or fetch prices.
    from .daily_observation_queue import status as queue_status
    filtered_cohort = any(filters.get(key) for key in ('date', 'version', 'symbol'))
    health = queue_status(service, cutoff) if total or filtered_cohort else {'signalCount': 0, 'dueCount': 0,
        'overdueCount': 0, 'missingDataCount': 0, 'readOnly': True, 'trainingEligible': False}
    return {'items': signals, 'count': len(signals), 'total': total, 'limit': limit,
            'truncated': total > len(signals), 'asOf': cutoff, 'groupScope': 'all_filtered_signals',
            'groups': [dict(zip(('trading_day', 'task_id', 'task_version', 'first_category', 'horizon'), key), **value)
                       for key, value in groups.items()], 'health': health,
            'healthScope': 'all_daily_observations', 'meaning': MEANING, 'trainingEligible': False,
            'scope': '仅新增保存运行；按首次曝光类别分组，后续选入不会重置参考价或重分类对照；非全市场覆盖'}
