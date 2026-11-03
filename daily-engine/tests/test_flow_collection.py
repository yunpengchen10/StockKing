"""Exact flow denominators and source-independent collection priorities."""
from datetime import datetime, timedelta

import pytest

from src.services.yao_scout.flow_collection import flow_collection_order
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
