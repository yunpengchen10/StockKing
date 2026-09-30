"""Auditable V1.1 rule scores. These values are rankings, not probabilities.

Only same-clock completed-minute baselines with at least five sessions enter
scores. A missing factor is excluded and the remaining declared weights are
renormalized. Calibrated MFE, MAE and state probabilities are withheld.
"""
from __future__ import annotations

import math
from datetime import datetime

EARLY_WEIGHTS = {'M': .18, 'V': .14, 'F': .14, 'W': .12, 'S': .12,
                 'B': .10, 'C': .08, 'G': .07, 'L': .05}
MAIN_WEIGHTS = {'F': .18, 'M': .16, 'S': .14, 'B': .13, 'W': .12,
                'V': .10, 'G': .07, 'C': .05, 'L': .05}
RISK_WEIGHTS = {'volumeExhaustion': .20, 'priceInefficiency': .20,
                'vwapBreak': .15, 'lowerHigh': .15, 'sectorDivergence': .10,
                'activeSell': .10, 'failedBreakout': .10}
SCORE_VERSION = 'stockking-v1.1-rules'
RISK_PENALTY_COEFFICIENT = .2


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clamp(value, low=0., high=100.):
    return min(high, max(low, value))


def _z_score(value):
    value = _number(value)
    return _clamp(50 + 12.5 * value) if value is not None else None


def _weighted(values, weights):
    available = {key: _number(values.get(key)) for key in weights}
    available = {key: value for key, value in available.items() if value is not None}
    weight_sum = sum(weights[key] for key in available)
    return ((sum(available[key] * weights[key] for key in available) / weight_sum,
             {key: round(weights[key] / weight_sum, 6) for key in available})
            if weight_sum > 0 else (None, {}))


def _evidence_values(candidate):
    return {row.get('key'): _number(row.get('value'))
            for row in candidate.get('indicatorEvidence', [])
            if isinstance(row, dict) and row.get('status') == 'observed'}


def score_v11(candidate, minute, *, now=None):
    """Add ranking evidence without relaxing eligibility or implying a fill."""
    minute = minute or {}
    raw = dict(minute.get('v11Inputs') or {})
    days = int(raw.get('history_days') or 0)
    price_days = int(raw.get('price_history_days') or 0)
    status = ('complete' if days >= 20 and price_days >= 20 else
              'low' if max(days,price_days) >= 5 else 'insufficient')
    base = {'scoreVersion': SCORE_VERSION, 'scoreMeaning':
            'V1.1可观察因子启发式排序；非概率、收益预测或已成交绩效',
            'riskPenaltyCoefficient': RISK_PENALTY_COEFFICIENT, 'riskPenaltyCoefficientStatus': 'unvalidated_initial',
            'historicalCoverageDays': days, 'baselineHistoryDays': days,
            'priceHistoricalCoverageDays': price_days,
            'confidenceStatus': status, 'probabilityStatus': 'withheld_until_calibrated',
            'mainRiseProbability': None, 'transitionProbability': None,
            'distributionProbability': None,
            'expectedMFE1': None, 'expectedMFE3': None, 'expectedMFE5': None,
            'expectedMAE1': None, 'expectedMAE3': None, 'expectedMAE5': None,
            'remainingSpace': None, 'firstTradablePrice': None,
            'factorScores': {key: None for key in (*EARLY_WEIGHTS, 'T')},
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
            if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= 30:
                active_buy = None
        except (TypeError, ValueError):
            active_buy = None
    # All monetary inputs and their historical baselines are interval CNY.
    breakout = (_clamp(50 + 500 * (price/high5-1))
                if price is not None and high5 and high5 > 0 else None)
    momentum, _ = _weighted({
        'r3': _z_score(raw.get('z_r3_pct')), 'r5': _z_score(raw.get('z_r5_pct')),
        'a3': _z_score(raw.get('z_a3_pct_per_min')),
        'a1': _z_score(raw.get('z_a1_pct_per_min')),
        'break': breakout}, {'r3': .25, 'r5': .20, 'a3': .30, 'a1': .15, 'break': .10})
    if all(raw.get(key) is None for key in ('z_r3_pct','z_r5_pct','z_a3_pct_per_min','z_a1_pct_per_min')):
        momentum = None
    volume_structure = (_clamp(50 + 20 * (v3/v5-1)) if v3 is not None and v5 and v5 > 0 else None)
    volume, _ = _weighted({
        'v3': _z_score(raw.get('z_amount_3m')),
        'v5': _z_score(raw.get('z_amount_5m')),
        'va': _clamp(50 + 20 * (raw['va_ratio']-1)) if raw.get('va_ratio') is not None else None,
        'structure': volume_structure}, {'v3': .35, 'v5': .25, 'va': .20, 'structure': .20})
    funds, _ = _weighted({
        'active': _clamp(50 + (active_buy-50)*2) if active_buy is not None else None,
        'vendor_flow': 75. if flow3 is not None and flow3 > 0 else 25. if flow3 is not None and flow3 < 0 else 50. if flow3 is not None else None},
        {'active': .5, 'vendor_flow': .5})
    vwap_position = _clamp(50 + 500 * (price/vwap-1)) if price is not None and vwap and vwap > 0 else None
    vwap_score, _ = _weighted({
        'position':vwap_position,
        'slope':_clamp(50 + 500*raw['vwap_slope_5m_pct']) if raw.get('vwap_slope_5m_pct') is not None else None,
        'reclaim':90. if raw.get('vwap_reclaim') == 1 else 50. if raw.get('vwap_reclaim') == 0 else None,
        'retest':85. if raw.get('vwap_retest_success') == 1 else 50. if raw.get('vwap_retest_success') == 0 else None},
        {'position':.35,'slope':.25,'reclaim':.20,'retest':.20})
    sector, _ = _weighted({
        'relative': _clamp(50 + 10 * sector_rel) if sector_rel is not None else None,
        'sector_return': _clamp(50 + 10 * sector_ret) if sector_ret is not None else None,
        'breadth': breadth}, {'relative': .45, 'sector_return': .30, 'breadth': .25})
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
    turnover_score = (_clamp(50 + 20*(turnover_ratio-1)) if turnover_ratio is not None
                      and r3 is not None and r3 > 0 else
                      _clamp(50 - 20*(turnover_ratio-1)) if turnover_ratio is not None and r3 is not None else None)
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
                                        0 <= (now-book_at).total_seconds() <= 30)
        except (TypeError,ValueError):
            book_fresh = False
        if book_fresh and ask_price and bid_price and ask_price >= bid_price > 0 and depth > 0:
            spread_pct = (ask_price-bid_price)/((ask_price+bid_price)/2)*100
            tradability_score = _clamp(.6*_clamp(50+10*math.log10(depth/1e6))
                                       +.4*_clamp(100-150*spread_pct))
            base['tradability'] = {'status':'order_book_observed',
                'estimatedAskPrice':ask_price,'askDepthNotionalCny':round(depth,2),
                'bidAskSpreadPct':round(spread_pct,4),'sourceTime':book.get('source_time'),
                'firstTradablePrice':None,'executionVerified':False}
    # C/G require verified events and a long historical stock-character sample.
    factors = {'M': momentum, 'V': volume, 'F': funds, 'W': vwap_score,
               'S': sector, 'B': breakout, 'C': None, 'G': None, 'L': tradability_score}
    risk = {
        'volumeExhaustion': (_clamp(40 + 20 * (v3-2)) if v3 is not None and r3 is not None and v3 >= 2 and r3 <= 0 else 0.
                             if v3 is not None and r3 is not None else None),
        'priceInefficiency': (_clamp(70 + 10 * (v5-2)) if v5 is not None and r5 is not None and v5 >= 2 and r5 <= .2 else 0.
                              if v5 is not None and r5 is not None else None),
        'vwapBreak': 80. if price is not None and vwap and price < vwap else 0. if price is not None and vwap else None,
        'lowerHigh': _number((minute.get('v11Inputs') or {}).get('lower_high_risk')),
        'sectorDivergence': 85. if sector_ret is not None and r5 is not None and sector_ret > 0 and r5 < 0 else 0.
                            if sector_ret is not None and r5 is not None else None,
        'activeSell': 80. if active_buy is not None and active_buy < 45 else 0. if active_buy is not None else None,
        'failedBreakout': _number((minute.get('v11Inputs') or {}).get('failed_breakout_risk'))}
    early, ew = _weighted(factors,EARLY_WEIGHTS)
    main, mw = _weighted(factors,MAIN_WEIGHTS)
    distribution, dw = _weighted(risk,RISK_WEIGHTS)
    coverage = max(.1, .5*min(days,20)/20 + .5*min(price_days,20)/20)
    available_weight = sum(EARLY_WEIGHTS[key] for key, value in factors.items() if value is not None)
    freshness = 1.
    try:
        at = datetime.fromisoformat(minute.get('asOf') or '')
        if now is not None and (at.tzinfo is None or not 0 <= (now-at).total_seconds() < 300):
            freshness = 0.
    except (TypeError, ValueError):
        freshness = 0.
    confidence = round(coverage * available_weight * freshness, 4)
    base.update(factorScores={key:round(value,4) if value is not None else None for key,value in {**factors,'T':turnover_score}.items()},
                earlyScore=round(early,4) if early is not None else None,
                mainRiseScore=round(main,4) if main is not None else None,
                finalScore=round(_clamp(max(early,main)-RISK_PENALTY_COEFFICIENT*distribution),4) if early is not None and main is not None and distribution is not None and freshness else None,
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
    return base
