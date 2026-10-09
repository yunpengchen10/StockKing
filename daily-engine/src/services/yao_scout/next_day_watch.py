"""Independent T+1 limit-up research, never an executable-entry fallback.

The fixed rule ranks pre-decision price structures. It is not trained/calibrated;
next-session touch and closing-limit outcomes are evaluated separately from P&L.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import json
import math
from zoneinfo import ZoneInfo

from .labels import calculate_limit_price
from .research_factors import _sessions

TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'next-day-limit-watch-v2'
SUPPORTED_VERSIONS = frozenset({VERSION, 'next-day-limit-watch-v1'})
MEANING = '未触板潜伏与已触板延续独立排序；未校准为涨停概率，观察入选不代表通过买入或成交检查'
FIRST_BOARD_POLICY = {'minChangePct': -3., 'maxChangePct': 5., 'minLimitDistancePct': 4.,
                      'maxAtrExtension': 2.5, 'todayLimitTouchAllowed': False,
                      'knownPreviousSessionLimitCloseAllowed': False,
                      'limitMoveAtrFullScore': 3., 'limitMoveAtrZeroScore': 8.,
                      'meaning': '时点未触及主板10%价格模式的潜伏研究；未核实昨日形态时不称已验证首板'}
FIRST_BOARD_WEIGHTS = {'highPosition': 15, 'platformReadiness': 20, 'trend': 15,
                       'lowExtension': 15, 'rangeCapacity': 20, 'vwapSupport': 10, 'minuteStrength': 5}
CONTINUATION_WEIGHTS = {'highPosition': 25, 'breakoutProximity': 25, 'trend': 20,
                        'vwapSupport': 15, 'minuteStrength': 15}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return value.astimezone(TZ) if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def clip(value):
    return max(0., min(1., value))


def session_dates(provider, start, end):
    try:
        return sorted({value for value in provider(start, end)
                       if isinstance(value, date) and not isinstance(value, datetime) and start <= value <= end})
    except Exception:
        return []


def previous_session_pattern(candidate, day, current_previous_close, code, *, preceding_day=None):
    """Only explicit unadjusted previous-session prices may classify yesterday."""
    packet = candidate.get('dailyPreviousSession') or {}
    basis = str(packet.get('priceBasis') or '').lower()
    previous, close, high = [number(packet.get(key)) for key in ('preClose', 'close', 'high')]
    valid = (str(packet.get('asOf') or '')[:10] == day.isoformat() and bool(packet.get('source'))
             and preceding_day is not None and str(packet.get('preCloseAsOf') or '')[:10] == preceding_day.isoformat()
             and basis in {'unadjusted', 'none', 'raw', '不复权'}
             and all(value is not None and value > 0 for value in (previous, close, high))
             and high >= close and abs(close-current_previous_close) <= .011)
    if not valid:
        return {'status': 'unknown', 'closeAtLimit': None, 'touchedLimit': None,
                'meaning': '昨日未复权价格或口径未取得；不能据此声称已经核实首板'}
    limit = calculate_limit_price(previous, code, day)
    return {'status': 'observed_price_pattern', 'closeAtLimit': abs(close-limit) <= .005,
            'touchedLimit': high >= limit-.005, 'limitPrice': limit, 'asOf': day.isoformat(),
            'meaning': '仅为昨日主板10%价格模式；历史ST及除权制度未经核验'}


def build_next_day_watchlist(candidates, now, top_n=5, *, calendar_provider=None):
    """Pure ranking of frozen candidates; never fetch or mutate a past decision."""
    now = timestamp(now)
    calendar = calendar_provider or _sessions
    exclusions, research = Counter(), []
    population = Counter()
    seen = set()
    for candidate in candidates:
        code = str(candidate.get('code') or '')
        if code in seen:
            continue
        seen.add(code)
        quote = candidate.get('quote') or {}
        at = timestamp(quote.get('provider_timestamp') or quote.get('source_time'))
        decision = timestamp(candidate.get('decision_at')) or now
        name = str(candidate.get('name') or code)
        if not code.startswith(('00', '60')) or len(code) != 6 or 'ST' in name.upper():
            exclusions['unsupported_board'] += 1
            continue
        if (now is None or decision is None or decision > now or at is None or not quote.get('source')
                or decision.date() != now.date() or str(quote.get('code') or '') != code
                or not 0 <= (decision-at).total_seconds() <= 30 or not 0 <= (now-at).total_seconds() <= 30):
            exclusions['quote_identity_or_timestamp'] += 1
            continue
        sessions = session_dates(calendar, at.date()-timedelta(days=35), at.date()+timedelta(days=25))
        if not sessions:
            exclusions['calendar_unavailable'] += 1
            continue
        if at.date() not in sessions or not time(9, 30) <= at.time().replace(tzinfo=None) <= time(15, 1):
            exclusions['no_trading_session_observation'] += 1
            continue
        later = [day for day in sessions if day > at.date()]
        earlier = [day for day in sessions if day < at.date()]
        if not later or not earlier:
            exclusions['calendar_unavailable'] += 1
            continue
        p, prev, op, high, low, amount, volume = [number(quote.get(key)) for key in
            ('price', 'pre_close', 'open_price', 'high', 'low', 'amount', 'volume')]
        if (any(value is None or value <= 0 for value in (p, prev, op, high, low, amount, volume))
                or not low <= min(p, op) <= max(p, op) <= high
                or quote.get('suspended') is True or quote.get('corporate_action') is True
                or quote.get('no_price_limit') is True):
            exclusions['unusable_price_or_special_session'] += 1
            continue
        change = (p/prev-1)*100
        limit = calculate_limit_price(prev, code, at.date())
        touched = high >= limit-.005
        distance = (limit/p-1)*100
        ordinary = FIRST_BOARD_POLICY['minChangePct']-1e-8 <= change <= FIRST_BOARD_POLICY['maxChangePct']+1e-8
        population['validQuotePrice'] += 1
        population['todayTouched'] += int(touched)
        population['ordinaryUntouched'] += int(ordinary and not touched)
        rows = {row.get('key'): row for row in candidate.get('indicatorEvidence') or []}
        daily_keys = ('ma5', 'ma10', 'ma20', 'high20', 'atr14')
        daily = {key: number((rows.get(key) or {}).get('value')) for key in daily_keys}
        if (any(value is None or value <= 0 for value in daily.values())
                or any(str((rows.get(key) or {}).get('asOf') or '')[:10] != earlier[-1].isoformat()
                       or not (rows.get(key) or {}).get('source') for key in daily_keys)):
            exclusions['daily_structure_unavailable_or_stale'] += 1
            continue
        # This explicit comparison prevents adjusted historical levels being
        # treated as the same price basis as the quote.
        last_close = number(candidate.get('dailyLastClose'))
        if last_close is None:
            # Old frozen scans have already recorded this alignment check.
            aligned = (candidate.get('evidenceChecks') or {}).get('daily_price_aligned')
            if aligned is None:
                aligned = (candidate.get('precisionDecision') or {}).get('metrics', {}).get('biasMa5Pct') is not None
        else:
            aligned = abs(last_close-prev) <= .011
        if not aligned:
            exclusions['daily_quote_basis_unaligned'] += 1
            continue
        vwap = amount / volume
        if not low-.01 <= vwap <= high+.01:
            exclusions['cumulative_units_inconsistent'] += 1
            continue
        context = (candidate.get('precisionDecision') or {}).get('sourceContext') or {}
        negative = any(event.get('type') in {'首亏', '续亏', '增亏', '预减'}
                       and '净利润' in str(event.get('metric') or '') for event in context.get('forecasts') or [])
        if negative:
            exclusions['known_negative_earnings'] += 1
            continue
        location = (p-low)/(high-low) if high > low else float(p >= prev)
        proximity = p / daily['high20']
        trend = sum((p >= daily['ma5'], daily['ma5'] >= daily['ma10'], daily['ma10'] >= daily['ma20'])) / 3
        yesterday = previous_session_pattern(candidate, earlier[-1], prev, code,
                                             preceding_day=earlier[-2] if len(earlier) >= 2 else None)
        extension = (p-daily['ma5']) / daily['atr14']
        if touched:
            queue, queue_label = 'continuation', '已触板延续'
        else:
            if not ordinary:
                exclusions['outside_ordinary_change_band'] += 1
                continue
            if distance < FIRST_BOARD_POLICY['minLimitDistancePct']-1e-8:
                exclusions['too_close_to_today_limit'] += 1
                continue
            if yesterday['closeAtLimit'] is True:
                exclusions['previous_session_limit_close'] += 1
                continue
            if extension > FIRST_BOARD_POLICY['maxAtrExtension']:
                exclusions['first_board_overextended'] += 1
                continue
            queue, queue_label = 'first_board', '未触板潜伏'
        branches = []
        if p >= daily['high20']*.97 and change > 0 and location >= .65:
            branches.append('平台突破蓄势')
        if touched and p >= limit*.98:
            branches.append('触板形态延续（10%价位）')
        if p > op and p > vwap and p >= daily['ma20'] and location >= .70:
            branches.append('分歧修复承接')
        if not branches:
            exclusions['no_first_board_structure' if queue == 'first_board' else 'no_continuation_structure'] += 1
            continue
        population['ordinaryUntouchedWithStructure'] += int(queue == 'first_board')
        gaps = ['初筛快照时间与报价未必一致；快照涨幅、量比和换手不参与本次排序']
        if (candidate.get('dataEligibility') or {}).get('status') != 'formal':
            gaps.append('正式交易所需20日同刻分钟或关键因子未完整；观察不等于交易数据门槛已通过')
        if not all((context.get('coverage') or {}).get(key) for key in ('forecast', 'unlock')):
            gaps.append('业绩预告或解禁覆盖未完整；观察不等于风险已排清')
        if yesterday['status'] == 'unknown':
            gaps.append(yesterday['meaning'])
        minute = rows.get('speed_5m_pct') or {}
        minute_at = timestamp(minute.get('asOf'))
        speed = number(minute.get('value'))
        minute_valid = bool(minute.get('source') and minute.get('status') == 'observed'
                            and minute_at and minute_at.date() == at.date()
                            and 0 <= (decision-minute_at).total_seconds() < 300)
        if not minute_valid or speed is None:
            speed = None
            gaps.append('当前5分钟转强证据缺失；该分量计零，不重新分配权重')
        # Different research objectives must not compete for the same five
        # places. Latent setups reward moderate extension/readiness, not heat.
        if queue == 'first_board':
            distance_atr = (daily['high20']-p)/daily['atr14']
            # A 10% move many times larger than the observed typical daily
            # range is weak evidence for this target, even in a smooth trend.
            # These common parameters are unvalidated, never stock-specific.
            limit_move_atr = (limit-prev)/daily['atr14']
            capacity = clip((FIRST_BOARD_POLICY['limitMoveAtrZeroScore']-limit_move_atr)
                            /(FIRST_BOARD_POLICY['limitMoveAtrZeroScore']-FIRST_BOARD_POLICY['limitMoveAtrFullScore']))
            components = {'highPosition': 15*clip(location),
                          'platformReadiness': 20*clip(1-abs(distance_atr-.5)/2),
                          'trend': 15*trend,
                          'lowExtension': 15*clip(1-max(0., extension-.5)/2),
                          'rangeCapacity': 20*capacity,
                          'vwapSupport': 10*clip((p/vwap-1)*100),
                          'minuteStrength': 5*clip((speed or 0)/.5)}
            penalty = min(15., (high/p-1)*100*3)
        else:
            components = {'highPosition': 25*clip(location),
                          'breakoutProximity': 25*clip((proximity-.90)/.10),
                          'trend': 20*trend,
                          'vwapSupport': 15*clip((p/vwap-1)*100/2),
                          'minuteStrength': 15*clip((speed or 0)/1)}
            penalty = min(20., max(0., extension-3)*5) + min(15., (high/p-1)*100*3)
        score = round(max(0., sum(components.values())-penalty), 2)
        reasons = [f'结构：{" / ".join(branches)}',
                   f'价格处于当日区间{location*100:.0f}%位置，距此前20日高点{(proximity-1)*100:+.2f}%',
                   f'价格较累计VWAP {(p/vwap-1)*100:+.2f}%，日线趋势条件{round(trend*3)}/3']
        if queue == 'first_board':
            reasons.insert(0, f'截至源时间当日涨跌{change:+.2f}%，盘中尚未触及10%模式价，距该价{distance:.2f}%；不与已触板股票争抢潜伏名额')
            reasons.append(f'较MA5延伸{extension:.2f}个ATR；优先观察未过度延伸的平台或修复结构')
            reasons.append(f'历史ATR14占前收{daily["atr14"]/prev*100:.2f}%；今日10%模式幅度约{limit_move_atr:.2f}个ATR，作为冲板幅度与既有波动的比较，不是概率')
        else:
            reasons.insert(0, f'盘中已触及10%模式价，当前涨跌{change:+.2f}%；仅进入延续队列，不占潜伏名额')
        signal = {'version': VERSION, 'target': 'next_session_limit_up', 'signalSession': at.date().isoformat(),
                  'targetSession': later[0].isoformat(), 'score': score, 'probability': None,
                  'scoreMeaning': MEANING, 'validationStatus': 'unvalidated_rules', 'branches': branches,
                  'reasons': reasons, 'gaps': gaps, 'executionEligible': False,
                  'queue': queue, 'queueLabel': queue_label, 'touchedLimitToday': touched,
                  'previousSessionPattern': yesterday,
                  'executionState': candidate.get('status'), 'sourceTime': at.isoformat(),
                  'priceLimitBasis': '主板10%价格结构；未核验目标日ST/除权制度，复盘独立标记价格模式',
                  'trigger': '下一交易日观察突破与承接；交易前重新核验报价、盘口及风险',
                  'invalidation': '回落跌破支撑、无法成交或新披露利空时重新评估',
                  'features': {'rangePosition': location, 'high20Ratio': proximity, 'trendConditions': trend*3,
                               'vwapPremiumPct': (p/vwap-1)*100, 'speed5mPct': speed,
                               'atrExtension': extension, 'changePct': change,
                               'atrPctOfPreviousClose': daily['atr14']/prev*100,
                               'limitMoveInAtr': (limit-prev)/daily['atr14'],
                               'limitDistancePct': distance, 'todayLimitPrice': limit,
                               'todayHigh': high, 'touchedLimitToday': touched,
                               'previousSessionCloseAtLimit': yesterday['closeAtLimit']},
                  'components': components, 'extensionAndRetreatPenalty': penalty}
        research.append({**candidate, 'nextDaySignal': signal})
    research.sort(key=lambda row: (-row['nextDaySignal']['score'], row['code']))
    first_board = [row for row in research if row['nextDaySignal']['queue'] == 'first_board']
    continuation = [row for row in research if row['nextDaySignal']['queue'] == 'continuation']
    limit = max(0, min(5, int(top_n)))
    def select(rows):
        return [{**row, 'status': 'next_day_watch', 'stateLabel': row['nextDaySignal']['queueLabel'], 'rank': rank,
                 'horizon': '下一交易日', 'selectionReasons': row['nextDaySignal']['reasons'],
                 'nextDaySignal': {**row['nextDaySignal'], 'selected': True}}
                for rank, row in enumerate(rows[:limit], 1)]
    selected, continuation_selected = select(first_board), select(continuation)
    summary = {'version': VERSION, 'target': 'next_session_limit_up', 'count': len(research),
               'selectedCount': len(selected), 'firstBoardCount': len(first_board),
               'continuationCount': len(continuation), 'continuationSelectedCount': len(continuation_selected),
               'status': 'unvalidated_rules', 'meaning': MEANING,
               'exclusionCounts': dict(exclusions), 'researchedCount': len(seen),
               'populationCounts': dict(population), 'firstBoardPolicy': dict(FIRST_BOARD_POLICY),
               'snapshotPolicy': '快照时间未核实的量比、换手及涨幅只安排初筛队列，不进入T+1分数；未深研股票不作排除结论',
               'weights': {'first_board': FIRST_BOARD_WEIGHTS, 'continuation': CONTINUATION_WEIGHTS}}
    return {'nextDayWatchlist': selected, 'nextDayContinuationWatchlist': continuation_selected,
            'nextDayResearch': summary, 'researchCandidates': research}


def project_frozen_next_day(run):
    """Compatibility view only. Do not rewrite or claim a historical selection."""
    if 'nextDayWatchlist' in run:
        return run
    candidates = [item for key in ('candidates', 'controls', 'precisionResearch', 'windvanes', 'evidenceInsufficient')
                  for item in run.get(key) or []]
    result = build_next_day_watchlist(candidates, run.get('generatedAt') or run.get('as_of'))
    return {**run, 'nextDayWatchlist': result['nextDayWatchlist'],
            'nextDayContinuationWatchlist': result['nextDayContinuationWatchlist'],
            'nextDayResearch': {**result['nextDayResearch'], 'retrospectiveProjection': True,
                               'projectionMeaning': '按已存扫描证据重排供查看；不是当时已发出的T+1推荐，不进入新规则命中率'}}


def next_day_outcome(signal, bars, as_of, *, calendar_provider=None):
    """T+1 labels use T's actual close, never the intraday recommendation price."""
    as_of = timestamp(as_of)
    day = date.fromisoformat(signal['signalSession'])
    calendar = calendar_provider or _sessions
    sessions = session_dates(calendar, day, day+timedelta(days=25))
    following = [value for value in sessions if value > day]
    target = following[0] if day in sessions and following else None
    result = {'version': signal.get('version', VERSION), 'queue': signal.get('queue', 'legacy_mixed'),
              'status': 'pending', 'targetSession': target.isoformat() if target else None,
              'touchLimit': None, 'closeAtLimit': None, 'onePriceLimit': None,
              'executionVerified': False, 'reason': None}
    if target is None or as_of is None or as_of < datetime.combine(target, time(15, 1), TZ):
        return result
    rows = {timestamp(row.get('end')): row for row in bars if timestamp(row.get('end')) is not None
            and timestamp(row.get('end')) <= as_of}
    closing = rows.get(datetime.combine(day, time(15), TZ)) or {}
    previous = number(closing.get('close'))
    expected = [datetime.combine(target, time(9, 31), TZ)+timedelta(minutes=i) for i in range(120)]
    expected += [datetime.combine(target, time(13, 1), TZ)+timedelta(minutes=i) for i in range(120)]
    target_rows = [rows.get(at) for at in expected]
    if not previous or any(row is None for row in target_rows):
        return {**result, 'status': 'incomplete', 'reason': '缺少T收盘或T+1完整240根分钟；缺口不记为未涨停'}
    for row in target_rows:
        values = [number(row.get(key)) for key in ('open', 'high', 'low', 'close')]
        if (any(value is None or value <= 0 for value in values)
                or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]
                or row.get('corporate_action') or row.get('no_price_limit')):
            return {**result, 'status': 'incomplete', 'reason': '目标日价格或涨停制度无法核验'}
    volumes = [number(row.get('volume_shares')) for row in target_rows]
    if (any(row.get('suspended') is True for row in target_rows)
            or all(value is not None and value <= 0 for value in volumes)):
        return {**result, 'status': 'incomplete', 'reason': '目标日已知停牌或全天无成交，不能判定涨停价格模式'}
    # Archived minute OHLC does not prove historical ST status or ex-right limits.
    # Keep the measurable 10% pattern separate from exchange-confirmed outcomes.
    limit = calculate_limit_price(previous, signal['code'], target)
    return {**result, 'status': 'mature_price_pattern', 'previousSessionClose': previous,
            'limitPrice': limit, 'limitBasis': '主板10%分币四舍五入价格模式；未核验目标日除权/ST状态',
            'touchLimit': max(row['high'] for row in target_rows) >= limit-.005,
            'closeAtLimit': abs(target_rows[-1]['close']-limit) <= .005,
            'onePriceLimit': all(abs(row[key]-limit) <= .005 for row in target_rows for key in ('open', 'high', 'low', 'close')),
            'labelEndAt': datetime.combine(target, time(15, 1), TZ).isoformat()}


def review_next_day_signals(service, as_of, cache=None):
    """Append independent labels for prospectively frozen T+1 signals and controls."""
    cache = cache if cache is not None else {}
    changed = []
    with service._connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS next_day_outcomes(signal_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, result_json TEXT NOT NULL)')
        rows = db.execute('SELECT e.signal_id,e.code,e.selected,e.snapshot_json FROM signal_events e '
                          'JOIN signal_runs r ON r.run_id=e.run_id WHERE r.trading_day<=?', (as_of.date().isoformat(),)).fetchall()
        for row in rows:
            snap = json.loads(row['snapshot_json'])
            signal = snap.get('nextDaySignal') or {}
            if signal.get('version') not in SUPPORTED_VERSIONS:
                continue
            old = db.execute('SELECT result_json FROM next_day_outcomes WHERE signal_id=?', (row['signal_id'],)).fetchone()
            if old and json.loads(old[0]).get('status') == 'mature_price_pattern':
                continue
            if row['code'] not in cache:
                try:
                    cache[row['code']] = service._bar_rows(service.bar_fetcher(row['code'], as_of), as_of)
                except Exception:
                    cache[row['code']] = []
            outcome = next_day_outcome({**signal, 'code': row['code']}, cache[row['code']], as_of,
                                       calendar_provider=service.calendar_provider)
            if outcome['status'] == 'pending':
                continue
            outcome.update(signalId=row['signal_id'], code=row['code'], selected=bool(signal.get('selected')))
            db.execute('INSERT INTO next_day_outcomes VALUES(?,?,?) ON CONFLICT(signal_id) DO UPDATE '
                       'SET reviewed_at=excluded.reviewed_at,result_json=excluded.result_json',
                       (row['signal_id'], as_of.isoformat(), json.dumps(outcome, ensure_ascii=False)))
            changed.append(outcome)
    mature = [row for row in changed if row['status'] == 'mature_price_pattern']
    return {'version': VERSION, 'items': changed, 'updated': len(changed), 'maturePatterns': len(mature),
            'meaning': '独立T+1触及/收盘涨停价格模式复盘；未验证交易所涨停制度，不等于成交或收益；缺失不记失败'}


def read_next_day_reviews(service, filters=None):
    filters = filters or {}
    as_of = timestamp(service.clock())
    versions = sorted(SUPPORTED_VERSIONS)
    where = ["json_extract(e.snapshot_json,'$.nextDaySignal.version') IN ("
             + ','.join('?' for _ in versions) + ')']
    args = list(versions)
    for key, column in (('date', 'r.trading_day'), ('symbol', 'e.code'), ('version', 'r.version')):
        if filters.get(key):
            where.append(column+'=?')
            args.append(str(filters[key]))
    with service._connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='next_day_outcomes'").fetchone()
        result_columns = 'o.reviewed_at,o.result_json' if exists else 'NULL reviewed_at,NULL result_json'
        query = ('SELECT e.signal_id,e.code,e.snapshot_json,r.trading_day,' + result_columns
                 + ' FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id')
        if exists:
            query += ' LEFT JOIN next_day_outcomes o ON o.signal_id=e.signal_id'
        if where:
            query += ' WHERE '+' AND '.join(where)
        query += " ORDER BY json_extract(e.snapshot_json,'$.nextDaySignal.selected') DESC,r.trading_day DESC,e.code LIMIT ?"
        rows = db.execute(query, (*args, max(1, min(int(filters.get('limit') or 1000), 5000)))).fetchall()
    items = []
    for row in rows:
        signal = json.loads(row['snapshot_json']).get('nextDaySignal') or {}
        if row['result_json']:
            outcome = json.loads(row['result_json'])
        else:
            try:
                outcome = next_day_outcome({**signal, 'code': row['code']}, [], as_of,
                                           calendar_provider=service.calendar_provider)
            except (KeyError, TypeError, ValueError):
                outcome = {'version': signal.get('version'), 'queue': signal.get('queue'),
                           'status': 'calendar_unavailable', 'targetSession': None,
                           'touchLimit': None, 'closeAtLimit': None, 'onePriceLimit': None,
                           'executionVerified': False}
            if outcome.get('targetSession') is None:
                outcome.update(status='calendar_unavailable', reason='交易日历或原信号日期缺失，无法确定复盘到期日')
            elif outcome['status'] == 'pending':
                outcome['reason'] = '尚未到下一交易日15:01，未执行复盘'
            else:
                outcome.update(status='review_not_run', reason='已到下一交易日收盘，尚无复盘执行记录')
        items.append({**outcome, 'signalId': row['signal_id'], 'code': row['code'],
                      'selected': bool(signal.get('selected')), 'signalDate': row['trading_day'],
                      'reviewedAt': row['reviewed_at']})
    return {'version': VERSION, 'items': items, 'count': len(items), 'asOf': as_of.isoformat() if as_of else None,
            'statusCounts': dict(Counter(item['status'] for item in items)), 'validationStatus': 'unvalidated_rules',
            'meaning': '主板10%价格模式观测，非已核实涨停命中率或交易收益；保留未完成样本'}
