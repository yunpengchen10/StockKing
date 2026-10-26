"""Predeclared factor ablations stored prospectively; never change live ranks."""
from collections import defaultdict, Counter
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from hashlib import sha256
import json
import math

VERSION = 'factor-ablation-shadow-v1'
TZ = ZoneInfo('Asia/Shanghai')
VARIANTS = ('baseline', 'without_duplicate_breakout', 'without_turnover_overlap',
            'vwap_position_capped', 'fund_flow_persistence')


def experiment_contract():
    from .v11_factors import EARLY_WEIGHTS, MAIN_WEIGHTS, PARAMS
    from .research_policy import _plain
    formula = {'early': EARLY_WEIGHTS, 'main': MAIN_WEIGHTS, 'parameters': PARAMS}
    payload = {'version': VERSION, 'variants': list(VARIANTS), 'shadowOnly': True,
               'normalization': 'fixed_original_denominator', 'vwapPositionCap': 75,
               'baselineFormulaHash': sha256(json.dumps(_plain(formula), sort_keys=True).encode()).hexdigest(),
               'fundPersistence': 'replace_vendor_sign_component_with_100_times_positive_fraction_of_three_verified_minute_deltas',
               'selectionAuthority': False, 'evaluation': 'frozen_scores_complete_cohorts_time_holdout',
               'promotion': 'none; independent validation required'}
    return {**payload, 'contractId': sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]}


def _finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _stamp(value):
    try:
        value = datetime.fromisoformat(str(value))
        return value.astimezone(TZ) if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def annotate_factor_experiments(candidate):
    from .v11_factors import EARLY_WEIGHTS, MAIN_WEIGHTS, RISK_PENALTY_COEFFICIENT, PARAMS
    branch = candidate.get('selectedScoreBranch')
    weights = EARLY_WEIGHTS if branch == 'early' else MAIN_WEIGHTS if branch == 'main' else None
    factors = candidate.get('factorScores') or {}
    risk = _finite(candidate.get('distributionRisk'))
    multiplier = _finite((candidate.get('marketRegime') or {}).get('multiplier'))
    baseline = _finite(candidate.get('finalScore'))
    scores = {key: None for key in VARIANTS}
    scores['baseline'] = baseline
    if weights and baseline is not None and risk is not None and multiplier is not None:
        weights = {key: value/sum(weights.values()) for key, value in weights.items()}
        stage = sum(weight * (_finite(factors.get(key)) or 0) for key, weight in weights.items())
        def score(adjustment):
            return round(min(100., max(0., stage - adjustment - RISK_PENALTY_COEFFICIENT*risk))*multiplier, 4)
        for variant, key in [('without_duplicate_breakout', 'B'), ('without_turnover_overlap', 'T')]:
            if _finite(factors.get(key)) is not None:
                scores[variant] = score(weights.get(key, 0)*float(factors[key]))
        inputs = candidate.get('v11Inputs') or {}
        price = _finite((candidate.get('quote') or {}).get('price'))
        vwap = _finite(inputs.get('vwap'))
        if price is not None and vwap and vwap > 0 and _finite(factors.get('W')) is not None:
            position = min(100., max(0., PARAMS['centre'] + PARAMS['relativePriceScale']*(price/vwap-1)))
            adjustment = max(0., position-75)*PARAMS['vwapWeights']['position']*weights.get('W', 0)
            scores['vwap_position_capped'] = score(adjustment)
        packet = candidate.get('fundEvidence') or {}
        metrics = packet.get('metrics') or {}
        deltas = metrics.get('main_net_flow_1m_deltas')
        flow = _finite(inputs.get('main_net_flow_3m'))
        fund_at, minute_at, cutoff = (_stamp(value) for value in
            (packet.get('asOf'), candidate.get('minuteSourceTime'), candidate.get('factorCutoffAt')))
        verified_window = (packet.get('source') and fund_at and minute_at and cutoff
            and fund_at == minute_at and 0 <= (cutoff-fund_at).total_seconds() < 300)
        if (isinstance(deltas, list) and len(deltas) == 3
                and verified_window
                and all(_finite(value) is not None for value in deltas)
                and flow is not None and _finite(factors.get('F')) is not None
                and math.isclose(sum(float(value) for value in deltas), flow, rel_tol=1e-8, abs_tol=.01)):
            old = PARAMS['flowPositive'] if flow > 0 else PARAMS['flowNegative'] if flow < 0 else PARAMS['centre']
            persistent = 100*sum(float(value) > 0 for value in deltas)/3
            fund_weight = PARAMS['fundWeights']['vendor_flow']/sum(PARAMS['fundWeights'].values())
            scores['fund_flow_persistence'] = score((old-persistent)*fund_weight*weights.get('F', 0))
    candidate['factorExperiments'] = {**experiment_contract(), 'scores': scores,
                                       'meaning': '预先定义的因子删减对照，仅记录研究分，不改变推荐或概率'}
    return candidate


def evaluate_frozen_experiments(rows, *, holdout_start, as_of=None, top_n=5, minimum_days=20):
    """Evaluate frozen score cohorts, not variants chosen after seeing outcomes.

Rows: day, code, decisionAt, labelEndAt, labelKnownAt, eligible,
factorExperiments, outcomeStatus, netReturn. Missing outcomes exclude the entire
day, never only its losing rows. Development labels cannot cross the holdout.
Repeated scans are reduced to the first stock/day. All variants share cohorts.
"""
    split = date.fromisoformat(holdout_start)
    boundary = datetime.combine(split, time(), TZ)
    cutoff = _stamp(as_of) if as_of is not None else datetime.now(TZ)
    if cutoff is None:
        raise ValueError('as_of requires a timezone-aware timestamp')
    if top_n < 1 or minimum_days < 1:
        raise ValueError('positive limits required')
    cohorts, seen = defaultdict(list), set()
    expected = experiment_contract()['contractId']
    for row in sorted(rows, key=lambda row: (str(row.get('day')), str(row.get('decisionAt')), str(row.get('code')))):
        if not row.get('eligible'):
            continue
        key = (str(row['day']), str(row['code']))
        if key in seen:
            continue
        seen.add(key)
        cohorts[key[0]].append(row)
    reports, exclusions = {}, Counter()
    usable = {'development': [], 'holdout': []}
    for day, cohort in sorted(cohorts.items()):
        timing = [(_stamp(row.get('decisionAt')), _stamp(row.get('labelEndAt')), _stamp(row.get('labelKnownAt'))) for row in cohort]
        if any(not decision or not end or not known or decision.date().isoformat() != day
               or not decision <= end <= known <= cutoff for decision, end, known in timing):
            exclusions['invalid_or_future_label_timing'] += 1
            continue
        if date.fromisoformat(day) < split and any(known >= boundary for _, _, known in timing):
            exclusions['development_label_overlaps_holdout'] += 1
            continue
        if any((row.get('factorExperiments') or {}).get('contractId') != expected for row in cohort):
            exclusions['contract_missing_or_mismatched'] += 1
            continue
        if any(row.get('outcomeStatus') not in {'mature', 'no_fill'} or _finite(row.get('netReturn')) is None
               or (row.get('outcomeStatus') == 'no_fill' and float(row['netReturn']) != 0) for row in cohort):
            exclusions['incomplete_outcome_cohort'] += 1
            continue
        if any(any(_finite((row['factorExperiments'].get('scores') or {}).get(variant)) is None for variant in VARIANTS) for row in cohort):
            exclusions['incomplete_factor_cohort'] += 1
            continue
        usable['holdout' if date.fromisoformat(day) >= split else 'development'].append((day, cohort))
    for period, days in usable.items():
        variants = {}
        for variant in VARIANTS:
            returns, daily, codes = [], [], []
            for day, cohort in days:
                selected = sorted(cohort, key=lambda row: (-float(row['factorExperiments']['scores'][variant]), str(row['code'])))[:top_n]
                values = [float(row['netReturn']) for row in selected]
                returns.extend(values)
                daily.append(sum(values)/len(values))
                codes.append({'day': day, 'codes': [row['code'] for row in selected]})
            variants[variant] = {'independentDays': len(days), 'observations': len(returns),
                'meanDailyNetReturn': sum(daily)/len(daily) if daily else None,
                'profitableCount': sum(value > 0 for value in returns),
                'lossCount': sum(value < 0 for value in returns),
                'worstTrade': min(returns) if returns else None, 'selections': codes}
        reports[period] = variants
    return {**experiment_contract(), 'holdoutStart': holdout_start, 'asOf': cutoff.isoformat(), 'topN': top_n,
            'status': 'descriptive_only' if len(usable['holdout']) >= minimum_days else 'insufficient_holdout_days',
            'excludedDays': dict(exclusions), 'periods': reports,
            'meaning': '冻结因子分的相同完整样本比较；不自动选择、训练或发布最优变体，不是组合回测'}
