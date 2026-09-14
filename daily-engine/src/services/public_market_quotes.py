"""Anonymous, bounded Tencent/Sina quotes with independently verifiable timestamps.

This module never imports account configuration or shares an authenticated session.
Provider update times are evidence of quotes, not exchange fills or auction matches.
"""
from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

import requests

TZ = timezone(timedelta(hours=8))
MAX_QUOTE_CODES = 20
MAX_AGE_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 1.8
BATCH_TIMEOUT_SECONDS = 4.5
MAX_PAYLOAD_BYTES = 128_000
_BATCH_SLOTS = threading.BoundedSemaphore(4)
_SOURCE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='public-quotes')


class QuoteGatewayBusyError(RuntimeError):
    pass


def normalize_quote_codes(codes):
    if isinstance(codes, str):
        codes = codes.split(',')
    if not isinstance(codes, (list, tuple)) or not 1 <= len(codes) <= MAX_QUOTE_CODES:
        raise ValueError('Expected 1 to 20 Shanghai/Shenzhen A-share codes')
    if any(not isinstance(code, str) or not re.fullmatch(r'[036]\d{5}', code.strip()) for code in codes):
        raise ValueError('Expected six-digit A-share codes starting with 0, 3 or 6')
    return list(dict.fromkeys(code.strip() for code in codes))


def _now():
    return datetime.now(TZ)


def _iso(stamp):
    if stamp.tzinfo is None:
        raise ValueError('Timezone-aware clock required')
    return stamp.astimezone(TZ).isoformat(timespec='milliseconds')


def _source_time(value):
    fmt = '%Y%m%d%H%M%S' if re.fullmatch(r'\d{14}', value) else '%Y-%m-%d %H:%M:%S'
    try:
        stamp = datetime.strptime(value, fmt)
        if stamp.strftime(fmt) != value:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('invalid_source_time') from None
    return stamp.replace(tzinfo=TZ).isoformat()


def _number(value, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):
        raise ValueError('invalid_numeric_field')
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError('invalid_numeric_field')
    return number


def market_phase(stamp):
    clock = stamp[11:19] if isinstance(stamp, str) else stamp.astimezone(TZ).strftime('%H:%M:%S')
    for boundary, phase in [('09:15:00', 'pre_open'), ('09:25:00', 'opening_auction'),
                            ('09:30:00', 'opening_pause'), ('11:30:00', 'continuous'),
                            ('13:00:00', 'lunch_break'), ('14:57:00', 'continuous'),
                            ('15:00:00', 'closing_auction')]:
        if clock < boundary:
            return phase
    return 'post_close'


def _book(fields, source):
    sides, valid = [], True
    for start in ((9, 19) if source == 'tencent' else (10, 20)):
        side, zero_seen = [], False
        for level in range(5):
            offset = start + level * 2
            try:
                price = _number(fields[offset + (0 if source == 'tencent' else 1)])
                volume = _number(fields[offset + (1 if source == 'tencent' else 0)])
                if not volume.is_integer() or volume > 2**53 - 1 or (price == 0) != (volume == 0):
                    valid = False
                if zero_seen and price > 0:
                    valid = False
                zero_seen = zero_seen or price == 0
                side.append({'price': price, 'volume_shares': volume * (100 if source == 'tencent' else 1),
                             'source_volume': volume,
                             'source_volume_unit': 'lots_100_shares' if source == 'tencent' else 'shares'})
            except (IndexError, ValueError):
                valid = False
        sides.append(side)
    bids, asks = sides
    positives = [[level for level in side if level['price'] > 0] for side in sides]
    if any(b['price'] > a['price'] for a, b in zip(positives[0], positives[0][1:])):
        valid = False
    if any(b['price'] < a['price'] for a, b in zip(positives[1], positives[1][1:])):
        valid = False
    if all(positives) and positives[0][0]['price'] >= positives[1][0]['price']:
        valid = False
    return {'bids': bids, 'asks': asks, 'order_book_valid': valid}


def parse_quote_payload(payload, source, requested_codes):
    if source not in ('tencent', 'sina'):
        raise ValueError('Unknown public source')
    quotes, errors = {}, {}
    prefix = 'v_' if source == 'tencent' else 'hq_str_'
    for exchange, code, body in re.findall(prefix + r'(sh|sz)(\d{6})="([^"]*)"', payload):
        if code not in requested_codes or exchange != ('sh' if code.startswith('6') else 'sz'):
            continue
        if code in quotes or code in errors:
            quotes.pop(code, None)
            errors[code] = 'duplicate_quote'
            continue
        try:
            fields = body.split('~' if source == 'tencent' else ',')
            if len(fields) < (38 if source == 'tencent' else 32):
                raise ValueError('empty_or_incomplete_quote')
            if source == 'tencent' and fields[2] != code:
                raise ValueError('code_mismatch')
            name = fields[1 if source == 'tencent' else 0].strip()
            if not name:
                raise ValueError('missing_name')
            price = _number(fields[3], True)
            previous = _number(fields[4 if source == 'tencent' else 2], True)
            op = _number(fields[5 if source == 'tencent' else 1])
            high = _number(fields[33 if source == 'tencent' else 4])
            low = _number(fields[34 if source == 'tencent' else 5])
            stamp = _source_time(fields[30] if source == 'tencent' else fields[30] + ' ' + fields[31])
            if market_phase(stamp) == 'continuous' and not (0 < low <= price <= high and low <= op <= high):
                raise ValueError('inconsistent_price_fields')
            quotes[code] = {
                'code': code, 'name': name, 'price': price, 'previous_close': previous,
                'open': op, 'high': high, 'low': low, 'change_pct': round((price / previous - 1) * 100, 4),
                'volume_shares': _number(fields[36 if source == 'tencent' else 8]) * (100 if source == 'tencent' else 1),
                'amount_cny': _number(fields[37 if source == 'tencent' else 9]) * (10_000 if source == 'tencent' else 1),
                'source_time': stamp, **_book(fields, source),
            }
            # This is an explicit provider field, never a guessed 10% limit.
            if source == 'tencent' and len(fields) > 47:
                try:
                    quotes[code]['limit_up'] = _number(fields[47], True)
                except ValueError:
                    pass
        except (IndexError, ValueError) as exc:
            errors[code] = str(exc) if isinstance(exc, ValueError) else 'empty_or_incomplete_quote'
    return {'quotes': quotes, 'errors': errors}


def _fetch_payload(url, *, timeout):
    deadline = time.monotonic() + timeout
    # No environment proxy credentials, .netrc authentication, shared cookies or redirects.
    with requests.Session() as session:
        session.trust_env = False
        headers = {'User-Agent': 'StockKingPublicQuotes/1.0', 'Cache-Control': 'no-cache',
                   'Referer': 'https://gu.qq.com/' if 'qt.gtimg.cn/' in url else 'https://finance.sina.com.cn/'}
        with session.get(url, headers=headers, timeout=(timeout, timeout), stream=True,
                         allow_redirects=False, verify=True) as response:
            if response.status_code != 200:
                raise ValueError(f'upstream_http_{response.status_code}')
            announced = response.headers.get('Content-Length')
            if announced and int(announced) > MAX_PAYLOAD_BYTES:
                raise ValueError('payload_too_large')
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=4096):
                if time.monotonic() > deadline:
                    raise ValueError('upstream_timeout')
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    raise ValueError('payload_too_large')
                chunks.append(chunk)
            return b''.join(chunks).decode('gb18030')


def _age(stamp, now):
    return (now - datetime.fromisoformat(stamp)).total_seconds() * 1000


def _fresh(quote, now):
    return quote['source_time'][:10] == _iso(now)[:10] and 0 <= _age(quote['source_time'], now) <= MAX_AGE_SECONDS * 1000


def _fetch_source(source, codes, fetcher, clock, progress=None, progress_lock=None):
    symbols = ','.join(('sh' if code.startswith('6') else 'sz') + code for code in codes)
    path = ('qt.gtimg.cn/q=' if source == 'tencent' else 'hq.sinajs.cn/list=') + symbols
    observations, attempts, errors = [], [], {}
    for transport in ('https', 'http'):
        url = transport + '://' + path
        try:
            payload = fetcher(url, timeout=REQUEST_TIMEOUT_SECONDS)
            if len(payload.encode('utf-8')) > MAX_PAYLOAD_BYTES:
                raise ValueError('payload_too_large')
            fetched = clock()
            parsed = parse_quote_payload(payload, source, codes)
            errors.update(parsed['errors'])
            received = []
            for quote in parsed['quotes'].values():
                if _age(quote['source_time'], fetched) < 0:
                    errors[quote['code']] = 'future_source_time'
                    continue
                observation = {**quote, 'source': source, 'transport': transport,
                               'source_url': url, 'fetched_at': _iso(fetched)}
                observations.append(observation)
                received.append(observation)
            attempts.append({'source': source, 'transport': transport,
                             'status': 'ok' if received else 'failed',
                             'codes_received': [quote['code'] for quote in received],
                             **({} if received else {'error': 'no_valid_quotes'})})
            if all(any(row['code'] == code and _fresh(row, fetched) for row in received) for code in codes):
                break
        except Exception as exc:
            safe = str(exc)
            if isinstance(exc, requests.Timeout):
                safe = 'upstream_timeout'
            elif not re.fullmatch(r'upstream_http_\d+|upstream_timeout|payload_too_large', safe):
                safe = 'upstream_unavailable'
            attempts.append({'source': source, 'transport': transport, 'status': 'failed', 'error': safe})
        finally:
            if progress is not None:
                with progress_lock:
                    progress.update(observations=list(observations), attempts=list(attempts), errors=dict(errors))
    return {'observations': observations, 'attempts': attempts, 'errors': errors}


def _assess(code, results, checked):
    ranked = sorted([row for result in results for row in result['observations'] if row['code'] == code],
                    key=lambda row: (row['source_time'], row['transport'] == 'https'), reverse=True)
    if not ranked:
        return {'code': code, 'status': 'missing_data', 'quote': None, 'execution_verified': False,
                'quote_usable_for_current_price_check': False,
                'issues': list(dict.fromkeys(result['errors'].get(code, 'source_quote_unavailable') for result in results))}
    quote = ranked[0]
    phase, source_phase = market_phase(checked), market_phase(quote['source_time'])
    fresh = _fresh(quote, checked)
    same_day = quote['source_time'][:10] == _iso(checked)[:10]
    issues = []
    if not same_day:
        issues.append('different_market_date')
    if not fresh:
        issues.append('stale_quote')
    if phase != 'continuous':
        issues.append('request_phase_' + phase)
    if source_phase != 'continuous':
        issues.append('source_phase_' + source_phase)
    if checked.astimezone(TZ).weekday() >= 5:
        issues.append('non_weekday_reference')
    if 'post_close' in (phase, source_phase):
        issues.append('post_close_cannot_reconstruct_1455')
    if 'opening_auction' in (phase, source_phase):
        issues.append('auction_fields_not_supported')
    if quote['transport'] == 'http':
        issues.append('unencrypted_public_source_fallback')
    if not quote['order_book_valid']:
        issues.append('invalid_order_book')
    peer = next((row for row in ranked if row['source'] != quote['source'] and _fresh(row, checked)), None)
    difference = abs(_age(peer['source_time'], datetime.fromisoformat(quote['source_time']))) / 1000 if peer else None
    agree = abs(peer['price'] - quote['price']) <= 0.011 if peer and difference == 0 else None
    if agree is False:
        issues.append('cross_source_price_conflict')
    if peer and difference != 0:
        issues.append('cross_source_asynchronous_reference')
    usable = fresh and phase == source_phase == 'continuous' and agree is not False and checked.astimezone(TZ).weekday() < 5
    book_quote = next((row for row in ranked if usable and row['order_book_valid']
                       and abs(row['price'] - quote['price']) < 1e-8 and _fresh(row, checked)
                       and market_phase(row['source_time']) == 'continuous'), None)
    book = None
    if book_quote:
        book = {key: book_quote[key] for key in ('source', 'source_time', 'fetched_at', 'source_url', 'transport', 'bids', 'asks')}
        book.update(observed_quote_price=book_quote['price'], age_milliseconds=_age(book_quote['source_time'], checked))
        if book_quote is not quote:
            issues.append('order_book_from_separate_observation')
            if book_quote['transport'] == 'http':
                issues.append('order_book_unencrypted_public_source_fallback')
    age_ms = _age(quote['source_time'], checked)
    return {'code': code, 'status': 'fresh' if usable else 'reference_only', 'quote': quote,
            'age_seconds': round(age_ms / 1000, 1), 'age_milliseconds': age_ms,
            'same_market_date': same_day, 'phase': source_phase,
            'quote_usable_for_current_price_check': usable, 'order_book': book,
            'buy_side_liquidity_observed': bool(book and any(row['price'] > 0 and row['volume_shares'] > 0 for row in book['asks'])),
            'execution_verified': False, 'issues': issues,
            'cross_source_check': {'source': peer['source'], 'source_time': peer['source_time'], 'price': peer['price'],
                                   'time_difference_seconds': difference, 'synchronized': difference == 0,
                                   'prices_agree': agree} if peer else None}


def get_public_market_quotes(codes, *, clock=None, fetcher=None):
    """Return at most 20 independently assessed quotes; clock must return aware datetime.

    Caller latency is bounded even for a stalled DNS or body read. Timed-out workers
    retain their capacity slots until finished; repeated calls cannot grow a queue.
    """
    requested = normalize_quote_codes(codes)
    clock, fetcher = clock or _now, fetcher or _fetch_payload
    _iso(clock())
    if not _BATCH_SLOTS.acquire(blocking=False):
        raise QuoteGatewayBusyError('quote_gateway_busy')
    futures = []
    progress = [{}, {}]
    progress_lock = threading.Lock()
    try:
        for source, snapshot in zip(('tencent', 'sina'), progress):
            futures.append(_SOURCE_POOL.submit(_fetch_source, source, requested, fetcher, clock, snapshot, progress_lock))
    except Exception:
        _BATCH_SLOTS.release()
        raise
    release_lock, remaining = threading.Lock(), [len(futures)]

    def release_when_finished(_future):
        with release_lock:
            remaining[0] -= 1
            if remaining[0] == 0:
                _BATCH_SLOTS.release()

    for future in futures:
        future.add_done_callback(release_when_finished)
    done, _ = wait(futures, timeout=BATCH_TIMEOUT_SECONDS)
    results = []
    for source, future, snapshot in zip(('tencent', 'sina'), futures, progress):
        if future in done:
            results.append(future.result())
        else:
            # Preserve successful HTTPS siblings if a missing stock's HTTP fallback stalls.
            with progress_lock:
                observations = list(snapshot.get('observations', []))
                errors = dict(snapshot.get('errors', {}))
                attempts = list(snapshot.get('attempts', []))
            for code in requested:
                if not any(row['code'] == code for row in observations):
                    errors[code] = 'upstream_timeout'
            attempts.append({'source': source, 'status': 'failed', 'error': 'upstream_timeout'})
            results.append({'observations': observations, 'errors': errors, 'attempts': attempts})
    checked = clock()
    return {'schema_version': 1, 'checked_at': _iso(checked), 'max_age_seconds': MAX_AGE_SECONDS,
            'source_time_meaning': 'provider_quote_update_time', 'exchange_execution_time_verified': False,
            'historical_snapshot_supported': False, 'auction_fields_supported': False, 'trading_calendar_verified': False,
            'quotes': [_assess(code, results, checked) for code in requested],
            'attempts': [attempt for result in results for attempt in result['attempts']]}


def adapt_public_quote(batch, row):
    """Adapt one assessed batch row for DailyOpportunityService without refetching."""
    metadata = {key: value for key, value in batch.items() if key != 'quotes'}
    metadata.update({key: value for key, value in row.items() if key != 'quote'})
    quote = row['quote']
    if quote is None:
        return {'code': row['code'], 'is_stale': True, 'execution_verified': False, 'public_quote_metadata': metadata}
    book = row.get('order_book')
    locked_limit_up = None
    if (row['quote_usable_for_current_price_check'] and quote.get('limit_up')
            and abs(quote['price'] - quote['limit_up']) < 1e-8 and book
            and len(book['asks']) == 5 and all(level['price'] == 0 and level['volume_shares'] == 0 for level in book['asks'])):
        locked_limit_up = True
    return {**quote, 'pre_close': quote['previous_close'], 'open_price': quote['open'],
            'provider_timestamp': quote['source_time'], 'volume': quote['volume_shares'], 'amount': quote['amount_cny'],
            'is_stale': not row['quote_usable_for_current_price_check'],
            'locked_limit_up': locked_limit_up,
            'quote_usable_for_current_price_check': row['quote_usable_for_current_price_check'],
            'execution_verified': False, 'public_quote_metadata': metadata}


def get_public_realtime_quote(code, **options):
    """Compatibility adapter for DailyOpportunityService's quote_fetcher(code)."""
    batch = get_public_market_quotes([code], **options)
    return adapt_public_quote(batch, batch['quotes'][0])
