"""Current flow/trend research entry point; no execution or training authority.

No legacy fallback. Missing mandatory stock/sector flow or a complete thirty
minute path remains observation-only. Short price speed never grants rank.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

from .flow_opportunity_policy import VERSION, PARAMETERS, policy_contract, assess_flow_policy, rank_qualified
from .fund_flow_evidence import compute_stock_flow
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
    if (_number(packet.get('historyDays')) or 0) < 20:
        missing.append('20日同刻基准未齐；使用当日真实变化，历史异常程度未知')
    if (packet.get('dataLineage') or row.get('dataLineage') or {}).get('status') != 'verified':
        missing.append('分钟来源尚未完成独立交叉核验；保持规则研究属性')
    trend = packet.get('trendEvidence') or {}
    trend_metrics = (trend.get('metrics') or {}) if _at(trend.get('asOf')) == stamp else {}
    return raw, {key: _number(value) for key, value in trend_metrics.items()}, stamp


def _risk_context(context, now, code, hard, missing):
    # These clock/source/identity guards remain independent of the flow policy.
    for event in (context.get('majorNegativeEvents') or [])+(context.get('regulatoryRisks') or []):
        if (_known_event(event, now) and event.get('severity') in ('major', 'critical', '重大')
                and (event.get('code') is None or str(event['code']) == code)):
            hard.append('已核验重大负面或监管限制：'+str(event.get('title') or event.get('type') or '重大事件'))
    disclosures = [deepcopy(event) for event in context.get('disclosures') or []
                   if _known_disclosure(event, code, now)]
    for event in disclosures:
        missing.append('已披露待验证线索（仅标题，不替代资金证据）：'+str(event.get('title') or event.get('category')))
    return disclosures


def _assess(candidate, now, minute, daily, context, previous, sector_flow):
    row = deepcopy(candidate)
    quote, price, hard, missing, buy = _quote_assessment(row, now)
    raw, trend, minute_at = _minute_assessment(row, minute, now, hard, missing)
    disclosures = _risk_context(context or {}, now, str(row.get('code') or ''), hard, missing)
    minute_price = _number((minute or {}).get('currentPrice'))
    if (minute_price and minute_at and _number(quote.get('high')) and _number(quote.get('low'))
            and not _number(quote['low']) <= minute_price <= _number(quote['high'])):
        hard.append('当日分钟价格与独立报价日内范围冲突')
    stock_flow = compute_stock_flow(row.get('fundEvidence') or {}, minute or {}, minute_at,
                                   horizon=PARAMETERS['windowMinutes'])
    sector_flow = sector_flow or {}
    assessment = assess_flow_policy(row, minute, daily, quote, buy, trend, minute_at, now,
                                    stock_flow, sector_flow)
    missing.extend(assessment['missing'])
    support, counter = assessment['support'], assessment['counterEvidence']
    grade = 'C' if hard else 'F-A' if assessment['qualified'] else 'B'
    selection_mode = assessment['selectionMode'] if grade == 'F-A' else 'observation'
    first = previous.get('firstDetectionAt') if previous else now.isoformat()
    first_price = previous.get('firstDetectionPrice') if previous else price
    structural_low = assessment['metrics']['structuralLow30m']
    invalidation = ['报价、盘口或必需资金/分钟证据失效时停止沿用本次机会',
                    '30分钟结构破坏或已核验重大负面时重新评估；T+1不保证按失效参考价退出']
    if structural_low is not None:
        invalidation.append(f'完整30分钟已观察低点{structural_low:.2f}元为结构参考，不是保证成交的止损价')
    metrics = {**raw, **assessment['metrics'],
               'stockNetFlowRatio30m': (stock_flow.get('metrics') or {}).get('netFlowRatio'),
               'sectorNetFlowRatio30m': (sector_flow.get('metrics') or {}).get('netFlowRatio')}
    opportunity = {
        'version': VERSION, 'channel': 'F', 'grade': grade, 'selectionMode': selection_mode,
        'rankScore': None, 'rankingComposite': None, 'rankingComponents': {},
        'rankingValues': assessment['rankingValues'], 'rankScoreMeaning': policy_contract()['rankScoreMeaning'],
        'sectorKey': assessment['sectorKey'], 'sector': assessment['sector'],
        'support': support, 'supports': support, 'missing': list(dict.fromkeys(missing)),
        'counterEvidence': list(dict.fromkeys(counter+hard)), 'hardVetoes': hard,
        'failedConditions': assessment['failedConditions'],
        'reasons': hard or assessment['failedConditions'] or support,
        'invalidation': invalidation, 'firstDetectionAt': first, 'firstDetectionPrice': first_price,
        'detectedAt': now.isoformat(), 'triggerAt': None,
        'sourceEvidence': {'quoteSource': quote.get('source'),
            'quoteSourceTime': quote.get('provider_timestamp') or quote.get('source_time'),
            'minuteSource': (minute or {}).get('source'), 'minuteAsOf': minute_at.isoformat() if minute_at else None,
            'bookSourceTime': buy['sourceTime']},
        'flowEvidence': {'stock': stock_flow, 'sector': deepcopy(sector_flow)},
        'metrics': metrics, 'buyWindow': buy, 'spaceStatus': 'unknown', 'remainingSpacePct': None,
        'researchOnly': True, 'probability': None, 'disclosureEvidence': disclosures,
        'validationStatus': 'prospective_rules_unvalidated',
        'triggerMeaning': '固定窗口决策记录；不是已成交或事后重建首次触发',
    }
    row.update(opportunity=opportunity, ruleRecommendation=False, formalEligible=False,
               trainingEligible=False, training_eligible=False, executable=False)
    return row


def build_opportunity_channels(candidates, now, *, top_n=5, minute_packets=None,
                               daily_packets=None, contexts=None, previous=None, sector_flow_packets=None):
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
    research, seen = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        code = str(candidate.get('code') or '')
        if not code or code in seen:
            continue
        seen.add(code)
        research.append(_assess(candidate, now, (minute_packets or {}).get(code),
            (daily_packets or {}).get(code), (contexts or {}).get(code), prior.get(code),
            (sector_flow_packets or {}).get(code)))
    eligible = rank_qualified([row for row in research if row['opportunity']['grade'] == 'F-A'])
    limit = max(0, min(PARAMETERS['maximumRecommendations'], int(top_n)))
    recommendations, sectors = [], Counter()
    for index, row in enumerate(eligible):
        opportunity = row['opportunity']
        opportunity['rank'] = index + 1
        opportunity['rankingPeerCount'] = len(eligible)
        sector = opportunity['sectorKey']
        if len(recommendations) >= limit:
            opportunity['selectionExclusion'] = 'outside_maximum_recommendations'
        elif sectors[sector] >= PARAMETERS['maximumPerSector']:
            opportunity['selectionExclusion'] = 'sector_concentration_limit'
        else:
            row['ruleRecommendation'] = True
            recommendations.append(row)
            sectors[sector] += 1
    observations = [row for row in research if not row['ruleRecommendation']]
    reasons = Counter(reason for row in research for reason in row['opportunity']['hardVetoes'])
    failures = Counter(reason for row in research for reason in row['opportunity']['failedConditions'])
    return {'version': VERSION, 'asOf': now.isoformat(), 'policy': policy_contract(),
        'recommendations': recommendations, 'observations': observations, 'researchCandidates': research,
        'diagnostics': {'researchedCount': len(research), 'qualifiedCount': len(eligible),
            'recommendationCount': len(recommendations), 'observationCount': len(observations),
            'gradeCounts': dict(Counter(row['opportunity']['grade'] for row in research)),
            'hardVetoCounts': dict(reasons), 'failedConditionCounts': dict(failures),
            'missingConfirmationDoesNotEqualAdverseEvidence': True,
            'mandatoryMissingRemainsObservation': True, 'selectionVersion': VERSION}}
