"""Pure, same-source money-flow windows. Vendor estimates are not account flows.

Amounts are CNY and ratios are fractions, not percentages. A negative observed
flow is valid evidence, not a data failure. No thresholds here grant a trade.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math

VERSION = 'same-window-fund-flow-v1'
TZ = timezone(timedelta(hours=8))
HORIZONS = (15, 30, 60)


def timestamp(value):
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at.astimezone(TZ) if at.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def sourced(value):
    return bool(isinstance(value, str) and value.strip()
                and not any(word in value.lower() for word in ('unknown', 'missing', '未记录', '未取得')))


def session(at):
    if at is None or at.second or at.microsecond:
        return None
    minute = at.hour * 60 + at.minute
    period = 'am' if 570 < minute <= 690 else 'pm' if 780 < minute <= 900 else None
    return (at.date(), period) if period else None


def series_rows(fund, cutoff):
    """Validate source identity and reject conflicting duplicates, never last-wins."""
    now = timestamp(cutoff)
    identity = tuple(fund.get(key) for key in ('provider', 'measurementVersion', 'source'))
    if (now is None or not fund.get('code') or not all(sourced(v) for v in identity)
            or fund.get('amountUnit') != 'CNY' or fund.get('normalizedTimeSemantics') != 'bar_end'):
        return {}, ['fund_identity_or_time_basis_missing']
    rows, conflicts = {}, set()
    for item in fund.get('cumulativeSeries') or []:
        if not isinstance(item, dict):
            continue
        at, net = timestamp(item.get('end')), finite(item.get('main_net_flow'))
        if (at is None or at > now or at.date() != now.date() or session(at) is None or net is None):
            continue
        if any(item.get(key) is not None and item[key] != fund.get(key)
               for key in ('code', 'provider', 'measurementVersion', 'source')):
            conflicts.add(at)
            continue
        components = {key: finite(item.get(key)) for key in
                      ('small_net_flow', 'medium_net_flow', 'large_net_flow', 'extra_large_net_flow')}
        if any(item.get(key) is not None and value is None for key, value in components.items()):
            conflicts.add(at)
            continue
        if (components['large_net_flow'] is not None and components['extra_large_net_flow'] is not None
                and abs(net-components['large_net_flow']-components['extra_large_net_flow']) > max(2, abs(net)*1e-5)):
            conflicts.add(at)
            continue
        normalized = {'end': at.isoformat(), 'main_net_flow': net, **components}
        if at in rows and rows[at] != normalized:
            conflicts.add(at)
        rows[at] = normalized
    for value in fund.get('conflictingTimes') or []:
        at = timestamp(value)
        if at is not None:
            conflicts.add(at)
    for at in conflicts:
        rows.pop(at, None)
    return rows, ['conflicting_fund_observations'] if conflicts else []


def flow_window(fund, cutoff, horizon=30):
    """Return one complete consecutive same-session cumulative-difference window."""
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError('horizon must be a positive integer')
    now, end = timestamp(cutoff), timestamp(fund.get('asOf'))
    packet = {'version': VERSION, 'status': 'unavailable', 'code': fund.get('code'),
              'asOf': end.isoformat() if end else None, 'windowStart': None,
              'horizonMinutes': horizon, 'provider': fund.get('provider'),
              'measurementVersion': fund.get('measurementVersion'), 'source': fund.get('source'),
              'flowKind': 'vendor_estimate', 'metrics': {}, 'reasons': []}
    if (now is None or end is None or end.date() != now.date()
            or not 0 <= (now-end).total_seconds() < 300 or session(end) is None):
        packet['reasons'].append('fund_window_end_missing_future_or_stale')
        return packet
    start = end-timedelta(minutes=horizon)
    packet['windowStart'] = start.isoformat()
    if session(start) != session(end):
        packet['reasons'].append('fund_window_crosses_session_boundary')
        return packet
    rows, reasons = series_rows(fund, now)
    stamps = [start+timedelta(minutes=index) for index in range(horizon+1)]
    if not all(at in rows for at in stamps):
        packet['reasons'] = reasons+['fund_window_missing_contiguous_observations']
        return packet
    nets = [rows[at]['main_net_flow'] for at in stamps]
    deltas = [right-left for left, right in zip(nets, nets[1:])]
    recent_n = min(5, horizon//2)
    recent = sum(deltas[-recent_n:]) if recent_n else None
    prior = sum(deltas[-2*recent_n:-recent_n]) if recent_n else None
    packet.update(status='observed', metrics={
        'netFlowCny': nets[-1]-nets[0], 'positiveMinuteFraction': sum(v > 0 for v in deltas)/horizon,
        'minuteDeltasCny': deltas, 'recentSubwindowMinutes': recent_n,
        'recentNetFlowCny': recent, 'priorNetFlowCny': prior,
        'fading': recent < prior if recent is not None else None,
        'recentOutflow': recent < 0 if recent is not None else None})
    return packet


def _turnover(minute, code, start, end, horizon):
    if (not isinstance(minute, dict) or str(minute.get('code') or '') != str(code)
            or not sourced(minute.get('source')) or timestamp(minute.get('asOf')) != end):
        return None, 'turnover_identity_source_or_window_mismatch'
    if (minute.get('dataLineage') or {}).get('status') == 'tainted':
        sessions = [item for item in (minute.get('dataLineage') or {}).get('sessions') or []
                    if str(item.get('code')) == str(code) and item.get('day') == end.date().isoformat()]
        if not sessions or not all(item.get('status') == 'verified'
                and timestamp(item.get('verifiedThrough')) and timestamp(item['verifiedThrough']) >= end for item in sessions):
            return None, 'turnover_current_session_tainted'
    window = (minute.get('flowAmountWindows') or {}).get(str(horizon))
    if isinstance(window, dict):
        amount = finite(window.get('amountCny'))
        if (timestamp(window.get('start')) != start or timestamp(window.get('end')) != end
                or isinstance(window.get('barCount'), bool) or isinstance(window.get('expectedBarCount'), bool)
                or window.get('barCount') != horizon or window.get('expectedBarCount') != horizon
                or not sourced(window.get('source')) or amount is None or amount <= 0):
            return None, 'turnover_window_incomplete_or_invalid'
        return amount, None
    # Optional explicit completed interval bars, useful for offline audit replay.
    bars, conflicts = {}, set()
    for row in minute.get('bars') or []:
        at = timestamp(row.get('end')) if isinstance(row, dict) else None
        if at is None or not start < at <= end:
            continue
        amount = finite(row.get('amount_cny'))
        if amount is None or amount < 0 or session(at) != session(end) or not sourced(row.get('source') or minute.get('source')):
            conflicts.add(at)
        elif at in bars and bars[at] != amount:
            conflicts.add(at)
        else:
            bars[at] = amount
    stamps = [start+timedelta(minutes=index) for index in range(1, horizon+1)]
    if not all(at in bars and at not in conflicts for at in stamps):
        return None, 'turnover_window_missing_contiguous_intervals'
    total = math.fsum(bars[at] for at in stamps)
    return (total, None) if total > 0 else (None, 'turnover_window_zero')


def compute_stock_flow(fund, minute, cutoff, horizon=30):
    packet = flow_window(fund or {}, cutoff, horizon)
    packet['metrics'].update(turnoverCny=None, netFlowRatio=None)
    if packet['status'] != 'observed':
        return packet
    amount, reason = _turnover(minute, packet['code'], timestamp(packet['windowStart']),
                               timestamp(packet['asOf']), horizon)
    if reason:
        packet.update(status='unavailable', reasons=[reason])
        return packet
    net = packet['metrics']['netFlowCny']
    # Classification net cannot exceed the entire interval turnover. Do not clip
    # a mismatched unit/provider window into a plausible-looking ratio.
    if abs(net) > amount+max(2., amount*1e-5):
        packet.update(status='unavailable', reasons=['fund_net_exceeds_same_window_turnover'])
        return packet
    packet['metrics'].update(turnoverCny=amount, netFlowRatio=net/amount)
    return packet


def annotate_fund_windows(fund, cutoff, minute=None):
    """Retain legacy metrics and add only complete longer-window measurements."""
    packet = {**fund, 'metrics': dict(fund.get('metrics') or {})}
    packet['flowWindows'] = {}
    for horizon in HORIZONS:
        window = compute_stock_flow(fund, minute, cutoff, horizon) if minute is not None else flow_window(fund, cutoff, horizon)
        packet['flowWindows'][str(horizon)] = window
        metrics = window['metrics']
        if 'netFlowCny' not in metrics:
            continue
        packet['metrics'].update({
            f'main_net_flow_{horizon}m': metrics['netFlowCny'],
            f'main_net_positive_fraction_{horizon}m': metrics['positiveMinuteFraction'],
            f'main_net_flow_1m_deltas_{horizon}m': metrics['minuteDeltasCny'],
            f'main_net_flow_recent_{horizon}m': metrics['recentNetFlowCny'],
            f'main_net_flow_prior_{horizon}m': metrics['priorNetFlowCny'],
            f'main_net_recent_subwindow_minutes_{horizon}m': metrics['recentSubwindowMinutes'],
            f'main_net_fading_{horizon}m': metrics['fading'],
            f'main_net_recent_outflow_{horizon}m': metrics['recentOutflow']})
        if metrics.get('netFlowRatio') is not None:
            packet['metrics'][f'main_net_flow_to_amount_{horizon}m'] = metrics['netFlowRatio']
    return packet


def compute_sector_flows(membership, peers, funds, minutes, cutoff, horizon=30):
    """Aggregate comparable complete member windows, never only selected stocks.

    ``peers[sector_id]`` must be the complete membership catalogue supplied by
    the caller. All missing members remain in the denominator. The candidate is
    excluded. Each provider/measurement/source/window group is assessed alone.
    """
    stocks = {}
    now = timestamp(cutoff)

    def stock_at(peer, end):
        key = (peer, end)
        if key not in stocks:
            fund = (funds or {}).get(peer) or {}
            source_end = timestamp(fund.get('asOf'))
            # A source's actual cumulative endpoint authorizes truncation. An
            # older response may never be retimestamped forward to another name.
            if (source_end is None or source_end < end or not any(
                    timestamp(row.get('end')) == end for row in fund.get('cumulativeSeries') or []
                    if isinstance(row, dict))):
                stocks[key] = {}
                return stocks[key]
            fund = {**fund, 'asOf': end.isoformat()}
            parent = (minutes or {}).get(peer) or {}
            minute = parent if timestamp(parent.get('asOf')) == end else (
                (parent.get('flowAmountWindowsByEnd') or {}).get(end.isoformat()) or {})
            # Quarantine discovered by a later independent quote also applies
            # to compact earlier windows; an old subpacket cannot bypass it.
            minute = {**minute, 'dataLineage': parent.get('dataLineage')}
            stocks[key] = compute_stock_flow(fund, minute, end, horizon)
        return stocks[key]

    result = {}
    for code, sector in (membership or {}).items():
        sector = sector if isinstance(sector, dict) else {}
        catalogue = (peers or {}).get(sector.get('code'))
        members = sorted(set(str(c) for c in catalogue)-{str(code)}) if isinstance(catalogue, (list, tuple, set)) else []
        coverage = {'requiredFraction': .8, 'minimumPeers': 3, 'totalPeers': len(members),
                    'usablePeers': 0, 'fraction': 0., 'excludedSelf': True, 'mixedSources': False, 'groups': []}
        packet = {'version': VERSION, 'status': 'unavailable', 'code': code, 'sector': dict(sector),
                  'asOf': None, 'windowStart': None, 'horizonMinutes': horizon, 'source': None,
                  'provider': None, 'measurementVersion': None, 'coverageRequiredPct': 80, 'minimumPeers': 3,
                  'coverage': coverage, 'flowKind': 'vendor_estimate', 'metrics': {}, 'reasons': []}
        result[code] = packet
        if (not members or not isinstance(catalogue, (list, tuple))
                or len(set(map(str, catalogue))) != len(catalogue)
                or str(code) not in set(map(str, catalogue))
                or sector.get('membershipComplete') is not True
                or isinstance(sector.get('membershipCount'), bool)
                or sector.get('membershipCount') != len(catalogue)):
            packet['reasons'].append('complete_sector_membership_unavailable')
            continue
        end = timestamp(((minutes or {}).get(code) or {}).get('asOf'))
        if (end is None or now is None or end.date() != now.date()
                or not 0 <= (now-end).total_seconds() < 300 or session(end) is None):
            packet['reasons'].append('candidate_flow_endpoint_missing_future_or_stale')
            continue
        groups = defaultdict(list)
        for peer in members:
            observation = stock_at(peer, end)
            if observation.get('status') == 'observed':
                identity = tuple(observation.get(k) for k in ('provider', 'measurementVersion', 'source', 'asOf', 'windowStart'))
                groups[identity].append((peer, observation['metrics']))
        ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
        coverage['mixedSources'] = len({key[:3] for key in groups}) > 1
        coverage['groups'] = [{'provider': key[0], 'measurementVersion': key[1], 'source': key[2],
            'asOf': key[3], 'windowStart': key[4], 'usablePeers': len(rows), 'fraction': len(rows)/len(members)}
            for key, rows in ranked]
        if not ranked:
            packet['reasons'].append('sector_no_verified_comparable_flow_windows')
            continue
        key, rows = ranked[0]
        coverage.update(usablePeers=len(rows), fraction=len(rows)/len(members))
        if len(rows) < 3 or len(rows)/len(members) < .8:
            packet['reasons'].append('sector_comparable_flow_coverage_below_required')
            if coverage['mixedSources']:
                packet['reasons'].append('different_provider_measurements_not_summed')
            continue
        net = math.fsum(row['netFlowCny'] for _, row in rows)
        amount = math.fsum(row['turnoverCny'] for _, row in rows)
        positives = [max(0., row['netFlowCny']) for _, row in rows]
        positive_sum = math.fsum(positives)
        packet.update(status='observed', provider=key[0], measurementVersion=key[1], source=key[2],
                      asOf=key[3], windowStart=key[4], observedPeerCodes=[peer for peer, _ in rows], metrics={
            'netFlowCny': net, 'turnoverCny': amount, 'netFlowRatio': net/amount,
            'positiveMemberFraction': sum(row['netFlowCny'] > 0 for _, row in rows)/len(rows),
            'top1PositiveContribution': max(positives)/positive_sum if positive_sum > 0 else None})
    return result
