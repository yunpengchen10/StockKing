"""Same-provider cumulative fund-flow discovery before bounded deep research.

These observations allocate research capacity only. Provider large-order
classification is not institutional money, a 30-minute path, or entry approval.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import re
from time import monotonic
from zoneinfo import ZoneInfo

VERSION = 'fund-flow-discovery-v1'
SOURCE = 'eastmoney:clist-f62'
SCOPE = 'all_shanghai_shenzhen_a_shares'
SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_AGE_SECONDS = 30
PAGE_SIZE = 100  # Provider caps even pz=6000 at 100; verified by read-only probe.
MAX_PAGES = 100
FLOW_MEANING = ('东财大单分类当日累计净额，仅供资金板块/个股发现；'
                '不代表机构账户资金、近30分钟持续流入、买点或最终入选资格')


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return value.astimezone(SHANGHAI) if value.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh(value, now):
    stamp = _at(value)
    return bool(stamp and now and stamp.date() == now.date()
                and 0 <= (now - stamp).total_seconds() <= MAX_AGE_SECONDS)


def _valid_row(row, now):
    flow, amount = _number(row.get('main_net_flow')), _number(row.get('amount_cny'))
    return bool(re.fullmatch(r'(?:60|68|00|30)\d{4}', str(row.get('code') or ''))
                and row.get('source') and _fresh(row.get('source_time'), now)
                and flow is not None and amount is not None and amount > 0
                and abs(flow) <= amount)


def _normalize_provider_row(row):
    # f124 is the provider's row update epoch, NOT our request completion time.
    epoch = _number(row.get('f124'))
    try:
        stamp = datetime.fromtimestamp(epoch, SHANGHAI).isoformat() if epoch and epoch > 0 else None
    except (OverflowError, OSError, ValueError):
        stamp = None
    return {'code': str(row.get('f12') or ''), 'name': row.get('f14'),
            'main_net_flow': _number(row.get('f62')), 'amount_cny': _number(row.get('f6')),
            'source': SOURCE, 'source_time': stamp}


def fetch_flow_snapshot(cutoff, deadline, *, getter=None):
    """Read all stable code-ordered pages under one monotonic deadline.

    ``cutoff`` records the request's starting decision context. Rows retain f124
    and are checked against completion time here and the actual decision time
    again in build_flow_discovery. No partially paged list is returned as usable.
    getter is an offline test seam with requests.get's interface.
    """
    from .minute_history import bounded_fetch_map
    from .sector_fund_evidence import _fund_request

    cutoff = _at(cutoff)
    result = {'version': VERSION, 'status': 'unavailable', 'rows': [], 'source': SOURCE,
              'scope': SCOPE, 'requestedCutoffAt': cutoff.isoformat() if cutoff else None,
              'sourceTimeMeaning': 'f124 provider row update epoch; fund-specific update time is not separately supplied',
              'coverage': {'expectedCount': None, 'receivedCount': 0, 'validCount': 0, 'complete': False},
              'sourceTimeRange': [], 'reasonCodes': [], 'meaning': FLOW_MEANING}
    if cutoff is None or _number(deadline) is None or deadline <= monotonic():
        result['reasonCodes'] = ['invalid_cutoff_or_exhausted_deadline']
        return result

    def page_data(host, page):
        if monotonic() >= deadline:
            raise TimeoutError('fund discovery shared deadline')
        response = _fund_request(host + 'clist/get', getter=getter, deadline=deadline,
            params={'pn': str(page), 'pz': str(PAGE_SIZE), 'po': '0', 'np': '1',
                    'fltt': '2', 'invt': '2', 'fid': 'f12',
                    'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
                    'fields': 'f12,f14,f62,f6,f124'})
        response.raise_for_status()
        response.encoding = 'utf-8'
        payload = response.json()
        data = payload.get('data') if isinstance(payload, dict) else None
        if payload.get('rc') != 0 or not isinstance(data, dict):
            raise ValueError('fund snapshot schema unavailable')
        total, rows = data.get('total'), data.get('diff')
        if isinstance(rows, dict):
            rows = list(rows.values())
        if (isinstance(total, bool) or not isinstance(total, int) or total <= 0
                or not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows)):
            raise ValueError('fund snapshot page malformed')
        return page, total, rows

    # At most two first-page attempts; never restart pagination or loop retries.
    first, host = None, None
    errors = []
    for host in ('https://70.push2.eastmoney.com/api/qt/', 'https://17.push2.eastmoney.com/api/qt/'):
        if monotonic() >= deadline:
            break
        try:
            first = page_data(host, 1)
            break
        except Exception as exc:
            errors.append(type(exc).__name__)
    if first is None:
        result['reasonCodes'] = ['fund_snapshot_source_unavailable']
        result['errors'] = errors
        return result
    result['sourceUrl'] = host + 'clist/get'
    _, expected, first_rows = first
    result['coverage']['expectedCount'] = expected
    pages = math.ceil(expected / PAGE_SIZE)
    if pages > MAX_PAGES or len(first_rows) != min(PAGE_SIZE, expected):
        result['reasonCodes'] = ['unexpected_page_size_or_page_limit']
        return result
    completed, missing = bounded_fetch_map(lambda page: page_data(host, page), range(2, pages + 1),
        workers=2, timeout=max(0, deadline - monotonic()))
    collected, seen = [], set()
    failure = 'incomplete_pagination' if missing else None
    for page, total, rows in [first, *completed]:
        if total != expected or len(rows) != min(PAGE_SIZE, expected - (page - 1) * PAGE_SIZE):
            failure = 'changing_total_or_incomplete_page'
        for row in rows:
            code = str(row.get('f12') or '')
            if not re.fullmatch(r'\d{6}', code) or code in seen:
                failure = 'duplicate_or_invalid_security_code'
            seen.add(code)
            collected.append(_normalize_provider_row(row))
    result['coverage']['receivedCount'] = len(seen)
    if failure or len(seen) != expected:
        result['reasonCodes'] = [failure or 'incomplete_pagination']
        result['missingPages'] = missing
        return result
    completed_at = datetime.now(SHANGHAI)
    valid = [row for row in collected if _valid_row(row, completed_at)]
    stamps = [row['source_time'] for row in collected if row['source_time']]
    result.update(rows=collected, status='observed' if valid else 'unavailable',
                  checkedAt=completed_at.isoformat(), sourceTimeRange=[min(stamps), max(stamps)] if stamps else [])
    result['coverage'].update(complete=True, validCount=len(valid))
    if not valid:
        result['reasonCodes'] = ['no_fresh_valid_fund_rows']
    return result


def build_flow_discovery(snapshot_rows, membership, peers, now):
    """Pure same-provider stock/sector observations, using complete denominators.

    membership and peers use prepare_sector_membership's contract. Only a
    membershipComplete=True catalogue with an exact membershipCount supplies a
    denominator. Sparse observed quotes never become the constituent universe.
    """
    now = _at(now)
    if now is None:
        return {}
    rows = [row for row in snapshot_rows or [] if isinstance(row, dict)]
    # Reject mixtures even if both vendors describe their estimates as "main".
    sources = {row.get('source') for row in rows if row.get('source')}
    if len(sources) != 1:
        return {}
    source = next(iter(sources))
    counts = Counter(str(row.get('code') or '') for row in rows)
    valid = {str(row['code']): row for row in rows
             if counts[str(row.get('code') or '')] == 1 and _valid_row(row, now)}
    sectors = {}
    for code, stock in valid.items():
        sector = (membership or {}).get(code) or {}
        board = sector.get('code')
        member_list = (peers or {}).get(board)
        if (sector.get('membershipComplete') is not True or not isinstance(member_list, (list, tuple))
                or not member_list or len(set(member_list)) != len(member_list)
                or sector.get('membershipCount') != len(member_list) or code not in member_list):
            continue
        # Same board metadata must agree, rather than choosing a convenient denominator.
        identity = (tuple(sorted(member_list)), sector.get('membershipSource'), sector.get('classification'))
        previous = sectors.get(board)
        if previous is not None and previous['identity'] != identity:
            previous['conflict'] = True
            continue
        if previous is not None:
            previous['codes'].append(code)
            continue
        sectors[board] = {'identity': identity, 'codes': [code], 'conflict': False,
                         'members': list(member_list), 'metadata': dict(sector)}
    result = {}
    for sector in sectors.values():
        if sector['conflict']:
            continue
        for code in sector['codes']:
            row = valid[code]
            if row['main_net_flow'] <= 0:
                continue
            # A candidate cannot provide its own positive industry evidence.
            # Coverage, normalized flow, breadth, concentration and source times
            # all refer to OTHER members of the complete provider catalogue.
            member_list = [c for c in sector['members'] if c != code]
            available = [valid[c] for c in member_list if c in valid]
            if not member_list or len(available) < 3 or len(available) / len(member_list) < .8:
                continue
            positive = [float(x['main_net_flow']) for x in available if x['main_net_flow'] > 0]
            positive_total = sum(positive)
            amount = sum(float(x['amount_cny']) for x in available)
            net = sum(float(x['main_net_flow']) for x in available)
            if net <= 0 or amount <= 0:
                continue
            stamps = [x['source_time'] for x in available]
            meta = sector['metadata']
            p = {'code': meta['code'], 'name': meta.get('name'), 'excludedSelf': True,
                'catalogueMemberCount': len(sector['members']),
                'memberCount': len(member_list), 'observedCount': len(available),
                'coveragePct': 100 * len(available) / len(member_list),
                'missingCodes': [c for c in member_list if c not in valid],
                'mainNetFlowCny': net, 'amountCny': amount, 'netFlowRatio': net / amount,
                'positiveBreadthPct': 100 * len(positive) / len(available),
                'topPositiveSharePct': 100 * max(positive) / positive_total if positive_total else None,
                'positiveFlowHhi': sum((value / positive_total) ** 2 for value in positive) if positive_total else None,
                'source': source, 'sourceTimeRange': [min(stamps), max(stamps)],
                'membershipSource': meta.get('membershipSource'),
                'membershipComplete': True, 'classification': meta.get('classification'),
                'meaning': '完整成分剔除自身后的真实同行分母；同源当日累计资金覆盖、净额/成交额、正流入广度和集中度，非分钟持续流'}
            result[code] = {'version': VERSION, 'code': code, 'name': row.get('name'),
                'status': 'observed', 'researchOnly': True, 'discoveryOnly': True,
                'source': source, 'sourceTime': row['source_time'], 'evaluatedAt': now.isoformat(),
                'stock': {'mainNetFlowCny': row['main_net_flow'], 'amountCny': row['amount_cny'],
                          'netFlowRatio': row['main_net_flow'] / row['amount_cny']},
                'sector': p, 'meaning': FLOW_MEANING,
                'validationStatus': 'unvalidated_discovery_rules'}
    return result


def allocate_flow_discovery(rows, legacy, limit, evidence, now):
    """Reserve up to half of the unchanged queue budget, with sector rotation."""
    now = _at(now)
    by_code = {str(row['code']): row for row in rows}
    capacity = min(limit, len(by_code))
    packets = {}
    for code, packet in (evidence or {}).items():
        if not isinstance(packet, dict) or str(code) not in by_code:
            continue
        sector, stock = packet.get('sector') or {}, packet.get('stock') or {}
        stamps = sector.get('sourceTimeRange') or []
        if (packet.get('version') != VERSION or packet.get('code') != code
                or packet.get('status') != 'observed' or packet.get('discoveryOnly') is not True
                or packet.get('researchOnly') is not True or not packet.get('source')
                or sector.get('source') != packet['source'] or len(stamps) != 2
                or not all(_fresh(stamp, now) for stamp in [packet.get('sourceTime'), *stamps])
                or (_number(stock.get('netFlowRatio')) or 0) <= 0
                or (_number(sector.get('netFlowRatio')) or 0) <= 0
                or (_number(sector.get('observedCount')) or 0) < 3
                or (_number(sector.get('coveragePct')) or 0) < 80):
            continue
        packets[code] = packet
    budget = min(capacity // 2, len(packets))
    if not budget:
        return legacy
    groups = {}
    for code, packet in packets.items():
        groups.setdefault(packet['sector']['code'], []).append(code)
    for codes in groups.values():
        codes.sort(key=lambda code: (-packets[code]['stock']['netFlowRatio'], code))
    boards = sorted(groups, key=lambda board: (
        -packets[groups[board][0]]['sector']['netFlowRatio'],
        -packets[groups[board][0]]['sector']['positiveBreadthPct'],
        packets[groups[board][0]]['sector']['topPositiveSharePct'], board))
    selected = []
    while len(selected) < budget:
        for board in boards:
            if groups[board] and len(selected) < budget:
                selected.append(groups[board].pop(0))
    flow_codes = set(selected)
    legacy_by_code = {str(row['code']): row for row in legacy}
    for row in [*legacy, *rows]:
        code = str(row['code'])
        if len(selected) >= capacity:
            break
        if code not in selected:
            selected.append(code)
    result = []
    for code in selected:
        row = legacy_by_code.get(code, by_code[code])
        packet = dict(row.get('discoveryEvidence') or {})
        packet.update(researchOnly=True, researchBudget=capacity, fundFlowBudget=budget)
        if code in packets:
            packet['fundFlowDiscovery'] = packets[code]
            packet['channels'] = list(dict.fromkeys([*(packet.get('channels') or []), 'fund_flow']))
        if code in flow_codes:
            packet['allocatedBy'] = 'fund_flow'
        result.append({**row, 'discoveryEvidence': packet})
    return result
