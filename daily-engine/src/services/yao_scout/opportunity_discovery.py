"""Bounded discovery lanes from already captured evidence; no network calls.

These ranks allocate research time. They are neither admission gates nor return
probabilities. Previous observations retain their original provider clocks.
"""
from __future__ import annotations

from datetime import datetime, time
import math
from zoneinfo import ZoneInfo

import pandas as pd


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return value.astimezone(ZoneInfo('Asia/Shanghai')) if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _session(stamp):
    clock = stamp.time().replace(tzinfo=None)
    return 'am' if time(9, 30) <= clock <= time(11, 30) else 'pm' if time(13) <= clock <= time(15) else None


def _quote_shape(row, now):
    quote = row.get('market_quote')
    if not isinstance(quote, dict):
        quote = row
    stamp = _at(quote.get('source_time'))
    if (str(quote.get('code')) != str(row.get('code')) or not quote.get('source')
            or stamp is None or not 0 <= (now - stamp).total_seconds() <= 30):
        return None
    values = {key: _number(quote.get(key)) for key in ('price', 'open', 'high', 'low')}
    if (any(value is None or value <= 0 for value in values.values())
            or not values['low'] <= min(values['open'], values['price'])
            <= max(values['open'], values['price']) <= values['high']
            or values['high'] == values['low']):
        return None
    return values, quote['source'], stamp.isoformat()


def _ranked(records, axes):
    """Union of leaders on independent positive axes; no fitted composite."""
    if not records:
        return []
    data = pd.DataFrame(records).set_index('code')
    ranks = []
    for key in axes:
        values = pd.to_numeric(data.get(key, pd.Series(index=data.index, dtype=float)), errors='coerce')
        ranks.append(values.where(values > 0).rank(ascending=False, method='min'))
    rank = pd.concat(ranks, axis=1).min(axis=1).dropna()
    return sorted(rank.index, key=lambda code: (rank[code], code))


def discovery_lanes(rows, now, *, previous_rows=None, previous_at=None, catalysts=None):
    """Return lane orders and per-code audit packets from validated selections.

    ``previous_rows`` must be prepared using its own captured_at cutoff through
    research_selection_row. Never validate an old observation against now or
    replace its timestamp with the latest quote time.
    """
    now = _at(now)
    prior_at = _at(previous_at)
    prior = {str(row['code']): row for row in previous_rows or []}
    comparable = bool(prior_at and prior_at.date() == now.date()
                      and _session(now) is not None and _session(prior_at) == _session(now)
                      and 0 < (now - prior_at).total_seconds() <= 3600)
    packets, dynamic, reversals, events, amount_pairs = {}, [], [], [], []
    for row in rows:
        code = str(row['code'])
        packet = {'version': 'opportunity-discovery-v1', 'researchOnly': True,
                  'asOf': now.isoformat(), 'channels': [], 'metrics': {}, 'evidence': {}, 'gaps': []}
        packets[code] = packet
        fields = (row.get('selectionEvidence') or {}).get('fields') or {}
        before = prior.get(code)
        old = ((before or {}).get('selectionEvidence') or {}).get('fields') or {}
        changes = {'code': code}
        for key in ('price', 'amount'):
            current, previous = fields.get(key) or {}, old.get(key) or {}
            current_at, old_at = _at(current.get('sourceTime')), _at(previous.get('sourceTime'))
            valid = bool(comparable and before and current_at and old_at
                         and current.get('source') == previous.get('source')
                         and current_at.date() == old_at.date()
                         and _session(current_at) == _session(old_at) == _session(now)
                         and 0 < (current_at - old_at).total_seconds() <= 3600)
            left, right = _number(previous.get('value')), _number(current.get('value'))
            if not valid or left is None or right is None or min(left, right) <= 0:
                continue
            minutes = (current_at - old_at).total_seconds() / 60
            packet['evidence'][key + 'Change'] = {'before': previous, 'after': current,
                                                        'intervalMinutes': minutes}
            if key == 'price':
                changes['price_change_pct'] = 100 * (right / left - 1)
                changes['price_change_pct_per_minute'] = changes['price_change_pct'] / minutes
            elif right >= left:
                elapsed = (old_at.hour * 60 + old_at.minute + old_at.second / 60) - 570
                if old_at.hour >= 13:
                    elapsed -= 90
                changes['interval_amount_cny'] = right - left
                if elapsed > 0:
                    changes['interval_amount_relative_prior_pace'] = (right - left) / minutes / (left / elapsed)
                amount_pairs.append({'code': code, 'before': left, 'after': right})
            else:
                packet['gaps'].append('cumulative_amount_decreased_or_reset')
        if not comparable:
            packet['gaps'].append('previous_same_session_snapshot_unavailable')
        dynamic.append(changes)
        shape = _quote_shape(row, now)
        if shape:
            values, source, stamp = shape
            span = values['high'] - values['low']
            # A low below the open followed by recovery through the open is a
            # measurable reversal candidate; daily highs do not prove a path.
            if values['low'] < values['open'] <= values['price']:
                metrics = {'low_recovery_fraction': (values['price'] - values['low']) / span,
                           'open_to_price_range_efficiency': (values['price'] - values['open']) / span}
                reversals.append({'code': code, **metrics})
                packet['metrics'].update(metrics)
                packet['evidence']['intradayShape'] = {'source': source, 'sourceTime': stamp,
                    'values': values, 'meaning': '同快照低位修复与方向效率代理；未证明分钟路径或持续买盘'}
        context = (catalysts or {}).get(code)
        if context is not None:
            from .research_factors import event_factor
            catalyst = event_factor({**context, 'code': code}, now)
            packet['evidence']['catalyst'] = catalyst
            features = catalyst.get('features') or {}
            eligible_ages = [age for count, age in (
                (features.get('positive_event_count', 0), features.get('youngest_positive_age_sessions')),
                (features.get('discovery_event_count', 0), features.get('youngest_discovery_age_sessions')))
                if count and age is not None and age <= 5]
            if eligible_ages:
                events.append({'code': code, 'catalyst_score': 1 / (1 + min(eligible_ages))})
        else:
            packet['gaps'].append('disclosure_context_unavailable')
    if amount_pairs:
        data = pd.DataFrame(amount_pairs).set_index('code')
        improvement = data.before.rank(ascending=False, method='min') - data.after.rank(ascending=False, method='min')
        for changes in dynamic:
            if changes['code'] in improvement.index:
                changes['amount_rank_improvement'] = float(improvement[changes['code']])
                packets[changes['code']]['evidence']['rankComparisonSize'] = len(data)
    for changes in dynamic:
        packets[changes['code']]['metrics'].update({k: v for k, v in changes.items() if k != 'code'})
    # Do not call a high turnover interval with falling price a new strength
    # signal. Such rows still retain their original activity queue eligibility.
    strengthening = [row for row in dynamic if row.get('price_change_pct', 0) > 0]
    lanes = {'state_change': _ranked(strengthening, ('price_change_pct_per_minute',
                'interval_amount_relative_prior_pace', 'amount_rank_improvement')),
             'low_recovery': _ranked(reversals, ('low_recovery_fraction', 'open_to_price_range_efficiency')),
             'disclosure': _ranked(events, ('catalyst_score',))}
    for lane, codes in lanes.items():
        for code in codes:
            packets[code]['channels'].append(lane)
    return lanes, packets


def allocate_discovery(rows, limit, lanes, packets):
    """Reserve half the existing budget for independent discovery, round-robin.

    Empty lanes return capacity to activity. The total budget and admission
    rules do not change. Every selected item says how research time was assigned.
    """
    by_code = {str(row['code']): row for row in rows}
    capacity = min(limit, len(rows))
    discovery_budget = (capacity + 1) // 2 if any(lanes.values()) else 0
    selected, assigned = [], {}
    for row in rows[:capacity - discovery_budget]:
        code = str(row['code'])
        selected.append(code)
        assigned[code] = 'activity'
    pending = {lane: list(codes) for lane, codes in lanes.items()}
    while len(selected) < capacity and any(pending.values()):
        for lane, codes in pending.items():
            while codes and codes[0] in assigned:
                codes.pop(0)
            if codes and len(selected) < capacity:
                code = codes.pop(0)
                selected.append(code)
                assigned[code] = lane
    for row in rows:
        code = str(row['code'])
        if len(selected) >= capacity:
            break
        if code not in assigned:
            selected.append(code)
            assigned[code] = 'activity_backfill'
    return [{**by_code[code], 'discoveryEvidence': {**packets[code],
             'allocatedBy': assigned[code], 'researchBudget': capacity,
             'discoveryBudget': discovery_budget}} for code in selected]
