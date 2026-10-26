from copy import deepcopy
from time import monotonic

import pytest
import requests

from src.services.yao_scout import sector_fund_evidence as sf


CUTOFF = '2026-10-22T14:00:00+08:00'


def packet(code='600001', direction=1):
    symbol = ('sh' if code.startswith('6') else 'sz')+code
    rows = []
    for n, minute in enumerate((56, 57, 58, 59)):
        main = direction*(100+n*10)
        rows.append({'time':f'2026102213{minute:02d}', 'MainNetInflow':str(main),
            'SmallNetInflow':str(-main), 'NormalNetInflow':'0', 'BigNetInflow':str(main),
            'SuperNetInflow':'0', 'MainInflow':str(1000+n*20+main), 'MainOutflow':str(1000+n*20)})
    return {'code':0, 'data':{'todayFundTrend':{'stockCode':symbol, 'minList':rows},
                            'todayFundFlow':{'stockCode':symbol, 'mainNetIn':'999999999'}}}


@pytest.mark.parametrize('direction', [-1, 1])
def test_real_provider_cumulative_fields_are_differenced_without_using_day_summary(direction):
    result = sf.compute_tencent_fund_evidence('600001', packet(direction=direction), CUTOFF)
    assert result['metrics'] == {'main_net_flow':direction*130, 'main_net_flow_3m':direction*30,
        'main_net_flow_1m_deltas': [direction*10]*3,
        'main_net_positive_fraction_3m': 1. if direction > 0 else 0.}
    assert result['asOf'] == '2026-10-22T14:00:00+08:00'
    assert result['rawSourceAsOf'] == '2026-10-22T13:59:00+08:00'
    assert result['rawTimeSemantics'] == 'minute_start' and result['normalizedTimeSemantics'] == 'bar_end'
    assert result['provider'] == 'tencent' and result['flowKind'] == 'vendor_estimate'
    assert result['measurementVersion'] == 'tencent-main-super-big-cumulative-bar-end-v2'
    assert result['amountUnit'] == 'CNY' and '20万元' in result['basis'] and '6万股' in result['basis']
    assert 'Tencent' in result['source'] and 'Eastmoney' not in result['source']


@pytest.mark.parametrize('field', ['MainNetInflow', 'BigNetInflow', 'SuperNetInflow', 'MainInflow', 'MainOutflow'])
def test_inconsistent_main_classification_or_cash_flow_breaks_window(field):
    raw = packet()
    row = raw['data']['todayFundTrend']['minList'][1]
    row[field] = str(float(row[field])+100)
    result = sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)
    assert 'main_net_flow_3m' not in result['metrics']


@pytest.mark.parametrize('branch', ['todayFundTrend', 'todayFundFlow'])
def test_cross_security_response_rejected(branch):
    raw = packet()
    raw['data'][branch]['stockCode'] = 'sz000001'
    with pytest.raises(ValueError, match='identity mismatch'):
        sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)


def test_no_identity_or_classified_components_cannot_be_promoted():
    raw = packet()
    del raw['data']['todayFundTrend']['stockCode']
    with pytest.raises(ValueError, match='identity mismatch'):
        sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)
    raw = packet()
    for row in raw['data']['todayFundTrend']['minList']:
        row.pop('BigNetInflow')
    assert sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)['metrics'] == {}


@pytest.mark.parametrize('date', ['20261021', '20261023'])
def test_wrong_day_never_supplies_evidence(date):
    raw = packet()
    for row in raw['data']['todayFundTrend']['minList']:
        row['time'] = date+row['time'][8:]
    assert sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)['metrics'] == {}


def test_future_current_minute_is_excluded_at_frozen_cutoff():
    raw = packet()
    future = deepcopy(raw['data']['todayFundTrend']['minList'][-1])
    future['time'] = '202610221401'
    raw['data']['todayFundTrend']['minList'].append(future)
    result = sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)
    assert result['asOf'].endswith('14:00:00+08:00') and result['metrics']['main_net_flow_3m'] == 30


def test_current_start_label_cannot_be_used_before_its_end_even_if_it_looks_fresh():
    raw = packet()
    partial = deepcopy(raw['data']['todayFundTrend']['minList'][-1])
    partial['time'] = '202610221400'
    for field in ('MainNetInflow', 'BigNetInflow', 'MainInflow'):
        partial[field] = str(float(partial[field])+9000000)
    raw['data']['todayFundTrend']['minList'].append(partial)
    result = sf.compute_tencent_fund_evidence('600001', raw, '2026-10-22T14:00:30+08:00')
    assert result['asOf'] == '2026-10-22T14:00:00+08:00'
    assert result['rawSourceAsOf'] == '2026-10-22T13:59:00+08:00'
    assert result['metrics'] == {'main_net_flow':130, 'main_net_flow_3m':30,
        'main_net_flow_1m_deltas': [10.]*3, 'main_net_positive_fraction_3m': 1.}


def test_opening_start_labels_normalize_to_ends_and_duplicated_lunch_endpoint_is_discarded():
    raw = packet()
    for row, stamp in zip(raw['data']['todayFundTrend']['minList'], ['202610220930','202610220931','202610220932','202610220933']):
        row['time'] = stamp
    result = sf.compute_tencent_fund_evidence('600001', raw, '2026-10-22T09:34:00+08:00')
    assert result['metrics']['main_net_flow_3m'] == 30 and result['asOf'].endswith('09:34:00+08:00')
    for row, stamp in zip(raw['data']['todayFundTrend']['minList'], ['202610221126','202610221127','202610221128','202610221129']):
        row['time'] = stamp
    duplicate = deepcopy(raw['data']['todayFundTrend']['minList'][-1]); duplicate['time']='202610221130'
    raw['data']['todayFundTrend']['minList'].append(duplicate)
    result = sf.compute_tencent_fund_evidence('600001', raw, '2026-10-22T11:31:00+08:00')
    assert result['asOf'].endswith('11:30:00+08:00') and result['rawSourceAsOf'].endswith('11:29:00+08:00')
    assert result['metrics']['main_net_flow_3m'] == 30


def test_conflicting_duplicate_or_lunch_gap_never_creates_3m_difference():
    raw = packet()
    duplicate = deepcopy(raw['data']['todayFundTrend']['minList'][1])
    for field in ('MainNetInflow','BigNetInflow','MainInflow'):
        duplicate[field] = str(float(duplicate[field])+100)
    raw['data']['todayFundTrend']['minList'].append(duplicate)
    assert 'main_net_flow_3m' not in sf.compute_tencent_fund_evidence('600001', raw, CUTOFF)['metrics']
    raw = packet()
    for row, stamp in zip(raw['data']['todayFundTrend']['minList'], ['202610221129','202610221130','202610221300','202610221301']):
        row['time'] = stamp
    result = sf.compute_tencent_fund_evidence('600001', raw, '2026-10-22T13:01:30+08:00')
    assert 'main_net_flow_3m' not in result['metrics']


def test_healthy_tencent_finishes_before_any_broken_eastmoney_attempt(monkeypatch):
    seen = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return packet()
    def request(url, **kwargs):
        seen.append((url, kwargs))
        return Response()
    monkeypatch.setattr(sf, '_fund_request', request)
    monkeypatch.setattr(sf, '_fund_data', lambda *a, **k: pytest.fail('healthy first provider must finish'))
    deadline = monotonic()+2
    result = sf.fetch_fund_evidence('600001', CUTOFF, deadline=deadline)
    assert len(seen) == 1 and seen[0][0] == sf.TENCENT_FUND_URL
    assert seen[0][1]['deadline'] == deadline and seen[0][1]['params']['code'] == 'sh600001'
    assert result['metrics']['main_net_flow_3m'] == 30


def test_partial_providers_are_never_spliced_into_one_window(monkeypatch):
    tencent = sf.compute_tencent_fund_evidence('600001', packet(), CUTOFF)
    tencent['metrics'].pop('main_net_flow_3m')
    monkeypatch.setattr(sf, '_tencent_fund_evidence', lambda *a: deepcopy(tencent))
    monkeypatch.setattr(sf, '_fund_data', lambda *a, **k: ({'code':'600001', 'klines':[
        '2026-10-22 13:58:00,400,0,0,400,0','2026-10-22 13:59:00,500,0,0,500,0']},sf.BASE))
    result = sf.fetch_fund_evidence('600001', CUTOFF, deadline=monotonic()+2)
    assert result['provider'] == 'tencent' and 'main_net_flow_3m' not in result['metrics']


def test_eastmoney_remains_independent_fallback_after_tencent_failure(monkeypatch):
    def unavailable(*a):
        raise requests.ConnectionError('provider unavailable')
    monkeypatch.setattr(sf, '_tencent_fund_evidence', unavailable)
    monkeypatch.setattr(sf, '_fund_data', lambda *a, **k: ({'code':'600001', 'klines':[
        f'2026-10-22 13:{minute}:00,{-10*n},0,0,{-10*n},0' for n,minute in enumerate((56,57,58,59))]},sf.BASE))
    result = sf.fetch_fund_evidence('600001', CUTOFF, deadline=monotonic()+2)
    assert result['provider'] == 'eastmoney' and result['metrics']['main_net_flow_3m'] == -30
    assert 'Tencent' not in result['source']
