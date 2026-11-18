"""Point-in-time entry checks shared by formal picks and research references."""
from copy import deepcopy
from datetime import timedelta

import pytest

from src.services.yao_scout.limit_entry import assess_limit_entry
from src.services.yao_scout.primary_policy import evaluate_primary_admission
from tests.primary_fixture import make_primary_evidence
from tests.test_flow_opportunity_policy import NOW, inputs
from tests.test_theme_opportunities import build, theme_board


def quote_fixture():
    return {
        'code': '600001', 'source': 'fixture_price', 'source_time': NOW.isoformat(),
        'price': 10.45, 'pre_close': 10, 'high': 10.48, 'limit_up': 10.50,
        'order_book': {'source': 'fixture_price', 'source_time': NOW.isoformat(),
                       'asks': [{'price': 10.46, 'volume_shares': 500}]},
    }


def test_sell_offer_and_provider_limits_keep_independent_provenance():
    quote = quote_fixture()
    earlier = (NOW - timedelta(seconds=5)).isoformat()
    quote['price_limit_evidence'] = {
        'method': 'explicit_provider_fields', 'status': 'partial', 'source_time': earlier,
        'source': 'fixture_limit', 'previous_close': 10, 'limit_up': 10.5,
    }
    quote['order_book'].update(source='fixture_book', source_time=earlier)
    saved = deepcopy(quote)
    result = assess_limit_entry('600001', quote, NOW)
    assert quote == saved and result['status'] == 'pre_limit' and result['eligible']
    assert (result['price'], result['sessionHigh'], result['limitUp']) == (10.45, 10.48, 10.50)
    assert (result['askPrice'], result['askVolumeShares']) == (10.46, 500)
    assert (result['source'], result['sourceTime']) == ('fixture_price', NOW.isoformat())
    assert (result['limitSource'], result['limitSourceTime']) == ('fixture_limit', earlier)
    assert (result['bookSource'], result['bookSourceTime']) == ('fixture_book', earlier)


@pytest.mark.parametrize('missing', [
    'missing_upper', 'no_limit', 'missing_high', 'wrong_stock', 'bad_high', 'future',
    'stale_price', 'stale_book', 'future_fetch', 'future_book_fetch', 'no_asks',
    'no_volume', 'crossed', 'invalid_book', 'book_price_mismatch', 'ask_at_limit',
    'bad_limit_provenance', 'stale_limit_source', 'cross_source_price_conflict',
])
def test_unverified_quotes_never_pass(missing):
    quote = quote_fixture()
    book = quote['order_book']
    if missing == 'missing_upper': quote.pop('limit_up')
    elif missing == 'no_limit': quote['no_price_limit'] = True
    elif missing == 'missing_high': quote.pop('high')
    elif missing == 'wrong_stock': quote['code'] = '600999'
    elif missing == 'bad_high': quote['high'] = 10.40
    elif missing == 'future': quote['source_time'] = (NOW + timedelta(seconds=1)).isoformat()
    elif missing == 'stale_price': quote['source_time'] = (NOW - timedelta(seconds=31)).isoformat()
    elif missing == 'stale_book': book['source_time'] = (NOW - timedelta(seconds=31)).isoformat()
    elif missing == 'future_fetch': quote['fetched_at'] = (NOW + timedelta(seconds=1)).isoformat()
    elif missing == 'future_book_fetch': book['fetched_at'] = (NOW + timedelta(seconds=1)).isoformat()
    elif missing == 'no_asks': book['asks'] = []
    elif missing == 'no_volume': book['asks'][0]['volume_shares'] = 0
    elif missing == 'crossed': book['bids'] = [{'price': 10.47, 'volume_shares': 100}]
    elif missing == 'invalid_book': quote['order_book_valid'] = False
    elif missing == 'book_price_mismatch': book['observed_quote_price'] = 10.40
    elif missing == 'ask_at_limit': book['asks'][0]['price'] = 10.50
    elif missing == 'cross_source_price_conflict': quote['issues'] = [missing]
    else:
        quote['price_limit_evidence'] = {
            'method': 'explicit_provider_fields', 'status': 'observed', 'source': 'fixture_limits',
            'source_time': (NOW - timedelta(seconds=31 if missing == 'stale_limit_source' else 0)).isoformat(),
            'previous_close': 9.8 if missing == 'bad_limit_provenance' else 10, 'limit_up': 10.50,
        }
    result = assess_limit_entry('600001', quote, NOW)
    assert not result['eligible'] and result['status'] == 'unavailable'


@pytest.mark.parametrize('case,expected', [
    ('valid', 'pre_limit'), ('ceiling', 'at_limit'), ('reopened', 'touched_limit'),
    ('no_ceiling', 'unavailable'), ('empty_book', 'unavailable'), ('only_limit_ask', 'unavailable'),
])
def test_primary_and_theme_selection_apply_same_gate_before_ranking(case, expected):
    row, minute, daily, sector = inputs()
    quote = row['quote']
    if case == 'ceiling':
        quote.update(limit_up=quote['price'], high=quote['price'], order_book_valid=False,
                     issues=['invalid_order_book'])
        quote['order_book']['asks'] = []
    elif case == 'reopened': quote['high'] = quote['limit_up']
    elif case == 'no_ceiling': quote.pop('limit_up')
    elif case == 'empty_book': quote['order_book']['asks'] = []
    elif case == 'only_limit_ask': quote['order_book']['asks'][0]['price'] = quote['limit_up']
    primary_row = make_primary_evidence({'code': row['code'], 'decision_at': NOW.isoformat()})
    primary_row['quote'].update(quote)
    if case == 'no_ceiling':
        primary_row['quote'].pop('limit_up')
    primary = evaluate_primary_admission(primary_row)
    board = build((row, minute, daily, sector), board=theme_board())
    opportunity = board['researchCandidates'][0]['opportunity']
    assert opportunity['limitEntry']['status'] == primary['limitEntry']['status'] == expected
    assert bool(board['recommendations']) == primary['eligible'] == (case == 'valid')
    if case != 'valid':
        reason = 'pre_limit_entry_' + expected
        assert reason in opportunity['failedConditions']
        assert reason in primary['reasons'] + primary['missingReasons']
