"""Point-in-time market paths for channel rules, in completed-minute end time."""
from datetime import datetime, timedelta
import re
from time import monotonic

import requests

from .intraday_evidence import SHANGHAI, _datetime, _normalize, parse_tencent_intraday
from .minute_history import bar_session, bounded_fetch_map
from .minute_integrity import identity_matches


def _path(points, source):
    if not points:
        return {}
    return {'status': 'observed', 'source': source, 'sourceTime': points[-1]['sourceTime'],
            'amountKind': 'interval', 'amountUnit': 'CNY', 'volumeKind': 'interval',
            'volumeUnit': 'shares', 'points': points[-35:]}


def archived_path(code, rows, cutoff):
    points, sources = [], set()
    for row in rows:
        end = _datetime(row['end'])
        if end.date() != cutoff.date() or end > cutoff or row.get('data_quality_status') == 'tainted':
            continue
        if row.get('data_quality_status') != 'verified' and not identity_matches(row, code):
            continue
        if row.get('corporate_action') or row.get('suspended') or row.get('no_price_limit'):
            continue
        points.append({'sourceTime': end.isoformat(), 'price': row['close'],
                       'amount': row.get('amount_cny'), 'volume': row.get('volume_shares')})
        sources.add(row.get('source') or '分钟归档')
    if not points:
        return {}
    last = _datetime(points[-1]['sourceTime'])
    boundary = cutoff
    if 690 <= cutoff.hour * 60 + cutoff.minute <= 780:
        boundary = cutoff.replace(hour=11, minute=30)
    elif cutoff.hour >= 15:
        boundary = cutoff.replace(hour=15, minute=0)
    if (boundary - last).total_seconds() > 120:
        return {}
    return _path(points, ' / '.join(sorted(sources)))


def provider_path(payload, symbol, cutoff):
    root = (payload.get('data') or {}).get(symbol) or {}
    qt = (root.get('qt') or {}).get(symbol) or []
    if len(qt) < 31 or qt[2] != symbol[2:]:
        return {}
    try:
        quote_time = datetime.strptime(qt[30], '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
        rows = parse_tencent_intraday(payload, symbol[2:], market_symbol=symbol)
    except (TypeError, ValueError):
        return {}
    if cutoff.date() != quote_time.date():
        return {}
    minute = timedelta(minutes=1)
    normalized, _ = _normalize(rows, min(cutoff, quote_time))
    points = []
    for start, row in sorted(normalized.items()):
        end = start + minute
        if start.date() != cutoff.date() or not bar_session(end):
            continue
        point = {'sourceTime': end.isoformat(), 'price': row['price']}
        prev = normalized.get(start - minute)
        if prev and bar_session(start) == bar_session(end):
            for field, key in [('amount', 'cumulative_amount_cny'), ('volume', 'cumulative_volume_shares')]:
                if row[key] is not None and prev[key] is not None and row[key] >= prev[key]:
                    point[field] = row[key] - prev[key]
        points.append(point)
    path = _path(points, '腾讯分时')
    if path:
        path.update(code=qt[2], name=qt[1], providerTime=quote_time.isoformat())
        try:
            previous = float(qt[4])
            if 0 < previous < float('inf'):
                path['previousClose'] = previous
        except (TypeError, ValueError):
            pass
    return path


def fetch_strategy_paths(codes, board, cutoff, archived, deadline):
    """Cover the index and representative peers first, then missing candidate paths."""
    from itertools import zip_longest
    candidates = set(codes)
    themes = [theme for theme in board.get('themes', [])
              if candidates.intersection(theme.get('memberCodes') or [])]
    themes.sort(key=lambda theme: (not theme.get('selected'), str(theme.get('id'))))
    peers = [sorted(set(str(code) for code in theme.get('memberCodes', ()))) for theme in themes]
    # Interleave groups in stable membership order. Selection is independent of returns.
    first_peers = [code for group in zip_longest(*(members[:8] for members in peers))
                   for code in group if code]
    symbols = ['sh000300']
    ordered = (code for pair in zip_longest(codes, first_peers) for code in pair if code)
    for code in dict.fromkeys(ordered):
        if not re.fullmatch(r'(60|68|00|30)\d{4}', code) or code in archived:
            continue
        symbols.append(('sh' if code.startswith('6') else 'sz') + code)

    def fetch(symbol):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return symbol, {}
        response = requests.get('https://web.ifzq.gtimg.cn/appstock/app/minute/query',
                                params={'code': symbol}, timeout=min(2.5, remaining))
        response.raise_for_status()
        return symbol, provider_path(response.json(), symbol, cutoff)

    results, _ = bounded_fetch_map(fetch, symbols, workers=8, timeout=max(0, deadline-monotonic()))
    series = dict(results)
    benchmark = series.pop('sh000300', {}) or {'status': 'unavailable', 'code': '000300', 'name': '沪深300'}
    return {'cutoffAt': cutoff.isoformat(), 'benchmark': benchmark,
            'providerPlanned': len(symbols) - 1, 'providerObserved': sum(bool(path) for path in series.values()),
            'stocks': {symbol[2:]: path for symbol, path in series.items() if path}}
