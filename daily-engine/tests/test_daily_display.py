"""Daily evidence can be displayed without minutes and survives history reload."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from src.services.stock_king_display import LocalPicksDisplayStore, displayable_run, display_counts
from src.services.yao_scout.daily_primary import TASK_ID, VERSION


def scan(*, count=1, minute=30):
    stamp = datetime.fromisoformat('2026-10-16T10:00:00+08:00') + timedelta(minutes=minute)
    candidates = [{'code': '600001', 'name': '测试股份', 'stateLabel': '日线条件推荐'}] if count else []
    return {'run_id': f'daily-{minute}', 'mode': 'king_live', 'scanSlot': 'live',
        'generatedAt': stamp.isoformat(), 'status': 'completed_observations',
        'primarySelectionMode': 'daily_conditional', 'primaryObjective': TASK_ID,
        'candidates': candidates, 'dailyCandidates': deepcopy(candidates),
        'profileCandidates': {'regular': deepcopy(candidates)},
        'nextDayWatchlist': [{'code': '600002'}],
        'dailyAssessment': {'version': VERSION, 'taskId': TASK_ID,
            'status': 'completed' if count else 'no_daily_candidates',
            'asOf': stamp.isoformat(), 'baselineAsOf': '2026-10-15',
            'minuteDataRequired': False, 'dailyUsableCount': 4, 'quoteUsableCount': 3,
            'eventCoveragePassedCount': count, 'count': count,
            'marketBreadth': {'available': False, 'passed': None}},
        'dataQuality': {'snapshot_count': 3000, 'quote_coverage': {'requested': 4, 'fresh': 3},
                        'minute_coverage': {'requested': 4, 'usable': 0, 'required': False}}}


@pytest.mark.parametrize('count', [0, 1])
def test_daily_without_any_minutes_survives_display_restart(tmp_path, monkeypatch, count):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    result = scan(count=count)
    result['nextDayWatchlist'] = []  # No research-list exception to the minute gate.
    assert displayable_run(result)
    saved = LocalPicksDisplayStore(None).save(result)
    assert saved['dailyCandidateCount'] == saved['tradeCandidateCount'] == count
    assert saved['savedRecommendationCount'] == count
    before = (tmp_path/'local-picks-display-v1.json').read_bytes()
    assert LocalPicksDisplayStore(None).read() == saved
    assert (tmp_path/'local-picks-display-v1.json').read_bytes() == before


@pytest.mark.parametrize('change', [
    {'dailyUsableCount': 0}, {'quoteUsableCount': 0}, {'dailyUsableCount': True},
    {'status': 'outside_continuous_trading_window'}, {'version': 'other'},
    {'taskId': 'minute_model'}, {'minuteDataRequired': True},
    {'baselineAsOf': '2026-10-14'}, {'baselineAsOf': '2026-10-16'},
    {'asOf': '2026-10-15T10:30:00+08:00'}, {'asOf': '2026-10-16T09:30:00+08:00'},
    {'asOf': '2026-10-16T10:31:00+08:00'}, {'asOf': '2026-10-16T10:30:00'},
])
def test_missing_or_stale_daily_evidence_preserves_last_success(tmp_path, monkeypatch, change):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    saved = store.save(scan(minute=20))
    result = scan()
    result['dailyAssessment'].update(change)
    assert not displayable_run(result)
    with pytest.raises(ValueError, match='日线.*已保留上次结果'):
        store.save(result)
    assert LocalPicksDisplayStore(None).read() == saved


@pytest.mark.parametrize('change', [
    {'primaryObjective': 'entry_today_exit_next_session'}, {'dailyAssessment': None},
    {'dataQuality': {'stale': True}}, {'dataQuality': {'snapshot_count': 0}},
    {'dataQuality': {'quote_coverage': {'requested': 4, 'fresh': 0}}},
])
def test_daily_branch_label_cannot_hide_invalid_sources(change):
    result = scan()
    result.update(change)
    assert not displayable_run(result)


def test_daily_assessment_cannot_cross_lunch_or_close_window():
    result = scan()
    result['generatedAt'] = '2026-10-16T13:00:00+08:00'
    result['dailyAssessment']['asOf'] = '2026-10-16T11:30:00+08:00'
    assert not displayable_run(result)
    result['generatedAt'] = '2026-10-16T14:57:00+08:00'
    result['dailyAssessment']['asOf'] = '2026-10-16T14:56:59+08:00'
    assert not displayable_run(result)


def test_daily_counts_never_include_other_research_queues(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    saved = LocalPicksDisplayStore(None).save(scan())
    assert saved['dailyCandidateCount'] == saved['savedRecommendationCount'] == 1
    assert saved['nextDayCandidateCount'] == 1


@pytest.mark.parametrize('version', ['king-daily-primary-v2', 'king-daily-primary-v3', 'king-daily-primary-v4'])
def test_saved_display_survives_upgrade_without_rewriting_version(tmp_path, monkeypatch, version):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    saved = store.save(scan())
    saved['adaptive']['dailyAssessment']['version'] = version
    store.path.write_text(json.dumps(saved), encoding='utf-8')
    before = store.path.read_bytes()
    assert LocalPicksDisplayStore(None).read() == saved
    assert store.path.read_bytes() == before
    assert not displayable_run(saved['adaptive'])
    with pytest.raises(ValueError):
        store.save(saved['adaptive'])  # A fresh publication must use the current version.


def test_unknown_daily_version_is_rejected_even_on_historical_reads(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    saved = store.save(scan())
    saved['adaptive']['dailyAssessment']['version'] = 'king-daily-primary-future'
    store.path.write_text(json.dumps(saved), encoding='utf-8')
    assert store.read() == {}
    assert not displayable_run(saved['adaptive'], historical=True)
    with pytest.raises(ValueError):
        store.save(saved['adaptive'], source='migration')


@pytest.mark.parametrize('version', [VERSION, 'king-daily-primary-v2', 'king-daily-primary-v3', 'king-daily-primary-v4'])
def test_sql_history_header_restores_daily_branch_without_loading_rejected_payloads(tmp_path, monkeypatch, version):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage import DatabaseManager, YaoRun

    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    engine = create_engine(f"sqlite:///{(tmp_path/'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    valid = scan()
    valid['dailyAssessment']['version'] = version
    missing = scan(minute=31)
    missing['dailyAssessment']['dailyUsableCount'] = 0
    stale = scan(minute=32)
    stale['dailyAssessment']['baselineAsOf'] = '2026-10-14'
    unknown = scan(minute=33)
    unknown['dailyAssessment']['version'] = 'king-daily-primary-future'
    rows = [{'run_id': row['run_id'], 'mode': 'king_live', 'status': row['status'],
             'as_of_at': datetime.fromisoformat(row['generatedAt']).replace(tzinfo=None),
             'cutoff_at': datetime.fromisoformat(row['generatedAt']).replace(tzinfo=None),
             'model_version': 'fixture', 'result_json': json.dumps(row)}
            for row in (valid, missing, stale, unknown)]
    try:
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), rows)
        headers = db.list_yao_display_runs(modes=['king_live'], statuses=['completed_observations'])
        header = next(row['result'] for row in headers if row['run_id'] == valid['run_id'])
        assert header['primarySelectionMode'] == 'daily_conditional'
        assert header['primaryObjective'] == TASK_ID
        assert header['dailyAssessment'] == valid['dailyAssessment']
        assert displayable_run(header, historical=True)
        assert displayable_run(header) is (version == VERSION)
        reads, get_run = [], db.get_yao_run
        def fetch(run_id):
            reads.append(run_id)
            return get_run(run_id)
        db.get_yao_run = fetch
        saved = LocalPicksDisplayStore(db).read()
        assert saved['adaptive'] == valid and saved['dailyCandidateCount'] == 1
        assert reads == [valid['run_id']]
        assert LocalPicksDisplayStore(db).read() == saved
        assert reads == [valid['run_id']]
        assert LocalPicksDisplayStore(None).read() == saved
    finally:
        engine.dispose()


@pytest.mark.parametrize('formal_count', [0, 1])
@pytest.mark.parametrize('observation_count', [0, 1])
@pytest.mark.parametrize('mode', ['daily_observation', 'mixed_intraday_and_daily_observation'])
def test_new_observation_snapshot_replaces_old_formal_list_without_promoting_research(
        tmp_path, monkeypatch, formal_count, observation_count, mode):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    store.save(scan(minute=20))
    run = scan(count=observation_count)
    run['primarySelectionMode'] = mode
    run['dailyAssessment']['status'] = 'completed_observations' if observation_count else 'no_daily_candidates'
    run['primaryObjective'] = 'morning_signal_to_same_session_close'
    run['candidates'] = [{'code': '600003', 'primaryAdmission': {'eligible': True}}] if formal_count else []
    run['profileCandidates'] = {'regular': deepcopy(run['candidates'])}
    for pick in run['dailyCandidates']:
        pick.update(status='daily_observation', observationOnly=True, entryEligible=False, stateLabel='日线观察')
    run['dailyProfileCandidates'] = {'regular': deepcopy(run['dailyCandidates'])}
    run['dailyObservationResearch'] = {'candidates': deepcopy(run['dailyCandidates'])}
    assert displayable_run(run)
    saved = store.save(run)
    assert saved['tradeCandidateCount'] == saved['savedRecommendationCount'] == formal_count
    assert saved['dailyObservationCount'] == saved['dailyCandidateCount'] == observation_count
    assert saved['adaptive']['candidates'] == run['candidates']
    assert saved['adaptive']['dailyCandidates'] == run['dailyCandidates']
    assert 'dailyObservationResearch' not in saved['adaptive']
    assert LocalPicksDisplayStore(None).read() == saved
    assert json.loads(store.path.read_text(encoding='utf-8'))['adaptive']['dailyObservationResearch'] == run['dailyObservationResearch']


def test_observation_flag_cannot_inflate_formal_count_even_if_candidate_list_is_malformed():
    row = {'code': '600001', 'status': 'daily_observation', 'observationOnly': True}
    counts = display_counts({'primarySelectionMode': 'daily_observation', 'candidates': [row],
                             'dailyCandidates': [row], 'nextDayWatchlist': [{'code': '600002'}]})
    assert counts['tradeCandidateCount'] == counts['savedRecommendationCount'] == 0
    assert counts['dailyObservationCount'] == 1


def repaired_directory_observation(*, count=1, mode='mixed_intraday_and_daily_observation', minute=30):
    """Old directory activity is separate from this scan's verified daily evidence."""
    result = scan(count=count, minute=minute)
    result.update(primarySelectionMode=mode, candidates=[],
                  profileCandidates={key: [] for key in ('conservative', 'regular', 'aggressive')})
    result['dailyAssessment']['status'] = 'completed_observations' if count else 'no_daily_candidates'
    for pick in result['dailyCandidates']:
        pick.update(status='daily_observation', observationOnly=True, entryEligible=False)
    result['dataQuality'].update(
        stale=True, directory_complete=True, selection_fields_stale=True,
        selectionInputFreshness={
            'version': 'fresh-selection-inputs-v1', 'rebuiltAfterWholeMarketQuotes': True,
            'freshRankedCount': 3, 'directoryOnlyCount': 1,
            'sourceTimePolicy': 'each_input_own_provider_clock_max30s_no_retimestamping',
        })
    return result


@pytest.mark.parametrize('count', [0, 1])
@pytest.mark.parametrize('mode', ['daily_observation', 'mixed_intraday_and_daily_observation'])
def test_verified_daily_observation_replaces_old_display_despite_stale_directory_fields(
        tmp_path, monkeypatch, count, mode):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    store.save(scan(minute=20))
    result = repaired_directory_observation(count=count, mode=mode)

    assert displayable_run(result)
    saved = store.save(result)
    assert saved['adaptive'] == result
    assert saved['tradeCandidateCount'] == saved['savedRecommendationCount'] == 0
    assert saved['dailyObservationCount'] == count
    # Saving a new observation must not relabel the original old fields as live.
    assert saved['adaptive']['dataQuality']['stale'] is True
    before = store.path.read_bytes()
    assert LocalPicksDisplayStore(None).read() == saved
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('section, changes', [
    ('dataQuality', {'directory_complete': False}),
    ('dataQuality', {'directory_complete': None}),
    ('dataQuality', {'directory_complete': 1}),
    ('dataQuality', {'selection_fields_stale': False}),
    ('dataQuality', {'selection_fields_stale': None}),
    ('dataQuality', {'selection_fields_stale': 1}),
    ('dataQuality', {'snapshot_count': 0}),
    ('dataQuality', {'snapshot_count': True}),
    ('selectionInputFreshness', {'version': 'future'}),
    ('selectionInputFreshness', {'rebuiltAfterWholeMarketQuotes': False}),
    ('selectionInputFreshness', {'rebuiltAfterWholeMarketQuotes': 1}),
    ('selectionInputFreshness', {'freshRankedCount': 0}),
    ('selectionInputFreshness', {'freshRankedCount': True}),
    ('selectionInputFreshness', {'directoryOnlyCount': -1}),
    ('selectionInputFreshness', {'directoryOnlyCount': True}),
    ('quote_coverage', {'requested': 0}),
    ('quote_coverage', {'requested': True}),
    ('quote_coverage', {'fresh': 0}),
    ('quote_coverage', {'fresh': True}),
    ('quote_coverage', {'fresh': 5}),
    ('dailyAssessment', {'dailyUsableCount': 0}),
    ('dailyAssessment', {'quoteUsableCount': 0}),
    ('dailyAssessment', {'baselineAsOf': '2026-10-14'}),
    ('dailyAssessment', {'asOf': '2026-10-16T09:30:00+08:00'}),
    ('dailyAssessment', {'status': 'outside_continuous_trading_window'}),
    ('dailyAssessment', {'version': 'future'}),
])
def test_directory_exception_requires_current_daily_and_quote_evidence(
        tmp_path, monkeypatch, section, changes):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    previous = store.save(scan(minute=20))
    before = store.path.read_bytes()
    result = repaired_directory_observation()
    target = result.get(section) or result['dataQuality'][section]
    target.update(changes)

    assert not displayable_run(result)
    with pytest.raises(ValueError, match='已保留上次结果'):
        store.save(result)
    assert store.path.read_bytes() == before
    assert store.read() == previous


@pytest.mark.parametrize('location', ['candidates', 'conservative', 'regular', 'aggressive'])
def test_stale_directory_daily_evidence_cannot_admit_formal_candidates(location):
    result = repaired_directory_observation()
    formal = {'code': '600003', 'primaryAdmission': {'eligible': False}}
    if location == 'candidates':
        result['candidates'] = [formal]
    else:
        result['profileCandidates'][location] = [formal]
    assert not displayable_run(result)


def test_stale_directory_exception_does_not_restore_legacy_daily_recommendations():
    result = repaired_directory_observation(mode='daily_conditional')
    result['dailyAssessment']['status'] = 'completed'
    assert not displayable_run(result)


def test_history_headers_restore_repaired_observation_and_reject_formal_bypass(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage import DatabaseManager, YaoRun

    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    engine = create_engine(f"sqlite:///{(tmp_path/'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    valid = repaired_directory_observation()
    invalid_main = repaired_directory_observation(minute=31)
    invalid_main['candidates'] = [{'code': '600003', 'primaryAdmission': {'eligible': False}}]
    invalid_profile = repaired_directory_observation(minute=32)
    invalid_profile['profileCandidates']['aggressive'] = [
        {'code': '600003', 'primaryAdmission': {'eligible': False}}]
    rows = [{'run_id': row['run_id'], 'mode': 'king_live', 'status': row['status'],
             'as_of_at': datetime.fromisoformat(row['generatedAt']).replace(tzinfo=None),
             'cutoff_at': datetime.fromisoformat(row['generatedAt']).replace(tzinfo=None),
             'model_version': 'fixture', 'result_json': json.dumps(row)}
            for row in (valid, invalid_main, invalid_profile)]
    try:
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), rows)
        headers = db.list_yao_display_runs(modes=['king_live'], statuses=['completed_observations'])
        for header in headers:
            assert displayable_run(header['result'], historical=True) is (header['run_id'] == valid['run_id'])
        valid_header = next(row['result'] for row in headers if row['run_id'] == valid['run_id'])
        assert valid_header['dataQuality'] == valid['dataQuality']
        reads, get_run = [], db.get_yao_run
        def fetch(run_id):
            reads.append(run_id)
            return get_run(run_id)
        db.get_yao_run = fetch
        saved = LocalPicksDisplayStore(db).read()
        assert saved['adaptive'] == valid
        assert reads == [valid['run_id']]
        assert LocalPicksDisplayStore(None).read() == saved
    finally:
        engine.dispose()
