"""Completed daily-bar conditions, independent of minute models and their ledger.

The lane uses downloaded historical daily bars immediately. It never treats a
daily rule as a calibrated probability, a morning T0 model, or a verified fill.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, time
import re

from .daily_opportunities import finite, is_market_open, quote_check
from .premarket import _at, _daily_valid, _local, previous_session
from .precision_policy import execution_quote_data, POLICIES
from .research_policy import DEFAULT_PARAMETERS

VERSION = 'king-daily-primary-v3'
TASK_ID = 'daily_structure_to_next_session'
RANK_POLICY = 'equal_percentile_open_return_atr_and_directional_efficiency_then_amount_then_code'
INTRADAY_DIRECTION_RULE = 'price > pre_close and price > open_price and price > amount / volume'
INTRADAY_DIRECTION_MEANING = ('同一新鲜报价的价格方向约束，只排除已知逆向走势；规则尚未验证，'
    '单次量价快照不能确认持续主力资金、资金身份或主升，也不表示上涨概率')


def daily_policy_contract():
    return {'version': VERSION, 'taskId': TASK_ID, 'rankingPolicy': RANK_POLICY,
        'validationStatus': 'unvalidated_rules', 'probability': None,
        'attackWatchlist': 'shadow_only_not_selected_not_training_eligible',
        'directionalEfficiency': '(price-open_price)/(high-low)',
        'openReturnAtr': '(price-open_price)/completed_daily_atr14',
        'rankWeights': {'directionalEfficiencyPercentile': .5, 'openReturnAtrPercentile': .5},
        'percentileDefinition': '(count_less + 0.5*count_equal)/eligible_peer_count',
        'meaning': '方向效率与ATR标准化涨幅各占一半经验名次；等权为待验证基线，金额仅同分排序'}


def _rank_evidence(quote, atr):
    span = quote['high'] - quote['low']
    return {'policy': RANK_POLICY, 'validationStatus': 'unvalidated_rules', 'probability': None,
        'directionalEfficiency': (quote['price'] - quote['open_price']) / span if span > 0 else None,
        'openReturnAtr': (quote['price'] - quote['open_price']) / atr if atr > 0 else None,
        'amountTieBreakCny': quote['amount'],
        'meaning': '单次快照方向与波动标准化排序；未证明分钟加速、持续买盘或后续收益'}


def _rank_key(row):
    evidence = row['dailyRankEvidence']
    return (-evidence['relativeRankScore'], -evidence['amountTieBreakCny'], row['code'])


def _rank_candidates(rows):
    """Frozen cross-sectional ranks in this lane, not a probability or time series.

    Two independent questions: how much progress relative to normal volatility,
    and how directional that progress was relative to today's range. Neither a
    one-tick move at the high nor large unsigned turnover wins on its own.
    """
    result = []
    for row in rows:
        evidence = dict(row['dailyRankEvidence'])
        for key in ('directionalEfficiency', 'openReturnAtr'):
            peers = [x['dailyRankEvidence'][key] for x in rows if x['dailyRankEvidence'][key] is not None]
            value = evidence[key]
            evidence[key + 'Percentile'] = (sum(x < value for x in peers) + .5 * sum(x == value for x in peers)) / len(peers) if peers and value is not None else None
        evidence['referencePeerCount'] = len(rows)
        evidence['relativeRankScore'] = sum(.5 * (evidence[key + 'Percentile'] or 0.)
                                          for key in ('directionalEfficiency', 'openReturnAtr'))
        result.append({**row, 'dailyRankEvidence': evidence})
    return sorted(result, key=_rank_key)


def _number(value):
    return None if isinstance(value, bool) else finite(value)


def _intraday_direction(quote):
    """Observed snapshot metrics, not signed money flow or persistent-fund proof.

    Callers must first verify quote source, freshness, code and price limits.
    Amount is CNY and volume is shares, as in the existing VWAP sanity gate.
    """
    price, prev, opened, high, low, amount, volume = (
        _number(quote.get(key)) for key in
        ('price', 'pre_close', 'open_price', 'high', 'low', 'amount', 'volume'))
    valid = (all(value is not None and value > 0 for value in
                 (price, prev, opened, high, low, amount, volume))
             and low <= price <= high and low <= opened <= high)
    vwap = amount / volume if valid else None
    valid = bool(valid and low - .01 <= vwap <= high + .01)
    metrics = {'returnFromPreviousClosePct': None, 'returnFromOpenPct': None,
               'sessionVwap': None, 'vwapGapPct': None,
               'pullbackFromSessionHighPct': None, 'sessionRangeLocation': None}
    reasons = []
    if valid:
        metrics.update(returnFromPreviousClosePct=(price / prev - 1) * 100,
            returnFromOpenPct=(price / opened - 1) * 100, sessionVwap=vwap,
            vwapGapPct=(price / vwap - 1) * 100,
            pullbackFromSessionHighPct=(high - price) / high * 100,
            sessionRangeLocation=(price - low) / (high - low) if high > low else None)
        reasons = [reason for reference, reason in (
            (prev, 'current_price_not_above_previous_close'),
            (opened, 'current_price_not_above_open'),
            (vwap, 'current_price_not_above_session_vwap')) if price <= reference]
    else:
        reasons = ['intraday_direction_inputs_unverified']
    return {'version': VERSION, 'status': 'observed' if valid else 'unavailable',
        'passed': not reasons if valid else None, 'validationStatus': 'unvalidated_rules',
        'probability': None, 'rule': INTRADAY_DIRECTION_RULE, 'reasons': reasons,
        'source': quote.get('source'),
        'sourceTime': quote.get('provider_timestamp') or quote.get('source_time'),
        'metrics': metrics,
        'formulas': {'returnFromPreviousClosePct': '100 * (price / pre_close - 1)',
            'returnFromOpenPct': '100 * (price / open_price - 1)',
            'sessionVwap': 'amount_CNY / volume_shares',
            'vwapGapPct': '100 * (price / sessionVwap - 1)',
            'pullbackFromSessionHighPct': '100 * (high - price) / high',
            'sessionRangeLocation': '(price - low) / (high - low); null when high == low'},
        'meaning': INTRADAY_DIRECTION_MEANING}


def _known(record, now):
    stamps = [_at(record.get(key)) for key in ('observedAt', 'availableAt', 'firstSeenAt')]
    return bool(record.get('source') and all(stamp is not None and stamp <= now for stamp in stamps)
                and stamps[2] <= stamps[0] and stamps[2] <= stamps[1])


def _events(context, now):
    """Coverage, disclosure availability and negative risks are separate facts."""
    missing, adverse = [], []
    coverage = context.get('coverage') or {}
    if any(coverage.get(key) is not True for key in ('financials', 'forecast', 'unlock')):
        missing.append('financial_forecast_or_unlock_coverage_missing')
    context_at = _at(context.get('decisionAt'))
    if context_at is None or not 0 <= (now-context_at).total_seconds() <= 21600:
        missing.append('financial_context_time_missing_future_or_stale')
    financials = context.get('financials') or {}
    values = {key: _number(financials.get(key)) for key in ('profit', 'eps', 'cashPerShare')}
    if not _known(financials, now) or any(value is None for value in values.values()):
        missing.append('financial_report_or_availability_unverified')
    elif values['profit'] <= 0 or values['eps'] <= 0:
        adverse.append('known_loss_or_nonpositive_earnings')
    for event in context.get('forecasts') or []:
        if not _known(event, now) or not event.get('type') or not event.get('metric'):
            missing.append('forecast_disclosure_unverified')
        elif event['type'] in DEFAULT_PARAMETERS['entry']['negativeForecastTypes'] and '净利润' in event['metric']:
            adverse.append('known_negative_earnings_forecast')
    unlock = 0.
    for event in context.get('unlocks') or []:
        if not _known(event, now):
            missing.append('unlock_disclosure_unverified')
            continue
        try:
            days = (date.fromisoformat(str(event.get('eventDate'))) - now.date()).days
        except ValueError:
            missing.append('unlock_date_unverified')
            continue
        ratio = _number(event.get('floatRatioPct'))
        if ratio is None or ratio < 0:
            missing.append('unlock_ratio_unverified')
        elif 0 <= days <= 14:
            unlock += ratio
    return {'missingReasons': sorted(set(missing)), 'riskReasons': sorted(set(adverse)),
            'unlock14dPct': unlock, 'positiveOperatingCashFlow': bool(values['cashPerShare'] is not None
                                                                   and values['cashPerShare'] > 0),
            'coverage': dict(coverage)}


def _market(evidence, now):
    packet = evidence or {}
    values = packet.get('features') or {}
    stamp = _at(packet.get('asOf'))
    coverage, universe, breadth = (_number(values.get(key)) for key in
                                  ('source_coverage', 'universe_count', 'advancing_pct'))
    available = bool(packet.get('source') and stamp and 0 <= (now-stamp).total_seconds() <= 300
                     and coverage is not None and .8 <= coverage <= 1
                     and universe is not None and universe >= 300
                     and breadth is not None and 0 <= breadth <= 100)
    blocked = bool(available and (breadth < 50 or packet.get('regime') == 'weak'))
    return {'status': 'weak_market_veto' if blocked else 'observed_non_weak' if available else 'unavailable',
            'available': available, 'passed': not blocked if available else None,
            'advancingPct': breadth if available else None,
            'source': packet.get('source') if available else None,
            'asOf': packet.get('asOf') if available else None,
            'meaning': '有可靠负面广度时否决；缺失时不宣称市场环境已通过'}


def _limit_evidence_current(quote, now):
    """An auxiliary provider's limit timestamp cannot borrow the newer quote time."""
    evidence = quote.get('price_limit_evidence')
    if evidence is None:
        return True  # Direct provider fields are checked by execution_quote_data.
    stamp = _at(evidence.get('source_time'))
    if (evidence.get('status') != 'observed' or evidence.get('method') != 'explicit_provider_fields'
            or not evidence.get('source') or stamp is None or stamp.date() != now.date()
            or not 0 <= (now-stamp).total_seconds() <= 30):
        return False
    for field in ('limit_up', 'limit_down'):
        value = _number(evidence.get(field))
        if value is None or quote.get(field) is None or abs(value-quote[field]) > .000001:
            return False
    reference = _number(evidence.get('previous_close'))
    return bool(reference and quote.get('pre_close') and abs(reference-quote['pre_close']) <= .000001)


def build_daily_candidates(queued, daily_by_code, quotes_by_code, decision_at, *, top_n=5,
                           contexts=None, market_evidence=None):
    """Return independent daily candidates/profiles plus an inspectable gate summary.

    ``daily_by_code`` contains packets produced by ``daily_evidence`` from real
    completed, unadjusted daily history. ``contexts`` contains ``for_stock``
    disclosure packets. Minute lineage, minute factors and morning predictions
    are deliberately never read. The caller must keep this task out of the
    minute/morning training ledger and recheck quote freshness at publication.
    """
    now = _local(decision_at)
    market = _market(market_evidence, now)
    assessment = {'version': VERSION, 'taskId': TASK_ID, 'label': '日线条件推荐',
        'asOf': now.isoformat(), 'status': 'completed', 'validationStatus': 'unvalidated_rules',
        'probability': None, 'horizon': 'next_session', 'minuteDataRequired': False,
        'morningModelRequired': False, 'minuteSectorFundRequired': False,
        'marketBreadth': market, 'dailyUsableCount': 0, 'quoteUsableCount': 0,
        'intradayDirectionPassedCount': 0, 'policyContract': daily_policy_contract(),
        'intradayDirectionPolicy': {'version': VERSION, 'rule': INTRADAY_DIRECTION_RULE,
                                   'meaning': INTRADAY_DIRECTION_MEANING},
        'eventCoveragePassedCount': 0, 'researchedCount': 0, 'excludedCounts': {},
        'profileExcludedCounts': {}, 'profileQualifiedCounts': {}, 'count': 0,
        'meaning': '真实已完成日K、当日报价及已披露风险的独立条件筛选；不表示分钟主力确认、上午T0模型通过或上涨概率'}
    result = {'dailyCandidates': [], 'dailyProfileCandidates': {key: [] for key in POLICIES},
              'dailyAttackWatchlist': [], 'dailyRejectionLedger': [],
              'marketEmotionPanel': {**dict(market_evidence or {}),
                  'evaluatedAt': now.isoformat(), 'decisionAssessment': market},
              'dailyAssessment': assessment}
    clock = now.time().replace(tzinfo=None)
    if (not is_market_open('cn', now.date()) or not
            (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock < time(14, 57))):
        assessment.update(status='outside_continuous_trading_window')
        return result
    previous = previous_session(now).isoformat()
    assessment['baselineAsOf'] = previous
    excluded, profiles, profile_excluded, seen = Counter(), {key: [] for key in POLICIES}, {key: Counter() for key in POLICIES}, set()
    attacks, ledger = [], {}
    for item in queued:
        code, name = str(item.get('code') or ''), str(item.get('name') or item.get('code') or '')
        if code in seen:
            continue
        seen.add(code)
        record = {'code': code, 'name': name, 'decisionAt': now.isoformat(),
                  'state': 'excluded', 'reasons': [], 'profileDecisions': {}}
        ledger[code] = record
        def reject(*reasons):
            record['reasons'].extend(reasons)
            excluded.update(reasons)
        if not re.fullmatch(r'(600|601|603|605|000|001|002|003)\d{3}', code) or re.search(r'ST|退|整理|停牌', name, re.I):
            reject('unsupported_or_risk_board')
            continue
        daily = daily_by_code.get(code) or {}
        prior = daily.get('previousSession') or {}
        if (not _daily_valid(daily, now, previous) or prior.get('priceBasis') != 'unadjusted'
                or prior.get('asOf') != previous or prior.get('source') != daily.get('source')
                or 'unknown' in str(daily.get('source')).lower()):
            reject('daily_history_source_basis_or_latest_session_unverified')
            continue
        d = {key: _number(value) for key, value in daily['metrics'].items()}
        required = ('last_close', 'ma5', 'ma10', 'ma20', 'high20', 'low20', 'atr14', 'history_sessions')
        prior_close = _number(prior.get('close'))
        if (any(d.get(key) is None or d[key] <= 0 for key in required) or d['history_sessions'] < 20
                or prior_close is None or abs(prior_close-d['last_close']) > .000001):
            reject('daily_history_prices_invalid')
            continue
        assessment['dailyUsableCount'] += 1
        quote = dict(quotes_by_code.get(code) or {})
        for key in ('price', 'pre_close', 'high', 'low', 'open_price', 'change_pct',
                    'amount', 'volume', 'limit_up', 'limit_down'):
            quote[key] = _number(quote.get(key))
        execution = execution_quote_data({'code': code, 'quote': quote})
        if (quote_check(quote, code, now) or execution['status'] != 'complete'
                or not _limit_evidence_current(quote, now)):
            reject('quote_or_provider_price_limits_unverified')
            continue
        price, prev, amount, volume = (_number(quote.get(key)) for key in ('price', 'pre_close', 'amount', 'volume'))
        if abs(prev-d['last_close']) > DEFAULT_PARAMETERS['entry']['previousCloseToleranceCny']:
            reject('daily_quote_previous_close_mismatch')
            continue
        if (any(quote.get(key) is True for key in ('suspended', 'locked_limit_up', 'corporate_action', 'no_price_limit'))
                or quote.get('quote_usable_for_current_price_check') is False
                or amount is None or volume is None or amount <= 0 or volume <= 0
                or not quote['low']-.01 <= amount/volume <= quote['high']+.01
                or not quote['limit_down']+.005 < price < quote['limit_up']-.005):
            reject('not_tradable_or_limit_locked')
            continue
        assessment['quoteUsableCount'] += 1
        record.update(quoteSource=quote.get('source'), quoteSourceTime=quote.get('provider_timestamp') or quote.get('source_time'),
                      referencePrice=price)
        direction = _intraday_direction(quote)
        if direction['passed'] is not True:
            reject(*direction['reasons'])
            continue
        assessment['intradayDirectionPassedCount'] += 1
        events = _events((contexts or {}).get(code) or {}, now)
        if events['missingReasons'] or events['riskReasons']:
            reject(*(events['missingReasons'] + events['riskReasons']))
            continue
        assessment['eventCoveragePassedCount'] += 1
        rank_evidence = _rank_evidence(quote, d['atr14'])
        # A new high has no observed overhead resistance. Preserve the opportunity
        # for prospective research without inventing a target or bypassing entry gates.
        attacks.append({'code': code, 'name': name, 'decision_at': now.isoformat(),
            'version': VERSION, 'status': 'research_only', 'stateLabel': '进攻观察（未确认）',
            'taskId': 'unsealed_intraday_attack_shadow', 'sourceTime': direction['sourceTime'],
            'quote': quote, 'dailyRankEvidence': rank_evidence, 'intradayDirectionEvidence': direction,
            'breakout20': price > d['high20'],
            'breakout60': price > d['high60'] if d.get('high60') else None,
            'marketBreadth': market, 'eventAssessment': events,
            'entryEligible': False, 'executable': False, 'trainingEligible': False,
            'probability': None, 'validationStatus': 'unvalidated_rules',
            'resistance': None, 'structureRewardRisk': None,
            'missingConfirmation': ['completed_minute_path', 'aligned_sector_resonance',
                                    'persistent_trading_pressure', 'validated_T1_payoff'],
            'meaning': '未封板的方向性观察，不证明随后涨停；须补齐分钟、板块、资金及T+1证据，不能当成正式推荐'})
        if market['status'] == 'weak_market_veto':
            reject('verified_weak_market')
            continue
        if not d['ma5'] > d['ma10'] > d['ma20'] or price < d['ma20']:
            reject('daily_uptrend_not_confirmed')
            continue
        support = max((d[key] for key in ('ma20', 'low20') if 0 < d[key] < price), default=None)
        resistance = min((d[key] for key in ('high20', 'high60') if d.get(key) and d[key] > price), default=None)
        if support is None or resistance is None:
            reject('daily_support_or_upside_space_unknown')
            continue
        risk_distance = max(price-support, d['atr14'] * DEFAULT_PARAMETERS['entry']['volatilityRiskFloorAtr'])
        rr, bias, extension = (resistance-price)/risk_distance, (price/d['ma5']-1)*100, (price-d['ma5'])/d['atr14']
        indicator_keys = ('last_close', 'ma5', 'ma10', 'ma20', 'high20', 'high60', 'low20', 'atr14')
        indicators = [{'key': key, 'label': key, 'value': d.get(key), 'unit': '元', 'source': daily['source'],
                       'asOf': previous, 'status': 'observed' if d.get(key) is not None else 'missing'} for key in indicator_keys]
        gaps = ['未依赖分钟板块、分钟资金或上午T0模型；其状态不构成本分支确认']
        if not market['available']:
            gaps.append('全市场广度未取得或未满足时效覆盖要求，市场环境保持未知')
        reasons = [f"{int(d['history_sessions'])}个已完成日K截至{previous}，报价前收盘与日线末收盘一致",
                   '现价严格高于前收盘、当日开盘及成交额/成交量计算的当日VWAP',
                   'MA5>MA10>MA20，现价维持MA20上方',
                   f'压力空间/支撑及ATR风险距离为{rr:.2f}倍；MA5乖离{bias:.2f}%',
                   '财报、业绩预告及未来14日解禁覆盖已核验']
        risk_notes = ['日线条件规则尚未经过概率校准；排序不代表上涨概率',
                      INTRADAY_DIRECTION_MEANING,
                      '按A股T+1约束评估下一交易日；支撑和压力不是保证成交的止损价或收益目标']
        candidate = {'code': code, 'name': name, 'status': 'daily_conditional', 'stateLabel': '日线条件推荐',
            'strategyBranch': '日线趋势与T+1价格纪律', 'strategyId': TASK_ID,
            'version': VERSION, 'decision_at': now.isoformat(), 'quote': quote,
            'price': price, 'signalPrice': price, 'referencePrice': price,
            'source': quote.get('source'), 'sourceTime': quote.get('provider_timestamp') or quote.get('source_time'),
            'dailyLastClose': d['last_close'], 'dailyPreviousSession': dict(prior),
            'baselineAsOf': previous, 'baselineSource': daily['source'], 'dailyEvidence': daily,
            'dailyEvidenceStatus': 'completed_daily_source_and_quote_preclose_verified',
            'intradayDirectionEvidence': direction,
            'dailyRankEvidence': rank_evidence,
            'support': support, 'resistance': resistance, 'atr14': d['atr14'], 'structureRewardRisk': rr,
            'biasMa5Pct': bias, 'atrExtension': extension, 'riskDistance': risk_distance,
            'indicatorEvidence': indicators, 'selectionReasons': reasons, 'reasons': reasons,
            'risks': risk_notes, 'riskFlags': risk_notes, 'dataGaps': gaps, 'eventAssessment': events,
            'marketBreadth': market, 'entryEligible': True, 'executable': False,
            'validationStatus': 'unvalidated_rules', 'probability': None, 'probabilities': {}, 'confidence': None,
            'horizon': '下一交易日（T+1）', 'horizonId': 'next_session',
            'minuteModelTrainingEligible': False, 'trainingEligible': False, 'training_eligible': False,
            'rankMeaning': '方向效率与ATR标准化开盘后涨幅的截面名次各占一半，成交金额只作同分项；等权为待验证基线，不是上涨概率',
            'dailySignal': {'version': VERSION, 'taskId': TASK_ID, 'horizon': 'next_session',
                'validationStatus': 'unvalidated_rules', 'probability': None, 'minuteDataRequired': False,
                'morningModelRequired': False, 'trainingEligibleAtSignal': False},
            'executionQuoteData': execution}
        decisions = {}
        for key, rule in POLICIES.items():
            blocked = []
            if bias > rule.max_bias_pct or extension > rule.max_atr_extension:
                blocked.append('daily_ma5_extension_too_large')
            if rr < rule.min_reward_risk:
                blocked.append('daily_upside_reward_risk_insufficient')
            if events['unlock14dPct'] >= rule.max_unlock_pct:
                blocked.append('known_large_unlock')
            if rule.require_financial_quality and not events['positiveOperatingCashFlow']:
                blocked.append('known_nonpositive_operating_cash_flow')
            decisions[key] = {'label': rule.label, 'entryEligible': not blocked,
                              'state': 'avoid' if blocked else 'conditional', 'reasons': blocked}
            profile_excluded[key].update(blocked)
        candidate['dailyProfileDecision'] = decisions
        record['profileDecisions'] = decisions
        record['state'] = 'qualified_below_top_n' if decisions['regular']['entryEligible'] else 'profile_rejected'
        record['reasons'] = list(decisions['regular']['reasons'])
        for key in POLICIES:
            if decisions[key]['entryEligible']:
                profiles[key].append(candidate)
    limit = max(0, min(5, int(top_n)))
    for key, candidates in profiles.items():
        candidates = _rank_candidates(candidates)
        for rank, row in enumerate(candidates, 1):
            decision = ledger[row['code']]['profileDecisions'][key]
            decision.update(rank=rank, selected=rank <= limit,
                            rankEvidence=dict(row['dailyRankEvidence']))
        result['dailyProfileCandidates'][key] = [{**row, 'rank': rank, 'riskProfile': key}
                                                for rank, row in enumerate(candidates[:limit], 1)]
    result['dailyCandidates'] = result['dailyProfileCandidates']['regular']
    for row in result['dailyCandidates']:
        ledger[row['code']]['state'] = 'selected_regular'
    assessment['attackObservedCount'] = len(attacks)
    attacks = _rank_candidates([row for row in attacks if ledger[row['code']]['state'] != 'selected_regular'])
    result['dailyAttackWatchlist'] = [{**row, 'rank': rank,
        'dailyPrimaryState': ledger[row['code']]['state'],
        'dailyPrimaryReasons': ledger[row['code']]['reasons']}
        for rank, row in enumerate(attacks[:limit], 1)]
    result['dailyRejectionLedger'] = list(ledger.values())
    assessment['attackAlternativeCount'] = len(attacks)
    assessment['attackWatchlistCount'] = len(result['dailyAttackWatchlist'])
    assessment['auditMeaning'] = '只覆盖实际深研队列；每股保留首个失败阶段，非全市场漏选率'
    assessment.update(researchedCount=len(seen), excludedCounts=dict(excluded),
        profileExcludedCounts={key: dict(value) for key, value in profile_excluded.items()},
        profileQualifiedCounts={key: len(value) for key, value in profiles.items()},
        count=len(result['dailyCandidates']))
    if not result['dailyCandidates']:
        assessment['status'] = 'no_daily_candidates'
    assessment['message'] = (f"日线条件推荐{assessment['count']}只；可用日K {assessment['dailyUsableCount']}/{len(seen)}只，"
                             f"日线与新鲜报价核验{assessment['quoteUsableCount']}只，事件风险覆盖通过{assessment['eventCoveragePassedCount']}只。")
    return result
