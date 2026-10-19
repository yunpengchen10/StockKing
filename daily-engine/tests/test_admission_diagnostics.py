from copy import deepcopy

from src.services.yao_scout.admission_diagnostics import build_admission_diagnostics
from src.services.yao_scout.precision_policy import evaluate
from tests.test_local_algorithm import _eligible_candidate, CONTEXT, DAILY


def ready(code='600000'):
    row = _eligible_candidate()
    row['code'] = code
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    row['primaryAdmission'] = row['precisionDecision']['primaryAdmission']
    return row


def test_data_outage_is_distinct_from_adverse_market_and_counts_overlap():
    row = ready()
    row['baselineHistoryDays'] = 19
    row['dataLineage'] = {'status': 'unknown'}
    row['primaryAdmission']['missingReasons'] = [
        'minute_lineage_not_verified', 'S_source_missing_stale_or_misaligned']
    frozen = deepcopy(row)
    result = build_admission_diagnostics([row, row], [], {})
    assert result['status'] == 'data_incomplete'
    assert result['total'] == 1
    assert {x['key'] for x in result['checks'] if x['blocked']} == {'history', 'lineage', 'sector'}
    assert result['riskReasonCounts'] == {}
    assert row == frozen


def test_complete_data_can_fail_only_the_morning_model_without_false_source_gaps():
    row = ready()
    row['morningSignal'] = {'selectionEligible': False, 'learningStatus': 'rules_unvalidated'}
    result = build_admission_diagnostics([row], [], {}, morning=True)
    assert result['status'] == 'model_not_ready'
    assert result['evidenceAndRiskPassed'] == 1
    assert result['morningModelBlocked'] == 1
    assert all(x['blocked'] == 0 for x in result['checks'])
    assert '主要阻断' in result['summary']


def test_negative_direction_is_not_reported_as_missing_data():
    row = ready()
    row['evidenceChecks']['sector_resonance'] = False
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    row['primaryAdmission'] = row['precisionDecision']['primaryAdmission']
    result = build_admission_diagnostics([row], [], {})
    assert result['status'] == 'conditions_not_met'
    assert result['riskReasonCounts']['sector_resonance_not_confirmed'] == 1
    assert result['missingReasonCounts'] == {}
    assert '方向、价格或风险条件未满足' in result['summary']


def test_active_model_rejection_is_not_missing_model():
    row = ready()
    row['morningSignal'] = {'selectionEligible': False, 'learningStatus': 'active'}
    result = build_admission_diagnostics([row], [], {}, morning=True)
    assert result['status'] == 'model_risk_rejected'
    assert result['morningModelUnavailable'] == 0


def test_missing_active_classification_remains_a_data_failure():
    row = ready()
    row['quote'].pop('active_flow_source')
    row['precisionDecision'] = evaluate(row, CONTEXT, DAILY)
    row['primaryAdmission'] = row['precisionDecision']['primaryAdmission']
    result = build_admission_diagnostics([row], [], {})
    assert result['status'] == 'data_incomplete'
    assert '主动资金分类未通过1/1' in result['summary']
