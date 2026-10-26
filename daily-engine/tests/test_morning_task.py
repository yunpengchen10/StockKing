"""Independent T0 labels, prospective cohorts and fail-closed model publication."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json

import numpy as np
import pytest

from src.services.yao_scout import morning_task as morning
from src.services.yao_scout import task_learning as learning
from src.services.yao_scout.local_algorithm import algorithm_contract
from src.services.yao_scout.signal_learning import SignalLearningService, TZ
from tests.test_signal_learning import _candidate, _clock, _bars, verified_ledger_quality
from tests.test_task_learning import synthetic

DAY = date(2026, 10, 15)


def signal(published=None):
    published = published or _clock(DAY)
    candidate = _candidate('000001', DAY)
    queue = morning.morning_queue('live', published)
    candidate['morningSignal'] = {'version': morning.MORNING_VERSION, 'queue': queue,
        'learningTaskContractId': learning.task_contract(morning.MORNING_TASK_ID, queue)['taskContractId'],
        'researchEligibleAtDecision': True}
    return morning.freeze_morning_signal(candidate, published, 'live',
        {'eligible': True, 'qualityKnownAt': published.isoformat()}, selected=True, official=False)


@pytest.mark.parametrize('slot,hour,minute,expected', [
    ('live', 9, 33, 'opening'), ('live', 9, 40, '0940'), ('live', 9, 55, '0955'),
    ('live', 10, 30, '1030'), ('live', 11, 29, '1030'), ('live', 11, 30, None),
    ('0920', 9, 33, None), ('1455', 14, 55, None), ('0940', 10, 40, '1030'),
    ('1030', 9, 33, 'opening')])
def test_morning_queue_preserves_clock_strata(slot, hour, minute, expected):
    assert morning.morning_queue(slot, _clock(DAY, hour, minute)) == expected


def test_full_minute_after_publication_then_same_day_close_and_mae():
    frozen = signal()
    assert frozen['observationStartAt'] == _clock(DAY, 9, 41, 0).isoformat()
    assert frozen['observationMinuteEndAt'] == _clock(DAY, 9, 42, 0).isoformat()
    bars = _bars([DAY])
    # Before-publication prices never become an unusually favorable reference.
    bars[0]['low'] = .01
    outcome = morning.morning_outcome(frozen, bars, _clock(DAY, 15, 1), calendar_provider=lambda a, b: [DAY])
    assert outcome['status'] == 'mature_price_observation'
    assert outcome['referencePrice'] == 10.
    assert outcome['observedReturn'] == pytest.approx(.005)
    assert outcome['observedMae'] == pytest.approx(.01)
    assert outcome['nextCompleteMinutePrice'] == 10.
    assert outcome['referenceSourceTime'] == frozen['referenceSourceTime']
    assert outcome['up'] is True and outcome['executionVerified'] is False
    assert morning.morning_outcome(frozen, bars, _clock(DAY, 15, 0), calendar_provider=lambda a, b: [DAY])['status'] == 'pending'


def test_recommendation_loses_even_if_next_complete_minute_proxy_gains():
    frozen, bars = signal(), _bars([DAY])
    assert frozen['referencePrice'] == 10.
    for row in bars:
        row.update(open=9., high=9.6, low=8.9, close=9.5)
    outcome = morning.morning_outcome(frozen, bars, _clock(DAY, 15, 1), calendar_provider=lambda a, b: [DAY])
    assert outcome['status'] == 'mature_price_observation'
    assert outcome['referencePrice'] == frozen['firstReferencePrice'] == 10.
    assert outcome['observedReturn'] == pytest.approx(-.05) and outcome['up'] is False
    assert outcome['observedMae'] == pytest.approx(.11)
    assert outcome['nextCompleteMinutePrice'] == 9.
    assert outcome['nextCompleteMinuteReturn'] == pytest.approx(9.5/9-1)
    assert learning.class_target({'net': outcome['observedReturn']}, morning.MORNING_TASK_ID) == 0


@pytest.mark.parametrize('failure', ['missing_price', 'missing_quote_price', 'mismatched_quote',
                                     'missing_source', 'future_source', 'stale_source'])
def test_unknown_recommendation_quote_cannot_receive_a_label_or_training_admission(failure):
    published = _clock(DAY)
    candidate = _candidate('000001', DAY)
    candidate['morningSignal'] = {'version': morning.MORNING_VERSION, 'queue': '0940',
        'learningTaskContractId': learning.task_contract(morning.MORNING_TASK_ID, '0940')['taskContractId'],
        'researchEligibleAtDecision': True}
    if failure == 'missing_price':
        candidate.pop('referencePrice')
        candidate.pop('signalPrice')
    elif failure == 'missing_quote_price':
        candidate['quote'].pop('price')
    elif failure == 'mismatched_quote':
        candidate['quote']['price'] = 9.
    elif failure == 'missing_source':
        candidate['quote'].pop('source_time')
    else:
        candidate['quote']['source_time'] = (published + timedelta(seconds=1) if failure == 'future_source'
                                             else published - timedelta(seconds=31)).isoformat()
    frozen = morning.freeze_morning_signal(candidate, published, '0940', {'eligible': True}, selected=False, official=True)
    assert not frozen['trainingEligibleAtSignal'] and not frozen['referenceVerifiedAtPublication']
    outcome = morning.morning_outcome(frozen, _bars([DAY]), _clock(DAY, 15, 1), calendar_provider=lambda a, b: [DAY])
    assert outcome['status'] == 'unverified' and outcome['observedReturn'] is None and outcome['up'] is None


@pytest.mark.parametrize('failure', ['gap', 'conflict', 'no_volume', 'tainted', 'calendar', 'window'])
def test_unknown_price_path_never_turns_into_success_or_failure_label(failure):
    frozen, bars = signal(), _bars([DAY])
    calendar = lambda a, b: [DAY]
    index = next(i for i, row in enumerate(bars) if row['end'] == frozen['observationMinuteEndAt'])
    if failure == 'gap':
        bars.pop(index)
    elif failure == 'conflict':
        bars.append({**bars[index], 'close': 10.01})
    elif failure == 'no_volume':
        bars[index]['volume_shares'] = 0
    elif failure == 'tainted':
        bars[index]['data_quality_status'] = 'tainted'
    elif failure == 'calendar':
        calendar = lambda a, b: []
    else:
        frozen['observationStartAt'] = _clock(DAY, 9, 40, 0).isoformat()
    outcome = morning.morning_outcome(frozen, bars, _clock(DAY, 15, 30), calendar_provider=calendar)
    assert outcome['status'] in {'incomplete', 'unverified'}
    assert outcome['observedReturn'] is None and outcome['up'] is None


def test_first_reference_and_earlier_missing_outcome_survive_later_better_scan():
    first = {'morningSignal': {'firstReferenceTime': _clock(DAY).isoformat(), 'firstReferencePrice': 20.}}
    later = _candidate('000001', DAY, price=18)
    state = morning.morning_signal_state(later, first, selected=True)
    assert state['firstReferencePrice'] == 20. and state['currentSignalState'] == 'continuing'
    assert morning.morning_signal_state(later, first, selected=False)['currentSignalState'] == 'invalidated'
    records = [{'code': '000001', 'publishedAt': _clock(DAY).isoformat(), 'selected': True,
                'version': morning.MORNING_VERSION, 'queue': '0940', 'status': 'incomplete'},
               {'code': '000001', 'publishedAt': _clock(DAY, 10, 30).isoformat(), 'selected': True,
                'version': morning.MORNING_VERSION, 'queue': '1030', 'status': 'mature_price_observation',
                'observedReturn': .1, 'observedMae': .01, 'up': True, 'largeDrop': False}]
    summary = morning.summarize_morning_records(records)
    assert summary['selected']['signalCount'] == 1
    assert summary['selected']['matureCount'] == 0
    assert summary['byQueue']['1030']['selected']['upCount'] == 1


def test_cold_start_never_recommends_but_freezes_rule_comparator_and_controls(tmp_path):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    rows = service.rank_morning_candidates([_candidate('000001', DAY), _candidate('000002', DAY)], _clock(DAY), slot='0940')
    assert all(row['morningSignal']['selectionEligible'] is False for row in rows)
    assert all(row['morningSignal']['researchEligibleAtDecision'] is True for row in rows)
    assert all(row['morningSignal']['upProbability'] is None for row in rows)
    assert all(row['morningSignal']['ruleBaselineSelected'] is True for row in rows)
    run = {'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'completed_observations',
           'candidates': [], 'controls': rows, 'morningResearch': rows}
    saved = service.persist_scan(run, '0940', _clock(DAY), True)
    assert all(row['morningSignal']['trainingEligibleAtSignal'] for row in saved['controls'])
    assert all(row['firstReferencePrice'] is None for row in saved['controls'])
    with service._connect() as db:
        assert db.execute('SELECT count(*) FROM signal_events').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM model_versions').fetchone()[0] == 0
    replay = service.persist_scan({**run, 'controls': []}, '0940', _clock(DAY), True)
    assert replay['learningLedger']['idempotentReplay']
    assert replay['controls'] == saved['controls']


def test_failed_preadmission_after_first_selection_is_explicit_invalidation(tmp_path):
    at = [_clock(DAY)]
    service = SignalLearningService(tmp_path, clock=lambda: at[0])
    row = service.rank_morning_candidates([_candidate('000001', DAY)], at[0], slot='0940')[0]
    # Fixture models an externally finalized selection; production filters it.
    row['rank'] = 1
    first = service.persist_scan({'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'completed_observations',
                                 'candidates': [row]}, '0940', at[0], True)
    original = first['candidates'][0]
    at[0] = _clock(DAY, 9, 55)
    rejected = _candidate('000001', DAY, price=9)
    rejected.update(status='evidence_insufficient', decision_at=at[0].isoformat(), evidenceEligible=False)
    later = service.persist_scan({'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'no_candidates_with_coverage_limits',
                                  'candidates': [], 'controls': [rejected]}, '0955', at[0], True)
    invalidated = later['invalidatedCandidates'][0]
    assert invalidated['firstReferencePrice'] == original['firstReferencePrice'] == 10
    assert invalidated['currentSignalState'] == 'invalidated'
    assert invalidated['morningSignal']['version'] == morning.MORNING_VERSION
    assert invalidated['morningSignal']['trainingEligibleAtSignal'] is False


def test_labels_review_same_day_without_rewriting_legacy_and_keep_cohort_incomplete(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=lambda a, b: [DAY] if a <= DAY <= b else [], bar_fetcher=lambda code, at: _bars([DAY]))
    rows = service.rank_morning_candidates([_candidate('000001', DAY), _candidate('000002', DAY)], _clock(DAY), slot='0940')
    run = {'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'completed_observations', 'candidates': [], 'controls': rows}
    service.persist_scan(run, '0940', _clock(DAY), True)
    service.persist_scan({'status': 'completed_observations', 'candidates': [_candidate('000003', DAY)]},
                         'live', _clock(DAY), False)
    monkeypatch.setattr(learning, 'lineage_valid', lambda *args: True)
    monkeypatch.setattr(learning, 'lineage_known_at', lambda *args: _clock(DAY, 15, 1).isoformat())
    monkeypatch.setattr(service, '_label_lineage', lambda *args, **kw: {'status': 'verified', 'sessions': []})
    reviewed = morning.review_morning_signals(service, _clock(DAY, 15, 30))
    assert reviewed['updated'] == 2
    assert all(row['status'] == 'mature_price_observation' for row in reviewed['items'])
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    samples, days, complete = learner.data(_clock(DAY, 15, 45))
    assert complete and len(samples) == 2 and days == [DAY.isoformat()]
    assert all(not row['selected'] and row['baselineSelected'] for row in samples)
    with service._connect() as db:
        db.execute('DELETE FROM morning_outcomes WHERE signal_id=?', (samples[0]['signalId'],))
        legacy = json.loads(db.execute("SELECT snapshot_json FROM signal_events WHERE code='000003'").fetchone()[0])
    assert 'morningSignal' not in legacy
    samples, days, complete = learner.data(_clock(DAY, 15, 45))
    assert len(samples) == 1 and days == [DAY.isoformat()] and not complete
    assert learner.train(_clock(DAY, 15, 45))['lastFitCompletedAt'] is None


def test_real_morning_heads_fit_return_up_event_and_adverse_excursion_independently():
    rows = synthetic()
    for row in rows:
        row['observedMae'] = max(.001, .02-.018*row['features']['x'])
    artifact = learning.fit_model(rows[:720], rows[840:1080], morning.MORNING_TASK_ID, '0940')
    prediction = learning.predict_model(json.loads(json.dumps(artifact)), rows[1080:])
    assert artifact['targetHorizon'] == 0 and artifact['sameDayExitAllowed'] is False
    assert artifact['taskContractId'] != learning.task_contract()['taskContractId']
    assert artifact['taskContractId'] != learning.task_contract(morning.MORNING_TASK_ID, '1030')['taskContractId']
    assert 'maeRegressor' in artifact and np.all(prediction['expectedMae'] >= 0)
    assert np.allclose(prediction['utility'], prediction['net']-np.maximum(-prediction['tail'], prediction['expectedMae']))
    assert prediction['eligible'].any() and (~prediction['eligible']).any()
    assert np.all(prediction['probability'][prediction['eligible']] > .5)
    low_probability = learning.predict_model({**artifact, 'calibrationCoef': 0., 'calibrationIntercept': -2.}, rows[1080:])
    assert (low_probability['utility'] > 0).any() and not low_probability['eligible'].any()
    assert learning.class_target({'net': .001}, morning.MORNING_TASK_ID) == 1
    assert learning.class_target({'net': .001}, learning.ENTRY_TASK_ID) == 1
    assert learning.task_contract()['classMeaning'] == 'net_return_strictly_positive'
    metric = learning.diagnostics(rows[1080:], prediction, artifact)
    assert metric['maePredictionAbsoluteError'] >= 0 and metric['brierSkill'] > 0


def test_morning_model_activation_uses_own_artifact_and_rejection_keeps_controls(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    monkeypatch.setattr(learning.TaskLearner, 'artifact', lambda self, *a, **k: {'version': 'morning-test'})
    monkeypatch.setattr(learning, 'lineage_valid', lambda *a: True)
    monkeypatch.setattr(learning, 'predict_model', lambda artifact, rows: {
        'rank': np.array([80., 0.]), 'net': np.array([.04, -.03]), 'probability': np.array([.8, .2]),
        'tail': np.array([-.01, -.06]), 'expectedMae': np.array([.01, .06]),
        'utility': np.array([.03, -.09]), 'eligible': np.array([True, False])})
    rows = service.rank_morning_candidates([_candidate('000001', DAY), _candidate('000002', DAY)], _clock(DAY), slot='0940')
    assert rows[0]['morningSignal']['selectionEligible'] is True
    assert rows[1]['morningSignal']['selectionEligible'] is False
    assert all(row['morningSignal']['researchEligibleAtDecision'] for row in rows)
    assert rows[0]['firstReferencePrice'] is None  # Not published yet.


def test_missing_or_unverified_data_cannot_become_morning_training_eligible(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    monkeypatch.setattr(service, '_learning_quality', lambda *a: {'eligible': False, 'qualityKnownAt': None})
    row = service.rank_morning_candidates([_candidate('000001', DAY)], _clock(DAY), slot='0940')[0]
    assert row['morningSignal']['researchEligibleAtDecision'] is False
    assert row['morningSignal']['upProbability'] is None
    saved = service.persist_scan({'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'completed_observations',
                                  'candidates': [], 'controls': [row]}, '0940', _clock(DAY), True)
    assert saved['controls'][0]['morningSignal']['trainingEligibleAtSignal'] is False


def test_publication_rechecks_factor_ttl_even_with_fresh_quote():
    from src.services.yao_scout.signal_learning import _signal_preflight
    from tests.primary_fixture import make_primary_evidence
    row = _candidate('000001', DAY)
    # Decision snapshot is valid. Its minute/S/F/E evidence hits 300 seconds
    # during publication although the final quote/classification was refreshed.
    publish = _clock(DAY, 9, 45, 0)
    decision = publish-timedelta(seconds=20)
    row['decision_at'] = decision.isoformat()
    row = make_primary_evidence(row)
    minute = _clock(DAY, 9, 40, 0).isoformat()
    row['minuteSourceTime'] = minute
    row['sectorEvidence']['asOf'] = row['fundEvidence']['asOf'] = row['researchFactors']['E']['asOf'] = minute
    row['quote'].update(source_time=decision.isoformat(), active_flow_as_of=decision.isoformat())
    assert not _signal_preflight(row, publish)
    assert _signal_preflight(row, publish-timedelta(microseconds=1))


def test_positive_return_advantage_cannot_hide_worse_adverse_excursions(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    days = [(DAY+timedelta(days=i)).isoformat() for i in range(60)]
    rows = []
    for day in days:
        for index in range(10):
            winner = index < 5
            rows.append({'day': day, 'code': f'{index:06}', 'runId': day,
                'decisionAt': day+'T09:40:30+08:00', 'net': .05 if winner else .01,
                'observedMae': .15 if winner else .01,
                'selected': False, 'baselineSelected': not winner, 'baselineRank': index})
    model = np.array([90 if index % 10 < 5 else 0 for index in range(len(rows))])
    prediction = {'rank': model, 'baselineRank': np.where(model == 0, 90, 0)}
    monkeypatch.setattr(learning, 'diagnostics', lambda *a: {'brierSkill': .5, 'positiveEvents': 300})
    gate = learner.gate(rows, prediction, {}, days, True)
    assert gate['meanPriceReturn95Lower'] > 0 and gate['priceReturnAdvantage95Lower'] > 0
    assert gate['ridgeLogisticPriceAdvantage95Lower'] > 0
    assert gate['downsideNotWorse'] is False and gate['passed'] is False


def test_a_few_large_winners_with_low_up_accuracy_cannot_pass_morning_gate(tmp_path, monkeypatch):
    learner = learning.TaskLearner(SignalLearningService(tmp_path), morning.MORNING_TASK_ID, '0940')
    days = [(DAY+timedelta(days=i)).isoformat() for i in range(60)]
    rows = []
    for day in days:
        for index in range(10):
            rows.append({'day': day, 'code': f'{index:06}', 'runId': day,
                'decisionAt': day+'T09:40:30+08:00',
                'net': .15 if index < 2 else -.01 if index < 5 else .005,
                'observedMae': .01, 'selected': False,
                'baselineSelected': index >= 5, 'baselineRank': index})
    model = np.array([90 if index % 10 < 5 else 0 for index in range(len(rows))])
    prediction = {'rank': model, 'baselineRank': np.where(model == 0, 90, 0)}
    monkeypatch.setattr(learning, 'diagnostics', lambda *a: {'brierSkill': .5, 'positiveEvents': 420})
    gate = learner.gate(rows, prediction, {}, days, True)
    assert gate['meanPriceReturn95Lower'] > 0 and gate['priceReturnAdvantage95Lower'] > 0
    assert gate['downsideNotWorse'] is True
    assert gate['upAccuracyComparison']['model']['upRate'] == pytest.approx(.4)
    assert gate['selectedUpRate95Lower'] == pytest.approx(.4)
    assert gate['upAccuracyPassed'] is False and gate['passed'] is False


def test_forward_prediction_must_precede_fixed_reference_window(tmp_path):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    row = service.rank_morning_candidates([_candidate('000001', DAY)], _clock(DAY), slot='0940')[0]
    service.persist_scan({'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'completed_observations',
                          'candidates': [], 'controls': [row]}, '0940', _clock(DAY), True)
    with service._connect() as db:
        event = db.execute('SELECT signal_id,snapshot_json FROM signal_events').fetchone()
        frozen = json.loads(event['snapshot_json'])['morningSignal']
        db.execute('INSERT INTO forward_predictions VALUES(?,?,?,?)',
                   (event['signal_id'], 'future-model', 80., frozen['observationStartAt']))
    sample = {'signalId': event['signal_id'], 'decisionAt': row['decision_at'],
              'publishedAt': frozen['publishedAt'], 'observationStartAt': frozen['observationStartAt']}
    assert service._forward_scores('future-model', [sample]) is None


def test_morning_scan_completing_after_window_fails_closed_with_explicit_signal(tmp_path):
    now = _clock(DAY, 11, 30)
    service = SignalLearningService(tmp_path, clock=lambda: now)
    rows = service.rank_morning_candidates([_candidate('000001', DAY)], now, slot='live')
    signal = rows[0]['morningSignal']
    assert signal['learningStatus'] == 'morning_window_closed' and signal['selectionEligible'] is False
    saved = service.persist_scan({'scoreVersion': algorithm_contract()['scoreVersion'], 'status': 'completed_observations',
                                  'candidates': [], 'controls': rows}, 'live', now, False)
    frozen = saved['controls'][0]['morningSignal']
    assert frozen['publicationValid'] is False and frozen['trainingEligibleAtSignal'] is False
    assert frozen['observationStartAt'] is None and frozen['upProbability'] is None
