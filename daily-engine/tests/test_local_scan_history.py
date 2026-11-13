"""Offline regression for bounded local history; no providers or browsers."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src import storage
from src.storage import DatabaseManager, YaoRun
from src.services.yao_scout.local_opportunities import LocalOpportunityService

NOW = datetime(2026, 11, 13, 13, 0, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.fixture
def database():
    engine = create_engine('sqlite://')
    YaoRun.__table__.create(engine)
    holder = SimpleNamespace(get_session=lambda: Session(engine))
    holder.list_yao_scan_history = lambda **kw: DatabaseManager.list_yao_scan_history(holder, **kw)
    yield holder, engine
    engine.dispose()


def save(engine, identity, at, result=None, *, status='completed', mode='king_live'):
    stamp = at.astimezone(timezone.utc).replace(tzinfo=None)
    payload = {'as_of': at.isoformat(), 'generatedAt': at.isoformat(), **(result or {})}
    with Session(engine) as session:
        session.add(YaoRun(run_id=identity, mode=mode, status=status, as_of_at=stamp,
                          cutoff_at=stamp, model_version='fixture', result_json=json.dumps(payload)))
        session.commit()


def read(holder, **kw):
    return holder.list_yao_scan_history(now=NOW, previous_day=(NOW-timedelta(days=1)).date(), **kw)


def test_projection_keeps_seed_and_first_detection_without_raw_research(database, monkeypatch):
    holder, engine = database
    at = NOW-timedelta(minutes=20)
    first = (at-timedelta(minutes=40)).isoformat()
    opportunity = {'version': 'opportunity-theme-20261113-v1', 'grade': 'F-A',
                   'firstDetectionAt': first, 'firstDetectionPrice': 10.25,
                   'detectedAt': at.isoformat()}
    seed = {'code': '600001', 'name': '测试主板', 'price': 10.5, 'amount': 123,
            'volume_ratio': 2, 'turnover_rate': 3, 'open': 10,
            'referencePrice': 10.5, 'decision_at': at.isoformat()}
    raw = 'RAW_MINUTES_MUST_NOT_ENTER_PYTHON' * 100000
    save(engine, 'current', at, {'candidates': [{**seed, 'rawMinutePackets': raw}],
        'delivery': {'received_at': None}, 'rawMarket': raw,
        'opportunityChannels': {'version': 'current', 'asOf': at.isoformat(),
            'researchCandidates': [{'code': '600001', 'name': '测试主板',
                                    'opportunity': opportunity, 'rawMinutePackets': raw}]}})
    yesterday = (NOW-timedelta(days=1)).replace(hour=14, minute=55)
    save(engine, 'overnight', yesterday, {'nextDayWatchlist': [seed]})
    original_loads = storage.json.loads
    sizes = []
    def small_only(value, *args, **kwargs):
        sizes.append(len(value))
        assert 'RAW_MINUTES_MUST_NOT_ENTER_PYTHON' not in value
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(storage.json, 'loads', small_only)
    packet = read(holder)
    current, overnight = packet['rows']
    assert current['result']['opportunityChannels']['researchCandidates'][0]['opportunity'] == opportunity
    assert current['result']['generatedAt'] == at.isoformat()
    assert current['result']['as_of'] == at.isoformat()
    assert current['result']['candidates'][0] == seed
    assert overnight['result']['generatedAt'] == yesterday.isoformat()
    assert overnight['result']['nextDayWatchlist'][0] == seed
    assert max(sizes) < 10000
    assert len(json.dumps(packet)) < 15000


def test_previous_session_seed_survives_more_than_100_current_refreshes(database):
    holder, engine = database
    yesterday = (NOW-timedelta(days=1)).replace(hour=14, minute=55)
    save(engine, 'last-valid-seed', yesterday, {'nextDayContinuationWatchlist': [{'code': '600001', 'name': '种子'}]})
    for index in range(105):
        save(engine, f'today-{index}', NOW-timedelta(minutes=120-index))
    save(engine, 'failed-refresh', NOW-timedelta(seconds=4), status='failed')
    save(engine, 'missed-refresh', NOW-timedelta(seconds=3), status='missed_slot')
    save(engine, 'review', NOW-timedelta(seconds=2), mode='king_review')
    save(engine, 'future', NOW+timedelta(seconds=1))
    packet = read(holder)
    assert len(packet['headers']) == 100
    assert [row['run_id'] for row in packet['rows']] == ['today-104', 'today-103', 'last-valid-seed']
    assert packet['rows'][-1]['result']['nextDayContinuationWatchlist'][0]['name'] == '种子'
    assert all(row['run_id'] != 'future' for row in packet['headers'])


def test_empty_channel_semantics_and_invalid_grade_are_preserved(database):
    holder, engine = database
    save(engine, 'current', NOW-timedelta(minutes=1), {'opportunityChannels': {}})
    invalid = {'version': 'legacy', 'grade': 'C', 'firstDetectionAt': 'original',
               'firstDetectionPrice': 9, 'detectedAt': 'original-later'}
    save(engine, 'prior', NOW-timedelta(minutes=2), {'opportunityChannels': {
        'observations': [{'code': '600001', 'opportunity': invalid}]}})
    rows = read(holder)['rows']
    assert 'opportunityChannels' not in rows[0]['result']
    assert rows[1]['result']['opportunityChannels']['observations'][0]['opportunity'] == invalid


def test_local_service_uses_projection_api_and_only_selects(database, monkeypatch):
    holder, engine = database
    save(engine, 'current', NOW-timedelta(minutes=1))
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, statement, parameters, context, many:
                 statements.append(statement))
    holder.list_yao_runs = lambda **kw: pytest.fail('legacy full-history query called')
    service = object.__new__(LocalOpportunityService)
    service.db, service.clock = holder, lambda: NOW
    monkeypatch.setattr('src.services.yao_scout.premarket.previous_session', lambda now: (NOW-timedelta(days=1)).date())
    rows, coverage = service._history()
    assert len(rows) == 1 and coverage['maximumProjectedRuns'] == 4
    assert all(sql.lstrip().upper().startswith(('SELECT', 'WITH')) for sql in statements)
    assert all('result_json' not in sql or 'json_object' in sql for sql in statements)


def test_legacy_adapter_never_loads_100_full_results(monkeypatch):
    calls = []
    service = object.__new__(LocalOpportunityService)
    service.db = SimpleNamespace(list_yao_runs=lambda **kw: calls.append(kw) or [])
    service.clock = lambda: NOW
    monkeypatch.setattr('src.services.yao_scout.premarket.previous_session', lambda now: (NOW-timedelta(days=1)).date())
    _, coverage = service._history()
    assert calls == [{'limit': 100, 'include_result': False}, {'limit': 4, 'include_result': True}]
    assert coverage['gaps'] == ['legacy_history_adapter_limited_to_four_results']


def test_budget_gap_is_explicit_and_never_falls_back_to_full_payload(database, monkeypatch):
    holder, engine = database
    save(engine, 'current', NOW-timedelta(minutes=1))
    ticks = iter([0, 1, 2])
    monkeypatch.setattr(storage.time, 'monotonic', lambda: next(ticks))
    packet = read(holder, budget_seconds=.1)
    assert packet['rows'] == []
    assert packet['gaps'] == ['compact_history_budget_exhausted']


def test_older_sqlite_text_projection_retains_the_same_seed(database, monkeypatch):
    holder, engine = database
    save(engine, 'current', NOW-timedelta(minutes=1), {'candidates': [{'code': '600001', 'name': '保留'}]})
    execute = Session.execute
    def old_version(session, statement, *args, **kwargs):
        if str(statement) == 'SELECT sqlite_version()':
            return SimpleNamespace(scalar_one=lambda: '3.40.0')
        return execute(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, 'execute', old_version)
    row = read(holder)['rows'][0]['result']['candidates'][0]
    assert row['code'] == '600001' and row['name'] == '保留'
