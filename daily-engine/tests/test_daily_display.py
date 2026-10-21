"""Daily evidence can be displayed without minutes and survives history reload."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from src.services.stock_king_display import LocalPicksDisplayStore, displayable_run
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


def test_v2_saved_display_survives_upgrade_without_rewriting_version(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    saved = store.save(scan())
    saved['adaptive']['dailyAssessment']['version'] = 'king-daily-primary-v2'
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


@pytest.mark.parametrize('version', [VERSION, 'king-daily-primary-v2'])
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
