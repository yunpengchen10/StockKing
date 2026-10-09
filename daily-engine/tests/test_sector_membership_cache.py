from datetime import datetime, timedelta
import json

from src.services.yao_scout import sector_fund_evidence as mod


NOW = datetime(2026, 10, 9, 10, 30, tzinfo=mod.SHANGHAI)


def saved(tmp_path, key, value, at=NOW):
    directory = tmp_path / 'sector-membership'
    directory.mkdir(exist_ok=True)
    (directory / (key + '.json')).write_text(
        json.dumps({'fetchedAt': at.isoformat(), 'value': value}), encoding='utf-8')


def test_cached_members_and_constituents_survive_an_exhausted_budget(tmp_path):
    saved(tmp_path, 'stock-600001', {'code': 'BK001', 'name': 'Industry'})
    saved(tmp_path, 'board-BK001', [{'code': '600001'}, {'code': '600002'}])
    def unavailable(*args, **kwargs):
        raise AssertionError('No network request is necessary for cached evidence')
    members, peers = mod.prepare_sector_membership(
        ['600001'], NOW, tmp_path, getter=unavailable, timeout=0)
    assert members['600001']['code'] == 'BK001'
    assert peers == {'BK001': ['600001', '600002']}


def test_uncompleted_uncached_requests_do_not_erase_later_cached_members(tmp_path, monkeypatch):
    saved(tmp_path, 'industry-list', [{'code': 'BK001', 'name': 'Industry'}])
    saved(tmp_path, 'stock-600002', {'code': 'BK001', 'name': 'Industry'})
    saved(tmp_path, 'board-BK001', [{'code': '600002'}, {'code': '600003'}])
    calls = []
    def timed_out(fetch, items, **kwargs):
        items = list(items)
        calls.append(items)
        return [], items
    monkeypatch.setattr(mod, 'bounded_fetch_map', timed_out)
    members, peers = mod.discover_sectors(['600001', '600002'], NOW, tmp_path)
    assert calls[0] == ['600001']
    assert members == {'600002': {'code': 'BK001', 'name': 'Industry'}}
    assert peers == {'BK001': ['600002', '600003']}


def test_partial_primary_uses_fallback_only_for_missing_stocks(tmp_path, monkeypatch):
    primary = {'600001': {'code': 'BK001', 'name': 'Primary', 'provider': 'Eastmoney'}}
    monkeypatch.setattr(mod, 'discover_sectors', lambda *a, **k: (dict(primary), {'BK001': ['600001', '600003']}))
    requested = []
    def fallback(codes, *args, **kwargs):
        requested.extend(codes)
        return {'600002': {'code': 'new_industry', 'name': 'Fallback', 'provider': 'Sina'}}, {'new_industry': ['600002', '600004']}
    monkeypatch.setattr(mod, 'discover_sina_sectors', fallback)
    members, peers = mod.prepare_sector_membership(['600001', '600002'], NOW, tmp_path)
    assert requested == ['600002']
    assert members['600001'] == primary['600001']
    assert members['600002']['provider'] == 'Sina'
    assert peers['BK001'] == ['600001', '600003']
    assert peers['new_industry'] == ['600002', '600004']


def test_previous_day_cache_is_not_promoted_by_budget_expiration(tmp_path, monkeypatch):
    saved(tmp_path, 'stock-600001', {'code': 'OLD', 'name': 'Old'}, NOW - timedelta(days=1))
    monkeypatch.setattr(mod, 'discover_sina_sectors', lambda *a, **k: ({'600001': {'code': 'NEW', 'provider': 'Sina'}}, {}))
    members, _ = mod.prepare_sector_membership(['600001'], NOW, tmp_path, timeout=0)
    assert members['600001']['code'] == 'NEW'


def test_same_day_future_cache_is_not_reused_by_lookup(tmp_path):
    saved(tmp_path, 'stock-600001', {'code': 'FUTURE'}, NOW + timedelta(seconds=1))
    fetched = []
    def fetch():
        fetched.append(True)
        return {'code': 'CURRENT'}
    result = mod.cached_metadata(tmp_path / 'sector-membership', 'stock-600001', NOW, fetch)
    assert result == {'code': 'CURRENT'}
    assert fetched == [True]
