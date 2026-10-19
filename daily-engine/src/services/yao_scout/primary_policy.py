"""Fail-closed primary admission; research observations never fill its quota.

These are prospective evidence/risk requirements, not fitted win probabilities.
Market breadth uses a current, sufficiently covered observation independently of
the 60-session history required for the separate emotion percentile factor.
"""
from datetime import datetime

from .daily_opportunities import finite
from .research_policy import DEFAULT_PARAMETERS

VERSION = 'primary-evidence-20261015-v1'


def policy_contract():
    return {'version': VERSION, 'validationStatus': 'rules_unvalidated',
            'requires': ['verified_minute_lineage', 'fresh_aligned_minute_sector_fund',
                         'minute_structure', 'relative_volume', 'sector_resonance',
                         'fund_direction', 'fresh_whole_market_breadth',
                         'no_weak_market', 'profile_risk_pass', 'morning_validated_model'],
            'minimumAdvancingPct': 50,
            'missingPolicy': 'research_only', 'quotaPolicy': 'up_to_limit_no_fill',
            'meaning': '主推荐须通过数据、方向和分档风险检查；上午另须通过当天收盘任务验证；可为空，规则不代表胜率'}


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value or '')
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_primary_admission(candidate, now=None):
    now = _at(now or candidate.get('decision_at'))
    missing, adverse = [], []
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
    elif breadth < policy_contract()['minimumAdvancingPct'] or market.get('regime') == 'weak':
        adverse.append('weak_market_primary_entries_paused')
    return {'version': VERSION, 'eligible': not missing and not adverse,
            'reasons': adverse, 'missingReasons': missing,
            'evaluatedAt': now.isoformat() if now else None,
            'marketAdvancingPct': breadth, 'validationStatus': 'rules_unvalidated'}
