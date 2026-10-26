"""Fail-closed primary admission; research observations never fill its quota.

These are prospective evidence/risk requirements, not fitted win probabilities.
Market breadth uses a current, sufficiently covered observation independently of
the 60-session history required for the separate emotion percentile factor.
"""
from datetime import datetime

from .daily_opportunities import finite
from .research_policy import DEFAULT_PARAMETERS

VERSION = 'primary-evidence-20261015-v2'


def policy_contract():
    return {'version': VERSION, 'validationStatus': 'rules_unvalidated',
            'requires': ['verified_minute_lineage', 'fresh_aligned_minute_sector_fund',
                         'minute_structure', 'relative_volume', 'sector_resonance',
                         'fund_direction', 'fresh_whole_market_breadth',
                         'market_risk_budget', 'profile_risk_pass'],
            'modelAdmission': {'requiredTask': 'entry_today_exit_next_session',
                               'requiresValidatedNetProfitHead': True, 'morningDirectionRole': 'research_only'},
            'weakMarketAdvancingPctThreshold': 50,
            'weakMarketPolicy': 'verified_weak_market_uses_existing_reduced_portfolio_budget',
            'missingPolicy': 'research_only', 'quotaPolicy': 'up_to_limit_no_fill',
            'meaning': '证据准入检查数据、方向和分档风险；已核验弱市使用减半总风险预算。正式主推荐另需冻结入场区间和已验证T+1净盈利模型；上午当日方向仅研究。'}


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value or '')
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_primary_admission(candidate, now=None):
    now = _at(now or candidate.get('decision_at'))
    missing, adverse, warnings = [], [], []
    market_risk = 'unknown'
    minute = _at(candidate.get('minuteSourceTime'))
    boundary = _at(candidate.get('factorCutoffAt'))
    max_age = DEFAULT_PARAMETERS['freshness']['minuteSeconds']

    def fresh(stamp, limit=max_age):
        return bool(now and stamp and 0 <= (now-stamp).total_seconds() < limit)

    if (candidate.get('dataLineage') or {}).get('status') != 'verified':
        missing.append('minute_lineage_not_verified')
    if not fresh(minute) or not boundary or not now or not minute or not minute <= boundary <= now:
        missing.append('minute_window_missing_future_or_stale')
    availability = candidate.get('factorAvailability') or {}
    checks = candidate.get('evidenceChecks') or {}
    for factor, packet_key, check in (('S', 'sectorEvidence', 'sector_resonance'),
                                      ('F', 'fundEvidence', 'fund_direction')):
        packet = candidate.get(packet_key) or {}
        stamp = _at(packet.get('asOf'))
        if not availability.get(factor, {}).get('available') or not fresh(stamp) or stamp != minute:
            missing.append(factor + '_source_missing_stale_or_misaligned')
        elif checks.get(check) is not True:
            adverse.append(check + '_not_confirmed')
    quote = candidate.get('quote') or {}
    active = finite(quote.get('active_buy_share_pct'))
    if (not quote.get('active_flow_source') or
            not fresh(_at(quote.get('active_flow_as_of')), DEFAULT_PARAMETERS['freshness']['quoteSeconds']) or
            active is None or not 0 <= active <= 100):
        missing.append('active_flow_classification_missing_or_stale')
    # Missing minute data already fails admission above. Its default false
    # feature flags are not observations of adverse price/volume conditions.
    minute_observed = not any(reason in missing for reason in (
        'minute_lineage_not_verified', 'minute_window_missing_future_or_stale'))
    if minute_observed:
        for check in ('minute_structure', 'relative_volume'):
            if checks.get(check) is not True:
                adverse.append(check + '_not_confirmed')
        if finite((candidate.get('v11Inputs') or {}).get('failed_breakout_risk')) == 100:
            adverse.append('latest_completed_minute_failed_breakout')

    market = (candidate.get('researchFactors') or {}).get('E') or {}
    features = market.get('features') or {}
    policy = DEFAULT_PARAMETERS['researchFactors']['E']
    coverage, universe = finite(features.get('source_coverage')), finite(features.get('universe_count'))
    breadth = finite(features.get('advancing_pct'))
    if (not market.get('source') or not fresh(_at(market.get('asOf')), policy['maxSourceAgeSeconds']) or
            coverage is None or not policy['minimumCoverage'] <= coverage <= 1 or
            universe is None or universe < policy['minimumUniverse'] or
            breadth is None or not 0 <= breadth <= 100):
        missing.append('whole_market_breadth_unavailable_or_stale')
    elif breadth < policy_contract()['weakMarketAdvancingPctThreshold'] or market.get('regime') == 'weak':
        market_risk = 'weak'
        warnings.append('verified_weak_market_reduced_risk_budget')
    else:
        market_risk = 'hot' if market.get('regime') == 'hot' else 'normal'
    return {'version': VERSION, 'eligible': not missing and not adverse,
            'reasons': adverse, 'missingReasons': missing,
            'marketRiskState': market_risk, 'warnings': warnings,
            'evaluatedAt': now.isoformat() if now else None,
            'marketAdvancingPct': breadth, 'validationStatus': 'rules_unvalidated'}
