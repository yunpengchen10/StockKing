from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from tests.primary_fixture import make_primary_evidence
from src.services.yao_scout.primary_policy import evaluate_primary_admission
from tests.test_local_algorithm import _eligible_candidate, CONTEXT, DAILY
from src.services.yao_scout.precision_policy import evaluate
from src.services.yao_scout.local_algorithm import is_entry_eligible, is_research_eligible


def test_current_breadth_does_not_require_inventing_sixty_days_of_emotion_history():
    row = make_primary_evidence({})
    assert row['researchFactors']['E']['score'] is None
    assert evaluate_primary_admission(row)['eligible']
    row['researchFactors']['E']['features']['advancing_pct'] = 40
    result = evaluate_primary_admission(row)
    assert not result['eligible']
    assert result['reasons'] == ['weak_market_primary_entries_paused']


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
def test_observed_negative_direction_vetoes_all_profiles(key):
    row = _eligible_candidate()
    row['evidenceChecks'][key] = False
    result = evaluate_primary_admission(row)
    assert not result['eligible'] and result['reasons']
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    assert all(p['state'] == 'avoid' for p in row['precisionDecision']['profiles'].values())


def test_model_rejections_are_controls_and_missing_morning_model_cannot_publish():
    row = _eligible_candidate()
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    row['morningSignal'] = {'selectionEligible': False, 'learningStatus': 'insufficient'}
    assert is_research_eligible(row)
    assert not is_entry_eligible(row)
    row['morningSignal']['selectionEligible'] = True
    assert is_entry_eligible(row)
    row['prediction'] = {'status': 'active', 'entryEligible': False}
    assert not is_entry_eligible(row)  # T0 approval never overrides the T+1 gate.


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
