"""Strategy choices are frozen at submission and shared by homepage/history."""
from copy import deepcopy
from datetime import datetime
from threading import Event
from types import SimpleNamespace

import pytest

from api.v1.endpoints import stock_king as endpoint
from src.services.stock_king_display import LocalPicksDisplayStore
from src.services.yao_scout import strategy_channels as strategies
from src.services.yao_scout.daily_opportunities import DailyOpportunityService
from src.services.yao_scout.local_opportunities import LocalOpportunityService
from tests.test_local_picks_refresh import runtime, install_scan, completed_response, scan


def custom_rule(version='1'):
    return {'id': 'custom.test', 'name': '自定义测试', 'version': version,
            'baseStrategy': 'trend_space', 'conditions': [{'field': 'score', 'op': 'gte', 'value': 0}],
            'rankBy': {'field': 'score', 'direction': 'desc'}}


def selection(run, strategy_id='all', custom_rules=(), top_n=5):
    return {'version': strategies.VERSION, 'strategyId': strategy_id, 'strategyName': '测试打法',
            'strategyVersion': '1', 'sourceRunId': run['run_id'], 'sourceTime': run['generatedAt'],
            'items': [{'code': f'60000{i}', 'name': f'样本{i}', 'rank': i, 'price': 10,
                       'sourceTime': run['generatedAt'], 'reasons': ['价格与结构条件通过'],
                       'strategyMatches': [{'id': strategy_id, 'name': '测试打法', 'version': '1',
                           'rank': i, 'score': 1, 'reasons': ['通过'], 'evidence': []}]} for i in range(1, 6)],
            'channels': [{'id': strategy_id, 'name': '测试打法', 'version': '1', 'status': 'matched', 'count': 5}],
            'ruleSnapshots': [{'id': strategy_id, 'name': '测试打法', 'version': '1'}],
            'selectionPolicy': '同股合并，最多5只'}


def test_catalog_import_preview_enable_never_changes_recommendation_history(runtime):
    client, db, rows, _ = runtime
    store = LocalPicksDisplayStore(db)
    old = store.save(scan())
    rows.append({'mode': 'king_live', 'result': scan()})
    catalog = client.get('/picks/strategies').json()
    assert len(catalog['items']) >= 4
    initial_fingerprint = catalog['defaultStrategyFingerprint']
    imported = client.post('/picks/strategies/import', json=custom_rule())
    assert imported.status_code == 200 and imported.json()['strategy']['enabled'] is False
    assert client.get('/picks/strategies').json()['defaultStrategyFingerprint'] == initial_fingerprint
    assert client.post('/picks/strategies/custom.test/enable', json={'confirm': True}).status_code == 422
    preview = client.post('/picks/strategies/preview', json={'strategy_id': 'custom.test', 'run_id': scan()['run_id']})
    assert preview.status_code == 200 and preview.json()['preview'] is True
    assert preview.json()['runId'] == scan()['run_id']
    assert client.post('/picks/strategies/custom.test/enable', json={'confirm': False}).status_code == 422
    receipt = preview.json()['previewReceipt']
    assert client.post('/picks/strategies/custom.test/enable', json={'confirm': True, 'expected_version': 'wrong'}).status_code == 422
    expected = {'expected_version': receipt['ruleVersion'], 'expected_fingerprint': receipt['ruleFingerprint']}
    assert client.post('/picks/strategies/custom.test/enable', json={'confirm': True, **expected}).json()['strategy']['enabled'] is True
    assert client.get('/picks/strategies').json()['defaultStrategyFingerprint'] != initial_fingerprint
    assert client.post('/picks/strategies/custom.test/enable', json={'confirm': True, 'enabled': False, **expected}).json()['strategy']['enabled'] is False
    assert client.get('/picks/strategies').json()['defaultStrategyFingerprint'] == initial_fingerprint
    assert store.records(scope='history')['items'] == [old['adaptive']['publishedPicks']]
    assert store.read() == old
    assert client.post('/picks/strategies/import', json={**custom_rule(), 'code': 'exec()'}).status_code == 422


def test_different_strategy_cannot_reuse_active_refresh(runtime, monkeypatch):
    client, db, _, queue = runtime
    gate, started = Event(), Event()
    calls = install_scan(monkeypatch, scan(), gate=gate, started=started)
    try:
        first = client.post('/picks/refresh', json={'strategy_id': 'theme_lead'})
        assert first.status_code == 202 and started.wait(2)
        assert first.json()['strategyId'] == 'theme_lead'
        same = client.post('/picks/refresh', json={'strategy_id': 'theme_lead'})
        assert same.json()['task_id'] == first.json()['task_id']
        assert client.post('/picks/refresh', json={'strategy_id': 'trend_space'}).status_code == 409
        assert client.post('/picks/refresh', json={'strategy_id': 'custom.missing'}).status_code == 422
    finally:
        gate.set()
    done = completed_response(client, queue, first)
    assert done['strategyId'] == 'theme_lead'
    assert calls[0][1]['strategy_id'] == 'theme_lead'


def test_imported_rule_version_is_frozen_while_task_waits(runtime, monkeypatch):
    client, db, rows, queue = runtime
    rows.append({'mode': 'king_live', 'result': scan()})
    client.post('/picks/strategies/import', json=custom_rule())
    first_preview = client.post('/picks/strategies/preview', json={'strategy_id': 'custom.test', 'run_id': scan()['run_id']}).json()
    client.post('/picks/strategies/custom.test/enable', json={'confirm': True})
    gate, started = Event(), Event()
    calls = install_scan(monkeypatch, scan(), gate=gate, started=started)
    try:
        first = client.post('/picks/refresh', json={'strategy_id': 'custom.test'})
        assert started.wait(2)
        client.post('/picks/strategies/import', json=custom_rule('2'))
        client.post('/picks/strategies/preview', json={'strategy_id': 'custom.test', 'run_id': scan()['run_id']})
        assert client.post('/picks/strategies/custom.test/enable', json={'confirm': True,
            'expected_version': first_preview['previewReceipt']['ruleVersion'],
            'expected_fingerprint': first_preview['previewReceipt']['ruleFingerprint']}).status_code == 422
        client.post('/picks/strategies/custom.test/enable', json={'confirm': True})
        assert client.post('/picks/refresh', json={'strategy_id': 'custom.test'}).status_code == 409
    finally:
        gate.set()
    assert completed_response(client, queue, first)['strategyVersion'] == '1'
    assert calls[0][1]['strategy_rules'][0]['version'] == '1'


def test_official_jobs_always_capture_all_and_manual_keeps_selection(tmp_path, monkeypatch):
    service = object.__new__(LocalOpportunityService)
    service.yao = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(DailyOpportunityService, '_run', lambda self, slot, **kw: deepcopy(self._strategy_context))
    assert service._run('1030', official=True, strategy_id='theme_lead')['id'] == 'all'
    assert service._run('live', official=False, strategy_id='theme_lead')['id'] == 'theme_lead'
    with pytest.raises(ValueError):
        service._run('live', official=False, strategy_id='custom.missing')


def test_new_scan_frozen_strategy_document_is_identical_in_all_views(tmp_path, monkeypatch):
    from src.services.yao_scout import signal_learning, primary_readiness, history_maintenance
    archived = []
    service = object.__new__(LocalOpportunityService)
    service.yao = SimpleNamespace(data_dir=tmp_path)
    service.db = SimpleNamespace(save_yao_run=lambda row: archived.append(deepcopy(row)))
    service.clock = lambda: datetime.fromisoformat('2026-11-19T10:30:00+08:00')
    service._strategy_context = {'id': 'theme_lead', 'rules': []}
    monkeypatch.setattr(strategies, 'build_strategy_publication', selection)
    monkeypatch.setattr(signal_learning, 'SignalLearningService', lambda *_: SimpleNamespace(
        persist_scan=lambda result, *_: result, review_daily_observations=lambda *_, **__: {}))
    monkeypatch.setattr(primary_readiness, 'read_primary_readiness', lambda *_, **__: {})
    monkeypatch.setattr(history_maintenance, 'start_history_preparation', lambda *_: None)
    run = scan(generatedAt=service.clock().isoformat(), official=False)
    completed = service._persist_run(run)
    store = LocalPicksDisplayStore(service.db, data_dir=tmp_path)
    doc = completed['publishedPicks']
    assert doc['strategyId'] == 'theme_lead' and len(doc['items']) == 5
    assert doc['official'] is False
    assert doc['ruleSnapshots'] == completed['strategySelection']['ruleSnapshots']
    assert archived[-1]['publishedPicks'] == store.read()['adaptive']['publishedPicks'] == doc
    assert store.records(scope='history')['items'] == [doc]
    service._strategy_context['id'] = 'countertrend_leader'
    replay = service._persist_run(run)
    assert replay['publishedPicks'] == doc
    assert replay['strategySelection']['strategyId'] == 'theme_lead'


def test_global_display_tracks_latest_manual_and_scheduled_without_rewriting_old_scopes(runtime):
    client, db, rows, _ = runtime
    store = LocalPicksDisplayStore(db)
    docs = []
    for minute, strategy_id, official in ((30, 'all', True), (31, 'theme_lead', False), (32, 'all', True)):
        run = scan(code=f'6000{minute}', generatedAt=f'2026-11-19T10:{minute}:00+08:00', official=official)
        run['strategySelection'] = selection(run, strategy_id)
        shown = store.save(run, source='scheduled' if official else 'manual')
        docs.append(shown['adaptive']['publishedPicks'])
        rows.append({'run_id': run['run_id'], 'mode': 'king_live', 'result': {**run, 'publishedPicks': docs[-1]}})
        assert client.get('/picks/display').json()['adaptive']['publishedPicks'] == docs[-1]
    before = store.path.read_bytes()
    filtered = client.get('/picks/display', params={'strategy_id': 'theme_lead'}).json()
    assert filtered['adaptive']['publishedPicks'] == docs[1]
    assert store.path.read_bytes() == before
    assert store.records(scope='history')['items'] == list(reversed(docs))
