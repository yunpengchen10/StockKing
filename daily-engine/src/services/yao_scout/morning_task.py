"""Prospective morning-to-close price observations, separate from T+1 trades.

Only explicitly frozen new morning signals receive labels. Publication time is
immutable; a later scan never replaces the first selected stock-day reference.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
import json
import math
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
MORNING_TASK_ID = 'morning_signal_to_same_session_close'
MORNING_QUEUES = ('opening', '0940', '0955', '1030')
MORNING_VERSION = 'morning-frozen-signal-price-to-close-v2'
LARGE_DROP_THRESHOLD = .03  # Declared research risk threshold, not fitted.


def morning_readiness(service, as_of, *, current_candidates=None, current_queue=None,
                      current_data_eligibility=None, refresh=False):
    """Independent queue diagnostics; ordinary reads never scan historical events.

    ``refresh=True`` is an explicit background/close-time audit, not a scan-time
    option. It stores only progress and never fits, promotes or admits a model.
    Pre-publication callers may pass existing data-only eligibility flags for
    ``current_queue`` instead of recomputing admission inside this module.
    """
    from .task_learning import TaskLearner
    now = timestamp(as_of)
    if now is None:
        raise ValueError('readiness_timestamp_with_offset_required')
    if current_queue is not None and current_queue not in MORNING_QUEUES:
        raise ValueError('morning_time_stratum_required')
    rows = list(current_candidates) if current_candidates is not None else None
    states = []
    for queue in MORNING_QUEUES:
        learner = TaskLearner(service, MORNING_TASK_ID, queue)
        if refresh:
            state = learner.refresh_readiness(now)
        else:
            candidates = (rows if current_queue == queue else
                          [row for row in rows if (row.get('morningSignal') or {}).get('queue') == queue]
                          if rows is not None and current_queue is None else None)
            state = learner.readiness(now, current_candidates=candidates,
                current_data_eligibility=current_data_eligibility if current_queue == queue else None)
        states.append(state)
    return {'version': 'morning-readiness-v1', 'asOf': now.isoformat(), 'taskId': MORNING_TASK_ID,
            'currentQueue': current_queue, 'queues': states,
            'current': next((state for state in states if state['queue'] == current_queue), None),
            'diagnosticOnly': True, 'selectionAuthority': False}


def timestamp(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return value.astimezone(TZ) if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def morning_queue(slot, as_of):
    at = timestamp(as_of)
    if at is None or str(slot) == '0920' or not time(9, 30) <= at.time() < time(11, 30):
        return None
    return ('opening' if at.time() < time(9, 40) else '0940' if at.time() < time(9, 55)
            else '0955' if at.time() < time(10, 30) else '1030')


def observation_window(published_at):
    """First full minute whose START is strictly after publication.

    For 09:40:35 (or exactly 09:40:00), freeze 09:41..09:42. Its open is an
    observation proxy, never a claimed fill; no earlier minute's low is used.
    """
    published = timestamp(published_at)
    if published is None or not time(9, 30) <= published.time() < time(11, 30):
        return None
    start = published.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if start.time() >= time(11, 30):
        start = start.replace(hour=13, minute=0)
    return start, start + timedelta(minutes=1)


def morning_signal_state(candidate, previous=None, *, selected=False):
    previous = previous or {}
    old = previous.get('morningSignal') or previous
    existing = old.get('firstReferenceTime') is not None
    return {
        'firstReferenceTime': old.get('firstReferenceTime') if existing else
            candidate.get('decision_at') if selected else None,
        'firstReferencePrice': old.get('firstReferencePrice') if existing else
            number(candidate.get('referencePrice') or candidate.get('signalPrice')) if selected else None,
        'currentSignalState': ('continuing' if existing else 'new') if selected else
            ('invalidated' if existing else 'research'),
        'isRepeatedSignal': bool(existing),
        'referenceMeaning': '首次实际入选参考价；不因重复扫描重置，不代表成交',
    }


def freeze_morning_signal(candidate, published_at, slot, quality, *, selected, official, previous=None):
    from .task_learning import task_contract
    from .learning_contracts import frozen_learning_fields
    signal = dict(candidate.get('morningSignal') or {})
    if not signal:
        return None  # Never synthesize a morning task for legacy records.
    published = timestamp(published_at)
    # Freeze the price actually attached to this publication. A subsequent
    # minute's price must never reset a losing recommendation's denominator.
    reference = number(candidate.get('referencePrice') if candidate.get('referencePrice') is not None
                       else candidate.get('signalPrice'))
    quote = candidate.get('quote') or {}
    source = timestamp(quote.get('provider_timestamp') or quote.get('source_time') or candidate.get('sourceTime'))
    quote_price = number(quote.get('price'))
    reference_valid = bool(published and source and reference is not None and reference > 0
        and quote_price is not None and quote_price > 0 and abs(reference-quote_price) <= .000001
        and 0 <= (published-source).total_seconds() <= 30)
    queue = morning_queue(slot, published)
    window = observation_window(published)
    valid = bool(signal.get('version') == MORNING_VERSION and queue and signal.get('queue') == queue and window
                 and signal.get('learningTaskContractId') == task_contract(MORNING_TASK_ID, queue)['taskContractId'])
    state = morning_signal_state(candidate, previous, selected=selected)
    if selected and not state['isRepeatedSignal'] and published:
        state['firstReferenceTime'] = published.isoformat()
    contracts = frozen_learning_fields(task_contract(MORNING_TASK_ID, queue)) if valid else {}
    return {**signal, **contracts, **state,
        'publishedAt': published.isoformat() if published else None,
        'referencePrice': reference, 'referenceSourceTime': source.isoformat() if source else None,
        'referenceVerifiedAtPublication': reference_valid,
        'actualSlot': str(slot), 'selected': bool(selected),
        'rank': candidate.get('rank') if selected else None,
        'observationStartAt': window[0].isoformat() if valid else None,
        'observationMinuteEndAt': window[1].isoformat() if valid else None,
        'targetSession': published.date().isoformat() if published else None,
        'trainingEligibleAtSignal': bool(valid and reference_valid and official and quality.get('eligible') is True
            and signal.get('researchEligibleAtDecision') is True
            and signal.get('sourceUsableAtCompletion') is not False),
        'qualityKnownAt': quality.get('qualityKnownAt'), 'publicationValid': valid}


def morning_outcome(signal, bars, as_of, *, calendar_provider):
    now, published = timestamp(as_of), timestamp(signal.get('publishedAt'))
    result = {'version': MORNING_VERSION, 'queue': signal.get('queue'), 'status': 'pending',
        'targetSession': signal.get('targetSession'), 'observedReturn': None,
        'observedMae': None, 'observedMfe': None, 'up': None, 'largeDrop': None,
        'nextCompleteMinutePrice': None, 'nextCompleteMinuteReturn': None,
        'executionVerified': False,
        'returnMeaning': '发布时冻结的推荐参考价至当日收盘涨跌；非成交收益，A股当日买入不能卖出',
        'excursionMeaning': '发布后固定完整分钟路径相对推荐参考价的最大不利/有利变动；不含发布所在残缺分钟'}
    if (signal.get('version') != MORNING_VERSION or published is None or now is None
            or signal.get('publicationValid') is not True):
        return {**result, 'status': 'unverified', 'reason': 'publication_contract_unverified'}
    reference, source = number(signal.get('referencePrice')), timestamp(signal.get('referenceSourceTime'))
    if (signal.get('referenceVerifiedAtPublication') is not True or reference is None or reference <= 0
            or source is None or not 0 <= (published-source).total_seconds() <= 30):
        return {**result, 'status': 'unverified', 'reason': 'publication_reference_quote_unverified'}
    day = published.date()
    due = datetime.combine(day, time(15, 1), TZ)
    if now < due:
        return result
    try:
        sessions = list(calendar_provider(day, day))
    except Exception:
        sessions = []
    if day not in sessions:
        return {**result, 'status': 'incomplete', 'reason': 'trading_calendar_unverified'}
    window = observation_window(published)
    if (not window or signal.get('observationStartAt') != window[0].isoformat()
            or signal.get('observationMinuteEndAt') != window[1].isoformat()
            or signal.get('targetSession') != day.isoformat()):
        return {**result, 'status': 'unverified', 'reason': 'observation_window_contract_mismatch'}
    expected = [datetime.combine(day, time(9, 31), TZ)+timedelta(minutes=i) for i in range(120)]
    expected += [datetime.combine(day, time(13, 1), TZ)+timedelta(minutes=i) for i in range(120)]
    expected = [at for at in expected if at >= window[1]]
    rows, conflicts = {}, set()
    for raw in bars:
        at = timestamp(raw.get('end'))
        if at not in expected or at > now:
            continue
        if at in rows and any(rows[at].get(key) != raw.get(key) for key in ('open', 'high', 'low', 'close')):
            conflicts.add(at)
        rows[at] = raw
    missing = [at.isoformat() for at in expected if at not in rows or at in conflicts]
    if missing:
        return {**result, 'status': 'incomplete', 'reason': 'complete_post_publication_path_missing',
                'missingMinutes': len(missing), 'missingMinuteTimes': missing}
    path = [rows[at] for at in expected]
    for row in path:
        op, high, low, close = [number(row.get(key)) for key in ('open', 'high', 'low', 'close')]
        if (any(value is None or value <= 0 for value in (op, high, low, close))
                or not low <= min(op, close) <= max(op, close) <= high or not row.get('source')
                or row.get('data_quality_status') == 'tainted'
                or any(row.get(key) is True for key in ('corporate_action', 'no_price_limit', 'suspended'))):
            return {**result, 'status': 'unverified', 'reason': 'invalid_or_exceptional_price_path'}
    if number(path[0].get('volume_shares')) is None or number(path[0].get('volume_shares')) <= 0:
        return {**result, 'status': 'incomplete', 'reason': 'reference_minute_has_no_observed_trades'}
    next_minute, close = float(path[0]['open']), float(path[-1]['close'])
    ret = close/reference-1
    mae = max(0., 1-min(float(row['low']) for row in path)/reference)
    return {**result, 'status': 'mature_price_observation', 'observationStartAt': window[0].isoformat(),
        'observationMinuteEndAt': window[1].isoformat(), 'referencePrice': reference,
        'referenceSourceTime': source.isoformat(),
        'nextCompleteMinutePrice': next_minute, 'nextCompleteMinuteReturn': close/next_minute-1,
        'targetClose': close, 'observedReturn': ret, 'observedMae': mae,
        'observedMfe': max(0., max(float(row['high']) for row in path)/reference-1),
        'up': ret > 0, 'largeDrop': mae >= LARGE_DROP_THRESHOLD,
        'largeDropThreshold': LARGE_DROP_THRESHOLD, 'labelEndAt': due.isoformat()}


def review_morning_signals(service, as_of, cache=None):
    from .task_learning import lineage_valid
    now = timestamp(as_of)
    cache = cache if cache is not None else {}
    with service._connect() as db:
        rows = db.execute("SELECT e.signal_id,e.code,e.decision_at,e.snapshot_json,o.result_json "
            "FROM signal_events e LEFT JOIN morning_outcomes o ON o.signal_id=e.signal_id "
            "WHERE json_extract(e.snapshot_json,'$.morningSignal.version')=? "
            "AND substr(e.decision_at,1,10)<=? ORDER BY e.code,e.decision_at",
            (MORNING_VERSION, now.date().isoformat())).fetchall()
    changed = []
    for row in rows:
        snapshot = json.loads(row['snapshot_json'])
        signal = snapshot['morningSignal']
        old = json.loads(row['result_json']) if row['result_json'] else {}
        if old.get('status') == 'mature_price_observation' and lineage_valid(service, old.get('dataLineage')):
            continue
        due = morning_outcome(signal, [], now, calendar_provider=service.calendar_provider)
        if due['status'] == 'pending':
            continue
        if row['code'] not in cache:
            try:
                cache[row['code']] = service._bar_rows(service.bar_fetcher(row['code'], now), now)
            except Exception:
                cache[row['code']] = []
        result = morning_outcome(signal, cache[row['code']], now, calendar_provider=service.calendar_provider)
        if result.get('labelEndAt'):
            lineage = service._label_lineage(row['code'], signal['publishedAt'], result['labelEndAt'])
            result['dataLineage'] = lineage
            if not lineage_valid(service, lineage):
                result.update(status='unverified', reason='label_data_lineage_unverified')
        # Small append/update transaction only after I/O and full label checks.
        with service._connect() as db:
            db.execute('INSERT INTO morning_outcomes VALUES(?,?,?) ON CONFLICT(signal_id) DO UPDATE SET '
                       'reviewed_at=excluded.reviewed_at,result_json=excluded.result_json',
                       (row['signal_id'], now.isoformat(), json.dumps(result, ensure_ascii=False)))
        changed.append({'signalId': row['signal_id'], 'code': row['code'], **result})
    return {'items': changed, 'updated': len(changed), 'statusCounts': dict(Counter(row['status'] for row in changed)),
            'meaning': '独立上午T0价格标签；不重写旧推荐、不替代T+1成交评估'}


def summarize_morning_records(items):
    """First actual selected stock-day, separate from first control stock-day.

    Missing outcomes on an earlier decision cannot be hidden by a later winner.
    Slot strata remain available without calling repeated scans independent.
    """
    first, strata = {}, {}
    for row in sorted(items, key=lambda r: (r.get('publishedAt') or r.get('decisionAt') or '', r.get('signalId', ''))):
        day = (row.get('publishedAt') or row.get('decisionAt') or '')[:10]
        key = (row.get('code'), day, bool(row.get('selected')), row.get('version'))
        first.setdefault(key, row)
        strata.setdefault((*key, row.get('queue')), row)
    def summarize(rows):
        rows = list(rows)
        mature = [row for row in rows if row.get('status') == 'mature_price_observation']
        return {'signalCount': len(rows), 'matureCount': len(mature),
            'upCount': sum(row.get('up') is True for row in mature),
            'downCount': sum((number(row.get('observedReturn')) or 0) < 0 for row in mature),
            'largeDropCount': sum(row.get('largeDrop') is True for row in mature),
            'meanObservedReturn': sum(row['observedReturn'] for row in mature)/len(mature) if mature else None,
            'meanObservedMae': sum(row['observedMae'] for row in mature)/len(mature) if mature else None,
            'statusCounts': dict(Counter(row.get('status', 'pending') for row in rows))}
    return {'selected': summarize(row for key, row in first.items() if key[2]),
        'controls': summarize(row for key, row in first.items() if not key[2]),
        'byQueue': {queue: {kind: summarize(row for key, row in strata.items() if key[2] == selected and key[-1] == queue)
                           for kind, selected in (('selected', True), ('controls', False))} for queue in MORNING_QUEUES},
        'dedupe': '同股同交易日取首次实际入选；对照另计，保留首次缺失，不以后续较优价格替换',
        'rawRecordCount': len(items), 'returnMeaning': 'price_observation_not_trading_return'}


def morning_dataset(service, now, contract, queue):
    """Keep the entire prospective qualified cohort, including rejected controls."""
    from .local_algorithm import algorithm_contract
    from .learning_contracts import compatibility, learning_contracts, task_data_compatible
    from .signal_learning import _feature_values
    algorithm_id = algorithm_contract()['contractId']
    data_contract = learning_contracts(contract)['learningDataContractId']
    with service._connect() as db:
        rows = db.execute("SELECT e.signal_id,e.code,e.decision_at,e.snapshot_json,r.run_id,r.trading_day,"
            "r.created_at,o.result_json,o.reviewed_at,COALESCE(json_extract(r.scan_json,'$.algorithmManifest'),"
            "json_extract(r.scan_json,'$.result.algorithm')) manifest_json FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
            "LEFT JOIN morning_outcomes o ON o.signal_id=e.signal_id WHERE r.official=1 AND r.trading_day<=? "
            "AND (json_extract(e.snapshot_json,'$.morningSignal.learningTaskContractId')=? "
            "OR json_extract(e.snapshot_json,'$.morningSignal.learningDataContractId')=?) "
            "AND json_extract(e.snapshot_json,'$.morningSignal.trainingEligibleAtSignal')=1 "
            "ORDER BY r.trading_day,e.decision_at,e.code",
            (now.date().isoformat(), contract['taskContractId'], data_contract)).fetchall()
        runs = db.execute("SELECT trading_day,created_at,json_extract(scan_json,'$.algorithmContractId') algorithm_id,"
            "COALESCE(json_extract(scan_json,'$.algorithmManifest'),json_extract(scan_json,'$.result.algorithm')) manifest_json "
            "FROM signal_runs WHERE official=1 AND trading_day<=? "
            "AND (json_extract(scan_json,'$.morningTaskContractId')=? OR json_extract(scan_json,'$.morningDataContractId')=?)",
            (now.date().isoformat(), contract['taskContractId'], data_contract)).fetchall()
    days = sorted({row['trading_day'] for row in runs if timestamp(row['created_at']) <= now
                   and datetime.fromisoformat(row['trading_day']+'T15:01:00+08:00') <= now
                   and compatibility({'algorithmContractId': row['algorithm_id'],
                       'algorithmManifest': json.loads(row['manifest_json']) if row['manifest_json'] else None}, task_id=MORNING_TASK_ID)['compatible']})
    result = []
    for row in rows:
        if row['trading_day'] not in days:
            continue
        snapshot = json.loads(row['snapshot_json'])
        audit = compatibility({**snapshot, 'algorithmManifest': json.loads(row['manifest_json']) if row['manifest_json'] else None}, task_id=MORNING_TASK_ID)
        if not audit['compatible']:
            continue
        signal = snapshot['morningSignal']
        if not task_data_compatible(signal, contract):
            continue
        outcome = json.loads(row['result_json']) if row['result_json'] else {}
        published = timestamp(signal.get('publishedAt'))
        publication_ok = bool(published and published == timestamp(row['created_at']) and published <= now
                              and signal.get('queue') == queue and signal.get('publicationValid') is True)
        ret, mae = number(outcome.get('observedReturn')), number(outcome.get('observedMae'))
        complete = bool(publication_ok and outcome.get('status') == 'mature_price_observation'
            and outcome.get('version') == signal.get('version')
            and outcome.get('targetSession') == signal.get('targetSession')
            and outcome.get('observationStartAt') == signal.get('observationStartAt')
            and outcome.get('referencePrice') == signal.get('referencePrice')
            and outcome.get('referenceSourceTime') == signal.get('referenceSourceTime')
            and ret is not None and mae is not None and mae >= 0
            and timestamp(outcome.get('labelEndAt')) is not None and timestamp(outcome['labelEndAt']) <= now
            and timestamp(row['reviewed_at']) is not None and timestamp(row['reviewed_at']) <= now)
        item = {'signalId': row['signal_id'], 'runId': row['run_id'], 'code': row['code'],
            'day': row['trading_day'], 'decisionAt': row['decision_at'], 'publishedAt': signal.get('publishedAt'),
            'observationStartAt': signal.get('observationStartAt'), 'queue': queue,
            'features': _feature_values(snapshot), 'selected': signal.get('selected') is True,
            'selectionRank': signal.get('rank'),
            'baselineSelected': signal.get('ruleBaselineSelected') is True,
            'baselineRank': signal.get('ruleBaselineRank'), 'dataLineage': snapshot.get('dataLineage'),
            'labelDataLineage': outcome.get('dataLineage'), 'labelEndAt': outcome.get('labelEndAt'),
            'labelKnownAt': row['reviewed_at'], 'outcome': outcome, 'complete': complete,
            'contractCompatibility': audit}
        if complete:
            item.update(net=ret, observedMae=mae, eventLabel=int(ret > 0))
        result.append(item)
    return result, days
