"""Cold-start diagnostics must stay cheap, truthful and separate from admission."""
from datetime import date, timedelta
import sqlite3

from src.services.yao_scout import morning_task as morning
from src.services.yao_scout import task_learning as learning
from src.services.yao_scout.local_algorithm import algorithm_contract
from src.services.yao_scout.signal_learning import SignalLearningService
from tests.test_signal_learning import _clock


DAY = date(2026, 10, 16)


def registry_for(learner, now, data):
    return {'taskContractId': learner.contract['taskContractId'],
            'algorithmContractId': algorithm_contract()['contractId'],
            'readinessSnapshot': learner._readiness_snapshot(now, data)}


def test_hot_read_uses_only_registry_primary_keys_and_does_not_invent_zeroes(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path)
    queries = []

    def readonly_connection():
        db = sqlite3.connect(service.db_path.as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        db.set_trace_callback(queries.append)
        return db

    monkeypatch.setattr(service, '_connect', readonly_connection)
    monkeypatch.setattr(learning.TaskLearner, 'data', lambda *_: (_ for _ in ()).throw(AssertionError('ledger scan')))
    monkeypatch.setattr(learning.TaskLearner, 'artifact', lambda *_: (_ for _ in ()).throw(AssertionError('artifact load')))
    result = morning.morning_readiness(service, _clock(DAY), current_queue='0940',
                                      current_data_eligibility=[False, False])
    assert len(queries) == 4 and all('FROM learning_registry WHERE registry_key=' in query for query in queries)
    assert result['current']['status'] == 'data_blocked'
    assert result['current']['currentScan']['dataRejectedCount'] == 2
    assert '研究样本资格通过 0 只' in result['current']['message']
    assert 'current_research_eligibility_failed' in result['current']['blockerCodes']
    assert all(state['progress']['validSamples'] is None for state in result['queues'])
    assert all(state['progressKnown'] is False and state['selectionAuthority'] is False for state in result['queues'])


def test_refresh_deduplicates_stock_days_without_training_or_fit_timestamp(tmp_path, monkeypatch):
    now = _clock(DAY, 15, 30)
    service = SignalLearningService(tmp_path)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    data = ([{'day': DAY.isoformat(), 'code': '000001'},
             {'day': DAY.isoformat(), 'code': '000001'},
             {'day': DAY.isoformat(), 'code': '000002'}], [DAY.isoformat()], True)
    monkeypatch.setattr(learner, 'data', lambda _: data)
    monkeypatch.setattr(learning, 'fit_model', lambda *_: (_ for _ in ()).throw(AssertionError('fit')))
    state = learner.refresh_readiness(now)
    assert state['progress']['validSamples'] == 2
    assert state['progress']['rawSampleCount'] == 3
    assert state['progress']['validSampleDays'] == 1
    assert state['progressFresh'] is True
    assert state['lastFitCompletedAt'] is None and state['lastLearningCheckAt'] is None
    with service._connect() as db:
        assert db.execute('SELECT count(*) FROM model_versions').fetchone()[0] == 0
    later = learner.readiness(_clock(DAY + timedelta(days=1)))
    assert later['progressKnown'] is True and later['progressFresh'] is False
    assert later['progress']['validSamples'] == 2


def test_old_contract_counts_cannot_be_reused_as_current_progress(tmp_path):
    now = _clock(DAY)
    service = SignalLearningService(tmp_path)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    registry = registry_for(learner, now, ([], [], False))
    registry['readinessSnapshot']['algorithmContractId'] = 'old-algorithm'
    learner.save_registry(registry, now)
    state = learner.readiness(now)
    assert state['status'] == 'progress_unknown'
    assert state['progress']['validSamples'] is None
    assert 'cohort_contract_changed' in state['blockerCodes']


def test_audit_from_later_time_is_not_presented_as_already_known(tmp_path):
    now = _clock(DAY)
    service = SignalLearningService(tmp_path)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    learner.save_registry(registry_for(learner, _clock(DAY, 15, 30), ([], [], False)), now)
    state = learner.readiness(now)
    assert state['progress']['validSamples'] is None and state['progressKnown'] is False
    assert 'cohort_audit_after_as_of' in state['blockerCodes']


def test_ready_validation_failure_and_shadow_are_distinct_without_relaxing_gates(tmp_path):
    now = _clock(DAY, 15, 30)
    service = SignalLearningService(tmp_path)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '1030')
    days = [(DAY - timedelta(days=day)).isoformat() for day in range(120)]
    samples = [{'day': day, 'code': str(code)} for day in days for code in range(10)]
    registry = registry_for(learner, now, (samples, days, True))
    learner.save_registry(registry, now)
    state = learner.readiness(now)
    assert state['status'] == 'ready_for_training'
    assert state['progress']['minimumHoldoutDays'] == 60
    assert state['progress']['shadowRequiredDays'] == 20
    assert 'validated_model_missing' in state['blockerCodes']
    registry.update(reason='challenger_holdout_gate_failed', gates={'holdout': {'passed': False}})
    learner.save_registry(registry, now)
    state = learner.readiness(now)
    assert state['status'] == 'validation_blocked'
    assert state['validationGates']['holdout']['passed'] is False
    registry.update(reason='shadow_collecting_forward_mature_days', shadowVersion='shadow', shadowDays=7)
    learner.save_registry(registry, now)
    state = learner.readiness(now)
    assert state['status'] == 'shadow_collecting' and state['progress']['shadowDays'] == 7
    assert state['selectionAuthority'] is False


def test_queue_progress_and_current_data_qualification_remain_independent(tmp_path):
    now = _clock(DAY)
    service = SignalLearningService(tmp_path)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    registry = registry_for(learner, now, ([], [], False))
    learner.save_registry(registry, now)
    rows = [{'code': '000001', 'morningSignal': {'queue': '0940', 'researchEligibleAtDecision': True,
                                              'selectionEligible': False}},
            {'code': '000002', 'morningSignal': {'queue': '1030', 'researchEligibleAtDecision': False}}]
    result = morning.morning_readiness(service, now, current_candidates=rows)
    states = {state['queue']: state for state in result['queues']}
    assert states['0940']['status'] == 'no_eligible_samples'
    assert states['0940']['currentScan']['dataEligibleCount'] == 1
    assert states['1030']['status'] == 'data_blocked'
    assert states['1030']['progress']['validSamples'] is None
    assert states['opening']['currentScan']['candidateCount'] == 0


def test_training_check_saves_readiness_without_claiming_model_fit(tmp_path):
    now = _clock(DAY)
    service = SignalLearningService(tmp_path, clock=lambda: now)
    learner = learning.TaskLearner(service, morning.MORNING_TASK_ID, '0940')
    state = learner.train(now)
    assert state['readiness']['status'] == 'no_eligible_samples'
    assert state['lastFitCompletedAt'] is None
    saved = learner.readiness(now)
    assert saved['progress']['validSamples'] == 0
    assert saved['progressKnown'] is True and saved['lastFitCompletedAt'] is None
    assert saved['lastLearningCheckAt'] == now.isoformat()
