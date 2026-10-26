from datetime import datetime, timedelta
import json

import pandas as pd
import pytest

from src.services.yao_scout import sector_fund_evidence as mod


NOW = datetime(2026, 10, 22, 13, 40, tzinfo=mod.SHANGHAI)


def row(code, industry='化学制品', day='2026-10-21'):
    return {'SECURITY_CODE':code, 'INDUSTRY':industry, 'MAX_TRADE_DATE':day}


def payload(rows, *, count=None, nextpage=False):
    return {'success':True, 'result':{'count':len(rows) if count is None else count,
                                    'nextpage':nextpage, 'data':rows}}


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_complete_bulk_membership_keeps_growth_and_star_peers_and_source_dates(tmp_path):
    rows = [row('002585'), row('600001'), row('300001'), row('688001'),
            row('920001'), row('000001', '银行')]
    requests = []
    def getter(url, **kwargs):
        requests.append((url, kwargs['params']))
        return Response(payload(rows))
    members, boards = mod.prepare_sector_membership(['002585'], NOW, tmp_path, getter=getter, timeout=1)
    board = members['002585']
    assert len(requests) == 1
    assert requests[0][0] == mod.BULK_INDUSTRY_URL
    assert 'filter' not in requests[0][1]
    assert boards[board['code']] == ['002585', '300001', '600001', '688001']
    assert board['membershipComplete'] is True
    assert board['membershipCatalogueCount'] == 6
    assert board['membershipUniverseCount'] == 5
    assert board['membershipExcludedCount'] == 1
    assert board['membershipSourceDates'] == ['2026-10-21']
    assert board['classification'] == 'xuangu-industry-v2'
    assert 'not a quote time' in board['membershipSourceTimeMeaning']
    evidence = mod.compute_sector_evidence('002585',
        {'asOf':NOW.isoformat(), 'metrics':{'speed_5m_pct':2}}, board,
        boards[board['code']], {}, NOW,
        _peer_returns={'002585':2, '600001':1, '300001':1, '688001':1})
    assert evidence['totalPeers'] == 3
    assert evidence['peerCount'] == 3
    assert evidence['metrics']['sector_coverage_pct'] == 100
    assert evidence['membershipMetadata']['membershipSourceDates'] == ['2026-10-21']
    assert evidence['asOf'] == NOW.isoformat()


def test_filtered_desktop_snapshot_never_defines_sector_denominator(tmp_path):
    snapshot = pd.DataFrame([{'code':'002585', 'industry':'化学制品'}])
    snapshot.attrs.update(complete=True, expected_count=1, unique_count=1, source='software:go/eastmoney')
    members, boards = mod.prepare_sector_membership(['002585'], NOW, tmp_path,
        snapshot=snapshot, getter=lambda *a, **k:Response(payload([
            row('002585'), row('300001'), row('688001'), row('600001')])), timeout=1)
    assert len(boards[members['002585']['code']]) == 4


def test_bulk_industry_taxonomy_does_not_mix_old_bk_members(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'discover_sectors', lambda *a, **k:pytest.fail('Complete bulk data needs no BK lookup'))
    members, boards = mod.prepare_sector_membership(['002585'], NOW, tmp_path,
        getter=lambda *a, **k:Response(payload([row('002585'), row('300001')])), timeout=1)
    assert set(boards) == {'EMXG:化学制品'}
    assert members['002585']['code'] == 'EMXG:化学制品'


@pytest.mark.parametrize('data', [
    payload([row('002585')], count=2),
    payload([row('002585'), row('002585')]),
    payload([row('002585', '')]),
    payload([row('002585', day='2026-10-23')]),
    payload([row('002585', day='not-a-date')]),
    {'success':False, 'result':{'count':1, 'nextpage':False, 'data':[row('002585')]}},
])
def test_invalid_or_future_bulk_catalogue_cannot_be_cached_as_complete(tmp_path, data):
    with pytest.raises(ValueError):
        mod.discover_bulk_sectors(['002585'], NOW, tmp_path,
            getter=lambda *a, **k:Response(data))
    assert not (tmp_path/'sector-membership'/'xuangu-industries-v1.json').exists()


def test_all_pages_are_validated_before_industry_is_returned(tmp_path):
    requests = []
    def get(url, **kwargs):
        page = int(kwargs['params']['p'])
        requests.append(page)
        return Response(payload([row('002585' if page == 1 else '300001')], count=2, nextpage=page == 1))
    members, boards = mod.discover_bulk_sectors(['002585'], NOW, tmp_path, getter=get)
    assert requests == [1, 2]
    assert boards[members['002585']['code']] == ['002585', '300001']


def test_same_day_bulk_cache_preserves_observation_time_and_avoids_network(tmp_path):
    directory = tmp_path/'sector-membership'
    directory.mkdir()
    observed = NOW-timedelta(minutes=5)
    catalogue = {'source':'eastmoney:xuangu-unfiltered', 'complete':True,
        'expectedCount':2, 'observedAt':observed.isoformat(),
        'rows':[{'code':'002585', 'industry':'化学制品', 'sourceDate':'2026-10-21'},
                {'code':'300001', 'industry':'化学制品', 'sourceDate':'2026-10-21'}]}
    (directory/'xuangu-industries-v1.json').write_text(json.dumps(
        {'fetchedAt':observed.isoformat(), 'value':catalogue}), encoding='utf-8')
    members, boards = mod.prepare_sector_membership(['002585'], NOW, tmp_path,
        getter=lambda *a, **k:pytest.fail('Cached complete metadata must need no network'), timeout=0)
    assert members['002585']['membershipObservedAt'] == observed.isoformat()
    assert boards['EMXG:化学制品'] == ['002585', '300001']


def test_bulk_failure_keeps_legacy_industry_fallback(tmp_path, monkeypatch):
    def unavailable(*a, **k):
        raise ValueError('bulk unavailable')
    monkeypatch.setattr(mod, 'discover_bulk_sectors', unavailable)
    monkeypatch.setattr(mod, 'discover_sectors', lambda *a, **k:(
        {'002585':{'code':'BK001', 'name':'Old taxonomy'}}, {'BK001':['002585', '600001']}))
    members, boards = mod.prepare_sector_membership(['002585'], NOW, tmp_path, timeout=0)
    assert members['002585']['code'] == 'BK001'
    assert boards == {'BK001':['002585', '600001']}
