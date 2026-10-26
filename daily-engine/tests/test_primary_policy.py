from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from tests.primary_fixture import make_primary_evidence
from src.services.yao_scout.primary_policy import evaluate_primary_admission
from tests.test_local_algorithm import _eligible_candidate, CONTEXT, DAILY
from src.services.yao_scout.precision_policy import evaluate
from src.services.yao_scout.local_algorithm import is_entry_eligible, is_research_eligible
from src.services.yao_scout.decision_policy import allocate_portfolio


def test_current_breadth_does_not_require_inventing_sixty_days_of_emotion_history():
    row = make_primary_evidence({})
    assert row['researchFactors']['E']['score'] is None
    assert evaluate_primary_admission(row)['eligible']
    row['researchFactors']['E']['features']['advancing_pct'] = 16.72
    result = evaluate_primary_admission(row)
    assert result['eligible'] and not result['reasons'] and not result['missingReasons']
    assert result['version'] == 'primary-evidence-20261015-v2'
    assert result['marketRiskState'] == 'weak'
    assert result['warnings'] == ['verified_weak_market_reduced_risk_budget']


def test_verified_weak_market_keeps_stock_gates_and_allocates_only_half_total_budget():
    pool = []
    for index in range(5):
        row = _eligible_candidate()
        row.update(code=f'60000{index}', industry=f'industry{index}', marketRegime={'state': 'normal'})
        row['researchFactors']['E']['features']['advancing_pct'] = 16.72
        row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
        row['primaryAdmission'] = row['precisionDecision']['primaryAdmission']
        assert row['precisionDecision']['profiles']['regular']['entryEligible']
        assert is_research_eligible(row)
        assert not is_entry_eligible(row)  # Evidence/risk admission cannot invent a validated T+1 model.
        assert row['primaryAdmission']['marketRiskState'] == 'weak'
        assert row.get('prediction') is None
        pool.append(row)
    result = allocate_portfolio(pool)
    assert [row['plannedWeight'] for row in result['candidates']] == [.2, .2, .1]
    assert result['policy']['plannedExposure'] == .5
    assert result['policy']['cashWeight'] == .5


@pytest.mark.parametrize('change', ['missing', 'stale', 'future', 'uncovered'])
def test_unknown_or_stale_breadth_still_blocks_formal_entry(change):
    row = _eligible_candidate()
    market = row['researchFactors']['E']
    now = datetime.fromisoformat(row['decision_at'])
    market['features']['advancing_pct'] = 16.72
    if change == 'missing':
        market.pop('source')
    elif change == 'stale':
        market['asOf'] = (now-timedelta(seconds=301)).isoformat()
    elif change == 'future':
        market['asOf'] = (now+timedelta(seconds=1)).isoformat()
    else:
        market['features']['source_coverage'] = .79
    result = evaluate_primary_admission(row)
    assert not result['eligible'] and result['marketRiskState'] == 'unknown'
    assert 'whole_market_breadth_unavailable_or_stale' in result['missingReasons']
    assert result['warnings'] == []
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    assert not is_entry_eligible(row)


@pytest.mark.parametrize('key', ['dataLineage', 'minuteSourceTime', 'factorCutoffAt',
    'factorAvailability', 'fundEvidence', 'sectorEvidence', 'researchFactors', 'decision_at'])
def test_high_score_cannot_override_missing_primary_evidence(key):
    row = _eligible_candidate()
    row['finalScore'] = 100
    row.pop(key)
    result = evaluate_primary_admission(row)
    assert not result['eligible'] and result['missingReasons']
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    assert not any(p['entryEligible'] for p in row['precisionDecision']['profiles'].values())
    assert not is_entry_eligible(row)


@pytest.mark.parametrize('key', ['sector_resonance', 'fund_direction', 'minute_structure', 'relative_volume'])
@pytest.mark.parametrize('breadth', [65, 16.72])
def test_observed_negative_direction_vetoes_all_profiles(key, breadth):
    row = _eligible_candidate()
    row['researchFactors']['E']['features']['advancing_pct'] = breadth
    row['evidenceChecks'][key] = False
    result = evaluate_primary_admission(row)
    assert not result['eligible'] and result['reasons']
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    assert all(p['state'] == 'avoid' for p in row['precisionDecision']['profiles'].values())


def test_model_rejections_are_controls_and_t0_cannot_replace_t1_entry_gate():
    row = _eligible_candidate()
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    row['morningSignal'] = {'selectionEligible': False, 'learningStatus': 'insufficient'}
    assert is_research_eligible(row)
    assert not is_entry_eligible(row)
    row['morningSignal']['selectionEligible'] = True
    assert not is_entry_eligible(row)  # T0 approval has no trading authority.
    from src.services.yao_scout.entry_execution_policy import VERSION
    row['entryExecution'] = {'version': VERSION, 'priceEligible': True}
    row['prediction'] = {'taskId': 'entry_today_exit_next_session', 'status': 'active',
                         'entryEligible': True, 'winProbability': .7,
                         'expectedNetReturn': .02, 'utility': .01}
    row['morningSignal']['selectionEligible'] = False
    assert is_entry_eligible(row)  # Missing T0 direction model cannot veto an eligible T+1 trade.
    row['prediction'] = {'status': 'active', 'entryEligible': False}
    assert not is_entry_eligible(row)


def test_completion_recheck_catches_expiry_and_future_or_different_windows():
    row = make_primary_evidence({})
    now = datetime.fromisoformat(row['decision_at'])
    assert not evaluate_primary_admission(row, now + timedelta(seconds=30))['eligible']
    for key in ('sectorEvidence', 'fundEvidence'):
        broken = deepcopy(row)
        broken[key]['asOf'] = (now - timedelta(minutes=1)).isoformat()
        assert not evaluate_primary_admission(broken)['eligible']
    row['researchFactors']['E']['asOf'] = (now + timedelta(seconds=1)).isoformat()
    assert not evaluate_primary_admission(row)['eligible']


@pytest.mark.parametrize('key', ['minuteSourceTime', 'dataLineage'])
def test_missing_minutes_are_not_misreported_as_observed_price_and_volume_failure(key):
    row = _eligible_candidate()
    row.pop(key)
    row['evidenceChecks'].update(minute_structure=False, relative_volume=False)
    row['v11Inputs'] = {'failed_breakout_risk': 100}
    result = evaluate_primary_admission(row)
    assert not result['eligible'] and result['missingReasons']
    assert 'minute_structure_not_confirmed' not in result['reasons']
    assert 'relative_volume_not_confirmed' not in result['reasons']
    assert 'latest_completed_minute_failed_breakout' not in result['reasons']
