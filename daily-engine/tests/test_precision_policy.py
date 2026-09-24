from datetime import datetime, timedelta
import pandas as pd
import pytest

from src.services.akshare_context import AkshareContextProvider, for_stock, TZ
from src.services.yao_scout.precision_policy import evaluate

NOW = datetime(2026, 9, 24, 10, 30, tzinfo=TZ)


def fixture_fetch(endpoint, params, timeout):
    if endpoint == 'stock_yjbb_em':
        return pd.DataFrame([{'股票代码': '600001', '最新公告日期': '2026-08-30',
            '净利润-净利润': 100000, '每股收益': 1, '每股经营现金流量': .8}])
    if endpoint == 'stock_yjyg_em':
        return pd.DataFrame(columns=['股票代码', '公告日期', '预测指标', '预告类型'])
    return pd.DataFrame(columns=['股票代码', '解禁时间', '占解禁前流通市值比例'])


def good_context():
    return {'financials': {'profit': 100000, 'eps': 1, 'cashPerShare': .8}, 'forecasts': [],
            'unlocks': [], 'coverage': {'financials': True, 'forecast': True, 'unlock': True}, 'gaps': []}


def candidate(price=10):
    return {'evidenceEligible': True, 'status': 'conditional', 'quote': {'price': price, 'pre_close': 9.8},
            'structureRewardRisk': 3, 'evidenceChecks': {'minute_structure': True}}


DAILY = {'metrics': {'ma5': 9.8, 'ma10': 9.7, 'ma20': 9.6, 'atr14': .4, 'last_close': 9.8}}


def test_three_policies_have_different_chase_limits():
    result = evaluate(candidate(10.3), good_context(), DAILY)['profiles']
    assert result['conservative']['state'] == 'avoid'
    assert result['regular']['state'] == 'avoid'
    assert result['aggressive']['entryEligible']


def test_price_location_is_a_gate_not_just_a_warning():
    result = evaluate({**candidate(), 'structureRewardRisk': .8}, good_context(), DAILY)
    assert all(r['state'] == 'avoid' for r in result['profiles'].values())


def test_financial_missing_is_not_healthy_and_does_not_become_probability():
    context = {**good_context(), 'financials': None}
    result = evaluate(candidate(), context, DAILY)
    assert result['profiles']['conservative']['state'] == 'watch'
    assert result['profiles']['regular']['entryEligible']
    assert result['probability'] is None


@pytest.mark.parametrize('context', [
    {**good_context(), 'coverage': {}},
    {**good_context(), 'forecasts': [{'type': '首亏', 'metric': '归属净利润'}]},
    {**good_context(), 'unlocks': [{'floatRatioPct': 10}]},
])
def test_missing_event_coverage_and_known_material_risks_cannot_enter(context):
    assert not any(r['entryEligible'] for r in evaluate(candidate(), context, DAILY)['profiles'].values())


def test_missing_quote_or_premarket_never_upgraded():
    for item in ({**candidate(), 'evidenceEligible': False}, {**candidate(), 'status': 'premarket'},
                 {**candidate(), 'status': 'expired'}):
        assert not any(r['entryEligible'] for r in evaluate(item, good_context(), DAILY)['profiles'].values())


def test_archive_replay_refuses_data_observed_later(tmp_path):
    provider = AkshareContextProvider(tmp_path, fetcher=fixture_fetch, clock=lambda: NOW)
    bundle = provider.collect()
    assert for_stock(bundle, '600001', NOW)['financials']['profit'] == 100000
    assert for_stock(bundle, '600001', NOW-timedelta(seconds=1))['financials'] is None
    with pytest.raises(ValueError, match='Historical'):
        provider.collect(NOW-timedelta(days=1))
    replay = provider.collect(NOW-timedelta(days=1), refresh=False)
    assert for_stock(replay, '600001', NOW-timedelta(days=1))['financials'] is None


def test_publication_date_is_not_report_period_or_midnight_of_same_day(tmp_path):
    def fetch(endpoint, params, timeout):
        frame = fixture_fetch(endpoint, params, timeout)
        if endpoint == 'stock_yjbb_em':
            frame['最新公告日期'] = NOW.date().isoformat()
        return frame
    bundle = AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: NOW).collect()
    assert for_stock(bundle, '600001', NOW)['financials'] is None


def test_unlock_units_and_future_return_fields_are_not_imported(tmp_path):
    def fetch(endpoint, params, timeout):
        if endpoint == 'stock_restricted_release_detail_em':
            return pd.DataFrame([{'股票代码': '600001', '解禁时间': '2026-09-28',
                '占解禁前流通市值比例': .06, '解禁后20日涨跌幅': 999}])
        return fixture_fetch(endpoint, params, timeout)
    provider = AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: NOW)
    bundle = provider.collect()
    assert '解禁后20日涨跌幅' not in str(bundle)
    assert for_stock(bundle, '600001', NOW)['unlocks'][0]['floatRatioPct'] == 6


def test_successful_empty_event_table_differs_from_error(tmp_path):
    def broken(endpoint, params, timeout):
        raise TimeoutError('fixture')
    good = AkshareContextProvider(tmp_path/'good', fetcher=fixture_fetch, clock=lambda: NOW).collect()
    bad = AkshareContextProvider(tmp_path/'bad', fetcher=broken, clock=lambda: NOW).collect()
    assert for_stock(good, '600001', NOW)['coverage']['unlock']
    assert not for_stock(bad, '600001', NOW)['coverage']['unlock']


def test_cached_calls_do_not_refetch(tmp_path):
    calls = []
    def fetch(*args):
        calls.append(args[0])
        return fixture_fetch(*args)
    provider = AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: NOW)
    provider.collect()
    provider.collect()
    assert len(calls) == 5


def test_several_unlock_batches_are_cumulative():
    context = {**good_context(), 'unlocks': [{'floatRatioPct': 3}, {'floatRatioPct': 3}]}
    result = evaluate(candidate(), context, DAILY)['profiles']
    assert not result['regular']['entryEligible']
    assert result['aggressive']['entryEligible']


@pytest.mark.parametrize('value', [None, -1])
def test_unknown_unlock_ratio_never_becomes_zero_risk(tmp_path, value):
    def fetch(endpoint, params, timeout):
        if endpoint == 'stock_restricted_release_detail_em':
            return pd.DataFrame([{'股票代码': '600001', '解禁时间': '2026-09-28', '占解禁前流通市值比例': value}])
        return fixture_fetch(endpoint, params, timeout)
    bundle = AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: NOW).collect()
    assert not for_stock(bundle, '600001', NOW)['coverage']['unlock']


def test_same_day_forecast_is_unknown_until_next_day(tmp_path):
    def fetch(endpoint, params, timeout):
        if endpoint == 'stock_yjyg_em':
            return pd.DataFrame([{'股票代码': '600001', '公告日期': '2026-09-24', '预测指标': '净利润', '预告类型': '首亏'}])
        return fixture_fetch(endpoint, params, timeout)
    bundle = AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: NOW).collect()
    context = for_stock(bundle, '600001', NOW)
    assert not context['coverage']['forecast'] and not context['forecasts']
    assert not any(d['entryEligible'] for d in evaluate(candidate(), context, DAILY)['profiles'].values())


def test_old_positive_financials_do_not_hide_incomplete_latest_coverage():
    context = good_context()
    context['coverage']['financials'] = False
    assert evaluate(candidate(), context, DAILY)['profiles']['conservative']['state'] == 'watch'
