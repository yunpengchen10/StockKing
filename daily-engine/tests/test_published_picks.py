"""The ranked homepage list and machine records share one immutable publication."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.services.stock_king_display import LocalPicksDisplayStore, publish_picks
from src.services.stock_king_publications import PicksPublications
from src.services.yao_scout import potential_picks


def scan(key='one'):
    return {'run_id': key, 'mode': 'king_live', 'scanSlot': 'live', 'status': 'completed_observations',
            'generatedAt': '2026-11-17T10:30:00+08:00', 'candidates': [], 'dataQuality': {'snapshot_count': 100}}


def selections():
    return [{'rank': rank, 'code': f'60000{rank}', 'name': f'样本{rank}', 'price': 10.0,
             'sourceTime': '2026-11-17T10:29:57+08:00', 'reasons': ['趋势与相对空间'], 'risk': '支撑待确认'}
            for rank in range(1, 6)]


def test_home_history_filters_and_source_run_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    expected = selections()
    monkeypatch.setattr(potential_picks, 'select_potential_picks', lambda *_: deepcopy(expected))
    original = scan()
    db = SimpleNamespace(get_yao_run=lambda key: {'mode': 'king_live', 'run_id': key, 'result': original})
    store = LocalPicksDisplayStore(None)
    published = store.save(original)['adaptive']['publishedPicks']
    assert published['items'] == expected
    assert store.records()['items'] == [published]
    assert store.records(run_id='one')['items'] == [published]
    assert store.records(date='2026-11-17', version=potential_picks.VERSION, symbol='600004')['items'] == [
        {**published, 'items': [expected[3]]}]
    assert not store.records(symbol='600099')['items']
    expected.clear()
    assert store.save(original)['adaptive']['publishedPicks'] == published
    assert LocalPicksDisplayStore(db).history_snapshot('one')['result']['publishedPicks'] == published
    newer = {**scan('two'), 'generatedAt': '2026-11-17T10:35:00+08:00'}
    empty = store.save(newer)['adaptive']['publishedPicks']
    assert empty['status'] == 'published' and empty['items'] == []
    assert store.records()['items'] == [empty]
    assert store.records(scope='history')['items'] == [empty, published]
    legacy = store.save({**scan('old'), 'generatedAt': '2026-11-17T10:40:00+08:00'}, source='history')
    assert legacy['adaptive']['publishedPicks']['status'] == 'unrecorded'
    assert store.records(run_id='old')['status'] == 'unrecorded'


def test_independent_writers_and_interrupted_storage(tmp_path):
    ledger = PicksPublications(tmp_path)
    run = scan()
    def interrupted(doc):
        raise OSError('scan commit interrupted')
    with pytest.raises(OSError):
        ledger.publish(run, selections, reason='scan', persist=interrupted)
    assert ledger.history() == []
    with ThreadPoolExecutor(4) as pool:
        docs = list(pool.map(lambda _: ledger.publish(run, selections, reason='scan'), range(8)))
    assert all(doc == docs[0] for doc in docs)
    assert ledger.history() == [docs[0]]
    # The scan row committed its exact document before the auxiliary index commit.
    restored = publish_picks(tmp_path / 'restored', {**run, 'publishedPicks': docs[0]})
    assert restored == docs[0]
    legacy = ledger.restore_verified_legacy(scan('earlier'), selections()[:1],
                                             version='legacy-home-v1', proof='archived homepage evidence')
    assert legacy['publishedAt'] is None and legacy['recordedAt'] and len(legacy['items']) == 1


@pytest.mark.parametrize('second_at', ['2026-11-17T10:29:59+08:00', '2026-11-17T02:30:00Z'])
def test_older_or_tied_completion_cannot_publish_after_winner(tmp_path, monkeypatch, second_at):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    first = scan()
    winner = store.save(first)
    second = {**scan('second'), 'generatedAt': second_at}
    assert store.save(second) == winner
    # Official and manual jobs can persist their raw scan in a different order.
    persisted = []
    rejected = publish_picks(tmp_path, second, persist=persisted.append)
    assert persisted == [rejected] and rejected['status'] == 'superseded'
    assert store.save({**second, 'publishedPicks': rejected}) == winner
    assert store.records(scope='history')['items'] == [winner['adaptive']['publishedPicks']]
    assert store.records()['items'] == [winner['adaptive']['publishedPicks']]
    assert not store.records(run_id='second')['items']


def test_failed_display_write_and_unusable_scan_create_no_records(tmp_path, monkeypatch):
    from src.services import stock_king_display
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    bad = {**scan(), 'status': 'unavailable'}
    with pytest.raises(ValueError):
        store.save(bad)
    def refuse(*_):
        raise OSError('display file replacement failed')
    monkeypatch.setattr(stock_king_display.os, 'replace', refuse)
    with pytest.raises(OSError, match='replacement failed'):
        store.save(scan())
    assert not store.records(scope='history')['items']
