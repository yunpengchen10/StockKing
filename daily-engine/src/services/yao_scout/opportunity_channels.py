"""Independent E/M research opportunities from evidence available at decision time.

This pure module performs no I/O and grants no execution or model-training
eligibility. Missing confirmation is not adverse evidence. Early opportunities
do not inherit the formal model's historical, sector, VWAP or profit gates.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

VERSION = 'opportunity-em-20261029-v2'
TZ = ZoneInfo('Asia/Shanghai')


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.astimezone(TZ) if value.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fresh(value, now, seconds):
    stamp = _at(value)
    return bool(stamp and stamp.date() == now.date() and 0 <= (now-stamp).total_seconds() <= seconds)


def _source(value):
    return bool(value and not any(word in str(value).lower() for word in ('unknown', 'missing', '未取得', '未记录')))


def policy_contract():
    return {'version': VERSION, 'validationStatus': 'prospective_rules_unvalidated',
        'channels': {'E': '状态跃迁、反转或加速的早期机会', 'M': '已有延续或回踩再起证据的确认机会'},
        'missingPolicy': 'show_unknown_never_infer_adverse_or_fabricate_evidence',
        'notRequired': ['twenty_day_baseline', 'above_vwap', 'sector_synchrony', 'estimated_fund_flow', 'trained_model'],
        'hardVetoes': ['stale_or_conflicting_quote', 'tainted_market_data', 'no_buy_window',
                       'strong_distribution_evidence', 'verified_major_adverse_event_or_restriction'],
        'ranking': 'state_change_with_held_structure_then_directional_acceleration_then_path_efficiency_then_support_space_buyability',
        'rankScoreMeaning': '当轮合格机会的规则名次分，不是胜率、收益预测或成交保证',
        'maximumRecommendations': 5, 'probability': None, 'formalRecommendationAllowed': False,
        'trainingEligible': False, 'executionVerified': False,
        'meaning': '独立规则机会；至少需要真实时间变化及主动性、催化或板块支持，不以当日涨幅快照冒充启动。'}


def _previous_rows(previous):
    if isinstance(previous, list):
        return previous
    if not isinstance(previous, dict):
        return []
    if previous.get('code'):
        return [previous]
    found = []
    for key in ('researchCandidates', 'recommendations', 'observations', 'opportunityChannels', 'opportunities'):
        found.extend(_previous_rows(previous.get(key)))
    return found


def _known_event(event, now):
    if not isinstance(event, dict) or not _source(event.get('source')):
        return False
    available = _at(event.get('availableAt'))
    first = _at(event.get('firstSeenAt'))
    published = _at(event.get('publishedAt') or event.get('sourceTime'))
    return bool(available and first and published and published <= available <= now and first <= available)


def _known_disclosure(event, code, now):
    """A date-only public listing can be known without inventing publication time."""
    if not isinstance(event, dict) or not _source(event.get('source')) or str(event.get('code')) != code:
        return False
    available, first, observed = (_at(event.get(key)) for key in ('availableAt', 'firstSeenAt', 'observedAt'))
    try:
        published = datetime.strptime(str(event.get('publishedDate')), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return False
    return bool(available and first and observed and first <= available <= observed <= now
                and published <= available.date() and event.get('evidenceScope') == 'title_only'
                and event.get('impact') == 'neutral')


def _quote_assessment(row, now):
    quote = row.get('quote') or {}
    hard, missing = [], []
    stamp = quote.get('provider_timestamp') or quote.get('source_time')
    price = _number(quote.get('price'))
    if (str(quote.get('code') or '').split('.')[0] != str(row.get('code') or '')
            or not _source(quote.get('source')) or not _fresh(stamp, now, 30)
            or quote.get('is_stale') is True or price is None or price <= 0):
        hard.append('报价来源、代码、价格或30秒时效未通过核验')
    prev, opened, high, low, change = (_number(quote.get(key)) for key in
        ('pre_close', 'open_price', 'high', 'low', 'change_pct'))
    if any(quote.get(key) is not None and _number(quote[key]) is None
           for key in ('price', 'pre_close', 'open_price', 'high', 'low', 'change_pct')):
        hard.append('报价含非有限数值或布尔价格，数据不可核验')
    if price and ((high is not None and (high <= 0 or price > high))
                  or (low is not None and (low <= 0 or price < low))
                  or (high and low and high < low)
                  or (opened and high and low and not low <= opened <= high)
                  or (prev and change is not None and abs((price/prev-1)*100-change) > .15)):
        hard.append('当时报价的价格范围或涨跌幅相互冲突')
    if quote.get('quote_usable_for_current_price_check') is False:
        hard.append('行情源明确标记当前报价不可用')
    amount, volume = _number(quote.get('amount')), _number(quote.get('volume'))
    if amount is not None and volume is not None and (amount < 0 or volume < 0
            or (amount > 0 and volume == 0)
            or (amount > 0 and volume > 0 and high and low and not low-.01 <= amount/volume <= high+.01)):
        hard.append('成交额、成交股数与价格范围冲突')
    if any(quote.get(key) is True for key in ('suspended', 'locked_limit_up', 'trading_restricted')):
        hard.append('停牌、封死涨停或明确交易限制，没有正常买窗')
    clock = now.time().replace(tzinfo=None)
    # The scheduled 14:55 delivery's separate 14:57 expiry belongs to its
    # caller. It must not become a new global cutoff for manual research.
    in_session = time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(15)
    if not in_session:
        hard.append('当前不在盘中研究时间内')
    book = quote.get('order_book') or (quote.get('public_quote_metadata') or {}).get('order_book') or {}
    book_time = book.get('source_time')
    buy = {'status': 'unknown', 'askPrice': None, 'sourceTime': book_time, 'spreadPct': None}
    if _fresh(book_time, now, 30) and _source(book.get('source') or quote.get('source')):
        asks = book.get('asks')
        offers = [ask for ask in (asks or []) if isinstance(ask, dict)
                  and (_number(ask.get('price')) or 0) > 0
                  and (_number(ask.get('volume_shares')) or 0) > 0]
        if isinstance(asks, list) and not offers:
            hard.append('新鲜盘口没有有效卖盘，正常买窗不存在')
            buy['status'] = 'unavailable'
        elif offers:
            ask = min(_number(item['price']) for item in offers)
            bids = [item for item in (book.get('bids') or []) if isinstance(item, dict)
                    and (_number(item.get('price')) or 0) > 0 and (_number(item.get('volume_shares')) or 0) > 0]
            bid = max((_number(item['price']) for item in bids), default=None)
            if bid and bid > ask:
                hard.append('盘口买卖报价倒挂，数据相互冲突')
            else:
                buy.update(status='observed', askPrice=ask,
                           spreadPct=(ask/bid-1)*100 if bid else None)
    if buy['status'] == 'unknown':
        missing.append('正常卖盘尚未核验，暂列观察')
    return quote, price, hard, missing, buy


def _minute_assessment(row, packet, now, hard, missing):
    """Use current unstandardized metrics even when 20-day z-scores are absent."""
    packet = packet or {}
    lineages = [value for value in (row.get('dataLineage'), packet.get('dataLineage')) if isinstance(value, dict)]
    tainted_history = any(value.get('status') == 'tainted' for value in lineages)
    current_sessions = [session for lineage in lineages for session in lineage.get('sessions') or []
                        if str(session.get('code')) == str(row.get('code')) and session.get('day') == now.date().isoformat()]
    if tainted_history:
        current_verified = bool(current_sessions and all(session.get('status') == 'verified'
            and _at(session.get('verifiedThrough')) and _at(packet.get('asOf'))
            and _at(session['verifiedThrough']) >= _at(packet['asOf']) for session in current_sessions))
        if not current_verified:
            hard.append('分钟行情已被标记污损且当日证据未独立核验，不能使用其变化证据')
            return {}, {}, None
        missing.append('历史分钟存在污损，仅使用独立核验的当日变化；不使用历史标准化因子')
    if packet.get('code') is not None and str(packet['code']) != str(row.get('code')):
        hard.append('分钟证据的股票代码与候选不一致')
        return {}, {}, None
    stamp = _at(packet.get('asOf'))
    if stamp and stamp > now:
        hard.append('分钟证据晚于决策时点，存在未来数据冲突')
        return {}, {}, None
    fresh = bool(_source(packet.get('source')) and _fresh(stamp, now, 299))
    if packet.get('sourceTimePrecision') == 'minute_label' and stamp and stamp+timedelta(minutes=1) > now:
        fresh = False
    if not fresh:
        missing.append('缺少来源明确、当时已完成且新鲜的分钟变化证据')
        return {}, {}, None
    raw = {key: _number(value) for key, value in (packet.get('v11Inputs') or {}).items()}
    metrics = {key: _number(value) for key, value in (packet.get('metrics') or {}).items()}
    for n in (1, 3, 5, 15):
        raw.setdefault(f'r{n}_pct', metrics.get(f'speed_{n}m_pct'))
    for key in ('amount_3m', 'relative_amount_3m_20d', 'local_high_5m', 'local_low_5m', 'vwap'):
        raw.setdefault(key, metrics.get(key))
    if tainted_history:
        current_keys = {'r1_pct', 'r3_pct', 'r5_pct', 'r15_pct', 'a1_pct_per_min', 'a3_pct_per_min',
            'amount_3m', 'amount_15m', 'local_high_5m', 'local_low_5m', 'vwap',
            'vwap_retest_success', 'vwap_reclaim', 'failed_breakout_risk', 'lower_high_risk'}
        raw = {key: value for key, value in raw.items() if key in current_keys}
    if raw.get('a1_pct_per_min') is None and raw.get('r1_pct') is not None and raw.get('r5_pct') is not None:
        raw['a1_pct_per_min'] = raw['r1_pct']-raw['r5_pct']/5
    if raw.get('a3_pct_per_min') is None and raw.get('r3_pct') is not None and raw.get('r15_pct') is not None:
        raw['a3_pct_per_min'] = raw['r3_pct']/3-raw['r15_pct']/15
    if not any(raw.get(f'r{n}_pct') is not None for n in (1, 3, 5, 15)):
        missing.append('分钟包没有可核验涨速，不能由日涨幅补造状态跃迁')
    if (_number(packet.get('historyDays')) or 0) < 20:
        missing.append('20日同刻基准未齐；使用当日真实变化，历史异常程度未知')
    if (packet.get('dataLineage') or row.get('dataLineage') or {}).get('status') != 'verified':
        missing.append('分钟来源尚未完成独立交叉核验；保持规则研究属性')
    trend = packet.get('trendEvidence') or {}
    trend_metrics = (trend.get('metrics') or {}) if _at(trend.get('asOf')) == stamp else {}
    return raw, {key: _number(value) for key, value in trend_metrics.items()}, stamp


def _auxiliary(row, context, now, minute_at, support, missing, counter, hard):
    quote = row.get('quote') or {}
    active = _number(quote.get('active_buy_share_pct'))
    if not (_source(quote.get('active_flow_source')) and _fresh(quote.get('active_flow_as_of'), now, 30)
            and active is not None and 0 <= active <= 100):
        active = None
    if active is None:
        missing.append('主动买卖分类未知')
    elif active > 50:
        support.append('新鲜外内盘口径买方占优（累计分类，不等于机构资金）')
    elif active < 50:
        counter.append('累计主动分类仍偏卖方，需继续观察近期修复')
    flow, sector_relative, sector_return = None, None, None
    for name in ('fundEvidence', 'sectorEvidence'):
        packet = row.get(name) or {}
        valid = bool(_source(packet.get('source')) and _fresh(packet.get('asOf'), now, 299)
                     and minute_at and _at(packet.get('asOf')) == minute_at)
        values = (packet.get('metrics') or {}) if valid else {}
        if name == 'fundEvidence':
            flow = _number(values.get('main_net_flow_3m'))
        else:
            sector_relative = _number(values.get('sector_relative_5m_pct'))
            sector_return = _number(values.get('sector_return_5m_pct'))
    if flow is None:
        missing.append('同窗资金估算未知；不作为早期机会否决条件')
    elif flow > 0:
        support.append('同窗近3分钟供应商估算净额转正')
    elif flow < 0:
        counter.append('同窗近3分钟供应商估算净额为负')
    leadership = sector_relative is not None and sector_relative > 0
    if leadership:
        support.append('同窗价格推进领先行业，行业未同步不否决早期机会')
    if sector_return is None:
        missing.append('板块同步证据未知')
    elif sector_return <= 0:
        counter.append('行业同窗尚未转强')
    elif leadership:
        support.append('行业与个股同窗向上，形成确认支持')
    catalyst = False
    event = (row.get('researchFactors') or {}).get('C') or {}
    event_at = _at(event.get('asOf'))
    if (event.get('status') == 'observed' and _source(event.get('source')) and event_at
            and 0 <= (now-event_at).total_seconds() <= 86400
            and (_number(event.get('score')) or 0) > 50
            and ((_number((event.get('features') or {}).get('positive_event_count')) or 0) > 0)):
        catalyst = True
        support.append('时点合法的已披露正面业绩事件提供催化支持')
    for event in context.get('catalysts') or []:
        if (_known_event(event, now) and event.get('direction') == 'positive'
                and (now-_at(event['availableAt'])).total_seconds() <= 86400):
            catalyst = True
            support.append('当时已可知的新催化：'+str(event.get('title') or event.get('type') or '已核验事件'))
    for event in (context.get('majorNegativeEvents') or [])+(context.get('regulatoryRisks') or []):
        if _known_event(event, now) and event.get('severity') in ('major', 'critical', '重大'):
            hard.append('已核验重大负面或监管限制：'+str(event.get('title') or event.get('type') or '重大事件'))
    if not catalyst:
        missing.append('独立新催化未核验；不把缺失当作利空')
    return active, flow, leadership, sector_return, catalyst


def _assess(candidate, now, minute, daily, context, previous):
    row = deepcopy(candidate)
    quote, price, hard, missing, buy = _quote_assessment(row, now)
    support, counter = [], []
    raw, trend, minute_at = _minute_assessment(row, minute, now, hard, missing)
    active, flow, leadership, sector_return, catalyst = _auxiliary(
        row, context or {}, now, minute_at, support, missing, counter, hard)
    disclosures = [deepcopy(event) for event in (context or {}).get('disclosures') or []
                   if _known_disclosure(event, str(row.get('code') or ''), now)]
    for event in disclosures:
        missing.append('已披露待验证线索（仅标题，不作正催化）：'+str(event.get('title') or event.get('category')))
    r1, r3, r5, r15 = (raw.get(f'r{n}_pct') for n in (1, 3, 5, 15))
    a1, a3 = raw.get('a1_pct_per_min'), raw.get('a3_pct_per_min')
    minute_price = _number((minute or {}).get('currentPrice'))
    if (minute_price and minute_at and _number(quote.get('high')) and _number(quote.get('low'))
            and not _number(quote['low']) <= minute_price <= _number(quote['high'])):
        hard.append('当日分钟价格与独立报价日内范围冲突')
    # The price at the end of the metric window is the breakout evidence.
    # A later quote alone cannot turn an old minute window into a breakout.
    high5, low5, vwap = (raw.get(key) for key in ('local_high_5m', 'local_low_5m', 'vwap'))
    minute_breakout = bool(minute_price and high5 and minute_price > high5)
    # A later executable-price observation can invalidate an old signal. It
    # cannot manufacture a new completed-minute breakout or recovery.
    breakout = bool(minute_breakout and price and price >= high5)
    reclaim = bool((raw.get('vwap_reclaim') == 1 or raw.get('vwap_retest_success') == 1)
                   and minute_price and vwap and minute_price >= vwap and price and price >= vwap)
    reversal = bool(r1 is not None and r1 > 0 and
                    ((r3 is not None and r3 <= 0) or (r15 is not None and r15 <= 0)))
    accelerating = bool((r1 is not None and r1 > 0 and a1 is not None and a1 > 0)
                        or (r3 is not None and r3 > 0 and a3 is not None and a3 > 0))
    rising = bool(r3 is not None and r5 is not None and r3 > 0 and r5 > 0)
    latest_falling = r1 is not None and r1 < 0
    # A shallow latest-minute pullback is counter-evidence, not a shared gate
    # that discards an otherwise observed 3/5/15-minute move in both channels.
    repair_confirmed = bool((r3 is not None and r3 > 0) or breakout or reclaim)
    transition = bool(minute_at and (reversal or accelerating or breakout) and repair_confirmed)
    if accelerating:
        support.append('短窗涨速高于自身较长窗口均速，已观察到加速')
    if reversal:
        support.append('短窗转正而此前窗口仍弱，已观察到反转线索')
    if (reversal or accelerating) and not repair_confirmed:
        missing.append('单分钟反弹尚未形成3分钟转正、完成突破或收复确认，不进入推荐')
    if breakout:
        support.append('已完成分钟价格越过此前5分钟局部高点')
    if minute_breakout and not breakout:
        counter.append('最新报价已跌回分钟突破位下方，原突破证据失效')
    if latest_falling:
        counter.append('最近完整1分钟价格回落，当前启动证据减弱')
    above_vwap = bool(price and vwap and price > vwap)
    if vwap is None:
        missing.append('VWAP位置未知')
    elif not above_vwap:
        counter.append('当前尚未站回VWAP；保留早期反转评估')
    else:
        support.append('当前价格处于已核验分钟VWAP上方')
    failed_breakout_unrepaired = raw.get('failed_breakout_risk') == 100 and not breakout
    if failed_breakout_unrepaired:
        counter.append('最近完整分钟存在突破失败，须核对是否已恢复')
    selling = bool((flow is not None and flow < 0) or (active is not None and active < 50))
    structural_failure = bool((price and low5 and price < low5) or raw.get('failed_breakout_risk') == 100)
    live_low_broken = bool(price and low5 and price < low5)
    if live_low_broken:
        counter.append('最新报价已跌破信号窗口局部低点，原机会结构失效')
    lower_high_unrepaired = bool(raw.get('lower_high_risk') == 100 and not (breakout or reclaim))
    if lower_high_unrepaired:
        counter.append('局部高点降低且尚无完成突破或收复，弱势结构未修复')
    if (r1 is not None and r1 < 0 and r3 is not None and r3 < 0 and structural_failure and selling):
        hard.append('完整分钟转弱、结构破坏与卖压同时出现，存在明确派发反证')
    amount3, amount15 = raw.get('amount_3m'), raw.get('amount_15m')
    volume_acceleration = bool(amount3 is not None and amount15 is not None and amount15 > 0
                               and amount3/3 > amount15/15)
    if volume_acceleration:
        support.append('当日短窗成交速度超过自身较长窗口，不依赖20日基准')
    # Acceleration and breakout are price transformations, not independent
    # buying evidence. Nor can cumulative buying or unsigned turnover cancel
    # an observed negative flow in the actual decision window.
    initiative = bool((flow is not None and flow > 0)
                      or (active is not None and active > 50 and (flow is None or flow >= 0))
                      or (transition and volume_acceleration and not selling))
    corroborated = initiative or catalyst or leadership
    efficiency = trend.get('signed_log_efficiency_15m')
    sustained = bool(rising and
        ((r15 is not None and r15 > 0 and efficiency is not None and efficiency > 0)
         or raw.get('vwap_retest_success') == 1
         or (previous and price and price > (_number(previous.get('firstDetectionPrice')) or price))))
    confirmed = bool(sustained and corroborated and
                     (above_vwap or leadership or (flow is not None and flow > 0)))
    channel = 'M' if confirmed else 'E'
    if confirmed:
        support.append('跨窗口延续或回踩再起已有当时证据')
    structure_held = not (live_low_broken or failed_breakout_unrepaired or lower_high_unrepaired
                          or (minute_breakout and not breakout))
    qualified = bool((confirmed or transition) and corroborated and structure_held)
    if not (confirmed or transition):
        missing.append('尚未观察到独立状态跃迁或延续证据，日涨幅和热度不替代分钟变化')
    if not corroborated:
        missing.append('主动性、新催化或相对板块地位尚无独立支持')
    daily_metrics = (daily or {}).get('metrics') or {}
    daily_prev = _number(daily_metrics.get('last_close'))
    aligned = bool(daily_prev and _number(quote.get('pre_close')) and abs(daily_prev-_number(quote['pre_close'])) <= .011)
    resistance = min((value for value in (_number(daily_metrics.get('high20')),
                                          _number(daily_metrics.get('high60')))
                      if aligned and price and value and value > price), default=None)
    remaining = (resistance/price-1)*100 if resistance and price else None
    if remaining is None:
        missing.append('T至T+5剩余空间未知；新高不因缺少历史压力而被否决，不编造目标价')
    invalidation = ['新鲜报价或正常卖盘失效时停止沿用本次机会',
                    '连续转弱并伴随结构破坏、明确卖压或重大负面时重新评估']
    if low5 and price and low5 < price:
        invalidation.append(f'失效参考为已观察分钟低点{low5:.2f}元；T+1不保证可按此价退出')
    grade = 'C' if hard else channel+'-A' if qualified and buy['status'] == 'observed' else 'B'
    first = previous.get('firstDetectionAt') if previous else now.isoformat()
    first_price = previous.get('firstDetectionPrice') if previous else price
    change_rate = max((value for value in (a1, a3) if value is not None), default=None)
    # Falling comparison windows must not inflate a rebound's rank: e.g.
    # +0.1% after a -10% five-minute move is not +2.1%/minute new strength.
    directional_rates = []
    for short_return, long_return, short_minutes, long_minutes in ((r1, r5, 1, 5), (r3, r15, 3, 15)):
        if short_return is not None and long_return is not None:
            directional_rates.append(short_return/short_minutes-max(long_return/long_minutes, 0.))
    directional_rate = max(directional_rates, default=None)
    pullback_adjustment = min(r1, 0.) if r1 is not None else 0.
    ranking_change = directional_rate+pullback_adjustment if directional_rate is not None else None
    rank_tuple = (float(transition), ranking_change if ranking_change is not None else -math.inf,
                  efficiency if efficiency is not None else -math.inf,
                  float(initiative)+float(leadership)+float(catalyst),
                  remaining if remaining is not None else -math.inf,
                  -(buy['spreadPct'] if buy['spreadPct'] is not None else math.inf))
    opportunity = {'version': VERSION, 'channel': channel, 'grade': grade, 'rankScore': None,
        'rankScoreMeaning': policy_contract()['rankScoreMeaning'],
        'support': list(dict.fromkeys(support)), 'supports': list(dict.fromkeys(support)),
        'missing': list(dict.fromkeys(missing)), 'counterEvidence': list(dict.fromkeys(counter+hard)),
        'hardVetoes': hard, 'reasons': hard or support or ['仅保留观察，证据尚不支持机会入选'],
        'invalidation': invalidation, 'firstDetectionAt': first, 'firstDetectionPrice': first_price,
        'detectedAt': now.isoformat(), 'triggerAt': None,
        'sourceEvidence': {'quoteSource': quote.get('source'),
            'quoteSourceTime': quote.get('provider_timestamp') or quote.get('source_time'),
            'minuteSource': (minute or {}).get('source'), 'minuteAsOf': minute_at.isoformat() if minute_at else None,
            'bookSourceTime': buy['sourceTime']},
        'metrics': {**raw, 'pathEfficiency15m': efficiency, 'accelerationPerMinute': change_rate,
                    'directionalAccelerationPerMinute': directional_rate,
                    'repairConfirmed': repair_confirmed, 'structureHeldAtDecision': structure_held,
                    'independentInitiative': initiative,
                    'recentPullbackRankAdjustment': pullback_adjustment,
                    'rankingChangeRate': ranking_change,
                    'activeBuySharePct': active, 'fundFlow3m': flow},
        'buyWindow': buy, 'spaceStatus': 'observed' if remaining is not None else 'unknown',
        'remainingSpacePct': remaining, 'researchOnly': True, 'probability': None,
        'disclosureEvidence': disclosures,
        'validationStatus': 'prospective_rules_unvalidated', 'triggerMeaning': '未重建首次触发；分钟源时仅为观察截止'}
    row.update(opportunity=opportunity, ruleRecommendation=False, formalEligible=False,
               trainingEligible=False, training_eligible=False, executable=False)
    return row, rank_tuple


def build_opportunity_channels(candidates, now, *, top_n=5, minute_packets=None,
                               daily_packets=None, contexts=None, previous=None):
    now = _at(now)
    if now is None:
        raise ValueError('opportunity decision time requires an explicit timezone')
    prior = {}
    for item in _previous_rows(previous):
        record = item.get('opportunity') or {}
        first, detected = _at(record.get('firstDetectionAt')), _at(record.get('detectedAt'))
        if (record.get('version') == VERSION and first and detected and first.date() == now.date()
                and first <= detected < now and record.get('grade') != 'C'):
            code = str(item.get('code') or '')
            if code not in prior or first < _at(prior[code]['firstDetectionAt']):
                prior[code] = record
    assessed, seen = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        code = str(candidate.get('code') or '')
        if not code or code in seen:
            continue
        seen.add(code)
        assessed.append(_assess(candidate, now, (minute_packets or {}).get(code),
            (daily_packets or {}).get(code), (contexts or {}).get(code), prior.get(code)))
    eligible = sorted((item for item in assessed if item[0]['opportunity']['grade'] in ('E-A', 'M-A')),
                      key=lambda item: tuple(-value for value in item[1])+(str(item[0]['code']),))
    for index, (row, _) in enumerate(eligible):
        row['opportunity']['rankScore'] = round(100*(len(eligible)-index)/len(eligible), 4)
        row['opportunity']['rank'] = index+1
        row['opportunity']['rankingPeerCount'] = len(eligible)
    limit = max(0, min(5, int(top_n)))
    recommendations = [row for row, _ in eligible[:limit]]
    for row in recommendations:
        row['ruleRecommendation'] = True
    research = [row for row, _ in assessed]
    observations = [row for row in research if not row['ruleRecommendation']]
    reasons = Counter(reason for row in research for reason in row['opportunity']['hardVetoes'])
    return {'version': VERSION, 'asOf': now.isoformat(), 'policy': policy_contract(),
        'recommendations': recommendations, 'observations': observations,
        'researchCandidates': research,
        'diagnostics': {'researchedCount': len(research), 'qualifiedCount': len(eligible),
            'recommendationCount': len(recommendations), 'observationCount': len(observations),
            'gradeCounts': dict(Counter(row['opportunity']['grade'] for row in research)),
            'hardVetoCounts': dict(reasons), 'missingConfirmationDoesNotEqualAdverseEvidence': True}}
