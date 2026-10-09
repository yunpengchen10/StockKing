"""The live selection and model validation must use the same contract and gate."""
from copy import deepcopy

import pytest

from src.services.yao_scout.local_algorithm import algorithm_contract, is_entry_eligible
from src.services.yao_scout.precision_policy import evaluate, V11_VERSION
from src.services.yao_scout.v11_factors import EARLY_WEIGHTS, MAIN_WEIGHTS, RISK_WEIGHTS, SCORE_VERSION, RISK_PENALTY_COEFFICIENT


def test_contract_freezes_defaults_and_fingerprints_all_parameters(monkeypatch):
    from src.services.yao_scout import research_policy
    before = algorithm_contract()
    assert before['scoreVersion'] == SCORE_VERSION
    assert before['entryPolicyVersion'] == V11_VERSION
    assert before['weights'] == {'early': EARLY_WEIGHTS, 'main': MAIN_WEIGHTS, 'distribution': RISK_WEIGHTS}
    assert before['riskPenaltyCoefficient'] == RISK_PENALTY_COEFFICIENT
    before['weights']['early']['M'] = 999
    assert algorithm_contract()['weights']['early']['M'] != 999
    original = algorithm_contract()['contractId']
    with pytest.raises(TypeError):
        EARLY_WEIGHTS['M'] = 999
    parameters = research_policy.parameter_contract()['defaults']
    parameters['researchFactors']['C']['halfLifeSessions'] = 3
    monkeypatch.setattr(research_policy, 'DEFAULT_PARAMETERS', research_policy._freeze(parameters))
    assert algorithm_contract()['contractId'] != original


def _eligible_candidate():
    return {'scoreVersion': SCORE_VERSION, 'status': 'conditional', 'evidenceEligible': True,
            'strategyBranch': '分钟局部突破观察', 'finalScore': 61,
            'quote': {'price': 10, 'pre_close': 9.8, 'limit_up': 10.78, 'limit_down': 8.82}, 'baselineHistoryDays': 20,
            'priceHistoricalCoverageDays': 20, 'factorScores': {key: 60 for key in ('M','V','W','B')},
            'dataEligibility': {'status': 'formal'}, 'structureRewardRisk': 3,
            'evidenceChecks': {'minute_structure': True}}


CONTEXT = {'financials': {'profit': 10000, 'eps': 1, 'cashPerShare': .5},
           'coverage': {'financials': True, 'forecast': True, 'unlock': True}}
DAILY = {'metrics': {'last_close': 9.8, 'ma5': 9.8, 'ma10': 9.7, 'ma20': 9.6, 'atr14': .4}}


@pytest.mark.parametrize('change,expected', [({}, True), ({'status': 'premarket'}, False),
    ({'status': 'data_insufficient'}, False), ({'evidenceEligible': False}, False),
    ({'finalScore': None}, False), ({'finalScore': float('nan')}, False),
    ({'scoreVersion': 'old-rules'}, False), ({'strategyBranch': '盘前观察'}, False),
    ({'baselineHistoryDays': 19}, False), ({'priceHistoricalCoverageDays': 19}, False),
    ({'factorScores': {'M': 90, 'V': 90, 'W': 90}}, False),
    ({'dataEligibility': {'status': 'observation'}}, False)])
def test_shared_gate_matches_live_precision_decision(change, expected):
    candidate = {**_eligible_candidate(), **change}
    candidate['precisionDecision'] = evaluate(candidate, CONTEXT, DAILY)
    for profile in ('conservative', 'regular', 'aggressive'):
        assert is_entry_eligible(candidate, profile) is expected
        if candidate['scoreVersion'] == SCORE_VERSION:
            assert candidate['precisionDecision']['profiles'][profile]['entryEligible'] is expected


def test_old_or_incomplete_entry_decision_cannot_qualify_by_score_alone():
    candidate = {**_eligible_candidate(), 'finalScore': 99, 'precisionDecision': {'version': V11_VERSION,
                     'profiles': {'regular': {'entryEligible': True}}}}
    assert is_entry_eligible(candidate)
    old = deepcopy(candidate)
    old['precisionDecision']['version'] = 'old-policy'
    assert not is_entry_eligible(old)
    candidate['precisionDecision']['profiles']['regular']['entryEligible'] = False
    assert not is_entry_eligible(candidate)


def test_confidence_is_coverage_and_not_an_extra_probability_threshold():
    candidate = {**_eligible_candidate(), 'confidence': .2, 'dataConfidence': .2}
    candidate['precisionDecision'] = evaluate(candidate, CONTEXT, DAILY)
    assert is_entry_eligible(candidate)
