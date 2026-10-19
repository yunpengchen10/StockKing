"""Task isolation, point-in-time validation and deployed decision parity."""
from copy import deepcopy
from datetime import date, timedelta
import json

import numpy as np
import pytest

from src.services.yao_scout import task_learning as learning
from src.services.yao_scout.local_algorithm import algorithm_contract
from src.services.yao_scout.signal_learning import SignalLearningService, _snapshot, _signal_preflight
from src.quant.executable_backtest import AccountConfig
from tests.test_signal_learning import _candidate, _clock, verified_ledger_quality
from tests.test_signal_learning_contract import artifact, fixture_predictions

DAY = date(2026, 1, 5)


def synthetic(days=100, stocks=12):
    rows = []
    for index in range(days):
        day = DAY+timedelta(days=index)
        for number in range(stocks):
            x = ((index*7+number*13) % 31)/30
            rows.append({'day':day.isoformat(), 'code':f'{number:06}', 'signalId':f'{index}:{number}',
                'features':{'x':x, 'constant':55}, 'net':.18*x-.075,
                'labelEndAt':_clock(day+timedelta(days=1),15,0).isoformat(),
                'labelKnownAt':_clock(day+timedelta(days=1),15,30).isoformat(),
                'dataQualityKnownAt':_clock(day+timedelta(days=1),15,30).isoformat()})
    return rows


def test_real_heads_fit_calibrate_and_roundtrip_with_missing_indicators():
    rows = synthetic()
    model = learning.fit_model(rows[:720], rows[840:1080], learning.ENTRY_TASK_ID)
    test = [deepcopy(row) for row in rows[1080:]]
    test[0]['features'].pop('x')
    prediction = learning.predict_model(json.loads(json.dumps(model)), test)
    assert model['taskId'] == learning.ENTRY_TASK_ID
    assert model['returnMeaning'] == 'after_cost_execution_proxy'
    assert len(model['ridgeCoef']) == 2*len(model['features'])
    assert np.all((prediction['probability'] > 0) & (prediction['probability'] < 1))
    assert prediction['eligible'].any() and (~prediction['eligible']).any()
    assert np.all(prediction['rank'][~prediction['eligible']] == 0)
    assert np.allclose(prediction['utility'], prediction['net']-np.maximum(0,-prediction['tail']))
    metric = learning.diagnostics(test,prediction,model)
    assert metric['brierSkill'] > 0
    assert metric['independentTradingDays'] == 10
    # Same inputs always mean same calibrated probability, independent of the
    # contemporaneous candidate queue; this is not a within-queue percentile.
    alone = learning.predict_model(model,[test[1]])
    assert alone['probability'][0] == pytest.approx(prediction['probability'][1])


def test_task_targets_are_not_interchangeable_or_fake_t_plus_2():
    row = {'net':.06,'eventLabel':0}
    assert learning.class_target(row,learning.ENTRY_TASK_ID) == 1
    assert learning.class_target(row,learning.NEXT_DAY_TASK_ID) == 0
    first = learning.task_contract(learning.NEXT_DAY_TASK_ID,'first_board')
    continuation = learning.task_contract(learning.NEXT_DAY_TASK_ID,'continuation')
    assert first['taskContractId'] != continuation['taskContractId']
    assert first['taskContractId'] != learning.task_contract()['taskContractId']
    assert first['returnMeaning'] == 'price_observation_not_trading_return'
    with pytest.raises(ValueError,match='T_plus_2'):
        learning.task_contract(learning.DEFERRED_ENTRY_TASK_ID)


def test_partition_purges_actual_exit_label_and_quality_knowledge_times():
    rows = synthetic(stocks=1)
    days = sorted({row['day'] for row in rows})
    # boundary=80, calibration starts55, train stops50. All three late
    # discoveries must be excluded even though their signal timestamps are old.
    rows[0]['labelEndAt'] = days[55]+'T09:31:00+08:00'
    rows[1]['labelKnownAt'] = days[55]+'T08:00:00+08:00'
    rows[2]['dataQualityKnownAt'] = days[55]+'T08:00:00+08:00'
    rows[55]['labelKnownAt'] = days[80]+'T08:00:00+08:00'
    train, calibration = learning.partition(rows,days,80)
    assert {row['signalId'] for row in train}.isdisjoint({'0:0','1:0','2:0'})
    assert '55:0' not in {row['signalId'] for row in calibration}
    assert max(row['day'] for row in train) < min(row['day'] for row in calibration)
    assert all(row['labelKnownAt'] < days[80] for row in calibration)


def test_day_and_stock_day_weighting_does_not_treat_rescans_as_independent():
    rows = [{'day':'a','code':'1'}, {'day':'a','code':'1'}, {'day':'a','code':'2'},
            {'day':'b','code':'1'}]
    weights = learning.balanced_sample_weights(rows)
    assert weights[0]+weights[1] == pytest.approx(weights[2])
    assert sum(weights[:3]) == pytest.approx(weights[3])


def test_fold_metrics_use_each_fold_baseline_not_last_fold_prevalence():
    rows = [{'day':'a','code':'1','features':{},'net':0}, {'day':'b','code':'1','features':{},'net':.1}]
    model = {**learning.task_contract(),'trainingPrevalence':.9,'trainingMeanReturn':.9}
    prediction = {'probability':np.array([.1,.9]),'net':np.array([.01,.09]),'tail':np.array([-.01,.05]),
        'eligible':np.array([False,True]), 'trainingPrevalence':np.array([.1,.9]),
        'trainingMeanReturn':np.array([0,.1])}
    result = learning.diagnostics(rows,prediction,model)
    assert result['baselineBrier'] == pytest.approx(.01)
    assert result['outOfSampleR2'] is None  # per-fold constant baseline predicts exactly


def test_lineage_combination_preserves_valid_different_prefixes():
    first = {'sessions':[{'code':'000001','day':'2026-01-05','frozenThrough':'10:30','revision':'a'}]}
    later = {'sessions':[{'code':'000001','day':'2026-01-05','frozenThrough':'14:55','revision':'b'}]}
    assert len(learning.combine_lineages([first,later])['sessions']) == 2
    corrupt = deepcopy(first)
    corrupt['sessions'][0]['revision'] = 'different'
    with pytest.raises(ValueError,match='conflicting'):
        learning.combine_lineages([first,corrupt])


def test_corrupt_heads_and_invalid_lineage_cannot_be_effective_champion(tmp_path,monkeypatch):
    service = SignalLearningService(tmp_path)
    with service._connect() as db:
        db.execute('INSERT INTO model_versions VALUES(?,?,?,?,?)',
            ('model','active',json.dumps(artifact('model')),json.dumps({'passed':True}),_clock(DAY).isoformat()))
    monkeypatch.setattr(learning,'lineage_valid',lambda *_:True)
    assert learning.load_artifact(service,'model',True) is None  # fixture is not a LightGBM file
    monkeypatch.setattr(learning,'predict_model',fixture_predictions)
    assert learning.load_artifact(service,'model',True)
    monkeypatch.setattr(learning,'lineage_valid',lambda *_:False)
    assert learning.load_artifact(service,'model',True) is None  # probe cache never bypasses lineage


def test_no_fit_timestamp_for_checks_and_no_fake_model_at_cold_start(tmp_path):
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY))
    state = service.train_and_evaluate(_clock(DAY))
    assert state['lastLearningCheckAt'] == _clock(DAY).isoformat()
    assert state['lastFitCompletedAt'] is None and state['lastTrainingAt'] is None
    assert len(state['taskStates']) == 3 + len(learning.MORNING_QUEUES)
    assert all(row['stage'] == 'rules_cold_start' for row in state['taskStates'])
    with service._connect() as db:
        assert db.execute('SELECT count(*) FROM model_versions').fetchone()[0] == 0
    row = service.rank_candidates([_candidate('000001',DAY)])[0]
    assert row['prediction']['status'] == 'rules_unvalidated'
    assert row['prediction']['bigMoveProbability'] is None
    assert row['prediction']['entryEligible'] is None


def test_model_abstention_does_not_remove_control_training_eligibility(tmp_path,monkeypatch):
    row = _candidate('000001',DAY)
    row['prediction'] = {'status':'active','entryEligible':False,'expectedNetReturn':-.04,'utility':-.09}
    assert _signal_preflight(row,_clock(DAY))
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY))
    saved = service.persist_scan({'status':'completed_observations','scoreVersion':algorithm_contract()['scoreVersion'],
        'candidates':[],'controls':[row]},'0940',_clock(DAY),True)
    signals = service.history()['items'][0]['signals']
    assert signals[0]['trainingEligible'] and not signals[0]['selected']


def test_factor_evidence_and_predecision_caps_are_frozen():
    row = _candidate('000001',DAY)
    row.update(factorAvailability={'M':{'available':False,'missing_reason':'path_missing'}},
        factorCutoffAt=_clock(DAY).isoformat(),portfolioWeight=.2,portfolioWeightKind='cap',
        portfolioPolicy={'version':'king-portfolio-v1','industry':'bank'})
    frozen = _snapshot(row)
    assert frozen['factorAvailability'] == row['factorAvailability']
    assert frozen['factorCutoffAt'] == row['factorCutoffAt']
    assert frozen['portfolioWeightKind'] == 'cap'


def account_row(code='000001',industry='a',run='scan',hour=9):
    tomorrow = DAY+timedelta(days=1)
    return {'code':code,'day':DAY.isoformat(),'runId':run,'selected':True,'filled':True,
        'features':{'finalScore':90},'decisionAt':_clock(DAY,hour,40).isoformat(),
        'entryAt':_clock(DAY,hour,42).isoformat(),'entryPrice':10.,
        'exitAt':_clock(tomorrow,15,0,0).isoformat(),'exitPrice':11.,
        'entryMinuteVolume':1_000_000,'exitMinuteVolume':1_000_000,
        'marks':{DAY.isoformat():10.,tomorrow.isoformat():11.},
        'portfolioWeight':.2,'portfolioWeightKind':'cap',
        'portfolioPolicy':{'totalExposureCap':.4,'maxIndustryWeight':.2,'industry':industry}}


def test_zero_advantage_keeps_cash_and_caps_apply_across_scans():
    rows = [account_row(),account_row('000002','b','later',14)]
    rejected = SignalLearningService._account_nav(rows,np.zeros(2),AccountConfig())
    assert rejected['valid'] and rejected['completedTrades'] == 0 and rejected['net'] == 0
    rows += [account_row('000003','c','later',14)]
    result = SignalLearningService._account_nav(rows,np.array([90.,80.,70.]),AccountConfig())
    assert result['valid'] and 2 <= result['completedTrades'] <= 3
    assert 0 < result['net'] < .041  # maximum 40% exposure, no per-scan reset to full equity


def test_sector_block_does_not_hide_later_eligible_industry():
    rows = [account_row('000001','a'),account_row('000002','a'),account_row('000003','b')]
    result = SignalLearningService._account_nav(rows,np.array([90.,80.,70.]),AccountConfig(max_positions=2))
    assert result['valid'] and result['completedTrades'] == 2


def test_execution_label_uses_frozen_ten_percent_cap_and_fee_inclusive_lots(tmp_path):
    from tests.test_signal_learning import _bars
    from src.services.yao_scout.signal_learning import _quantity_with_fees
    days = [DAY+timedelta(days=i) for i in range(2)]
    bars = _bars(days)
    config = AccountConfig(initial_cash=100_000)
    candidate = _candidate('000001',DAY)
    candidate['portfolioWeight'] = .1
    # A 10% allocation is executable; the old hard-coded 20% allocation is not.
    for bar in bars:
        if bar['end'] == _clock(DAY,9,42,0).isoformat():
            bar['volume_shares'] = 12_000
            bar['amount_cny'] = 12_000*bar['close']
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY),bar_fetcher=lambda *_:bars,
        calendar_provider=lambda start,end:[d for d in days if start <= d <= end])
    service.execution_config = config
    service.persist_scan({'status':'completed_observations','candidates':[candidate]},'0940',_clock(DAY),True)
    service.review_due(_clock(days[1],15,30))
    item = next(row for row in service.reviews()['items'] if row['horizon'] == 1)
    assert item['status'] == 'mature'
    qty,fee = _quantity_with_fees('000001',10.,10_000,DAY,config)
    assert qty*10+fee <= 10_000 and qty == 900


def test_next_day_dataset_keeps_selected_and_controls_with_independent_task(tmp_path,monkeypatch):
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY))
    tomorrow = DAY+timedelta(days=1)
    rows = [_candidate(f'00000{i}',DAY) for i in (1,2)]
    for row in rows:
        row['evidenceEligible'] = False  # not eligible for entry-today training
        row['nextDaySignal'] = {'queue':'first_board','version':'fixture-next','targetSession':tomorrow.isoformat(),
            'learningTaskContractId':learning.task_contract(learning.NEXT_DAY_TASK_ID,'first_board')['taskContractId'],
            'selected':row['code'] == '000001','score':70,'features':{'r5':.2}}
    service.persist_scan({'status':'completed_observations','scoreVersion':algorithm_contract()['scoreVersion'],
        'candidates':[],'controls':rows},'0940',_clock(DAY),True)
    with service._connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS next_day_outcomes(signal_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, result_json TEXT NOT NULL)')
        signals = db.execute('SELECT signal_id FROM signal_events').fetchall()
        for index,signal in enumerate(signals):
            outcome = {'status':'mature_price_pattern','targetSession':tomorrow.isoformat(),'version':'fixture-next',
                'observedReturnFromSignalPct':8 if index == 0 else -4,'closeAtLimit':index == 0,
                'labelEndAt':_clock(tomorrow,15,0).isoformat()}
            db.execute('INSERT INTO next_day_outcomes VALUES(?,?,?)',
                (signal[0],_clock(tomorrow,15,30).isoformat(),json.dumps(outcome)))
    learner = learning.TaskLearner(service,learning.NEXT_DAY_TASK_ID,'first_board')
    before = learner._next_events(_clock(tomorrow,15,15))
    assert len(before) == 2 and not any(row['complete'] for row in before)
    after = learner._next_events(_clock(tomorrow,15,45))
    assert len(after) == 2 and all(row['complete'] for row in after)
    assert {row['selected'] for row in after} == {False,True}
    assert {row['net'] for row in after} == {-.04,.08}
    assert service._dataset(_clock(tomorrow,15,45)) == []
    assert learning.TaskLearner(service,learning.NEXT_DAY_TASK_ID,'continuation')._next_events(_clock(tomorrow,15,45)) == []


def test_unknown_at_publication_is_immutable_observation_not_future_training(tmp_path,monkeypatch,verified_ledger_quality):
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY))
    monkeypatch.setattr(service,'_learning_quality',verified_ledger_quality.__get__(service))
    row = _candidate('000001',DAY)
    row['nextDaySignal'] = {'queue':'first_board','selected':True}
    result = {'status':'completed_observations','candidates':[row]}
    service.persist_scan(result,'0940',_clock(DAY),True)
    item = service.history()['items'][0]['signals'][0]
    assert not item['trainingEligible']
    assert item['snapshot']['learningDataEligibility']['eligible'] is False
    assert item['snapshot']['nextDaySignal']['trainingEligibleAtSignal'] is False
    # Independent verification obtained later cannot amend the historical run.
    monkeypatch.setattr(learning,'lineage_known_at',lambda *_:_clock(DAY,9,40,34).isoformat())
    service.persist_scan(result,'0940',_clock(DAY),True)
    assert not service.history()['items'][0]['signals'][0]['trainingEligible']
    monkeypatch.setattr(learning,'lineage_known_at',lambda *_:_clock(DAY,9,41).isoformat())
    assert not service._learning_quality(row,_clock(DAY))['eligible']


def test_fixed_calendar_cohort_does_not_select_surviving_returns(tmp_path,monkeypatch):
    service = SignalLearningService(tmp_path)
    rows = synthetic(days=300,stocks=1)
    for row in rows:
        row.update(dataLineage={},labelDataLineage={},portfolioWeight=.2,portfolioPolicy={'version':'test'})
    days = sorted({row['day'] for row in rows})
    monkeypatch.setattr(service,'_evaluation_days',lambda *_:days)
    monkeypatch.setattr(service,'_dataset',lambda *_,**__:rows)
    monkeypatch.setattr(learning,'lineage_known_at',lambda *_:'2026-01-01T00:00:00+08:00')
    expected = {row['signalId'] for row in rows[60:]}
    monkeypatch.setattr(service,'_complete_universe',lambda selected,cohort:
        selected == set(days[-240:]) and {row['signalId'] for row in cohort} == expected)
    learner = learning.TaskLearner(service)
    samples,selected,complete = learner.data(_clock(date(2026,12,31)))
    assert len(samples) == len(selected) == 240 and complete
    rows.pop(100)
    assert learner.data(_clock(date(2026,12,31)))[2] is False


def test_prefix_validation_cache_scoped_to_one_operation(tmp_path,monkeypatch):
    from src.services.yao_scout import minute_history as minute
    from tests.test_minute_integrity import CODE, DAY as archive_day, NOW, bars, anchor
    import time
    archive = minute.MinuteArchive(tmp_path/'minute_history')
    archive.save(CODE,bars(),NOW,verification=anchor())
    lineage = minute.read_archive_lineage(tmp_path/'minute_history',CODE,[archive_day])
    assert lineage['status'] == 'verified'
    original = minute.validate_archive_lineage
    calls = []
    def counted(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(minute,'validate_archive_lineage',counted)
    service = SignalLearningService(tmp_path)
    @learning.scoped_lineage_validation
    def operation(owner):
        return [learning.lineage_known_at(owner,lineage) for _ in range(1000)]
    start = time.perf_counter()
    result = operation(service)
    elapsed = time.perf_counter()-start
    assert all(result) and len(calls) == 1
    assert elapsed < 5  # 1000 repeated frozen dependencies must not reload 240,000 bars
    operation(service)
    assert len(calls) == 2  # subsequent request rechecks integrity


def test_review_archive_reader_honors_quarantine(tmp_path):
    from src.services.yao_scout import minute_history as minute
    from tests.test_minute_integrity import CODE, NOW, bars
    archive = minute.MinuteArchive(tmp_path/'minute_history')
    archive.save(CODE,bars(),NOW)
    service = SignalLearningService(tmp_path)
    assert service._archive_bars(CODE,NOW)
    archive.save(CODE,bars(price=7.),NOW)  # same-minute contradiction preserves old canonical bytes
    assert service._archive_bars(CODE,NOW) == []


def test_missing_entry_volume_is_unknown_not_cash_zero(tmp_path,monkeypatch):
    from tests.test_signal_learning import _bars
    days = [DAY+timedelta(days=i) for i in range(2)]
    rows = _bars(days)
    for row in rows:
        if row['end'] == _clock(DAY,9,42,0).isoformat():
            row['volume_shares'] = None
    service = SignalLearningService(tmp_path,clock=lambda:_clock(DAY),bar_fetcher=lambda *_:rows,
        calendar_provider=lambda start,end:[day for day in days if start <= day <= end])
    # Isolate execution semantics after normalisation; production normalisation
    # may reject this bar earlier, which also correctly leaves pending_data.
    monkeypatch.setattr(service,'_bar_rows',lambda values,cutoff:values)
    service.persist_scan({'status':'completed_observations','candidates':[_candidate('000001',DAY)]},'0940',_clock(DAY),True)
    service.review_due(_clock(days[1],15,30))
    item = next(row for row in service.reviews()['items'] if row['horizon'] == 1)
    assert item['status'] == 'pending_data' and item['reason'] == 'entry_volume_unknown'
    assert service._dataset(_clock(days[1],15,30)) == []
