"""Daily rule publications remain auditable without becoming minute training."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src.services.yao_scout import signal_learning as ledger
from src.services.yao_scout import morning_task, task_learning
from src.services.yao_scout.local_algorithm import algorithm_contract
from tests.test_daily_primary import NOW, CODE, run


def daily_row(at=NOW, price=10.05):
    row = deepcopy(run()['dailyCandidates'][0])
    row.update(decision_at=at.isoformat(), signalPrice=price, referencePrice=price)
    row['quote'].update(price=price, provider_timestamp=at.isoformat())
    return row


def scan(row, *, profile_only=False, run_id='daily-one'):
    return {'run_id': run_id, 'status': 'completed_observations',
            'scoreVersion': algorithm_contract()['scoreVersion'],
            'candidates': [] if profile_only else [row],
            'profileCandidates': {'aggressive': [row]}, 'dailyProfileCandidates': {'aggressive': [row]}}


@pytest.mark.parametrize('compact', [False, True])
def test_profile_only_daily_publication_roundtrips_evidence_without_minute_task(tmp_path, monkeypatch, compact):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    monkeypatch.setattr(service, '_learning_quality', lambda *_: pytest.fail('daily must not check minute lineage'))
    monkeypatch.setattr(ledger, '_signal_preflight', lambda *_: pytest.fail('daily must not use minute entry gate'))
    result = scan(daily_row(), profile_only=True)
    original = deepcopy(result)
    saved = service.persist_scan(result, '1030', NOW, True)
    assert result == original
    assert saved['learningLedger']['selectedCount'] == 0
    assert saved['learningLedger']['dailySelectedCount'] == saved['learningLedger']['savedRecommendationCount'] == 1
    assert not saved['learningLedger']['trainingEligible'] and not saved['learningLedger']['reviewEligible']
    event = service.history({'compact': compact, 'include_controls': False})['items'][0]['signals'][0]
    assert not event['selected'] and event['dailySelected']
    assert event['recommendationKinds'] == ['daily_condition']
    assert not event['trainingEligible'] and not event['reviewEligible'] and event['features'] == {}
    assert event['reviewStatus'] == 'daily_observation_only'
    snapshot = event['snapshot']
    assert snapshot['dailyEvidence'] == original['profileCandidates']['aggressive'][0]['dailyEvidence']
    assert snapshot['selectionReasons'] == original['profileCandidates']['aggressive'][0]['selectionReasons']
    assert snapshot['dailyProfileDecision'] == original['profileCandidates']['aggressive'][0]['dailyProfileDecision']
    assert snapshot['dailyRankEvidence'] == original['profileCandidates']['aggressive'][0]['dailyRankEvidence']
    assert snapshot['dailyRankEvidence']['referencePeerCount'] == 1
    assert snapshot['dailyRankEvidence']['probability'] is None
    assert snapshot['dailySignal']['selectedProfiles'] == ['aggressive']
    assert snapshot['dailySignal']['referenceVerifiedAtPublication'] is True
    assert snapshot['dailyFirstReferenceTime'] == NOW.isoformat()
    assert snapshot['dailyFirstReferencePrice'] == 10.05
    assert 'morningSignal' not in snapshot
    assert service.reviews()['items'] == []
    assert service.review_requirements(NOW) == {}
    with service._connect() as db:
        meta = json.loads(db.execute('SELECT scan_json FROM signal_runs').fetchone()[0])
        assert not meta.get('algorithmContractId') and not meta.get('morningTaskContractId')
        assert db.execute('SELECT COUNT(*) FROM forward_predictions').fetchone()[0] == 0


def test_daily_first_reference_survives_price_changes_refreshes_and_profile_moves(tmp_path):
    clock = [NOW]
    service = ledger.SignalLearningService(tmp_path, clock=lambda: clock[0])
    first = service.persist_scan(scan(daily_row()), 'live', NOW, False)
    clock[0] = NOW + timedelta(minutes=5)
    later = service.persist_scan(scan(daily_row(clock[0], 9.8), profile_only=True, run_id='later'),
                                 'live', clock[0], False)
    signal = later['profileCandidates']['aggressive'][0]['dailySignal']
    assert signal['referencePrice'] == 9.8
    assert signal['firstReferencePrice'] == 10.05 and signal['firstReferenceTime'] == NOW.isoformat()
    assert signal['currentSignalState'] == 'continued' and signal['isRepeatedSignal']
    assert first['candidates'][0]['dailySignal']['currentSignalState'] == 'new'
    assert later['dailyProfileCandidates']['aggressive'][0]['dailySignal'] == signal
    assert all(event['snapshot']['dailyFirstReferencePrice'] == 10.05
               for item in service.history()['items'] for event in item['signals'])


def test_daily_and_morning_first_prices_are_independent_in_both_directions(tmp_path, monkeypatch):
    clock = [NOW]
    service = ledger.SignalLearningService(tmp_path, clock=lambda: clock[0])
    monkeypatch.setattr(service, '_learning_quality', lambda *_: {'eligible': True, 'qualityKnownAt': NOW.isoformat()})
    monkeypatch.setattr(ledger, '_signal_preflight', lambda *_: True)
    queue = morning_task.morning_queue('live', NOW)
    minute = {'code': CODE, 'name': '分钟样本', 'decision_at': NOW.isoformat(),
              'referencePrice': 11., 'signalPrice': 11., 'quote': {'price': 11., 'source_time': NOW.isoformat()},
              'morningSignal': {'version': morning_task.MORNING_VERSION, 'queue': queue,
                'learningTaskContractId': task_learning.task_contract(morning_task.MORNING_TASK_ID, queue)['taskContractId'],
                'researchEligibleAtDecision': True}}
    prior = service.persist_scan({'run_id': 'minute-first', 'status': 'completed_observations', 'candidates': [minute]},
                                 'live', NOW, False)
    clock[0] += timedelta(minutes=1)
    daily = service.persist_scan(scan(daily_row(clock[0])), 'live', clock[0], False)['candidates'][0]
    assert daily['firstReferencePrice'] == daily['dailyFirstReferencePrice'] == 10.05
    assert daily['firstReferenceTime'] == clock[0].isoformat()
    assert 'morningSignal' not in daily
    clock[0] += timedelta(minutes=1)
    minute.update(decision_at=clock[0].isoformat(), referencePrice=10.7, signalPrice=10.7,
                  quote={'price': 10.7, 'source_time': clock[0].isoformat()})
    after = service.persist_scan({'run_id': 'minute-again', 'status': 'completed_observations', 'candidates': [minute]},
                                 'live', clock[0], False)['candidates'][0]
    assert after['morningSignal']['firstReferencePrice'] == 11.
    assert after['morningSignal']['firstReferenceTime'] == prior['candidates'][0]['firstReferenceTime']


def test_copied_minute_contract_cannot_promote_daily_or_create_forward_predictions(tmp_path, monkeypatch):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    monkeypatch.setattr(ledger, '_signal_preflight', lambda *_: True)
    monkeypatch.setattr(service, '_learning_quality', lambda *_: {'eligible': True})
    row = daily_row()
    row.update(morningSignal={'selected': True, 'trainingEligibleAtSignal': True},
               algorithmContractId=algorithm_contract()['contractId'], v11Inputs={'r3_pct': 3.},
               prediction={'upProbability': .99}, trainingEligible=True,
               firstReferenceTime=(NOW-timedelta(hours=1)).isoformat(), firstReferencePrice=1.)
    row['dailySignal']['trainingEligibleAtSignal'] = True
    assert ledger._current_entry_eligible(row) is False
    saved = service.persist_scan(scan(row), '1030', NOW, True)
    published = saved['candidates'][0]
    assert 'morningSignal' not in published and 'prediction' not in published
    assert 'algorithmContractId' not in published and 'v11Inputs' not in published
    assert published['dailySignal']['firstReferencePrice'] == 10.05
    assert published['dailySignal']['trainingEligibleAtSignal'] is False
    assert published['trainingEligible'] is False
    event = service.history()['items'][0]['signals'][0]
    assert event['recommendationKinds'] == ['daily_condition']
    assert not event['trainingEligible'] and not event['reviewEligible']
    assert service._dataset(NOW) == []


def test_official_replay_keeps_original_daily_reference_and_evidence(tmp_path):
    clock = [NOW]
    service = ledger.SignalLearningService(tmp_path, clock=lambda: clock[0])
    saved = service.persist_scan(scan(daily_row()), '1030', NOW, True)
    clock[0] += timedelta(minutes=1)
    changed = scan(daily_row(clock[0], 9.))
    replay = service.persist_scan(changed, '1030', clock[0], True)
    assert replay['learningLedger']['idempotentReplay']
    assert replay['candidates'] == saved['candidates']
    assert replay['learningLedger']['dailySelectedCount'] == 1


def test_publication_reference_rechecks_quote_age_and_next_day_starts_new_reference(tmp_path):
    clock = [NOW + timedelta(seconds=31)]
    service = ledger.SignalLearningService(tmp_path, clock=lambda: clock[0])
    stale = service.persist_scan(scan(daily_row()), 'live', NOW, False)['candidates'][0]['dailySignal']
    assert stale['referenceVerifiedAtPublication'] is False
    clock[0] = NOW + timedelta(days=1)
    next_day = service.persist_scan(scan(daily_row(clock[0], 10.2), run_id='next-day'),
                                    'live', clock[0], False)['candidates'][0]['dailySignal']
    assert next_day['firstReferenceTime'] == clock[0].isoformat()
    assert next_day['firstReferencePrice'] == 10.2 and next_day['isRepeatedSignal'] is False


def test_profile_daily_overlapping_minute_control_does_not_mix_contracts(tmp_path):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    result = scan(daily_row(), profile_only=True)
    result['controls'] = [{'code': CODE, 'status': 'conditional', 'v11Inputs': {'r3_pct': 1.}}]
    service.persist_scan(result, '1030', NOW, True)
    event = service.history({'include_controls': False})['items'][0]['signals'][0]
    assert event['dailySelected'] and event['features'] == {}
    assert not event['trainingEligible'] and not event['reviewEligible']
    assert 'v11Inputs' not in event['snapshot']


def test_empty_daily_scan_is_not_a_minute_or_morning_cash_only_session(tmp_path):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    service.persist_scan({'status': 'completed_observations', 'candidates': [],
                          'primarySelectionMode': 'daily_conditional',
                          'modelVersion': algorithm_contract()['scoreVersion']}, '1030', NOW, True)
    with service._connect() as db:
        meta = json.loads(db.execute('SELECT scan_json FROM signal_runs').fetchone()[0])
    assert meta['algorithmContractId'] is None
    assert 'morningTaskContractId' not in meta
    assert service.history()['items'][0]['signals'] == []
