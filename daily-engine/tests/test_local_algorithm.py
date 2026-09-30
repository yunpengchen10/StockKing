"""The live selection and model validation must use the same contract and gate."""
from copy import deepcopy

import pytest

from src.services.yao_scout.local_algorithm import algorithm_contract, is_entry_eligible
from src.services.yao_scout.precision_policy import evaluate, V11_VERSION
from src.services.yao_scout.v11_factors import EARLY_WEIGHTS, MAIN_WEIGHTS, RISK_WEIGHTS, SCORE_VERSION, RISK_PENALTY_COEFFICIENT


def test_contract_reads_runtime_weights_and_changes_when_weights_change(monkeypatch):
    before = algorithm_contract()
    assert before['scoreVersion'] == SCORE_VERSION
    assert before['entryPolicyVersion'] == V11_VERSION
    assert before['weights'] == {'early': EARLY_WEIGHTS, 'main': MAIN_WEIGHTS, 'distribution': RISK_WEIGHTS}
    assert before['riskPenaltyCoefficient'] == RISK_PENALTY_COEFFICIENT
    before['weights']['early']['M'] = 999
    assert algorithm_contract()['weights']['early']['M'] != 999
    original = algorithm_contract()['contractId']
    monkeypatch.setitem(EARLY_WEIGHTS, 'M', EARLY_WEIGHTS['M'] + .01)
    assert algorithm_contract()['contractId'] != original


@pytest.mark.parametrize('change,expected', [({}, True), ({'status': 'premarket'}, False),
    ({'status': 'data_insufficient'}, False), ({'evidenceEligible': False}, False),
    ({'finalScore': None}, False), ({'finalScore': float('nan')}, False),
    ({'scoreVersion': 'old-rules'}, False), ({'strategyBranch': '盘前观察'}, False)])
def test_shared_gate_matches_live_precision_decision(change, expected):
    candidate = {'scoreVersion': SCORE_VERSION, 'status': 'conditional', 'evidenceEligible': True,
                 'finalScore': 61, 'quote': {'price': 10, 'pre_close': 9.8}, **change}
    candidate['precisionDecision'] = evaluate(candidate, {'gaps': []})
    for profile in ('conservative', 'regular', 'aggressive'):
        assert is_entry_eligible(candidate, profile) is expected
        if candidate['scoreVersion'] == SCORE_VERSION:
            assert candidate['precisionDecision']['profiles'][profile]['entryEligible'] is expected


def test_old_or_incomplete_entry_decision_cannot_qualify_by_score_alone():
    candidate = {'scoreVersion': SCORE_VERSION, 'status': 'conditional', 'evidenceEligible': True,
                 'finalScore': 99, 'precisionDecision': {'version': V11_VERSION,
                     'profiles': {'regular': {'entryEligible': True}}}}
    assert is_entry_eligible(candidate)
    old = deepcopy(candidate)
    old['precisionDecision']['version'] = 'old-policy'
    assert not is_entry_eligible(old)
    candidate['precisionDecision']['profiles']['regular']['entryEligible'] = False
    assert not is_entry_eligible(candidate)
