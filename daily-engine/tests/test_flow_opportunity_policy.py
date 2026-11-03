"""Current strategy acceptance; offline fixtures, no scans or network providers."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.services.yao_scout.opportunity_channels import VERSION, build_opportunity_channels
from src.services.yao_scout.flow_opportunity_policy import policy_contract
from src.services.yao_scout.trend_evidence import compute_trend_evidence


NOW = datetime(2026, 11, 3, 10, 30, 15, tzinfo=ZoneInfo('Asia/Shanghai'))
CUTOFF = NOW.replace(second=0)


def inputs(code='600001', *, prices=None, flow_delta=1000., sector='A', scale=1., now=NOW):
    cutoff = now.replace(second=0)
    prices = prices or [10 + .004 * i for i in range(31)]
    stamps = [cutoff - timedelta(minutes=len(prices)-1-i) for i in range(len(prices))]
    bars = {at: {'close': price, 'low': price-.01, 'amount_cny': 100000.*scale}
            for at, price in zip(stamps, prices)}
    price = prices[-1]
    minute = {'code': code, 'source': 'fixture_minutes', 'asOf': cutoff.isoformat(),
        'sourceTimePrecision': 'bar_end', 'currentPrice': price, 'historyDays': 0,
        'dataLineage': {'status': 'verified'},
        'trendEvidence': compute_trend_evidence(bars, cutoff, {at: 10 for at in bars}),
        'v11Inputs': {'r1_pct': -.01, 'r3_pct': -.02, 'r5_pct': -.03, 'a1_pct_per_min': -1., 'a3_pct_per_min': -1.},
        'flowAmountWindows': {'30': {'amountCny': 3000000.*scale,
            'start': (cutoff-timedelta(minutes=30)).isoformat(), 'end': cutoff.isoformat(),
            'barCount': 30, 'expectedBarCount': 30, 'source': 'fixture_minutes'}}}
    fund = {'code': code, 'provider': 'fixture_provider', 'measurementVersion': 'main-order-v1',
        'source': 'fixture_funds', 'amountUnit': 'CNY', 'normalizedTimeSemantics': 'bar_end',
        'asOf': cutoff.isoformat(), 'cumulativeSeries': [
            {'end': (cutoff-timedelta(minutes=30-i)).isoformat(), 'main_net_flow': i*flow_delta*scale}
            for i in range(31)]}
    row = {'code': code, 'name': '测试', 'fundEvidence': fund, 'dataLineage': {'status': 'verified'},
        'quote': {'code': code, 'price': price, 'pre_close': 10., 'open_price': 10.,
            'high': max(prices)+.2, 'low': min(prices)-.2, 'change_pct': (price/10-1)*100,
            'source': 'fixture_quote', 'source_time': now.isoformat(),
            'order_book': {'source': 'fixture_quote', 'source_time': now.isoformat(),
                'asks': [{'price': price+.01, 'volume_shares': 10000}],
                'bids': [{'price': price-.01, 'volume_shares': 10000}]}},
        'prediction': {'status': 'rules_unvalidated'}}
    from src.services.yao_scout.premarket import previous_session
    previous = previous_session(now).isoformat()
    daily = {'source': 'fixture_daily', 'asOf': previous,
        'previousSession': {'asOf': previous, 'priceBasis': 'unadjusted'},
        'metrics': {'atr14': .2, 'last_close': 10.}}
    sector_packet = {'code': code, 'status': 'observed', 'asOf': cutoff.isoformat(),
        'windowStart': (cutoff-timedelta(minutes=30)).isoformat(), 'horizonMinutes': 30,
        'source': 'fixture_funds', 'provider': 'fixture_provider', 'measurementVersion': 'main-order-v1',
        'sector': {'code': sector, 'name': sector, 'provider': 'fixture_membership'},
        'coverage': {'totalPeers': 10, 'usablePeers': 8, 'fraction': .8, 'excludedSelf': True},
        'metrics': {'netFlowCny': 80000., 'turnoverCny': 8000000., 'netFlowRatio': .01,
                    'positiveMemberFraction': .75, 'top1PositiveContribution': .2}, 'reasons': []}
    return row, minute, daily, sector_packet


def build(*packets, now=NOW, **kwargs):
    packets = packets or [inputs()]
    return build_opportunity_channels([x[0] for x in packets], now,
        minute_packets={x[0]['code']: x[1] for x in packets},
        daily_packets={x[0]['code']: x[2] for x in packets},
        sector_flow_packets={x[0]['code']: x[3] for x in packets}, **kwargs)


def test_slow_trend_qualifies_without_short_window_rise_acceleration_or_sixty_minutes():
    row = build()['recommendations'][0]
    opportunity = row['opportunity']
    assert opportunity['version'] == VERSION == 'opportunity-flow-20261103-v1'
    assert opportunity['grade'] == 'F-A'
    assert opportunity['selectionMode'] == 'flow_trend'
    assert opportunity['metrics']['r3_pct'] < 0
    assert opportunity['metrics']['pathEfficiency60m'] is None
    assert opportunity['rankingComposite'] == pytest.approx(.5)
    assert opportunity['rankScore'] is None and opportunity['probability'] is None
    assert not row['executable'] and not row['trainingEligible'] and not row['formalEligible']


def test_normal_short_pullback_does_not_destroy_longer_supported_trend():
    prices = [10 + .006*i for i in range(26)] + [10.15-.002*i for i in range(1, 6)]
    row = build(inputs(prices=prices))['recommendations'][0]
    assert row['opportunity']['selectionMode'] == 'flow_pullback'
    assert row['opportunity']['metrics']['pullbackToRise30m'] > 0


@pytest.mark.parametrize('which', ['stock', 'sector', 'lineage', 'atr', 'path', 'book', 'coverage', 'self', 'source', 'basis'])
def test_unknown_required_evidence_is_observation_never_replaced_with_catalyst_or_price_spike(which):
    row, minute, daily, sector = inputs()
    if which == 'stock': row['fundEvidence'] = {}
    elif which == 'sector': sector['status'] = 'unavailable'
    elif which == 'lineage': row['dataLineage'] = minute['dataLineage'] = {'status': 'unknown'}
    elif which == 'atr': daily['metrics']['atr14'] = None
    elif which == 'path': minute['trendEvidence']['metrics']['net_return_30m_pct'] = None
    elif which == 'book': row['quote'].pop('order_book')
    elif which == 'coverage': sector['coverage'].update(usablePeers=7, fraction=.7)
    elif which == 'self': sector['coverage']['excludedSelf'] = False
    elif which == 'source': sector['provider'] = 'other_provider'
    elif which == 'basis': daily['previousSession']['priceBasis'] = 'qfq'
    minute['v11Inputs'].update(r1_pct=8, r3_pct=10, a1_pct_per_min=99)
    event = {'source': 'fixture_event', 'publishedAt': (NOW-timedelta(hours=1)).isoformat(),
        'firstSeenAt': (NOW-timedelta(minutes=5)).isoformat(), 'availableAt': (NOW-timedelta(minutes=4)).isoformat(),
        'direction': 'positive', 'title': 'Positive event'}
    result = build((row, minute, daily, sector), contexts={row['code']: {'catalysts': [event]}})
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['grade'] == 'B'


@pytest.mark.parametrize('bad', ['stale', 'future', 'wrong_code', 'range', 'tainted', 'no_asks', 'locked', 'future_minute'])
def test_existing_quote_identity_time_book_and_lineage_guards_remain_hard(bad):
    row, minute, daily, sector = inputs()
    if bad == 'stale': row['quote']['source_time'] = (NOW-timedelta(seconds=31)).isoformat()
    elif bad == 'future': row['quote']['source_time'] = (NOW+timedelta(seconds=1)).isoformat()
    elif bad == 'wrong_code': row['quote']['code'] = '600999'
    elif bad == 'range': row['quote']['high'] = 9.
    elif bad == 'tainted': minute['dataLineage']['status'] = 'tainted'
    elif bad == 'no_asks': row['quote']['order_book']['asks'] = []
    elif bad == 'locked': row['quote']['locked_limit_up'] = True
    elif bad == 'future_minute': minute['asOf'] = (NOW+timedelta(minutes=1)).isoformat()
    result = build((row, minute, daily, sector))
    assert not result['recommendations']
    assert result['observations'][0]['opportunity']['grade'] == 'C'


def test_verified_major_adverse_event_still_vetoes_and_future_event_does_not():
    event = {'source': 'fixture_event', 'publishedAt': (NOW-timedelta(hours=1)).isoformat(),
        'firstSeenAt': (NOW-timedelta(minutes=5)).isoformat(), 'availableAt': (NOW-timedelta(minutes=4)).isoformat(),
        'severity': 'major', 'title': 'Major adverse event'}
    result = build(contexts={'600001': {'majorNegativeEvents': [event]}})
    assert not result['recommendations'] and result['observations'][0]['opportunity']['grade'] == 'C'
    event['availableAt'] = (NOW+timedelta(seconds=1)).isoformat()
    assert build(contexts={'600001': {'majorNegativeEvents': [event]}})['recommendations']


def test_recent_five_minute_outflow_is_a_warning_not_a_veto_on_supported_thirty_minute_trend():
    row, minute, daily, sector = inputs()
    for i in range(26, 31):
        row['fundEvidence']['cumulativeSeries'][i]['main_net_flow'] = 25000 - 1000*(i-25)
    result = build((row, minute, daily, sector))
    assert result['recommendations']
    opportunity = result['recommendations'][0]['opportunity']
    assert opportunity['flowEvidence']['stock']['metrics']['netFlowCny'] > 0
    assert opportunity['flowEvidence']['stock']['metrics']['recentOutflow'] is True
    assert not opportunity['failedConditions']
    assert any('仅作短期减弱提示' in text for text in opportunity['counterEvidence'])


def test_sector_midrank_is_bounded_and_order_invariant_for_different_self_excluded_values():
    packets = [inputs('600001', sector='A'), inputs('600002', sector='A'), inputs('600003', sector='B')]
    for packet, ratio in zip(packets, (.01, .09, .03)):
        packet[3]['metrics'].update(netFlowRatio=ratio, netFlowCny=ratio*8000000.)
    forward, reverse = build(*packets), build(*reversed(packets))
    summarize = lambda result: {row['code']: row['opportunity']['rankingComponents'] for row in result['researchCandidates']}
    assert summarize(forward) == summarize(reverse)
    parts = summarize(forward)
    assert parts['600001']['sectorNetFlowRatio']['rankValue'] == pytest.approx(.05)
    assert parts['600002']['sectorNetFlowRatio']['rankValue'] == pytest.approx(.05)
    assert parts['600001']['sectorNetFlowRatio']['rawValue'] == .01
    assert all(0 <= component['percentile'] <= 1 for components in parts.values() for component in components.values())


@pytest.mark.parametrize('kind', ['negative_stock', 'negative_sector', 'giveback', 'distant_entry', 'wide_spread', 'stale_daily'])
def test_observed_adverse_or_excessive_entry_risk_is_not_missing_evidence(kind):
    row, minute, daily, sector = inputs(flow_delta=-1000 if kind == 'negative_stock' else 1000)
    if kind == 'negative_sector': sector['metrics'].update(netFlowCny=-80000, netFlowRatio=-.01)
    elif kind == 'giveback': minute['trendEvidence']['metrics']['close_pullback_to_rise_30m'] = .6
    elif kind == 'distant_entry': daily['metrics']['atr14'] = .01
    elif kind == 'wide_spread': row['quote']['order_book']['asks'][0]['price'] += .1
    elif kind == 'stale_daily': daily['asOf'] = '2026-10-30'
    result = build((row, minute, daily, sector))
    assert not result['recommendations']
    assert result['observations'][0]['opportunity']['grade'] == 'B'


def test_first_place_is_not_100_and_turnover_scale_or_short_speed_cannot_change_ranking():
    a, b = inputs('600001'), inputs('600002', scale=100.)
    b[1]['v11Inputs'].update(r1_pct=9, r3_pct=10, a1_pct_per_min=99)
    result = build(b, a)
    assert [r['code'] for r in result['recommendations']] == ['600001', '600002']
    assert all(r['opportunity']['rankingComposite'] == .5 for r in result['recommendations'])
    assert all(r['opportunity']['rankScore'] is None for r in result['recommendations'])
    assert all(r['opportunity']['rankingComponents']['sectorNetFlowRatio']['peerCount'] == 1 for r in result['recommendations'])


def test_sector_limit_keeps_diversification_without_filling_a_five_stock_quota():
    result = build(*(inputs(f'60000{i}') for i in range(1, 6)))
    assert result['diagnostics']['qualifiedCount'] == 5
    assert len(result['recommendations']) == 2
    assert all(r['opportunity']['selectionExclusion'] == 'sector_concentration_limit' for r in result['observations'])


def test_sixty_minute_strength_does_not_rescue_bad_fixed_thirty_minute_window():
    packet = inputs()
    packet[1]['trendEvidence']['metrics'].update(signed_log_efficiency_30m=-.2,
        net_return_30m_pct=-.1, signed_log_efficiency_60m=1.)
    assert build(packet)['recommendations'] == []


def test_early_incomplete_window_and_lunch_cannot_be_manufactured():
    packet = inputs(prices=[10 + .004*i for i in range(15)])
    result = build(packet)
    assert not result['recommendations']
    assert 'thirty_minute_path_unavailable' in result['observations'][0]['opportunity']['failedConditions']


def test_old_version_first_detection_does_not_rewrite_current_reference_or_input():
    packet = inputs()
    previous = {'recommendations': [{'code': '600001', 'opportunity': {
        'version': 'opportunity-em-20261029-v2', 'grade': 'E-A',
        'firstDetectionAt': (NOW-timedelta(minutes=20)).isoformat(),
        'detectedAt': (NOW-timedelta(minutes=10)).isoformat(), 'firstDetectionPrice': 1.}}]}
    original = deepcopy((packet, previous))
    result = build(packet, previous=previous)
    assert (packet, previous) == original
    assert result['recommendations'][0]['opportunity']['firstDetectionAt'] == NOW.isoformat()
    assert result['recommendations'][0]['opportunity']['firstDetectionPrice'] == packet[0]['quote']['price']


def test_policy_publishes_constraints_weights_and_non_probability_semantics():
    contract = policy_contract()
    assert contract['parameters']['windowMinutes'] == 30
    assert contract['parameters']['recentFlowSubwindowMinutes'] == 5
    assert sum(p['weight'] for p in contract['rankingDimensions'].values()) == 1
    assert 'uncalibrated' in contract['thresholdSource']
    assert contract['probability'] is None
