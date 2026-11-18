"""Scheduled publication is immediately visible, including after a cold restart."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services import stock_king_display as display
from src.services.yao_scout import potential_picks


def run(key, minute):
    return {'run_id': key, 'mode': 'king_1030', 'scanSlot': '1030', 'official': True,
            'status': 'completed_observations', 'generatedAt': f'2026-11-18T10:{minute:02d}:00+08:00',
            'candidates': [], 'dataQuality': {'snapshot_count': 3000}}


@pytest.fixture(autouse=True)
def offline_selection(monkeypatch):
    monkeypatch.setattr(potential_picks, 'select_potential_picks', lambda *_: [
        {'rank': 1, 'code': '600001', 'name': '离线样本', 'price': 10,
         'sourceTime': '2026-11-18T10:29:57+08:00', 'reasons': ['趋势空间'], 'risk': '支撑待确认'}])


def test_scheduled_persistence_updates_home_without_any_reader(tmp_path, monkeypatch):
    from src.services.yao_scout import history_maintenance, primary_readiness, signal_learning
    from src.services.yao_scout.local_opportunities import LocalOpportunityService
    archived = []
    service = object.__new__(LocalOpportunityService)
    service.db = SimpleNamespace(save_yao_run=lambda row: archived.append(deepcopy(row)))
    service.yao = SimpleNamespace(data_dir=tmp_path)
    service.clock = lambda: datetime.fromisoformat('2026-11-18T10:30:00+08:00')
    monkeypatch.setattr(signal_learning, 'SignalLearningService', lambda *_: SimpleNamespace(
        persist_scan=lambda result, *_: result, review_daily_observations=lambda *_, **__: {}))
    monkeypatch.setattr(primary_readiness, 'read_primary_readiness', lambda *_, **__: {})
    monkeypatch.setattr(history_maintenance, 'start_history_preparation', lambda *_: None)
    result = service._persist_run(run('official-1030', 30))
    # Inspect files directly: no read endpoint is allowed to repair the write.
    saved = json.loads((tmp_path / 'local-picks-display-v1.json').read_text(encoding='utf-8'))
    view = json.loads((tmp_path / 'local-picks-display-view-v1.json').read_text(encoding='utf-8'))['payload']
    expected = result['publishedPicks']
    assert saved['displaySource'] == 'scheduled'
    assert saved['displayRunId'] == view['displayRunId'] == result['run_id']
    assert archived[-1]['publishedPicks'] == saved['adaptive']['publishedPicks'] == view['adaptive']['publishedPicks'] == expected
    assert display.PicksPublications(tmp_path).latest() == expected


def test_publication_ahead_of_home_recovers_exact_scan_then_stops_reading_history(tmp_path):
    store = display.LocalPicksDisplayStore(None, data_dir=tmp_path)
    store.save(run('old', 30))
    newer = run('new', 31)
    publication = display.publish_picks(tmp_path, newer)
    reads = []
    def fetch(key):
        reads.append(key)
        return {'result': newer}
    def no_history(**_):
        pytest.fail('published homepage must not replay full scan history')
    db = SimpleNamespace(get_yao_run=fetch, list_yao_run_headers=no_history)
    recovered = display.LocalPicksDisplayStore(db, data_dir=tmp_path).read()
    assert recovered['displayRunId'] == 'new'
    assert recovered['adaptive']['publishedPicks'] == publication
    for _ in range(3):
        assert display.LocalPicksDisplayStore(db, data_dir=tmp_path).read() == recovered
    assert reads == ['new']


def test_cold_read_uses_persisted_projection_and_version_change_rebuilds(tmp_path, monkeypatch):
    store = display.LocalPicksDisplayStore(None, data_dir=tmp_path)
    expected = store.save(run('one', 30))
    display._display_cache.clear()
    read_text = Path.read_text
    raw_reads = []
    def tracked(path, *args, **kwargs):
        if path == store.path:
            raw_reads.append(str(path))
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', tracked)
    assert display.LocalPicksDisplayStore(None, data_dir=tmp_path).read() == expected
    assert raw_reads == []
    display._display_cache.clear()
    monkeypatch.setattr(display, '_PROJECTION_VERSION', display._PROJECTION_VERSION + 1)
    assert display.LocalPicksDisplayStore(None, data_dir=tmp_path).read() == expected
    assert len(raw_reads) == 1
    packet = json.loads(store._projection_path.read_text(encoding='utf-8'))
    assert packet['projectionVersion'] == display._PROJECTION_VERSION
