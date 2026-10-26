from copy import deepcopy
from src.services.yao_scout.factor_experiments import (
    annotate_factor_experiments, evaluate_frozen_experiments, experiment_contract, VARIANTS)


def test_ablation_never_changes_live_score_and_does_not_redistribute_missing_weight():
    row = {'selectedScoreBranch': 'early', 'factorScores': {key: 80. for key in 'MVT FWSBCGL'.replace(' ', '')},
           'distributionRisk': 10., 'finalScore': 75., 'marketRegime': {'multiplier': 1.},
           'quote': {'price': 11.}, 'v11Inputs': {'vwap': 10.}}
    before = deepcopy(row)
    annotate_factor_experiments(row)
    assert all(row[key] == value for key, value in before.items())
    assert row['factorExperiments']['selectionAuthority'] is False
    scores = row['factorExperiments']['scores']
    assert scores['without_duplicate_breakout'] < scores['baseline']
    missing = deepcopy(before); missing['factorScores']['T'] = None
    annotate_factor_experiments(missing)
    assert missing['factorExperiments']['scores']['without_turnover_overlap'] is None


def test_holdout_uses_first_signal_and_drops_entire_incomplete_day():
    packet = {**experiment_contract(), 'scores': {key: 50. for key in VARIANTS}}
    def row(day, code, net, clock='10:00'):
        return {'day': day, 'code': code, 'decisionAt': day+'T'+clock+':00+08:00', 'eligible': True,
                'labelEndAt': day+'T15:00:00+08:00', 'labelKnownAt': day+'T15:30:00+08:00',
                'factorExperiments': deepcopy(packet), 'outcomeStatus': 'mature', 'netReturn': net}
    rows = [row('2026-10-22', '1', .01), row('2026-10-23', '1', -.02),
            row('2026-10-23', '1', .5, '11:00'), row('2026-10-26', '1', .03), row('2026-10-26', '2', None)]
    result = evaluate_frozen_experiments(rows, holdout_start='2026-10-23', as_of='2026-10-27T16:00:00+08:00')
    assert result['periods']['holdout']['baseline']['meanDailyNetReturn'] == -.02
    assert result['periods']['holdout']['baseline']['observations'] == 1
    assert result['excludedDays']['incomplete_outcome_cohort'] == 1
    assert result['status'] == 'insufficient_holdout_days'


def test_fund_persistence_requires_verified_deltas_and_no_fill_is_cash():
    row = {'selectedScoreBranch': 'early', 'factorScores': {key: 60. for key in 'MVT FWSBCGL'.replace(' ', '')},
           'distributionRisk': 10., 'finalScore': 58., 'marketRegime': {'multiplier': 1.},
           'v11Inputs': {'main_net_flow_3m': 9.}, 'minuteSourceTime': '2026-10-26T10:00:00+08:00',
           'factorCutoffAt': '2026-10-26T10:00:00+08:00', 'fundEvidence': {'source': 'fixture',
            'asOf': '2026-10-26T10:00:00+08:00', 'metrics': {'main_net_flow_1m_deltas': [10., -2., 1.]}}}
    annotate_factor_experiments(row)
    assert row['factorExperiments']['scores']['fund_flow_persistence'] is not None
    row['fundEvidence']['metrics']['main_net_flow_1m_deltas'] = [10., 1.]
    annotate_factor_experiments(row)
    assert row['factorExperiments']['scores']['fund_flow_persistence'] is None
    packet = {**experiment_contract(), 'scores': {key: 50. for key in VARIANTS}}
    result = evaluate_frozen_experiments([{'day': '2026-10-26', 'code': '1', 'decisionAt': '2026-10-26T10:00:00+08:00',
        'labelEndAt': '2026-10-27T15:00:00+08:00', 'labelKnownAt': '2026-10-27T15:30:00+08:00',
        'eligible': True, 'factorExperiments': packet, 'outcomeStatus': 'no_fill', 'netReturn': 0}],
        holdout_start='2026-10-23', as_of='2026-10-27T16:00:00+08:00')
    assert result['periods']['holdout']['baseline']['meanDailyNetReturn'] == 0


def test_holdout_purges_development_labels_and_refuses_future_results():
    packet = {**experiment_contract(), 'scores': {key: '50.0' for key in VARIANTS}}
    row = {'day': '2026-10-22', 'code': '1', 'decisionAt': '2026-10-22T10:00:00+08:00',
        'labelEndAt': '2026-10-23T15:00:00+08:00', 'labelKnownAt': '2026-10-23T15:30:00+08:00',
        'eligible': True, 'factorExperiments': packet, 'outcomeStatus': 'mature', 'netReturn': .01}
    result = evaluate_frozen_experiments([row], holdout_start='2026-10-23', as_of='2026-10-23T16:00:00+08:00')
    assert result['excludedDays']['development_label_overlaps_holdout'] == 1
    result = evaluate_frozen_experiments([row], holdout_start='2026-10-22', as_of='2026-10-23T15:00:00+08:00')
    assert result['excludedDays']['invalid_or_future_label_timing'] == 1
    result = evaluate_frozen_experiments([row], holdout_start='2026-10-22', as_of='2026-10-23T16:00:00+08:00')
    assert result['periods']['holdout']['baseline']['observations'] == 1
