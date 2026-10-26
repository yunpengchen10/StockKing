"""Cold-start visibility and prospective controls do not grant trading rights."""
from copy import deepcopy
from contextlib import closing
from datetime import date, timedelta
import json
import sqlite3

import pytest

from src.services.yao_scout import primary_readiness as readiness
from src.services.yao_scout import signal_learning as ledger
from src.services.yao_scout import task_learning
from src.services.yao_scout.entry_execution_policy import VERSION
from src.services.yao_scout.local_algorithm import algorithm_contract, is_entry_eligible
from tests.test_signal_learning import _candidate, _clock, _bars

DAY = date(2026, 9, 7)


def control():
    row = _candidate('000001', DAY)
    row['prediction'] = {'taskId': readiness.TASK_ID, 'status': 'rules_unvalidated',
                         'entryEligible': None, 'winProbability': None}
    row['entryExecution'] = {'version': VERSION, 'state': 'ready', 'priceEligible': True,
        'triggerAt': _clock(DAY).isoformat(), 'expiresAt': _clock(DAY, 9, 45).isoformat(),
        'minimumPrice': 9.95, 'maximumPrice': 10.15, 'invalidationPrice': 9.5}
    return row


def test_readiness_missing_db_does_not_create_state_or_invent_counts(tmp_path, monkeypatch):
    missing = tmp_path / 'not-created'
    monkeypatch.setattr(ledger.SignalLearningService, '__init__', lambda *_args, **_kwargs: pytest.fail('service creation'))
    result = readiness.read_primary_readiness(missing, _clock(DAY), candidates=[control()])
    assert not missing.exists()
    state = result['primaryReadiness']
    assert state['progressKnown'] is False and state['progress']['validSamples'] is None
    assert state['minimumSignalDaysForAnyTraining'] == 156
    assert state['modelUsableNow'] is False
    assert 'primary_registry_missing' in state['blockerCodes']
    assert result['coldStartResearch']['recordingMode'] == 'manual_audit_only'
    assert result['coldStartResearch']['trainingEligibleCount'] is None
    assert result['coldStartResearch']['coldStartControlCount'] == 1
    assert not result['coldStartResearch']['items'][0]['formalRecommendation']


def test_readiness_queries_only_registry_in_readonly_mode(tmp_path, monkeypatch):
    service = ledger.SignalLearningService(tmp_path)
    learner = task_learning.TaskLearner(service)
    now = _clock(DAY)
    payload = {'taskContractId': learner.contract['taskContractId'],
        'algorithmContractId': algorithm_contract()['contractId'],
        'readinessSnapshot': learner._readiness_snapshot(now, ([], [], True)),
        'lastLearningCheckAt': now.isoformat()}
    learner.save_registry(payload, now)
    original_connect = sqlite3.connect
    queries = []
    def readonly_connect(path, *args, **kwargs):
        assert '?mode=ro' in str(path) and kwargs['uri'] is True
        db = original_connect(path, *args, **kwargs)
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(readiness.sqlite3, 'connect', readonly_connect)
    monkeypatch.setattr(task_learning.TaskLearner, 'data', lambda *_: pytest.fail('cohort scan'))
    monkeypatch.setattr(task_learning.TaskLearner, 'artifact', lambda *_: pytest.fail('model load'))
    result = readiness.read_primary_readiness(tmp_path, now, candidates=[control()], slot='0940', official=True)
    assert len(queries) == 1 and 'FROM learning_registry WHERE registry_key=' in queries[0]
    state = result['primaryReadiness']
    assert state['progressKnown'] is True and state['progress']['validSamples'] == 0
    assert state['countsAsOf'] == now.isoformat() and state['lastLearningCheckAt'] == now.isoformat()
    assert state['lastFitCompletedAt'] is None and state['selectionAuthority'] is False
    assert result['coldStartResearch']['recordingMode'] == 'official_pending_publication'


def test_readiness_closes_connection_without_waiting_for_garbage_collection(tmp_path, monkeypatch):
    path = tmp_path / 'signal-learning-v1.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE learning_registry(registry_key TEXT PRIMARY KEY, payload_json TEXT)')
        db.commit()
    original_connect, retained = sqlite3.connect, []
    def tracked_connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        retained.append(db)
        return db
    monkeypatch.setattr(readiness.sqlite3, 'connect', tracked_connect)
    readiness.read_primary_readiness(tmp_path, _clock(DAY))
    assert len(retained) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        retained[0].execute('SELECT 1')
    path.unlink()  # Windows must release the file even while refs are retained.


def test_contract_change_does_not_relabel_old_sample_counts(tmp_path):
    service = ledger.SignalLearningService(tmp_path)
    learner = task_learning.TaskLearner(service)
    now = _clock(DAY)
    snapshot = learner._readiness_snapshot(now, ([], [], False))
    snapshot.update(taskContractId='old-task', validSamples=9000, matureDays=500)
    learner.save_registry({'readinessSnapshot': snapshot}, now)
    state = readiness.read_primary_readiness(tmp_path, now)['primaryReadiness']
    assert not state['progressKnown'] and state['progress']['validSamples'] is None
    assert 'cohort_contract_changed' in state['blockerCodes']


def test_cold_research_distinguishes_evidence_plan_and_manual_audit():
    good = control()
    bad = deepcopy(good)
    bad.update(code='000002', evidenceEligible=False)
    expired = deepcopy(good)
    expired['code'] = '000003'
    expired['entryExecution'].update(priceEligible=False, state='entry_plan_expired')
    before = deepcopy([good, bad, expired])
    result = readiness.cold_start_research([good, bad, expired], slot='live', official=False)
    assert [row['code'] for row in result['items']] == ['000001']
    assert result['evidenceEligibleCount'] == 2 and result['entryPlanReadyCount'] == 1
    assert result['trainingEligibleCount'] is None and result['recordingMode'] == 'manual_audit_only'
    assert [good, bad, expired] == before
    assert not is_entry_eligible(good)


def test_official_unselected_control_enters_pool_and_daily_copy_cannot_erase_it(tmp_path, monkeypatch):
    days = [DAY + timedelta(days=i) for i in range(6)]
    service = ledger.SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=lambda a, b: [d for d in days if a <= d <= b], bar_fetcher=lambda *_: _bars(days))
    # Isolate minute-archive validation; all publication and research gates run.
    monkeypatch.setattr(service, '_learning_quality', lambda *_: {'eligible': True,
        'qualityKnownAt': _clock(DAY).isoformat(), 'reason': 'verified_at_publication'})
    row = control()
    daily = {'code': row['code'], 'name': row['name'], 'observationOnly': True,
        'dailySignal': {'version': 'king-daily-primary-v4', 'taskId': 'daily_structure_to_next_session'},
        'referencePrice': 10, 'quote': deepcopy(row['quote'])}
    source = {'status': 'completed_observations', 'scoreVersion': algorithm_contract()['scoreVersion'],
        'candidates': [], 'controls': [row], 'precisionResearch': [row],
        'dailyCandidates': [daily], 'dailyProfileCandidates': {'regular': [daily]}}
    saved = service.persist_scan(source, '0940', _clock(DAY), True)
    assert saved['learningLedger']['trainingEligibleCount'] == 1
    assert saved['learningLedger']['trainingControlCount'] == 1
    assert saved['learningLedger']['trainingSelectedCount'] == 0
    assert saved['controls'][0]['trainingEligible'] is True
    assert saved['controls'][0]['learningSampleEligibility']['modelRequired'] is False
    assert saved['coldStartResearch']['trainingEligibleCount'] == 1
    assert saved['coldStartResearch']['recordingMode'] == 'official_frozen'
    replay = service.persist_scan(source, '0940', _clock(DAY), True)
    assert replay['coldStartResearch'] == saved['coldStartResearch']
    assert not is_entry_eligible(saved['controls'][0])
    with service._connect() as db:
        event = db.execute('SELECT selected,training_eligible,snapshot_json FROM signal_events').fetchone()
        frozen = json.loads(event['snapshot_json'])
        assert event['selected'] == 0 and event['training_eligible'] == 1
        assert 'dailySignal' not in frozen and frozen['prediction']['winProbability'] is None
        assert db.execute('SELECT COUNT(*) FROM daily_observation_signals').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM model_versions').fetchone()[0] == 0
    service.review_due(_clock(days[-1], 15, 30))
    samples = service._dataset(_clock(days[-1], 15, 30))
    assert len(samples) == 1 and samples[0]['selected'] is False
    assert samples[0]['filled'] is True


def test_manual_and_replayed_official_records_never_upgrade_original_eligibility(tmp_path, monkeypatch):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    verified = [False]
    monkeypatch.setattr(service, '_learning_quality', lambda *_: {'eligible': verified[0],
        'qualityKnownAt': _clock(DAY).isoformat() if verified[0] else None})
    source = {'status': 'completed_observations', 'scoreVersion': algorithm_contract()['scoreVersion'],
              'candidates': [], 'controls': [control()]}
    original = service.persist_scan(source, '0940', _clock(DAY), True)
    verified[0] = True
    replay = service.persist_scan(source, '0940', _clock(DAY), True)
    assert replay['learningLedger']['idempotentReplay'] is True
    assert replay['learningLedger']['trainingEligibleCount'] == 0
    assert replay['controls'][0]['learningSampleEligibility'] == original['controls'][0]['learningSampleEligibility']
    assert 'lineage_not_verified_at_publication' in replay['controls'][0]['learningSampleEligibility']['blockerCodes']
    manual = service.persist_scan({**source, 'run_id': 'manual'}, 'live', _clock(DAY), False)
    assert manual['learningLedger']['trainingEligibleCount'] == 0
    assert 'manual_audit_only' in manual['controls'][0]['learningSampleEligibility']['blockerCodes']
    assert manual['controls'][0]['learningSampleEligibility']['eligible'] is False
    assert original['learningLedger']['trainingEligible'] is False


def test_empty_official_replay_preserves_known_zero_without_a_ledger_scan(tmp_path):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    source = {'status': 'completed_observations', 'candidates': [],
              'scoreVersion': algorithm_contract()['scoreVersion']}
    first = service.persist_scan(source, '0940', _clock(DAY), True)
    replay = service.persist_scan(source, '0940', _clock(DAY), True)
    assert first['learningLedger']['trainingEligibleCount'] == replay['learningLedger']['trainingEligibleCount'] == 0
    assert replay['learningLedger']['idempotentReplay']


def test_legacy_replay_without_frozen_count_remains_unknown_and_unchanged(tmp_path):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: _clock(DAY))
    source = {'status': 'completed_observations', 'candidates': []}
    first = service.persist_scan(source, '0940', _clock(DAY), True)
    with service._connect() as db:
        payload = json.loads(db.execute('SELECT scan_json FROM signal_runs').fetchone()[0])
        payload.pop('sampleEligibilitySummary')
        original = json.dumps(payload)
        db.execute('UPDATE signal_runs SET scan_json=?', (original,))
    replay = service.persist_scan(source, '0940', _clock(DAY), True)
    assert replay['learningLedger']['trainingEligibleCount'] is None
    assert replay['learningLedger']['trainingEligible'] is None
    with service._connect() as db:
        assert db.execute('SELECT scan_json FROM signal_runs').fetchone()[0] == original
