"""Dated one-minute bars, a persistent archive and exact same-clock baselines.

All timestamps are bar END times; amounts are CNY and volumes are shares.
Never splice cumulative Tencent snapshots into interval bars. Public Sina
backfills about eight sessions; the software MAC source pages older history,
with optional Tushare access as a further source. Missing sessions stay missing.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from time import monotonic, time as wall_time
import zlib
from statistics import median

import requests

from .intraday_evidence import SHANGHAI, _datetime, _prior_sessions, METRIC_DEFINITIONS
from .research_policy import DEFAULT_PARAMETERS
from .trend_evidence import compute_trend_evidence
from .minute_integrity import (DATA_QUALITY_VERSION, canonical_hash, inspect_session,
                               session_revision, read_archive_lineage, validate_archive_lineage)

FORMAL_HISTORY_DAYS = DEFAULT_PARAMETERS['history']['formalDays']
MINIMUM_HISTORY_DAYS = DEFAULT_PARAMETERS['history']['minimumScoreDays']
MINUTE_MAX_AGE = DEFAULT_PARAMETERS['freshness']['minuteSeconds']
MINUTE_PARAMS = DEFAULT_PARAMETERS['minute']

SINA_URL = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}


def bounded_fetch_map(fetch, items, *, workers=4, timeout=30):
    """Return completed values and missing inputs without waiting for slow I/O.

    Only ``workers`` calls can be in flight; queued work is never submitted after
    the deadline. Each provider must also set its own network timeout so the few
    already-running calls eventually release their connections.
    """
    items = list(items)
    deadline = monotonic() + max(0, timeout)
    if not items or timeout <= 0:
        return [], items
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    pending, completed = {}, {}
    cursor = 0
    try:
        while cursor < len(items) and len(pending) < max(1, workers):
            pending[pool.submit(fetch, items[cursor])] = cursor
            cursor += 1
        while pending:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            done, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                index = pending.pop(future)
                try:
                    completed[index] = future.result()
                except Exception:
                    pass
            while cursor < len(items) and len(pending) < max(1, workers) and monotonic() < deadline:
                pending[pool.submit(fetch, items[cursor])] = cursor
                cursor += 1
    finally:
        # A context manager would wait for every pending request on exit and turn
        # the deadline into a cosmetic timeout.
        pool.shutdown(wait=False, cancel_futures=True)
    return ([completed[i] for i in sorted(completed)],
            [item for i, item in enumerate(items) if i not in completed])


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def symbol(code):
    if not re.fullmatch(r'(?:60|68|00|30)\d{4}', str(code)):
        raise ValueError('invalid SH/SZ equity code')
    return ('sh' if code.startswith(('6',)) else 'sz') + code


def bar_session(at):
    minute = at.hour * 60 + at.minute
    return 'am' if 570 < minute <= 690 else 'pm' if 780 < minute <= 900 else None


def _retain_special_session_flags(target, *sources):
    """Never erase a known exceptional trading regime during bar normalization."""
    for key in ('corporate_action', 'no_price_limit', 'suspended'):
        values = [source.get(key) for source in (target, *sources) if isinstance(source.get(key), bool)]
        if values:
            target[key] = any(values)  # A reported exception wins over missing/false metadata.
    return target


def normalize_bars(rows, cutoff, *, conflicts=None, include_tainted=False):
    """Reject conflicting duplicates, malformed bars and future/partial minutes."""
    cutoff = _datetime(cutoff)
    rows = list(rows)
    tainted_days = {str(row.get('end', ''))[:10] for row in rows if row.get('data_quality_status') == 'tainted'}
    result, conflicted = {}, set()
    for row in rows:
        try:
            at = _datetime(row['end'])
            if not include_tainted and at.date().isoformat() in tainted_days:
                continue
            values = {key: number(row.get(key)) for key in ('open','high','low','close','amount_cny','volume_shares')}
            if at.second or at.microsecond or at > cutoff or not bar_session(at):
                continue
            if any(values[key] is None for key in ('open', 'high', 'low', 'close')):
                continue
            if not 0 < values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
                continue
            if values['amount_cny'] is not None and values['amount_cny'] < 0:
                continue
            if values['volume_shares'] is not None and values['volume_shares'] < 0:
                continue
            # A display adapter may encode an unavailable amount as zero while
            # reporting positive volume. Preserve the missing value explicitly.
            if values['amount_cny'] == 0 and values['volume_shares'] and values['volume_shares'] > 0:
                values['amount_cny'] = None
            # Unit checks catch lots/shares and thousands/yuan errors. Zero-volume
            # suspension bars may exist; an amount without volume is invalid.
            if values['volume_shares'] == 0 and values['amount_cny'] not in (None, 0):
                continue
            if values['volume_shares'] and values['amount_cny'] is not None and not values['low']-.03 <= values['amount_cny']/values['volume_shares'] <= values['high']+.03:
                continue
            cleaned = {'end':at.isoformat(), **values, 'source':row.get('source','unknown'),
                       'fetched_at':row.get('fetched_at') or row.get('fetchedAt')}
            for key in ('identity_evidence', 'integrity_revision', 'raw_data_revision', 'data_quality_version', 'data_quality_status', 'conflicting_observations'):
                if key in row:
                    cleaned[key] = row[key]
            if isinstance(row.get('sources'), list):
                cleaned['sources'] = sorted({str(source) for source in row['sources']})
            _retain_special_session_flags(cleaned, row)
            if at in result:
                economic = ('open','high','low','close','amount_cny','volume_shares')
                if any(result[at].get(key) is not None and cleaned.get(key) is not None
                       and result[at][key] != cleaned[key] for key in economic):
                    conflicted.add(at)
                else:
                    # A second source can fill an absent field only when every
                    # jointly available economic value agrees. Source labels and
                    # fetch times are provenance, not price disagreements.
                    original = result[at]
                    for key in economic:
                        if original.get(key) is None:
                            original[key] = cleaned.get(key)
                    sources = set(original.get('sources') or [original['source']])
                    sources.update(cleaned.get('sources') or [cleaned['source']])
                    if len(sources) > 1:
                        original['sources'] = sorted(sources)
                    _retain_special_session_flags(cleaned, result[at])
                    _retain_special_session_flags(result[at], cleaned)
                    if result[at].get('fetched_at') is None and cleaned['fetched_at'] is not None:
                        result[at]['fetched_at'] = cleaned['fetched_at']
            else:
                result[at] = cleaned
        except (KeyError, TypeError, ValueError):
            continue
    for at in conflicted:
        result.pop(at, None)
    if conflicts is not None:
        conflicts.update(conflicted)
    return result


def minute_coverage(rows, cutoff, required_days):
    """Inspect actual exchange-session dates; never invent a missing minute."""
    cutoff = _datetime(cutoff)
    normalized = normalize_bars(rows, cutoff)
    days = sorted({value.isoformat() if isinstance(value, date) else str(value)[:10]
                   for value in required_days})
    missing = {}
    missing_fields = {'amount_cny': 0, 'volume_shares': 0}
    expected = 0
    for day in days:
        session = date.fromisoformat(day)
        stamps = [datetime.combine(session, time(9, 31), SHANGHAI) + timedelta(minutes=i) for i in range(120)]
        stamps += [datetime.combine(session, time(13, 1), SHANGHAI) + timedelta(minutes=i) for i in range(120)]
        stamps = [at for at in stamps if at <= cutoff]
        expected += len(stamps)
        absent = [at.isoformat() for at in stamps if at not in normalized]
        if absent:
            missing[day] = absent
        for at in stamps:
            if at in normalized:
                for field in missing_fields:
                    missing_fields[field] += normalized[at].get(field) is None
    return {'requiredDays': days, 'expectedMinutes': expected,
            'missingDays': list(missing), 'missingMinutes': sum(map(len, missing.values())),
            'missingMinuteTimes': missing, 'missingFields': missing_fields,
            'pricePathComplete': expected > 0 and not missing}


def supplement_bars(primary, extra, cutoff, *, diagnostics=None):
    """Keep established bars; fill gaps/compatible fields with an audited source."""
    result = normalize_bars(primary, cutoff)
    added = enriched = disagreements = 0
    tainted_days = set()
    for at, row in normalize_bars(extra, cutoff).items():
        previous = result.get(at)
        if previous is None:
            result[at] = row
            added += 1
            continue
        compatible = normalize_bars([previous, row], cutoff)
        if at not in compatible:
            disagreements += 1  # Never overwrite a stored source with this disagreement.
            tainted_days.add(at.date().isoformat())
            previous['conflicting_observations'] = [*previous.get('conflicting_observations', []), row]
            _retain_special_session_flags(previous, row)
            continue
        merged = compatible[at]
        enriched += any(previous.get(key) is None and merged.get(key) is not None
                        for key in ('amount_cny', 'volume_shares'))
        result[at] = merged
    if diagnostics is not None:
        diagnostics.update(addedMinutes=added, enrichedMinutes=enriched,
                           conflictingOverlapMinutes=disagreements)
    return [{**row, 'data_quality_status':'tainted'} if at.date().isoformat() in tainted_days else row
            for at, row in result.items()]


@lru_cache(maxsize=256)
def _checked_session_rows(code, day, payload, verification_json, cutoff_key, quality_version):
    """Cache immutable checked bytes, never eligibility detached from its inputs.

    The database payload and independent anchor are part of the key. Callers
    still read the current quarantine flag on every access, and decode a fresh
    result so modifying returned rows cannot corrupt the cache. The size bound
    limits this to recent sessions; it is not a persistent validation shortcut.
    """
    stored = json.loads(zlib.decompress(payload))
    evidence = json.loads(verification_json) if verification_json else None
    if inspect_session(code, day, stored, evidence)['status'] == 'tainted':
        return b'[]', 'tainted', None
    cleaned = list(normalize_bars(stored, _datetime(cutoff_key)).values())
    scoped = inspect_session(code, day, cleaned, evidence)
    if scoped['status'] == 'tainted':
        return b'[]', 'tainted', None
    # Legacy sessions have no quality-row revision: compute it ONCE per day.
    revision = session_revision(code, day, cleaned)
    return (json.dumps(cleaned, separators=(',', ':'), ensure_ascii=False).encode(),
            scoped['status'], revision)


@lru_cache(maxsize=4096)
def _checked_feature_window(code, day, payload, verification_json, end_clock, quality_version):
    """Keep only the formula's 31 bars after checking the whole stored session.

    A large scan must not cache thousands of complete decoded days. Payload and
    anchor bytes are part of this bounded immutable cache; quarantine is read
    from SQLite on every call, before consulting it.
    """
    stored = json.loads(zlib.decompress(payload))
    evidence = json.loads(verification_json) if verification_json else None
    quality = inspect_session(code, day, stored, evidence)
    if quality['status'] == 'tainted':
        return b'[]'
    end = _datetime(day + 'T' + end_clock)
    start = end-timedelta(minutes=30)
    if quality['status'] == 'verified':
        # Identity and independent price bounds have covered the entire day.
        # Only these 31 bars supply amount/volume/price formula inputs; their
        # duplicate and economic-unit checks are unchanged. Normalizing the
        # other 209 bars cannot affect these windows or their lineage hashes.
        inputs = [row for row in stored if start <= _datetime(row['end']) <= end]
        cleaned = normalize_bars(inputs, end)
    else:
        # Legacy/unverified data retains the full normalization and subsequent
        # discontinuity check; removing an invalid row may expose a conflict.
        cleaned = normalize_bars(stored, _datetime(day + 'T15:00:00+08:00'))
        if inspect_session(code, day, list(cleaned.values()), evidence)['status'] == 'tainted':
            return b'[]'
    return json.dumps([row for at, row in cleaned.items() if start <= at <= end],
        ensure_ascii=False, separators=(',', ':')).encode()


def _read_feature_windows(directory, code, days, end):
    from .minute_integrity import _read_archive_records
    records, _ = _read_archive_records(directory, code, days)
    rows = []
    clock = _datetime(end).isoformat()[11:]
    for record in records:
        if record[3] == 'tainted':
            continue
        try:
            rows.extend(json.loads(_checked_feature_window(code, record[1], record[5], record[6],
                clock, DATA_QUALITY_VERSION)))
        except (KeyError, TypeError, ValueError, zlib.error):
            continue
    return rows


def read_archive_rows(directory, code, cutoff, *, lookback_days=None, required_days=None):
    """Read features/label bars without schema writes or bypassing quarantine."""
    path = Path(directory) / 'minute-bars-v2.sqlite3'
    if not path.is_file():
        return []
    cutoff = _datetime(cutoff)
    parameters = [code, cutoff.date().isoformat()]
    where = ' WHERE s.code=? AND s.day<=?'
    if lookback_days is not None:
        where += ' AND s.day>=?'
        parameters.append((cutoff-timedelta(days=max(0,int(lookback_days)))).date().isoformat())
    if required_days is not None:
        days = sorted({str(day)[:10] for day in required_days})
        if not days:
            return []
        where += ' AND s.day IN (' + ','.join('?' for _ in days) + ')'
        parameters.extend(days)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone():
            return []
        quality_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_quality'").fetchone()
        query = ('SELECT s.day,s.payload,q.status,q.revision,q.verification_json FROM sessions s LEFT JOIN session_quality q '
                 'ON q.code=s.code AND q.day=s.day' if quality_exists else
                 'SELECT s.day,s.payload,NULL,NULL,NULL FROM sessions s')
        records = db.execute(query+where+' ORDER BY s.day',parameters).fetchall()
    rows = []
    for day,payload,status,revision,verification_json in records:
        if status == 'tainted':
            continue
        try:
            # Historical session prefixes are stable across today's cutoffs.
            session_cutoff = min(cutoff, _datetime(day + 'T15:00:00+08:00'))
            checked, scoped_status, computed_revision = _checked_session_rows(
                code, day, payload, verification_json, session_cutoff.isoformat(), DATA_QUALITY_VERSION)
            if scoped_status == 'tainted':
                continue
            cleaned = json.loads(checked)
            raw_revision = revision or computed_revision
            rows.extend({**row,'raw_data_revision':raw_revision,
                         'data_quality_version':DATA_QUALITY_VERSION,'data_quality_status':scoped_status}
                        for row in cleaned)
        except (KeyError,TypeError,ValueError,zlib.error):
            continue
    # Each day was normalized above and its security/session identity checked;
    # session keys cannot overlap, so a second full-history pass is redundant.
    return rows


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class MinuteArchive:
    def __init__(self, directory):
        self.path = Path(directory) / 'minute-bars-v2.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions(code TEXT, day TEXT, payload BLOB, PRIMARY KEY(code,day))')
            db.execute('CREATE TABLE IF NOT EXISTS attempts(code TEXT, day TEXT, source TEXT, status TEXT, PRIMARY KEY(code,day,source))')
            db.execute('CREATE TABLE IF NOT EXISTS attempt_times(code TEXT, day TEXT, source TEXT, attempted_at REAL, PRIMARY KEY(code,day,source))')
            db.execute('CREATE TABLE IF NOT EXISTS session_quality(code TEXT,day TEXT,revision TEXT,status TEXT,reasons_json TEXT,updated_at TEXT,verification_json TEXT,PRIMARY KEY(code,day))')
            db.execute('CREATE TABLE IF NOT EXISTS session_evidence(evidence_id TEXT PRIMARY KEY,code TEXT,day TEXT,created_at TEXT,kind TEXT,payload BLOB,metadata_json TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_session_evidence_code_day_kind ON session_evidence(code,day,kind)')
            db.execute('CREATE TABLE IF NOT EXISTS minute_quality_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT,day TEXT,revision TEXT,event TEXT,created_at TEXT,details_json TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30, factory=_ClosingConnection)

    def read(self, code, cutoff, *, lookback_days=65):
        return read_archive_rows(self.path.parent,code,cutoff,lookback_days=lookback_days)

    def read_recent(self, code, cutoff, minutes=20):
        """Today's cached rows only, with the same whole-session quarantine."""
        rows = self.read(code, cutoff, lookback_days=0)
        return rows[-max(1, int(minutes)):]

    @staticmethod
    def _evidence(db, code, day, rows, kind, metadata=None):
        digest = canonical_hash([code, day, kind, rows, metadata])
        db.execute('INSERT OR IGNORE INTO session_evidence VALUES(?,?,?,?,?,?,?)',
                   (digest, code, day, datetime.now(SHANGHAI).isoformat(), kind,
                    zlib.compress(json.dumps(rows, ensure_ascii=False, default=str).encode()),
                    json.dumps(metadata or {}, ensure_ascii=False)))
        return digest

    @staticmethod
    def _quality(db, code, day, rows, quality, verification=None, event='ingest'):
        revision = session_revision(code, day, rows)
        now = datetime.now(SHANGHAI).isoformat()
        verification = dict(verification or {})
        if verification:
            new_anchor = 'known_at' not in verification
            verification.setdefault('known_at', now)
            if new_anchor:
                MinuteArchive._evidence(db, code, day, [], 'independent_price_anchor',
                    {'verification':verification, 'integrityRevision':rows[0].get('integrity_revision') if rows else None})
        previous = db.execute('SELECT revision,status,reasons_json FROM session_quality WHERE code=? AND day=?', (code, day)).fetchone()
        reasons = json.dumps(quality['reasonCodes'], ensure_ascii=False)
        db.execute('INSERT OR REPLACE INTO session_quality VALUES(?,?,?,?,?,?,?)',
                   (code, day, revision, quality['status'], reasons, now, json.dumps(verification or {}, ensure_ascii=False)))
        if previous != (revision, quality['status'], reasons):
            db.execute('INSERT INTO minute_quality_events(code,day,revision,event,created_at,details_json) VALUES(?,?,?,?,?,?)',
                       (code, day, revision, event, now, json.dumps(quality, ensure_ascii=False)))
        return revision

    def verify_quote(self, code, quote, cutoff):
        """Check this day's archived identity/price bounds against an independent quote.

        The quote's real source clock bounds its coverage. This does not claim a
        second complete minute path, repair quarantined data, or bless history.
        """
        symbol(code)
        cutoff = _datetime(cutoff)
        day = cutoff.date().isoformat()
        try:
            source_time = datetime.fromisoformat(str(quote.get('provider_timestamp') or quote.get('source_time') or ''))
            if (source_time.tzinfo is None or source_time > cutoff or source_time.date() != cutoff.date()
                    or str(quote.get('code', '')) != code):
                raise ValueError('invalid quote identity or clock')
            evidence = {'kind':'quote_prefix', 'code':code, 'day':day, 'source':quote.get('source'),
                'source_time':source_time.isoformat(), 'observed_at':cutoff.isoformat(),
                'price_basis':'unadjusted', 'open':quote.get('open_price', quote.get('open')),
                'high':quote.get('high'), 'low':quote.get('low'), 'close':quote.get('price')}
            if source_time.hour >= 15:
                evidence.update(kind='daily_ohlc', complete=True)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                record = db.execute('SELECT s.payload,q.status,q.verification_json FROM sessions s LEFT JOIN session_quality q '
                    'ON q.code=s.code AND q.day=s.day WHERE s.code=? AND s.day=?', (code,day)).fetchone()
                if record and record[1] != 'tainted':
                    rows = json.loads(zlib.decompress(record[0]))
                    quality = inspect_session(code, day, rows, evidence)
                    # Invalid/unusable anchors never displace usable earlier evidence.
                    if quality['status'] == 'tainted' or quality.get('verifiedThrough'):
                        old_evidence = json.loads(record[2]) if record[2] else {}
                        old_quality = inspect_session(code, day, rows, old_evidence)
                        if (quality['status'] == 'tainted' or not old_quality.get('verifiedThrough')
                                or quality['verifiedThrough'] >= old_quality['verifiedThrough']):
                            self._evidence(db,code,day,rows,'independent_quote_verification',evidence)
                            self._quality(db,code,day,rows,quality,evidence,'quote_verification')
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        return self.quality(code, cutoff, [day])

    def quality(self, code, cutoff, required_days=None):
        if required_days is None:
            start = (_datetime(cutoff)-timedelta(days=65)).date().isoformat()
            with self.connect() as db:
                required_days = [row[0] for row in db.execute('SELECT day FROM sessions WHERE code=? AND day>=? AND day<=?',
                    (code, start, _datetime(cutoff).date().isoformat()))]
        return read_archive_lineage(self.path.parent, code, required_days, cutoff=_datetime(cutoff))

    def save(self, code, rows, cutoff, *, verification=None):
        symbol(code)  # Validate storage identity before accepting any payload.
        if verification:
            verification = {key:value for key,value in verification.items() if key != 'known_at'}
        original = list(rows)
        duplicate_conflicts = set()
        rows = normalize_bars(original, cutoff, conflicts=duplicate_conflicts, include_tainted=True)
        days = {}
        for at,row in rows.items():
            days.setdefault(at.date().isoformat(),{})[row['end']] = row
        added = enriched = conflicts = 0
        quarantined = []
        for at in duplicate_conflicts:
            days.setdefault(at.date().isoformat(), {})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for day,fresh in days.items():
                old = db.execute('SELECT payload FROM sessions WHERE code=? AND day=?',(code,day)).fetchone()
                old_rows = json.loads(zlib.decompress(old[0])) if old else []
                merged = {row['end']:row for row in old_rows}
                old_quality = db.execute('SELECT status,verification_json,reasons_json FROM session_quality WHERE code=? AND day=?', (code, day)).fetchone()
                day_conflicts = sum(at.date().isoformat() == day for at in duplicate_conflicts)
                day_added = day_enriched = 0
                for stamp, row in fresh.items():
                    previous = merged.get(stamp)
                    if previous:
                        audit = {}
                        resolved = supplement_bars([previous], [row], cutoff, diagnostics=audit)
                        conflicts += audit['conflictingOverlapMinutes']
                        day_conflicts += audit['conflictingOverlapMinutes']
                        day_enriched += audit['enrichedMinutes']
                        if resolved:
                            merged[stamp] = resolved[0]
                    else:
                        merged[stamp] = row
                        day_added += 1
                merged_rows = [merged[k] for k in sorted(merged)]
                anchor = verification if verification and verification.get('day') == day else (json.loads(old_quality[1]) if old_quality else None)
                quality = inspect_session(code, day, merged_rows, anchor)
                if day_conflicts:
                    quality.update(status='tainted', reasonCodes=sorted(set(quality['reasonCodes'] + ['conflicting_minute_observations'])))
                if old_quality and old_quality[0] == 'tainted':
                    quality.update(status='tainted', reasonCodes=sorted(set(quality['reasonCodes'] + json.loads(old_quality[2]) + ['explicit_verified_rebuild_required'])))
                if quality['status'] == 'tainted':
                    quarantined.append(day)
                    self._evidence(db, code, day, old_rows, 'before_quarantine')
                    self._evidence(db, code, day, [row for row in original if str(row.get('end', ''))[:10] == day], 'quarantined_ingest', quality)
                    # Keep the prior stored payload, never splice a suspect new
                    # segment into it; both versions are retained as evidence.
                    merged_rows = old_rows or merged_rows
                else:
                    added += day_added
                    enriched += day_enriched
                payload = zlib.compress(json.dumps(merged_rows,separators=(',',':')).encode('utf-8'))
                db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)',(code,day,payload))
                self._quality(db, code, day, merged_rows, quality, anchor, 'quarantine' if quality['status'] == 'tainted' else 'ingest')
        return {'addedMinutes': added, 'enrichedMinutes': enriched, 'conflictingOverlapMinutes': conflicts,
                'quarantinedDays': quarantined, 'data_quality_version': DATA_QUALITY_VERSION}

    def quarantine(self, code, day, reason, *, evidence=None):
        """Explicit quarantine; preserves bytes and records an invalidation event."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM sessions WHERE code=? AND day=?', (code, day)).fetchone()
            if not old:
                raise ValueError('cannot quarantine a missing session')
            rows = json.loads(zlib.decompress(old[0]))
            self._evidence(db, code, day, rows, 'explicit_quarantine', evidence)
            revision = self._quality(db, code, day, rows, {'status':'tainted', 'reasonCodes':[reason]}, event='quarantine')
        return {'code':code, 'day':day, 'revision':revision, 'status':'tainted'}

    def rebuild(self, code, day, rows, cutoff, *, verification, expected_revision, allow_tainted=True):
        """Publish a new full-session version only after independent verification.

        This never fetches data or silently amends an old recommendation. The
        caller must supply the revision it reviewed, preventing stale repairs.
        """
        verification = {key:value for key,value in verification.items() if key != 'known_at'}
        original = list(rows)
        conflicts = set()
        normalized = list(normalize_bars(original, cutoff, conflicts=conflicts).values())
        if conflicts or any(row['end'][:10] != day for row in normalized):
            raise ValueError('rebuild requires one nonconflicting complete session')
        coverage = minute_coverage(normalized, cutoff, [day])
        if coverage['expectedMinutes'] != 240 or not coverage['pricePathComplete']:
            raise ValueError('rebuild requires all 240 completed minute prices')
        quality = inspect_session(code, day, normalized, verification)
        if quality['status'] != 'verified':
            raise ValueError('rebuild requires verified identity and independent daily prices')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM sessions WHERE code=? AND day=?', (code, day)).fetchone()
            if not old:
                raise ValueError('repair target session is missing')
            old_rows = json.loads(zlib.decompress(old[0]))
            if session_revision(code, day, old_rows) != expected_revision:
                raise ValueError('repair target revision changed')
            old_quality = db.execute('SELECT status FROM session_quality WHERE code=? AND day=?', (code, day)).fetchone()
            if not allow_tainted and old_quality and old_quality[0] == 'tainted':
                raise ValueError('quarantined repair requires explicit authorization')
            repair_id = canonical_hash([expected_revision, datetime.now(SHANGHAI).isoformat(), verification])
            normalized = [{**row, 'integrity_revision': repair_id} for row in normalized]
            self._evidence(db, code, day, old_rows, 'before_verified_rebuild')
            self._evidence(db, code, day, original, 'verified_rebuild', verification)
            db.execute('UPDATE sessions SET payload=? WHERE code=? AND day=?',
                       (zlib.compress(json.dumps(normalized,separators=(',',':')).encode()), code, day))
            revision = self._quality(db, code, day, normalized, quality, verification, 'verified_rebuild')
        return {'code':code, 'day':day, 'revision':revision, 'previousRevision':expected_revision, 'status':'verified'}

    def replace_unknown_session(self, code, day, rows, cutoff, *, verification, expected_revision):
        """Replace unverified legacy bytes with a genuinely re-fetched subset.

        Missing minutes stay missing. This cannot repair a quarantined session,
        which still requires the full 240-minute rebuild above. Price bounds and
        security identities must be independently verified before the CAS write.
        """
        verification = {key:value for key,value in verification.items() if key != 'known_at'}
        original, conflicts = list(rows), set()
        normalized = list(normalize_bars(original, cutoff, conflicts=conflicts).values())
        if not normalized or conflicts or any(row['end'][:10] != day for row in normalized):
            raise ValueError('replacement requires nonconflicting actual session rows')
        if verification.get('kind') != 'daily_ohlc' or verification.get('day') != day:
            raise ValueError('replacement requires independent historical daily prices')
        quality = inspect_session(code, day, normalized, verification)
        if quality['status'] != 'verified':
            raise ValueError('replacement requires verified identity and independent prices')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT s.payload,q.status,q.verification_json FROM sessions s '
                'LEFT JOIN session_quality q ON q.code=s.code AND q.day=s.day '
                'WHERE s.code=? AND s.day=?', (code, day)).fetchone()
            if not old:
                raise ValueError('replacement target missing')
            old_rows = json.loads(zlib.decompress(old[0]))
            if session_revision(code, day, old_rows) != expected_revision:
                raise ValueError('replacement target revision changed')
            old_quality = inspect_session(code, day, old_rows, json.loads(old[2]) if old[2] else None)
            if old[1] == 'tainted' or old_quality['status'] == 'tainted':
                raise ValueError('quarantined session requires full verified rebuild')
            if old[1] == 'verified' or old_quality['status'] == 'verified':
                raise ValueError('verified session must not be downgraded to a partial replacement')
            repair_id = canonical_hash([expected_revision, datetime.now(SHANGHAI).isoformat(), verification])
            normalized = [{**row, 'integrity_revision':repair_id} for row in normalized]
            self._evidence(db, code, day, old_rows, 'before_verified_legacy_replacement')
            self._evidence(db, code, day, original, 'verified_legacy_replacement', verification)
            db.execute('UPDATE sessions SET payload=? WHERE code=? AND day=?',
                (zlib.compress(json.dumps(normalized, separators=(',',':')).encode()), code, day))
            revision = self._quality(db, code, day, normalized, quality, verification, 'verified_legacy_replacement')
        return {'code':code, 'day':day, 'revision':revision, 'previousRevision':expected_revision,
                'status':'verified', 'actualMinutes':len(normalized), 'fullSessionRebuild':False}

    def supplement_verified_session(self, code, day, rows, cutoff, *, verification, expected_revision):
        """Add compatible actual minutes to a verified partial session with CAS.

        An incomplete response must never replace a better archive. Both the
        fresh subset and the combined session need independent daily price and
        provider identity checks. Any overlapping economic disagreement rejects
        the whole supplement; quarantined sessions still require a full rebuild.
        """
        verification = {key:value for key,value in verification.items() if key != 'known_at'}
        original, conflicts = list(rows), set()
        normalized = list(normalize_bars(original, cutoff, conflicts=conflicts).values())
        if not normalized or conflicts or any(row['end'][:10] != day for row in normalized):
            raise ValueError('supplement requires nonconflicting actual session rows')
        if verification.get('kind') != 'daily_ohlc' or verification.get('day') != day:
            raise ValueError('supplement requires independent historical daily prices')
        if inspect_session(code, day, normalized, verification)['status'] != 'verified':
            raise ValueError('supplement requires verified identity and independent prices')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT s.payload,q.status,q.verification_json FROM sessions s '
                'LEFT JOIN session_quality q ON q.code=s.code AND q.day=s.day '
                'WHERE s.code=? AND s.day=?', (code, day)).fetchone()
            if not old:
                raise ValueError('supplement target missing')
            old_rows = json.loads(zlib.decompress(old[0]))
            if session_revision(code, day, old_rows) != expected_revision:
                raise ValueError('supplement target revision changed')
            previous = inspect_session(code, day, old_rows, json.loads(old[2]) if old[2] else None)
            if old[1] == 'tainted' or previous['status'] == 'tainted':
                raise ValueError('quarantined session requires full verified rebuild')
            if old[1] != 'verified' or previous['status'] != 'verified':
                raise ValueError('supplement target must already be verified')
            coverage = minute_coverage(old_rows, cutoff, [day])
            complete = (coverage['expectedMinutes'] == 240 and coverage['pricePathComplete']
                        and not any(coverage['missingFields'].values()))
            result = {'code':code, 'day':day, 'revision':expected_revision,
                'previousRevision':expected_revision, 'status':'verified', 'actualMinutes':len(old_rows),
                'addedMinutes':0, 'enrichedMinutes':0, 'fullSessionRebuild':False}
            if complete:
                return {**result, 'unchanged':True, 'reason':'complete_session_preserved'}
            audit = {}
            merged = supplement_bars(old_rows, normalized, cutoff, diagnostics=audit)
            if audit['conflictingOverlapMinutes']:
                raise ValueError('supplement economic overlap conflicts with verified history')
            if not audit['addedMinutes'] and not audit['enrichedMinutes']:
                return {**result, 'unchanged':True, 'reason':'no_new_verified_minutes'}
            quality = inspect_session(code, day, merged, verification)
            if quality['status'] != 'verified':
                raise ValueError('combined supplement requires independent verification')
            generation = canonical_hash([expected_revision, datetime.now(SHANGHAI).isoformat(), verification])
            merged = [{**row, 'integrity_revision':generation} for row in merged]
            self._evidence(db, code, day, old_rows, 'before_verified_supplement')
            self._evidence(db, code, day, original, 'verified_supplement', verification)
            db.execute('UPDATE sessions SET payload=? WHERE code=? AND day=?',
                (zlib.compress(json.dumps(merged,separators=(',',':')).encode()), code, day))
            revision = self._quality(db, code, day, merged, quality, verification, 'verified_supplement')
        return {**result, 'revision':revision, 'actualMinutes':len(merged),
                'addedMinutes':audit['addedMinutes'], 'enrichedMinutes':audit['enrichedMinutes'], 'unchanged':False}

    def attempted(self, code, day, source):
        with self.connect() as db:
            return db.execute('SELECT status FROM attempts WHERE code=? AND day=? AND source=?', (code,day,source)).fetchone()

    def claim_attempt(self, code, day, source, *, retry_incomplete=False):
        """Claim historical work across processes; failed/partial work can retry."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = wall_time()
            cursor = db.execute('INSERT OR IGNORE INTO attempts VALUES(?,?,?,?)',
                                (code,day,source,'started'))
            if cursor.rowcount == 1:
                db.execute('INSERT OR REPLACE INTO attempt_times VALUES(?,?,?,?)', (code,day,source,now))
                return True
            # A transient provider failure must not lock historical repair for
            # the rest of the trading day. The atomic update still has one owner.
            cursor = db.execute("UPDATE attempts SET status='started' WHERE code=? AND day=? AND source=? AND status IN ('failed','partial')",
                                (code,day,source))
            claimed = cursor.rowcount == 1
            if not claimed and retry_incomplete:
                stamp = db.execute('SELECT attempted_at FROM attempt_times WHERE code=? AND day=? AND source=?', (code,day,source)).fetchone()
                if stamp is None or now-stamp[0] >= 300:
                    cursor = db.execute("UPDATE attempts SET status='started' WHERE code=? AND day=? AND source=? AND status IN ('empty','fetched','started')", (code,day,source))
                    claimed = cursor.rowcount == 1
            if claimed:
                db.execute('INSERT OR REPLACE INTO attempt_times VALUES(?,?,?,?)', (code,day,source,now))
            return claimed

    def record(self, code, day, source, status):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)',(code,day,source,status))
            db.execute('INSERT OR REPLACE INTO attempt_times VALUES(?,?,?,?)',(code,day,source,wall_time()))


def fetch_sina_bars(code, *, count=1970, timeout=5, getter=None):
    response = (getter or requests.get)(SINA_URL, params={'symbol':symbol(code), 'scale':'1','ma':'no','datalen':str(count)},
                                        headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    match = re.search(r'=\s*\((.*)\);?\s*$', response.text, re.S)
    raw = json.loads(match.group(1)) if match else None
    if not isinstance(raw, list) or not raw:
        raise ValueError('Sina returned no minute bars')
    fetched_at = datetime.now(SHANGHAI).isoformat()
    return [{'end':row.get('day'), **{key:row.get(key) for key in ('open','high','low','close')},
             'amount_cny':row.get('amount'), 'volume_shares':row.get('volume'), 'source':'Sina 1min unadjusted',
             'fetched_at': fetched_at} for row in raw]


def fetch_software_bars(code, *, count=8000, client=None, end=None, timeout=12):
    """Reuse the desktop chart provider and its loopback cache for raw bars."""
    if client is None:
        from src.services.software_market import SoftwareMarketClient
        client = SoftwareMarketClient.from_environment()
        client.read_timeout = timeout
    if not client.available:
        raise RuntimeError('software market gateway unavailable')
    options = {'end':end} if end else {}
    packet = client.bars(code, period='1', count=count, **options)
    if packet.get('time_semantics') not in (None, 'bar_end'):
        raise ValueError('software market minute timestamps are not bar ends')
    bars = packet.get('bars') or []
    if not isinstance(bars, list) or not bars:
        raise ValueError('software market returned no minute bars')
    return bars


def fetch_recent_bars(code, *, count=240, cutoff=None, required_days=None, diagnostics=None, deadline=None):
    """Supplement a short/incomplete software response without replacing its bars."""
    cutoff = _datetime(cutoff or datetime.now(SHANGHAI))
    audit = diagnostics if diagnostics is not None else {}
    audit.update(sourceFailures=[], sources=[])
    def remaining(maximum):
        return maximum if deadline is None else max(0., min(maximum, deadline-monotonic()))
    if remaining(1) <= 0:
        raise TimeoutError('minute fetch deadline expired')
    primary = []
    try:
        primary = list(normalize_bars(fetch_software_bars(code,count=count,timeout=max(.1,remaining(4))), cutoff).values())
        if not primary:
            raise ValueError('software returned no usable complete minutes')
        audit['sources'].append('software')
    except (requests.RequestException,RuntimeError,ValueError,TypeError,KeyError) as error:
        audit['sourceFailures'].append({'source': 'software', 'error': type(error).__name__})
    coverage = minute_coverage(primary, cutoff, required_days) if required_days else None
    needs_more = (len(primary) < count or coverage is not None and
                  (not coverage['pricePathComplete'] or any(coverage['missingFields'].values())))
    if needs_more and remaining(1) > 0:
        try:
            public = fetch_sina_bars(code,count=max(count, 1970 if required_days else count),timeout=max(.1,remaining(5)))
            if not normalize_bars(public, cutoff):
                raise ValueError('Sina returned no usable complete minutes')
            primary = supplement_bars(primary, public, cutoff, diagnostics=audit)
            audit['sources'].append('sina')
        except (requests.RequestException,RuntimeError,ValueError,TypeError,KeyError) as error:
            audit['sourceFailures'].append({'source': 'sina', 'error': type(error).__name__})
    if not primary:
        raise ValueError('No source returned usable complete minute bars')
    return primary


def fetch_tushare_bars(code, cutoff, *, timeout=8, poster=None):
    token = os.getenv('TUSHARE_TOKEN','').strip() or os.getenv('TUSHARE_API_TOKEN','').strip()
    if not token:
        raise ValueError('Tushare minute token not configured')
    dates = _prior_sessions(_datetime(cutoff).date())
    if len(dates) != FORMAL_HISTORY_DAYS:
        raise ValueError('Exchange calendar unavailable')
    # 20 previous sessions = 4800 rows, below the documented 8000-row limit.
    url = os.getenv('TUSHARE_API_URL','').strip() or os.getenv('TUSHARE_HTTP_URL','').strip() or 'https://api.tushare.pro'
    response = (poster or requests.post)(url, json={'api_name':'stk_mins','token':token,
        'params':{'ts_code':code+('.SH' if code.startswith('6') else '.SZ'),'freq':'1min',
                  'start_date':str(dates[0])+' 09:00:00','end_date':str(dates[-1])+' 15:01:00'},
        'fields':'ts_code,trade_time,open,close,high,low,vol,amount'}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
        # Never include response messages/URLs: providers can echo credentials.
        raise ValueError('Tushare minute permission or service unavailable')
    data = payload['data']
    rows = [dict(zip(data.get('fields', []), item)) for item in data.get('items', [])]
    if not rows or any(row.get('ts_code') != code+('.SH' if code.startswith('6') else '.SZ') for row in rows):
        raise ValueError('Tushare minute identity or data missing')
    return [{'end':row['trade_time'], **{key:row.get(key) for key in ('open','high','low','close')},
             'amount_cny':row.get('amount'), 'volume_shares':row.get('vol'), 'source':'Tushare stk_mins 1min unadjusted'} for row in rows]


def window(rows, end, size):
    stamps = [end-timedelta(minutes=i) for i in range(size-1,-1,-1)]
    if any(at not in rows or bar_session(at) != bar_session(end) for at in stamps):
        return None
    return [rows[at] for at in stamps]


def session_window_status(end):
    """Explain scheduled warm-up without treating the lunch break as lost data.

    Bar labels are end times: the first afternoon price is 13:01, so a
    three-minute return needs 13:01--13:04 and a five-minute return needs
    13:01--13:06. No price or amount is carried across a session boundary.
    """
    end = _datetime(end).replace(second=0, microsecond=0)
    session = bar_session(end)
    if session is None:
        return {'status': 'outside_trading_session', 'session': None,
                'asOf': end.isoformat(), 'firstBarAt': None,
                'baselineReadyAt': None, 'price5mReadyAt': None}
    first = end.replace(hour=9 if session == 'am' else 13, minute=31 if session == 'am' else 1)
    baseline_at, five_at = first + timedelta(minutes=3), first + timedelta(minutes=5)
    return {'status': 'session_warmup' if end < five_at else 'window_elapsed',
            'session': session, 'asOf': end.isoformat(), 'firstBarAt': first.isoformat(),
            'baselineReadyAt': baseline_at.isoformat(), 'price5mReadyAt': five_at.isoformat(),
            'baselineWindowElapsed': end >= baseline_at,
            'price5mWindowElapsed': end >= five_at}


def _window_features(rows, end):
    """Completed, contiguous, same-session windows only; returns in percentage points."""
    result = {}
    for minutes in (1, 3, 5, 15, 30):
        prices = window(rows, end, minutes + 1)
        result[f'r{minutes}_pct'] = (prices[-1]['close'] / prices[0]['close'] - 1) * 100 if prices else None
        amounts = window(rows, end, minutes)
        result[f'amount_{minutes}m'] = (sum(row['amount_cny'] for row in amounts)
            if amounts and all(row['amount_cny'] is not None for row in amounts) else None)
        result[f'volume_{minutes}m'] = (sum(row['volume_shares'] for row in amounts)
            if amounts and all(row['volume_shares'] is not None for row in amounts) else None)
    if result['r1_pct'] is not None and result['r5_pct'] is not None:
        result['a1_pct_per_min'] = result['r1_pct'] - result['r5_pct'] / 5
    else:
        result['a1_pct_per_min'] = None
    if result['r3_pct'] is not None and result['r15_pct'] is not None:
        result['a3_pct_per_min'] = result['r3_pct'] / 3 - result['r15_pct'] / 15
    else:
        result['a3_pct_per_min'] = None
    result['short_rebound_in_long_weak'] = (float(result['r3_pct'] > 0 and result['r15_pct'] < 0)
        if result['r3_pct'] is not None and result['r15_pct'] is not None else None)
    return result


def _robust_z(current, history, *, floor):
    """Median/MAD with a unit-aware floor; zero dispersion never explodes a score."""
    if current is None or len(history) < MINIMUM_HISTORY_DAYS:
        return None
    centre = median(history)
    scale = max(MINUTE_PARAMS['madConsistency'] * median(abs(value-centre) for value in history),
                abs(centre) * MINUTE_PARAMS['relativeMadFloor'], floor)
    return max(-MINUTE_PARAMS['zClip'], min(MINUTE_PARAMS['zClip'], (current-centre)/scale))


def compute_bar_evidence(rows, cutoff, *, expected_dates=None):
    cutoff = _datetime(cutoff)
    rows = normalize_bars(rows, cutoff)
    current = {at:row for at,row in rows.items() if at.date() == cutoff.date()}
    result = {'metrics':dict.fromkeys(METRIC_DEFINITIONS), 'source':'dated 1min bars', 'asOf':None,
              'gaps':[], 'historyDays':0, 'priceHistoryDays':0, 'featureHistoryDays':{},
              'dependencyDays':[cutoff.date().isoformat()],
              'baselineReady':False, 'baselineDates':[], 'missingHistoryDates':[],
              'baselineStatus':'current_minutes_unavailable', 'sessionWindow':None,
              'sourceTimePrecision':'bar_end', 'metricDefinitions':METRIC_DEFINITIONS.copy(),
              'v11Inputs':{}, 'v11Coverage':{'status':'insufficient','days':0,'requiredDays':FORMAL_HISTORY_DAYS}}
    result['metricDefinitions'].update(amount_3m='截至该分钟结束时间的连续3根1分钟K线成交额之和（元）',
        local_high_5m='此前5根完整1分钟K线最高价，不含当前根（元）',
        local_low_5m='此前5根完整1分钟K线最低价，不含当前根（元）')
    gaps, metrics = result['gaps'], result['metrics']
    if not current:
        gaps.append('当日完整1分钟K线未取得')
        return result
    end = max(current)
    result.update(asOf=end.isoformat(), source=current[end]['source'], currentPrice=current[end]['close'],
                  sourceFetchedAt=current[end].get('fetched_at'),
                  sourceAgeSeconds=(cutoff-end).total_seconds(), sessionWindow=session_window_status(end))
    if (cutoff-end).total_seconds() >= MINUTE_MAX_AGE:
        result['baselineStatus'] = 'current_minutes_stale'
        gaps.append('分钟行情已陈旧，未用于入选')
        return result
    for n in (1,3,5):
        part = window(current,end,n+1)
        if part:
            metrics[f'speed_{n}m_pct'] = (part[-1]['close']/part[0]['close']-1)*100
        else:
            first = _datetime(result['sessionWindow']['firstBarAt'])
            ready_at = first + timedelta(minutes=n)
            gaps.append(f'本交易段预热：{n}分钟价格窗口最早{ready_at:%H:%M}形成'
                        if end < ready_at else f'缺连续{n}分钟价格窗口')
    prior = window(current,end-timedelta(minutes=1),5)
    if prior:
        metrics.update(local_high_5m=max(r['high'] for r in prior),local_low_5m=min(r['low'] for r in prior))
    current_features = _window_features(current, end)
    metrics['amount_3m'] = current_features['amount_3m']
    # Cumulative VWAP only when every elapsed regular-session bar is present.
    stamps = [end.replace(hour=9,minute=31)+timedelta(minutes=i) for i in range(330)]
    needed = [at for at in stamps if at <= end and bar_session(at)]
    vwap_series = {}
    if needed and all(at in current and current[at]['amount_cny'] is not None
                      and current[at]['volume_shares'] is not None for at in needed):
        vol = sum(current[at]['volume_shares'] for at in needed)
        metrics['vwap'] = sum(current[at]['amount_cny'] for at in needed)/vol if vol else None
        cumulative_amount = cumulative_volume = 0.
        for at in needed:
            cumulative_amount += current[at]['amount_cny']
            cumulative_volume += current[at]['volume_shares']
            if cumulative_volume > 0:
                vwap_series[at] = cumulative_amount/cumulative_volume
    dates = list(expected_dates) if expected_dates is not None else list(_prior_sessions(end.date()))
    dates = sorted({datetime.fromisoformat(str(d)).date() for d in dates if str(d) < end.date().isoformat()})[-FORMAL_HISTORY_DAYS:]
    if len(dates) == FORMAL_HISTORY_DAYS:
        result['dependencyDays'] = [day.isoformat() for day in dates] + [end.date().isoformat()]
    samples = []
    feature_history = {key: [] for key in current_features}
    if len(dates) == FORMAL_HISTORY_DAYS:
        for day in dates:
            previous = end.replace(year=day.year,month=day.month,day=day.day)
            part = window(rows,previous,3)
            sample = _window_features(rows, previous)
            for key, value in sample.items():
                if value is not None:
                    feature_history[key].append(value)
            if part and all(r['amount_cny'] is not None for r in part):
                samples.append(sample['amount_3m'])
                result['baselineDates'].append(day.isoformat())
            elif result['sessionWindow']['baselineWindowElapsed']:
                result['missingHistoryDates'].append(day.isoformat())
    result['historyDays'] = len(samples)
    result['priceHistoryDays'] = len(feature_history['r3_pct'])
    result['featureHistoryDays'] = {key:len(values) for key,values in feature_history.items()}
    coverage = 'complete' if len(samples) == FORMAL_HISTORY_DAYS else 'low' if len(samples) >= MINIMUM_HISTORY_DAYS else 'insufficient'
    result['v11Coverage'] = {'status':coverage, 'days':len(samples),
                             'priceDays':result['priceHistoryDays'],
                             'factorDays':result['featureHistoryDays'],'requiredDays':FORMAL_HISTORY_DAYS}
    if not result['sessionWindow']['baselineWindowElapsed']:
        result['v11Coverage']['status'] = 'session_warmup'
    v11 = {'history_days':len(samples),'price_history_days':result['priceHistoryDays'], **current_features}
    if end in vwap_series:
        previous_at = end-timedelta(minutes=1)
        previous_vwap = vwap_series.get(previous_at)
        v11['vwap_reclaim'] = (1. if previous_vwap is not None and current[previous_at]['close'] <= previous_vwap
                                  and current[end]['close'] > vwap_series[end] else 0.
                                  if previous_vwap is not None else None)
        five_back = vwap_series.get(end-timedelta(minutes=5))
        v11['vwap_slope_5m_pct'] = ((vwap_series[end]/five_back-1)*100
            if five_back and five_back > 0 else None)
        recent = window(current,end,5)
        if recent and all(datetime.fromisoformat(row['end']) in vwap_series for row in recent):
            v11['vwap_retest_success'] = 1. if any(
                row['low'] <= vwap_series[datetime.fromisoformat(row['end'])]*(1+MINUTE_PARAMS['vwapRetestTolerance'])
                and row['close'] > vwap_series[datetime.fromisoformat(row['end'])]
                for row in recent) else 0.
    # Descriptive path evidence is frozen separately from the existing rule
    # feature vector. It cannot silently change a fitted model's input contract.
    result['trendEvidence'] = compute_trend_evidence(current, end, vwap_series)
    previous_five = window(current,end-timedelta(minutes=1),5)
    if previous_five:
        prior_high = max(row['high'] for row in previous_five)
        v11['failed_breakout_risk'] = (100. if current[end]['high'] > prior_high
            and current[end]['close'] <= prior_high else 0.)
        recent_highs = [row['high'] for row in previous_five[-2:]] + [current[end]['high']]
        v11['lower_high_risk'] = (100. if recent_highs[0] > recent_highs[1] > recent_highs[2] else 0.)
    for key in ('r1_pct','r3_pct','r5_pct','r15_pct','r30_pct','a1_pct_per_min','a3_pct_per_min'):
        v11['z_'+key] = _robust_z(current_features[key],feature_history[key],floor=MINUTE_PARAMS['priceMadFloor'])
    for minutes in (3,5,15):
        key = f'amount_{minutes}m'
        baseline = feature_history[key]
        centre = median(baseline) if len(baseline) >= MINIMUM_HISTORY_DAYS else None
        v11[f'baseline_{key}'] = centre
        v11[f'v{minutes}_ratio'] = (current_features[key]/centre
            if current_features[key] is not None and centre is not None and centre > 0 else None)
        v11[f'z_{key}'] = _robust_z(current_features[key],baseline,floor=MINUTE_PARAMS['amountMadFloor'])
        volume_key = f'volume_{minutes}m'
        volume_history = feature_history[volume_key]
        v11[f'baseline_{volume_key}'] = (median(volume_history)
            if len(volume_history) >= MINIMUM_HISTORY_DAYS else None)
    v3, v15 = v11.get('v3_ratio'),v11.get('v15_ratio')
    v11['va_ratio'] = v3/v15 if v3 is not None and v15 and v15 > 0 else None
    v11['price_efficiency_r5_v5'] = (v11['r5_pct']/v11['v5_ratio']
        if v11.get('r5_pct') is not None and v11.get('v5_ratio') and v11['v5_ratio'] > 0 else None)
    result['v11Inputs'] = v11
    result['baselineMedianAmount'] = median(samples) if len(samples) == FORMAL_HISTORY_DAYS else None
    result['baselineSource'] = ', '.join(sorted({r['source'] for at,r in rows.items() if at.date() in dates}))
    if len(samples) == FORMAL_HISTORY_DAYS and median(samples) > 0 and metrics['amount_3m'] is not None:
        metrics['relative_amount_3m_20d'] = metrics['amount_3m']/median(samples)
        result['baselineReady'] = True
    if not result['sessionWindow']['baselineWindowElapsed']:
        result['baselineStatus'] = 'session_warmup'
        gaps.append('本交易段同刻量价窗口尚在预热，最早'
                    + _datetime(result['sessionWindow']['baselineReadyAt']).strftime('%H:%M')
                    + '形成；这不代表20日历史丢失')
    elif len(samples) != FORMAL_HISTORY_DAYS or result['priceHistoryDays'] != FORMAL_HISTORY_DAYS:
        result['baselineStatus'] = 'missing_history'
        gaps.append(f'同刻20日分钟基准未就绪（{len(samples)}/20）；不以少量样本或全天量比替代')
    elif not result['baselineReady'] or metrics['speed_3m_pct'] is None:
        result['baselineStatus'] = 'current_window_unavailable'
    else:
        result['baselineStatus'] = 'ready'
    result['basis'] = '1分钟K线结束时间；3分钟额为连续3根成交额之和；历史基准严格使用此前20个交易日'
    return result


def _formal_history_ready(evidence):
    # A three-minute amount needs three bars; its price return needs four.
    # Twenty amount samples alone therefore cannot close the history repair.
    return all(evidence.get(key, 0) >= FORMAL_HISTORY_DAYS
               for key in ('historyDays', 'priceHistoryDays'))


def read_bar_evidence(code, cutoff, cache_dir, *, required_days=None):
    """Recompute a frozen cutoff from the archive, without network/schema writes.

    Read today's session first. Missing/stale current data cannot use a baseline,
    so it must not pay to decode 65 calendar days. When current data is usable,
    only the exact prior exchange sessions used by the formula are loaded.
    Whole-session integrity is still checked before taking any feature window.
    """
    cutoff = _datetime(cutoff)
    started = monotonic()
    rows = read_archive_rows(cache_dir, code, cutoff, lookback_days=0)
    current_end = max((_datetime(row['end']) for row in rows), default=None)
    dates, feature_rows = [], []
    if current_end is not None and (cutoff-current_end).total_seconds() < MINUTE_MAX_AGE:
        dates = sorted({str(day)[:10] for day in _prior_sessions(cutoff.date())
                        if str(day)[:10] < cutoff.date().isoformat()})[-FORMAL_HISTORY_DAYS:]
        if len(dates) == FORMAL_HISTORY_DAYS:
            feature_rows = _read_feature_windows(cache_dir, code, dates, current_end)
    result = compute_bar_evidence(feature_rows + rows, cutoff, expected_dates=dates)
    dependency_days = required_days or result['dependencyDays']
    result.update(code=code, archivePath=str(Path(cache_dir) / 'minute-bars-v2.sqlite3'),
                  dataLineage=read_archive_lineage(cache_dir, code, dependency_days, cutoff=cutoff))
    if required_days:
        coverage_rows = read_archive_rows(cache_dir, code, cutoff, required_days=required_days)
        result['requiredCoverage'] = minute_coverage(coverage_rows, cutoff, required_days)
    result['readEvidenceMs'] = round((monotonic()-started)*1000, 3)
    return result


def _fetch_scan_bar_evidence(code, cutoff, cache_dir, *, getter=None, required_days=None, deadline=None):
    """Incremental scan path: fetch current bars first, read history once last."""
    def remaining(maximum):
        return maximum if deadline is None else max(0., min(maximum, deadline-monotonic()))
    started = monotonic()
    errors, software_fetched = [], False
    supplemental = {'attempted': False}
    if remaining(1) > 0:
        archive = MinuteArchive(cache_dir)
        if getter is None:
            from src.services.software_market import SoftwareMarketClient
            client = SoftwareMarketClient.from_environment()
            client.read_timeout = max(.1, remaining(4))
            if client.available and remaining(1) > 0:
                try:
                    bars = fetch_software_bars(code, count=240, client=client)
                    normalized = {at:row for at,row in normalize_bars(bars, cutoff).items()
                                  if at.date() == cutoff.date()}
                    if not normalized:
                        raise ValueError('no usable software minute bars')
                    archive.save(code, list(normalized.values()), cutoff)
                    software_fetched = True
                except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError) as exc:
                    errors.append('软件分钟更新失败：'+type(exc).__name__)
        if not software_fetched and remaining(1) > 0:
            supplemental['attempted'] = True
            try:
                bars = fetch_sina_bars(code, getter=getter, count=240, timeout=max(.1, remaining(5)))
                current = [row for at,row in normalize_bars(bars, cutoff).items()
                           if at.date() == cutoff.date()]
                if not current:
                    raise ValueError('Sina returned no usable complete minutes')
                supplemental.update(archive.save(code, current, cutoff), status='fetched')
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                errors.append('Sina分钟更新失败：'+type(exc).__name__)
                supplemental.update(status='failed', error=type(exc).__name__)
    else:
        errors.append('分钟采集已到截止时间，仅读缓存，未追加历史回填')
    fetch_ms = round((monotonic()-started)*1000, 3)
    result = read_bar_evidence(code, cutoff, cache_dir, required_days=required_days)
    result.update(fetchedAt=datetime.now(SHANGHAI).isoformat(), softwareMarketUsed=software_fetched,
                  supplementalMinuteFetch=supplemental, historyBackfill={
                      'status':'disabled_during_scan', 'pages':0, 'newBars':0, 'requestedEnds':[], 'pageCoverage':[]},
                  fetchTimingsMs={'currentFetchAndArchive':fetch_ms, 'readEvidence':result['readEvidenceMs']},
                  historyAccess='configured' if os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN') else 'public_archive_warming')
    result['gaps'].extend(errors)
    return result


def fetch_bar_evidence(code, cutoff, cache_dir, *, getter=None, poster=None, backfill=True, software_count=8000,
                       required_days=None, deadline=None):
    cutoff = _datetime(cutoff)
    if not backfill and software_count <= 240:
        return _fetch_scan_bar_evidence(code, cutoff, cache_dir, getter=getter,
                                       required_days=required_days, deadline=deadline)
    archive = MinuteArchive(cache_dir)
    def remaining(maximum):
        return maximum if deadline is None else max(0., min(maximum, deadline-monotonic()))
    if deadline is not None and remaining(1) <= 0:
        result = compute_bar_evidence(archive.read(code, cutoff), cutoff)
        result.update(code=code, archivePath=str(archive.path),
                      dataLineage=archive.quality(code, cutoff, required_days or result['dependencyDays']))
        result['gaps'].append('分钟采集已到截止时间，仅读缓存，未追加历史回填')
        return result
    archived = archive.read(code, cutoff)
    # Once history is warm, merge the current session into that archive. Do not
    # call an old or incomplete archive current, or reduce cold-start coverage.
    if archived and software_count > 240:
        latest = max(_datetime(row['end']) for row in archived)
        history = compute_bar_evidence(archived, cutoff)
        if (cutoff-latest).total_seconds() < MINUTE_MAX_AGE and _formal_history_ready(history):
            software_count = 240
    errors = []
    software_fetched = False
    client = None
    if getter is None:
        from src.services.software_market import SoftwareMarketClient
        client = SoftwareMarketClient.from_environment()
        client.read_timeout = max(.1, remaining(12))
        if client.available and remaining(1) > 0:
            try:
                bars = fetch_software_bars(code,count=software_count,client=client)
                normalized = normalize_bars(bars,cutoff)
                if not normalized:
                    raise ValueError('no usable software minute bars')
                archive.save(code,list(normalized.values()),cutoff)
                software_fetched = True
            except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError) as exc:
                errors.append('软件分钟更新失败：'+type(exc).__name__)
    supplemental = {'attempted': False}
    cached_after_software = archive.read(code, cutoff)
    required_coverage = minute_coverage(cached_after_software, cutoff, required_days or [cutoff.date()])
    needs_supplement = backfill and (not required_coverage['pricePathComplete'] or
        any(required_coverage['missingFields'].values()) or
        software_count > 240 and not _formal_history_ready(compute_bar_evidence(cached_after_software, cutoff)))
    if (not software_fetched or needs_supplement) and remaining(1) > 0:
        supplemental['attempted'] = True
        try:
            public_rows = fetch_sina_bars(code,getter=getter,count=1970 if backfill else 240,timeout=max(.1,remaining(5)))
            if not normalize_bars(public_rows, cutoff):
                raise ValueError('Sina returned no usable complete minutes')
            supplemental.update(archive.save(code, public_rows, cutoff))
            supplemental['status'] = 'fetched'
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            errors.append('Sina分钟更新失败：'+type(exc).__name__)
            supplemental.update(status='failed', error=type(exc).__name__)
    rows = archive.read(code,cutoff)
    result = compute_bar_evidence(rows, cutoff)
    day = cutoff.date().isoformat()
    history_backfill = {'status':'not_needed' if _formal_history_ready(result) else 'not_available',
                        'pages':0,'newBars':0,'requestedEnds':[], 'pageCoverage': []}
    if (backfill and remaining(1) > 0 and not _formal_history_ready(result) and client is not None and client.available
            and rows and archive.claim_attempt(code,day,'software_history',retry_incomplete=True)):
        history_backfill['status'] = 'empty'
        try:
            for _ in range(2):
                if remaining(1) <= 0:
                    break
                client.read_timeout = max(.1, remaining(12))
                oldest = min(_datetime(row['end']) for row in rows)
                before = (oldest-timedelta(minutes=1)).strftime('%Y%m%d%H%M%S')
                history_backfill['requestedEnds'].append(before)
                page = fetch_software_bars(code,count=8000,client=client,end=before)
                history_backfill['pages'] += 1
                accepted = normalize_bars(page,cutoff)
                older = [row for at,row in accepted.items() if at < oldest]
                history_backfill['pageCoverage'].append({'requestedEnd': before, 'returnedMinutes': len(accepted),
                    'oldestReturned': min(accepted).isoformat() if accepted else None, 'olderMinutes': len(older)})
                if not older:
                    history_backfill['reason'] = 'provider_returned_no_older_minutes'
                    break  # Provider ignored end or has no older data.
                archive.save(code,older,cutoff)
                history_backfill['newBars'] += len(older)
                rows = archive.read(code,cutoff)
                result = compute_bar_evidence(rows,cutoff)
                history_backfill['status'] = 'fetched'
                if _formal_history_ready(result):
                    break
        except (requests.RequestException,RuntimeError,ValueError,TypeError,KeyError) as exc:
            errors.append('软件历史分钟回填失败：'+type(exc).__name__)
            history_backfill['status'] = 'failed' if not history_backfill['newBars'] else 'partial'
        archive.record(code,day,'software_history',history_backfill['status'])
    elif backfill and not _formal_history_ready(result) and archive.attempted(code,day,'software_history'):
        history_backfill['status'] = 'already_attempted_today'
    if backfill and remaining(1) > 0 and (not result['baselineReady'] or not _formal_history_ready(result)):
        if ((os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN'))
                and archive.claim_attempt(code,day,'tushare',retry_incomplete=True)):
            try:
                archive.save(code, fetch_tushare_bars(code,cutoff,poster=poster,timeout=max(.1,remaining(8))), cutoff)
                archive.record(code,day,'tushare','fetched')
                result = compute_bar_evidence(archive.read(code,cutoff),cutoff)
            except (requests.RequestException,ValueError,TypeError,KeyError) as exc:
                errors.append('Tushare历史分钟回填失败：'+type(exc).__name__)
                archive.record(code,day,'tushare','failed')
    result.update(code=code, fetchedAt=datetime.now(SHANGHAI).isoformat(), archivePath=str(archive.path))
    result['historyBackfill'] = history_backfill
    result['supplementalMinuteFetch'] = supplemental
    if required_days:
        result['requiredCoverage'] = minute_coverage(archive.read(code, cutoff), cutoff, required_days)
    result['gaps'].extend(errors)
    result['softwareMarketUsed'] = software_fetched
    result['dataLineage'] = archive.quality(code, cutoff, required_days or result['dependencyDays'])
    result['historyAccess'] = 'configured' if os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN') else 'public_archive_warming'
    return result


def archive_universe(codes, cutoff, cache_dir, *, workers=4, fetcher=None, progress=None,
                     include_registered=True, timeout=None, count=240, required_days=None):
    """Daily whole-universe maintenance, independent of recommendation membership."""
    cutoff = _datetime(cutoff)
    archive = MinuteArchive(cache_dir)
    registry = Path(cache_dir)/'archive-universe.json'
    try:
        saved = json.loads(registry.read_text(encoding='utf-8')).get('codes',[])
    except (OSError,ValueError,TypeError):
        saved = []
    requested = list(dict.fromkeys(str(code) for code in codes if re.fullmatch(r'(?:60|00)\d{4}', str(code))))
    registered = sorted(set(requested) | {str(code) for code in saved if re.fullmatch(r'(?:60|00)\d{4}', str(code))})
    codes = requested + sorted(set(registered) - set(requested)) if include_registered else requested
    temporary=registry.with_suffix('.tmp')
    temporary.write_text(json.dumps({'codes':registered,'updatedAt':cutoff.isoformat()}),encoding='utf-8')
    os.replace(temporary,registry)
    day = cutoff.date().isoformat()
    def collect(code):
        dates = (required_days or {}).get(code)
        diagnostic = {'code': code}
        # A prior pre-close or stale response must not suppress the close fetch.
        post_close = cutoff.hour >= 15
        def complete(rows):
            if dates:
                coverage = minute_coverage(rows, cutoff, dates)
                return coverage['pricePathComplete'] and not any(coverage['missingFields'].values())
            current = [row for row in rows if str(row['end'])[:10] == day]
            return len(current) == 240 if post_close else bool(current)
        before = archive.read(code, cutoff)
        if ((dates or count <= 240) and archive.attempted(code,day,'sina_archive') == ('ok',) and complete(before)):
            diagnostic.update(status='cached', coverage=minute_coverage(before, cutoff, dates) if dates else None)
            return diagnostic
        try:
            raw = fetcher(code) if fetcher else fetch_recent_bars(code,count=count,cutoff=cutoff,
                                                                required_days=dates,diagnostics=diagnostic)
            rows = normalize_bars(raw,cutoff)
            if not rows:
                raise ValueError('no valid bars')
            diagnostic['archiveMerge'] = archive.save(code,list(rows.values()),cutoff)
            after = archive.read(code, cutoff)
            status = 'updated' if complete(after) else 'incomplete'
            if not dates and count > 240 and len(after) < count:
                status = 'incomplete'
            if dates:
                diagnostic['coverage'] = minute_coverage(after, cutoff, dates)
            archive.record(code,day,'sina_archive','ok' if status == 'updated' else status)
            diagnostic['status'] = status
            return diagnostic
        except Exception as error:
            archive.record(code,day,'sina_archive','failed')
            diagnostic.update(status='failed', error=type(error).__name__)
            if dates:
                diagnostic['coverage'] = minute_coverage(archive.read(code, cutoff), cutoff, dates)
            return diagnostic
    if timeout is None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            details = list(pool.map(collect,codes))
        unfinished = []
    else:
        details, unfinished = bounded_fetch_map(collect, codes, workers=workers, timeout=timeout)
    statuses = [item['status'] for item in details]
    if progress:
        progress({'done':len(statuses),'total':len(codes),'failed':statuses.count('failed')})
    return {'universe':len(codes), **{key:statuses.count(key) for key in ('cached','updated','failed','incomplete')},
            'deferred':len(unfinished), 'status':'partial' if unfinished or any(s in ('failed','incomplete') for s in statuses) else 'completed',
            'archivePath':str(archive.path),'asOf':cutoff.isoformat(),
            'details':details,
            'scope':'全部沪深主板；与是否入选无关；公共源滚动积累，完整20日方可启用基准'}
