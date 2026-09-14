from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import time

import pytest
import requests

from src.services import public_market_quotes as quotes

NOW = datetime.fromisoformat('2026-09-14T14:55:00+08:00')
CODES = ['603936', '603002', '600026']


def tencent(code='603936', **overrides):
    fields = ['0'] * 50
    values = {0: '1', 1: 'Fixture', 2: code, 3: '10.5', 4: '10', 5: '10.1', 6: '500',
              30: '20260914145500', 33: '11', 34: '9.5', 36: '500', 37: '52.5'}
    for level in range(5):
        values.update({9 + level * 2: f'{10.49 - level * .01:.2f}', 10 + level * 2: '12',
                       19 + level * 2: f'{10.51 + level * .01:.2f}', 20 + level * 2: '5'})
    values.update({int(key): value for key, value in overrides.items()})
    for key, value in values.items():
        fields[key] = value
    exchange = 'sh' if code.startswith('6') else 'sz'
    return f'v_{exchange}{code}="{"~".join(fields)}";'


def sina(code='603936', **overrides):
    fields = ['0'] * 33
    values = {0: 'Fixture', 1: '10.1', 2: '10', 3: '10.5', 4: '11', 5: '9.5',
              8: '50000', 9: '525000', 30: '2026-09-14', 31: '14:55:00'}
    for level in range(5):
        values.update({10 + level * 2: '1200', 11 + level * 2: f'{10.49 - level * .01:.2f}',
                       20 + level * 2: '500', 21 + level * 2: f'{10.51 + level * .01:.2f}'})
    values.update({int(key): value for key, value in overrides.items()})
    for key, value in values.items():
        fields[key] = value
    exchange = 'sh' if code.startswith('6') else 'sz'
    return f'var hq_str_{exchange}{code}="{",".join(fields)}";'


def both(a=None, b=None, now=NOW):
    a, b = tencent() if a is None else a, sina() if b is None else b
    return {'clock': lambda: now, 'fetcher': lambda url, **_kwargs: a if 'gtimg' in url else b}


@pytest.mark.parametrize('codes', ['', ['603936&q=secret'], ['123456'], ['603936'] * 21, [], ['http://localhost'], [603936]])
def test_limits_reject_unbounded_codes_and_arbitrary_endpoints(codes):
    with pytest.raises(ValueError):
        quotes.normalize_quote_codes(codes)


def test_real_field_layouts_timestamps_and_units():
    assert quotes.normalize_quote_codes('603936, 603002,600026,603936') == CODES
    a = quotes.parse_quote_payload(tencent(), 'tencent', CODES)['quotes']['603936']
    b = quotes.parse_quote_payload(sina(), 'sina', CODES)['quotes']['603936']
    assert a['source_time'] == '2026-09-14T14:55:00+08:00'
    assert a['volume_shares'] == b['volume_shares'] == 50000
    assert a['amount_cny'] == b['amount_cny'] == 525000
    assert a['bids'][0]['volume_shares'] == b['bids'][0]['volume_shares'] == 1200
    assert a['bids'][0]['source_volume_unit'] == 'lots_100_shares'
    assert b['bids'][0]['source_volume_unit'] == 'shares'
    assert len(a['asks']) == 5
    assert a['asks'][0]['volume_shares'] == 500
    assert a['order_book_valid'] is True


@pytest.mark.parametrize('fields', [{'3': ''}, {'4': '0'}, {'3': 'NaN'}, {'2': '600026'},
                                  {'30': '20260230145500'}, {'30': '2026091414550'}, {'3': '12'}])
def test_bad_quotes_do_not_destroy_siblings(fields):
    result = quotes.parse_quote_payload(tencent(**fields) + tencent('603002'), 'tencent', CODES)
    assert '603936' not in result['quotes']
    assert '603002' in result['quotes']


def test_duplicate_and_wrong_exchange_records_are_not_used():
    result = quotes.parse_quote_payload(tencent() + tencent() + tencent('603002').replace('v_sh', 'v_sz'), 'tencent', CODES)
    assert result['quotes'] == {}
    assert result['errors']['603936'] == 'duplicate_quote'


def test_compatibility_adapter_preserves_source_and_fetch_times_without_execution_claim():
    result = quotes.get_public_realtime_quote('603936', **both(now=NOW + timedelta(seconds=3)))
    assert result['provider_timestamp'] == result['source_time'] == '2026-09-14T14:55:00+08:00'
    assert result['fetched_at'] == '2026-09-14T14:55:03.000+08:00'
    assert result['pre_close'] == 10 and result['open_price'] == 10.1
    assert result['is_stale'] is False and result['execution_verified'] is False
    assert result['public_quote_metadata']['cross_source_check']['prices_agree'] is True
    assert result['public_quote_metadata']['exchange_execution_time_verified'] is False


@pytest.mark.parametrize(('age_ms', 'expected'), [(0, 'fresh'), (30000, 'fresh'), (30049, 'reference_only'), (31000, 'reference_only')])
def test_30_second_policy_uses_unrounded_age(age_ms, expected):
    row = quotes.get_public_market_quotes(['603936'], **both(now=NOW + timedelta(milliseconds=age_ms)))['quotes'][0]
    assert row['status'] == expected
    assert row['age_milliseconds'] == age_ms
    if age_ms == 30049:
        assert row['age_seconds'] == 30
        assert row['order_book'] is None


def test_prior_date_is_stale_and_future_time_is_rejected():
    prior = quotes.get_public_realtime_quote('603936', **both(tencent(**{'30': '20260911145500'}), sina(**{'30': '2026-09-11'})))
    assert prior['is_stale'] is True
    assert 'different_market_date' in prior['public_quote_metadata']['issues']
    future = quotes.get_public_realtime_quote('603936', **both(now=NOW - timedelta(seconds=1)))
    assert future['is_stale'] is True and 'provider_timestamp' not in future
    assert future['public_quote_metadata']['issues'] == ['future_source_time']


def test_per_stock_fallback_keeps_good_quotes_and_reports_only_failed_symbols():
    result = quotes.get_public_market_quotes(CODES, **both(tencent(), sina('603002')))
    assert [row['status'] for row in result['quotes']] == ['fresh', 'fresh', 'missing_data']
    assert result['quotes'][1]['quote']['source'] == 'sina'
    stale = quotes.get_public_market_quotes(CODES, **both(tencent(**{'30': '20260914144325'}) + tencent('603002'), ''))
    assert stale['quotes'][0]['status'] == 'reference_only'
    assert stale['quotes'][0]['age_seconds'] == 695
    assert stale['quotes'][1]['status'] == 'fresh'


@pytest.mark.parametrize(('clock', 'compact', 'reason'), [
    ('2026-09-14T15:26:55+08:00', '20260914152651', 'post_close_cannot_reconstruct_1455'),
    ('2026-09-14T09:20:00+08:00', '20260914092000', 'auction_fields_not_supported'),
    ('2026-09-14T12:00:00+08:00', '20260914120000', 'request_phase_lunch_break'),
])
def test_market_phase_limits_cannot_be_bypassed_with_fresh_provider_update(clock, compact, reason):
    result = quotes.get_public_realtime_quote('603936', **both(tencent(**{'30': compact}), '', datetime.fromisoformat(clock)))
    assert result['is_stale'] is True
    assert result['public_quote_metadata']['status'] == 'reference_only'
    assert reason in result['public_quote_metadata']['issues']
    assert result['public_quote_metadata']['auction_fields_supported'] is False
    assert result['public_quote_metadata']['historical_snapshot_supported'] is False


def test_only_synchronized_price_disagreement_is_a_conflict():
    conflict = quotes.get_public_market_quotes(['603936'], **both(b=sina(**{'3': '10.8'})))['quotes'][0]
    assert conflict['status'] == 'reference_only'
    assert conflict['cross_source_check']['prices_agree'] is False
    asynchronous = quotes.get_public_market_quotes(['603936'], **both(b=sina(**{'3': '10.8', '31': '14:54:40'})))['quotes'][0]
    assert asynchronous['status'] == 'fresh'
    assert asynchronous['cross_source_check']['prices_agree'] is None
    assert asynchronous['cross_source_check']['time_difference_seconds'] == 20
    assert 'cross_source_price_conflict' not in asynchronous['issues']


def test_valid_same_price_peer_book_retains_its_own_source_and_timestamp():
    row = quotes.get_public_market_quotes(['603936'], **both(tencent(**{'19': '10.40'}), sina(**{'31': '14:54:55', '20': '2700'})))['quotes'][0]
    assert row['quote']['source'] == 'tencent' and row['quote']['order_book_valid'] is False
    assert row['quote']['asks'][0]['price'] == 10.4
    assert row['order_book']['source'] == 'sina'
    assert row['order_book']['source_time'] == '2026-09-14T14:54:55+08:00'
    assert row['order_book']['age_milliseconds'] == 5000
    assert row['order_book']['asks'][0]['volume_shares'] == 2700
    assert row['buy_side_liquidity_observed'] is True and row['execution_verified'] is False


@pytest.mark.parametrize('peer', [{'31': '14:54:29'}, {'31': '14:54:55', '3': '10.51'}, {'31': '14:54:55', '21': '10.40'}])
def test_stale_different_price_or_bad_peer_books_cannot_be_spliced(peer):
    row = quotes.get_public_market_quotes(['603936'], **both(tencent(**{'19': '10.40'}), sina(**peer)))['quotes'][0]
    assert row['order_book'] is None and row['buy_side_liquidity_observed'] is False


def test_zero_asks_retain_all_real_levels_but_do_not_claim_liquidity():
    zero_asks = {str(index): '0' for index in range(19, 29)}
    row = quotes.get_public_market_quotes(['603936'], **both(tencent(**zero_asks), ''))['quotes'][0]
    assert len(row['quote']['asks']) == 5
    assert row['buy_side_liquidity_observed'] is False


@pytest.mark.parametrize(('limit_up', 'asks_empty', 'age_seconds', 'locked'), [
    ('10.5', True, 0, True), ('0', True, 0, None), ('10.6', True, 0, None),
    ('10.5', False, 0, None), ('10.5', True, 31, None),
])
def test_locked_limit_requires_explicit_limit_and_fresh_complete_empty_asks(limit_up, asks_empty, age_seconds, locked):
    fields = {'47': limit_up}
    if asks_empty:
        fields.update({str(index): '0' for index in range(19, 29)})
    batch = quotes.get_public_market_quotes(['603936'], **both(tencent(**fields), '', NOW + timedelta(seconds=age_seconds)))
    adapted = quotes.adapt_public_quote(batch, batch['quotes'][0])
    assert adapted['locked_limit_up'] is locked
    assert 'suspended' not in adapted


def test_https_failure_falls_back_to_marked_anonymous_http_and_sanitizes_errors():
    seen = []

    def fetcher(url, **kwargs):
        seen.append((url, kwargs))
        if url.startswith('https:'):
            raise requests.exceptions.SSLError('a-private-secret-must-not-be-persisted')
        return tencent() if 'gtimg' in url else sina()

    result = quotes.get_public_market_quotes(['603936'], clock=lambda: NOW, fetcher=fetcher)
    assert len(seen) == 4
    assert all(kwargs['timeout'] == 1.8 for _, kwargs in seen)
    assert result['quotes'][0]['quote']['transport'] == 'http'
    assert 'unencrypted_public_source_fallback' in result['quotes'][0]['issues']
    assert 'a-private-secret' not in str(result)


def test_transport_disables_redirects_auth_environment_and_checks_tls(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def iter_content(self, **_kwargs):
            yield tencent().encode('gb18030')

    seen = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, url, **kwargs):
            seen.append((self.trust_env, url, kwargs))
            return Response()

    monkeypatch.setattr(quotes.requests, 'Session', Session)
    quotes._fetch_payload('https://qt.gtimg.cn/q=sh603936', timeout=1.8)
    trust_env, url, kwargs = seen[0]
    assert trust_env is False
    assert kwargs['allow_redirects'] is False and kwargs['verify'] is True
    assert kwargs['timeout'] == (1.8, 1.8)
    assert set(kwargs['headers']) == {'User-Agent', 'Referer', 'Cache-Control'}


def test_oversized_payloads_are_bounded():
    result = quotes.get_public_market_quotes(['603936'], **both('x' * 128001, 'x' * 128001))
    assert result['quotes'][0]['status'] == 'missing_data'
    assert all(attempt['error'] == 'payload_too_large' for attempt in result['attempts'])


def test_stalled_fallback_preserves_sibling_success_from_https(monkeypatch):
    monkeypatch.setattr(quotes, 'BATCH_TIMEOUT_SECONDS', .05)
    release = threading.Event()

    def fetcher(url, **_kwargs):
        if url.startswith('http:'):
            release.wait(1)
        return tencent() if 'gtimg' in url else sina()

    try:
        result = quotes.get_public_market_quotes(['603936', '603002'], clock=lambda: NOW, fetcher=fetcher)
        assert [row['status'] for row in result['quotes']] == ['fresh', 'missing_data']
        assert result['quotes'][0]['quote']['transport'] == 'https'
    finally:
        release.set()


def test_stalled_sources_bound_caller_latency_and_hold_capacity_until_completion(monkeypatch):
    monkeypatch.setattr(quotes, 'BATCH_TIMEOUT_SECONDS', .05)
    release = threading.Event()
    count_lock, all_started, count = threading.Lock(), threading.Event(), [0]

    def fetcher(_url, **_kwargs):
        with count_lock:
            count[0] += 1
            if count[0] == 8:
                all_started.set()
        release.wait(2)
        return tencent() if 'gtimg' in _url else sina()

    with ThreadPoolExecutor(max_workers=4) as callers:
        running = [callers.submit(quotes.get_public_market_quotes, ['603936'], clock=lambda: NOW, fetcher=fetcher) for _ in range(4)]
        try:
            assert all_started.wait(1)
            with pytest.raises(quotes.QuoteGatewayBusyError):
                quotes.get_public_market_quotes(['603936'], **both())
            started = time.monotonic()
            results = [future.result(timeout=1) for future in running]
            assert time.monotonic() - started < .5
            assert all(result['quotes'][0]['status'] == 'missing_data' for result in results)
            with pytest.raises(quotes.QuoteGatewayBusyError):
                quotes.get_public_market_quotes(['603936'], **both())
        finally:
            release.set()
    # Wait for the actual worker completions, not merely the bounded caller futures.
    for _ in range(100):
        try:
            quotes.get_public_market_quotes(['603936'], **both())
            break
        except quotes.QuoteGatewayBusyError:
            time.sleep(.005)
    else:
        pytest.fail('Timed-out workers failed to release concurrency slots')
