"""Exact flow denominators and source-independent collection priorities."""
from datetime import datetime, timedelta
from copy import deepcopy

import pytest

from src.services.yao_scout.flow_collection import flow_candidate_priority, flow_collection_order
from src.services.yao_scout.minute_history import SHANGHAI, compute_bar_evidence


END = datetime(2026, 11, 3, 10, 31, tzinfo=SHANGHAI)


def bars():
    return [{'end': (END-timedelta(minutes=60-i)).isoformat(),
             'open': 10, 'close': 10, 'low': 9.99, 'high': 10.01,
             'volume_shares': (1000+i)/10, 'amount_cny': 1000 + i, 'source': 'fixture'}
            for i in range(61)]


def test_denominator_is_exact_half_open_interval_and_carries_provenance():
    packet = compute_bar_evidence(bars(), END, expected_dates=[])
    for horizon in (15, 30, 60):
        item = packet['flowAmountWindows'][str(horizon)]
        assert item['amountCny'] == sum(1000+i for i in range(61-horizon, 61))
        assert item['start'] == (END-timedelta(minutes=horizon)).isoformat()
        assert item['end'] == END.isoformat()
        assert item['barCount'] == item['expectedBarCount'] == horizon
        assert item['source'] == 'fixture'


@pytest.mark.parametrize('corruption', ['gap', 'missing_amount', 'mixed_source', 'missing_endpoint', 'future_only'])
def test_incomplete_or_mixed_windows_cannot_supply_flow_denominator(corruption):
    rows = bars()
    if corruption == 'gap':
        del rows[-10]
    elif corruption == 'missing_amount':
        rows[-10]['amount_cny'] = None
    elif corruption == 'mixed_source':
        rows[-10]['source'] = 'another_provider'
    elif corruption == 'missing_endpoint':
        del rows[-31]
    else:
        rows = [{**r, 'end': (datetime.fromisoformat(r['end'])+timedelta(days=1)).isoformat()} for r in rows]
    assert '30' not in compute_bar_evidence(rows, END, expected_dates=[])['flowAmountWindows']


def test_lunch_does_not_stitch_into_contiguous_flow_window():
    end = END.replace(hour=13, minute=15)
    rows = [{**row, 'end': (end-timedelta(minutes=60-i)).isoformat()} for i, row in enumerate(bars())]
    assert '30' not in compute_bar_evidence(rows, end, expected_dates=[])['flowAmountWindows']


def test_collection_finishes_true_sector_members_and_never_uses_price_speed():
    membership = {'600001': {'code': 'S1'}, '600003': {'code': 'S2'}, '600002': {'code': 'S1'}}
    peers = {'S1': ['600002', '600001', '300999'], 'S2': ['600003', '688001']}
    assert flow_collection_order(['600001', '600003', '600002'], membership, peers) == [
        '600001', '300999', '600002', '600003', '688001']
    assert flow_collection_order(['600001'], {}, {}) == ['600001']


def minute_packet(code, *, end=END):
    packet = compute_bar_evidence(bars(), end, expected_dates=[])
    packet['code'] = code
    return packet


def test_complete_declining_candidates_precede_missing_windows_without_changing_pool_or_rank():
    codes = ['600001', '600002', '600003', '600004', '600005']
    packets = {code: minute_packet(code) for code in codes}
    packets['600001'].pop('flowAmountWindows')
    packets['600001']['metrics']['speed_5m_pct'] = 20
    packets['600003'] = {'code': '600003', 'asOf': END.isoformat(), 'metrics': {'speed_5m_pct': 30}}
    packets['600002']['metrics'].update(speed_5m_pct=-5, net_return_30m_pct=-7)
    packets['600002']['fundEvidence'] = {'metrics': {'main_net_flow_30m': -9000000}}
    packets['600004']['metrics'].update(speed_5m_pct=1, net_return_30m_pct=2)
    packets['600005']['flowAmountWindows']['30']['amountCny'] = None
    original = deepcopy((codes, packets))
    prioritized = flow_candidate_priority(codes, packets, END)
    assert prioritized == ['600002', '600004', '600001', '600003', '600005']
    assert (codes, packets) == original
    membership = {code: {'code': 'same-sector'} for code in codes}
    peers = {'same-sector': [*codes, '300999']}
    assert set(flow_collection_order(prioritized, membership, peers)) == set(codes) | {'300999'}


@pytest.mark.parametrize('damage', ['stale', 'previous_day', 'future', 'tainted', 'unknown_source',
    'wrong_identity', 'missing_amount', 'negative_amount', 'zero_amount', 'nan_amount', 'bool_amount',
    'missing_bar', 'mismatched_window', 'lunch_window'])
def test_unverifiable_candidate_never_takes_priority_from_complete_window(damage):
    bad = minute_packet('600001')
    good = minute_packet('600002')
    if damage in {'stale', 'previous_day', 'future'}:
        shift = {'stale': -timedelta(minutes=5), 'previous_day': -timedelta(days=1), 'future': timedelta(minutes=1)}[damage]
        end = END + shift
        bad['asOf'] = end.isoformat()
        bad['flowAmountWindows']['30'].update(start=(end-timedelta(minutes=30)).isoformat(), end=end.isoformat())
    elif damage == 'tainted':
        bad['dataLineage'] = {'status': 'tainted', 'sessions': [{'code': '600001', 'day': END.date().isoformat(), 'status': 'tainted'}]}
    elif damage == 'unknown_source':
        bad['source'] = 'unknown'
    elif damage == 'wrong_identity':
        bad['code'] = '600999'
    elif damage.endswith('_amount'):
        bad['flowAmountWindows']['30']['amountCny'] = {
            'missing_amount': None, 'negative_amount': -100, 'zero_amount': 0,
            'nan_amount': float('nan'), 'bool_amount': True}[damage]
    elif damage == 'missing_bar':
        bad['flowAmountWindows']['30']['barCount'] = 29
    elif damage == 'mismatched_window':
        bad['flowAmountWindows']['30']['end'] = (END-timedelta(minutes=1)).isoformat()
    else:
        end = END.replace(hour=13, minute=15)
        bad['asOf'] = end.isoformat()
        bad['flowAmountWindows']['30'].update(start=(end-timedelta(minutes=30)).isoformat(), end=end.isoformat())
        good = {'code': '600002', 'asOf': end.isoformat(), 'source': 'fixture'}
        # Neither incomplete candidate may be promoted across lunch.
        assert flow_candidate_priority(['600002', '600001'], {'600001': bad, '600002': good}, end) == ['600002', '600001']
        return
    assert flow_candidate_priority(['600001', '600002'], {'600001': bad, '600002': good}, END) == ['600002', '600001']


def test_historical_taint_with_verified_current_window_and_unknown_cutoff():
    ready = minute_packet('600002')
    ready['dataLineage'] = {'status': 'tainted', 'sessions': [
        {'code': '600002', 'day': '2026-11-02', 'status': 'tainted'},
        {'code': '600002', 'day': END.date().isoformat(), 'status': 'verified', 'verifiedThrough': END.isoformat()},
    ]}
    codes = ['600001', '600002']
    assert flow_candidate_priority(codes, {'600002': ready}, END) == ['600002', '600001']
    assert flow_candidate_priority(codes, {'600002': ready}, 'unknown') == codes
    ready['dataLineage']['sessions'][-1]['verifiedThrough'] = (END-timedelta(minutes=1)).isoformat()
    assert flow_candidate_priority(codes, {'600002': ready}, END) == codes
