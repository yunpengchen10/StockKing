from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from src.services.yao_scout.fund_flow_evidence import compute_stock_flow, compute_sector_flows
from src.services.yao_scout.fund_flow_archive import FundFlowArchive
from src.services.yao_scout.sector_fund_evidence import compute_fund_evidence, compute_tencent_fund_evidence

END = datetime.fromisoformat('2026-11-03T11:00:00+08:00')


def evidence(code='600001', horizon=30, increment=10., *, provider='eastmoney', end=END):
    start = end-timedelta(minutes=horizon)
    series = [{'end': (start+timedelta(minutes=i)).isoformat(), 'main_net_flow': i*increment}
              for i in range(horizon+1)]
    fund = {'code': code, 'provider': provider, 'measurementVersion': provider+'-v1', 'source': provider+' funds',
            'amountUnit': 'CNY', 'normalizedTimeSemantics': 'bar_end', 'asOf': end.isoformat(),
            'cumulativeSeries': series}
    minute = {'code': code, 'source': 'verified interval bars', 'asOf': end.isoformat(),
              'flowAmountWindows': {str(horizon): {'amountCny': horizon*100., 'start': start.isoformat(),
                 'end': end.isoformat(), 'barCount': horizon, 'expectedBarCount': horizon, 'source': 'interval bars'}}}
    return fund, minute


@pytest.mark.parametrize('horizon', [15, 30, 60])
def test_same_window_normalization_preserves_negative_evidence(horizon):
    fund, minute = evidence(horizon=horizon, increment=-10)
    result = compute_stock_flow(fund, minute, END, horizon)
    assert result['status'] == 'observed'
    assert result['metrics']['netFlowCny'] == -10*horizon
    assert result['metrics']['netFlowRatio'] == -.1
    assert result['metrics']['positiveMinuteFraction'] == 0
    assert result['metrics']['recentOutflow'] is True


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'future', 'wrong_source', 'stale', 'lunch', 'next_day'])
def test_broken_fund_window_cannot_borrow_other_time_or_source(damage):
    fund, minute = evidence()
    if damage == 'missing':
        fund['cumulativeSeries'].pop(5)
    elif damage == 'duplicate':
        row = deepcopy(fund['cumulativeSeries'][5]); row['main_net_flow'] += 1
        fund['cumulativeSeries'].append(row)
    elif damage == 'future':
        fund['asOf'] = (END+timedelta(minutes=1)).isoformat()
    elif damage == 'wrong_source':
        fund['cumulativeSeries'][5]['provider'] = 'other'
    elif damage == 'stale':
        fund['asOf'] = (END-timedelta(minutes=5)).isoformat()
    elif damage == 'lunch':
        fund, minute = evidence(end=END.replace(hour=13, minute=10))
    else:
        fund['cumulativeSeries'][5]['end'] = (END-timedelta(days=1, minutes=25)).isoformat()
    cutoff = END.replace(hour=13, minute=10) if damage == 'lunch' else END
    assert compute_stock_flow(fund, minute, cutoff)['status'] == 'unavailable'


@pytest.mark.parametrize('damage', ['missing', 'no_source', 'end', 'start', 'count', 'zero', 'units', 'tainted'])
def test_turnover_must_have_verified_same_window_coverage(damage):
    fund, minute = evidence()
    window = minute['flowAmountWindows']['30']
    if damage == 'missing': minute['flowAmountWindows'] = {}
    elif damage == 'no_source': window['source'] = 'unknown'
    elif damage == 'end': window['end'] = (END-timedelta(minutes=1)).isoformat()
    elif damage == 'start': window['start'] = (END-timedelta(minutes=29)).isoformat()
    elif damage == 'count': window['barCount'] = 29
    elif damage == 'zero': window['amountCny'] = 0
    elif damage == 'units': window['amountCny'] = 3
    else: minute['dataLineage'] = {'status': 'tainted'}
    result = compute_stock_flow(fund, minute, END)
    assert result['status'] == 'unavailable'
    assert result['metrics']['netFlowRatio'] is None
    assert result['metrics']['netFlowCny'] == 300  # availability is separate from direction


def test_recent_outflow_is_not_hidden_by_positive_30_minute_total():
    fund, minute = evidence()
    for i in range(26, 31):
        fund['cumulativeSeries'][i]['main_net_flow'] = 250-(i-25)*5
    metrics = compute_stock_flow(fund, minute, END)['metrics']
    assert metrics['netFlowCny'] == 225
    assert metrics['priorNetFlowCny'] == 50
    assert metrics['recentNetFlowCny'] == -25
    assert metrics['recentSubwindowMinutes'] == 5
    assert metrics['fading'] and metrics['recentOutflow']
    assert metrics['positiveMinuteFraction'] == pytest.approx(25/30)


def sector_fixture(count=5):
    codes = [f'60000{i}' for i in range(count+1)]
    membership = {codes[0]: {'code': 'board', 'name': 'complete industry', 'provider': 'taxonomy',
                             'membershipComplete': True, 'membershipCount': len(codes)}}
    funds, minutes = {}, {}
    for code in codes:
        funds[code], minutes[code] = evidence(code)
    return codes, membership, {'board': codes}, funds, minutes


def test_sector_excludes_self_and_keeps_missing_members_in_denominator():
    codes, membership, peers, funds, minutes = sector_fixture()
    funds[codes[0]], minutes[codes[0]] = evidence(codes[0], increment=-90)
    del funds[codes[-1]]
    packet = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert packet['status'] == 'observed'
    assert packet['coverage']['totalPeers'] == 5 and packet['coverage']['usablePeers'] == 4
    assert packet['coverage']['fraction'] == .8
    assert packet['metrics']['netFlowCny'] == 1200
    assert packet['metrics']['positiveMemberFraction'] == 1
    assert packet['metrics']['top1PositiveContribution'] == .25
    assert codes[0] not in packet['observedPeerCodes']
    del funds[codes[-2]]
    packet = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert packet['status'] == 'unavailable' and packet['coverage']['fraction'] == .6


def test_mixed_provider_or_different_windows_cannot_fill_coverage():
    codes, membership, peers, funds, minutes = sector_fixture()
    for code in codes[-2:]:
        funds[code], minutes[code] = evidence(code, provider='tencent')
    packet = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert packet['status'] == 'unavailable' and packet['coverage']['mixedSources']
    assert packet['coverage']['fraction'] == .6
    for code in codes[-2:]:
        funds[code], minutes[code] = evidence(code, end=END-timedelta(minutes=1))
    packet = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert packet['status'] == 'unavailable'
    assert len(packet['coverage']['groups']) == 1  # older peers cannot be moved forward


def test_later_peer_series_and_compact_turnover_align_to_real_candidate_endpoint():
    codes, membership, peers, funds, minutes = sector_fixture(count=3)
    earlier = END-timedelta(minutes=1)
    funds[codes[0]], minutes[codes[0]] = evidence(codes[0], end=earlier)
    for code in codes[1:]:
        funds[code], _ = evidence(code, horizon=60)
        # A huge later movement must not leak into the frozen earlier window.
        funds[code]['cumulativeSeries'][-1]['main_net_flow'] = 1e9
        _, old_minute = evidence(code, end=earlier)
        minutes[code]['flowAmountWindowsByEnd'] = {earlier.isoformat(): old_minute}
    result = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert result['status'] == 'observed' and result['asOf'] == earlier.isoformat()
    assert result['metrics']['netFlowCny'] == 900
    assert result['metrics']['netFlowRatio'] == .1
    funds[codes[-1]]['cumulativeSeries'] = [row for row in funds[codes[-1]]['cumulativeSeries']
                                         if row['end'] != earlier.isoformat()]
    result = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert result['status'] == 'unavailable' and result['coverage']['usablePeers'] == 2


def test_earlier_compact_window_cannot_bypass_parent_quarantine():
    codes, membership, peers, funds, minutes = sector_fixture(count=3)
    earlier = END-timedelta(minutes=1)
    funds[codes[0]], minutes[codes[0]] = evidence(codes[0], end=earlier)
    for code in codes[1:]:
        funds[code], _ = evidence(code, horizon=60)
        _, old_minute = evidence(code, end=earlier)
        old_minute['dataLineage'] = {'status': 'verified'}
        minutes[code]['flowAmountWindowsByEnd'] = {earlier.isoformat(): old_minute}
    minutes[codes[-1]]['dataLineage'] = {'status': 'tainted'}
    result = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert result['status'] == 'unavailable' and result['coverage']['usablePeers'] == 2


def test_sector_concentration_and_negative_members_are_not_discarded():
    codes, membership, peers, funds, minutes = sector_fixture(count=3)
    for code, increment in zip(codes[1:], [10, 30, -20]):
        funds[code], minutes[code] = evidence(code, increment=increment)
    result = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert result['status'] == 'observed'
    assert result['metrics']['netFlowCny'] == 600
    assert result['metrics']['positiveMemberFraction'] == pytest.approx(2/3)
    assert result['metrics']['top1PositiveContribution'] == .75


@pytest.mark.parametrize('damage', ['unspecified', 'wrong_count', 'duplicate_member', 'false'])
def test_sector_requires_explicit_complete_denominator(damage):
    codes, membership, peers, funds, minutes = sector_fixture()
    if damage == 'unspecified': membership[codes[0]].pop('membershipComplete')
    elif damage == 'wrong_count': membership[codes[0]]['membershipCount'] = 4
    elif damage == 'false': membership[codes[0]]['membershipComplete'] = False
    else: peers['board'].append(codes[-1])
    result = compute_sector_flows(membership, peers, funds, minutes, END)[codes[0]]
    assert result['status'] == 'unavailable'
    assert result['reasons'] == ['complete_sector_membership_unavailable']


def test_long_windows_are_derived_from_legacy_provider_rows_without_replacing_3m():
    fund, minute = evidence(horizon=60)
    rows = [','.join([r['end'], str(r['main_net_flow']), '0', '0', str(r['main_net_flow']), '0'])
            for r in fund['cumulativeSeries']]
    result = compute_fund_evidence('600001', {'code': '600001', 'klines': rows}, END)
    assert len(result['cumulativeSeries']) == 61
    assert result['metrics']['main_net_flow_3m'] == 30
    for n in (15, 30, 60):
        assert result['metrics'][f'main_net_flow_{n}m'] == n*10
        assert result['metrics'][f'main_net_positive_fraction_{n}m'] == 1
    assert result['flowWindows']['60']['provider'] == 'eastmoney'


def test_archive_preserves_original_quarantines_revision_and_obeys_availability(tmp_path):
    archive = FundFlowArchive(tmp_path)
    fund, _ = evidence()
    archive.save(fund, END, observed_at=END)
    altered = deepcopy(fund)
    altered['cumulativeSeries'][5]['main_net_flow'] += 1
    late = END+timedelta(minutes=2)
    assert archive.save(altered, late, observed_at=late)['conflicts'] == 1
    before = archive.read('600001', END.date(), 'eastmoney', 'eastmoney-v1', as_of=END)
    after = archive.read('600001', END.date(), 'eastmoney', 'eastmoney-v1', as_of=late)
    assert len(before['cumulativeSeries']) == 31 and before['cumulativeSeries'][5]['main_net_flow'] == 50
    assert len(after['cumulativeSeries']) == 30 and len(after['conflictingTimes']) == 1
    archive.save(fund, late, observed_at=late)  # seeing original again does not erase known conflict
    assert len(archive.read('600001', END.date(), 'eastmoney', 'eastmoney-v1', as_of=late)['cumulativeSeries']) == 30


def test_archive_separates_measurements_and_excludes_future_or_late_received_data(tmp_path):
    archive = FundFlowArchive(tmp_path)
    fund, _ = evidence()
    future = {'end': (END+timedelta(minutes=1)).isoformat(), 'main_net_flow': 999}
    fund['cumulativeSeries'].append(future)
    archive.save(fund, END, observed_at=END+timedelta(minutes=1))
    assert archive.read('600001', END.date(), 'eastmoney', 'eastmoney-v1', as_of=END)['cumulativeSeries'] == []
    saved = archive.read('600001', END.date(), 'eastmoney', 'eastmoney-v1', as_of=END+timedelta(minutes=1))
    assert len(saved['cumulativeSeries']) == 31
    assert archive.read('600001', END.date(), 'tencent', 'tencent-v1')['cumulativeSeries'] == []


def test_archive_parser_conflicts_remain_quarantined_on_later_clean_reply(tmp_path):
    archive = FundFlowArchive(tmp_path)
    fund, _ = evidence()
    duplicate = deepcopy(fund['cumulativeSeries'][5]); duplicate['main_net_flow'] += 1
    fund['cumulativeSeries'].append(duplicate)
    result = archive.save(fund, END, observed_at=END)
    assert result['conflicts'] == 1
    original, _ = evidence()
    archive.save(original, END, observed_at=END)
    saved = archive.read('600001', END.date(), 'eastmoney', 'eastmoney-v1', as_of=END)
    assert len(saved['cumulativeSeries']) == 30 and len(saved['conflictingTimes']) == 1
    with archive._connect() as db:
        assert db.execute('SELECT count(*) FROM fund_revisions').fetchone()[0] == 2


@pytest.mark.parametrize('cached', [False, True])
def test_peer_minute_callback_uses_long_windows_once_per_constituent(tmp_path, monkeypatch, cached):
    from src.services.yao_scout import sector_fund_evidence as mod
    members = ['600001', '600002', '600003', '600004']
    bars = [{'end': (END-timedelta(minutes=i)).isoformat(), 'open': 10., 'close': 10.,
             'high': 10.1, 'low': 9.9, 'volume_shares': 100., 'amount_cny': 1000.,
             'source': 'interval-fixture'} for i in range(61)]
    emitted, requested, reads = {}, [], []
    class Archive:
        def __init__(self, *args): pass
        def read_recent(self, code, cutoff, **kwargs):
            reads.append(kwargs['minutes'])
            return bars
        def save(self, *args): pass
    def fetch(code, count):
        requested.append(count)
        return bars
    monkeypatch.setattr(mod, '_recent_sector_bars', {})
    monkeypatch.setattr(mod, 'MinuteArchive', Archive)
    monkeypatch.setattr(mod, 'fetch_software_bars', lambda *a, **k: pytest.fail('cache must satisfy window'))
    result = mod.fetch_sector_batch([members[0]],
        {members[0]: {'asOf': END.isoformat(), 'metrics': {'speed_5m_pct': 0.}}}, END, tmp_path,
        prepared=({members[0]: {'code': 'industry'}}, {'industry': members}),
        bar_fetcher=None if cached else fetch, timeout=2,
        on_minutes=lambda code, packet: emitted.setdefault(code, []).append(packet))
    assert set(emitted) == set(members)
    assert all(len(packets) == 1 for packets in emitted.values())
    assert all(packets[0]['code'] == code and packets[0]['asOf'] == END.isoformat() for code, packets in emitted.items())
    assert result[members[0]]['peerCount'] == 3
    assert (reads if cached else requested) == [65]*4


def test_flow_peer_collection_follows_fund_industry_order_instead_of_size_rotation(tmp_path):
    from src.services.yao_scout import sector_fund_evidence as mod
    first = [f'60000{i}' for i in range(1, 9)]
    second = [f'00000{i}' for i in range(1, 5)]
    bars = [{'end': (END-timedelta(minutes=i)).isoformat(), 'open': 10., 'close': 10.,
             'high': 10.1, 'low': 9.9, 'volume_shares': 100., 'amount_cny': 1000.,
             'source': 'interval-fixture'} for i in range(61)]
    requested = []
    def fetch(code, count):
        requested.append(code)
        return bars
    result = mod.fetch_sector_batch([first[0], second[0]],
        {code: {'asOf': END.isoformat(), 'metrics': {'speed_5m_pct': 0.}} for code in (first[0], second[0])},
        END, tmp_path, prepared=({first[0]: {'code': 'large'}, second[0]: {'code': 'small'}},
            {'large': first, 'small': second}), bar_fetcher=fetch, timeout=3, on_minutes=lambda *a: None)
    assert set(requested[:8]) == set(first)
    assert result[first[0]]['fetchBudget']['industryOrder'] == ['large', 'small']
    assert result[first[0]]['fetchBudget']['maximumIndustryWorkers'] == 8
