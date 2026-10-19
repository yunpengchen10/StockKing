"""The first run can publish daily conditions without a minute archive."""
from datetime import datetime
import pytest
from src.services.yao_scout.daily_opportunities import TZ
from tests.test_local_quote_integration import make_service


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
    assert result['primarySelectionMode'] == 'daily_conditional', result.get('message')
    assert result['dailyAssessment']['dailyUsableCount'] == 1
    assert result['dailyAssessment']['quoteUsableCount'] == 1
    assert result['dailyAssessment']['eventCoveragePassedCount'] == 1
    assert result['candidates'], result['dailyAssessment']
    assert result['candidates'][0]['dailySignal']['taskId'] == 'daily_structure_to_next_session'
    assert result['dataQuality']['minute_coverage']['required'] is False
    assert not (tmp_path/'minute_history'/'minute-bars-v2.sqlite3').exists()
    assert result['historyPreparationCodes'] == ['600001']


def test_desktop_gateway_none_enum_is_explicit_unadjusted_daily_history(monkeypatch, tmp_path):
    service, _, _, _ = service_for(monkeypatch, tmp_path)
    service.yao.history_fetcher().attrs['daily_source'] = 'software:go/tdx-mac; adjustment=none'
    service.yao.history_fetcher().attrs['daily_adjustment'] = 'none'
    result = service.run('live')
    assert result['dailyAssessment']['dailyUsableCount'] == 1
    assert result['candidates'][0]['dailyPreviousSession']['priceBasis'] == 'unadjusted'


def test_another_stock_or_clock_readiness_does_not_disable_daily_cold_start(monkeypatch, tmp_path):
    from src.services.yao_scout import history_preparation
    service, _, _, _ = service_for(monkeypatch, tmp_path)
    monkeypatch.setattr(history_preparation, 'read_preparation_status', lambda *a: {
        'targetSession':'2026-09-14', 'ready':1, 'requested':1,
        'details':{'600001':{'ready':True, 'fullSessionReady':False, 'baselineClock':'10:29:00'}}})
    result = service.run('live')
    assert result['candidates']
    assert result['primarySelectionMode'] == 'daily_conditional'


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
    assert result['candidates']
