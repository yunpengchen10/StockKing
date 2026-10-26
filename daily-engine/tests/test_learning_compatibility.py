from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json

import pytest

from src.services.yao_scout.learning_contracts import compatibility, learning_contracts, frozen_learning_fields, task_data_compatible
from src.services.yao_scout.local_algorithm import algorithm_contract
from src.services.yao_scout.signal_learning import _historical_entry_eligibility, SignalLearningService
from src.services.yao_scout import task_learning as learning
from tests.test_signal_learning import _candidate, _clock
from tests.test_signal_learning_contract import artifact, fixture_predictions


def rehash(manifest):
    payload = {key: value for key, value in manifest.items() if key != 'contractId'}
    manifest['contractId'] = 'local-picks-' + sha256(json.dumps(payload, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:20]
    return manifest


def test_only_verified_unrelated_manifest_changes_can_reuse_frozen_eligibility():
    current = algorithm_contract()
    old = deepcopy(current)
    old['dailyPolicy']['version'] = 'older-independent-daily-observation'
    old['learningTaskContracts'] = [learning.task_contract()['taskContractId'], 'unrelated-model-release']
    rehash(old)
    row = {**_candidate('000001', date(2026, 10, 1)), 'algorithmContractId': old['contractId']}
    before = deepcopy(row)
    eligible, audit = _historical_entry_eligibility(row, old)
    assert eligible and audit['reason'] == 'verified_manifest_equivalent_learning_policy'
    assert row == before
    assert not _historical_entry_eligibility({**row, 'evidenceEligible': False}, old)[0]
    assert not _historical_entry_eligibility(row)[0]
    corrupt = deepcopy(old)
    corrupt['weights']['early']['M'] = 999
    assert not _historical_entry_eligibility(row, corrupt)[0]


@pytest.mark.parametrize('field', ['weights', 'primaryAdmissionPolicy', 'entryExecutionPolicy', 'new_unknown_policy'])
def test_feature_admission_execution_and_unknown_changes_invalidate(field):
    current = algorithm_contract()
    old = deepcopy(current)
    old[field] = {'changed': True}
    rehash(old)
    assert not compatibility({'algorithmContractId': old['contractId'], 'algorithmManifest': old}, current)['compatible']


def test_model_and_label_fingerprints_are_independent():
    task = learning.task_contract()
    first = learning_contracts(task)
    changed_model = learning_contracts({**task, 'taskContractId': 'changed-fitted-model-policy'})
    assert first['modelContractId'] != changed_model['modelContractId']
    assert first['learningDataContractId'] == changed_model['learningDataContractId']
    changed_label = learning_contracts({**task, 'labelVersion': 'different-fill-or-exit-label'})
    assert first['labelContractId'] != changed_label['labelContractId']
    assert first['learningDataContractId'] != changed_label['learningDataContractId']


def test_execution_changes_do_not_reset_independent_price_observations():
    current = algorithm_contract()
    old = deepcopy(current)
    old.pop('entryExecutionPolicy')
    rehash(old)
    source = {'algorithmContractId': old['contractId'], 'algorithmManifest': old}
    assert not compatibility(source, current, learning.ENTRY_TASK_ID)['compatible']
    assert compatibility(source, current, learning.MORNING_TASK_ID)['compatible']
    assert compatibility(source, current, learning.NEXT_DAY_TASK_ID)['compatible']


def test_new_frozen_data_contract_survives_own_model_only_change_but_legacy_cannot_guess():
    task = learning.task_contract(learning.MORNING_TASK_ID, '0940')
    signal = {**frozen_learning_fields(task), 'learningTaskContractId': task['taskContractId']}
    changed = {**task, 'taskContractId': 'different-model-parameters'}
    assert task_data_compatible(signal, changed)
    assert not task_data_compatible({'learningTaskContractId': task['taskContractId']}, changed)
    assert not task_data_compatible(signal, {**changed, 'labelVersion': 'different-reference-price'})


def test_verified_manifest_keeps_artifact_usable_after_daily_only_change(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path)
    model = artifact('compatible-model')
    old = deepcopy(algorithm_contract())
    old['dailyPolicy']['version'] = 'previous-daily-policy'
    rehash(old)
    model.update(algorithmContractId=old['contractId'], **learning_contracts(learning.task_contract(), old))
    with service._connect() as db:
        db.execute('INSERT INTO model_versions VALUES(?,?,?,?,?)',
            ('compatible-model', 'active', json.dumps(model), json.dumps({'passed': True}), '2026-10-01T15:30:00+08:00'))
    monkeypatch.setattr(learning, 'lineage_valid', lambda *_: True)
    monkeypatch.setattr(learning, 'predict_model', fixture_predictions)
    assert learning.load_artifact(service, 'compatible-model', True)


def test_old_target_probability_cannot_impersonate_required_win_head(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path)
    model = artifact('old-target-head')
    model.pop('mainProbabilityTarget')
    with service._connect() as db:
        db.execute('INSERT INTO model_versions VALUES(?,?,?,?,?)',
            ('old-target-head', 'active', json.dumps(model), json.dumps({'passed': True}), '2026-10-01T15:30:00+08:00'))
    monkeypatch.setattr(learning, 'lineage_valid', lambda *_: True)
    monkeypatch.setattr(learning, 'predict_model', fixture_predictions)
    assert learning.load_artifact(service, 'old-target-head', True) is None


def test_collection_floor_does_not_claim_splits_ready_and_missing_labels_remain_missing(tmp_path):
    day = date(2026, 10, 1)
    days = sorted((day-timedelta(days=index)).isoformat() for index in range(120))
    rows = [{'day': stamp, 'code': str(code), 'net': .01 if code % 2 else -.01,
             'labelEndAt': stamp+'T15:01:00+08:00', 'labelKnownAt': stamp+'T15:30:00+08:00',
             'dataQualityKnownAt': stamp+'T15:30:00+08:00'} for stamp in days for code in range(10)]
    learner = learning.TaskLearner(SignalLearningService(tmp_path), learning.MORNING_TASK_ID, '0940')
    now = _clock(day, 15, 30)
    snap = learner._readiness_snapshot(now, (rows, days, True))
    state = learner._readiness_report(now, {}, snapshot=snap)
    assert state['status'] == 'split_not_ready'
    split = state['progress']['trainingSplits']
    assert not split['ready'] and split['additionalSignalDaysForAnyTraining'] == 36
    assert split['folds'][0]['trainSamplesMissing'] == 300
    assert len(split['folds']) == 4


def test_split_readiness_checks_each_fold_known_at_not_just_total_samples():
    day = date(2026, 10, 1)
    days = sorted((day-timedelta(days=index)).isoformat() for index in range(190))
    rows = [{'day': stamp, 'code': str(code), 'net': .08 if code % 2 else -.01,
             'labelEndAt': stamp+'T15:01:00+08:00', 'labelKnownAt': stamp+'T15:30:00+08:00',
             'dataQualityKnownAt': stamp+'T15:30:00+08:00'} for stamp in days for code in range(10)]
    assert learning.training_split_readiness(rows, days, learning.ENTRY_TASK_ID)['ready']
    for row in rows:
        row['dataQualityKnownAt'] = '2099-01-01T00:00:00+08:00'
    blocked = learning.training_split_readiness(rows, days, learning.ENTRY_TASK_ID)
    assert not blocked['ready']
    assert blocked['folds'][0]['trainSamples'] == 0
