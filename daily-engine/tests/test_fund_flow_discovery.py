from datetime import datetime, timedelta
from time import monotonic

import pandas as pd
import pytest

from src.services.yao_scout import fund_flow_discovery as flow
from src.services.yao_scout.recommendation_evidence import research_queue

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=flow.SHANGHAI)


def cohort(count=5, values=None):
    codes = [f'600{i:03}' for i in range(1, count + 1)]
    members = {code: {'code': 'EMXG:example', 'name': 'example', 'membershipComplete': True,
                     'membershipCount': count, 'membershipSource': 'eastmoney:xuangu-unfiltered',
                     'classification': 'xuangu-industry-v2'} for code in codes}
    rows = [{'code': code, 'name': code, 'main_net_flow': (values or list(range(1, count + 1)))[i],
             'amount_cny': 100., 'source': flow.SOURCE, 'source_time': NOW.isoformat()}
            for i, code in enumerate(codes)]
    return rows, members, {'EMXG:example': codes}


def test_full_denominator_normalized_flow_breadth_and_concentration_are_auditable():
    rows, members, peers = cohort(values=[-1, 2, 3, 4, 5])
    result = flow.build_flow_discovery(rows, members, peers, NOW)
    assert set(result) == {r['code'] for r in rows[1:]}
    item = result['600005']
    sector = item['sector']
    assert item['stock']['netFlowRatio'] == .05
    assert sector['mainNetFlowCny'] == 8
    assert sector['netFlowRatio'] == 8 / 400
    assert sector['catalogueMemberCount'] == 5
    assert sector['memberCount'] == sector['observedCount'] == 4
    assert sector['excludedSelf'] is True
    assert sector['coveragePct'] == 100
    assert sector['positiveBreadthPct'] == 75
    assert sector['topPositiveSharePct'] == pytest.approx(100 * 4 / 9)
    assert sector['positiveFlowHhi'] == pytest.approx(sum((x / 9) ** 2 for x in [2, 3, 4]))
    assert item['researchOnly'] and item['discoveryOnly']
    assert '近30分钟持续流入' in item['meaning']
    assert not any(key in item for key in ('selected', 'probability', 'entryEligible'))


@pytest.mark.parametrize('bad', ['missing_time', 'stale', 'future', 'naive', 'other_day', 'bad_amount', 'nan', 'impossible_net'])
def test_bad_rows_are_missing_coverage_not_zero_or_redated_to_checked_at(bad):
    rows, members, peers = cohort(6)
    bad_row = rows[0]
    bad_row['checked_at'] = NOW.isoformat()
    updates = {
        'missing_time': {'source_time': None},
        'stale': {'source_time': (NOW - timedelta(seconds=31)).isoformat()},
        'future': {'source_time': (NOW + timedelta(seconds=1)).isoformat()},
        'naive': {'source_time': NOW.replace(tzinfo=None).isoformat()},
        'other_day': {'source_time': (NOW - timedelta(days=1)).isoformat()},
        'bad_amount': {'amount_cny': 0}, 'nan': {'main_net_flow': float('nan')},
        'impossible_net': {'main_net_flow': 101},
    }
    bad_row.update(updates[bad])
    result = flow.build_flow_discovery(rows, members, peers, NOW)
    assert '600001' not in result
    sector = result['600002']['sector']
    assert sector['memberCount'] == 5 and sector['observedCount'] == 4
    assert sector['coveragePct'] == 80
    assert sector['missingCodes'] == ['600001']


def test_missing_majority_never_becomes_small_fake_sector_and_requires_three_observations():
    rows, members, peers = cohort(100)
    assert flow.build_flow_discovery(rows[:10], members, peers, NOW) == {}
    rows, members, peers = cohort(3)
    assert flow.build_flow_discovery(rows, members, peers, NOW) == {}
    rows, members, peers = cohort(5)
    assert flow.build_flow_discovery(rows[:3], members, peers, NOW) == {}


@pytest.mark.parametrize('bad', ['incomplete', 'count', 'missing_proof', 'duplicate_peer', 'nonmember'])
def test_membership_needs_complete_real_denominator(bad):
    rows, members, peers = cohort()
    for member in members.values():
        if bad == 'incomplete':
            member['membershipComplete'] = False
        elif bad == 'count':
            member['membershipCount'] = 100
        elif bad == 'missing_proof':
            member.pop('membershipComplete')
    if bad == 'duplicate_peer':
        peers['EMXG:example'][-1] = peers['EMXG:example'][0]
    if bad == 'nonmember':
        peers['EMXG:example'] = [f'600{i:03}' for i in range(20, 25)]
    assert flow.build_flow_discovery(rows, members, peers, NOW) == {}


def test_mixed_provider_snapshots_are_not_summed():
    rows, members, peers = cohort()
    rows[0]['source'] = 'tencent:large-order'
    assert flow.build_flow_discovery(rows, members, peers, NOW) == {}


def test_conflicting_duplicate_stock_is_excluded_from_denominator_coverage():
    rows, members, peers = cohort(6)
    rows.append({**rows[0], 'main_net_flow': 90})
    result = flow.build_flow_discovery(rows, members, peers, NOW)
    assert '600001' not in result
    assert result['600002']['sector']['coveragePct'] == 80
    assert result['600002']['sector']['mainNetFlowCny'] == 18


def test_candidate_cannot_supply_its_own_positive_sector_or_peer_source_time():
    rows, members, peers = cohort(4, values=[100, -5, -5, -5])
    assert flow.build_flow_discovery(rows, members, peers, NOW) == {}
    rows, members, peers = cohort(4)
    rows[0]['source_time'] = (NOW - timedelta(seconds=20)).isoformat()
    result = flow.build_flow_discovery(rows, members, peers, NOW)
    assert result['600001']['sourceTime'] == rows[0]['source_time']
    assert result['600001']['sector']['sourceTimeRange'] == [NOW.isoformat(), NOW.isoformat()]
    assert result['600001']['sector']['mainNetFlowCny'] == 9
    assert result['600002']['sector']['sourceTimeRange'][0] == rows[0]['source_time']


def test_positive_stock_without_positive_sector_is_not_a_flow_discovery():
    rows, members, peers = cohort(values=[1, -10, -10, -10, -10])
    assert flow.build_flow_discovery(rows, members, peers, NOW) == {}


def research_frame():
    return pd.DataFrame([{'code': f'600{i:03}', 'name': str(i), 'amount': (10-i)*1e8,
        'source': 'sina', 'source_time': NOW.isoformat()} for i in range(1, 9)])


def test_flow_entrance_reaches_low_activity_stocks_without_expanding_budget_or_granting_entry():
    rows, members, peers = cohort(8, values=[-1] * 4 + [2, 3, 4, 5])
    discovery = flow.build_flow_discovery(rows, members, peers, NOW)
    frame = research_frame()
    legacy = research_queue(frame, 4, now=NOW)
    result = research_queue(frame, 4, now=NOW, flow_discovery=discovery)
    assert [r['code'] for r in legacy] == ['600001', '600002', '600003', '600004']
    assert [r['code'] for r in result] == ['600008', '600007', '600001', '600002']
    assert len(result) == len(legacy) == 4
    assert sum(r['discoveryEvidence']['allocatedBy'] == 'fund_flow' for r in result) == 2
    for row in result:
        assert row['discoveryEvidence']['fundFlowBudget'] == 2
        assert not any(k in row for k in ('selected', 'probability', 'entryEligible'))
    assert result[0]['discoveryEvidence']['fundFlowDiscovery'] == discovery['600008']


def test_queue_rechecks_flow_freshness_and_absent_flow_preserves_exact_old_queue():
    rows, members, peers = cohort(8)
    discovery = flow.build_flow_discovery(rows, members, peers, NOW)
    frame = research_frame()
    for entry in discovery.values():
        entry['sourceTime'] = (NOW - timedelta(seconds=31)).isoformat()
    legacy = research_queue(frame, 4, now=NOW)
    assert research_queue(frame, 4, now=NOW, flow_discovery=discovery) == legacy
    assert research_queue(frame, 4, now=NOW, flow_discovery={}) == legacy
    assert research_queue(frame, 4, flow_discovery=discovery) == research_queue(frame, 4)


def test_flow_queue_prefers_normalized_intensity_not_absolute_money_and_rotates_sectors():
    rows, members, peers = cohort(8, values=[1] * 8)
    rows[-1].update(main_net_flow=10, amount_cny=100)
    rows[-2].update(main_net_flow=100, amount_cny=10000)
    discovery = flow.build_flow_discovery(rows, members, peers, NOW)
    result = research_queue(research_frame(), 2, now=NOW, flow_discovery=discovery)
    assert result[0]['code'] == '600008'
    other = 'EMXG:other'
    peers[other] = peers['EMXG:example'][4:]
    peers['EMXG:example'] = peers['EMXG:example'][:4]
    for i, member in enumerate(members.values()):
        member['membershipCount'] = 4
        if i >= 4:
            member['code'] = other
    discovery = flow.build_flow_discovery(rows, members, peers, NOW)
    result = research_queue(research_frame(), 4, now=NOW, flow_discovery=discovery)
    boards = [r['discoveryEvidence']['fundFlowDiscovery']['sector']['code'] for r in result[:2]]
    assert len(set(boards)) == 2


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {'rc': 0, 'data': self.data}


def provider_rows(n, now):
    return [{'f12': f'000{i:03}', 'f14': str(i), 'f62': i, 'f6': 1e8,
             'f124': int(now.timestamp())} for i in range(1, n + 1)]


def test_fetch_paginates_all_rows_and_keeps_provider_epoch_and_missing_fields():
    now = datetime.now(flow.SHANGHAI)
    raw = provider_rows(101, now)
    raw[0].update(f124=None, f62='-')
    calls = []

    def get(url, *, params, **kwargs):
        calls.append((url, params))
        page = int(params['pn'])
        return Response({'total': len(raw), 'diff': raw[(page-1)*100:page*100]})

    packet = flow.fetch_flow_snapshot(now, monotonic() + 5, getter=get)
    assert packet['status'] == 'observed'
    assert packet['coverage'] == {'expectedCount': 101, 'receivedCount': 101, 'validCount': 100, 'complete': True}
    assert packet['rows'][0]['source_time'] is None
    assert packet['rows'][0]['main_net_flow'] is None
    assert packet['rows'][1]['source_time'] == datetime.fromtimestamp(raw[1]['f124'], flow.SHANGHAI).isoformat()
    assert [c[1]['pn'] for c in calls] == ['1', '2']
    assert all(c[1]['fields'] == 'f12,f14,f62,f6,f124' and c[1]['fid'] == 'f12' for c in calls)


@pytest.mark.parametrize('broken', ['duplicate', 'changing_total', 'short_page', 'failed_page'])
def test_partial_or_inconsistent_pages_never_reach_discovery(broken):
    now = datetime.now(flow.SHANGHAI)
    raw = provider_rows(101, now)

    def get(url, *, params, **kwargs):
        page = int(params['pn'])
        if page == 1:
            return Response({'total': 101, 'diff': raw[:100]})
        if broken == 'failed_page':
            raise TimeoutError('fixture')
        return Response({'total': 102 if broken == 'changing_total' else 101,
            'diff': [] if broken == 'short_page' else raw[:1] if broken == 'duplicate' else raw[100:]})

    packet = flow.fetch_flow_snapshot(now, monotonic() + 5, getter=get)
    assert packet['status'] == 'unavailable'
    assert packet['rows'] == []
    assert packet['coverage']['complete'] is False
    assert packet['reasonCodes']


def test_exhausted_deadline_never_starts_network_and_failures_have_finite_attempts():
    calls = []

    def fail(*args, **kwargs):
        calls.append(args[0])
        raise TimeoutError('fixture')

    assert flow.fetch_flow_snapshot(NOW, monotonic() - 1, getter=fail)['status'] == 'unavailable'
    assert calls == []
    result = flow.fetch_flow_snapshot(NOW, monotonic() + 5, getter=fail)
    assert result['status'] == 'unavailable' and result['rows'] == []
    assert len(calls) == 2
