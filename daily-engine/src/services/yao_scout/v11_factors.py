"""Auditable V1.2 rule scores. These values are rankings, not probabilities.

Under five same-clock sessions no score is emitted; 5--19 are observation only.
Missing factors never redistribute their weights. Missing risk uses its upper
bound. Calibrated MFE, MAE and state probabilities remain withheld.
"""
from __future__ import annotations

import math
from datetime import datetime

from .research_policy import DEFAULT_PARAMETERS, VALIDATION_STATUS

EARLY_WEIGHTS = DEFAULT_PARAMETERS['weights']['early']
MAIN_WEIGHTS = DEFAULT_PARAMETERS['weights']['main']
RISK_WEIGHTS = DEFAULT_PARAMETERS['weights']['distribution']
PARAMS = DEFAULT_PARAMETERS['score']
SCORE_VERSION = 'stockking-v1.2-rules'
RISK_PENALTY_COEFFICIENT = PARAMS['riskPenaltyCoefficient']


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clamp(value, low=PARAMS['minimum'], high=PARAMS['maximum']):
    return min(high, max(low, value))


def _z_score(value):
    value = _number(value)
    return _clamp(PARAMS['centre'] + PARAMS['zScale'] * value) if value is not None else None


def _weighted(values, weights, *, missing=0.):
    """Fixed denominator: removing an observed positive factor cannot improve it."""
    available = {key: _number(values.get(key)) for key in weights}
    available = {key: value for key, value in available.items() if value is not None}
    weight_sum = sum(weights.values())
    return ((sum(available.get(key, missing) * weight for key, weight in weights.items()) / weight_sum,
             {key: round(weights[key] / weight_sum, 6) for key in available})
            if weight_sum > 0 and available else (None, {}))


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value or '')
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _research_score(candidate, key, cutoff):
    packet = (candidate.get('researchFactors') or {}).get(key) or {}
    if not isinstance(packet, dict):
        return None
    stamp = _timestamp(packet.get('asOf'))
    value = _number(packet.get('score'))
    max_age = DEFAULT_PARAMETERS['freshness']['researchMaxAgeSeconds'][key]
    features = packet.get('features') or {}
    valid = (packet.get('status') == 'observed' and stamp is not None and cutoff is not None
             and 0 <= (cutoff - stamp).total_seconds() <= max_age
             and value is not None and PARAMS['minimum'] <= value <= PARAMS['maximum']
             and isinstance(features, dict) and all(_number(item) is not None for item in features.values()))
    return value if valid else None


def _stage(branch):
    if '盘前' in branch:
        return 'unconfirmed', None
    if '突破' in branch:
        stage = 'breakout'
    elif '修复' in branch:
        stage = 'repair'
    elif '延续' in branch:
        stage = 'continuation'
    else:
        return 'unconfirmed', None
    return stage, PARAMS['stageBranches'][stage]


def _evidence_values(candidate):
    return {row.get('key'): _number(row.get('value'))
            for row in candidate.get('indicatorEvidence', [])
            if isinstance(row, dict) and row.get('status') == 'observed'}


def score_v11(candidate, minute, *, now=None):
    """Add ranking evidence without relaxing eligibility or implying a fill."""
    minute = minute or {}
    raw = {key: _number(value) for key, value in (minute.get('v11Inputs') or {}).items()}
    days = max(0, int(raw.get('history_days') or 0))
    price_days = max(0, int(raw.get('price_history_days') or 0))
    minimum = DEFAULT_PARAMETERS['history']['minimumScoreDays']
    formal = DEFAULT_PARAMETERS['history']['formalDays']
    critical = DEFAULT_PARAMETERS['history']['criticalFactors']
    status = ('complete' if days >= formal and price_days >= formal else
              'low' if min(days, price_days) >= minimum else 'insufficient')
    cutoff = now if now is not None and now.tzinfo is not None else _timestamp(minute.get('asOf'))
    stage, selected_branch = _stage(str(candidate.get('strategyBranch') or ''))
    base = {'scoreVersion': SCORE_VERSION, 'scoreMeaning':
            'V1.2固定权重、分阶段规则排序；非概率、收益预测或已成交绩效',
            'validationStatus': VALIDATION_STATUS,
            'riskPenaltyCoefficient': RISK_PENALTY_COEFFICIENT, 'riskPenaltyCoefficientStatus': 'unvalidated_initial',
            'historicalCoverageDays': days, 'baselineHistoryDays': days,
            'priceHistoricalCoverageDays': price_days,
            'confidenceStatus': status, 'probabilityStatus': 'withheld_until_calibrated',
            'confidenceMeaning': '历史与因子数据覆盖度，不是预测概率或准确率',
            'dataEligibility': {'status': 'insufficient' if status == 'insufficient' else 'observation',
                'minimumScoreDays': minimum, 'formalHistoryDays': formal,
                'criticalFactors': list(critical), 'criticalFactorsReady': False,
                'amountHistoryDays': days, 'priceHistoryDays': price_days},
            'missingFactorPolicy': PARAMS['missingFactorPolicy'],
            'missingRiskPolicy': PARAMS['missingRiskPolicy'],
            'missingFactors': list(EARLY_WEIGHTS), 'missingRiskComponents': list(RISK_WEIGHTS),
            'factorCoverage': {'early': 0., 'main': 0., 'distribution': 0.},
            'scoreStage': stage, 'selectedScoreBranch': selected_branch,
            'marketRegime': {'state': 'unknown', 'score': None, 'multiplier': 1., 'mode': 'downward_only'},
            'mainRiseProbability': None, 'transitionProbability': None,
            'distributionProbability': None,
            'expectedMFE1': None, 'expectedMFE3': None, 'expectedMFE5': None,
            'expectedMAE1': None, 'expectedMAE3': None, 'expectedMAE5': None,
            'remainingSpace': None, 'firstTradablePrice': None,
            'factorScores': {key: None for key in EARLY_WEIGHTS},
            'earlyScore': None, 'mainRiseScore': None, 'finalScore': None,
            'distributionRisk': None, 'riskComponents': {},
            'turnoverScore': None, 'tradabilityScore': None,
            'factorWeightsApplied': {'early': {}, 'main': {}, 'distribution': {}},
            'dataConfidence': 0., 'confidence': 0.,
            'v11Inputs': {key: _number(value) for key, value in raw.items()
                          if _number(value) is not None}}
    base['minuteArchivePath'] = minute.get('archivePath')
    base['sourceTime'] = (candidate.get('quote') or {}).get('provider_timestamp') or (candidate.get('quote') or {}).get('source_time')
    base['signalPrice'] = _number(candidate.get('referencePrice'))
    base['observableStructure'] = candidate.get('strategyBranch')
    base['triggerConditions'] = list(candidate.get('triggers') or [])
    base['invalidationConditions'] = list(candidate.get('invalidations') or [])
    base['tradability'] = {'status': 'unverified', 'firstTradablePrice': None,
                            'reason': '未验证盘口排队和实际成交'}
    if status == 'insufficient':
        return base
    evidence = _evidence_values(candidate)
    price = _number(candidate.get('referencePrice'))
    vwap = evidence.get('vwap')
    r1, r3, r5 = (raw.get(key) for key in ('r1_pct', 'r3_pct', 'r5_pct'))
    v3, v5 = raw.get('v3_ratio'), raw.get('v5_ratio')
    high5 = evidence.get('local_high_5m')
    sector_rel = evidence.get('sector_relative_5m_pct')
    sector_ret = evidence.get('sector_return_5m_pct')
    breadth = evidence.get('sector_breadth')
    active_buy = evidence.get('active_buy_share_pct')
    flow3 = evidence.get('main_net_flow_3m')
    if now is not None:
        try:
            stamp = datetime.fromisoformat((candidate.get('quote') or {}).get('active_flow_as_of') or '')
            if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= DEFAULT_PARAMETERS['freshness']['quoteSeconds']:
                active_buy = None
        except (TypeError, ValueError):
            active_buy = None
    # All monetary inputs and their historical baselines are interval CNY.
    breakout = (_clamp(PARAMS['centre'] + PARAMS['relativePriceScale'] * (price/high5-1))
                if price is not None and high5 and high5 > 0 else None)
    momentum, _ = _weighted({
        'r3': _z_score(raw.get('z_r3_pct')), 'r5': _z_score(raw.get('z_r5_pct')),
        'a3': _z_score(raw.get('z_a3_pct_per_min')),
        'a1': _z_score(raw.get('z_a1_pct_per_min')),
        'break': breakout}, PARAMS['momentumWeights'])
    if all(raw.get(key) is None for key in ('z_r3_pct','z_r5_pct','z_a3_pct_per_min','z_a1_pct_per_min')):
        momentum = None
    volume_structure = (_clamp(PARAMS['centre'] + PARAMS['ratioScale'] * (v3/v5-1)) if v3 is not None and v5 and v5 > 0 else None)
    volume, _ = _weighted({
        'v3': _z_score(raw.get('z_amount_3m')),
        'v5': _z_score(raw.get('z_amount_5m')),
        'va': _clamp(PARAMS['centre'] + PARAMS['ratioScale'] * (raw['va_ratio']-1)) if raw.get('va_ratio') is not None else None,
        'structure': volume_structure}, PARAMS['volumeWeights'])
    funds, _ = _weighted({
        'active': _clamp(PARAMS['centre'] + (active_buy-PARAMS['centre'])*PARAMS['activeBuyScale']) if active_buy is not None else None,
        'vendor_flow': PARAMS['flowPositive'] if flow3 is not None and flow3 > 0 else PARAMS['flowNegative'] if flow3 is not None and flow3 < 0 else PARAMS['centre'] if flow3 is not None else None},
        PARAMS['fundWeights'])
    vwap_position = _clamp(PARAMS['centre'] + PARAMS['relativePriceScale'] * (price/vwap-1)) if price is not None and vwap and vwap > 0 else None
    vwap_score, _ = _weighted({
        'position':vwap_position,
        # Input is percentage points: +0.1% -> 55, +1% -> 100, not +0.1% -> 100.
        'slope':_clamp(PARAMS['centre'] + PARAMS['vwapSlopePerPct']*raw['vwap_slope_5m_pct']) if raw.get('vwap_slope_5m_pct') is not None else None,
        'reclaim':PARAMS['vwapReclaim'] if raw.get('vwap_reclaim') == 1 else PARAMS['centre'] if raw.get('vwap_reclaim') == 0 else None,
        'retest':PARAMS['vwapRetest'] if raw.get('vwap_retest_success') == 1 else PARAMS['centre'] if raw.get('vwap_retest_success') == 0 else None},
        PARAMS['vwapWeights'])
    sector, _ = _weighted({
        'relative': _clamp(PARAMS['centre'] + PARAMS['sectorPctScale'] * sector_rel) if sector_rel is not None else None,
        'sector_return': _clamp(PARAMS['centre'] + PARAMS['sectorPctScale'] * sector_ret) if sector_ret is not None else None,
        'breadth': _clamp(breadth) if breadth is not None else None}, PARAMS['sectorWeights'])
    # T requires an explicit share-float unit. A snapshot turnover percentage or
    # uncertain circulating-market-cap unit is not a valid interval denominator.
    quote = candidate.get('quote') or {}
    float_shares = _number(candidate.get('float_shares') or quote.get('float_shares'))
    volume3 = _number(raw.get('volume_3m'))
    baseline_volume3 = _number(raw.get('baseline_volume_3m'))
    turnover_velocity = (volume3/float_shares*100 if float_shares and float_shares > 0
                         and volume3 is not None else None)
    turnover_ratio = (volume3/baseline_volume3 if turnover_velocity is not None
                      and baseline_volume3 and baseline_volume3 > 0 else None)
    turnover_score = (_clamp(PARAMS['centre'] + PARAMS['ratioScale']*(turnover_ratio-1)) if turnover_ratio is not None
                      and r3 is not None and r3 > 0 else
                      _clamp(PARAMS['centre'] - PARAMS['ratioScale']*(turnover_ratio-1)) if turnover_ratio is not None and r3 is not None else None)
    # L is a current order-book observation, never a verified executable fill.
    book = quote.get('order_book') or (quote.get('public_quote_metadata') or {}).get('order_book') or {}
    asks, bids = book.get('asks') or [],book.get('bids') or []
    tradability_score = None
    if asks and bids and quote.get('quote_usable_for_current_price_check') is not False:
        ask_price = _number(asks[0].get('price'))
        bid_price = _number(bids[0].get('price'))
        depth = sum((_number(row.get('price')) or 0)*(_number(row.get('volume_shares')) or 0)
                    for row in asks)
        try:
            book_at = datetime.fromisoformat(book.get('source_time') or '')
            book_fresh = now is None or (book_at.tzinfo is not None and
                                         0 <= (now-book_at).total_seconds() <= DEFAULT_PARAMETERS['freshness']['quoteSeconds'])
        except (TypeError,ValueError):
            book_fresh = False
        if book_fresh and ask_price and bid_price and ask_price >= bid_price > 0 and depth > 0:
            spread_pct = (ask_price-bid_price)/((ask_price+bid_price)/2)*100
            tradability_score = _clamp(PARAMS['bookDepthWeight']*_clamp(PARAMS['centre']+PARAMS['bookDepthLogScale']*math.log10(depth/PARAMS['bookDepthReferenceCny']))
                                       +PARAMS['bookSpreadWeight']*_clamp(PARAMS['maximum']-PARAMS['bookSpreadScale']*spread_pct))
            base['tradability'] = {'status':'order_book_observed',
                'estimatedAskPrice':ask_price,'askDepthNotionalCny':round(depth,2),
                'bidAskSpreadPct':round(spread_pct,4),'sourceTime':book.get('source_time'),
                'firstTradablePrice':None,'executionVerified':False}
    factors = {'M': momentum, 'V': volume, 'T': turnover_score, 'F': funds, 'W': vwap_score,
               'S': sector, 'B': breakout, 'C': _research_score(candidate, 'C', cutoff),
               'G': _research_score(candidate, 'G', cutoff), 'L': tradability_score}
    risk = {
        'volumeExhaustion': (_clamp(PARAMS['exhaustionBase'] + PARAMS['exhaustionScale'] * (v3-PARAMS['riskVolumeRatio'])) if v3 is not None and r3 is not None and v3 >= PARAMS['riskVolumeRatio'] and r3 <= 0 else 0.
                             if v3 is not None and r3 is not None else None),
        'priceInefficiency': (_clamp(PARAMS['inefficiencyBase'] + PARAMS['inefficiencyScale'] * (v5-PARAMS['riskVolumeRatio'])) if v5 is not None and r5 is not None and v5 >= PARAMS['riskVolumeRatio'] and r5 <= PARAMS['inefficiencyReturnPct'] else 0.
                              if v5 is not None and r5 is not None else None),
        'vwapBreak': PARAMS['vwapBreakRisk'] if price is not None and vwap and price < vwap else 0. if price is not None and vwap else None,
        'lowerHigh': _number((minute.get('v11Inputs') or {}).get('lower_high_risk')),
        'sectorDivergence': PARAMS['sectorDivergenceRisk'] if sector_ret is not None and r5 is not None and sector_ret > 0 and r5 < 0 else 0.
                            if sector_ret is not None and r5 is not None else None,
        'activeSell': PARAMS['activeSellRisk'] if active_buy is not None and active_buy < PARAMS['activeSellThreshold'] else 0. if active_buy is not None else None,
        'failedBreakout': _number((minute.get('v11Inputs') or {}).get('failed_breakout_risk'))}
    early, ew = _weighted(factors,EARLY_WEIGHTS)
    main, mw = _weighted(factors,MAIN_WEIGHTS)
    distribution, dw = _weighted(risk,RISK_WEIGHTS,missing=PARAMS['riskMissingScore'])
    coverage = min(days, price_days, formal) / formal
    available_weight = sum(EARLY_WEIGHTS[key] for key, value in factors.items() if value is not None)
    freshness = 1.
    try:
        at = datetime.fromisoformat(minute.get('asOf') or '')
        if at.tzinfo is None or cutoff is None or not 0 <= (cutoff-at).total_seconds() < DEFAULT_PARAMETERS['freshness']['minuteSeconds']:
            freshness = 0.
    except (TypeError, ValueError):
        freshness = 0.
    confidence = round(coverage * available_weight * freshness, 4)
    emotion = _research_score(candidate, 'E', cutoff)
    emotion_params = DEFAULT_PARAMETERS['researchFactors']['E']
    regime, multiplier = 'unknown', 1.
    if emotion is not None:
        regime = ('weak' if emotion < emotion_params['lowPercentile'] else
                  'hot' if emotion > emotion_params['highPercentile'] else 'normal')
        multiplier = (emotion_params['weakExposure'] if regime == 'weak' else
                      emotion_params['hotExposure'] if regime == 'hot' else 1.)
    selected = early if selected_branch == 'early' else main if selected_branch == 'main' else None
    critical_ready = all(factors.get(key) is not None for key in critical)
    base['dataEligibility'].update(criticalFactorsReady=critical_ready,
        status='formal' if status == 'complete' and critical_ready and freshness else 'observation')
    base.update(missingFactors=[key for key, value in factors.items() if value is None],
                missingRiskComponents=[key for key, value in risk.items() if value is None],
                factorCoverage={'early': round(available_weight, 4),
                    'main': round(sum(MAIN_WEIGHTS[key] for key, value in factors.items() if value is not None), 4),
                    'distribution': round(sum(RISK_WEIGHTS[key] for key, value in risk.items() if value is not None), 4)},
                marketRegime={'state': regime, 'score': emotion, 'multiplier': multiplier, 'mode': 'downward_only'})
    base.update(factorScores={key:round(value,4) if value is not None else None for key,value in {**factors,'T':turnover_score}.items()},
                earlyScore=round(early,4) if early is not None else None,
                mainRiseScore=round(main,4) if main is not None else None,
                finalScore=round(_clamp(selected-RISK_PENALTY_COEFFICIENT*distribution)*multiplier,4) if selected is not None and distribution is not None and freshness else None,
                distributionRisk=round(distribution,4) if distribution is not None else None,
                turnoverScore=round(turnover_score,4) if turnover_score is not None else None,
                tradabilityScore=round(tradability_score,4) if tradability_score is not None else None,
                riskComponents={key:value for key,value in risk.items() if value is not None},
                factorWeightsApplied={'early':ew,'main':mw,'distribution':dw},
                dataConfidence=confidence,confidence=confidence)
    for key,value in (('sector_relative_5m_pct',sector_rel),('sector_return_5m_pct',sector_ret),
                      ('sector_breadth',breadth),('active_buy_share_pct',active_buy),
                      ('main_net_flow_3m',flow3),('vwap',vwap),('local_high_5m',high5),
                      ('turnover_velocity_3m_pct',turnover_velocity),('turnover_velocity_ratio_3m',turnover_ratio)):
        if value is not None:
            base['v11Inputs'][key] = value
    for key in ('C', 'G', 'E', 'H'):
        research_value = _research_score(candidate, key, cutoff)
        if research_value is not None:
            base['v11Inputs'][f'research_{key}_score'] = research_value
            for name, value in ((candidate.get('researchFactors') or {})[key].get('features') or {}).items():
                base['v11Inputs'][f'research_{key}_{name}'] = _number(value)
    return base
