"""Daily cohorts freeze references and produce only time-ordered gross labels."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import sqlite3
import zlib

import pytest

from src.services.yao_scout import daily_observation_outcomes as mod
from src.services.yao_scout.signal_learning import SignalLearningService

NOW = datetime(2026, 10, 22, 10, 30, tzinfo=mod.TZ)
DAYS = [date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26),
        date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)]


def calendar(start, end):
    return [day for day in DAYS if start <= day <= end]


def candidate(code='600001', price=10., at=NOW, version='king-daily-primary-v4'):
    return {'code': code, 'name': code, 'decision_at': at.isoformat(), 'referencePrice': price,
            'signalPrice': price, 'observationOnly': True, 'formalEligible': False, 'entryEligible': False,
            'executable': False, 'trainingEligible': False,
            'quote': {'code': code, 'price': price, 'source': 'sina', 'source_time': at.isoformat()},
            'dailySignal': {'taskId': 'daily_structure_to_next_session', 'version': version, 'selected': False}}


def scan(*rows, run_id='one'):
    return {'run_id': run_id, 'status': 'completed_observations', 'primarySelectionMode': 'daily_conditional',
            'candidates': [], 'dailyCandidates': list(rows[:1]), 'dailyObservationResearch': list(rows),
            'dailyProfileCandidates': {'regular': list(rows[:1])}}


def service(tmp_path, clock=None):
    return SignalLearningService(tmp_path, calendar_provider=calendar, clock=clock or (lambda: NOW))


def fixture_close(tmp_path, code, day, price, *, known=None, sparse=False, status='verified', tainted=False):
    """Actual read-only SQLite close path, with provider security identity."""
    path = tmp_path / 'minute_history' / 'minute-bars-v2.sqlite3'
    path.parent.mkdir(exist_ok=True)
    known = known or datetime.combine(day, datetime.min.time(), mod.TZ).replace(hour=16)
    bars = []
    for hour, minute in ((9, 31), (13, 1)):
        for offset in range(120):
            stamp = datetime.combine(day, datetime.min.time(), mod.TZ).replace(hour=hour, minute=minute) + timedelta(minutes=offset)
            bars.append({'end': stamp.isoformat(), 'open': price, 'high': price, 'low': price, 'close': price,
                         'source': 'tdx-mac', 'volume_shares': 100, 'amount_cny': 100 * price,
                         'identity_evidence': {'status': 'provider_reply_checked', 'requested_code': code,
                            'response_code': code, 'requested_market': 1, 'response_market': 1,
                            'period': '1', 'page_cursor_checked': True}})
    if sparse:
        bars = bars[-1:]
    if tainted:
        bars[-1]['conflicting_observations'] = True
    anchor = {'kind': 'daily_ohlc', 'code': code, 'day': day.isoformat(), 'source': 'sina daily unadjusted',
              'price_basis': 'unadjusted', 'open': price, 'high': price, 'low': price, 'close': price,
              'complete': True, 'observed_at': known.isoformat(), 'known_at': known.isoformat()}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sessions(code TEXT,day TEXT,payload BLOB,PRIMARY KEY(code,day))')
        db.execute('CREATE TABLE IF NOT EXISTS session_quality(code TEXT,day TEXT,status TEXT,verification_json TEXT,PRIMARY KEY(code,day))')
        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (code, day.isoformat(), zlib.compress(json.dumps(bars).encode())))
        db.execute('INSERT OR REPLACE INTO session_quality VALUES(?,?,?,?)', (code, day.isoformat(), status, json.dumps(anchor)))
    return path


def test_v4_observation_and_full_controls_persist_without_formal_or_minute_promotion(tmp_path):
    svc = service(tmp_path)
    result = scan(candidate(), candidate('600002'))
    # Even an erroneous upstream placement in candidates cannot authorize it.
    result['candidates'] = [candidate()]
    original = deepcopy(result)
    saved = svc.persist_scan(result, '1030', NOW, True)
    assert result == original
    assert saved['learningLedger']['selectedCount'] == saved['learningLedger']['dailySelectedCount'] == 0
    assert saved['learningLedger']['dailyObservationCount'] == 2
    assert saved['dailyCandidates'][0]['dailySignal']['selected'] is False
    assert saved['dailyCandidates'][0]['dailySignal']['selectedProfiles'] == []
    items = svc.daily_observation_reviews()['items']
    assert {row['first_category'] for row in items} == {'observation_watchlist', 'unselected_control'}
    assert all(row['reference_verified'] for row in items)
    with svc._connect() as db:
        assert db.execute('SELECT SUM(selected),SUM(training_eligible),SUM(review_eligible) FROM signal_events').fetchone()[:] == (0, 0, 0)
        assert db.execute('SELECT COUNT(*) FROM forward_predictions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM signal_outcomes').fetchone()[0] == 0
    assert svc._dataset(NOW) == []


def test_reference_and_first_cohort_never_reset_on_promotion_or_price_drop(tmp_path):
    clock = [NOW]
    svc = service(tmp_path, lambda: clock[0])
    first = scan(candidate(), candidate('600002', price=10.))
    svc.persist_scan(first, 'live', NOW, False)
    clock[0] += timedelta(minutes=5)
    svc.persist_scan(scan(candidate('600002', price=8., at=clock[0]), run_id='later'), 'live', clock[0], False)
    row = next(row for row in svc.daily_observation_reviews()['items'] if row['code'] == '600002')
    assert row['reference_price'] == 10. and row['reference_source_time'] == NOW.isoformat()
    assert row['first_category'] == 'unselected_control'
    assert [item['categories'] for item in row['appearances']] == [['unselected_control'], ['observation_watchlist']]


def test_new_task_version_gets_distinct_cohort_and_official_replay_does_not_backfill(tmp_path):
    clock = [NOW]
    svc = service(tmp_path, lambda: clock[0])
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    clock[0] += timedelta(minutes=1)
    replay = svc.persist_scan(scan(candidate(price=8., at=clock[0], version='king-daily-primary-v5')), '1030', clock[0], True)
    assert replay['learningLedger']['idempotentReplay']
    assert svc.daily_observation_reviews()['total'] == 1
    svc.persist_scan(scan(candidate(price=8., at=clock[0], version='king-daily-primary-v5'), run_id='new'), 'live', clock[0], False)
    assert svc.daily_observation_reviews()['total'] == 2
    # Replaying a pre-upgrade run never inserts retrospective observations.
    with svc._connect() as db:
        db.execute('DELETE FROM daily_observation_appearances')
        db.execute('DELETE FROM daily_observation_signals')
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    assert svc.daily_observation_reviews()['total'] == 0


def test_exact_exchange_horizons_maturity_known_at_and_immutable_gross_labels(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    fixture_close(tmp_path, '600001', DAYS[1], 9.)
    fixture_close(tmp_path, '600001', DAYS[3], 11.)
    fixture_close(tmp_path, '600001', DAYS[5], 12.)
    before = NOW.replace(day=23, hour=14, minute=59)
    result = mod.review(svc, before)
    assert result['statusCounts'] == {'not_matured': 3}
    assert mod.review(svc, before)['updated'] == 0
    # 15:00 is reached, but independent anchor is not known until 16:00.
    mod.review(svc, before.replace(hour=15, minute=30))
    first = svc.daily_observation_reviews(as_of=before.replace(hour=15, minute=30))['items'][0]['outcomes'][0]
    assert first['status'] == 'unverified_close' and first['gross_reference_change_pct'] is None
    end = NOW.replace(day=29, hour=16)
    result = mod.review(svc, end)
    rows = svc.daily_observation_reviews(as_of=end)['items'][0]['outcomes']
    assert [row['target_day'] for row in rows] == [DAYS[index].isoformat() for index in (1, 3, 5)]
    assert [row['gross_reference_change_pct'] for row in rows] == pytest.approx([-10., 10., 20.])
    assert all(row['status'] == 'observed' and row['net_return'] is None for row in rows)
    assert all(row['label_end_at'] < row['label_known_at'] <= row['reviewed_at'] for row in rows)
    assert mod.review(svc, end)['updated'] == 0
    assert mod.review(svc, NOW)['statusCounts'] == {}
    assert svc.daily_observation_reviews(as_of=NOW)['items'][0]['outcomes'] == []
    with svc._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM daily_observation_outcomes').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM signal_outcomes').fetchone()[0] == 0
        assert db.execute('SELECT MAX(training_eligible),MAX(review_eligible) FROM signal_events').fetchone()[:] == (0, 0)


@pytest.mark.parametrize('change,gap', [
    ({'quote': {'price': 10., 'source': 'sina', 'source_time': (NOW-timedelta(minutes=1)).isoformat()}}, 'reference_source_not_fresh_at_publication'),
    ({'quote': {'price': 9., 'source': 'sina', 'source_time': NOW.isoformat()}}, 'reference_price_or_matching_quote_missing'),
    ({'quote': {'price': 10., 'source_time': NOW.isoformat()}}, 'reference_source_missing'),
    ({'quote': {'code': '600099', 'price': 10., 'source': 'sina', 'source_time': NOW.isoformat()}}, 'reference_security_mismatch'),
    ({'quote': {'price': 10., 'source': 'sina', 'source_time': (NOW+timedelta(seconds=1)).isoformat()}}, 'reference_source_not_fresh_at_publication'),
])
def test_bad_first_reference_stays_null_even_if_later_quote_recovers(tmp_path, change, gap):
    clock = [NOW]
    svc = service(tmp_path, lambda: clock[0])
    row = candidate()
    row.update(change)
    svc.persist_scan(scan(row), '1030', NOW, True)
    clock[0] += timedelta(minutes=1)
    svc.persist_scan(scan(candidate(at=clock[0]), run_id='new'), 'live', clock[0], False)
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    cutoff = NOW.replace(day=23, hour=16)
    mod.review(svc, cutoff)
    item = svc.daily_observation_reviews(as_of=cutoff)['items'][0]
    assert gap in item['reference_gaps'] and item['reference_verified'] == 0
    assert item['outcomes'][0]['status'] == 'unverified_reference'
    assert item['outcomes'][0]['gross_reference_change_pct'] is None


@pytest.mark.parametrize('status,tainted', [('unknown', False), ('tainted', False), ('verified', True)])
def test_unverified_or_conflicting_archive_cannot_produce_return(tmp_path, status, tainted):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    path = fixture_close(tmp_path, '600001', DAYS[1], 11., status=status, tainted=tainted)
    original = path.read_bytes()
    cutoff = NOW.replace(day=23, hour=16)
    mod.review(svc, cutoff)
    item = svc.daily_observation_reviews(as_of=cutoff)['items'][0]['outcomes'][0]
    assert item['status'] == 'unverified_close' and item['gross_reference_change_pct'] is None
    assert path.read_bytes() == original


def test_verified_complete_daily_anchor_can_supply_sparse_minute_close(tmp_path):
    fixture_close(tmp_path, '600001', DAYS[1], 11., sparse=True)
    result = mod.read_verified_close(tmp_path, '600001', DAYS[1].isoformat(), NOW.replace(day=23, hour=16))
    assert result['status'] == 'verified' and result['close'] == 11.
    assert result['evidence'] == 'verified_complete_daily_ohlc'


def test_close_evidence_known_time_is_separate_from_label_creation_time(tmp_path):
    svc = service(tmp_path)
    row = candidate()
    row['quote']['locked_limit_up'] = True  # A reference does not prove any fill.
    svc.persist_scan(scan(row), '1030', NOW, True)
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    cutoff = NOW.replace(day=26, hour=16)
    mod.review(svc, cutoff)
    outcome = svc.daily_observation_reviews(as_of=cutoff)['items'][0]['outcomes'][0]
    assert outcome['label_known_at'] == cutoff.isoformat()
    assert outcome['details']['closeEvidence']['known_at'] < outcome['label_known_at']
    assert outcome['gross_reference_change_pct'] == pytest.approx(10.)
    assert outcome['net_return'] is None and not outcome['details']['is_trade_return']
    assert len(outcome['details']['closeEvidence']['session_payload_sha256']) == 64
    with svc._connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE daily_observation_outcomes SET net_return=.1')


def test_missing_calendar_and_missing_close_keep_coverage_denominator(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate(), candidate('600002')), '1030', NOW, True)
    cutoff = NOW.replace(day=29, hour=16)
    svc.calendar_provider = lambda *_: []
    mod.review(svc, cutoff)
    assert svc.daily_observation_reviews(as_of=cutoff)['items'][0]['outcomes'][0]['status'] == 'calendar_unavailable'
    svc.calendar_provider = calendar
    mod.review(svc, cutoff)
    report = svc.daily_observation_reviews(as_of=cutoff)
    assert sum(group['signals'] for group in report['groups']) == 6
    assert sum(group['covered'] for group in report['groups']) == 0
    assert all(row['gross_reference_change_pct'] is None for item in report['items'] for row in item['outcomes'])


def test_new_day_queue_and_service_review_integrate_without_training(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    assert svc.review_requirements(NOW) == {}
    cutoff = NOW.replace(day=23, hour=16)
    assert svc.review_requirements(cutoff) == {'600001': ['2026-10-23']}
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    result = svc.review_due(cutoff)
    assert result['dailyObservations']['statusCounts'] == {'observed': 1, 'not_matured': 2}
    assert svc.review_requirements(cutoff) == {}


def test_empty_daily_observation_is_not_a_minute_cash_session(tmp_path):
    from src.services.yao_scout.local_algorithm import algorithm_contract
    svc = service(tmp_path)
    svc.persist_scan({'status': 'completed_observations', 'candidates': [],
                      'primarySelectionMode': 'daily_observation',
                      'modelVersion': algorithm_contract()['scoreVersion']}, '1030', NOW, True)
    with svc._connect() as db:
        result = json.loads(db.execute('SELECT scan_json FROM signal_runs').fetchone()[0])
    assert result['algorithmContractId'] is None and 'morningTaskContractId' not in result


def test_same_code_copied_minute_selection_cannot_promote_daily_observation(tmp_path):
    svc = service(tmp_path)
    result = scan(candidate())
    result['candidates'] = [{'code': '600001', 'referencePrice': 10., 'decision_at': NOW.isoformat()}]
    saved = svc.persist_scan(result, '1030', NOW, True)
    assert saved['learningLedger']['selectedCount'] == 0
    with svc._connect() as db:
        row = db.execute('SELECT selected,training_eligible,review_eligible,snapshot_json FROM signal_events').fetchone()
    assert row[:3] == (0, 0, 0)
    assert json.loads(row[3])['dailySignal']['selected'] is False


def test_reviews_api_filters_and_pages_details_but_aggregates_full_coverage(tmp_path):
    clock = [NOW]
    svc = service(tmp_path, lambda: clock[0])
    svc.persist_scan(scan(candidate(), candidate('600002')), '1030', NOW, True)
    clock[0] = NOW.replace(day=23, hour=16)
    mod.review(svc, clock[0])
    with svc._connect() as db:
        excluded = db.execute("SELECT observation_id FROM daily_observation_signals WHERE code='600002'").fetchone()[0]
        # Invalid JSON outside the requested detail page must never be decoded.
        db.execute("UPDATE daily_observation_signals SET snapshot_json='invalid' WHERE observation_id=?", (excluded,))
        db.execute("UPDATE daily_observation_outcomes SET details_json='invalid' WHERE observation_id=?", (excluded,))
        db.execute("UPDATE daily_observation_appearances SET categories_json='invalid' WHERE observation_id=?", (excluded,))
        plan = db.execute('EXPLAIN QUERY PLAN SELECT COUNT(*) FROM daily_observation_appearances WHERE run_id=?', ('x',)).fetchall()
        assert any('daily_observation_appearance_run' in row[3] for row in plan)
    filters = {'date': NOW.date().isoformat(), 'version': 'king-daily-primary-v4', 'horizon': 1, 'limit': 1}
    api = svc.reviews(filters)
    report = api['dailyObservationReview']
    assert api['items'] == [] and api['count'] == 0  # Independent from executable reviews.
    assert report['count'] == 1 and report['total'] == 2 and report['truncated']
    assert report['items'][0]['code'] == '600001'
    assert [row['horizon'] for row in report['items'][0]['outcomes']] == [1]
    assert sum(group['signals'] for group in report['groups']) == 2
    assert report['groupScope'] == 'all_filtered_signals'
    filtered = svc.reviews({**filters, 'symbol': '600001.SH'})['dailyObservationReview']
    assert filtered['total'] == 1 and not filtered['truncated']
    assert svc.reviews({**filters, 'symbol': '600099'})['dailyObservationReview']['items'] == []
    assert svc.reviews({**filters, 'version': 'other'})['dailyObservationReview']['total'] == 0
    empty_filter = svc.reviews({**filters, 'symbol': '600099'})['dailyObservationReview']
    assert empty_filter['healthScope'] == 'all_daily_observations'
    assert empty_filter['health'] == report['health']
    assert empty_filter['health']['signalCount'] == 2 and empty_filter['health']['dueCount'] == 2


def test_empty_review_does_not_scan_child_tables_and_unverified_reference_is_terminal(tmp_path, monkeypatch):
    svc = service(tmp_path)
    statements = []
    connect = svc._connect
    def traced():
        db = connect()
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(svc, '_connect', traced)
    assert mod.reviews(svc)['items'] == []
    assert len(statements) == 1 and 'COUNT(*)' in statements[0]
    bad = candidate()
    bad['quote'].pop('source')
    svc.persist_scan(scan(bad), '1030', NOW, True)
    end = NOW.replace(day=29, hour=16)
    assert mod.review(svc, end)['updated'] == 3
    assert mod.review(svc, end + timedelta(days=1))['updated'] == 0


def test_compact_control_uses_only_same_scan_quote_map_and_freezes_it(tmp_path):
    clock = [NOW]
    svc = service(tmp_path, lambda: clock[0])
    result = scan(candidate())
    result['dailyAssessment'] = {'version': 'king-daily-primary-v4'}
    result['dailyRejectionLedger'] = [{'code': '600002', 'name': 'control', 'reasons': ['weak_structure']}]
    quote = candidate('600002', price=8.)['quote']
    result['dailyObservationQuotes'] = {'600002': quote}
    original = deepcopy(result)
    svc.persist_scan(result, 'live', NOW, False)
    assert result == original
    item = next(x for x in svc.daily_observation_reviews()['items'] if x['code'] == '600002')
    assert item['reference_verified'] == 1 and item['reference_price'] == 8.
    assert item['snapshot']['quote'] == quote and item['first_category'] == 'unselected_control'
    clock[0] += timedelta(minutes=2)
    result['run_id'] = 'later-control'
    result['dailyObservationQuotes'] = {'600002': candidate('600002', price=7., at=clock[0])['quote']}
    svc.persist_scan(result, 'live', clock[0], False)
    again = next(x for x in svc.daily_observation_reviews()['items'] if x['code'] == '600002')
    assert again['reference_price'] == 8. and again['reference_source_time'] == NOW.isoformat()


@pytest.mark.parametrize('quote', [
    candidate('600099')['quote'],
    candidate('600002', at=NOW - timedelta(seconds=31))['quote'],
    candidate('600002', at=NOW + timedelta(seconds=1))['quote'],
])
def test_control_map_does_not_authorize_wrong_security_stale_or_future_quote(tmp_path, quote):
    svc = service(tmp_path)
    result = scan()
    result['dailyAssessment'] = {'version': 'king-daily-primary-v4'}
    result['dailyRejectionLedger'] = [{'code': '600002'}]
    result['dailyObservationQuotes'] = {'600002': quote}
    svc.persist_scan(result, '1030', NOW, True)
    assert svc.daily_observation_reviews()['items'][0]['reference_verified'] == 0


def test_queue_resumes_after_budget_across_new_service_and_checkpoints_atomically(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate(), candidate('600002')), '1030', NOW, True)
    end = NOW.replace(day=23, hour=16)
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    fixture_close(tmp_path, '600002', DAYS[1], 9.)
    first = mod.review(svc, end, max_labels=1)
    assert first['checked'] == 1 and first['budgetExhausted']
    restarted = service(tmp_path)
    for _ in range(5):
        mod.review(restarted, end, max_labels=1)
    with restarted._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM daily_observation_outcomes').fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM daily_observation_outcomes WHERE status='observed'").fetchone()[0] == 2
        assert db.execute('SELECT SUM(attempts) FROM daily_observation_review_queue').fetchone()[0] == 6
    assert mod.review(restarted, end)['updated'] == 0
    health = restarted.daily_observation_status(end)
    assert health['observedCount'] == 2 and health['dueCount'] == 0 and health['pendingCount'] == 4


def test_due_health_is_read_only_and_detects_stale_not_matured_without_running_worker(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    mod.review(svc, NOW)
    end = NOW.replace(day=26, hour=10)
    with svc._connect() as db:
        before = db.iterdump()
        frozen = '\n'.join(before)
    health = svc.daily_observation_status(end)
    assert health['dueCount'] == health['overdueCount'] == health['unreviewedDueCount'] == 1
    assert health['missingDataCount'] == 0 and health['pendingCount'] == 2
    assert health['lastSuccessAt'] == NOW.isoformat()
    assert svc.daily_observation_reviews(as_of=end)['health'] == health
    with svc._connect() as db:
        assert '\n'.join(db.iterdump()) == frozen


def test_missing_data_retries_after_cooldown_and_remains_distinct_from_worker_failure(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    end = NOW.replace(day=23, hour=16)
    first = svc.review_daily_observations(end)
    assert first['status'] == 'completed'
    health = svc.daily_observation_status(end)
    assert health['dueCount'] == health['overdueCount'] == health['missingDataCount'] == 1
    assert health['lastError'] is None and health['lastSuccessAt'] == end.isoformat()
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    assert svc.review_daily_observations(end + timedelta(seconds=30))['checked'] == 0
    result = svc.review_daily_observations(end + timedelta(minutes=5))
    assert result['updated'] == 1 and result['statusCounts']['observed'] == 1


def test_one_bad_close_does_not_block_other_stocks_and_retries_after_repair(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate(), candidate('600002')), '1030', NOW, True)
    end = NOW.replace(day=23, hour=16)
    fixture_close(tmp_path, '600002', DAYS[1], 11.)
    original = mod.read_verified_close
    def bad_close(data_dir, code, day, as_of):
        if code == '600001':
            raise ValueError('broken archive')
        return original(data_dir, code, day, as_of)
    monkeypatch.setattr(mod, 'read_verified_close', bad_close)
    result = mod.review(svc, end)
    assert result['status'] == 'partial_error' and result['errors'] == 1
    assert result['statusCounts']['observed'] == 1
    health = svc.daily_observation_status(end)
    assert health['lastError'] == 'ValueError' and health['lastSuccessAt'] is None
    monkeypatch.setattr(mod, 'read_verified_close', original)
    fixture_close(tmp_path, '600001', DAYS[1], 12.)
    assert mod.review(svc, end)['statusCounts']['observed'] == 2


def test_daily_results_commit_before_exception_in_long_execution_review(tmp_path, monkeypatch):
    from src.services.yao_scout import next_day_watch
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    end = NOW.replace(day=23, hour=16)
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    monkeypatch.setattr(next_day_watch, 'review_next_day_signals', lambda *_: (_ for _ in ()).throw(RuntimeError('long review failed')))
    with pytest.raises(RuntimeError, match='long review failed'):
        svc.review_due(end)
    health = svc.daily_observation_status(end)
    assert health['observedCount'] == 1 and health['lastSuccessAt'] == end.isoformat()


def test_worker_lease_prevents_overlap_and_expired_lease_is_recoverable(tmp_path):
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    with svc._connect() as db:
        db.execute('INSERT INTO daily_observation_worker_state(id,lease_owner,lease_until) VALUES(1,?,?)',
                   ('stopped-process', (datetime.now(mod.TZ) + timedelta(minutes=1)).isoformat()))
    assert svc.review_daily_observations(NOW)['status'] == 'busy'
    with svc._connect() as db:
        db.execute('UPDATE daily_observation_worker_state SET lease_until=?',
                   ((datetime.now(mod.TZ) - timedelta(seconds=1)).isoformat(),))
    assert svc.review_daily_observations(NOW)['updated'] == 3


def test_budget_exhaustion_preserves_queue_for_next_pass(tmp_path, monkeypatch):
    from src.services.yao_scout import daily_observation_queue as queue
    svc = service(tmp_path)
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    ticks = iter([0., 2., 2.])
    monkeypatch.setattr(queue, 'monotonic', lambda: next(ticks))
    result = mod.review(svc, NOW, budget_seconds=1.)
    assert result['checked'] == 0 and result['budgetExhausted']
    with svc._connect() as db:
        assert db.execute('SELECT SUM(attempts) FROM daily_observation_review_queue').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_observation_outcomes').fetchone()[0] == 0


def test_upgrade_recovers_matured_not_matured_labels_before_new_future_planning(tmp_path):
    clock = [NOW]
    svc = service(tmp_path, lambda: clock[0])
    svc.persist_scan(scan(candidate()), '1030', NOW, True)
    mod.review(svc, NOW)
    # Represents the old result table before durable queue support existed.
    with svc._connect() as db:
        db.execute('DELETE FROM daily_observation_review_queue')
    clock[0] = NOW.replace(day=23)
    svc.persist_scan(scan(candidate('600002', at=clock[0]), run_id='next-day'), '1030', clock[0], True)
    fixture_close(tmp_path, '600001', DAYS[1], 11.)
    end = NOW.replace(day=26, hour=10)
    result = mod.review(svc, end, max_labels=1)
    assert result['statusCounts']['observed'] == 1
    with svc._connect() as db:
        observed = db.execute("SELECT s.code FROM daily_observation_signals s JOIN daily_observation_outcomes o USING(observation_id) WHERE o.status='observed'").fetchone()[0]
    assert observed == '600001'
