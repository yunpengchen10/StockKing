from collections import Counter
from datetime import datetime, timedelta
from threading import Event, Lock
from time import monotonic

import pytest

from src.services.yao_scout import sector_fund_evidence as mod
from src.services.yao_scout.minute_history import bounded_fetch_map


NOW = datetime(2026, 10, 16, 10, 30, tzinfo=mod.SHANGHAI)


def bars(end=NOW, change=1):
    return [{'end': (end-timedelta(minutes=i)).isoformat(),
             'open': 10., 'close': 10.+change/10 if i == 0 else 10.,
             'high': 10.5, 'low': 9.5, 'volume_shares': 100., 'amount_cny': 1000.,
             'source': 'verified-fixture'} for i in range(6)]


def stock(end=NOW):
    return {'asOf': end.isoformat(), 'metrics': {'speed_5m_pct': 2}}


def test_slow_industry_keeps_completed_industry_within_outer_budget(tmp_path):
    fast = ['600001', '600002', '600003', '600004']
    slow = ['000001', '000002', '000003', '000004']
    release = Event()
    called = []

    def fetch(code, **kwargs):
        called.append(code)
        if code in slow:
            release.wait(2)
        return bars()

    def collect(_):
        return mod.fetch_sector_batch([slow[0], fast[0]],
            {slow[0]: stock(), fast[0]: stock()}, NOW, tmp_path,
            prepared=({slow[0]: {'code': 'slow'}, fast[0]: {'code': 'fast'}},
                      {'slow': slow, 'fast': fast}),
            bar_fetcher=fetch, timeout=.3)

    try:
        completed, unfinished = bounded_fetch_map(collect, [None], timeout=.3)
        assert not unfinished and len(completed) == 1
        result = completed[0]
        assert result[fast[0]]['confirmed']
        assert result[fast[0]]['metrics']['sector_return_5m_pct'] == pytest.approx(1)
        assert result[fast[0]]['fetchBudget']['returnReserveSeconds'] > 0
        assert result[slow[0]]['totalPeers'] == 3
        assert result[slow[0]]['peerCount'] == 0
        assert result[slow[0]]['coverageRequiredPct'] == 80
        assert 'sector_return_5m_pct' not in result[slow[0]]['metrics']
        assert result[slow[0]]['fetchBudget']['unfinishedPeers'] == 3
        assert 'sector_peer_deadline_exhausted' in result[slow[0]]['reasonCodes']
        assert Counter(called) == Counter(fast+slow)
    finally:
        release.set()


def test_cached_industry_is_published_before_slow_provider_and_survives_outer_timeout(tmp_path, monkeypatch):
    fast = ['600001', '600002', '600003', '600004']
    slow = ['000001', '000002', '000003', '000004']
    release, finished = Event(), Event()
    latest, lock = {}, Lock()

    class Archive:
        def __init__(self, path):
            pass

        def read_recent(self, code, *args, **kwargs):
            return bars() if code in fast else []

        def save(self, *args):
            pass

    def fetch(code, **kwargs):
        assert code in slow, 'Completed cached peers must not be downloaded again'
        release.wait(2)
        return []

    def publish(code, packet):
        with lock:
            latest[code] = packet

    monkeypatch.setattr(mod, 'MinuteArchive', Archive)
    monkeypatch.setattr(mod, '_recent_sector_bars', {})
    monkeypatch.setattr(mod, 'fetch_recent_bars', fetch)

    def collect(_):
        try:
            return mod.fetch_sector_batch([slow[0], fast[0]],
                {slow[0]: stock(), fast[0]: stock()}, NOW, tmp_path,
                prepared=({slow[0]: {'code': 'slow'}, fast[0]: {'code': 'fast'}},
                          {'slow': slow, 'fast': fast}),
                timeout=.5, on_result=publish)
        finally:
            finished.set()

    try:
        completed, unfinished = bounded_fetch_map(collect, [None], timeout=.15)
        assert not completed and unfinished == [None]
        with lock:
            frozen = dict(latest)
        assert frozen[fast[0]]['confirmed']
        assert frozen[fast[0]]['collectionPhase'] == 'cache_snapshot'
        assert frozen[fast[0]]['asOf'] == NOW.isoformat()
        assert frozen[fast[0]]['sourceIds'] == ['verified-fixture']
        assert frozen[fast[0]]['peerCount'] == frozen[fast[0]]['totalPeers'] == 3
        assert 'sector_return_5m_pct' not in frozen[slow[0]]['metrics']
        release.set()
        assert finished.wait(1)
        # The consumer's frozen packets are not later mutated by the worker.
        assert frozen[fast[0]]['collectionPhase'] == 'cache_snapshot'
    finally:
        release.set()
        assert finished.wait(2)


def test_overlapping_industries_fetch_each_peer_once_and_preserve_exact_window(tmp_path):
    a, b = '600001', '600002'
    members = ['600001', '600002', '600003', '600004', '600005']
    called = []

    def fetch(code, **kwargs):
        called.append(code)
        # This peer ends one minute early and must not count at NOW, but it
        # remains usable at the earlier candidate's own completed-minute end.
        return bars(NOW-timedelta(minutes=1) if code == members[-1] else NOW)

    result = mod.fetch_sector_batch([a, b], {a: stock(), b: stock(NOW-timedelta(minutes=1))},
        NOW, tmp_path, prepared=({a: {'code': 'a'}, b: {'code': 'b'}},
                                {'a': members, 'b': members}),
        bar_fetcher=fetch, timeout=1)
    assert Counter(called) == Counter(members)
    assert result[a]['totalPeers'] == result[b]['totalPeers'] == 4
    assert result[a]['peerCount'] == 3
    assert result[b]['peerCount'] == 1
    assert result[a]['metrics']['sector_coverage_pct'] == 75
    assert result[b]['metrics']['sector_coverage_pct'] == 25
    assert all('sector_return_5m_pct' not in row['metrics'] for row in result.values())
    assert result[a]['asOf'] == NOW.isoformat()
    assert result[b]['asOf'] == (NOW-timedelta(minutes=1)).isoformat()
    assert a not in result[a]['peerReturns'] and b not in result[b]['peerReturns']


def test_provider_exception_is_distinct_from_budget_exhaustion(tmp_path):
    members = ['600001', '600002', '600003', '600004']

    def fetch(code, **kwargs):
        if code == members[-1]:
            raise ConnectionError('fixture source failed')
        return bars()

    result = mod.fetch_sector_batch([members[0]], {members[0]: stock()}, NOW, tmp_path,
        prepared=({members[0]: {'code': 'a'}}, {'a': members}),
        bar_fetcher=fetch, timeout=1)[members[0]]
    assert result['fetchBudget']['unfinishedPeers'] == 0
    assert result['fetchBudget']['failedPeers'] == 1
    assert result['peerFetchErrors'] == {members[-1]: 'ConnectionError'}
    assert 'sector_peer_source_failed' in result['reasonCodes']
    assert 'sector_peer_deadline_exhausted' not in result['reasonCodes']
    assert result['totalPeers'] == 3 and result['peerCount'] == 2
    assert 'sector_return_5m_pct' not in result['metrics']


def test_prepared_unrelated_boards_and_missing_stock_windows_do_not_consume_peer_budget(tmp_path):
    called = []
    result = mod.fetch_sector_batch(['600001'], {}, NOW, tmp_path,
        prepared=({'600001': {'code': 'a'}}, {'a': ['600001', '600002'], 'unused': ['000001']}),
        bar_fetcher=lambda code, **kwargs: called.append(code) or bars(), timeout=1)
    assert not called
    assert not result['600001']['metrics']
    assert result['600001']['gaps'] == ['个股5分钟证据或所属行业未核实']


def test_membership_failure_is_published_as_explicit_missing_packet(tmp_path, monkeypatch):
    emitted = {}

    def unavailable(*args, **kwargs):
        raise TimeoutError('fixture membership unavailable')

    monkeypatch.setattr(mod, 'prepare_sector_membership', unavailable)
    result = mod.fetch_sector_batch(['600001'], {'600001': stock()}, NOW, tmp_path,
                                   on_result=lambda code, packet: emitted.update({code: packet}))
    assert emitted == result
    assert result['600001']['reasonCodes'] == ['sector_membership_unavailable']
    assert result['600001']['requestedCutoffAt'] == NOW.isoformat()


def test_fair_peer_budget_serves_large_industry_early_and_still_completes_fast_small_industry(tmp_path):
    small = [f'60000{i}' for i in range(6)]
    large = [f'0000{i:02d}' for i in range(20)]
    called = []
    release = Event()

    def fetch(code, **kwargs):
        called.append(code)
        if code in large:
            release.wait(2)
        return bars()

    try:
        result = mod.fetch_sector_batch([large[0], small[0]],
            {large[0]: stock(), small[0]: stock()}, NOW, tmp_path,
            prepared=({large[0]: {'code': 'large'}, small[0]: {'code': 'small'}},
                      {'large': large, 'small': small}),
            bar_fetcher=fetch, timeout=.3)
        first_large = next(i for i, code in enumerate(called) if code in large)
        assert first_large <= 4  # A slow industry cannot monopolize all eight workers.
        assert result[small[0]]['metrics']['sector_coverage_pct'] >= 80
        assert result[small[0]]['totalPeers'] == 5
        assert result[small[0]]['confirmed']
        assert result[large[0]]['totalPeers'] == 19
        assert 'sector_return_5m_pct' not in result[large[0]]['metrics']
    finally:
        release.set()


def test_complete_six_bar_software_window_does_not_trigger_unnecessary_other_source(tmp_path, monkeypatch):
    members = ['600001', '600002', '600003', '600004']
    called = []
    monkeypatch.setattr(mod, '_recent_sector_bars', {})
    monkeypatch.setattr(mod, 'fetch_software_bars', lambda code, **kwargs: called.append(code) or bars())

    def unexpected(*args, **kwargs):
        pytest.fail('A complete exact six-bar window must not trigger a 20-row supplement')

    monkeypatch.setattr(mod, 'fetch_recent_bars', unexpected)
    result = mod.fetch_sector_batch([members[0]], {members[0]: stock()}, NOW, tmp_path,
        prepared=({members[0]: {'code': 'a'}}, {'a': members}), timeout=1)[members[0]]
    assert Counter(called) == Counter(members)
    assert result['metrics']['sector_coverage_pct'] == 100
    assert result['confirmed']


def test_failed_member_is_replaced_without_starving_another_industry(tmp_path):
    small = [f'60000{i}' for i in range(6)]
    large = [f'0000{i:02d}' for i in range(20)]
    calls, emitted = [], []
    release = Event()

    def fetch(code, **kwargs):
        calls.append(code)
        if code == small[2]:
            raise ConnectionError('unavailable constituent remains in denominator')
        if code in large:
            release.wait(2)
        return bars()

    try:
        result = mod.fetch_sector_batch([large[0], small[0]],
            {large[0]:stock(), small[0]:stock()}, NOW, tmp_path,
            prepared=({large[0]:{'code':'large'}, small[0]:{'code':'small'}},
                      {'large':large, 'small':small}),
            bar_fetcher=fetch, timeout=.3,
            on_result=lambda code, packet:emitted.append((code, packet)))
        first_large = next(i for i, code in enumerate(calls) if code in large)
        assert first_large <= 4
        assert set(small).issubset(calls)
        assert result[small[0]]['metrics']['sector_coverage_pct'] == 80
        assert result[small[0]]['totalPeers'] == 5
        assert result[small[0]]['peerFetchErrors'] == {small[2]:'ConnectionError'}
        assert result[large[0]]['totalPeers'] == 19
        assert any(code == small[0] and packet.get('confirmed')
                   and packet['collectionPhase'] == 'industry_snapshot' for code, packet in emitted)
    finally:
        release.set()


def test_slow_quota_peer_gets_fast_substitute_without_exceeding_shared_concurrency_or_mutating_results(tmp_path):
    small = [f'60000{i}' for i in range(6)]
    large = [f'0000{i:02d}' for i in range(20)]
    release, finished = Event(), Event()
    lock = Lock()
    calls, emitted, concurrency = [], [], {'current': 0, 'maximum': 0}

    def fetch(code, **kwargs):
        with lock:
            calls.append(code)
            concurrency['current'] += 1
            concurrency['maximum'] = max(concurrency['maximum'], concurrency['current'])
            finished.clear()
        try:
            if code == small[2] or code in large:
                release.wait(2)
            return bars()
        finally:
            with lock:
                concurrency['current'] -= 1
                if not concurrency['current']:
                    finished.set()

    started = monotonic()
    try:
        completed, pending = bounded_fetch_map(lambda _: mod.fetch_sector_batch(
            [small[0], large[0]], {small[0]: stock(), large[0]: stock()}, NOW, tmp_path,
            prepared=({small[0]: {'code': 'small'}, large[0]: {'code': 'large'}},
                      {'small': small, 'large': large}),
            bar_fetcher=fetch, timeout=.5,
            on_result=lambda code, packet: emitted.append((code, packet))), [None], timeout=.5)
        assert not pending and len(completed) == 1
        result = completed[0]
        assert monotonic()-started < .5
        assert small[-1] in calls, 'A free worker must fetch the fast spare without waiting for the slow quota peer'
        assert result[small[0]]['confirmed']
        assert result[small[0]]['peerCount'] == 4 and result[small[0]]['totalPeers'] == 5
        assert result[small[0]]['metrics']['sector_coverage_pct'] == 80
        assert result[large[0]]['totalPeers'] == 19
        assert 'sector_return_5m_pct' not in result[large[0]]['metrics']
        assert concurrency['maximum'] <= 8
        assert len(calls) == len(set(calls))
        frozen = [packet for code, packet in emitted if code == small[0]
                  and packet.get('confirmed') and packet['collectionPhase'] == 'industry_snapshot'][0]
        release.set()
        assert finished.wait(1)
        assert frozen['peerCount'] == result[small[0]]['peerCount'] == 4
        assert frozen['collectionPhase'] == 'industry_snapshot'
    finally:
        release.set()
        assert finished.wait(2)


def test_missing_membership_for_one_stock_does_not_erase_other_industry(tmp_path):
    members = ['600001', '600002', '600003', '600004']
    result = mod.fetch_sector_batch(['000001', '600001'],
        {'000001':stock(), '600001':stock()}, NOW, tmp_path,
        prepared=({'600001':{'code':'industry'}}, {'industry':members}),
        bar_fetcher=lambda *a, **k:bars(), timeout=1)
    assert result['600001']['confirmed']
    assert 'sector_membership_unavailable' in result['000001']['reasonCodes']


def test_shared_peer_payload_is_normalized_once_across_industry_snapshots(tmp_path, monkeypatch):
    groups = {f'board-{n}':[f'60{n:02d}{i:02d}' for i in range(5)] for n in range(4)}
    candidates = [members[0] for members in groups.values()]
    memberships = {members[0]:{'code':board} for board,members in groups.items()}
    payloads, normalization_count = {}, Counter()
    original = mod.normalize_bars

    def normalize(rows, *args, **kwargs):
        if id(rows) in payloads:
            normalization_count[id(rows)] += 1
        return original(rows, *args, **kwargs)

    def fetch(code, **kwargs):
        result = bars()
        payloads[id(result)] = result
        return result

    monkeypatch.setattr(mod, 'normalize_bars', normalize)
    result = mod.fetch_sector_batch(candidates, {code:stock() for code in candidates}, NOW,
        tmp_path, prepared=(memberships,groups), bar_fetcher=fetch, timeout=2)
    assert all(packet['metrics']['sector_coverage_pct'] == 100 for packet in result.values())
    assert len(payloads) == 20
    assert len(normalization_count) == 20
    assert set(normalization_count.values()) == {1}


def test_additional_completed_peer_invalidates_cached_industry_window(tmp_path):
    members = [f'60000{i}' for i in range(6)]
    snapshots = []
    def fetch(code, **kwargs):
        return bars(change=5 if code == members[-1] else 1)
    result = mod.fetch_sector_batch([members[0]], {members[0]:stock()}, NOW,
        tmp_path, prepared=({members[0]:{'code':'industry'}}, {'industry':members}),
        bar_fetcher=fetch, timeout=1,
        on_result=lambda code,packet:snapshots.append(packet))[members[0]]
    assert result['peerCount'] == 5
    assert result['metrics']['sector_return_5m_pct'] == pytest.approx(1.8)
    assert snapshots[-1]['collectionPhase'] == 'complete'
    assert all(snapshot['peerCount'] <= result['peerCount'] for snapshot in snapshots)


def test_slow_secondary_source_cannot_prevent_other_primary_industries(tmp_path, monkeypatch):
    bad = ['600001', '600002', '600003', '600004']
    good = ['000001', '000002', '000003', '000004', '000005']
    release = Event()
    primary, secondary = [], []
    monkeypatch.setattr(mod, '_recent_sector_bars', {})

    def software(code, **kwargs):
        primary.append(code)
        if code in bad:
            raise ConnectionError('primary source unavailable')
        return bars()

    def fallback(code, **kwargs):
        secondary.append(code)
        assert set(primary) == set(bad+good)
        release.wait(2)
        raise ConnectionError('secondary source unavailable')

    monkeypatch.setattr(mod, 'fetch_software_bars', software)
    monkeypatch.setattr(mod, 'fetch_recent_bars', fallback)
    try:
        result = mod.fetch_sector_batch([bad[0], good[0]],
            {bad[0]:stock(), good[0]:stock()}, NOW, tmp_path,
            prepared=({bad[0]:{'code':'bad'}, good[0]:{'code':'good'}},
                      {'bad':bad, 'good':good}), timeout=.5)
        assert set(primary) == set(bad+good)
        assert secondary
        assert result[good[0]]['confirmed']
        assert result[good[0]]['metrics']['sector_coverage_pct'] == 100
        assert result[bad[0]]['totalPeers'] == 3
        assert result[bad[0]]['fetchBudget']['unfinishedPeers'] == 3
        assert not result[bad[0]].get('confirmed')
    finally:
        release.set()


def test_secondary_source_recovers_missing_peer_without_retaining_primary_error(tmp_path, monkeypatch):
    members = ['600001', '600002', '600003', '600004']
    primary = []
    monkeypatch.setattr(mod, '_recent_sector_bars', {})

    def software(code, **kwargs):
        primary.append(code)
        if code == members[-1]:
            raise ConnectionError('primary failure')
        return bars()

    def fallback(code, **kwargs):
        assert code == members[-1] and set(primary) == set(members)
        return bars()

    monkeypatch.setattr(mod, 'fetch_software_bars', software)
    monkeypatch.setattr(mod, 'fetch_recent_bars', fallback)
    result = mod.fetch_sector_batch([members[0]], {members[0]:stock()}, NOW, tmp_path,
        prepared=({members[0]:{'code':'industry'}}, {'industry':members}), timeout=1)[members[0]]
    assert result['confirmed']
    assert result['metrics']['sector_coverage_pct'] == 100
    assert result['fetchBudget']['failedPeers'] == result['fetchBudget']['unfinishedPeers'] == 0
    assert 'sector_peer_source_failed' not in result['reasonCodes']
