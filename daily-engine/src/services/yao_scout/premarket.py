"""Previous-session plans for the auction and first five opening minutes.

These are a separate, conditional research lane. Ordinary realtime quotes do
not supply indicative auction prices, matched volume or unmatched orders, and
this module never upgrades them to confirmed auction signals or trade fills.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import re

from .daily_opportunities import TZ, finite, is_market_open, quote_check
from .research_policy import DEFAULT_PARAMETERS

VERSION = 'king-premarket-plan-20261016'


def _local(now):
    # The service's historical naive callers mean Beijing time, never host time.
    return now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)


def opening_phase(now):
    """Return the Beijing opening phase, or None from 09:35/on closed days."""
    now = _local(now)
    clock = now.time().replace(tzinfo=None)
    if clock >= time(9, 35) or now.weekday() >= 5 or not is_market_open('cn', now.date()):
        return None
    if clock < time(9, 15):
        code, label, instruction = 'overnight_plan', '昨夜计划', '以最近已完成交易日准备条件；等待当日竞价与开盘核验'
    elif clock < time(9, 20):
        code, label, instruction = 'auction_cancellable', '集合竞价·可撤单阶段', '09:15—09:20委托可撤；早期报价不能确认最终开盘或承接'
    elif clock < time(9, 25):
        code, label, instruction = 'auction_non_cancellable', '集合竞价·不可撤单阶段', '09:20—09:25不接受撤单；等待最终开盘价格，不能把普通报价当竞价匹配'
    elif clock < time(9, 30):
        code, label, instruction = 'auction_priced', '竞价定价后', '09:25—09:30核对已产生的开盘价与成交；连续交易承接仍待开盘确认'
    else:
        code, label, instruction = 'opening_handoff', '开盘交接', '09:30—09:35沿用昨夜条件并核验实际开盘成交；完整分钟形成后交接盘中规则'
    return {'code': code, 'label': label, 'instruction': instruction,
            'asOf': now.isoformat(), 'validUntil': now.replace(hour=9, minute=35, second=0, microsecond=0).isoformat()}


def previous_session(now):
    """The latest completed Chinese exchange session before the decision date."""
    now = _local(now)
    day = now.date() - timedelta(days=1)
    while day.weekday() >= 5 or not is_market_open('cn', day):
        day -= timedelta(days=1)
    return day


def _at(value):
    try:
        at = datetime.fromisoformat(str(value))
        return at.astimezone(TZ) if at.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _daily_valid(daily, now, previous):
    metrics = daily.get('metrics') or {}
    required = ('last_close', 'ma5', 'ma10', 'ma20', 'high20', 'low20', 'atr14')
    numbers = {key: finite(metrics.get(key)) for key in required}
    source = daily.get('source')
    if (not source or '未记录' in str(source) or daily.get('asOf') != previous
            or (finite(metrics.get('history_sessions')) or 0) < 20
            or any(value is None or value <= 0 for value in numbers.values())):
        return False
    for key in ('observedAt', 'availableAt'):
        if key in daily and (_at(daily[key]) is None or _at(daily[key]) > now):
            return False
    previous_packet = daily.get('previousSession') or {}
    return (not previous_packet.get('asOf') or previous_packet['asOf'] == previous) and (
        numbers['low20'] <= numbers['last_close'] <= numbers['high20'])


def _quote_state(quote, code, now):
    if not quote:
        return 'missing', ['未取得当日普通报价，仅保留昨夜计划']
    stamp = _at(quote.get('provider_timestamp') or quote.get('source_time'))
    if stamp and stamp > now:
        return 'future', ['报价源时间晚于决策时点，未用于计划更新']
    if stamp and (stamp.date() != now.date() or (now-stamp).total_seconds() > 30):
        return 'stale', ['报价超过30秒或非当日，仅保留昨夜计划']
    gaps = [gap for gap in quote_check(quote, code, now) if gap != '必要价格字段不足']
    price, previous = finite(quote.get('price')), finite(quote.get('pre_close'))
    if price is None or previous is None or price <= 0 or previous <= 0:
        gaps.append('普通报价现价或前收盘无效')
    for key in ('open_price', 'high', 'low', 'amount', 'volume'):
        if quote.get(key) is not None and (finite(quote[key]) is None or finite(quote[key]) < 0):
            gaps.append('普通报价价格或成交字段无效')
    high, low, change = (finite(quote.get(key)) for key in ('high', 'low', 'change_pct'))
    if high and low and price and not low <= price <= high:
        gaps.append('普通报价高低价与现价不一致')
    if price and previous and change is not None and abs((price/previous-1)*100-change) > .15:
        gaps.append('普通报价涨跌幅不一致')
    return ('invalid', list(dict.fromkeys(gaps))) if gaps else ('fresh', [])


def _event_risks(context, now):
    """Use only already available disclosures; missing coverage remains unknown."""
    gaps, blocked = list(context.get('gaps') or []), []
    coverage = context.get('coverage') or {}
    if not all(coverage.get(key) for key in ('forecast', 'unlock')):
        gaps.append('业绩预告或解禁覆盖不完整，昨夜事件风险待补充核验')
    def known(row):
        times = [_at(row.get(key)) for key in ('observedAt', 'availableAt', 'firstSeenAt')]
        valid = bool(row.get('source') and all(at is not None and at <= now for at in times)
                     and times[2] <= times[0] and times[2] <= times[1])
        if not valid:
            gaps.append('公告缺少有效来源或可知时间，未用于当前判断')
        return valid
    for event in context.get('forecasts') or []:
        if known(event) and event.get('type') in DEFAULT_PARAMETERS['entry']['negativeForecastTypes'] and '净利润' in str(event.get('metric') or ''):
            blocked.append('known_negative_earnings')
    unlock = 0.
    for event in context.get('unlocks') or []:
        if not known(event):
            continue
        try:
            days = (date.fromisoformat(str(event.get('eventDate'))) - now.date()).days
        except ValueError:
            gaps.append('解禁日期缺失，风险待核验')
            continue
        value = finite(event.get('floatRatioPct'))
        if value is None or value < 0:
            gaps.append('解禁比例缺失，风险待核验')
        elif 0 <= days <= 14:
            unlock += value
    if unlock >= DEFAULT_PARAMETERS['profiles']['regular']['max_unlock_pct']:
        blocked.append('known_large_unlock')
    return list(dict.fromkeys(gaps)), blocked


def build_premarket_candidates(queued, daily_by_code, quotes_by_code, decision_at, *, top_n=5, contexts=None):
    """Build up to five daily-backed plans without minute, sector or fund gates."""
    now = _local(decision_at)
    phase = opening_phase(now)
    excluded = Counter()
    assessment = {'version': VERSION, 'asOf': now.isoformat(), 'decisionAt': now.isoformat(),
                  'sessionDate': now.date().isoformat(), 'marketPhase': phase,
                  'phase': phase['code'] if phase else None, 'phaseLabel': phase['label'] if phase else None,
                  'expiresAt': phase['validUntil'] if phase else None,
                  'status': 'outside_opening_window' if phase is None else 'completed',
                  'auctionDataStatus': 'unavailable', 'auctionConfirmed': False,
                  'auctionDataGaps': ['当前来源仅普通报价，未提供可核验的竞价虚拟匹配价、匹配量和未匹配委托'],
                  'minuteDataRequired': False, 'executable': False,
                  'meaning': '最近已完成交易日的条件推荐；不等于盘中正式入选、竞价确认、成交或收益预测',
                  'validationStatus': 'unvalidated_starting_points', 'excludedCounts': {}, 'count': 0,
                  'dailyUsableCount': 0, 'candidateCount': 0, 'quoteMaxAgeSeconds': 30, 'quoteFreshnessSeconds': 30}
    result = {'premarketCandidates': [], 'premarketAssessment': assessment}
    if phase is None:
        return result
    previous = previous_session(now).isoformat()
    assessment['baselineAsOf'] = previous
    assessment['baselineSession'] = previous
    seen, candidates, quote_coverage = set(), [], Counter()
    for item in queued:
        code, name = str(item.get('code') or ''), str(item.get('name') or item.get('code') or '')
        if code in seen:
            continue
        seen.add(code)
        raw_quote = quotes_by_code.get(code) or {}
        quote_status, quote_gaps = _quote_state(raw_quote, code, now)
        quote_coverage[quote_status] += 1
        if not re.fullmatch(r'(600|601|603|605|000|001|002|003)\d{3}', code) or re.search(r'ST|退|整理|停牌', name, re.I):
            excluded['unsupported_or_risk_board'] += 1
            continue
        daily = daily_by_code.get(code) or {}
        if not _daily_valid(daily, now, previous):
            excluded['daily_missing_stale_or_invalid'] += 1
            continue
        assessment['dailyUsableCount'] += 1
        d = daily['metrics']
        if not d['ma5'] > d['ma10'] > d['ma20'] or d['last_close'] < d['ma20']:
            excluded['previous_session_trend_not_confirmed'] += 1
            continue
        context = (contexts or {}).get(code) or {}
        event_gaps, event_blocked = _event_risks(context, now)
        if event_blocked:
            excluded.update(event_blocked)
            continue
        quote = raw_quote if quote_status == 'fresh' else {}
        if quote and any(quote.get(key) is True for key in ('suspended', 'locked_limit_up', 'corporate_action', 'no_price_limit')):
            excluded['suspended_locked_or_special_session'] += 1
            continue
        if quote and abs(float(quote['pre_close'])-d['last_close']) > .011:
            excluded['daily_quote_price_basis_mismatch'] += 1
            continue
        # This is a price check, never an auction imbalance/matching inference.
        price = float(quote['price']) if quote else d['last_close']
        support = max((d[key] for key in ('ma20', 'low20') if 0 < d[key] < d['last_close']), default=None)
        if support is None or (quote and price <= support):
            excluded['daily_support_invalidated'] += 1
            continue
        resistance = min((value for key in ('high20', 'high60') if (value := finite(d.get(key))) and value > price), default=None)
        amount, volume = (finite(quote.get(key)) for key in ('amount', 'volume'))
        open_price, high, low = (finite(quote.get(key)) for key in ('open_price', 'high', 'low'))
        actual_open = bool(quote and phase['code'] in {'auction_priced', 'opening_handoff'}
                           and _at(quote.get('provider_timestamp') or quote.get('source_time')).time().replace(tzinfo=None)
                           >= (time(9, 30) if phase['code'] == 'opening_handoff' else time(9, 25))
                           and not quote_check(quote, code, now) and amount and amount > 0 and volume and volume > 0
                           and low-.01 <= amount/volume <= high+.01)
        upper = finite(quote.get('limit_up'))
        if quote and upper and price >= upper-.005 and (not actual_open or (low and low >= upper-.005)):
            excluded['limit_up_without_buyable_trade_evidence'] += 1
            continue
        plan_status = 'overnight_plan' if not quote else 'awaiting_auction_confirmation'
        if phase['code'] == 'auction_priced':
            plan_status = 'opening_price_observed' if actual_open else 'awaiting_open_price'
        elif phase['code'] == 'opening_handoff':
            plan_status = 'opening_conditional' if actual_open else 'awaiting_open_trade'
        previous_high = finite((daily.get('previousSession') or {}).get('high')) or d['high20']
        trigger = previous_high
        ceiling = min((resistance+support)/2, resistance-d['atr14']) if resistance else None
        ceiling = ceiling if ceiling is not None and ceiling > support else None
        if ceiling and quote and price > ceiling:
            plan_status = 'avoid_chasing'
        gaps = list(daily.get('gaps') or []) + event_gaps + quote_gaps + assessment['auctionDataGaps']
        if any(finite(d.get(key)) is None for key in ('last_volume', 'last_amount')):
            gaps.append('上一交易日量额不完整，量价配合未确认；日线趋势仅作条件计划')
        if phase['code'] == 'opening_handoff' and not actual_open:
            gaps.append('开盘后的有效价格与成交量额尚未联合确认，继续保留条件计划')
        if not actual_open:
            gaps.append('竞价高低开幅度尚未确认；普通最新价不代替最终开盘价')
        evidence = []
        labels = {'last_close': '昨收', 'ma5': 'MA5', 'ma10': 'MA10', 'ma20': 'MA20',
                  'high20': '20日压力', 'low20': '20日低点', 'atr14': 'ATR14',
                  'last_volume': '上一交易日成交量', 'last_amount': '上一交易日成交额',
                  'relative_volume_last_vs_prior20': '上一交易日成交量/此前20日中位数',
                  'relative_amount_last_vs_prior20': '上一交易日成交额/此前20日中位数'}
        for key, label in labels.items():
            value = finite(d.get(key))
            evidence.append({'key': key, 'label': label, 'value': value,
                             'unit': (daily.get('liquidityEvidence') or {}).get('volumeUnit' if key == 'last_volume' else 'amountUnit', 'provider_native') if key in {'last_volume', 'last_amount'} else '倍' if key.startswith('relative_') else '元',
                             'source': daily['source'], 'asOf': daily['asOf'],
                             'status': 'observed' if value is not None else 'missing', 'role': '上一交易日结构'})
        reasons = [f"截至{previous}的{int(d['history_sessions'])}个已完成交易日：MA5>MA10>MA20，昨收维持趋势结构",
                   f'以昨高{trigger:.2f}元作为延续观察位，支撑{support:.2f}元、ATR14 {d["atr14"]:.2f}元用于核对开盘位置',
                   phase['instruction']]
        risks = ['普通报价未确认竞价匹配强度；候选不代表可以买到或已成交',
                 '历史压力不是目标价，ATR不是止损成交保证；A股T+1约束仍适用']
        if resistance is None:
            risks.append('当前参考价上方无已验证历史压力，剩余空间保持未知')
        elif ceiling is None:
            risks.append('历史压力与支撑间距不足以覆盖ATR风险距离，暂不形成买入价格上限')
        no_chase = (f'超过{ceiling:.2f}元须重新评估：该上限要求到历史压力的空间至少覆盖到支撑的距离及ATR14；不是收益预测'
                    if ceiling else '缺少足够历史压力空间，买入上限未知，等待重新评估')
        candidate = {'code': code, 'name': name, 'status': 'premarket', 'stateLabel': phase['label']+'·条件推荐',
                     'strategyBranch': '昨夜趋势与开盘条件计划', 'planStatus': plan_status,
                     'marketPhase': phase['code'], 'marketPhaseLabel': phase['label'],
                     'version': VERSION, 'decision_at': now.isoformat(), 'validUntil': phase['validUntil'],
                     'baselineAsOf': previous, 'baselineSource': daily['source'], 'dailyLastClose': d['last_close'],
                     'quote': dict(quote), 'quoteStatus': quote_status, 'price': price,
                     'source': quote.get('source') if quote else daily['source'],
                     'sourceTime': quote.get('provider_timestamp') or quote.get('source_time') if quote else previous,
                     'referencePriceKind': 'current_quote' if quote else 'previous_close',
                     'auctionConfirmed': False, 'auctionEvidence': {'status': 'unavailable', 'source': None, 'asOf': None},
                     'openingPrice': open_price if actual_open else None,
                     'openingGapPct': (open_price/d['last_close']-1)*100 if actual_open else None,
                     'openingTradeObserved': actual_open, 'executable': False, 'entryEligible': False,
                     'support': support, 'resistance': resistance, 'atr14': d['atr14'], 'triggerPrice': trigger,
                     'triggerCondition': f'核验最终开盘价后，观察价格在{support:.2f}元支撑上方承接、向昨高{trigger:.2f}元延续；须有当日新鲜成交及可买盘口',
                     'invalidCondition': f'跌破{support:.2f}元、停牌、一字封死无可买成交、前收盘口径不一致或报价失效时停止采用；高开远离支撑时重新评估',
                     'noChaseAbove': ceiling,
                     'noChaseCondition': no_chase+'；一字封板、无可买成交或报价失效时不追',
                     'selectionReasons': reasons, 'reasons': reasons, 'risks': risks, 'riskFlags': risks,
                     'dataGaps': list(dict.fromkeys(gaps)), 'indicatorEvidence': evidence, 'evidence': evidence,
                     'probabilities': {}, 'confidence': None, 'validationStatus': 'unvalidated_starting_points'}
        candidates.append(candidate)
    # No fabricated probability or heat score: current evidence first, then
    # prior-session liquidity when recorded, with stock code as a stable tie.
    candidates.sort(key=lambda row: (not row['openingTradeObserved'], row['quoteStatus'] != 'fresh',
                                    -(finite((daily_by_code[row['code']].get('metrics') or {}).get('last_amount')) or 0), row['code']))
    result['premarketCandidates'] = candidates[:max(0, min(5, int(top_n)))]
    assessment.update(count=len(result['premarketCandidates']), qualifiedPlanCount=len(candidates),
                      candidateCount=len(result['premarketCandidates']),
                      researchedCount=len(seen), excludedCounts=dict(excluded),
                      ordinaryQuoteCount=quote_coverage['fresh'],
                      quoteCoverage={'researchedCount': len(seen), 'freshCount': quote_coverage['fresh'], **dict(quote_coverage)},
                      openingTradeObservedCount=sum(row['openingTradeObserved'] for row in result['premarketCandidates']))
    return result
