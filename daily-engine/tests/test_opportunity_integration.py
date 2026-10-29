"""E/M scans, immutable cohorts and durable display work without a T+1 model."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.services.stock_king_display import display_counts, displayable_run, LocalPicksDisplayStore
from src.services.yao_scout.discovery_snapshot import DiscoverySnapshotStore
from src.services.yao_scout.opportunity_channels import build_opportunity_channels, VERSION
from tests.test_opportunity_channels import NOW, candidate, minute
from tests.test_local_quote_integration import make_service
from tests.test_daily_observation_outcomes import service as learning_service, fixture_close, DAYS
from src.services.yao_scout import daily_observation_outcomes as outcomes


def cold_start(monkeypatch, tmp_path, now=NOW):
    service, tick, _, saved = make_service(monkeypatch, now, tmp_path)
    service.selection_mode = 'auto'
    def quote(code):
        value = candidate(code)['quote']
        value.update(provider_timestamp=tick[0].isoformat(), source_time=tick[0].isoformat(),
                     active_flow_as_of=tick[0].isoformat())
        value['order_book']['source_time'] = tick[0].isoformat()
        return value
    service.quote_fetcher = quote
    service.minute_fetcher = lambda *args: {**minute(asOf=(tick[0].replace(second=0)-timedelta(minutes=1)).isoformat()),
                                            'dataLineage': {'status': 'verified'}}
    service.sector_fetcher = lambda *args: {}
    service.fund_fetcher = lambda *args: {'metrics': {}}
    service.context_provider = SimpleNamespace(collect=lambda now: {'tables': [], 'status': 'unavailable'})
    return service, tick, saved


def test_default_cold_start_fetches_current_minutes_and_publishes_early_opportunity(monkeypatch, tmp_path):
    service, _, saved = cold_start(monkeypatch, tmp_path)
    result = service.run('live')
    assert result.get('opportunityChannels'), result.get('message')
    picked = result['opportunityChannels']['recommendations']
    assert len(picked) == 1, result['opportunityChannels']['diagnostics']
    assert picked[0]['opportunity']['grade'] == 'E-A'
    assert result['dataQuality']['stockRouting']['earlyMinuteResearchCount'] == 1
    assert result['dataQuality']['minute_coverage']['requested'] == 1
    assert result['candidates'] == []
    assert result['learningLedger']['selectedCount'] == 0
    assert result['learningLedger']['opportunityRecommendationCount'] == 1
    assert saved[-1]['opportunityChannels']['recommendations'][0]['code'] == '600001'
    assert displayable_run(result)
    assert display_counts(result)['opportunityRecommendationCount'] == 1
    assert display_counts(result)['tradeCandidateCount'] == 0


def test_new_opportunities_survive_history_reconciliation_without_daily_or_model_pass(monkeypatch, tmp_path):
    service, _, _ = cold_start(monkeypatch, tmp_path/'engine')
    result = service.run('live')
    assert result['opportunityChannels']['recommendations']
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    # Lightweight DB headers contain no full research/quote payload.
    header = {'run_id': result['run_id'], 'mode': 'king_live', 'opportunity_recommendation_count': 1,
              'result': {key: result[key] for key in ('generatedAt', 'status', 'scanSlot')}}
    db = SimpleNamespace(list_yao_display_runs=lambda **kwargs: [header],
                         get_yao_run=lambda run_id: {'result': result})
    store = LocalPicksDisplayStore(db)
    response = store.read()
    assert response['opportunityRecommendationCount'] == 1
    assert response['adaptive']['opportunityChannels']['recommendations']
    frozen = store.path.read_bytes()
    again = store.read()
    assert again['opportunityRecommendationCount'] == 1
    assert store.path.read_bytes() == frozen
    assert 'researchCandidates' not in again['adaptive']['opportunityChannels']


def test_expired_publication_cannot_gain_display_from_opportunity_label(monkeypatch, tmp_path):
    service, _, _ = cold_start(monkeypatch, tmp_path)
    result = service.run('live')
    result['generatedAt'] = (NOW+timedelta(minutes=1)).isoformat()
    result['dataQuality']['quote_coverage']['fresh'] = 0
    assert not displayable_run(result)


def test_discovery_cache_keeps_source_times_and_never_reuses_future_or_prior_day(tmp_path):
    store = DiscoverySnapshotStore(tmp_path)
    frame = pd.DataFrame([{'code': '600001', 'market_quote': candidate()['quote']}])
    frame.attrs['source'] = 'test'
    assert store.save(frame, NOW)
    assert store.read(NOW-timedelta(seconds=1)) is None
    assert store.read(NOW+timedelta(days=1)) is None
    read = store.read(NOW+timedelta(minutes=1))
    assert read.attrs['captured_at'] == NOW.isoformat()
    assert read.iloc[0]['market_quote']['source_time'] == NOW.isoformat()
    before = store.path.read_bytes()
    store.read(NOW+timedelta(minutes=2))
    assert store.path.read_bytes() == before


def test_opportunity_reference_starts_at_recommendation_not_earlier_control(tmp_path):
    tick = [NOW]
    svc = learning_service(tmp_path, clock=lambda: tick[0])
    row = candidate()
    row.update(decision_at=NOW.isoformat(), observationOnly=True, formalEligible=False)
    row['opportunity'] = {'channel': 'E', 'grade': 'B', 'firstDetectionAt': NOW.isoformat()}
    packet = {'version': VERSION, 'recommendations': [], 'researchCandidates': [row]}
    scan = {'run_id': 'first-control', 'status': 'completed_observations', 'candidates': [],
            'opportunityChannels': packet}
    first = svc.persist_scan(scan, 'live', NOW, False)
    later = NOW+timedelta(minutes=2)
    tick[0] = later
    promoted = deepcopy(row)
    promoted.update(referencePrice=10.5, decision_at=later.isoformat(), ruleRecommendation=True)
    promoted['quote'].update(price=10.5, source_time=later.isoformat())
    promoted['opportunity']['grade'] = 'E-A'
    second = {**scan, 'run_id': 'promoted', 'opportunityChannels': {
        'version': VERSION, 'recommendations': [promoted], 'researchCandidates': [promoted]}}
    svc.persist_scan(second, 'live', later, False)
    promoted['referencePrice'] = 9.
    promoted['quote'].update(price=9., source_time=(later+timedelta(minutes=2)).isoformat())
    promoted['decision_at'] = (later+timedelta(minutes=2)).isoformat()
    tick[0] = later+timedelta(minutes=2)
    svc.persist_scan({**second, 'run_id': 'third'}, 'live', later+timedelta(minutes=2), False)
    with svc._connect() as db:
        rows = db.execute('SELECT task_id,reference_price,first_category,snapshot_json FROM daily_observation_signals ORDER BY task_id').fetchall()
        assert len(rows) == 2
        by_task = {item[0]: item for item in rows}
        assert by_task['opportunity_em_control'][1] == 10.
        assert by_task['opportunity_em_recommendation'][1] == 10.5
        assert by_task['opportunity_em_recommendation'][2] == 'opportunity_recommendation'
        assert json.loads(by_task['opportunity_em_recommendation'][3])['opportunity']['grade'] == 'E-A'
        assert db.execute("SELECT reference_verified FROM daily_observation_signals WHERE task_id='opportunity_em_recommendation'").fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM forward_predictions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM signal_outcomes').fetchone()[0] == 0
    assert first['learningLedger']['selectedCount'] == 0


def test_opportunity_future_outcomes_are_gross_reference_observations_only(tmp_path):
    # Use the existing exact calendar/verified-close fixture for settlement.
    from tests.test_daily_observation_outcomes import NOW as START
    svc = learning_service(tmp_path)
    row = candidate()
    row.update(referencePrice=10., decision_at=START.isoformat(), observationOnly=True)
    row['quote'].update(source_time=START.isoformat())
    row['opportunity'] = {'channel': 'M', 'grade': 'M-A'}
    scan = {'run_id': 'em', 'status': 'completed_observations', 'candidates': [],
            'opportunityChannels': {'version': VERSION, 'recommendations': [row], 'researchCandidates': [row]}}
    svc.persist_scan(scan, 'live', START, False)
    reviewed = START.replace(hour=17)+timedelta(days=7)
    for index in (1, 3, 5):
        fixture_close(tmp_path, '600001', DAYS[index], 11.)
    outcomes.review(svc, reviewed)
    with svc._connect() as db:
        labels = db.execute('SELECT horizon,status,gross_reference_change_pct,net_return FROM daily_observation_outcomes').fetchall()
        assert len(labels) == 3
        assert all(item[1] == 'observed' and round(item[2], 4) == 10. and item[3] is None for item in labels)


def opportunity_run(now, *, empty=False, no_offers=False, outage=False):
    row, packet = candidate(), minute()
    row['quote']['source_time'] = now.isoformat()
    row['quote']['active_flow_as_of'] = now.isoformat()
    row['quote']['order_book']['source_time'] = now.isoformat()
    packet['asOf'] = (now.replace(second=0)-timedelta(minutes=1)).isoformat()
    if empty:
        packet['v11Inputs'].update(r1_pct=-.3, r3_pct=-.8, r5_pct=-1., r15_pct=-1.5,
                                  a1_pct_per_min=-.1, a3_pct_per_min=-.16)
        packet['currentPrice'] = 9.9
    if no_offers:
        row['quote']['order_book']['asks'] = []
    if outage:
        row['quote'] = {}
        packet = {}
    return {'run_id': 'opportunity-'+now.isoformat(), 'status': 'completed_observations',
        'generatedAt': now.isoformat(), 'scanSlot': 'live', 'candidates': [], 'profileCandidates': {},
        'primarySelectionMode': 'daily_observation',
        'opportunityChannels': build_opportunity_channels([row], now, minute_packets={'600001': packet}),
        # Old directory and daily/model gates must not own this independent lane.
        'dataQuality': {'snapshot_count': 1, 'stale': True,
            'quote_coverage': {'requested': 1, 'fresh': 0 if outage else 1},
            'minute_coverage': {'requested': 1, 'usable': 0 if outage else 1}}}


@pytest.mark.parametrize('no_offers', [False, True])
def test_completed_zero_opportunities_replace_previous_recommendation(monkeypatch, tmp_path, no_offers):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    store = LocalPicksDisplayStore(None)
    first = opportunity_run(NOW)
    assert store.save(first)['opportunityRecommendationCount'] == 1
    empty = opportunity_run(NOW+timedelta(minutes=2), empty=not no_offers, no_offers=no_offers)
    assert empty['opportunityChannels']['recommendations'] == []
    assert displayable_run(empty)
    shown = store.save(empty)
    assert shown['displayRunId'] == empty['run_id']
    assert shown['opportunityRecommendationCount'] == 0
    assert shown['adaptive']['opportunityChannels']['observations']
    assert store.read()['opportunityRecommendationCount'] == 0


@pytest.mark.parametrize('corruption', ['all_source_failed', 'no_minutes', 'fake_count', 'wrong_version', 'timed_out'])
def test_empty_label_cannot_clear_saved_opportunities_without_completed_current_evidence(monkeypatch, tmp_path, corruption):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    store = LocalPicksDisplayStore(None)
    store.save(opportunity_run(NOW))
    frozen = store.path.read_bytes()
    later = NOW+timedelta(minutes=2)
    empty = opportunity_run(later, empty=True, outage=corruption == 'all_source_failed')
    packet = empty['opportunityChannels']
    if corruption == 'no_minutes':
        for row in packet['researchCandidates']:
            row['opportunity']['sourceEvidence']['minuteAsOf'] = None
            row['opportunity']['metrics'] = {}
    elif corruption == 'fake_count':
        packet['diagnostics']['researchedCount'] = 99
    elif corruption == 'wrong_version':
        packet['version'] = 'unknown-version'
    elif corruption == 'timed_out':
        empty['status'] = 'failed'
    assert not displayable_run(empty)
    with pytest.raises(ValueError):
        store.save(empty)
    assert store.path.read_bytes() == frozen
    assert store.read()['opportunityRecommendationCount'] == 1


def test_sql_headers_restore_zero_opportunity_assessment_and_verify_full_payload(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage import DatabaseManager, YaoRun

    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    engine = create_engine(f"sqlite:///{(tmp_path/'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    first = opportunity_run(NOW)
    empty = opportunity_run(NOW+timedelta(minutes=2), empty=True)
    def insert(run):
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), {'run_id': run['run_id'], 'mode': 'king_live',
                'status': run['status'], 'as_of_at': datetime.fromisoformat(run['generatedAt']).replace(tzinfo=None),
                'cutoff_at': datetime.fromisoformat(run['generatedAt']).replace(tzinfo=None),
                'model_version': 'fixture', 'result_json': json.dumps(run)})
    try:
        insert(first)
        store = LocalPicksDisplayStore(db)
        assert store.read()['opportunityRecommendationCount'] == 1
        insert(empty)
        headers = db.list_yao_display_runs(modes=['king_live'], statuses=['completed_observations'])
        header = next(row for row in headers if row['run_id'] == empty['run_id'])
        assert header['opportunity_recommendation_count'] == 0
        assert header['opportunity_assessment']['version'] == VERSION
        assert header['opportunity_assessment']['researchRowCount'] == 1
        assert 'researchCandidates' not in json.dumps(header)
        reads, original_get = [], db.get_yao_run
        def full_read(run_id):
            reads.append(run_id)
            return original_get(run_id)
        db.get_yao_run = full_read
        shown = store.read()
        assert shown['displayRunId'] == empty['run_id']
        assert shown['opportunityRecommendationCount'] == 0
        assert reads == [empty['run_id']]

        forged = opportunity_run(NOW+timedelta(minutes=4), empty=True, outage=True)
        insert(forged)  # Its well-formed lightweight marker is not full evidence.
        frozen = store.path.read_bytes()
        shown = store.read()
        assert shown['displayRunId'] == empty['run_id']
        assert shown['displaySyncError']
        assert store.path.read_bytes() == frozen
    finally:
        engine.dispose()


@pytest.mark.parametrize('slot,official,start,end,status', [
    ('0940', True, NOW.replace(second=30, minute=41), NOW.replace(second=0, minute=42), 'missed_slot'),
    ('1455', True, NOW.replace(hour=14, minute=56, second=30), NOW.replace(hour=14, minute=57, second=0), 'expired'),
    ('1455', False, NOW.replace(hour=14, minute=56, second=30), NOW.replace(hour=14, minute=57, second=0), 'expired'),
])
def test_tail_readiness_work_cannot_publish_after_original_slot_window(monkeypatch, tmp_path, slot, official, start, end, status):
    from src.services.yao_scout import primary_readiness

    service, tick, _ = cold_start(monkeypatch, tmp_path, start)
    original = primary_readiness.read_primary_readiness
    tail_calls = []
    def slow_readiness(*args, **kwargs):
        result = original(*args, **kwargs)
        tail_calls.append(tick[0])
        tick[0] = end
        return result
    monkeypatch.setattr(primary_readiness, 'read_primary_readiness', slow_readiness)
    result = service.run(slot, official=official)
    assert tail_calls, result.get('message')
    assert result['status'] == status, result.get('message')
    assert result['opportunityChannels']['recommendations'] == []
    assert result['opportunityChannels']['researchCandidates'] == []
    assert result['dataQuality']['opportunityPublication']['permitted'] is False
    assert result['candidates'] == []
    assert not displayable_run(result)


def test_manual_1458_opportunity_is_independent_of_legacy_formal_expiry(monkeypatch, tmp_path):
    service, _, _ = cold_start(monkeypatch, tmp_path, NOW.replace(hour=14, minute=58))
    result = service.run('live', official=False)
    assert result['opportunityChannels']['recommendations'], result.get('message')
    assert result['opportunityChannels']['recommendations'][0]['opportunity']['grade'] == 'E-A'
    assert result['legacySelectionStatus'] == 'expired'
    assert result['dataQuality']['opportunityPublication']['permitted'] is True
    assert result['candidates'] == []
    assert displayable_run(result)
