"""Frozen, numeric entry discipline; never a fill or a fitted return claim.

The research trigger is independent of model admission so rejected model choices
can still acquire executable counterfactual labels. Repeated scans cannot reset
the trigger price, deadline or invalidation. SQLite serializes concurrent scans.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'frozen-entry-band-v1'


def policy_contract():
    return {'version': VERSION, 'bandAtr': .25, 'maximumDistancePct': .5,
            'validMinutes': 5, 'quoteMaxAgeSeconds': 30,
            'triggerPolicy': 'first_evidence_qualified_quote_per_stock_session',
            'entryPolicy': 'next_complete_minute_open_with_slippage_inside_frozen_band',
            'exitPolicy': 'next_exchange_session_close_with_costs_and_liquidity_checks',
            'validationStatus': 'predeclared_risk_preferences_not_fitted',
            'successMetric': 'T+1_simulated_net_return_gt_zero',
            'targetMetric': 'T+1_simulated_net_return_at_least_5pct'}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _at(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('entry timestamps require timezone')
    return stamp.astimezone(TZ)


def _tick(value, up=False):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_CEILING if up else ROUND_FLOOR))


def _session_close(now):
    clock = now.time().replace(tzinfo=None)
    if time(9, 30) <= clock < time(11, 30):
        return datetime.combine(now.date(), time(11, 30), TZ)
    if time(13) <= clock < time(15):
        return datetime.combine(now.date(), time(15), TZ)
    return None


def model_entry_allowed(candidate):
    prediction = candidate.get('prediction') or {}
    mean, utility = _number(prediction.get('expectedNetReturn')), _number(prediction.get('utility'))
    win = _number(prediction.get('winProbability'))
    return bool(prediction.get('taskId') == 'entry_today_exit_next_session'
                and prediction.get('status') in {'active', 'model_active', 'validated_model'}
                and prediction.get('entryEligible') is True
                and mean is not None and mean > 0 and utility is not None and utility > 0
                and win is not None and .5 < win <= 1)


def execution_entry_allowed(candidate):
    plan = candidate.get('entryExecution') or {}
    return bool(plan.get('version') == VERSION and plan.get('priceEligible') is True
                and model_entry_allowed(candidate))


def replay_entry_check(plan, proposed_price, minute_end):
    """Check the actual proposed fill, independently of live model selection.

Legacy events without this contract retain their original execution semantics.
No high/low hindsight is used to invent a pullback fill inside an OHLC bar.
"""
    if not plan:
        return None
    if plan.get('version') != VERSION:
        return 'entry_contract_unknown'
    try:
        end, start, expiry = _at(minute_end), _at(plan['triggerAt']), _at(plan['expiresAt'])
        price = _number(proposed_price)
        low, high = _number(plan.get('minimumPrice')), _number(plan.get('maximumPrice'))
        invalid = _number(plan.get('invalidationPrice'))
        if any(value is None for value in (price, low, high, invalid)):
            return 'entry_contract_incomplete'
        if plan.get('state') in {'invalidated', 'entry_plan_invalidated'}:
            return 'entry_plan_invalidated'
        # A completed bar must start after the trigger and finish before expiry.
        if end - timedelta(minutes=1) < start or end > expiry or end.date() != start.date():
            return 'entry_plan_expired_or_before_trigger'
        if price <= invalid:
            return 'entry_plan_invalidated'
        if not low <= price <= high:
            return 'entry_price_outside_frozen_band'
    except (ValueError, TypeError, KeyError):
        return 'entry_contract_incomplete'
    return None


def annotate_entry_plans(candidates, now, data_dir, *, calendar_provider):
    now = _at(now)
    rows = list(candidates)
    contract = policy_contract()
    try:
        sessions = sorted(set(calendar_provider(now.date(), now.date() + timedelta(days=20))))
        sessions = [value.date() if isinstance(value, datetime) else value for value in sessions]
        next_day = next((value for value in sessions if value > now.date()), None)
        calendar_ok = now.date() in sessions and next_day is not None
    except Exception:
        calendar_ok, next_day = False, None
    close = _session_close(now)
    path = Path(data_dir) / 'entry-plans-v1.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(path, timeout=3) as db:
            db.execute('CREATE TABLE IF NOT EXISTS entry_plans(day TEXT,code TEXT,version TEXT,plan_json TEXT NOT NULL,PRIMARY KEY(day,code,version))')
            db.execute('BEGIN IMMEDIATE')
            for row in rows:
                code = str(row.get('code') or '')
                quote = row.get('quote') or {}
                price = _number(quote.get('price'))
                reason = None
                try:
                    source_at = _at(quote.get('provider_timestamp') or quote.get('source_time'))
                    fresh = bool(quote.get('source') and 0 <= (now-source_at).total_seconds() <= 30
                                 and (not quote.get('code') or str(quote['code']) == code))
                except (ValueError, TypeError):
                    fresh = False
                saved = db.execute('SELECT plan_json FROM entry_plans WHERE day=? AND code=? AND version=?',
                                   (now.date().isoformat(), code, VERSION)).fetchone()
                plan = json.loads(saved[0]) if saved else None
                qualified = (row.get('primaryAdmission') or {}).get('eligible') is True
                if not calendar_ok:
                    reason = 'trading_calendar_unavailable'
                elif close is None:
                    reason = 'outside_entry_session'
                elif not fresh or price is None or price <= 0:
                    reason = 'entry_quote_unverified'
                elif not qualified:
                    reason = 'entry_evidence_not_qualified'
                if plan is None and reason is None:
                    atr = _number(row.get('entryRiskFloorAtr'))
                    support = next((_number(item.get('value')) for item in row.get('indicatorEvidence') or []
                                    if item.get('key') == 'support'), None)
                    if atr is None or atr <= 0 or support is None or not 0 < support < price:
                        reason = 'entry_atr_or_support_missing'
                    else:
                        distance = min(atr * contract['bandAtr'], price * contract['maximumDistancePct'] / 100)
                        low, high = _tick(max(support + .01, price-distance), up=True), _tick(price+distance)
                        expiry = min(close, now + timedelta(minutes=contract['validMinutes']))
                        first_end = now.replace(second=0, microsecond=0) + timedelta(minutes=2 if now.second or now.microsecond else 1)
                        if low > high or first_end > expiry:
                            reason = 'no_complete_entry_minute'
                        else:
                            plan = {**contract, 'code': code, 'triggerAt': now.isoformat(),
                                    'triggerPrice': price, 'triggerSourceTime': source_at.isoformat(),
                                    'minimumPrice': low, 'maximumPrice': high,
                                    'invalidationPrice': support, 'expiresAt': expiry.isoformat(),
                                    'targetSession': next_day.isoformat(), 'state': 'ready',
                                    'meaning': '首次证据触发后冻结的研究入场区间；不是已成交，不保证按失效价退出'}
                            db.execute('INSERT INTO entry_plans VALUES(?,?,?,?)',
                                       (now.date().isoformat(), code, VERSION, json.dumps(plan, ensure_ascii=False)))
                if plan is not None:
                    if plan.get('state') == 'invalidated':
                        reason = 'entry_plan_invalidated'
                    elif now > _at(plan['expiresAt']):
                        reason = 'entry_plan_expired'
                    elif fresh and price is not None and price <= plan['invalidationPrice']:
                        plan['state'] = 'invalidated'
                        plan['invalidatedAt'] = now.isoformat()
                        db.execute('UPDATE entry_plans SET plan_json=? WHERE day=? AND code=? AND version=?',
                                   (json.dumps(plan, ensure_ascii=False), now.date().isoformat(), code, VERSION))
                        reason = 'entry_plan_invalidated'
                    elif reason is None and not plan['minimumPrice'] <= price <= plan['maximumPrice']:
                        reason = 'above_entry_band' if price > plan['maximumPrice'] else 'below_entry_band'
                row['entryExecution'] = {**(plan or contract), 'state': reason or 'ready',
                                         'priceEligible': reason is None and plan is not None,
                                         'modelEligible': model_entry_allowed(row),
                                         'evaluatedAt': now.isoformat()}
                row['entryExecution']['entryEligible'] = execution_entry_allowed(row)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # No unpersisted trigger may pass as a frozen entry plan.
        for row in rows:
            row['entryExecution'] = {**contract, 'state': 'entry_state_unavailable',
                                     'priceEligible': False, 'modelEligible': False, 'entryEligible': False}
    return rows
