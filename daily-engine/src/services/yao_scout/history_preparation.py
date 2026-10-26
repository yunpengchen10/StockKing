"""Bounded, resumable preparation of the exact prior twenty minute sessions.

Run outside the live scan. Historical identities come from fresh provider replies
and historical price anchors from independent, unadjusted daily OHLC. This module
never opens the signal ledger or changes past publication/training eligibility.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import sqlite3
from time import monotonic, sleep
from uuid import uuid4
import zlib

import requests

from .intraday_evidence import SHANGHAI, _datetime, _prior_sessions
from .minute_history import (FORMAL_HISTORY_DAYS, MinuteArchive, bounded_fetch_map,
    fetch_software_bars, minute_coverage, normalize_bars, session_window_status, symbol, window)
from .minute_integrity import inspect_session, read_archive_lineage, session_revision

VERSION = 'verified-history-preparation-v1'
STATUS_FILE = 'history-preparation.json'


def read_preparation_status(cache_dir, as_of=None):
    """Cheap display-only readiness snapshot; live admission still checks lineage."""
    path = Path(cache_dir) / STATUS_FILE
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict) or result.get('version') != VERSION:
            raise ValueError('unsupported preparation state')
    except (OSError, ValueError, TypeError):
        return {'version': VERSION, 'status': 'not_prepared', 'statusPath': str(path),
                'ready': 0, 'details': {}}
    if as_of is not None and result.get('targetSession') != _datetime(as_of).date().isoformat():
        return {**result, 'status': 'stale', 'ready': 0,
                'reason': 'prior_session_window_changed', 'statusPath': str(path)}
    return {**result, 'statusPath': str(path)}


def _write_status(path, result):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows antivirus/indexer readers can briefly hold the old
                # JSON without delete sharing. Keep atomic replacement intact.
                if attempt == 4:
                    raise
                sleep(.025 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_sessions(archive, code, days):
    with closing(sqlite3.connect(archive.path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT day,payload FROM sessions WHERE code=? AND day IN (' +
                          ','.join('?' for _ in days) + ')', [code, *days]).fetchall()
    return {day: json.loads(zlib.decompress(payload)) for day, payload in rows}


def _complete(rows, day, cutoff):
    coverage = minute_coverage(rows, cutoff, [day])
    return bool(coverage['expectedMinutes'] == 240 and coverage['pricePathComplete']
                and not any(coverage['missingFields'].values()))


def _audit(archive, code, days, cutoff):
    raw = _read_sessions(archive, code, days)
    lineage = read_archive_lineage(archive.path.parent, code, days, cutoff=cutoff)
    states = {row['day']: row for row in lineage['sessions']}
    verified = [day for day in days if states.get(day, {}).get('status') == 'verified']
    complete = [day for day in verified if _complete(raw.get(day, []), day, cutoff)]
    clock = min(cutoff.timetz().replace(tzinfo=None), time(15))
    clock = clock.replace(second=0, microsecond=0)
    window_days = []
    ready_clocks = None
    for day in verified:
        end = datetime.combine(date.fromisoformat(day), clock, SHANGHAI)
        bars = normalize_bars(raw[day], cutoff)
        price, amount = window(bars, end, 4), window(bars, end, 3)
        if price and amount and all(row['amount_cny'] is not None for row in amount):
            window_days.append(day)
        # Keep the genuine available clocks, not just the clock of this worker.
        # A small hole elsewhere in a session must not hide all its good windows.
        day_clocks = set()
        for at in bars:
            prices, amounts = window(bars, at, 4), window(bars, at, 3)
            if prices and amounts and all(row['amount_cny'] is not None for row in amounts):
                day_clocks.add(at.time().isoformat())
        ready_clocks = day_clocks if ready_clocks is None else ready_clocks & day_clocks
    return raw, states, {'code': code, 'ready': len(window_days) == FORMAL_HISTORY_DAYS,
        'fullSessionReady': len(complete) == FORMAL_HISTORY_DAYS,
        'readyClocks': sorted(ready_clocks or []) if len(verified) == FORMAL_HISTORY_DAYS else [],
        'verifiedDayCount': len(verified), 'sessionWindow': session_window_status(cutoff),
        'completeDays': complete, 'baselineClock': clock.isoformat(), 'windowReadyDays': window_days,
        'verifiedDays': verified, 'missingDays': [day for day in days if day not in raw],
        'incompleteDays': [day for day in days if day in raw and not _complete(raw[day], day, cutoff)],
        'unverifiedDays': [day for day in days if day in raw and states.get(day, {}).get('status') != 'verified'],
        'taintedDays': [day for day in days if states.get(day, {}).get('status') == 'tainted'],
        'reasonCodes': lineage['reasonCodes']}


def _call_before_deadline(fetch, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError('history_preparation_budget_exhausted')
    # Providers may ignore socket timeouts. Late workers only perform reads;
    # archive and checkpoint writes happen here, after this boundary, never there.
    result, _ = bounded_fetch_map(lambda _: fetch(max(.05, remaining)), [None],
                                 workers=1, timeout=remaining)
    if not result:
        raise TimeoutError('history_provider_failed_or_timed_out')
    if monotonic() >= deadline:
        raise TimeoutError('history_preparation_budget_exhausted')
    return result[0]


def fetch_daily_anchors(code, days, cutoff, *, timeout=10, getter=None):
    """Independent Sina historical unadjusted prices, never today's quote.

    This endpoint's daily volume unit is not inferred. Minute amount/volume
    integrity is checked separately, and every missing daily row stays missing.
    """
    response = (getter or requests.get)(
        'https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData',
        params={'symbol': symbol(code), 'scale': 240, 'ma': 'no', 'datalen': 120},
        headers={'User-Agent': 'Mozilla/5.0'}, timeout=min(10, timeout))
    response.raise_for_status()
    payload = response.json()
    rows = payload.get('result', {}).get('data') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError('historical_daily_provider_invalid')
    anchors, duplicates = {}, set()
    observed = datetime.now(SHANGHAI).isoformat()
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get('day') or row.get('date') or '')[:10]
        if (day not in days
                or datetime.combine(date.fromisoformat(day), time(15), SHANGHAI) > _datetime(cutoff)
                or datetime.combine(date.fromisoformat(day), time(15), SHANGHAI) > _datetime(observed)):
            continue
        anchor = {'kind': 'daily_ohlc', 'code': code, 'day': day,
            'source': 'sina daily unadjusted', 'price_basis': 'unadjusted',
            'complete': True, 'observed_at': observed,
            **{key: row.get(key) for key in ('open', 'high', 'low', 'close')}}
        if day in anchors and any(anchors[day][key] != anchor[key] for key in ('open', 'high', 'low', 'close')):
            duplicates.add(day)
        anchors[day] = anchor
    return {day: anchor for day, anchor in anchors.items() if day not in duplicates}


def _prepare_code(archive, code, days, cutoff, deadline, *, max_pages, minute_fetcher,
                  daily_fetcher, allow_tainted_rebuild):
    raw, states, initial = _audit(archive, code, days, cutoff)
    if initial['fullSessionReady']:
        return {**initial, 'status': 'ready', 'pages': 0, 'repairedDays': [], 'errors': []}
    blocked = set(initial['taintedDays']) if not allow_tainted_rebuild else set()
    pending = set(days) - set(initial['completeDays']) - blocked
    repaired, errors, requested_ends = [], [], []
    if blocked:
        errors.append({'reason': 'tainted_sessions_require_explicit_rebuild', 'days': sorted(blocked)})
    if not pending:
        return {**initial, 'status': 'blocked', 'pages': 0, 'repairedDays': repaired, 'errors': errors}
    try:
        anchors = _call_before_deadline(lambda remaining: daily_fetcher(
            code, days, cutoff, timeout=remaining), deadline)
    except TimeoutError:
        return {**initial, 'status': 'deferred', 'pages': 0, 'repairedDays': repaired,
                'errors': [*errors, {'reason': 'historical_daily_provider_failed_or_timed_out'}]}
    if not isinstance(anchors, dict):
        raise ValueError('historical_daily_anchor_map_required')

    # Even a legacy full session may lack response identity. Fetch it again;
    # never attach a fabricated identity to old bytes or use a request code as a
    # provider response code. An ignored page cursor cannot advance readiness.
    visited = set()
    for _ in range(max(0, max_pages)):
        targets = pending - visited
        if not targets or monotonic() >= deadline:
            break
        target = max(targets)
        end = target.replace('-', '') + '150000'
        requested_ends.append(end)
        try:
            page = _call_before_deadline(lambda remaining: minute_fetcher(
                code, count=8000, end=end, timeout=remaining), deadline)
        except TimeoutError:
            errors.append({'day': target, 'reason': 'historical_minute_provider_failed_or_timed_out'})
            break
        page = list(page)
        conflicts = set()
        normalized = normalize_bars(page, cutoff, conflicts=conflicts)
        original_days = {}
        for row in page:
            day = str(row.get('end', ''))[:10]
            if day in pending and day <= target:
                original_days.setdefault(day, []).append(row)
        # Keep malformed/rejected provider observations as evidence as well.
        # A filtered 239-row replacement must not erase the original 240th row.
        if monotonic() < deadline:
            with archive.connect() as db:
                for day, original in original_days.items():
                    archive._evidence(db, code, day, original, 'historical_preparation_source',
                        {'sourcePolicy': 'fresh_provider_reply_before_normalization', 'requestedEnd': end})
        fetched = {}
        for at, row in normalized.items():
            if at.date().isoformat() in pending and at.date().isoformat() <= target:
                fetched.setdefault(at.date().isoformat(), []).append(row)
        visited.add(target)
        # A page may contain many target sessions. Re-requesting those same
        # partial bytes via another cursor cannot make a missing bar appear.
        visited.update(fetched)
        conflict_days = {at.date().isoformat() for at in conflicts}
        for day in sorted(fetched):
            if monotonic() >= deadline:
                break
            rows, anchor = fetched[day], anchors.get(day)
            coverage = minute_coverage(rows, cutoff, [day])
            full = _complete(rows, day, cutoff)
            if day in conflict_days or not rows:
                errors.append({'day': day, 'reason': 'historical_minute_session_conflicting_or_empty'})
                continue
            if not full:
                normalized_times = {row['end'] for row in rows}
                source_times = set()
                for original in original_days.get(day, []):
                    label = str(original.get('end', ''))
                    try:
                        label = _datetime(label).isoformat()
                    except (TypeError, ValueError):
                        pass
                    source_times.add(label)
                errors.append({'day': day, 'reason': 'historical_minute_session_incomplete',
                    'actualMinutes': len(rows), 'sourceMinuteCount':len(source_times),
                    'normalizationRejectedMinuteTimes':sorted(source_times-normalized_times),
                    'missingMinuteTimes': coverage['missingMinuteTimes'],
                    'missingFields': coverage['missingFields'],
                    'meaning': '真实分钟保留缺口；仅完整同刻窗口可通过正式20日基准'})
            if not anchor or anchor.get('kind') != 'daily_ohlc' or anchor.get('day') != day:
                errors.append({'day': day, 'reason': 'independent_historical_daily_anchor_missing'})
                continue
            try:
                observed = datetime.fromisoformat(str(anchor.get('observed_at') or ''))
                after_close = observed.tzinfo is not None and observed >= datetime.combine(
                    date.fromisoformat(day), time(15), SHANGHAI)
            except (TypeError, ValueError):
                after_close = False
            if not after_close:
                errors.append({'day': day, 'reason': 'historical_daily_anchor_observed_before_session_close'})
                continue
            quality = inspect_session(code, day, rows, anchor)
            if quality['status'] != 'verified':
                errors.append({'day': day, 'reason': 'historical_session_verification_failed',
                               'reasonCodes': quality['reasonCodes']})
                continue
            # Missing sessions can be ingested; unknown/tainted prior bytes are
            # replaced only through full verified rebuild with revision CAS.
            try:
                if day in raw:
                    if full:
                        archive.rebuild(code, day, rows, cutoff, verification=anchor,
                                        expected_revision=session_revision(code, day, raw[day]),
                                        allow_tainted=allow_tainted_rebuild)
                    elif states.get(day, {}).get('status') == 'unknown':
                        archive.replace_unknown_session(code, day, rows, cutoff, verification=anchor,
                                        expected_revision=session_revision(code, day, raw[day]))
                    else:
                        supplemented = archive.supplement_verified_session(code, day, rows, cutoff,
                            verification=anchor, expected_revision=session_revision(code, day, raw[day]))
                        if supplemented.get('unchanged'):
                            errors.append({'day':day, 'reason':supplemented['reason']})
                            continue
                else:
                    written = archive.save(code, rows, cutoff, verification=anchor)
                    if day in written.get('quarantinedDays', []):
                        raise ValueError('concurrent_session_conflict_kept_quarantined')
                repaired.append(day)
                pending.discard(day)
            except (ValueError, sqlite3.Error) as error:
                errors.append({'day': day, 'reason': 'safe_history_write_rejected', 'error': type(error).__name__})
        if not fetched:
            errors.append({'day': target, 'reason': 'provider_returned_no_requested_historical_session'})
    _, _, result = _audit(archive, code, days, cutoff)
    return {**result, 'status': 'ready' if result['ready'] else 'partial',
            'pages': len(requested_ends), 'requestedEnds': requested_ends,
            'repairedDays': repaired, 'errors': errors}


def prepare_history(codes, cutoff, cache_dir, *, timeout=120, max_codes=20,
                    max_pages_per_code=3, minute_fetcher=None, daily_fetcher=None,
                    allow_tainted_rebuild=False, target_session=None):
    """Prepare a rotating bounded batch; return/write one stable status JSON.

    ``cache_dir`` is the minute archive directory, not the parent engine data
    directory. Injected fetchers are read-only and accept the default functions'
    signatures. Tainted rebuild is explicitly opt-in and remains fully verified.
    ``target_session`` may be the next exchange day after the real cutoff's
    close; it selects the baseline window without pretending evidence is future.
    """
    cutoff = _datetime(cutoff)
    target = date.fromisoformat(str(target_session)[:10]) if target_session is not None else cutoff.date()
    codes = list(dict.fromkeys(str(code) for code in codes))
    for code in codes:
        symbol(code)
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATUS_FILE
    days = sorted({str(day)[:10] for day in _prior_sessions(target)
                   if str(day)[:10] < target.isoformat()})[-FORMAL_HISTORY_DAYS:]
    prior = read_preparation_status(directory)
    same = prior.get('requiredDays') == days and prior.get('targetSession') == target.isoformat()
    result = {'version': VERSION, 'targetSession': target.isoformat(), 'evidenceCutoffAt': cutoff.isoformat(),
        'requiredDays': days, 'requiredCount': FORMAL_HISTORY_DAYS,
        'requested': len(codes), 'requestedCodes': codes, 'statusPath': str(path),
        'details': {code: value for code, value in prior.get('details', {}).items() if code in codes} if same else {},
        # Readiness is window-specific; the work cursor is not. Resetting it
        # each day would permanently starve symbols outside the first batch.
        'lastCode': prior.get('lastCode'),
        'allowTaintedRebuild': bool(allow_tainted_rebuild),
        'meaning': '固定20交易日真实分钟及独立日线核验；ready仅代表baselineClock同刻量价基准，fullSessionReady另记；未改写旧信号资格'}
    def checkpoint(status):
        result.update(status=status, updatedAt=datetime.now(SHANGHAI).isoformat(),
            ready=sum(value.get('ready') is True for value in result['details'].values()),
            fullSessionReady=sum(value.get('fullSessionReady') is True for value in result['details'].values()),
            preparedWithUsableClocks=sum(bool(value.get('readyClocks')) for value in result['details'].values()),
            pending=sum(result['details'].get(code, {}).get('ready') is not True for code in codes))
        _write_status(path, result)
    if len(days) != FORMAL_HISTORY_DAYS:
        result['reason'] = 'exact_twenty_exchange_sessions_unavailable'
        checkpoint('unavailable')
        return result
    if any(datetime.combine(date.fromisoformat(day), time(15), SHANGHAI) > cutoff for day in days):
        result['details'] = {}
        result['reason'] = 'required_historical_session_not_closed_at_real_cutoff'
        checkpoint('unavailable')
        return result
    deadline = monotonic() + max(0, timeout)
    archive = MinuteArchive(directory)
    if result['lastCode'] in codes:
        start = codes.index(result['lastCode']) + 1
        order = codes[start:] + codes[:start]
    else:
        order = codes
    processed = 0
    checkpoint('preparing')
    for code in order[:max(0, max_codes)]:
        if monotonic() >= deadline:
            break
        try:
            detail = _prepare_code(archive, code, days, cutoff, deadline,
                max_pages=max_pages_per_code, minute_fetcher=minute_fetcher or fetch_software_bars,
                daily_fetcher=daily_fetcher or fetch_daily_anchors,
                allow_tainted_rebuild=allow_tainted_rebuild)
        except (TimeoutError, ValueError, OSError, TypeError, KeyError, sqlite3.Error) as error:
            detail = {'code': code, 'ready': False, 'status': 'deferred',
                      'errors': [{'reason': 'history_preparation_failed_or_timed_out', 'error': type(error).__name__}]}
        result['details'][code] = {**detail, 'checkedAt': datetime.now(SHANGHAI).isoformat()}
        result['lastCode'] = code
        processed += 1
        checkpoint('preparing')
    result.update(processedThisRun=processed, deferredThisRun=len(codes)-processed,
                  budgetExhausted=monotonic() >= deadline)
    checkpoint('completed' if all(result['details'].get(code, {}).get('ready') is True for code in codes) else 'partial')
    return result
