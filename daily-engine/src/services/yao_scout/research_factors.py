"""Point-in-time research factors. No requests, language models or invented data.

Scores are unvalidated rule values, not probabilities. Market snapshots are an
observation archive, never a historical-universe reconstruction from today's list.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import mean
from zoneinfo import ZoneInfo

import pandas as pd

from .research_policy import DEFAULT_PARAMETERS

TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'research-factors-v1'


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _at(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result.astimezone(TZ) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _source_at(value):
    """Provider wall-clock strings are Shanghai time; date-only is not a clock."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    if not re.search(r'[T ]\d{2}:\d{2}', str(value or '')):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    except ValueError:
        return None


@lru_cache(maxsize=128)
def _sessions(start, end):
    try:
        import exchange_calendars as xcals
        return [stamp.date() for stamp in xcals.get_calendar('XSHG').sessions_in_range(start, end)]
    except Exception:
        return []


def _packet(cutoff, **extra):
    return {'version': VERSION, 'score': None, 'status': 'unavailable',
            'asOf': cutoff.isoformat() if cutoff else None, 'features': {}, 'gaps': [],
            'validationStatus': 'unvalidated_starting_points', **extra}


def _clamp(value):
    return min(100., max(0., value))


def event_factor(context, cutoff, *, half_life_sessions=None, calendar_provider=None):
    """Positive *net-profit forecasts* only; firstSeen gates knowledge, not hype.

Decay starts at the announcement's conservative availability date, so finding
an old announcement later or downloading it again cannot renew its freshness.
Known negative events are left to the independent precision risk gate.
"""
    now = _at(cutoff)
    out = _packet(now, source='archived_disclosures', socialSentimentAvailable=False)
    if now is None:
        out['gaps'].append('截止时间缺少明确时区')
        return out
    policy = DEFAULT_PARAMETERS['researchFactors']['C']
    half_life = _number(half_life_sessions if half_life_sessions is not None else policy['halfLifeSessions'])
    if not half_life or half_life <= 0:
        out['gaps'].append('事件半衰期无效')
        return out
    context = context or {}
    if not (context.get('coverage') or {}).get('forecast'):
        out['gaps'].append('业绩预告覆盖未知，不能当作无事件')
        return out
    positive_types = policy['positiveTypes']
    valid, invalid, deduplicated = {}, 0, 0
    for event in context.get('forecasts') or []:
        if not isinstance(event, dict):
            invalid += 1
            continue
        observed, available, first = (_at(event.get(key)) for key in ('observedAt', 'availableAt', 'firstSeenAt'))
        try:
            published = datetime.combine(date.fromisoformat(str(event.get('publishedDate'))[:10]) + timedelta(days=1), time(), TZ)
        except ValueError:
            published = None
        if (not event.get('source') or not all((observed, available, first, published))
                or not (first <= observed <= now and max(published, first) <= available <= now)):
            invalid += 1
            continue
        identity = event.get('eventId') or hashlib.sha256(json.dumps(
            [event.get(k) for k in ('source', 'reportPeriod', 'publishedDate', 'type', 'metric', 'changePct')],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if identity in valid:
            deduplicated += 1
            continue
        valid[identity] = (event, published)
    out['features'].update(verified_event_count=len(valid), rejected_event_count=invalid,
                           duplicate_event_count=deduplicated, half_life_sessions=half_life)
    if invalid:
        out['gaps'].append('公告来源或可知时间无效，覆盖不能视作完整')
        return out
    strengths, ages = [], []
    for event, published in valid.values():
        weight = positive_types.get(event.get('type'))
        if weight is None or '净利润' not in str(event.get('metric') or ''):
            continue
        growth = _number(event.get('changePct'))
        if growth is not None and growth <= 0:
            out['gaps'].append('正面预告类型与变动方向矛盾，未生成催化分')
            return out
        days = sorted(set((calendar_provider or _sessions)(published.date(), now.date())))
        if not days or days[-1] > now.date():
            out['gaps'].append('交易日历不可用，未把自然日伪装为交易日衰减')
            return out
        # Count completed intervening session dates, not repeated intraday calls.
        age = max(0, len(days) - 1)
        strengths.append(float(weight) * .5 ** (age / half_life))
        ages.append(age)
    out['features']['positive_event_count'] = len(strengths)
    if ages:
        out['features']['youngest_positive_age_sessions'] = min(ages)
    out.update(score=round(_clamp(policy['neutralScore'] + policy['positiveScale'] * max(strengths, default=0.)), 6),
               status='observed', decayBasis='announcement_availability; firstSeenAt limits knowledge',
               eventIds=sorted(valid))
    # Financials are contextual observations, never an invented earnings surprise.
    financial = context.get('financials') or {}
    times = [_at(financial.get(key)) for key in ('observedAt', 'availableAt', 'firstSeenAt')]
    if financial.get('source') and all(stamp is not None and stamp <= now for stamp in times):
        for key in ('profitGrowthPct', 'revenueGrowthPct', 'roePct'):
            value = _number(financial.get(key))
            if value is not None:
                out['features']['financial_' + key] = value
    return out


def _limit_price(previous, rate):
    return float((Decimal(str(previous)) * (1 + Decimal(str(rate)) / 100)).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP))


def character_factor(daily_history, cutoff, *, limit_pct=None, price_basis=None,
                     lookback=None, min_events=None, calendar_provider=None):
    """Mature next-session paths after historical 10% limit-like closes.

Unadjusted prices are required. Where historical exchange limit prices are not
provided the packet explicitly describes the 10% price-pattern assumption;
it does not infer past ST status or claim all such days were exchange limits.
"""
    now = _at(cutoff)
    out = _packet(now, source='completed_unadjusted_daily_bars')
    if now is None:
        out['gaps'].append('截止时间缺少明确时区')
        return out
    policy = DEFAULT_PARAMETERS['researchFactors']['G']
    lookback = int(lookback if lookback is not None else policy['lookbackSessions'])
    minimum = int(min_events if min_events is not None else policy['minEvents'])
    rate = float(limit_pct if limit_pct is not None else policy['limitPct'])
    attrs = getattr(daily_history, 'attrs', {}) or {}
    basis = str(price_basis or attrs.get('price_basis') or attrs.get('adjustment') or '').lower()
    if basis not in {'unadjusted', 'none', 'raw', '不复权'}:
        out['gaps'].append('历史价格复权口径未明确为不复权，股性分为空')
        return out
    frame = daily_history.copy() if isinstance(daily_history, pd.DataFrame) else pd.DataFrame(daily_history or [])
    if 'date' not in frame and 'end' in frame:
        frame = frame.rename(columns={'end': 'date'})
    if not {'date', 'open', 'high', 'low', 'close'}.issubset(frame.columns):
        out['gaps'].append('历史日线OHLC字段不完整')
        return out
    rows = {}
    for raw in frame.to_dict('records'):
        try:
            day = date.fromisoformat(str(raw['date'])[:10])
        except ValueError:
            continue
        if day >= now.date():
            continue  # Even after close, this factor only consumes earlier sessions.
        if any(raw.get(key) is not None and (_at(raw[key]) is None or _at(raw[key]) > now)
               for key in ('observedAt', 'availableAt')):
            out['gaps'].append('历史行包含尚不可知或无效的可用时间')
            return out
        prices = {key: _number(raw.get(key)) for key in ('open', 'high', 'low', 'close')}
        if (any(value is None or value <= 0 for value in prices.values())
                or not prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high']):
            out['gaps'].append('历史OHLC完整性检查失败')
            return out
        row = {**raw, **prices, 'date': day}
        if day in rows and any(rows[day][key] != row[key] for key in prices):
            out['gaps'].append('同一历史日期存在冲突价格')
            return out
        rows[day] = row
    # The window is exchange sessions preceding the decision, not the last N
    # supplied rows: suspensions and missing bars must not pull old events in.
    calendar_start = now.date() - timedelta(days=max(366, (lookback + 2) * 3))
    sessions = sorted(set((calendar_provider or _sessions)(calendar_start, now.date()))) if rows else []
    completed = [day for day in sessions if day < now.date()]
    if rows and (len(completed) < lookback or any(day > now.date() for day in sessions)):
        out['status'] = 'insufficient'
        out['gaps'].append('历史交易日历不完整，不能确认下一交易日路径已成熟')
        return out
    following_day = dict(zip(sessions, sessions[1:]))
    preceding_day = dict(zip(sessions[1:], sessions))
    event_days = set(completed[-lookback:])
    window_start = min(event_days) if event_days else now.date()
    if any(window_start <= day < now.date() and day not in completed for day in rows):
        out['gaps'].append('历史价格日期不属于已核验交易日历')
        return out
    kept_days = event_days | {preceding_day.get(window_start)}
    ordered = [rows[day] for day in sorted(rows) if day in kept_days]
    out['features']['history_sessions'] = len(ordered)
    out['features']['lookback_sessions'] = lookback
    def positive(*values):
        return next((number for value in values
                     if (number := _number(value)) is not None and number > 0), None)
    events, next_returns, reversals, assumed = [], [], [], 0
    for index in range(len(ordered) - 1):
        event, following = ordered[index:index + 2]
        previous = ordered[index - 1] if index else None
        if event['date'] not in event_days or following_day.get(event['date']) != following['date']:
            continue
        prior = positive(event.get('pre_close'), event.get('previous_close'))
        supplied_limit = positive(event.get('limit_up'))
        if not supplied_limit and prior is None:
            if previous is None or preceding_day.get(event['date']) != previous['date']:
                continue  # No provider reference and no immediately preceding bar.
            prior = previous['close']
        upper = supplied_limit or _limit_price(prior, rate)
        if abs(event['close'] - upper) > policy['priceTick'] / 2 or abs(event['high'] - event['close']) > policy['priceTick'] / 2:
            continue
        # Do not label an overnight corporate-action gap as continuation.
        next_previous = positive(following.get('pre_close'), following.get('previous_close'))
        if next_previous is not None and abs(next_previous - event['close']) > policy['priceTick']:
            continue
        next_returns.append((following['close'] / event['close'] - 1) * 100)
        reversals.append((1 - following['close'] / following['high']) * 100)
        events.append(event['date'].isoformat())
        assumed += supplied_limit is None
    out['features'].update(mature_event_count=len(events), assumed_limit_count=assumed,
                           required_events=minimum)
    if ordered:
        out['features']['last_close_location_pct'] = (100 * (ordered[-1]['close'] - ordered[-1]['low']) /
            (ordered[-1]['high'] - ordered[-1]['low']) if ordered[-1]['high'] > ordered[-1]['low'] else 50.)
    out.update(status='insufficient', eventDates=events,
               eventDefinition='historical supplied limit price or declared 10% unadjusted price pattern; not inferred historical ST status')
    if len(events) < minimum:
        out['gaps'].append(f'成熟历史事件{len(events)}个，少于{minimum}个，股性分为空')
        return out
    continuation = 100 * sum(value > 0 for value in next_returns) / len(next_returns)
    out['features'].update(next_session_positive_pct=continuation,
                           mean_next_session_return_pct=mean(next_returns),
                           mean_next_session_high_to_close_pct=mean(reversals))
    score = (policy['continuationWeight'] * continuation
             + policy['meanReturnWeight'] * _clamp(50 + policy['meanReturnScale'] * mean(next_returns))
             + policy['reversalWeight'] * _clamp(100 - policy['reversalScale'] * mean(reversals)))
    out.update(score=round(_clamp(score), 6), status='observed')
    return out


class MarketEmotionArchive:
    """Archive point-in-time full-mainboard observations; never fetch missing days."""
    def __init__(self, directory, *, calendar_provider=None, policy=None):
        self.path = Path(directory) / 'market-emotion.sqlite3'
        self.calendar = calendar_provider or _sessions
        self.policy = {**DEFAULT_PARAMETERS['researchFactors']['E'], **(policy or {})}

    def _database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=20)
        db.execute('''CREATE TABLE IF NOT EXISTS observations (
            digest TEXT PRIMARY KEY, source_at TEXT NOT NULL, received_at TEXT NOT NULL,
            trading_day TEXT NOT NULL, slot TEXT NOT NULL, is_close INTEGER NOT NULL,
            payload TEXT NOT NULL)''')
        return db

    def observe(self, snapshot, cutoff, *, received_at=None):
        now, received = _at(cutoff), _at(received_at)
        out = _packet(now, source='archived_full_mainboard_snapshots', regime='unknown',
                      exposureMultiplier=None, socialSentimentAvailable=False)
        attrs = getattr(snapshot, 'attrs', {}) or {}
        received = received or _at(attrs.get('received_at'))
        if now is None or received is None or received > now:
            out['gaps'].append('快照接收时间未知、超前或缺少时区')
            return out
        source = attrs.get('snapshot_source') or attrs.get('source')
        if not source or attrs.get('stale') or attrs.get('complete') is False:
            out['gaps'].append('快照来源未知或标记为陈旧')
            return out
        frame = snapshot.copy() if isinstance(snapshot, pd.DataFrame) else pd.DataFrame(snapshot or [])
        if not {'code', 'name', 'price'}.issubset(frame.columns):
            out['gaps'].append('全主板快照字段不完整')
            return out
        frame['code'] = frame.code.astype(str).str.zfill(6)
        frame = frame.loc[frame.code.str.match(r'^(?:600|601|603|605|000|001|002|003)\d{3}$')
                          & ~frame.name.astype(str).str.contains(r'ST|退|整理|停牌', case=False, regex=True)]
        if frame.code.duplicated().any():
            out['gaps'].append('全主板股票代码重复，不能改变市场指标分母')
            return out
        universe = len(frame)
        if universe < self.policy['minimumUniverse']:
            out['gaps'].append('全主板覆盖数量不足，不能使用候选小样本代替市场')
            return out
        aggregate = _source_at(attrs.get('provider_timestamp') or attrs.get('source_time'))
        data, stamps, close_flags = {}, [], []
        for row in frame.to_dict('records'):
            raw_stamp = row.get('source_time') or row.get('provider_timestamp')
            stamp = _source_at(raw_stamp) if raw_stamp is not None else aggregate
            if stamp is None or stamp > received or stamp.date() != now.date():
                continue
            closing = time(15, 0) <= stamp.time().replace(tzinfo=None) <= time(15, 5)
            if not closing and ((now - stamp).total_seconds() > self.policy['maxSourceAgeSeconds']
                                or (received - stamp).total_seconds() > self.policy['maxReceiptLagSeconds']):
                continue
            price = _number(row.get('price'))
            previous = _number(row.get('pre_close') or row.get('previous_close'))
            previous = previous if previous is not None and previous > 0 else None
            change = _number(row.get('change_pct'))
            if not price or price <= 0 or (previous is None and change is None):
                continue
            if previous is not None and previous > 0:
                change = (price / previous - 1) * 100
            high = _number(row.get('high'))
            upper, lower = _number(row.get('limit_up')), _number(row.get('limit_down'))
            # Explicit pre-close permits a declared normal-mainboard 10% estimate.
            if previous and previous > 0:
                upper = upper or _limit_price(previous, 10)
                lower = lower or _limit_price(previous, -10)
            data[row['code']] = {'price': price, 'previous_close': previous, 'change_pct': change,
                'up': abs(price - upper) < .005 if upper and upper > 0 else None,
                'down': abs(price - lower) < .005 if lower and lower > 0 else None,
                'touched': high >= upper - .005 if high is not None and upper and high >= price else None}
            stamps.append(stamp)
            close_flags.append(closing)
        coverage = len(data) / universe if universe else 0
        out['features'].update(universe_count=universe, source_coverage=coverage, observed_count=len(data))
        if coverage < self.policy['minimumCoverage'] or not stamps:
            out['gaps'].append('真实源时间或价格覆盖不足；日期字段不充当盘中时间')
            return out
        at, is_close = min(stamps), all(close_flags)
        out['asOf'] = at.isoformat()
        out.update(receivedAt=received.isoformat(), source=str(source))
        features = out['features']
        features['advancing_pct'] = 100 * sum(row['change_pct'] > 0 for row in data.values()) / len(data)
        for label, key in (('limit_up_pct', 'up'), ('limit_down_pct', 'down')):
            known = [row[key] for row in data.values() if row[key] is not None]
            if len(known) / universe >= self.policy['minimumCoverage']:
                features[label] = 100 * sum(known) / len(known)
        touched = [row for row in data.values() if row['touched'] is not None and row['up'] is not None]
        if len(touched) / universe >= self.policy['minimumCoverage']:
            count = sum(row['touched'] for row in touched)
            features['touched_count'] = count
            if count:
                features['broken_board_pct'] = 100 * sum(row['touched'] and not row['up'] for row in touched) / count
        if 'broken_board_pct' not in features:
            out['gaps'].append('触板路径或触板样本不足，炸板率未知')
        slot = f'{at.hour:02d}:{(at.minute // self.policy["slotMinutes"]) * self.policy["slotMinutes"]:02d}'
        calendar = sorted(set(self.calendar(now.date() - timedelta(days=550), now.date())))
        if not calendar or now.date() not in calendar:
            out['gaps'].append('完整交易日历不可用，昨日群体与历史分位为空')
        previous_days = [day for day in calendar if day < now.date()]
        packet = {'features': features, 'rows': data, 'source': str(source)}
        digest = hashlib.sha256(json.dumps([at.isoformat(), packet], sort_keys=True).encode()).hexdigest()
        with self._database() as db:
            # Never replace first receipt or replay a snapshot before it was known.
            existing = db.execute('SELECT received_at FROM observations WHERE digest=?', (digest,)).fetchone()
            first_received = min(received.isoformat(), existing[0]) if existing else received.isoformat()
            prior = db.execute('SELECT source_at,received_at,trading_day,slot,is_close,payload FROM observations '
                               'WHERE source_at < ? AND received_at <= ? ORDER BY source_at,received_at',
                               (at.isoformat(), now.isoformat())).fetchall()
            if previous_days:
                closes = [row for row in prior if row[2] == previous_days[-1].isoformat() and row[4]]
                if closes:
                    old = json.loads(closes[-1][5])['rows']
                    cohort = [code for code, row in old.items() if row['up'] is True]
                    matched = [code for code in cohort if code in data and data[code]['previous_close']
                               and abs(data[code]['previous_close'] - old[code]['price']) <= .01]
                    if cohort and len(matched) / len(cohort) >= self.policy['minimumCoverage']:
                        features['previous_limit_cohort_count'] = len(cohort)
                        features['previous_limit_cohort_coverage'] = len(matched) / len(cohort)
                        features['previous_limit_premium_pct'] = mean((data[code]['price'] / old[code]['price'] - 1) * 100 for code in matched)
                        if all(data[code]['up'] is not None for code in matched):
                            features['previous_limit_promotion_pct'] = 100 * sum(data[code]['up'] for code in matched) / len(matched)
            if 'previous_limit_premium_pct' not in features:
                out['gaps'].append('前一交易日完整收盘涨停群体未归档或覆盖不足，溢价及晋级未知')
            history = {}
            for row in prior:
                if row[3] == slot and row[2] < now.date().isoformat():
                    history[row[2]] = json.loads(row[5])['features']
            expected = previous_days[-int(self.policy['historySessions']):]
            features['same_slot_history_sessions'] = sum(day.isoformat() in history for day in expected)
            required = ('advancing_pct', 'limit_up_pct', 'limit_down_pct')
            complete = (len(expected) == self.policy['historySessions'] and
                        all(day.isoformat() in history and all(key in history[day.isoformat()] for key in required) for day in expected)
                        and all(key in features for key in required))
            out['status'] = 'insufficient'
            if complete and (now - at).total_seconds() <= self.policy['maxSourceAgeSeconds']:
                # Equal votes of three empirical ranks, with downturn direction inverted.
                ranks = []
                for key in required:
                    values = [history[day.isoformat()][key] for day in expected]
                    value = features[key]
                    rank = 100 * (sum(item < value for item in values) + .5 * sum(item == value for item in values)) / len(values)
                    ranks.append(100 - rank if key == 'limit_down_pct' else rank)
                score = mean(ranks)
                regime = 'weak' if score < self.policy['lowPercentile'] else 'hot' if score > self.policy['highPercentile'] else 'normal'
                out.update(score=round(score, 6), status='observed', regime=regime,
                           exposureMultiplier=self.policy['weakExposure'] if regime == 'weak' else self.policy['hotExposure'] if regime == 'hot' else 1.)
            else:
                out['gaps'].append('同刻历史不足或当前快照已过期，市场状态保持未知')
            # Digest uses raw features, before history-dependent derived diagnostics.
            db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?)',
                       (digest, at.isoformat(), first_received, now.date().isoformat(), slot, int(is_close),
                        json.dumps(packet, ensure_ascii=False, allow_nan=False)))
        out['gaps'] = list(dict.fromkeys(out['gaps']))
        return out
