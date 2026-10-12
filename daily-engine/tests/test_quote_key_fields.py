"""Provider limits retain their own provenance through selection and storage."""
from datetime import timedelta

import pytest

from src.services import public_market_quotes as quotes
from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout.signal_learning import SignalLearningService
from tests.test_public_market_quotes import NOW, both, sina, tencent
from tests.test_signal_learning import _candidate


def test_both_provider_price_limits_are_parsed_without_rule_derivation():
    parsed = quotes.parse_quote_payload(tencent(**{'47': '11', '48': '9'}), 'tencent', ['603936'])
    assert parsed['quotes']['603936']['limit_up'] == 11
    assert parsed['quotes']['603936']['limit_down'] == 9
    absent = quotes.parse_quote_payload(tencent(**{'47': '', '48': 'NaN'}), 'tencent', ['603936'])
    assert 'limit_up' not in absent['quotes']['603936']
    assert 'limit_down' not in absent['quotes']['603936']


def test_newer_sina_price_keeps_available_tencent_limits_with_separate_evidence():
    limits_at = NOW - timedelta(seconds=2)
    row = quotes.get_public_realtime_quote('603936', **both(
        tencent(**{'30': limits_at.strftime('%Y%m%d%H%M%S'), '47': '11', '48': '9'}),
        sina(**{'3': '10.51'})))
    assert row['source'] == 'sina' and row['price'] == 10.51
    assert row['source_time'] == row['provider_timestamp'] == NOW.isoformat()
    assert row['pre_close'] == 10 and row['limit_up'] == 11 and row['limit_down'] == 9
    evidence = row['price_limit_evidence']
    assert evidence['source'] == 'tencent' and evidence['source_time'] == limits_at.isoformat()
    assert evidence['previous_close'] == 10 and evidence['method'] == 'explicit_provider_fields'
    assert evidence['provider_field_indexes'] == {'limit_up': 47, 'limit_down': 48}
    assert 'suspended' not in row and 'corporate_action' not in row and 'no_price_limit' not in row
    assert not row['execution_verified']


@pytest.mark.parametrize('changes', [
    {'30': '20260914145429'},  # older than freshness bound
    {'30': '20260911145500'},  # a prior session
    {'30': '20260914145501'},  # not available at decision time
    {'4': '9.9', '30': '20260914145459'},  # reference close conflicts
])
def test_unavailable_or_incompatible_provider_limits_are_not_spliced(changes):
    row = quotes.get_public_realtime_quote('603936', **both(
        tencent(**{'47': '11', '48': '9', **changes}), sina()))
    assert row['source'] == 'sina'
    assert 'limit_up' not in row and 'limit_down' not in row
    assert row['price_limit_evidence']['status'] == 'unavailable'


def test_own_quote_limits_keep_provenance_and_do_not_relabel_its_order_book():
    row = quotes.get_public_realtime_quote('603936', **both(
        tencent(**{'47': '11', '48': '9'}), ''))
    assert row['price_limit_evidence']['source_time'] == row['source_time']
    assert row['price_limit_evidence']['source'] == row['source'] == 'tencent'
    assert 'order_book_from_separate_observation' not in row['public_quote_metadata']['issues']


def test_go_gateway_adapter_and_immutable_scan_preserve_key_field_provenance(tmp_path, monkeypatch):
    client = SoftwareMarketClient('http://127.0.0.1:9000', 'fixture')
    source_at = NOW - timedelta(seconds=1)
    packet = {'sources': {
        'tencent': {'payload': tencent(**{'30': source_at.strftime('%Y%m%d%H%M%S'), '47': '11', '48': '9'}),
                    'fetched_at': NOW.isoformat()},
        'sina': {'payload': sina(), 'fetched_at': NOW.isoformat()},
    }}
    monkeypatch.setattr(client, '_request', lambda *_: packet)
    batch = client.quotes(['603936'], clock=lambda: NOW)
    quote = quotes.adapt_public_quote(batch, batch['quotes'][0])
    candidate = {**_candidate('603936', NOW.date(), price=10.5), 'quote': quote,
                 'decision_at': NOW.isoformat()}
    service = SignalLearningService(tmp_path, clock=lambda: NOW)
    result = service.persist_scan({'status': 'completed_observations', 'candidates': [candidate]},
                                  '1455', NOW, True)
    saved = service.history()['items'][0]['signals'][0]['snapshot']['quote']
    assert saved == quote
    assert saved['limit_up'] == 11 and saved['limit_down'] == 9
    assert saved['price_limit_evidence']['source_time'] == source_at.isoformat()
    assert saved['provider_timestamp'] == NOW.isoformat()
    # A later result cannot rewrite the original recommendation's quote.
    service.persist_scan({'status': 'completed_observations', 'candidates': []}, '1455', NOW, True)
    assert service.history()['items'][0]['signals'][0]['snapshot']['quote'] == saved
    assert result['learningLedger']['selectedCount'] == 1
