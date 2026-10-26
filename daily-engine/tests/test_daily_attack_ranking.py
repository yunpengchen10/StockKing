"""Behavioral regressions for unsigned turnover bias and lost breakout research."""
from copy import deepcopy

import pytest

from tests.test_daily_primary import NOW, CODE, daily, quote, context, run
from src.services.yao_scout.daily_primary import build_daily_candidates, _rank_candidates


def ranked_row(code, progress, efficiency, amount):
    return {'code': code, 'dailyRankEvidence': {'directionalEfficiency': efficiency,
        'openReturnAtr': progress, 'amountTieBreakCny': amount}}


def test_direction_and_progress_beat_large_unsigned_amount_and_tiny_high_print():
    # These are causal ordering cases, not an asserted profitable historical strategy.
    rows = [ranked_row('600001', .01, 1., 1e9),  # one tick at the high
            ranked_row('600002', 1.5, .9, 1e7),  # sustained-looking snapshot progress
            ranked_row('600003', .05, .1, 1e10), # large amount, very poor direction
            ranked_row('600004', .1, .2, 1e8)]
    original = deepcopy(rows)
    result = _rank_candidates(rows)
    assert result[0]['code'] == '600002'
    assert result[-1]['code'] == '600003'
    assert rows == original
    assert result[0]['dailyRankEvidence']['referencePeerCount'] == 4
    assert result[0]['dailyRankEvidence']['relativeRankScore'] == pytest.approx(.75)
    assert [x['code'] for x in _rank_candidates(list(reversed(rows)))] == [x['code'] for x in result]


def test_breaking_all_historical_highs_is_retained_as_research_without_fabricated_target():
    packet = daily()
    packet['metrics'].update(high20=10., high60=10.)
    result = run(packet=packet)
    assert result['dailyCandidates'] == []
    watch = result['dailyAttackWatchlist'][0]
    assert watch['breakout20'] and watch['breakout60']
    assert watch['resistance'] is None and watch['structureRewardRisk'] is None
    assert watch['entryEligible'] is False and watch['trainingEligible'] is False
    assert watch['probability'] is None and watch['executable'] is False
    assert watch['dailyPrimaryReasons'] == ['daily_support_or_upside_space_unknown']
    assert result['dailyRejectionLedger'][0]['state'] == 'excluded'


def test_shadow_cannot_bypass_bad_data_direction_or_event_risk():
    for kwargs in ({'q': quote(amount=10_060_000)}, {'ctx': {}},
                   {'q': quote(no_price_limit=True)}, {'q': quote(provider_timestamp=None)}):
        result = run(**kwargs)
        assert not result['dailyCandidates'] and not result['dailyAttackWatchlist']
        assert result['dailyRejectionLedger'][0]['reasons']


def test_regular_selection_does_not_inherit_conservative_rejection_reason():
    ctx = context()
    ctx['financials']['cashPerShare'] = -.1
    result = run(ctx=ctx)
    ledger = result['dailyRejectionLedger'][0]
    assert ledger['state'] == 'observed_regular' and ledger['reasons'] == []
    assert ledger['profileDecisions']['conservative']['reasons'] == ['known_nonpositive_operating_cash_flow']
    assert ledger['profileDecisions']['regular']['rank'] == 1
    assert ledger['profileDecisions']['regular']['selected'] is False
    assert ledger['profileDecisions']['regular']['observationSelected'] is True
    assert result['dailyAttackWatchlist'] == []  # separate alternatives, no duplicated observation


def test_qualified_below_quota_is_distinct_from_rejected_and_freezes_peer_ranks():
    codes = ['600001', '600002']
    result = build_daily_candidates([{'code': code, 'name': code} for code in codes],
        {code: daily() for code in codes}, {code: quote(code=code) for code in codes}, NOW,
        contexts={code: context() for code in codes}, top_n=1)
    ledger = {row['code']: row for row in result['dailyRejectionLedger']}
    assert ledger['600001']['state'] == 'observed_regular'
    assert ledger['600002']['state'] == 'observation_below_top_n'
    assert ledger['600002']['reasons'] == []
    assert ledger['600002']['profileDecisions']['regular']['selected'] is False
    assert ledger['600002']['profileDecisions']['regular']['observationSelected'] is False
    assert ledger['600002']['profileDecisions']['regular']['rank'] == 2
    assert result['dailyCandidates'][0]['dailyRankEvidence']['referencePeerCount'] == 2
    assert result['dailyAttackWatchlist'][0]['dailyRankEvidence']['referencePeerCount'] == 1
    assert {row['code'] for row in result['dailyObservationResearch']} == set(codes)


def test_bad_market_is_preserved_as_risk_in_observations_and_never_becomes_entry():
    market = {'source': 'whole_market_snapshot', 'asOf': NOW.isoformat(),
        'features': {'source_coverage': 1., 'universe_count': 3000, 'advancing_pct': 40}}
    result = run(market=market, top_n=0)
    assert not result['dailyCandidates']
    observation = result['dailyObservationResearch'][0]
    assert observation['marketBreadth']['status'] == 'observed_weak'
    assert observation['entryEligible'] is False and observation['selected'] is False
    assert observation['marketRiskState'] == 'weak'
    assert result['marketEmotionPanel']['features'] == market['features']
    assert result['marketEmotionPanel']['decisionAssessment']['status'] == 'observed_weak'
