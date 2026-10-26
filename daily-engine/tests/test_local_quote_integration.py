from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from src.services.yao_scout import daily_opportunities as base
from src.services.yao_scout import local_opportunities as local


def make_service(monkeypatch, start, tmp_path, fetch=None, batch=None):
    tick = [start]
    events = []
    saved = []
    frame = pd.DataFrame([{'code': '600001', 'name': '测试股', 'price': 10}])
    def snapshot():
        events.append(('snapshot', tick[0]))
        return frame
    db = SimpleNamespace(list_yao_runs=lambda **kw: [], list_yao_dlm=lambda **kw: [], save_yao_run=saved.append)
    history = pd.DataFrame({'date': pd.bdate_range(end=start.date()-timedelta(days=1), periods=60),
                            'close': [9.2+i*.6/59 for i in range(60)]})
    # A tradable fixture needs room beyond normal daily volatility. Keep an
    # older pressure level within20 sessions but outside the14-day ATR window;
    # the former +1 high on every bar only passed with a tiny minute stop.
    history['high'], history['low'] = history.close+.15, history.close-.15
    history.loc[history.index[-18], 'high'] = 10.8
    history.attrs['daily_source'] = 'test'
    yao = SimpleNamespace(db=db, _fetch_snapshot=snapshot, _snapshot_meta=lambda *a, **kw: {},
                          history_fetcher=lambda *a, **kw: history, data_dir=tmp_path)
    monkeypatch.setattr(base, 'is_market_open', lambda *a: True)
    monkeypatch.setattr(base.HighClient, 'ask', lambda *a: pytest.fail('automatic scan called paid AI'))
    def quote(code):
        events.append(('quote', tick[0]))
        return {'code': code, 'source': 'tencent', 'price': 10, 'pre_close': 9.8, 'open_price': 9.7,
                'limit_up': 10.78, 'limit_down': 8.82,
                'high': 10.05, 'low': 9.65, 'change_pct': 100*(10/9.8-1),
                'amount': 980000000, 'volume':100000000, 'provider_timestamp': tick[0].isoformat(),
                'active_buy_share_pct':60,'active_flow_source':'fixture','active_flow_as_of':tick[0].isoformat()}
    service = local.LocalOpportunityService(yao, {'api_key': 'must-not-be-used'},
        selection_mode='intraday',  # These fixtures exercise the minute contract explicitly.
        context_provider=SimpleNamespace(collect=lambda now: {'tables': [
            {'kind': kind, 'status': 'available', 'observedAt': now.isoformat(), 'source': 'fixture', 'rows': rows}
            for kind, rows in [('financials', [{'股票代码': '600001', '最新公告日期': '2026-08-30',
                '净利润-净利润': 100, '每股收益': 1, '每股经营现金流量': 1}]), ('forecast', []), ('unlock', [])]]}),
        quote_fetcher=fetch or quote, batch_quote_fetcher=batch,
        minute_fetcher=lambda *a: {'metrics':{'speed_3m_pct':.3,'speed_5m_pct':.6,'local_high_5m':9.99,
                                               'local_low_5m':9.7,'amount_3m':5000000,'relative_amount_3m_20d':2},'historyDays':20,
                                   'priceHistoryDays':20,
                                   'v11Inputs':{'history_days':20,'price_history_days':20,
                                       'z_r3_pct':1,'z_r5_pct':1,'z_a3_pct_per_min':1,'z_a1_pct_per_min':1,
                                       'z_amount_3m':1,'z_amount_5m':1,'v3_ratio':2,'v5_ratio':2,
                                       'r3_pct':.3,'r5_pct':.6,'vwap_slope_5m_pct':.1},
                                   'asOf':(tick[0]-timedelta(minutes=1)).isoformat(), 'source':'test_minutes','gaps':[]},
        sector_fetcher=lambda codes,*a: {code:{'metrics':{'sector_coverage_pct':100,'sector_peer_count':10,'sector_return_5m_pct':.2,'sector_breadth':80,'sector_relative_5m_pct':.4},
            'asOf':(tick[0]-timedelta(minutes=1)).isoformat(),'sector':{'name':'fixture'}} for code in codes},
        fund_fetcher=lambda *a: {'metrics':{'main_net_flow_3m':1e5},'asOf':(tick[0]-timedelta(minutes=1)).isoformat()},
        clock=lambda: tick[0], sleeper=lambda seconds: tick.__setitem__(0, tick[0] + timedelta(seconds=seconds)))
    return service, tick, events, saved


def enable_primary_evidence(service):
    """Supply verified full-universe evidence for entry-path integration tests."""
    original = service.minute_fetcher
    service.minute_fetcher = lambda *args: {**original(*args), 'dataLineage': {'status': 'verified'}}
    service._observe_market_emotion = lambda frame, received: {
        'source': 'test_full_mainboard', 'asOf': received.isoformat(), 'status': 'insufficient',
        'score': None, 'regime': 'unknown', 'features': {
            'source_coverage': 1, 'universe_count': 3000, 'advancing_pct': 65}}


def enable_validated_execution_model(monkeypatch):
    """Isolate quote/collection tests from the independently tested model gate."""
    from src.services.yao_scout.signal_learning import SignalLearningService
    original = SignalLearningService.rank_candidates
    def rank(self, rows, now):
        return [{**row, 'prediction': {'taskId': 'entry_today_exit_next_session',
            'status': 'active', 'entryEligible': True, 'expectedNetReturn': .01, 'utility': .01,
            'winProbability': .7}}
                for row in original(self, rows, now)]
    monkeypatch.setattr(SignalLearningService, 'rank_candidates', rank)


def test_morning_formal_order_keeps_t1_priority_when_t0_research_rejects(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    start = datetime(2026, 9, 14, 9, 40, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    service.yao._fetch_snapshot = lambda: pd.DataFrame([
        {'code': code, 'name': '测试股', 'price': 10} for code in ('600001', '600002')])
    collect = service.context_provider.collect
    def context(now):
        result = collect(now)
        financials = result['tables'][0]['rows']
        financials.append({**financials[0], '股票代码': '600002'})
        return result
    service.context_provider.collect = context
    original_t1 = SignalLearningService.rank_candidates
    original_t0 = SignalLearningService.rank_morning_candidates
    monkeypatch.setattr(SignalLearningService, 'rank_candidates',
        lambda self, rows, now: sorted(original_t1(self, rows, now), key=lambda row: row['code'], reverse=True))
    def opposing_t0(self, rows, now, **kwargs):
        result = original_t0(self, rows, now, **kwargs)
        for row in result:
            row['morningSignal'].update(selectionEligible=False, researchEligibleAtDecision=False)
        return sorted(result, key=lambda row: row['code'])
    monkeypatch.setattr(SignalLearningService, 'rank_morning_candidates', opposing_t0)
    result = service.run('0940', official=True, top_n=1)
    assert [row['code'] for row in result['morningResearch']] == ['600001', '600002']
    assert [row['code'] for row in result['precisionResearch']] == ['600002', '600001']
    assert [row['code'] for row in result['candidates']] == ['600002']
    chosen = result['candidates'][0]
    assert chosen['primaryAdmission']['eligible'] is True
    assert chosen['precisionDecision']['profiles']['regular']['entryEligible'] is True
    assert chosen['entryExecution']['entryEligible'] is True
    assert chosen['morningSignal']['researchEligibleAtDecision'] is False


def test_active_execution_model_without_win_probability_keeps_primary_empty(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    start = datetime(2026, 9, 14, 13, 17, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    original_rank = SignalLearningService.rank_candidates
    def incomplete_prediction(self, rows, now):
        ranked = original_rank(self, rows, now)
        for row in ranked:
            row['prediction'].pop('winProbability')
            row['prediction']['bigMoveProbability'] = .9
        return ranked
    monkeypatch.setattr(SignalLearningService, 'rank_candidates', incomplete_prediction)
    result = service.run('live')
    assert result['precisionResearch'][0]['primaryAdmission']['eligible'] is True
    assert result['precisionResearch'][0]['entryExecution']['priceEligible'] is True
    assert result['precisionResearch'][0]['entryExecution']['modelEligible'] is False
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())


def test_complete_morning_evidence_without_validated_model_keeps_primary_empty(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    start = datetime(2026, 9, 14, 9, 40, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    # Isolate the archive's independently tested verification result. All live
    # evidence, profile and morning model admission gates remain production code.
    monkeypatch.setattr(SignalLearningService, '_learning_quality', lambda self, row, published: {
        'eligible': True, 'qualityKnownAt': published.isoformat(),
        'checkedAt': published.isoformat(), 'immutable': True})
    result = service.run('0940', official=True)
    assert result['primaryObjective'] == 'morning_signal_to_same_session_close'
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())
    assert result['morningResearch']
    research = result['morningResearch'][0]
    assert research['primaryAdmission']['eligible'] is True
    assert research['precisionDecision']['profiles']['regular']['entryEligible'] is True
    assert research['morningSignal']['researchEligibleAtDecision'] is True
    assert research['morningSignal']['learningStatus'] == 'rules_unvalidated'
    assert research['morningSignal']['selectionEligible'] is False
    assert research['morningSignal']['ruleBaselineSelected'] is True
    assert research['morningSignal']['upProbability'] is None
    assert research['firstReferenceTime'] is None
    assert result['nextDayWatchlist']  # Separate research cannot refill main picks.
    assert result['learningLedger']['selectedCount'] == 0


def test_official_preopen_preparation_keeps_target_morning_objective(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 9, 20, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    result = service.run('0940', official=True)
    target = start.replace(minute=40)
    assert events[0] == ('snapshot', start)
    assert tick[0] == target
    assert result['delivery']['target_at'] == target.isoformat()
    assert result['primaryObjective'] == 'morning_signal_to_same_session_close'
    assert result['morningAssessment']['queue'] == '0940'
    assert result['morningResearch']
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())


def test_scan_crossing_morning_end_preserves_research_and_does_not_recommend(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    start = datetime(2026, 9, 14, 11, 29, 50, tzinfo=base.TZ)
    service, tick, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    original_rank = SignalLearningService.rank_candidates
    def slow_rank(self, *args, **kwargs):
        rows = original_rank(self, *args, **kwargs)
        tick[0] += timedelta(seconds=20)
        return rows
    monkeypatch.setattr(SignalLearningService, 'rank_candidates', slow_rank)
    result = service.run('live')
    assert tick[0] == start + timedelta(seconds=20)
    assert result['primaryObjective'] == 'morning_signal_to_same_session_close'
    assert result['morningAssessment']['learningStatuses'] == ['morning_window_closed']
    assert result['morningResearch']
    assert all(row['morningSignal']['selectionEligible'] is False for row in result['morningResearch'])
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())


def test_final_primary_source_expiry_updates_profile_counts_even_with_fresh_quote(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    from src.services.yao_scout.primary_policy import evaluate_primary_admission
    start = datetime(2026, 9, 14, 13, 17, tzinfo=base.TZ)
    service, tick, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    original_quote = service.quote_fetcher
    service.quote_fetcher = lambda code: {**original_quote(code),
        'active_flow_as_of': (start - timedelta(seconds=29)).isoformat()}
    original_finish, before_finish = service._finish_quotes, []
    def observe_finish(run, quality):
        before_finish.append(len(run['candidates']))
        return original_finish(run, quality)
    service._finish_quotes = observe_finish
    original_rank = SignalLearningService.rank_next_day_candidates
    def slow_next_day_rank(self, *args, **kwargs):
        rows = original_rank(self, *args, **kwargs)
        tick[0] += timedelta(seconds=2)
        return rows
    monkeypatch.setattr(SignalLearningService, 'rank_next_day_candidates', slow_next_day_rank)
    result = service.run('live')
    assert before_finish == [1, 1]  # Evidence passed before the final source expiry.
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    assert result['dataQuality']['minute_coverage']['usable'] == 1
    admission = evaluate_primary_admission(result['precisionResearch'][0], tick[0])
    assert admission['eligible'] is False
    assert admission['missingReasons'] == ['active_flow_classification_missing_or_stale']
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())
    assert result['dataQuality']['precision_policy']['profile_counts'] == {
        profile: len(rows) for profile, rows in result['profileCandidates'].items()}


@pytest.mark.parametrize('slot,hour,minute', [('0920', 9, 20), ('1030', 10, 30), ('1455', 14, 55)])
def test_scheduled_local_scan_prepares_early_then_refreshes_at_target(monkeypatch, tmp_path, slot, hour, minute):
    start = datetime(2026, 9, 14, hour, minute, tzinfo=base.TZ) - timedelta(minutes=10)
    service, tick, events, saved = make_service(monkeypatch, start, tmp_path)
    result = service.run(slot, official=True)
    target = start + timedelta(minutes=10)
    assert events == ([('snapshot', start), ('quote', target)] if slot == '0920' else
                      [('snapshot', start), ('snapshot', target-timedelta(seconds=90)), ('quote', target)])
    assert result['delivery']['decision_at'] == target.isoformat()
    records = result['premarketCandidates'] if slot == '0920' else result['candidates'] or result['precisionWatchlist']
    assert records[0]['quote']['provider_timestamp'] == target.isoformat()
    assert result['generatedAt'] == target.isoformat()
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    assert result['llmUsed'] is False and result['delivery']['received_at'] is None
    assert result['learningLedger']['officialCollectionEligible'] is (slot != '0920')
    # The official schedule permits collection; this timing fixture has no
    # complete research/lineage admission and therefore adds no training sample.
    assert result['learningLedger']['trainingEligibleCount'] == 0
    assert result['learningLedger']['trainingEligible'] is False
    assert result['learningLedger']['researchCount'] == (0 if slot == '0920' else 1)
    if slot == '0920':
        assert records[0]['status'] == 'premarket'
        assert not any(result['profileCandidates'].values())


def test_manual_live_refresh_does_not_wait_for_schedule(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start, tmp_path)
    result = service.run('live')
    assert tick[0] == start
    assert result['delivery']['target_at'] is None
    assert len(events) == 2


def test_emotion_archive_failure_does_not_abort_quote_and_risk_checks(monkeypatch, tmp_path):
    import sqlite3
    from src.services.yao_scout import research_factors
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, _, events, _ = make_service(monkeypatch, start, tmp_path)
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError('database locked')
    monkeypatch.setattr(research_factors.MarketEmotionArchive, 'observe', failed)
    result = service.run('live')
    assert result['marketEmotion']['status'] == 'unavailable'
    assert result['marketEmotion']['score'] is None
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    assert ('quote', start) in events


def test_market_snapshot_is_archived_at_receipt_before_slow_research(monkeypatch, tmp_path):
    from src.services.yao_scout import research_factors
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, tick, _, _ = make_service(monkeypatch, start, tmp_path)
    original = service.yao.history_fetcher
    def slow(*args, **kwargs):
        tick[0] += timedelta(minutes=6)
        return original(*args, **kwargs)
    service.yao.history_fetcher = slow
    captured = []
    def observe(self, snapshot, cutoff, *, received_at):
        captured.append((tick[0], cutoff, received_at))
        return {'status': 'insufficient', 'score': None, 'asOf': start.isoformat(), 'features': {}, 'gaps': []}
    monkeypatch.setattr(research_factors.MarketEmotionArchive, 'observe', observe)
    service.run('live')
    assert captured == [(start, start, start)]


def test_manual_progress_tracks_evidence_stages_and_usable_minutes(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    progress = []
    service.progress_callback = lambda value, message: progress.append((value, message))
    result = service.run('live')
    assert [value for value, _ in progress] == [12, 20, 48, 55, 57, 60, 72, 80, 88, 94]
    assert result['dataQuality']['minute_coverage'] == {'requested': 1, 'usable': 1, 'missing': 0}
    assert 'minute_warmup' not in result['dataQuality']['stageTimingsMs']
    assert set(result['dataQuality']['stageTimingsMs']) >= {'snapshot', 'daily_history', 'minute_evidence', 'final_quotes', 'evaluation'}
    from src.services.yao_scout.local_algorithm import algorithm_contract, is_entry_eligible
    assert result['algorithm'] == algorithm_contract()
    for candidate in result['candidates']:
        assert candidate['algorithmContractId'] == result['algorithm']['contractId']
        assert is_entry_eligible(candidate)


def test_slow_context_evaluation_finishes_before_final_quote(monkeypatch, tmp_path):
    from src.services import akshare_context
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start, tmp_path)
    original = akshare_context.for_stock
    def slow_context(*args, **kwargs):
        result = original(*args, **kwargs)
        tick[0] += timedelta(seconds=45)
        return result
    monkeypatch.setattr(akshare_context, 'for_stock', slow_context)
    result = service.run('live')
    assert events[-1] == ('quote', start + timedelta(seconds=45))
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    assert result['dataQuality']['stageTimingsMs'].get('context_evaluation') is not None


def test_total_minute_stage_failure_is_reported_even_with_fresh_quotes(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    original = local.bounded_fetch_map
    def fail_minutes(fetch, items, **kwargs):
        return ([], list(items)) if fetch.__name__ == 'minutes_for' else original(fetch, items, **kwargs)
    monkeypatch.setattr(local, 'bounded_fetch_map', fail_minutes)
    result = service.run('live')
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    assert result['dataQuality']['minute_coverage'] == {'requested': 1, 'usable': 0, 'missing': 1}
    assert result['dataQuality']['stageIncompleteCounts']['minute_evidence'] == 1
    from src.services.stock_king_display import displayable_run
    # Current day/daily structure can support explicitly separate T+1 research;
    # it must not upgrade the failed intraday entry checks.
    assert not result['candidates']
    assert result['nextDayWatchlist']
    assert not result['nextDayWatchlist'][0]['nextDaySignal']['executionEligible']
    assert displayable_run(result)


def test_local_daily_history_reuses_software_chart_provider(monkeypatch, tmp_path):
    from src.services import software_market
    start = datetime(2026, 9, 14, 10, 30, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    service.yao.history_fetcher = lambda *a,**k: pytest.fail('legacy daily source used')
    dates = pd.bdate_range(end=start.date()-timedelta(days=1),periods=60)
    class Client:
        available=True
        def enrich_snapshot_quotes(self, frame, **kwargs):
            return frame
        def bars(self,code,period,count):
            assert (code,period,count)==('600001','101',260)
            return {'source':'fixture','adjustment':'none','bars':[
                {'end':day.date().isoformat()+'T15:00:00+08:00',
                 'open':9.7,'high':10.8,'low':9.5,'close':9.8}
                for day in dates]}
    monkeypatch.setattr(software_market.SoftwareMarketClient,'from_environment',lambda:Client())
    result = service.run('live')
    rows = result['candidates'] or result['precisionResearch'] or result['evidenceInsufficient']
    assert rows
    evidence = {row['key']:row for row in rows[0]['indicatorEvidence']}
    assert 'software:go/fixture' in evidence['ma5']['source']


def test_expired_slot_never_fetches_or_waits(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 14, 57, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start, tmp_path)
    result = service.run('1455')
    assert result['status'] == 'expired' and events == []


def test_default_provider_is_public_gateway_not_old_dsa_manager():
    service = base.DailyOpportunityService(SimpleNamespace(db=None))
    assert service.quote_fetcher is base.get_public_realtime_quote
    assert service.batch_quote_fetcher is base.get_public_market_quotes


def test_batch_partial_failure_preserves_good_stock(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 14, 55, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    service.quote_fetcher = lambda code: {'code': code} if code == '600001' else (_ for _ in ()).throw(ValueError())
    assert service._quotes(['600001', '600002']) == {'600001': {'code': '600001'}, '600002': {}}


def test_completion_rechecks_older_quote(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 14, 55, tzinfo=base.TZ)
    service, tick, _, _ = make_service(monkeypatch, start, tmp_path)
    result = service.run('1455')
    tick[0] += timedelta(seconds=31)
    service._finish_quotes(result, result['dataQuality'])
    assert result['candidates'] == []
    assert result['precisionWatchlist'][0]['status'] == 'data_insufficient'
    assert result['dataQuality']['quote_coverage']['fresh'] == 0
    assert not any(result['profileCandidates'].values())
    assert result['delivery']['late_seconds'] == 31
    tick[0] = start.replace(minute=57)
    service._finish_quotes(result, result['dataQuality'])
    assert result['status'] == 'expired'


def test_missing_timestamp_stays_missing_and_reviewable(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 10, 30, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path, fetch=lambda code: {'code': code, 'price': 10, 'fetched_at': start.isoformat()})
    result = service.run('1030')
    assert result['dataQuality']['quote_coverage']['source_time_min'] is None
    assert result['candidates'] == []
    assert result['evidenceInsufficient'][0]['status'] == 'evidence_insufficient'


def test_scan_reads_prior_review_reminders_without_changing_rank(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 10, 30, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    monkeypatch.setattr(local, 'read_observation_reminders_batch',
                        lambda db, codes, now: {code: ['历史缺口需重新核验'] for code in codes})
    result = service.run('1030')
    candidate = result['precisionResearch'][0]
    assert candidate['observationReminders'] == ['历史缺口需重新核验']
    assert '历史缺口需重新核验' in candidate['risks']
    assert candidate['code'] == '600001' and result['llmUsed'] is False
    assert not result['candidates']  # Research reminders cannot bypass the morning model gate.


def test_daily_review_wires_observations_and_new_ledger(monkeypatch, tmp_path):
    from src.services.yao_scout import minute_history
    start = datetime(2026, 9, 14, 15, 30, tzinfo=base.TZ)
    service, _, events, _ = make_service(monkeypatch, start, tmp_path)
    service.db.save_yao_adaptive_state = lambda *a: None
    monkeypatch.setattr(local, 'review_local_observations', lambda *a, **kw: {'status': 'reviewed', 'verified_count': 2})
    captured=[]
    monkeypatch.setattr(minute_history,'archive_universe',lambda codes,*a,**kw: captured.extend(codes) or {'universe':len(codes),'updated':len(codes)})
    result = service.run('review')
    assert result['observationReview']['verified_count'] == 2
    assert result['learningReview']['updated'] == 0
    assert events == [('snapshot',start)] and result['llmUsed'] is False
    assert captured==['600001'] and result['minuteArchiveMaintenance']['updated']==1


def test_review_is_saved_even_when_whole_market_archive_fails(monkeypatch, tmp_path):
    from src.services.yao_scout import minute_history, signal_learning
    start = datetime(2026, 9, 14, 15, 30, tzinfo=base.TZ)
    service, _, _, saved = make_service(monkeypatch, start, tmp_path)
    service.db.save_yao_adaptive_state = lambda *a: None
    calls = []
    monkeypatch.setattr(signal_learning.SignalLearningService, 'review_requirements',
                        lambda *a: {'600002': ['2026-09-14']})
    monkeypatch.setattr(signal_learning.SignalLearningService, 'review_due',
                        lambda *a: calls.append('review') or {'updated': 1})
    monkeypatch.setattr(local, 'review_local_observations', lambda *a, **kw: {'status': 'reviewed'})
    def archive(codes, *a, **kw):
        calls.append('priority' if not kw.get('include_registered', True) else 'whole_market')
        assert any(row.get('learningReview', {}).get('updated') == 1 for row in saved)
        if kw.get('include_registered', True):
            raise RuntimeError('provider failed')
        assert list(codes) == ['600002'] and kw['count'] == 1970 and kw['timeout'] == 120
        assert kw['required_days'] == {'600002': ['2026-09-14']}
        return {'updated': 1}
    monkeypatch.setattr(minute_history, 'archive_universe', archive)
    result = service.run('review')
    assert calls == ['review', 'priority', 'review', 'whole_market']
    assert result['learningReview']['updated'] == 1
    assert result['minuteArchiveMaintenance']['status'] == 'failed'
    assert saved[-1]['reviewProgress']['stage'] == 'completed'


def test_expanded_prescreen_researches_every_stock_in_a_small_universe(monkeypatch, tmp_path):
    from src.services import software_market
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    codes = [f'{600001 + i:06d}' for i in range(45)]
    frame = pd.DataFrame({'code': codes, 'name': ['测试股'] * len(codes),
                          'price': [10] * len(codes), 'amount': list(range(45, 0, -1))})
    service.yao._fetch_snapshot = lambda: frame
    monkeypatch.setattr(software_market.SoftwareMarketClient, 'from_environment',
                        lambda: SimpleNamespace(available=False))
    history_codes, minute_codes = [], []
    original_history, original_minutes = service.yao.history_fetcher, service.minute_fetcher
    def history(code, **kwargs):
        history_codes.append(code)
        return original_history(code, **kwargs)
    def minutes(code, *args, **kwargs):
        minute_codes.append(code)
        return original_minutes(code, *args, **kwargs)
    service.yao.history_fetcher, service.minute_fetcher = history, minutes
    result = service.run('live')
    assert set(history_codes) == set(minute_codes) == set(codes)
    assert result['dataQuality']['research_universe_count'] == 45
    assert result['dataQuality']['research_count'] == 45
    assert result['dataQuality']['research_limit'] == 45
    assert result['dataQuality']['research_policy'] == 'mainboard_10pct_min300_v1'
    assert result['dataQuality']['deep_research_count'] == 45
    budgets = result['dataQuality']['stageBudgetsSeconds']
    assert budgets['daily_history'] == 25
    assert budgets['minute_evidence'] == 30
    assert budgets['fund_evidence'] == 20
    assert result['dataQuality']['minute_coverage'] == {'requested': 45, 'usable': 45, 'missing': 0}
    assert result['learningLedger']['researchCount'] == 45
    assert all(len(rows) <= 5 for rows in result['profileCandidates'].values())


def test_quote_batches_run_concurrently_and_one_failure_preserves_other_batches(monkeypatch):
    from threading import Barrier, Lock
    codes = [f'{600001 + i:06d}' for i in range(45)]
    barrier, lock = Barrier(3, timeout=3), Lock()
    entered, passed = [], []
    def fetch_batch(batch_codes):
        with lock:
            entered.append(tuple(batch_codes))
        barrier.wait()
        with lock:
            passed.append(tuple(batch_codes))
        if codes[20] in batch_codes:
            raise ValueError('fixture provider batch failure')
        return {'quotes': [{'code': code, 'quote': {'code': code, 'price': 10}}
                           for code in [*batch_codes, '601999']]}
    monkeypatch.setattr(base, 'adapt_public_quote', lambda batch, row: row['quote'])
    service = base.DailyOpportunityService(SimpleNamespace(db=None), batch_quote_fetcher=fetch_batch)
    quotes = service._quotes(codes)
    assert len(entered) == len(passed) == 3
    assert sorted(len(batch) for batch in entered) == [5, 20, 20]
    assert set(quotes) == set(codes)
    assert all(quotes[code] == {} for code in codes[20:40])
    assert all(quotes[code] == {'code': code, 'price': 10} for code in codes[:20] + codes[40:])


def test_optional_evidence_cannot_extend_global_scan_deadline(monkeypatch, tmp_path):
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start, tmp_path)
    original_minutes, original_funds = service.minute_fetcher, service.fund_fetcher
    original_sectors = service.sector_fetcher
    service.sector_fetcher = lambda *args: {
        code: {**packet, 'confirmed': True} for code, packet in original_sectors(*args).items()}
    fetched_minutes = []
    def minutes(*args):
        fetched_minutes.append(tick[0])
        return original_minutes(*args)
    def slow_funds(*args):
        packet = original_funds(*args)
        tick[0] += timedelta(seconds=241)
        return packet
    service.minute_fetcher, service.fund_fetcher = minutes, slow_funds
    result = service.run('live')
    assert fetched_minutes == [start]
    assert result['dataQuality']['minute_refresh_count'] == 1
    assert not any(event[0] == 'quote' for event in events)
    assert not result['candidates']
    assert '全局时间上限' in result['message']


@pytest.mark.parametrize('minute_age, eligible', [(299, True), (300, False)])
def test_completion_requires_fresh_minutes_even_when_quotes_are_fresh(monkeypatch, tmp_path, minute_age, eligible):
    start = datetime(2026, 9, 14, 13, 17, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    result = service.run('live')
    assert result['candidates']
    collections = [result.get(key, []) for key in
                   ('candidates', 'controls', 'precisionResearch', 'precisionWatchlist', 'windvanes', 'evidenceInsufficient')]
    collections.extend(result['profileCandidates'].values())
    for rows in collections:
        for candidate in rows:
            candidate['minuteSourceTime'] = (start - timedelta(seconds=minute_age)).isoformat()
    service._finish_quotes(result, result['dataQuality'])
    assert bool(result['candidates']) is eligible
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    if not eligible:
        assert not any(result['profileCandidates'].values())
        candidate = result['precisionWatchlist'][0]
        assert candidate['evidenceEligible'] is False
        assert any('分钟价格结构已失效' in gap for gap in candidate['data_quality']['gaps'])


def test_official_expanded_stage_budgets_stay_inside_delivery_deadline(monkeypatch, tmp_path):
    from src.services import software_market
    target = datetime(2026, 9, 14, 10, 30, tzinfo=base.TZ)
    service, tick, _, _ = make_service(monkeypatch, target + timedelta(seconds=60), tmp_path)
    frame = pd.DataFrame({'code': [f'{600001 + i:06d}' for i in range(45)],
                          'name': ['测试股'] * 45, 'price': [10] * 45})
    service.yao._fetch_snapshot = lambda: frame
    monkeypatch.setattr(software_market.SoftwareMarketClient, 'from_environment',
                        lambda: SimpleNamespace(available=False))
    original_collect = service.context_provider.collect
    def collect(now):
        tick[0] += timedelta(seconds=15)
        return original_collect(now)
    service.context_provider.collect = collect
    original_sectors = service.sector_fetcher
    def sectors(*args, **kwargs):
        packet = original_sectors(*args, **kwargs)
        tick[0] += timedelta(seconds=30)
        return packet
    service.sector_fetcher = sectors
    original_map, observed_budgets = local.bounded_fetch_map, []
    def fetch_map(fetch, items, **kwargs):
        observed_budgets.append((kwargs['timeout'], (target + timedelta(seconds=120) - tick[0]).total_seconds()))
        return original_map(fetch, items, **kwargs)
    monkeypatch.setattr(local, 'bounded_fetch_map', fetch_map)
    service.sleeper = lambda seconds: pytest.fail('a late official scan must not wait for its target')
    result = service.run('1030', official=True)
    assert result['official'] is True
    assert result['delivery']['late_seconds'] == 105
    assert observed_budgets and all(0 <= budget <= remaining for budget, remaining in observed_budgets)
    budgets = result['dataQuality']['stageBudgetsSeconds']
    assert budgets['daily_history'] == 25
    assert budgets['minute_evidence'] == 30
    # Funds can start alongside sectors before the latter advances the clock.
    # Their budget remains bounded and never consumes the final quote reserve.
    assert 0 <= budgets['fund_evidence'] <= 20
    assert budgets['final_quotes'] == 15


def test_timed_out_quote_group_keeps_already_completed_groups(monkeypatch, tmp_path):
    from threading import Event
    from src.services import software_market
    start = datetime(2026, 9, 14, 13, 17, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    codes = [f'{600001 + i:06d}' for i in range(45)]
    frame = pd.DataFrame({'code': codes, 'name': ['测试股'] * 45, 'price': [10] * 45})
    service.yao._fetch_snapshot = lambda: frame
    monkeypatch.setattr(software_market.SoftwareMarketClient, 'from_environment',
                        lambda: SimpleNamespace(available=False))
    release, slow_started = Event(), Event()
    def grouped_quotes(group):
        if codes[20] in group:
            slow_started.set()
            release.wait(timeout=1)
        return {code: service.quote_fetcher(code) for code in group}
    service._quotes = grouped_quotes
    original_map = local.bounded_fetch_map
    def fetch_map(fetch, items, **kwargs):
        if fetch is grouped_quotes:
            kwargs['timeout'] = .05
        return original_map(fetch, items, **kwargs)
    monkeypatch.setattr(local, 'bounded_fetch_map', fetch_map)
    try:
        result = service.run('live')
        assert slow_started.is_set()
        assert result['dataQuality']['stageIncompleteCounts']['final_quotes'] == 1
        assert result['dataQuality']['quote_coverage']['requested'] == 45
        assert result['dataQuality']['quote_coverage']['fresh'] == 25
        assert result['dataQuality']['quote_coverage']['missing'] == 20
        assert result['candidates']
        assert not ({row['code'] for row in result['candidates']} & set(codes[20:40]))
    finally:
        release.set()
