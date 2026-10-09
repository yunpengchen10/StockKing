from datetime import datetime, timedelta
import json

import pytest

from src.services.yao_scout.intraday_evidence import SHANGHAI
from src.services.yao_scout.minute_history import (normalize_bars, compute_bar_evidence, MinuteArchive,
    archive_universe, fetch_tushare_bars)
from src.services.yao_scout.sector_fund_evidence import compute_sector_evidence, compute_fund_evidence, all_members

NOW = datetime(2026,9,18,10,30,tzinfo=SHANGHAI)
DATES = [NOW.date()-timedelta(days=i) for i in range(1,21)]

def bars(day=NOW.date(), amount=1000, count=7):
    end = NOW.replace(year=day.year,month=day.month,day=day.day)
    return [{'end':(end-timedelta(minutes=i)).isoformat(),'open':10,'close':10,'high':10.2,'low':9.8,
             'volume_shares':amount/10,'amount_cny':amount,'source':'fixture'} for i in range(count)]

def test_exact_three_minute_amount_and_twenty_distinct_dates():
    rows = bars(amount=3000) + [r for day in DATES for r in bars(day)]
    result = compute_bar_evidence(rows,NOW,expected_dates=DATES)
    assert result['metrics']['amount_3m'] == 9000
    assert result['baselineMedianAmount'] == 3000
    assert result['metrics']['relative_amount_3m_20d'] == 3
    assert result['historyDays'] == 20 and result['baselineReady']
    missing = [r for r in rows if r['end'][:10] != DATES[0].isoformat()]
    result = compute_bar_evidence(missing+bars(NOW.date()+timedelta(days=1)),NOW,expected_dates=DATES)
    assert result['metrics']['relative_amount_3m_20d'] is None
    assert result['historyDays']==19
    assert result['missingHistoryDates']==[DATES[0].isoformat()]

def test_interval_units_duplicates_and_unfinished_bars_cannot_enter_evidence():
    rows = bars()
    wrong = {**rows[0],'volume_shares':1}  # lots substituted for shares
    assert len(normalize_bars([wrong],NOW)) == 0
    assert len(normalize_bars([rows[0],{**rows[0],'amount_cny':1001}],NOW)) == 0
    assert not normalize_bars([{**rows[0],'end':(NOW+timedelta(minutes=1)).isoformat()}],NOW)

def test_three_minute_window_cannot_bridge_lunch_or_a_missing_bar():
    end = NOW.replace(hour=13,minute=2)
    rows = [{**bars()[0],'end':end.replace(hour=h,minute=m).isoformat()} for h,m in [(11,30),(13,1),(13,2)]]
    result = compute_bar_evidence(rows,end,expected_dates=DATES)
    assert result['metrics']['amount_3m'] is None
    assert result['metrics']['speed_3m_pct'] is None

def test_archive_merge_does_not_shrink_and_keeps_provider_provenance(tmp_path):
    archive=MinuteArchive(tmp_path)
    archive.save('600000',bars(),NOW)
    archive.save('600000',bars()[-2:],NOW-timedelta(minutes=4))
    assert len(archive.read('600000',NOW)) == 7
    assert {r['source'] for r in archive.read('600000',NOW)} == {'fixture'}

def test_daily_collection_covers_unselected_codes_and_retries_failures(tmp_path):
    called=[]
    def fetch(code):
        called.append(code)
        if code=='000001' and called.count(code)==1: raise ValueError('temporary')
        return bars()
    first=archive_universe(['600000','000001','600000'],NOW,tmp_path,fetcher=fetch)
    second=archive_universe(['600000','000001'],NOW,tmp_path,fetcher=fetch)
    assert first['universe']==2 and first['failed']==1
    assert second['cached']==1 and second['updated']==1


def test_priority_archive_does_not_expand_to_registered_universe(tmp_path):
    archive_universe(['600000', '000001'], NOW, tmp_path, fetcher=lambda code: bars())
    called = []
    result = archive_universe(['600002'], NOW, tmp_path, include_registered=False,
                              fetcher=lambda code: called.append(code) or bars())
    assert called == ['600002'] and result['universe'] == 1
    assert len(json.loads((tmp_path/'archive-universe.json').read_text())['codes']) == 3


def test_preclose_success_cannot_mask_incomplete_close_archive(tmp_path):
    archive_universe(['600000'], NOW, tmp_path, fetcher=lambda code: bars())
    called = []
    close = NOW.replace(hour=15, minute=30)
    result = archive_universe(['600000'], close, tmp_path,
                              fetcher=lambda code: called.append(code) or bars())
    assert called == ['600000'] and result['cached'] == 0
    assert result['incomplete'] == 1 and result['status'] == 'partial'


def test_archive_budget_reports_deferred_work_without_false_completion(tmp_path):
    called = []
    result = archive_universe(['600000', '000001'], NOW, tmp_path, timeout=0,
                              fetcher=lambda code: called.append(code) or bars())
    assert not called and result['deferred'] == 2 and result['status'] == 'partial'


def test_archive_keeps_priority_order_with_a_single_worker(tmp_path):
    called = []
    archive_universe(['600009', '000001'], NOW, tmp_path, workers=1, include_registered=False,
                     fetcher=lambda code: called.append(code) or bars())
    assert called == ['600009', '000001']


def test_normalization_preserves_known_suspension():
    row = {**bars()[0], 'suspended': True}
    assert list(normalize_bars([row], NOW).values())[0]['suspended'] is True

def test_sector_uses_same_five_minutes_all_peers_and_excludes_self():
    peers=['600000','600001','600002','600003','600004','600005']
    rows={code:bars() for code in peers}
    for code in peers:
        rows[code][0]['close']=10.1
    stock={'asOf':NOW.isoformat(),'metrics':{'speed_5m_pct':2}}
    result=compute_sector_evidence('600000',stock,{'name':'测试板块'},peers,rows,NOW)
    assert result['peerCount']==5 and result['totalPeers']==5
    assert result['metrics']['sector_return_5m_pct']==pytest.approx(1)
    assert result['metrics']['sector_relative_5m_pct']==pytest.approx(1)
    assert result['metrics']['sector_breadth']==100
    assert result['confirmed']
    result=compute_sector_evidence('600000',stock,{'name':'测试板块'},peers,{'600001':rows['600001']},NOW)
    assert result['metrics']['sector_coverage_pct']==20
    assert 'sector_breadth' not in result['metrics']
    shifted={code:[{**r,'end':(datetime.fromisoformat(r['end'])-timedelta(minutes=2)).isoformat()} for r in part] for code,part in rows.items()}
    assert compute_sector_evidence('600000',stock,{'name':'测试板块'},peers,shifted,NOW)['peerCount']==0

def test_fund_flow_handles_negative_cumulative_amounts_and_uses_dated_deltas():
    data={'code':'600000','klines':[f'{(NOW-timedelta(minutes=i)).isoformat()},{-1000-i*100},0,0,{-1000-i*100},0' for i in range(4)]}
    result=compute_fund_evidence('600000',data,NOW)
    assert result['metrics']['main_net_flow']==-1000
    assert result['metrics']['main_net_flow_3m']==300
    assert result['flowKind']=='vendor_estimate'
    assert not compute_fund_evidence('600001',data,NOW)['metrics']
    assert not compute_fund_evidence('600000',data,NOW+timedelta(days=1))['metrics']
    bad={'code':'600000','klines':[f'{NOW.isoformat()},1,0,0,1000,0']}
    assert not compute_fund_evidence('600000',bad,NOW)['metrics']

def test_membership_pagination_cannot_silently_return_first_hundred():
    class Response:
        def raise_for_status(self): pass
        def json(self):return {'rc':0,'data':{'total':2,'diff':[{'f12':'600000','f14':'test'}]}}
    with pytest.raises(ValueError,match='pagination'):
        all_members('b:BK0475',lambda *a,**k:Response())

def test_tushare_adapter_checks_identity_and_shares_yuan_units(monkeypatch):
    monkeypatch.setenv('TUSHARE_TOKEN','test-token-not-a-secret')
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'code':0,'data':{'fields':['ts_code','trade_time','open','close','high','low','vol','amount'],
                    'items':[['600000.SH','2026-09-17 10:30:00',10,10,10,10,100,1000]]}}
    observed={}
    def post(url,**kw):observed.update(kw);return Response()
    result=fetch_tushare_bars('600000',NOW,poster=post)
    assert result[0]['volume_shares']==100 and result[0]['amount_cny']==1000
    assert observed['json']['params']['end_date']=='2026-09-17 15:01:00'
    assert observed['json']['params']['freq']=='1min'


def test_public_data_request_does_not_enter_global_auth_patch(monkeypatch):
    from src.services.yao_scout import sector_fund_evidence as source
    calls=[]
    monkeypatch.setattr(source.requests.Session,'request',lambda *a,**k:pytest.fail('legacy auth patch invoked'))
    monkeypatch.setattr(source,'original_request',lambda session,method,url,**kw: calls.append((method,session.trust_env,kw)) or 'ok')
    assert source.public_get('https://70.push2.eastmoney.com/api/qt/clist/get',timeout=4)=='ok'
    assert calls==[('GET',False,{'timeout':4})]
