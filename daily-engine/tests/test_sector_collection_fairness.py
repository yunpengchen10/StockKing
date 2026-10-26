from datetime import timedelta

from src.services.yao_scout import sector_fund_evidence as mod
from tests.test_sector_partial_results import NOW, bars, stock


def test_frozen_peer_plan_is_independent_of_candidate_order_and_keeps_completion_groups_together(tmp_path):
    priorities = [(5, 5, 'large', [f'60000{i}' for i in range(5)]),
                  (2, 2, 'small', ['000001', '000002']),
                  (3, 3, 'other', ['001001', '001002', '001003'])]
    first, _, order = mod.sector_collection_plan(priorities, tmp_path)
    second, _, reversed_order = mod.sector_collection_plan(list(reversed(priorities)), tmp_path)
    assert first == second and order == reversed_order
    assert first[:5] == ['000001', '000002', '001001', '001002', '001003']
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


def test_many_industries_get_completed_cohorts_instead_of_one_peer_per_round(tmp_path):
    from time import sleep
    groups = {f'industry-{i:02}':[f'60{i:02}{j:02}' for j in range(10)] for i in range(20)}
    candidates = [members[0] for members in groups.values()]
    membership = {members[0]:{'code':board} for board,members in groups.items()}
    def fetch(code, **kwargs):
        sleep(.02)
        return bars()
    result = mod.fetch_sector_batch(candidates, {code:stock() for code in candidates}, NOW,
        tmp_path, prepared=(membership, groups), bar_fetcher=fetch, timeout=.3)
    completed = [row for row in result.values() if 'sector_return_5m_pct' in row['metrics']]
    assert len(completed) >= 2
    assert len(completed) < len(groups)
    assert all(row['totalPeers'] == 9 and row['coverageRequiredPct'] == 80 for row in result.values())
    assert all(row['peerCount'] >= 8 and row['asOf'] == NOW.isoformat() for row in completed)
    # The untouched cohort gets a focused turn in the next scan.
    priorities = [(9,10,board,members) for board,members in groups.items()]
    _, state, next_order = mod.sector_collection_plan(priorities, tmp_path)
    assert state['industries'][next_order[0]]['lastAttemptTurn'] == 0


def test_exact_memory_windows_are_not_lost_when_archive_read_queue_times_out(tmp_path, monkeypatch):
    cached = ['600001','600002','600003','600004']
    uncached = ['000001','000002','000003','000004']
    monkeypatch.setattr(mod, '_recent_sector_bars', {
        (str(tmp_path.resolve()), code):(mod.monotonic(), bars()) for code in cached})
    class Archive:
        def __init__(self, *args):
            pass
        def save(self, *args):
            pass
        def read_recent(self, *args, **kwargs):
            return []
    monkeypatch.setattr(mod, 'MinuteArchive', Archive)
    monkeypatch.setattr(mod, 'bounded_fetch_map', lambda fn, items, **kwargs:([], list(items)))
    calls = []
    monkeypatch.setattr(mod, 'fetch_software_bars', lambda code, **kwargs:calls.append(code) or [])
    monkeypatch.setattr(mod, 'fetch_recent_bars', lambda code, **kwargs:[])
    result = mod.fetch_sector_batch([uncached[0],cached[0]],
        {code:stock() for code in [uncached[0],cached[0]]}, NOW, tmp_path,
        prepared=({uncached[0]:{'code':'uncached'},cached[0]:{'code':'cached'}},
                  {'uncached':uncached,'cached':cached}), timeout=1)
    assert result[cached[0]]['metrics']['sector_coverage_pct'] == 100
    assert result[cached[0]]['fetchBudget']['memoryCacheUsablePeers'] == 4
    assert not set(cached).intersection(calls)
    assert 'sector_return_5m_pct' not in result[uncached[0]]['metrics']
