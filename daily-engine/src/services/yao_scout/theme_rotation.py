"""Point-in-time concept leadership; industry labels never supply theme membership.

Concept amounts overlap across boards. Their normalized share is an attention
measure, NOT a share of exchange turnover. Relative strength uses the concept
cross-section median, NOT an index return. All thresholds are research rules.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from statistics import median
from threading import local
from time import monotonic
from zoneinfo import ZoneInfo
from uuid import uuid4

VERSION = 'theme-first-v1'
SOURCE = 'eastmoney:concept-clist-t3'
FALLBACK_SOURCE = 'sina:concept-members+verified-member-quotes'
CATALOGUE_FILTER = 'm:90 t:3 f:!50'
SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_AGE_SECONDS = 300
MAXIMUM_THEMES = 3
MEMBER_FETCH_LIMIT = 6
PAGE_SIZE = 100
MAX_PAGES = 15
NON_NARRATIVE = {'融资融券', '沪股通', '深股通', '沪深股通', '转债标的',
                 '上证180_', '上证50_', 'HS300_', 'MSCI中国', '富时罗素', '标普概念'}
_http_local = local()


def _theme_public_get(url, **kwargs):
    # Respect the user's proxy/environment settings. Use the unpatched request
    # entry point so no unrelated authentication call or random sleep can evade
    # this scan's deadline. The shared transport still bounds actual I/O to two.
    import requests
    from src.patches.eastmoney_patch import original_request
    session = getattr(_http_local, 'session', None)
    if session is None:
        session = requests.Session()
        _http_local.session = session
    return original_request(session, 'GET', url, **kwargs)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.astimezone(SHANGHAI) if value.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _epoch(value):
    try:
        value = _number(value)
        return datetime.fromtimestamp(value, SHANGHAI).isoformat() if value and value > 0 else None
    except (ValueError, OSError, OverflowError):
        return None


def _fresh(value, now, known_at):
    stamp = _at(value)
    return bool(now and known_at and stamp and stamp <= known_at <= now
                and stamp.date() == now.date() and 0 <= (now - stamp).total_seconds() <= MAX_AGE_SECONDS)


def _midrank(value, peers):
    return (sum(item < value for item in peers) + .5 * sum(item == value for item in peers)) / len(peers)


def _invalid_quote_evidence(*packets):
    """A recent clock cannot rehabilitate an explicitly rejected observation."""
    rejected_issues = {'cross_source_price_conflict', 'stale_quote', 'different_market_date',
                       'code_mismatch', 'quote_identity_mismatch', 'inconsistent_price_fields',
                       'tainted_current_market_data'}
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        metadata = packet.get('public_quote_metadata')
        evidence = [packet, metadata] if isinstance(metadata, dict) else [packet]
        for item in evidence:
            if any(item.get(key) not in (None, False, '', 0) for key in
                   ('stale', 'is_stale', 'conflict', 'price_conflict', 'tainted')):
                return True
            if item.get('status') in {'stale', 'conflict', 'invalid', 'tainted', 'missing_data'}:
                return True
            if item.get('quote_usable_for_current_price_check') is False:
                return True
            issues = item.get('issues') or []
            if isinstance(issues, str):
                issues = [issues]
            if isinstance(issues, (list, tuple, set)) and any(str(issue) in rejected_issues for issue in issues):
                return True
            cross_check = item.get('cross_source_check')
            if isinstance(cross_check, dict) and cross_check.get('prices_agree') is False:
                return True
    return False


def _normal_row(row):
    return {'id': str(row.get('f12') or ''), 'name': str(row.get('f14') or ''),
            'changePct': _number(row.get('f3')), 'amount': _number(row.get('f6')),
            'upCount': _number(row.get('f104')), 'downCount': _number(row.get('f105')),
            'flatCount': _number(row.get('f106')), 'sourceTime': _epoch(row.get('f124'))}


def fetch_theme_market(now, deadline, data_dir, include_ids=(), *, quote_rows=None, getter=None):
    """Try timestamped concept indices, then cached Sina members + real quotes."""
    start = monotonic()
    primary_deadline = min(deadline, start + 6) if quote_rows is not None else deadline
    packet = _fetch_eastmoney_theme_market(now, primary_deadline, data_dir, include_ids, getter=getter)
    if packet.get('status') == 'observed' and build_theme_board(packet, datetime.now(SHANGHAI)).get('status') == 'observed':
        return packet
    if quote_rows is None or monotonic() >= deadline:
        return packet
    fallback = _fetch_sina_theme_market(now, deadline, data_dir, quote_rows, getter=getter)
    fallback['primaryRequestErrors'] = packet.get('requestErrors', [])
    return fallback


def _fetch_eastmoney_theme_market(now, deadline, data_dir, include_ids=(), *, getter=None):
    """Complete concept catalogue plus at most nine concept membership lists.

    Two concurrent HTTP calls, <= 3 s each and one shared monotonic deadline.
    A previous cache never substitutes for current evidence. data_dir remains an
    API seam: the scan's feedback store owns immutable history and persistence.
    """
    from .minute_history import bounded_fetch_map
    from .sector_fund_evidence import _fund_request

    now = _at(now)
    packet = {'version': VERSION, 'status': 'unavailable', 'source': SOURCE,
              'kind': 'concept', 'requestedAt': now.isoformat() if now else None,
              'catalogueFilter': CATALOGUE_FILTER,
              'checkedAt': None, 'rows': [], 'members': {},
              'coverage': {'expectedCount': None, 'receivedCount': 0, 'complete': False}, 'gaps': []}
    if not now or _number(deadline) is None or deadline <= monotonic():
        packet['gaps'].append('题材请求截止时间已到或决策时间无效')
        return packet

    def page(host, fs, number):
        if monotonic() >= deadline:
            raise TimeoutError('theme shared deadline')
        response = _fund_request(host + 'clist/get', getter=getter or _theme_public_get,
            deadline=min(deadline, monotonic() + 3),
            params={'pn': str(number), 'pz': str(PAGE_SIZE), 'po': '0', 'np': '1', 'fltt': '2',
                    'invt': '2', 'fid': 'f12', 'fs': fs,
                    'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
                    'fields': 'f12,f14,f2,f3,f6,f104,f105,f106,f124'})
        response.raise_for_status()
        response.encoding = 'utf-8'
        payload = response.json()
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get('rc') != 0:
            raise ValueError('theme response schema')
        rows, total = data.get('diff'), data.get('total')
        if isinstance(rows, dict):
            rows = list(rows.values())
        if (isinstance(total, bool) or not isinstance(total, int) or total <= 0
                or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)):
            raise ValueError('theme response rows')
        return number, total, rows

    def complete_parts(parts, expected, missing):
        rows = []
        valid = not missing
        for pn, total, part in parts:
            valid &= total == expected and len(part) == min(PAGE_SIZE, expected - (pn - 1) * PAGE_SIZE)
            rows.extend(part)
        ids = [str(row.get('f12') or '') for row in rows]
        return rows, bool(valid and len(rows) == expected and len(set(ids)) == expected)

    first = None
    for host in ('https://79.push2.eastmoney.com/api/qt/', 'https://17.push2.eastmoney.com/api/qt/'):
        if monotonic() >= deadline:
            break
        try:
            first = page(host, CATALOGUE_FILTER, 1)
            break
        except Exception as exc:
            packet.setdefault('requestErrors', []).append(type(exc).__name__)
            continue
    if first is None:
        packet['gaps'].append('公开概念目录读取失败或超时')
        return packet
    expected = first[1]
    packet['sourceUrl'] = host + 'clist/get'
    packet['coverage']['expectedCount'] = expected
    pages = math.ceil(expected / PAGE_SIZE)
    if pages > MAX_PAGES:
        packet['gaps'].append('概念目录超过预设分页上限')
        return packet
    parts, missing = bounded_fetch_map(lambda pn: page(host, CATALOGUE_FILTER, pn), range(2, pages + 1),
                                      workers=2, timeout=max(0, deadline - monotonic()))
    rows, complete = complete_parts([first, *parts], expected, missing)
    packet['rows'] = [_normal_row(row) for row in rows]
    packet['checkedAt'] = datetime.now(SHANGHAI).isoformat()
    packet['coverage'].update(receivedCount=len(rows), complete=complete)
    if not complete:
        packet['gaps'].append('概念目录分页不完整或重复，不能确定全市场题材排名')
        return packet
    packet['status'] = 'observed'
    preliminary = build_theme_board(packet, _at(packet['checkedAt']))
    desired = [row['id'] for row in preliminary['themes'] if row.get('marketEligible')][:MEMBER_FETCH_LIMIT]
    known_ids = {row['id'] for row in packet['rows']}
    desired = list(dict.fromkeys([*desired, *(str(value) for value in include_ids if str(value) in known_ids)]))[:9]
    first_members, absent = bounded_fetch_map(lambda tid: (tid, page(host, 'b:' + tid, 1)), desired,
                                              workers=2, timeout=max(0, deadline - monotonic()))
    tasks = [(tid, pn) for tid, part in first_members if math.ceil(part[1] / PAGE_SIZE) <= MAX_PAGES
             for pn in range(2, math.ceil(part[1] / PAGE_SIZE) + 1)]
    further, absent_pages = bounded_fetch_map(lambda task: (task[0], page(host, 'b:' + task[0], task[1])), tasks,
                                              workers=2, timeout=max(0, deadline - monotonic()))
    observed_at = datetime.now(SHANGHAI)
    for tid, first_part in first_members:
        parts = [first_part, *(part for board_id, part in further if board_id == tid)]
        missing_parts = [pn for board_id, pn in absent_pages if board_id == tid]
        member_rows, full = complete_parts(parts, first_part[1], missing_parts)
        member_codes = [str(row.get('f12') or '') for row in member_rows]
        full &= all(re.fullmatch(r'\d{6}', code) for code in member_codes)
        packet['members'][tid] = {'source': SOURCE, 'knownAt': observed_at.isoformat(),
            'expectedCount': first_part[1], 'complete': full,
            'rows': [{'code': str(row.get('f12') or ''), 'sourceTime': _epoch(row.get('f124')),
                      'name': str(row.get('f14') or ''), 'price': _number(row.get('f2')),
                      'source': SOURCE, 'changePct': _number(row.get('f3'))} for row in member_rows]}
    packet['checkedAt'] = observed_at.isoformat()
    if absent or absent_pages:
        packet['gaps'].append('部分主线候选的成分读取超时；缺失映射不进入主线推荐')
    return packet


def _fetch_sina_theme_market(now, deadline, data_dir, quote_rows, *, getter=None):
    """Persist complete membership only; never persist/retime a price as fresh.

    The Sina catalogue's counts are capped at 100. Each actual node count and
    every stable code-ordered page must agree before the membership is usable.
    Missing concepts are warmed incrementally under the same scan deadline.
    """
    from .minute_history import bounded_fetch_map
    from .sector_fund_evidence import _fund_request

    now = _at(now)
    packet = {'version': VERSION, 'kind': 'concept', 'source': FALLBACK_SOURCE,
              'status': 'unavailable', 'checkedAt': None, 'rows': [], 'members': {},
              'coverage': {'expectedCount': None, 'receivedCount': 0, 'complete': False},
              'returnMeaning': '同题材真实成员当日涨幅等权均值；非供应商概念指数收益', 'gaps': []}
    if now is None or data_dir is None or monotonic() >= deadline:
        packet['gaps'].append('概念成员缓存路径或本轮时间预算不可用')
        return packet
    cache_path = Path(data_dir) / 'sina-theme-members-v1.json'
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        if not isinstance(cache, dict) or cache.get('version') != 1:
            cache = {}
    except (OSError, ValueError, TypeError):
        cache = {}

    def cache_current(value):
        stamp = _at(value)
        return bool(stamp and stamp.date() == now.date() and stamp <= now)

    def request(url, params):
        response = _fund_request(url, params=params, getter=getter or _theme_public_get,
                                 deadline=min(deadline, monotonic() + 3))
        response.raise_for_status()
        return response

    catalogue = cache.get('catalogue') if cache_current(cache.get('catalogueKnownAt')) else None
    if (not isinstance(catalogue, list) or len(catalogue) < 3
            or any(not isinstance(item, dict) or not re.fullmatch(r'gn_[A-Za-z0-9_]+', str(item.get('id') or ''))
                   or not item.get('name') for item in catalogue)
            or len({item['id'] for item in catalogue}) != len(catalogue)):
        catalogue = None
    if not catalogue:
        try:
            response = request('https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php', {'param': 'class'})
            response.encoding = 'gbk'
            raw = response.text
            decoded = json.loads(raw[raw.index('{'):].strip().rstrip(';'))
            catalogue = []
            for tid, value in decoded.items():
                parts = str(value).split(',')
                if not re.fullmatch(r'gn_[A-Za-z0-9_]+', tid) or len(parts) < 3 or parts[0] != tid or not parts[1]:
                    raise ValueError('Sina concept catalogue identity')
                catalogue.append({'id': tid, 'name': parts[1]})
            if len(catalogue) < 3 or len({item['id'] for item in catalogue}) != len(catalogue):
                raise ValueError('Sina concept catalogue incomplete')
            cache = {'version': 1, 'catalogueKnownAt': datetime.now(SHANGHAI).isoformat(),
                     'catalogue': catalogue, 'members': {}}
        except Exception as exc:
            packet['gaps'].append('新浪真实概念目录读取失败：' + type(exc).__name__)
            return packet
    cached_members = cache.get('members') if isinstance(cache.get('members'), dict) else {}
    usable = {tid: item for tid, item in cached_members.items() if isinstance(item, dict)
              and cache_current(item.get('knownAt')) and item.get('complete') is True
              and item.get('expectedCount') == len(item.get('codes', []))
              and len(set(item.get('codes', []))) == len(item.get('codes', []))
              and all(re.fullmatch(r'\d{6}', str(code)) for code in item.get('codes', []))}
    pending = [item['id'] for item in catalogue if item['id'] not in usable]
    base = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'

    def get_members(tid):
        response = request(base + 'getHQNodeStockCount', {'node': tid})
        raw_count = response.json()
        if isinstance(raw_count, bool) or not re.fullmatch(r'\d+', str(raw_count)):
            raise ValueError('concept member count unavailable')
        expected = int(raw_count)
        if expected < 0 or expected > 3000:
            raise ValueError('concept member count outside bounded scope')
        collected = []
        for pn in range(1, math.ceil(expected / 100) + 1):
            response = request(base + 'getHQNodeData', {'page': str(pn), 'num': '100', 'sort': 'symbol',
                                'asc': '1', 'node': tid, 'symbol': '', '_s_r_a': 'page'})
            response.encoding = 'utf-8'
            rows = response.json()
            if not isinstance(rows, list) or len(rows) != min(100, expected - (pn - 1) * 100):
                raise ValueError('concept members pagination incomplete')
            for item in rows:
                code = str(item.get('code') or '')
                symbol = str(item.get('symbol') or '')
                if not re.fullmatch(r'\d{6}', code) or symbol not in {'sh' + code, 'sz' + code, 'bj' + code}:
                    raise ValueError('concept member identity mismatch')
                collected.append(code)
        if len(collected) != len(set(collected)):
            raise ValueError('concept members duplicate')
        return tid, {'complete': True, 'expectedCount': expected, 'codes': sorted(collected),
                     'knownAt': datetime.now(SHANGHAI).isoformat()}

    completed, missing = bounded_fetch_map(get_members, pending, workers=2, timeout=max(0, deadline - monotonic()))
    usable.update(dict(completed))
    cache['members'] = usable
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(cache_path.name + '.' + uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(temporary, cache_path)
    except OSError:
        packet['gaps'].append('概念成员缓存写入失败，下轮须重新采集')
    checked = datetime.now(SHANGHAI)
    packet['checkedAt'] = checked.isoformat()
    packet['catalogueKnownAt'] = cache.get('catalogueKnownAt')
    complete = not missing and all(item['id'] in usable for item in catalogue)
    packet['membershipWarmup'] = {'expectedCount': len(catalogue), 'completeCount': len(usable), 'complete': complete}
    # The catalogue itself is complete. Missing/inconsistent member maps remain
    # explicit invalid rows in the FULL catalogue denominator below; an 80%
    # valid-concept floor applies before any market ranking is available.
    packet['coverage'].update(expectedCount=len(catalogue), receivedCount=len(catalogue), complete=True)
    if not complete:
        packet['gaps'].append(f'全概念目录{len(catalogue)}个，已核验完整成分{len(usable)}个；缺失或总数冲突概念仍计入覆盖分母，低于80%不排行')
    quotes, repeated = {}, set()
    for row in quote_rows or []:
        if not isinstance(row, dict):
            continue
        quote = row.get('market_quote') if isinstance(row.get('market_quote'), dict) else row
        if _invalid_quote_evidence(row, quote):
            continue
        code = str(row.get('code') or '')
        if not re.fullmatch(r'\d{6}', code) or str(quote.get('code') or '') != code:
            continue
        if code in quotes:
            repeated.add(code)
            continue
        stamp = quote.get('source_time')
        price, amount = _number(quote.get('price')), _number(quote.get('amount_cny'))
        change = _number(quote.get('change_pct'))
        if (not quote.get('source') or not _fresh(stamp, checked, checked) or price is None or price <= 0
                or amount is None or amount < 0 or change is None):
            continue
        quotes[code] = {'code': code, 'name': str(quote.get('name') or row.get('name') or ''),
                       'price': price, 'amount': amount, 'changePct': change,
                       'sourceTime': stamp, 'source': quote['source']}
    for code in repeated:
        quotes.pop(code, None)
    for item in catalogue:
        tid, identity = item['id'], usable.get(item['id'])
        if identity is None:
            packet['rows'].append({'id': tid, 'name': item['name'], 'changePct': None,
                                   'amount': None, 'upCount': None, 'downCount': None,
                                   'flatCount': None, 'sourceTime': None})
            continue
        observed = [quotes[code] for code in identity['codes'] if code in quotes]
        valid = len(observed) >= 3 and len(observed) / identity['expectedCount'] >= .8
        packet['rows'].append({'id': tid, 'name': item['name'],
            'changePct': sum(row['changePct'] for row in observed) / len(observed) if valid else None,
            'amount': sum(row['amount'] for row in observed) if valid else None,
            'upCount': sum(row['changePct'] > 0 for row in observed),
            'downCount': sum(row['changePct'] < 0 for row in observed),
            'flatCount': sum(row['changePct'] == 0 for row in observed),
            'breadthDenominator': identity['expectedCount'],
            'sourceTime': min((_at(row['sourceTime']) for row in observed)).isoformat() if valid else None})
        packet['members'][tid] = {'source': FALLBACK_SOURCE, 'membershipSource': 'sina:gn-concept-nodes',
            'membershipKnownAt': identity['knownAt'], 'knownAt': checked.isoformat(),
            'expectedCount': identity['expectedCount'], 'complete': True,
            'rows': [quotes.get(code, {'code': code, 'sourceTime': None, 'changePct': None}) for code in identity['codes']]}
    packet['status'] = 'observed'
    packet['quoteSources'] = sorted({item['source'] for item in quotes.values()})
    return packet


def _history_rows(history, now):
    """Only earlier frozen boards; never allow same-day repeats to count as days."""
    dated = {}
    for board in history or []:
        if not isinstance(board, dict) or board.get('version') != VERSION or board.get('status') != 'observed':
            continue
        stamp = _at(board.get('asOf'))
        source_time = _at(board.get('sourceTime'))
        known_at = _at(board.get('knownAt') or board.get('asOf'))
        if (not stamp or stamp >= now or stamp.date() >= now.date() or (now - stamp).days > 14
                or not known_at or known_at > now
                or not source_time or source_time > stamp
                or any(not _at(item.get('sourceTime') or source_time)
                       or _at(item.get('sourceTime') or source_time) > stamp
                       for item in board.get('themes', []))):
            continue
        if stamp.date() not in dated or stamp > _at(dated[stamp.date()]['asOf']):
            dated[stamp.date()] = board
    return [dated[day] for day in sorted(dated, reverse=True)[:5]]


def build_theme_board(packet, now, history=None, feedback=None):
    """Rank then freeze themes before looking at individual stock outcomes."""
    now = _at(now)
    packet = packet if isinstance(packet, dict) else {}
    board = {'version': VERSION, 'status': 'unavailable', 'asOf': now.isoformat() if now else None,
             'source': packet.get('source'), 'sourceTime': None, 'themes': [],
             'returnMeaning': packet.get('returnMeaning', '供应商概念指数当日涨跌幅'),
             'selection': {'maximumThemes': MAXIMUM_THEMES, 'selectedIds': []},
             'amountShareMeaning': '有效概念累计成交额之和为分母；成分重叠，不是全市场成交占比',
             'relativeStrengthMeaning': '题材涨跌幅减有效概念涨跌幅中位数，不是指数超额',
             'policy': {'maximumAgeSeconds': MAX_AGE_SECONDS, 'minimumBreadthPct': 55,
                        'minimumCoveragePct': 80, 'maximumFeedbackPenalty': .1},
             'gaps': list(packet.get('gaps') or [])}
    known_at = _at(packet.get('checkedAt'))
    coverage = packet.get('coverage') or {}
    rows = [row for row in packet.get('rows', []) if isinstance(row, dict)]
    ids = [str(row.get('id') or '') for row in rows]
    if (not now or not known_at or known_at > now or packet.get('kind') != 'concept'
            or packet.get('status') != 'observed'
            or packet.get('source') not in {SOURCE, FALLBACK_SOURCE} or coverage.get('complete') is not True
            or coverage.get('expectedCount') != len(rows) or len(ids) != len(set(ids))
            or any(not re.fullmatch(r'BK\d{4}' if packet.get('source') == SOURCE else r'gn_[A-Za-z0-9_]+', tid) for tid in ids)):
        board['gaps'].append('题材目录、独立来源或可知时间不完整；不使用行业兜底')
        return board
    valid = []
    for row in rows:
        values = [_number(row.get(key)) for key in ('changePct', 'amount', 'upCount', 'downCount', 'flatCount')]
        change, amount, up, down, flat = values
        if (any(value is None for value in values) or not _fresh(row.get('sourceTime'), now, known_at)
                or amount <= 0 or min(up, down, flat) < 0 or up + down + flat < 3
                or any(value != int(value) for value in (up, down, flat)) or not row.get('name')):
            continue
        denominator = _number(row.get('breadthDenominator')) or (up + down + flat)
        if denominator < up + down + flat:
            continue
        valid.append({**row, 'changePct': change, 'amount': amount, 'breadthPct': 100 * up / denominator})
    board['coverage'] = {'expectedCount': len(rows), 'validCount': len(valid),
                         'pct': 100 * len(valid) / len(rows) if rows else 0, 'complete': len(valid) == len(rows)}
    if not rows or len(valid) / len(rows) < .8:
        board['gaps'].append('新鲜有效题材覆盖不足80%，不能确认热门主线')
        return board
    board['status'] = 'observed'
    board['sourceTime'] = min(row['sourceTime'] for row in valid)
    total_amount = sum(row['amount'] for row in valid)
    baseline = median(row['changePct'] for row in valid)
    history_rows = _history_rows(history, now)
    feedback = feedback if isinstance(feedback, dict) else {}
    feedback_at = _at(feedback.get('asOf') or feedback.get('knownAt'))
    adjustments = feedback.get('adjustments', {}) if feedback_at and feedback_at <= now else {}
    if not isinstance(adjustments, dict):
        adjustments = {}
    for row in valid:
        tid = row['id']
        membership = (packet.get('members') or {}).get(tid) or {}
        members = [item for item in membership.get('rows', []) if isinstance(item, dict)]
        member_codes = [str(item.get('code') or '') for item in members]
        member_known = _at(membership.get('knownAt'))
        complete = (membership.get('complete') is True and membership.get('source') == packet.get('source')
                    and member_known is not None and member_known <= now
                    and membership.get('expectedCount') == len(members) and len(members) >= 3
                    and len(member_codes) == len(set(member_codes))
                    and all(re.fullmatch(r'\d{6}', code) for code in member_codes))
        valid_members = sum(_fresh(item.get('sourceTime'), now, member_known)
                            and _number(item.get('changePct')) is not None for item in members)
        member_quotes = {str(item['code']): {'changePct': _number(item.get('changePct')),
                          'sourceTime': item.get('sourceTime'),
                          'name': str(item.get('name') or ''), 'price': _number(item.get('price')),
                          'source': item.get('source') or membership.get('source'),
                          'valid': bool(_fresh(item.get('sourceTime'), now, member_known)
                                        and _number(item.get('changePct')) is not None)} for item in members}
        coverage_pct = 100 * valid_members / len(members) if members else 0
        penalty = min(.1, max(0., _number(adjustments.get(tid)) or 0.))
        heat = sum(_midrank(row[key], [item[key] for item in valid]) for key in ('changePct', 'breadthPct', 'amount')) / 3
        relative = row['changePct'] - baseline
        eligible = row['changePct'] > 0 and row['breadthPct'] >= 55 and relative > 0 and row['name'] not in NON_NARRATIVE
        previous = [next((item for item in past.get('themes', []) if item.get('id') == tid), None) for past in history_rows]
        selected_days = sum(bool(item and item.get('selected')) for item in previous)
        was_selected = bool(previous and previous[0] and previous[0].get('selected'))
        stage = ('持续' if eligible and was_selected else '启动' if eligible else
                 '退潮' if was_selected and row['changePct'] <= 0 else '分歧' if was_selected else '观察')
        board['themes'].append({'id': tid, 'name': row['name'], 'rank': None,
            'heatScore': round(max(0, heat - penalty), 6), 'rawHeatScore': round(heat, 6),
            'feedbackPenalty': penalty, 'changePct': row['changePct'], 'breadthPct': round(row['breadthPct'], 4),
            'amountSharePct': round(100 * row['amount'] / total_amount, 4),
            'relativeStrengthPct': round(relative, 4), 'stage': stage, 'selected': False,
            'sourceTime': row['sourceTime'], 'memberCodes': sorted(set(member_codes)) if complete else [],
            'memberQuotes': member_quotes if complete else {},
            'marketEligible': eligible, 'membershipEligible': bool(complete and coverage_pct >= 80),
            'priorSelectedDays': selected_days,
            'coverage': {'expectedCount': membership.get('expectedCount'), 'receivedCount': len(members),
                         'validCount': valid_members, 'complete': bool(complete), 'pct': coverage_pct},
            'explanation': '涨幅、上涨广度与概念成交占比等权排序；每日反馈仅作有限扣分'})
    board['themes'].sort(key=lambda item: (-item['heatScore'], item['id']))
    market_candidates = [item for item in board['themes'] if item['marketEligible']]
    unverified = [item['id'] for item in market_candidates if not item['membershipEligible']]
    board['membershipAcquisition'] = {'maximumPrimaryRequests': MEMBER_FETCH_LIMIT,
        'marketEligibleCount': len(market_candidates), 'unverifiedCandidateIds': unverified}
    if unverified:
        board['gaps'].append(f'{len(unverified)}个强势题材尚缺完整有效成分证据，保留观察资格，不代表已排除')
    selected_members = set()
    for rank, theme in enumerate(board['themes'], 1):
        theme['rank'] = rank
        fingerprint = tuple(theme['memberCodes'])
        if (len(board['selection']['selectedIds']) < MAXIMUM_THEMES and theme['marketEligible']
                and theme['membershipEligible'] and fingerprint not in selected_members):
            theme['selected'] = True
            selected_members.add(fingerprint)
            board['selection']['selectedIds'].append(theme['id'])
    if not board['selection']['selectedIds']:
        board['gaps'].append('当前没有同时满足强度、广度及完整成分证据的热门主线')
    return board


def theme_for_code(board, code):
    """Return the highest frozen selected theme, never select after the outcome."""
    if not isinstance(board, dict) or board.get('status') != 'observed':
        return {'status': 'unavailable', 'selected': False, 'reason': '热门题材证据不可用'}
    code = str(code)
    themes = [theme for theme in board.get('themes', []) if theme.get('selected')
              and theme.get('membershipEligible') and code in theme.get('memberCodes', [])]
    if not themes:
        return {'status': 'unavailable', 'selected': False, 'reason': '不在已核验热门主线成分中'}
    theme = min(themes, key=lambda item: (item['rank'], item['id']))
    quote = theme.get('memberQuotes', {}).get(code) or {}
    change = _number(quote.get('changePct')) if quote.get('valid') else None
    peers = [item['changePct'] for item in theme.get('memberQuotes', {}).values() if item.get('valid')]
    return {**{key: value for key, value in theme.items() if key not in {'memberQuotes', 'memberCodes'}},
            'status': 'observed' if change is not None else 'unavailable',
            'selected': change is not None, 'boardAsOf': board.get('asOf'), 'source': board.get('source'),
            'memberChangePct': change, 'memberSourceTime': quote.get('sourceTime'),
            'leaderRank': 1 + sum(value > change for value in peers) if change is not None else None,
            'memberCount': len(theme.get('memberCodes', [])), 'validMemberCount': len(peers),
            'themeRelativeStrengthPct': theme.get('relativeStrengthPct'),
            'relativeStrengthPct': round(change - theme['changePct'], 4) if change is not None else None}


def prioritize_theme_queue(rows, board, limit):
    """Allocate bounded research to frozen leaders first; others remain controls."""
    limit = max(0, int(limit))
    decorated, seen = [], set()
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict) or str(row.get('code') or '') in seen:
            continue
        code = str(row.get('code') or '')
        if not code:
            continue
        seen.add(code)
        theme = theme_for_code(board, code)
        primary = theme.get('selected') is True
        evidence = dict(row.get('discoveryEvidence') or {})
        evidence.update(theme=theme, themeRole='mainline' if primary else 'control')
        decorated.append((0 if primary else 1, theme.get('rank', 9999), theme.get('leaderRank') or 9999, index,
                          {**row, 'themeEvidence': theme, 'themeRole': evidence['themeRole'], 'discoveryEvidence': evidence}))
    mainlines = sorted((item for item in decorated if item[0] == 0), key=lambda item: item[:4])
    controls = [item for item in decorated if item[0] == 1]
    control_budget = min(len(controls), math.ceil(limit * .2)) if mainlines else min(limit, len(controls))
    selected = mainlines[:max(0, limit - control_budget)]
    selected.extend(controls[:max(0, limit - len(selected))])
    if len(selected) < limit:
        selected.extend(mainlines[len(selected):limit])
    return [item[-1] for item in selected]
