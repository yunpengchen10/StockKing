"""Daily observations never refill formal picks; minute readiness is per stock."""
from datetime import datetime, timedelta
import pytest
from src.services.yao_scout.daily_opportunities import TZ
from tests.test_local_quote_integration import make_service, enable_primary_evidence, enable_validated_execution_model


def service_for(monkeypatch, tmp_path):
    service, tick, events, saved = make_service(monkeypatch,
        datetime(2026, 9, 14, 10, 30, tzinfo=TZ), tmp_path)
    service.selection_mode = 'auto'
    raw = service.yao.history_fetcher()
    raw.attrs['daily_adjustment'] = 'unadjusted'
    raw.attrs['daily_source'] = 'sina'
    def forbidden(*args, **kwargs):
        pytest.fail('initial daily selection must not fetch minute or minute sector/fund evidence')
    service.minute_fetcher = service.sector_fetcher = service.fund_fetcher = forbidden
    return service, tick, events, saved


def test_first_install_downloads_existing_daily_history_without_waiting(monkeypatch, tmp_path):
    service, _, _, _ = service_for(monkeypatch, tmp_path)
    assert not (tmp_path/'minute_history').exists()
    result = service.run('live')
    assert result['primarySelectionMode'] == 'daily_observation', result.get('message')
    assert result['dailyAssessment']['dailyUsableCount'] == 1
    assert result['dailyAssessment']['quoteUsableCount'] == 1
    assert result['dailyAssessment']['eventCoveragePassedCount'] == 1
    assert result['dailyCandidates'], result['dailyAssessment']
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())
    assert result['dailyCandidates'][0]['dailySignal']['taskId'] == 'daily_structure_to_next_session'
    assert result['dataQuality']['minute_coverage']['required'] is True
    assert not (tmp_path/'minute_history'/'minute-bars-v2.sqlite3').exists()
    assert result['historyPreparationCodes'] == ['600001']


def test_desktop_gateway_none_enum_is_explicit_unadjusted_daily_history(monkeypatch, tmp_path):
    service, _, _, _ = service_for(monkeypatch, tmp_path)
    service.yao.history_fetcher().attrs['daily_source'] = 'software:go/tdx-mac; adjustment=none'
    service.yao.history_fetcher().attrs['daily_adjustment'] = 'none'
    result = service.run('live')
    assert result['dailyAssessment']['dailyUsableCount'] == 1
    assert result['dailyCandidates'][0]['dailyPreviousSession']['priceBasis'] == 'unadjusted'


def test_explicit_unprepared_clock_keeps_daily_cold_start(monkeypatch, tmp_path):
    from src.services.yao_scout import history_preparation
    service, _, _, _ = service_for(monkeypatch, tmp_path)
    monkeypatch.setattr(history_preparation, 'read_preparation_status', lambda *a: {
        'targetSession':'2026-09-14', 'ready':1, 'requested':1,
        'details':{'600001':{'ready':True, 'fullSessionReady':False, 'baselineClock':'10:29:00',
                             'readyClocks':['10:29:00']}}})
    result = service.run('live')
    assert result['dailyCandidates']
    assert result['candidates'] == []
    assert result['primarySelectionMode'] == 'daily_observation'


def test_daily_branch_keeps_missing_quotes_as_missing_not_filled(monkeypatch, tmp_path):
    service, _, _, _ = service_for(monkeypatch, tmp_path)
    service.quote_fetcher = lambda _: {}
    result = service.run('live')
    assert result['dailyAssessment']['dailyUsableCount'] == 1
    assert result['dailyAssessment']['quoteUsableCount'] == 0
    assert result['candidates'] == []
    assert 'quote_or_provider_price_limits_unverified' in result['dailyAssessment']['excludedCounts']


def test_daily_official_scan_refreshes_pool_and_quote_at_target(monkeypatch, tmp_path):
    service, tick, events, _ = service_for(monkeypatch, tmp_path)
    tick[0] = tick[0].replace(minute=20)
    result = service.run('1030', official=True)
    target = tick[0].replace(minute=30)
    assert result['dataQuality']['final_universe_refreshed'] is True
    assert result['delivery']['decision_at'] == target.isoformat()
    assert events[-1] == ('quote', target)
    assert result['dailyCandidates']
    assert result['candidates'] == []


def test_daily_observation_collects_whole_market_before_last_quote(monkeypatch, tmp_path):
    from src.services import software_market
    service, tick, events, _ = service_for(monkeypatch, tmp_path)
    raw = service.yao.history_fetcher().copy()
    class Gateway:
        available = True
        def bars(self, code, **kwargs):
            return {'source': 'fixture', 'adjustment': 'none', 'bars': [
                {**row, 'end': row['date'].isoformat()} for row in raw.to_dict('records')]}
        def enrich_snapshot_quotes(self, frame, **kwargs):
            events.append(('whole_market', tick[0]))
            frame = frame.copy()
            frame.attrs['market_quote_enrichment'] = {'requested': 3052, 'observed': 3052}
            return frame
    monkeypatch.setattr(software_market.SoftwareMarketClient, 'from_environment', lambda: Gateway())
    result = service.run('live')
    assert result['dataQuality']['marketQuoteEnrichment']['observed'] == 3052
    assert [name for name, _ in events].index('whole_market') < [name for name, _ in events].index('quote')
    assert result['dailyCandidates'] and not result['candidates']
    assert result['recommendationPolicy']['missingPolicy'] == 'research_only'


def mixed_service_for(monkeypatch, tmp_path, start):
    from copy import deepcopy
    import pandas as pd
    from src.services.yao_scout import history_preparation
    service, tick, events, saved = make_service(monkeypatch, start, tmp_path)
    service.selection_mode = 'auto'
    service.yao.history_fetcher().attrs.update(daily_source='sina', daily_adjustment='unadjusted')
    service.yao._fetch_snapshot = lambda: pd.DataFrame([
        {'code': code, 'name': '测试股', 'price': 10} for code in ('600001', '600002')])
    original_context = service.context_provider.collect
    def context(now):
        packet = original_context(now)
        record = deepcopy(packet['tables'][0]['rows'][0])
        record['股票代码'] = '600002'
        packet['tables'][0]['rows'].append(record)
        return packet
    service.context_provider.collect = context
    monkeypatch.setattr(history_preparation, 'read_preparation_status', lambda *a: {
        'targetSession': '2026-09-14', 'details': {
            '600001': {'ready': True, 'fullSessionReady': True},
            '600002': {'ready': False}}})
    return service, tick, events, saved


def test_partial_readiness_researches_ready_stock_without_promoting_daily_or_failed_minute(monkeypatch, tmp_path):
    service, tick, events, _ = mixed_service_for(monkeypatch, tmp_path,
        datetime(2026, 9, 14, 10, 30, tzinfo=TZ))
    minute_calls, sector_calls, fund_calls = [], [], []
    original_minute, original_sector, original_fund = service.minute_fetcher, service.sector_fetcher, service.fund_fetcher
    def minute(code, *a):
        minute_calls.append(code)
        return original_minute(code, *a)
    def sector(codes, *a):
        sector_calls.extend(codes)
        return original_sector(codes, *a)
    def fund(code, *a):
        fund_calls.append(code)
        return original_fund(code, *a)
    service.minute_fetcher, service.sector_fetcher, service.fund_fetcher = minute, sector, fund
    result = service.run('live')
    assert result['primarySelectionMode'] == 'mixed_intraday_and_daily_observation', result.get('message')
    assert minute_calls == sector_calls == fund_calls == ['600001']
    assert [row['code'] for row in result['dailyCandidates']] == ['600002']
    assert result['candidates'] == [] and not any(result['profileCandidates'].values())
    assert result['dataQuality']['stockRouting']['minuteResearchCount'] == 1
    assert result['dataQuality']['stockRouting']['dailyObservationCount'] == 1
    assert result['historyPreparationCodes'] == ['600001', '600002']
    assert result['dataQuality']['quote_coverage']['requested'] == 2
    assert result['dataQuality']['minute_coverage']['requested'] == 1
    assert result['recommendationPolicy']['requires']
    assert result['dailyObservationPolicy']['formalRecommendationAllowed'] is False


@pytest.mark.parametrize('slot, hour, minute, delay', [('live', 13, 17, 31), ('1455', 14, 55, 121)])
def test_slow_mixed_scan_tail_cannot_preserve_expired_primary_profiles_or_extend_delivery(
        monkeypatch, tmp_path, slot, hour, minute, delay):
    from src.services.yao_scout import morning_task
    start = datetime(2026, 9, 14, hour, minute, tzinfo=TZ)
    service, tick, _, _ = mixed_service_for(monkeypatch, tmp_path, start)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    before_finish = []
    original_finish = service._finish_quotes
    def observe_finish(run, quality):
        before_finish.append(len(run['candidates']))
        return original_finish(run, quality)
    service._finish_quotes = observe_finish
    def delayed_readiness(*args, **kwargs):
        tick[0] += timedelta(seconds=delay)
        return {'status': 'test_delayed_readiness'}
    monkeypatch.setattr(morning_task, 'morning_readiness', delayed_readiness)
    result = service.run(slot, official=slot != 'live')
    assert 1 in before_finish  # The real evidence/profile rules initially admit the ready stock.
    assert result['candidates'] == []
    assert not any(result['profileCandidates'].values())
    assert result.get('dailyCandidates', []) == []  # Expired daily quotes cannot refill the main list.
    expiry = result['delivery']['expires_at']
    assert expiry is None or datetime.fromisoformat(expiry) <= start + timedelta(seconds=30)
    assert result['dataQuality']['primary_admission']['primary_count'] == 0
    assert result['admissionDiagnostics']['selected'] == 0
    if slot == '1455':
        assert result['status'] in {'missed_slot', 'expired'}


@pytest.mark.parametrize('target, clock, full, expected', [
    ('2026-09-14', '10:30:00', False, 'minute_research'),
    ('2026-09-14', '10:29:00', False, 'minute_research'),
    ('2026-09-14', '10:29:00', True, 'minute_research'),
    ('2026-09-11', '10:30:00', True, 'daily_observation'),
])
def test_routing_keeps_each_stock_session_and_clock_contract(target, clock, full, expected):
    from src.services.yao_scout.scan_routing import partition_queue
    minute, daily, audit = partition_queue([{'code': '600001'}, {'code': '600002'}], {
        'targetSession': target, 'details': {'600001': {
            'ready': True, 'baselineClock': clock, 'fullSessionReady': full}}},
        datetime(2026, 9, 14, 10, 30, tzinfo=TZ))
    assert audit['stocks'][0]['route'] == expected
    assert audit['stocks'][1]['route'] == 'daily_observation'
    assert all(not row['formalEligibilityGranted'] for row in audit['stocks'])
    assert len(minute) + len(daily) == 2


def test_readiness_at_other_clock_uses_verified_windows_not_last_ready_flag():
    from src.services.yao_scout.scan_routing import partition_queue
    minute, daily, audit = partition_queue([{'code':'600001'}, {'code':'600002'}], {
        'targetSession':'2026-09-14', 'details': {
            '600001':{'ready':False, 'readyClocks':['10:30:00']},
            '600002':{'ready':True, 'readyClocks':['10:29:00']}}},
        datetime(2026,9,14,10,30,tzinfo=TZ))
    assert [row['code'] for row in minute] == ['600001']
    assert [row['code'] for row in daily] == ['600002']
    assert all(not row['formalEligibilityGranted'] for row in audit['stocks'])
