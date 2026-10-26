from datetime import timedelta

from src.services.yao_scout import sector_fund_evidence as mod
from tests.test_sector_partial_results import NOW, bars, stock


def test_frozen_peer_plan_is_independent_of_candidate_order_and_gives_each_industry_first_turn(tmp_path):
    priorities = [(5, 5, 'large', [f'60000{i}' for i in range(5)]),
                  (2, 2, 'small', ['000001', '000002']),
                  (3, 3, 'other', ['001001', '001002', '001003'])]
    first, _, order = mod.sector_collection_plan(priorities, tmp_path)
    second, _, reversed_order = mod.sector_collection_plan(list(reversed(priorities)), tmp_path)
    assert first == second and order == reversed_order
    assert set(first[:3]) == {'600000', '000001', '001001'}
    assert len(first) == len(set(first)) == 10


def test_unreached_industry_gets_next_scan_first_turn_without_changing_denominator(tmp_path):
    boards = {'small': ['000001', '000002'], 'large': ['600001', '600002', '600003', '600004']}
    priorities = [(2, 2, 'small', boards['small']), (4, 4, 'large', boards['large'])]
    before, _, _ = mod.sector_collection_plan(priorities, tmp_path)
    assert before[0] == '000001'
    mod.record_sector_collection(tmp_path, boards, set(boards['small']), set(),
        {'small': 2, 'large': 4}, NOW)
    after, state, _ = mod.sector_collection_plan(priorities, tmp_path)
    assert after[0] == '600001'
    assert set(before) == set(after)
    assert state['industries']['large']['totalPeers'] == 4
    assert state['industries']['large']['usablePeers'] == 0


def test_peer_cache_reuses_exact_window_beyond_old_ten_second_ttl_but_not_next_window(monkeypatch, tmp_path):
    members = ['600001', '600002', '600003', '600004']
    old = mod.monotonic() - 60
    monkeypatch.setattr(mod, '_recent_sector_bars', {
        (str(tmp_path.resolve()), code): (old, bars()) for code in members})
    class Archive:
        def __init__(self, *args):
            pass
        def read_recent(self, code, cutoff, **kwargs):
            return []
        def save(self, *args):
            pass
    monkeypatch.setattr(mod, 'MinuteArchive', Archive)
    calls = []
    monkeypatch.setattr(mod, 'fetch_software_bars', lambda code, **kw: calls.append(code) or [])
    monkeypatch.setattr(mod, 'fetch_recent_bars', lambda code, **kw: [])
    prepared = ({members[0]: {'code': 'industry'}}, {'industry': members})
    result = mod.fetch_sector_batch([members[0]], {members[0]: stock()}, NOW,
        tmp_path, prepared=prepared, timeout=1)
    assert result[members[0]]['confirmed'] and not calls
    later = NOW + timedelta(minutes=1)
    result = mod.fetch_sector_batch([members[0]], {members[0]: stock(later)}, later,
        tmp_path, prepared=prepared, timeout=1)
    assert set(calls) == set(members)
    assert result[members[0]]['totalPeers'] == 3
    assert not result[members[0]].get('confirmed')
