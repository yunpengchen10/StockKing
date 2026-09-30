import json
from datetime import date, timedelta

import numpy as np
import pytest

from src.services.yao_scout.local_algorithm import algorithm_contract
from src.services.yao_scout.signal_learning import SignalLearningService, MODEL_VERSION, _signal_preflight
from src.quant.executable_backtest import AccountConfig
from tests.test_signal_learning import _candidate, _clock, _bars


DAY = date(2026, 9, 7)


def test_training_preflight_requires_the_exact_live_entry_contract():
    good = _candidate('000001', DAY)
    assert _signal_preflight(good, _clock(DAY))
    for changes in ({'evidenceEligible': False}, {'status': 'windvane'},
                    {'algorithmContractId': 'old-contract'}, {'scoreVersion': 'old-rules'},
                    {'precisionDecision': {}}, {'finalScore': float('nan')}):
        assert not _signal_preflight({**good, **changes}, _clock(DAY))
    legacy = dict(good)
    legacy.pop('algorithmContractId')
    assert not _signal_preflight(legacy, _clock(DAY))


def test_old_and_ineligible_samples_remain_history_but_not_current_dataset(tmp_path):
    days = [DAY+timedelta(days=i) for i in range(6)]
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=lambda start,end: [day for day in days if start<=day<=end],
        bar_fetcher=lambda code,cutoff: _bars(days,locked_entry=True))
    candidates = [_candidate(f'00000{i}',DAY) for i in range(1,5)]
    candidates[1]['algorithmContractId'] = 'old-contract'
    candidates[2]['evidenceEligible'] = False
    candidates[3].pop('algorithmContractId')
    service.persist_scan({'status':'completed_observations','scoreVersion':algorithm_contract()['scoreVersion'],
                          'candidates':candidates},'0940',_clock(DAY),True)
    # Simulate historical flags written before the contract guard existed.
    with service._connect() as db:
        db.execute('UPDATE signal_events SET training_eligible=1')
    service.review_due(_clock(days[-1],15,30))
    samples = service._dataset(_clock(days[-1],15,30),dedupe=False)
    assert [sample['code'] for sample in samples] == ['000001']
    assert service._complete_universe({DAY.isoformat()},samples)
    assert len(service.history()['items'][0]['signals']) == 4
    with service._connect() as db:
        db.execute('UPDATE signal_runs SET version=?',('old-score-version',))
    assert service._dataset(_clock(days[-1],15,30)) == []
    assert not service._complete_universe({DAY.isoformat()},samples)


def artifact(version, contract=None):
    return {'version':version,'executionModel':MODEL_VERSION,
            'algorithmContractId':contract or algorithm_contract()['contractId'],
            'features':['finalScore'],'medians':[50], 'netRegressor':'fixture', 'maeRegressor':'fixture',
            'calibrationKnots':[0,1], 'calibrationPercentiles':[0,100], 'riskCoefficient':1,
            'logisticMean':[0], 'logisticScale':[1], 'logisticCoef':[1], 'logisticIntercept':0}


@pytest.mark.parametrize('stage,passed,contract,expected', [
    ('active',True,None,True), ('shadow',True,None,False), ('rejected',True,None,False),
    ('active',False,None,False), ('active',True,'old-contract',False)])
def test_active_ranker_requires_matching_contract_and_passed_release(stage,passed,contract,expected,tmp_path):
    service = SignalLearningService(tmp_path)
    with service._connect() as db:
        db.execute('INSERT INTO model_versions VALUES(?,?,?,?,?)',
                   ('model',stage,json.dumps(artifact('model',contract)),json.dumps({'passed':passed}),_clock(DAY).isoformat()))
    assert bool(service._model_artifact('model',active_only=True)) is expected


def test_corrupt_configured_champion_is_not_reported_as_effective(tmp_path):
    service = SignalLearningService(tmp_path)
    service._save_registry({'championVersion':'missing'},_clock(DAY))
    state = service.learning_state(_clock(DAY))
    assert state['configuredChampionVersion'] == 'missing'
    assert state['championVersion'] is None
    assert state['stage'] == 'rules_cold_start'
    assert state['effectiveRankVersion'] == 'rules_v1.1'
    assert state['effectiveRankSource'] == 'rules'
    assert state['rankingFallbackReason']


def test_old_gate_results_are_not_relabelled_with_the_new_contract(tmp_path):
    service = SignalLearningService(tmp_path)
    old_gates = {'holdout':{'passed':True,'simulatedAccountNet':.9}}
    service._save_registry({'algorithmContractId':'old-contract','gates':old_gates,
                            'shadowVersion':'missing','shadowDays':40},_clock(DAY))
    state = service.learning_state(_clock(DAY))
    assert state['gates'] == {}
    assert state['shadowDays'] == 0
    assert state['historicalValidationAvailable']
    state = service.train_and_evaluate(_clock(DAY))
    assert state['gates'] == {}
    assert state['validationContractId'] == algorithm_contract()['contractId']
    assert service._registry()['priorValidation']['gates'] == old_gates
    service._save_registry({'algorithmContractId':algorithm_contract()['contractId'],
                            'gates':{'holdout':{'passed':False}}},_clock(DAY))
    assert service.learning_state(_clock(DAY))['gates'] == {'holdout':{'passed':False}}


def test_live_ranking_and_saved_provenance_use_only_shared_eligible_rows(tmp_path,monkeypatch):
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY))
    with service._connect() as db:
        db.execute('INSERT INTO model_versions VALUES(?,?,?,?,?)',
                   ('model','active',json.dumps(artifact('model')),json.dumps({'passed':True}),_clock(DAY).isoformat()))
    service._save_registry({'championVersion':'model'},_clock(DAY))
    observed = []
    def predict(model, rows):
        observed.append(len(rows))
        return np.arange(len(rows),dtype=float)+70,np.full(len(rows),.5)
    monkeypatch.setattr(service,'_predict_artifact',predict)
    good = _candidate('000001',DAY)
    blocked = {**_candidate('000002',DAY),'evidenceEligible':False,'finalScore':99}
    ranked = service.rank_candidates([blocked,good])
    assert observed == [1]
    assert ranked[0]['code'] == '000001'
    assert ranked[0]['learningRankVersion'] == 'model'
    assert ranked[1]['learningRankVersion'] == 'rules_v1.1'
    service.persist_scan({'status':'completed_observations','scoreVersion':algorithm_contract()['scoreVersion'],
                          'candidates':ranked[:1],'controls':ranked[1:]},'0940',_clock(DAY),True)
    signals = service.history()['items'][0]['signals']
    assert signals[0]['snapshot']['learningRankVersion'] == 'model'
    assert signals[0]['snapshot']['algorithmContractId'] == algorithm_contract()['contractId']
    assert signals[0]['trainingEligible']
    assert not signals[1]['trainingEligible']


def test_account_baseline_uses_saved_selection_rank_and_model_ties_use_live_order():
    day = DAY.isoformat()
    common = {'day':day,'runId':'same-scan','selected':True,'filled':True,
              'decisionAt':_clock(DAY).isoformat(),'entryAt':_clock(DAY,9,42).isoformat(),
              'exitAt':_clock(DAY,15,0).isoformat(),'entryPrice':10.,'entryMinuteVolume':1_000_000}
    winner = {**common,'code':'000001','features':{'finalScore':10},'selectionRank':1,
              'recordedRankScore':90,'rankingTieBreakOrder':0,'exitPrice':12.,'marks':{day:12.}}
    loser = {**common,'code':'000002','features':{'finalScore':99},'selectionRank':2,
             'recordedRankScore':80,'rankingTieBreakOrder':1,'exitPrice':8.,'marks':{day:8.}}
    samples = [loser,winner]  # SQL order is not the live ranking order.
    config = AccountConfig(max_positions=1)
    assert SignalLearningService._account_nav(samples,None,config)['net'] > 0
    assert SignalLearningService._account_nav(samples,np.array([90,90]),config)['net'] > 0
    assert SignalLearningService._account_nav(samples,np.array([99,80]),config)['net'] < 0
