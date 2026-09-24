from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest

from src.services.yao_scout import daily_opportunities as base
from src.services.yao_scout import local_opportunities as local


def make_service(monkeypatch, start, fetch=None, batch=None):
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
    history['high'], history['low'] = history.close+1, history.close-.2
    history.attrs['daily_source'] = 'test'
    yao = SimpleNamespace(db=db, _fetch_snapshot=snapshot, _snapshot_meta=lambda *a, **kw: {},
                          history_fetcher=lambda *a, **kw: history, data_dir=Path('.'))
    monkeypatch.setattr(base, 'is_market_open', lambda *a: True)
    monkeypatch.setattr(local, 'get_tier_model_service', lambda: SimpleNamespace(score_local_five_day=lambda rows: rows))
    monkeypatch.setattr(base.HighClient, 'ask', lambda *a: pytest.fail('automatic scan called paid AI'))
    def quote(code):
        events.append(('quote', tick[0]))
        return {'code': code, 'source': 'tencent', 'price': 10, 'pre_close': 9.8, 'open_price': 9.7,
                'high': 10.05, 'low': 9.65, 'change_pct': 100*(10/9.8-1),
                'amount': 980000000, 'volume':100000000, 'provider_timestamp': tick[0].isoformat(),
                'active_buy_share_pct':60,'active_flow_source':'fixture','active_flow_as_of':tick[0].isoformat()}
    service = local.LocalOpportunityService(yao, {'api_key': 'must-not-be-used'},
        context_provider=SimpleNamespace(collect=lambda now: {'tables': [
            {'kind': kind, 'status': 'available', 'observedAt': now.isoformat(), 'source': 'fixture', 'rows': rows}
            for kind, rows in [('financials', [{'股票代码': '600001', '最新公告日期': '2026-08-30',
                '净利润-净利润': 100, '每股收益': 1, '每股经营现金流量': 1}]), ('forecast', []), ('unlock', [])]]}),
        quote_fetcher=fetch or quote, batch_quote_fetcher=batch,
        minute_fetcher=lambda *a: {'metrics':{'speed_3m_pct':.3,'speed_5m_pct':.6,'local_high_5m':9.99,
                                               'local_low_5m':9.7,'amount_3m':5000000,'relative_amount_3m_20d':2},'historyDays':20,
                                   'asOf':(tick[0]-timedelta(minutes=1)).isoformat(), 'source':'test_minutes','gaps':[]},
        sector_fetcher=lambda codes,*a: {code:{'metrics':{'sector_coverage_pct':100,'sector_peer_count':10,'sector_return_5m_pct':.2,'sector_breadth':80,'sector_relative_5m_pct':.4},
            'asOf':(tick[0]-timedelta(minutes=1)).isoformat(),'sector':{'name':'fixture'}} for code in codes},
        fund_fetcher=lambda *a: {'metrics':{'main_net_flow_3m':1e5},'asOf':(tick[0]-timedelta(minutes=1)).isoformat()},
        clock=lambda: tick[0], sleeper=lambda seconds: tick.__setitem__(0, tick[0] + timedelta(seconds=seconds)))
    return service, tick, events, saved


@pytest.mark.parametrize('slot,hour,minute', [('0920', 9, 20), ('1030', 10, 30), ('1455', 14, 55)])
def test_scheduled_local_scan_prepares_early_then_refreshes_at_target(monkeypatch, slot, hour, minute):
    start = datetime(2026, 9, 14, hour, minute, tzinfo=base.TZ) - timedelta(minutes=10)
    service, tick, events, saved = make_service(monkeypatch, start)
    result = service.run(slot, official=True)
    target = start + timedelta(minutes=10)
    assert events == [('snapshot', start), ('snapshot', target-timedelta(seconds=90)), ('quote', target)]
    assert result['delivery']['decision_at'] == target.isoformat()
    records = result['candidates'] or result['precisionWatchlist']
    assert records[0]['quote']['provider_timestamp'] == target.isoformat()
    assert result['generatedAt'] == target.isoformat()
    assert result['dataQuality']['quote_coverage']['fresh'] == 1
    assert result['llmUsed'] is False and result['delivery']['received_at'] is None
    if slot == '0920':
        assert records[0]['status'] == 'premarket'
        assert not any(result['profileCandidates'].values())


def test_manual_live_refresh_does_not_wait_for_schedule(monkeypatch):
    start = datetime(2026, 9, 14, 10, 17, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start)
    result = service.run('live')
    assert tick[0] == start
    assert result['delivery']['target_at'] is None
    assert len(events) == 2


def test_expired_slot_never_fetches_or_waits(monkeypatch):
    start = datetime(2026, 9, 14, 14, 57, tzinfo=base.TZ)
    service, tick, events, _ = make_service(monkeypatch, start)
    result = service.run('1455')
    assert result['status'] == 'expired' and events == []


def test_default_provider_is_public_gateway_not_old_dsa_manager():
    service = base.DailyOpportunityService(SimpleNamespace(db=None))
    assert service.quote_fetcher is base.get_public_realtime_quote
    assert service.batch_quote_fetcher is base.get_public_market_quotes


def test_batch_partial_failure_preserves_good_stock(monkeypatch):
    start = datetime(2026, 9, 14, 14, 55, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start)
    service.quote_fetcher = lambda code: {'code': code} if code == '600001' else (_ for _ in ()).throw(ValueError())
    assert service._quotes(['600001', '600002']) == {'600001': {'code': '600001'}, '600002': {}}


def test_completion_rechecks_older_quote(monkeypatch):
    start = datetime(2026, 9, 14, 14, 55, tzinfo=base.TZ)
    service, tick, _, _ = make_service(monkeypatch, start)
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


def test_missing_timestamp_stays_missing_and_reviewable(monkeypatch):
    start = datetime(2026, 9, 14, 10, 30, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start, fetch=lambda code: {'code': code, 'price': 10, 'fetched_at': start.isoformat()})
    result = service.run('1030')
    assert result['dataQuality']['quote_coverage']['source_time_min'] is None
    assert result['candidates'] == []
    assert result['evidenceInsufficient'][0]['status'] == 'evidence_insufficient'


def test_scan_reads_prior_review_reminders_without_changing_rank(monkeypatch):
    start = datetime(2026, 9, 14, 10, 30, tzinfo=base.TZ)
    service, _, _, _ = make_service(monkeypatch, start)
    monkeypatch.setattr(local, 'read_observation_reminders', lambda db, code, now: ['历史缺口需重新核验'])
    result = service.run('1030')
    candidate = result['candidates'][0]
    assert candidate['observationReminders'] == ['历史缺口需重新核验']
    assert '历史缺口需重新核验' in candidate['risks']
    assert candidate['rank'] == 1 and result['llmUsed'] is False


def test_daily_review_wires_observations_and_preserves_existing_maintenance(monkeypatch):
    from src.services.yao_scout import minute_history
    start = datetime(2026, 9, 14, 15, 30, tzinfo=base.TZ)
    service, _, events, _ = make_service(monkeypatch, start)
    service.db.save_yao_adaptive_state = lambda *a: None
    service.yao.mature_outcomes = lambda: {'status': 'maintained'}
    monkeypatch.setattr(local, 'review_local_observations', lambda *a, **kw: {'status': 'reviewed', 'verified_count': 2})
    captured=[]
    monkeypatch.setattr(minute_history,'archive_universe',lambda codes,*a: captured.extend(codes) or {'universe':len(codes),'updated':len(codes)})
    result = service.run('review')
    assert result['observationReview']['verified_count'] == 2
    assert result['localMaintenance']['status'] == 'maintained'
    assert events == [('snapshot',start)] and result['llmUsed'] is False
    assert captured==['600001'] and result['minuteArchiveMaintenance']['updated']==1
