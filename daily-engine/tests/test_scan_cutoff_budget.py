"""Regression checks for scan latency, factor alignment and learned selection."""
from datetime import datetime, timedelta, timezone
from time import monotonic
import json

import pytest

from src.services.yao_scout import sector_fund_evidence as sector
from src.services.yao_scout.minute_history import MinuteArchive
from src.services.yao_scout.recommendation_evidence import explain_candidate
from tests.test_complete_market_evidence import NOW, bars
from tests.test_local_quote_integration import make_service
from tests.test_recommendation_evidence import quote


def test_sector_reuses_same_window_archive_without_network(monkeypatch, tmp_path):
    archive = MinuteArchive(tmp_path/'minute_history')
    codes = ['600001', '600002', '600003', '600004']
    for code in codes:
        archive.save(code, bars(), NOW)
    sector._recent_sector_bars.clear()
    monkeypatch.setattr(sector, 'fetch_recent_bars', lambda *a, **k: pytest.fail('cached peer fetched again'))
    prepared = ({codes[0]: {'code':'board','name':'fixture'}}, {'board':codes})
    result = sector.fetch_sector_batch([codes[0]], {codes[0]: {'asOf':NOW.isoformat(),
        'metrics':{'speed_5m_pct':.1}}}, NOW, tmp_path, prepared=prepared)
    assert result[codes[0]]['peerCount'] == 3
    assert result[codes[0]]['metrics']['sector_return_5m_pct'] == 0


def test_expired_fund_deadline_does_not_start_request():
    result = sector.fetch_fund_evidence('600001', NOW, deadline=monotonic()-1,
        getter=lambda *a, **k: pytest.fail('expired network request'))
    assert result['metrics'] == {}
    assert result['reasonCodes'] == ['fund_deadline_exhausted']


def test_equivalent_timestamp_and_negative_factor_are_available():
    end = NOW-timedelta(minutes=1)
    minute = {'asOf':end.isoformat(), 'requestedCutoffAt':NOW.isoformat(), 'metrics':{}}
    result = explain_candidate({'code':'600001'}, quote(), None, minute, NOW, 'live',
        sector={'asOf':end.astimezone(timezone.utc).isoformat(),
                'metrics':{'sector_return_5m_pct':-.5, 'sector_relative_5m_pct':-.1}},
        funds={'asOf':end.isoformat(), 'metrics':{'main_net_flow_3m':-100}})
    assert result['factorAvailability']['S']['available'] is True
    assert result['factorAvailability']['F']['available'] is True
    assert result['factorAvailability']['F']['reasonCodes'] == []
    evidence = {row['key']:row for row in result['indicatorEvidence']}
    assert evidence['main_net_flow_3m']['value'] == -100


def test_final_minute_precedes_sector_and_funds_with_one_cutoff(monkeypatch, tmp_path):
    service, tick, _, _ = make_service(monkeypatch, NOW, tmp_path)
    events, original = [], service.minute_fetcher
    def minutes(code, cutoff, cache):
        events.append(('minute',cutoff))
        tick[0] += timedelta(seconds=45)
        return {**original(code,cutoff,cache), 'asOf':(cutoff-timedelta(minutes=1)).isoformat()}
    def sectors(codes, packets, cutoff, cache):
        events.append(('sector',cutoff))
        return {code:{'asOf':packets[code]['asOf'], 'metrics':{'sector_return_5m_pct':-.5,
            'sector_relative_5m_pct':-.2,'sector_breadth':20}} for code in codes}
    def funds(code, cutoff):
        events.append(('fund',cutoff))
        return {'asOf':cutoff, 'metrics':{'main_net_flow_3m':-100}}
    service.minute_fetcher, service.sector_fetcher, service.fund_fetcher = minutes, sectors, funds
    result = service.run('live')
    assert events[0] == ('minute',NOW)
    assert set(events[1:]) == {('sector',NOW),('fund',(NOW-timedelta(minutes=1)).isoformat())}
    assert result['dataQuality']['independent_evidence']['sector_source_available'] == 1
    assert result['dataQuality']['independent_evidence']['fund_3m_available'] == 1
    assert result['dataQuality']['factorCutoffAt'] == NOW.isoformat()


def test_slow_sector_does_not_delay_independent_same_window_fund_collection(monkeypatch, tmp_path):
    from threading import Event
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    fund_started = Event()
    observed = []
    original_sector, original_fund = service.sector_fetcher, service.fund_fetcher
    def sectors(*args, **kwargs):
        observed.append(fund_started.wait(timeout=1))
        return original_sector(*args, **kwargs)
    def funds(*args, **kwargs):
        fund_started.set()
        return original_fund(*args, **kwargs)
    service.sector_fetcher, service.fund_fetcher = sectors, funds
    result = service.run('live')
    assert observed == [True]
    assert result['dataQuality']['parallelEvidenceCollection']['enabled'] is True
    assert result['dataQuality']['factorCutoffAt'] == NOW.isoformat()


def test_daily_history_cache_avoids_repeating_provider_io(monkeypatch, tmp_path):
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    calls, original = [], service.yao.history_fetcher
    service.yao.history_fetcher = lambda code, **kwargs: calls.append(code) or original(code, **kwargs)
    service.run('live')
    service.run('live')
    assert calls == ['600001']


def test_daily_cache_never_promotes_an_intraday_bar_to_completed_close(monkeypatch, tmp_path):
    import pandas as pd
    from src.services import software_market
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    class Client:
        available = True
        def bars(self, code, period, count):
            return {'source':'fixture','adjustment':'none','bars':[
                {'end':day.isoformat(),'open':10,'close':10,'high':11,'low':9}
                for day in pd.bdate_range(end=NOW.date(),periods=60)]}
    monkeypatch.setattr(software_market.SoftwareMarketClient,'from_environment',lambda:Client())
    service.run('live')
    payload = json.loads((tmp_path/'daily_history'/'scan-cache-v1'/'600001').read_text(encoding='utf-8'))
    assert payload['bars']
    assert all(row['date'][:10] < NOW.date().isoformat() for row in payload['bars'])


def test_scan_never_requests_minute_backfill_and_reports_missing_factors(monkeypatch, tmp_path):
    from src.services.yao_scout import minute_history
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    original, calls = service.minute_fetcher, []
    def fetch(code, cutoff, cache, **kwargs):
        calls.append(kwargs)
        return original(code, cutoff, cache)
    service.minute_fetcher = None
    monkeypatch.setattr(minute_history, 'fetch_bar_evidence', fetch)
    result = service.run('live')
    assert len(calls) == 1
    assert calls[0]['backfill'] is False and calls[0]['software_count'] == 240
    assert calls[0]['deadline'] > monotonic()-30
    candidate = (result['candidates'] or result['controls'])[0]
    assert candidate['factorAvailability']['T']['available'] is False
    assert 'float_shares' in candidate['factorAvailability']['T']['missing_reason']
    assert candidate['factorAvailability']['H']['available'] is False
    assert 'concept_membership' in candidate['factorAvailability']['H']['missing_reason']
    assert candidate['dataLineage']['status'] == 'unknown'


def test_next_day_model_rejection_cannot_be_filled_by_rule_winner(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    def reject(self, candidates, as_of=None):
        return [{**row, 'nextDaySignal':{**row['nextDaySignal'], 'selectionEligible':False,
            'learningStatus':'active', 'expectedPriceReturn':-.01}} for row in candidates]
    monkeypatch.setattr(SignalLearningService,'rank_next_day_candidates',reject,raising=False)
    result = service.run('live')
    assert result['nextDayResearch']['learningRejectedCount'] > 0
    assert result['nextDayWatchlist'] == []
    assert result['nextDayContinuationWatchlist'] == []
    assert all(not row.get('nextDaySignal',{}).get('selected') for row in result['controls'])


def _identity_bars(code, at, *, full_day=False):
    identity = {'status':'provider_reply_checked','requested_code':code,'response_code':code,
        'requested_market':1,'response_market':1,'period':'1','page_cursor_checked':True}
    ends = ([at.replace(hour=9,minute=31)+timedelta(minutes=i) for i in range(120)]
            + [at.replace(hour=13,minute=1)+timedelta(minutes=i) for i in range(120)]
            if full_day else [at-timedelta(minutes=i) for i in range(12)])
    return [{'end':end.isoformat(),'open':10.,'close':10.,'high':10.01,'low':9.99,
        'amount_cny':1000.,'volume_shares':100.,'source':'tdx-mac','identity_evidence':identity}
        for end in ends]


def test_live_final_quote_automatically_verifies_the_frozen_minute_prefix(monkeypatch, tmp_path):
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    original = service.minute_fetcher
    archive = MinuteArchive(tmp_path/'minute_history')
    def minutes(code, cutoff, cache):
        packet = original(code,cutoff,cache)
        archive.save(code,_identity_bars(code,datetime.fromisoformat(packet['asOf'])),cutoff)
        return {**packet,'dataLineage':archive.quality(code,cutoff)}
    service.minute_fetcher = minutes
    result = service.run('live')
    assert result['dataQuality']['independentMinuteVerification']['verified'] == 1
    candidate = (result['candidates'] or result['controls'])[0]
    assert candidate['dataLineage']['status'] == 'verified'
    assert candidate['dataLineage']['sessions'][0]['frozenThrough'] == (NOW-timedelta(minutes=1)).isoformat()


def test_review_automatically_verifies_daily_close_before_settling_labels(monkeypatch, tmp_path):
    from src.services.yao_scout import minute_history, signal_learning, local_opportunities
    close = NOW.replace(hour=15,minute=30)
    service, _, _, _ = make_service(monkeypatch,close,tmp_path)
    archive = MinuteArchive(tmp_path/'minute_history')
    archive.save('600001',_identity_bars('600001',close,full_day=True),close)
    assert archive.quality('600001',close)['status'] == 'unknown'
    service.quote_fetcher = lambda code: {'code':code,'source':'tencent','open_price':10.,
        'price':10.,'high':10.01,'low':9.99,'pre_close':9.8,
        'provider_timestamp':close.replace(minute=0).isoformat()}
    observed = []
    monkeypatch.setattr(signal_learning.SignalLearningService,'review_requirements',
        lambda *a: {'600001':[close.date().isoformat()]})
    monkeypatch.setattr(signal_learning.SignalLearningService,'review_due',
        lambda *a: observed.append(archive.quality('600001',close)['status']) or {'updated':0})
    monkeypatch.setattr(minute_history,'archive_universe',lambda *a,**kw: {'updated':1})
    monkeypatch.setattr(local_opportunities,'review_local_observations',lambda *a,**kw: {'status':'reviewed'})
    result = service._persist_run({'scanSlot':'review','status':'audit_only'})
    assert observed == ['unknown','verified','verified']
    assert result['priorityIndependentVerification']['verified'] == 1
    assert result['archiveIndependentVerification']['verified'] == 1


@pytest.mark.parametrize('slow_stage', ['independent_verification','next_day_model'])
def test_expiration_during_final_verification_or_model_cannot_publish_fresh(monkeypatch, tmp_path, slow_stage):
    from src.services.yao_scout.signal_learning import SignalLearningService
    service, tick, _, saved = make_service(monkeypatch,NOW,tmp_path)
    if slow_stage == 'independent_verification':
        original = service._verify_archived_quotes
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            tick[0] += timedelta(seconds=31)
            return result
        monkeypatch.setattr(service,'_verify_archived_quotes',slow)
    else:
        original = SignalLearningService.rank_next_day_candidates
        def slow(self, candidates, as_of=None):
            result = original(self,candidates,as_of)
            tick[0] += timedelta(seconds=31)
            return result
        monkeypatch.setattr(SignalLearningService,'rank_next_day_candidates',slow)
    result = service.run('live')
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())
    assert result['nextDayWatchlist'] == result['nextDayContinuationWatchlist'] == []
    assert result['dataQuality']['quote_coverage']['fresh'] == 0
    assert result['dataQuality']['quote_coverage']['source_time_max'] == NOW.isoformat()
    assert result['delivery']['decision_at'] == tick[0].isoformat()
    assert result['portfolioPolicy']['plannedExposure'] == 0
    assert saved[-1]['portfolioPolicy']['plannedExposure'] == 0


def test_model_data_rejected_stays_out_of_every_precision_profile(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    service, _, _, saved = make_service(monkeypatch,NOW,tmp_path)
    original = SignalLearningService.rank_candidates
    def rejected(self, candidates, as_of=None, **kwargs):
        rows = original(self,candidates,as_of,**kwargs)
        for row in rows:
            row['prediction'].update(status='data_rejected',entryEligible=False)
        return rows
    monkeypatch.setattr(SignalLearningService,'rank_candidates',rejected)
    result = service.run('live')
    assert not result['candidates'] and not any(result['profileCandidates'].values())
    assert result['learningLedger']['selectedCount'] == 0
    assert result['controls'][0]['prediction']['status'] == 'data_rejected'
    assert saved[-1]['controls'][0]['prediction']['entryEligible'] is False
    assert result['portfolioPolicy'] == result['profilePortfolioPolicies']['regular']


def test_weekly_task_check_times_remain_readable_after_service_restart(monkeypatch, tmp_path):
    from src.services.yao_scout.signal_learning import SignalLearningService
    from src.services.yao_scout.morning_task import MORNING_QUEUES
    service, _, _, saved = make_service(monkeypatch,NOW,tmp_path)
    result = service._persist_run({'scanSlot':'weekly','status':'audit_only'})
    assert len(result['localTraining']['taskStates']) == 3 + len(MORNING_QUEUES)
    state = SignalLearningService(tmp_path).learning_state(NOW)
    assert len(state['taskStates']) == 3 + len(MORNING_QUEUES)
    assert all(row['lastLearningCheckAt'] == NOW.isoformat() for row in state['taskStates'])
    assert all(row['lastFitCompletedAt'] is None for row in state['taskStates'])
    assert saved[-1]['localTraining']['taskStates'] == result['localTraining']['taskStates']


def test_archive_initialization_cannot_overrun_final_verification_budget(monkeypatch, tmp_path):
    from threading import Event
    from src.services.yao_scout import minute_history
    service, _, _, _ = make_service(monkeypatch,NOW,tmp_path)
    MinuteArchive(tmp_path/'minute_history').save('600001',_identity_bars('600001',NOW),NOW)
    entered, release = Event(), Event()
    original = minute_history.MinuteArchive
    def slow_archive(directory):
        entered.set()
        release.wait(2)
        return original(directory)
    monkeypatch.setattr(minute_history,'MinuteArchive',slow_archive)
    try:
        result = service._verify_archived_quotes(['600001'],NOW,quotes={'600001':quote()},timeout=.05)
        assert entered.is_set() and not release.is_set()
        assert result['status'] == 'partial' and result['deferred'] == 1
        assert result['reason'] == 'independent_verification_deadline_exhausted'
    finally:
        release.set()
