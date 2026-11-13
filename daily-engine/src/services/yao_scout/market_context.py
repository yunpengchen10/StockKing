"""Reuse desktop market evidence for a small, dated research brief.

Event-linked stocks are NOT a complete concept membership universe. A watch
idea does not need whole-market concept coverage or satisfy an entry signal.
Only the independent opportunity assessment may mark entry conditions met.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import re

from .theme_rotation import _at, _number, _invalid_quote_evidence

VERSION = 'market-brief-v1'
MAINBOARD = re.compile(r'(?:600|601|603|605|000|001|002|003)\d{3}')


def _code(value):
    value = str(value or '').strip()
    match = re.fullmatch(r'(?:(?:sh|sz))?(\d{6})(?:\.(?:SH|SZ))?', value, re.I)
    return match[1] if match else ''


def _previous_day(now):
    from .premarket import previous_session
    try:
        value = previous_session(now)
        return str(value)[:10]
    except Exception:
        return None


def _event_rows(packet, now, previous_day=None):
    part = packet.get('events') or {}
    observed = _at(part.get('observedAt') or packet.get('observedAt'))
    if part.get('status') not in {'observed', 'partial', 'reference_only'} or not observed or not 0 <= (now-observed).total_seconds() <= 600:
        return []
    previous_day = previous_day or _previous_day(now)
    allowed = {now.date().isoformat(), previous_day}
    rows, seen = [], set()
    for item in part.get('items') or []:
        if not isinstance(item, dict):
            continue
        stamp = _at(item.get('sourceTime'))
        day = str(item.get('date') or '')[:10]
        identity = str(item.get('eventId') or '')
        # Publication may predate the daily event list; retain both dates.
        if (not identity or identity in seen or day not in allowed or not stamp
                or stamp > observed or stamp > now or (now-stamp) > timedelta(days=7)
                or not item.get('title') or not part.get('source')):
            continue
        seen.add(identity)
        rows.append({**item, 'source': part['source'], 'eventDate': day,
                     'currentDay': day == now.date().isoformat()})
    return rows


def event_candidate_codes(packet, now):
    """Bounded research seeds; never infer prices from yesterday's event list."""
    now = _at(now)
    if not now:
        return []
    events = sorted(_event_rows(packet or {}, now),
                    key=lambda row: (not row['currentDay'], -(_number(row.get('heat')) or 0), row['eventId']))
    codes = []
    for event in events:
        for stock in event.get('topStocks') or []:
            if not isinstance(stock, dict):
                continue
            code = _code(stock.get('stockCode'))
            if MAINBOARD.fullmatch(code) and not re.search(r'ST|退|整理|停牌', str(stock.get('stockName') or ''), re.I):
                codes.append(code)
    return list(dict.fromkeys(codes))[:80]


def prioritize_market_queue(rows, packet, now, limit):
    """Event ideas get research slots even when the full concept rank is absent."""
    limit = max(0, int(limit))
    rows = [row for row in rows or [] if isinstance(row, dict)]
    codes = event_candidate_codes(packet, now)
    lookup = {str(row.get('code')): row for row in rows if isinstance(row, dict)}
    seeded = [lookup[code] for code in codes if code in lookup]
    seed_codes = {row['code'] for row in seeded}
    # Retain controls and existing theme/flow discovery; no event monopolizes I/O.
    count = min(len(seeded), max(1, int(limit*.6))) if limit else 0
    return [*seeded[:count], *(row for row in rows if row.get('code') not in seed_codes), *seeded[count:]][:limit]


def _session_reference(raw, code, stamp, now):
    """Permit explicitly assessed resting-session prices for observation only.

    The shared public adapter marks non-continuous prices unusable for current
    execution. That is distinct from an invalid price or a source conflict.
    """
    from src.services.public_market_quotes import market_phase
    metadata = raw.get('public_quote_metadata') or {}
    phase = market_phase(now)
    if (not isinstance(metadata, dict) or metadata.get('status') != 'reference_only'
            or metadata.get('code') != code or metadata.get('same_market_date') is not True
            or not stamp or stamp.date() != now.date() or stamp > now):
        return None
    known = _at(metadata.get('checked_at'))
    if known is None or not stamp <= known <= now:
        return None
    if phase == 'lunch_break':
        # Providers may refresh their quote packet during the break. Preserve
        # that provider update time; it is not a claim of a new trade.
        permitted_window = time(11, 27) <= stamp.time() < time(13)
        source_phases = {'continuous', 'lunch_break'}
    elif phase == 'post_close':
        permitted_window = time(14, 57) <= stamp.time() <= time(15, 5)
        source_phases = {'closing_auction', 'post_close'}
    else:
        return None
    if not permitted_window or metadata.get('phase') not in source_phases or metadata.get('phase') != market_phase(stamp):
        return None
    allowed = {'stale_quote', 'invalid_order_book', 'cross_source_asynchronous_reference',
               'unencrypted_public_source_fallback', 'post_close_cannot_reconstruct_1455',
               'request_phase_'+phase, *('source_phase_'+p for p in source_phases)}
    issues = metadata.get('issues') or []
    if not isinstance(issues, list) or 'request_phase_'+phase not in issues or set(issues)-allowed:
        return None
    cleaned = {**raw, 'stale': False, 'is_stale': False, 'quote_usable_for_current_price_check': True}
    cleaned['public_quote_metadata'] = {**metadata, 'stale': False, 'is_stale': False,
        'quote_usable_for_current_price_check': True, 'issues': []}
    return cleaned if not _invalid_quote_evidence(cleaned) else None


def _quote(raw, code, now):
    if not isinstance(raw, dict):
        return None
    quote = raw.get('market_quote') or raw.get('quote') or raw
    if not isinstance(quote, dict) or _code(quote.get('code')) != code:
        return None
    stamp = _at(quote.get('provider_timestamp') or quote.get('source_time'))
    if _invalid_quote_evidence(raw) or _invalid_quote_evidence(quote):
        # Normal service inputs are adapted flat quotes. A rejected wrapper may
        # not be bypassed by a valid nested quote or a timestamp alone.
        if quote is not raw or _session_reference(raw, code, stamp, now) is None:
            return None
    price = _number(quote.get('price'))
    previous = _number(quote.get('pre_close') or quote.get('previous_close'))
    change = _number(quote.get('change_pct'))
    if (not stamp or stamp > now or stamp.date() != now.date() or not quote.get('source')
            or price is None or price <= 0 or previous is None or previous <= 0 or change is None
            or abs((price/previous-1)*100-change) > .15):
        return None
    from src.services.public_market_quotes import market_phase
    current = market_phase(now) == market_phase(stamp) == 'continuous' and (now-stamp).total_seconds() <= 300
    reference = (time(11, 30) <= now.time() < time(13) and time(11, 27) <= stamp.time() < time(13)
                 or now.time() >= time(15) and time(14, 57) <= stamp.time() <= time(15, 5))
    if not current and not reference:
        return None
    return {'code': code, 'name': str(quote.get('name') or raw.get('name') or ''), 'price': price,
            'changePct': change, 'sourceTime': stamp.isoformat(), 'source': quote['source'],
            'quoteFreshness': 'live' if current else 'session_reference'}


def _funds(packet, names, now):
    part = packet.get('funds') or {}
    rows = []
    for row in part.get('items') or []:
        stamp = _at(row.get('snapshotObservedAt'))
        value = _number(row.get('netInflow'))
        if (row.get('name') in names and stamp and stamp <= now and stamp.date() == now.date()
                and value is not None):
            rows.append(row)
    if not rows:
        return {'status': 'unavailable', 'sourceTime': None}
    row = max(rows, key=lambda item: item['snapshotObservedAt'])
    age = (now-_at(row['snapshotObservedAt'])).total_seconds()
    return {**row, 'status': 'snapshot_only', 'source': part.get('source'), 'sourceTime': None,
            'referenceAgeSeconds': age, 'stale': age > 600,
            'meaning': '当日采集的累计资金参考快照；采集时间不是行情时间，不表示当前资金，也不替代30分钟资金核验'}


def _news(packet, names, now):
    part = packet.get('news') or {}
    observed = _at(part.get('observedAt') or packet.get('observedAt'))
    if not observed or observed > now:
        return []
    result = []
    for row in part.get('items') or []:
        stamp = _at(row.get('sourceTime'))
        text = str(row.get('title') or '') + str(row.get('content') or '')
        if (stamp and stamp <= observed and stamp.date() == now.date()
                and any(len(name) >= 2 and name in text for name in names)):
            result.append({key: row.get(key) for key in ('title', 'url', 'sourceTime', 'source')})
    return result[:3]


def build_market_brief(packet, quotes, now, *, opportunities=None, previous_day=None):
    now = _at(now)
    if not now:
        raise ValueError('Market brief requires a timezone-aware clock')
    packet = packet if isinstance(packet, dict) else {}
    result = {'version': VERSION, 'asOf': now.isoformat(), 'status': 'unavailable',
              'mainlines': [], 'focusStocks': [], 'gaps': [],
              'summary': '市场题材及当日报价暂未形成可核验的重点观察名单。',
              'meaning': '重点股票是有来源的研究观察；条件已满足仅取自独立资金与买点核验。'}
    events = _event_rows(packet, now, previous_day)
    channels = opportunities or {}
    recommended = {row['code']: row for row in channels.get('recommendations') or []}
    assessments = {row['code']: row for row in channels.get('researchCandidates') or []}
    choices = []
    for event in events:
        themes = [row for row in event.get('themes') or [] if isinstance(row, dict)]
        names = list(dict.fromkeys(str(row.get('showName') or row.get('indexName') or '') for row in themes))
        names = [name for name in names if name]
        title = ' / '.join(names[:2]) or str(event['title'])[:36]
        linked, seen = [], set()
        for stock in event.get('topStocks') or []:
            if not isinstance(stock, dict):
                continue
            code = _code(stock.get('stockCode'))
            if (code in seen or not MAINBOARD.fullmatch(code)
                    or re.search(r'ST|退|整理|停牌', str(stock.get('stockName') or ''), re.I)):
                continue
            seen.add(code)
            quote = _quote(quotes.get(code), code, now)
            if quote and not re.search(r'ST|退|整理|停牌', quote['name'], re.I):
                linked.append({**quote, 'name': quote['name'] or stock.get('stockName') or code,
                               'reason': str(stock.get('reason') or event['title'])})
        linked.sort(key=lambda row: (-row['changePct'], row['code']))
        positive = [row for row in linked if row['changePct'] > 0]
        if not positive:
            continue
        tid = 'ths-event:' + str(event['eventId'])
        mainline = {'id': tid, 'name': title, 'rank': None, 'catalyst': event['title'],
            'eventSourceTime': event['sourceTime'], 'eventDate': event['eventDate'],
            'source': event['source'], 'heat': _number(event.get('heat')), 'leaders': [row['code'] for row in positive],
            'status': 'current_event' if event['currentDay'] else 'continuation',
            'statusLabel': '今日题材观察' if event['currentDay'] else '昨日题材今日跟踪',
            'rankScope': '事件关联股票，非全概念成分排名',
            'quoteCoverage': {'validCount': len(linked), 'expectedCount': len(seen)},
            'funds': _funds(packet, names, now), 'news': _news(packet, [*names, *(row['name'] for row in linked)], now)}
        choices.append((mainline, positive))
    choices.sort(key=lambda pair: (-len(pair[1]), -(pair[0]['heat'] or 0), -pair[1][0]['changePct'], pair[0]['id']))
    used, signatures = set(), set()
    for mainline, linked in choices:
        fingerprint = tuple(sorted(mainline['leaders']))
        if fingerprint in signatures or not any(row['code'] not in used for row in linked):
            continue
        signatures.add(fingerprint)
        mainline['rank'] = len(result['mainlines'])+1
        result['mainlines'].append(mainline)
        for row in [row for row in linked if row['code'] not in used][:2]:
            if len(result['focusStocks']) >= 5:
                break
            used.add(row['code'])
            assessment = (assessments.get(row['code']) or {}).get('opportunity') or {}
            recommendation = recommended.get(row['code']) or {}
            entry = recommendation.get('opportunity') or {}
            assessed_at = _at(channels.get('asOf'))
            satisfied = (recommendation.get('ruleRecommendation') is True and entry.get('grade') == 'F-A'
                         and not entry.get('hardVetoes') and not entry.get('failedConditions')
                         and assessed_at is not None and 0 <= (now-assessed_at).total_seconds() <= 30
                         and row['quoteFreshness'] == 'live' and now.weekday() < 5
                         and (now-_at(row['sourceTime'])).total_seconds() <= 30)
            pending = list(assessment.get('missing') or [])[:3]
            if not satisfied and not pending:
                pending = ['资金持续性与买点仍待确认']
            risks = list(dict.fromkeys([*(assessment.get('hardVetoes') or []), *(assessment.get('counterEvidence') or [])]))[:3]
            if row['changePct'] >= 8:
                risks.insert(0, '当日涨幅较大，等待回落或量价确认，不追高')
            result['focusStocks'].append({**row, 'themeId': mainline['id'], 'themeName': mainline['name'],
                'eventSourceTime': mainline['eventSourceTime'], 'eventDate': mainline['eventDate'],
                'status': 'conditions_met' if satisfied else 'watch',
                'statusLabel': '条件已满足' if satisfied else '重点观察，等待确认',
                'ruleRecommendation': bool(satisfied), 'executable': False,
                'confirmed': [mainline['statusLabel']+'：'+mainline['catalyst'], '当日报价上涨，来源与时间已核验'],
                'pending': [] if satisfied else pending, 'risks': risks})
        if len(result['mainlines']) >= 3:
            break
    if result['focusStocks']:
        result['status'] = 'observed'
        result['summary'] = f"跟踪{len(result['mainlines'])}条题材线索、{len(result['focusStocks'])}只重点股票；逐股列出依据、待确认条件和风险。"
    if not events:
        result['gaps'].append('市场模块没有可核验的当日或上一交易日题材事件')
    elif not result['focusStocks']:
        result['gaps'].append('事件关联股票缺少当日有效上涨报价，未用事件榜旧涨幅补足名单')
    result['sourceStatus'] = {key: (packet.get(key) or {}).get('status', 'unavailable') for key in ('events', 'funds', 'news')}
    return result
