"""Point-in-time evidence for an offer below the applicable session price limit."""
from __future__ import annotations

import re

from src.services.public_market_quotes import market_phase
from .theme_rotation import _at, _number, _invalid_quote_evidence


VERSION = 'pre-limit-entry-v1'


def _positive(value):
    price = _number(value)
    return price if price is not None and price > 0 else None


def _fresh(source_time, as_of):
    stamp = _at(source_time)
    return bool(stamp and as_of and stamp.date() == as_of.date() and 0 <= (as_of-stamp).total_seconds() <= 30)


def _known(packet, stamp, now):
    for key in ('fetched_at', 'checked_at'):
        if packet.get(key) is not None:
            known = _at(packet[key])
            if not known or not stamp <= known <= now:
                return False
    return True


def _source(value):
    return isinstance(value, str) and value.strip().lower() not in ('', 'unknown', 'missing', '未取得', '未记录')


def _best_level(levels, *, descending=False):
    """Reject disordered or half-empty depth. Paired zeroes denote empty levels."""
    if not isinstance(levels, list):
        return None
    best = (None, 0)
    previous, empty = None, False
    for level in levels:
        if not isinstance(level, dict):
            return None
        price, size = _number(level.get('price')), _number(level.get('volume_shares'))
        if price == size == 0:
            empty = True
            continue
        if empty or not price or price < 0 or not size or size < 0 or not size.is_integer():
            return None
        if previous is not None and (price >= previous if descending else price <= previous):
            return None
        if previous is None:
            best = (price, int(size))
        previous = price
    return best


def assess_limit_entry(code, quote, as_of):
    """Use only this security's explicitly supplied prices, clock and order book."""
    quote = quote if isinstance(quote, dict) else {}
    now = _at(as_of)
    stamp = _at(quote.get('provider_timestamp') or quote.get('source_time'))
    price, upper, high = (_positive(quote.get(key)) for key in ('price', 'limit_up', 'high'))
    previous = _positive(quote.get('pre_close', quote.get('previous_close')))
    source = quote.get('source')
    result = dict(version=VERSION, code=code, status='unavailable', eligible=False,
                  reason='报价的代码、来源、价格或时效未通过核验', price=price, sessionHigh=high,
                  limitUp=None, source=source, sourceTime=stamp.isoformat() if stamp else None,
                  limitSource=None, limitSourceTime=None, askPrice=None, askVolumeShares=None,
                  bookSource=None, bookSourceTime=None)
    meta = quote.get('public_quote_metadata') or {}
    if (not re.fullmatch(r'\d{6}', str(code)) or quote.get('code') != code
            or not isinstance(meta, dict) or meta.get('code', code) != code
            or not price or not previous or not _source(source) or not _fresh(stamp, now)
            or not _known(quote, stamp, now) or not _known(meta, stamp, now)
            or _invalid_quote_evidence(quote)):
        return result
    if any(quote.get(key) for key in ('no_price_limit', 'corporate_action', 'suspended', 'trading_restricted')):
        return {**result, 'reason': '停牌、交易限制或特殊价格制度待核验'}
    lower = _positive(quote.get('limit_down'))
    if (not upper or upper <= previous or (lower and not lower < previous)
            or price > upper + 1e-7 or (high and not price-1e-7 <= high <= upper+1e-7)
            or (lower and price < lower-1e-7)):
        return {**result, 'reason': '缺少有效的当日涨停价，或报价与涨跌停边界冲突'}
    limits = quote.get('price_limit_evidence')
    if limits is not None:
        if not isinstance(limits, dict):
            return {**result, 'reason': '涨停价来源未核实'}
        at = _at(limits.get('source_time'))
        if (limits.get('method') != 'explicit_provider_fields' or limits.get('status') not in ('observed', 'partial')
                or not _source(limits.get('source')) or not _fresh(at, now) or at > stamp
                or not _known(limits, at, now) or limits.get('code', code) != code
                or _positive(limits.get('limit_up')) != upper
                or _positive(limits.get('previous_close')) != previous):
            return {**result, 'reason': '涨停价来源、时间或昨收口径不一致'}
    result.update(limitUp=upper, limitSource=(limits or {}).get('source') or source,
                  limitSourceTime=(limits or {}).get('source_time') or result['sourceTime'])
    book = quote.get('order_book') or meta.get('order_book') or {}
    if isinstance(book, dict) and book and _fresh(book.get('source_time'), now):
        book_source = book.get('source') or source
        at = _at(book['source_time'])
        bad_quote_book = (quote.get('order_book_valid') is False
                          or 'invalid_order_book' in (quote.get('issues') or [])
                          or 'invalid_order_book' in (meta.get('issues') or []))
        separate_book = book_source != source or at != stamp
        if (_source(book_source) and book.get('code', code) == code and _known(book, at, now)
                and not _invalid_quote_evidence(book) and book.get('order_book_valid') is not False
                and (book.get('observed_quote_price') is None or _positive(book['observed_quote_price']) == price)
                and (not bad_quote_book or separate_book)):
            offer = _best_level(book.get('asks'))
            bid = _best_level(book.get('bids', []), descending=True)
            if offer and bid:
                ask, size = offer
                if ask is None or ((not bid[0] or bid[0] <= ask) and ask <= upper and (not lower or lower <= ask)):
                    result.update(askPrice=ask, askVolumeShares=size, bookSource=book_source, bookSourceTime=at.isoformat())
    # A trustworthy last price at the ceiling establishes a touch even if
    # the provider discarded the one-sided book. No inference of a fill.
    if abs(price - upper) <= 1e-7:
        return {**result, 'status': 'at_limit', 'reason': '扫描时已到涨停价'}
    if quote.get('locked_limit_up') is True:
        return {**result, 'reason': '封板标记与最新报价冲突'}
    if not high:
        return {**result, 'reason': '缺少当日最高价，无法确认此前是否触板'}
    if abs(high - upper) <= 1e-7:
        return {**result, 'status': 'touched_limit', 'reason': '扫描前已经触板，当前已开板'}
    if (now.weekday() >= 5 or market_phase(now) != 'continuous' or market_phase(stamp) != 'continuous'
            or not result['bookSourceTime'] or market_phase(_at(result['bookSourceTime'])) != 'continuous'
            or result['askPrice'] is None or result['askPrice'] >= upper):
        return {**result, 'reason': '连续交易时段内低于涨停价的有效卖一价量尚未核实'}
    return {**result, 'status': 'pre_limit', 'eligible': True, 'reason': '当日尚未触板，扫描时有低于涨停价的卖一报价'}
