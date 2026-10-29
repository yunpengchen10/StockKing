from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.services import akshare_context as ctx
from src.services.yao_scout.research_factors import event_factor


NOW = datetime(2026, 10, 28, 10, 30, tzinfo=ctx.TZ)


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self.payload


def announcement(title='关于签订重大合同的公告', day='2026-10-28'):
    return {'art_code': 'AN202610280001', 'title': title, 'notice_date': day,
            'codes': [{'stock_code': '600008'}, {'stock_code': '000001'}]}


def table_fetch(*args):
    return ctx.fetch_recent_disclosures(*args, getter=lambda *a, **k: Response({'data': {'list': [announcement()]}}))


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    monkeypatch.setattr('src.services.yao_scout.research_factors._sessions',
                        lambda start, end: [day.date() for day in pd.bdate_range(start, end)])


def test_public_feed_is_one_bounded_request_and_keeps_actual_security_associations():
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response({'data': {'list': [announcement()]}})
    frame = ctx.fetch_recent_disclosures('stock_notice_report', {}, 25, getter=get)
    assert len(calls) == 1 and calls[0][1]['timeout'] == 6
    assert calls[0][1]['params']['page_size'] == '100'
    assert set(frame['股票代码']) == {'600008', '000001'}
    assert not frame.attrs['complete']
    assert 'AN202610280001' in frame.iloc[0]['公告链接']


def test_current_date_title_is_known_at_real_first_observation_not_invented_midnight(tmp_path):
    provider = ctx.AkshareContextProvider(tmp_path, fetcher=table_fetch, clock=lambda: NOW)
    table = provider._table('disclosures', {}, NOW, True)
    result = ctx.for_stock({'tables': [table]}, '600008', NOW)
    event = result['disclosures'][0]
    assert event['availableAt'] == event['firstSeenAt'] == NOW.isoformat()
    assert event['publishedDate'] == '2026-10-28' and 'publishedAt' not in event
    assert event['impact'] == 'neutral' and event['evidenceScope'] == 'title_only'
    packet = event_factor(result, NOW)
    assert packet['score'] == 50
    assert packet['features']['discovery_event_count'] == 1
    assert packet['features']['positive_event_count'] == 0
    assert packet['coverage']['disclosures'] == 'partial'
    assert not ctx.for_stock({'tables': [table]}, '600008', NOW - timedelta(seconds=1))['disclosures']


def test_title_metadata_cannot_be_upgraded_to_positive_catalyst_by_adapter(tmp_path):
    provider = ctx.AkshareContextProvider(tmp_path, fetcher=table_fetch, clock=lambda: NOW)
    table = provider._table('disclosures', {}, NOW, True)
    result = ctx.for_stock({'tables': [table]}, '600008', NOW)
    result['disclosures'][0]['impact'] = 'positive'
    packet = event_factor(result, NOW)
    assert packet['score'] == 50 and not packet['disclosureEvidence'][0]['positiveCatalyst']


def test_cache_ttl_and_first_seen_are_independent_of_repeated_downloads(tmp_path):
    clock, calls = [NOW], []
    def fetch(*args):
        calls.append(args)
        return table_fetch(*args)
    provider = ctx.AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: clock[0])
    first = provider._table('disclosures', {}, clock[0], True)
    clock[0] += timedelta(minutes=10)
    provider._table('disclosures', {}, clock[0], True)
    assert len(calls) == 1
    clock[0] += timedelta(minutes=6)
    second = provider._table('disclosures', {}, clock[0], True)
    assert len(calls) == 2
    assert second['rows'][0]['_firstSeenAt'] == first['rows'][0]['_firstSeenAt'] == NOW.isoformat()
    old = ctx.for_stock({'tables': [second]}, '600008', NOW)
    assert old['disclosures'] == []


def test_bulk_index_matches_single_stock_including_missing_coverage_and_unknown_stock(tmp_path):
    provider = ctx.AkshareContextProvider(tmp_path, fetcher=table_fetch, clock=lambda: NOW)
    table = provider._table('disclosures', {}, NOW, True)
    bundle = {'tables': [table, {'kind': 'forecast', 'status': 'unavailable', 'rows': []}]}
    codes = ['600008', '000001', '600999']
    result = ctx.for_stocks(bundle, codes + ['600008'], NOW)
    assert set(result) == set(codes)
    for code in codes:
        assert result[code] == ctx.for_stock(bundle, code, NOW)
    assert result['600999']['disclosures'] == []
    assert result['600999']['disclosureCoverage']['complete'] is False
    assert event_factor(result['600999'], NOW)['score'] is None


def test_future_publication_and_missing_security_schema_do_not_become_events(tmp_path):
    provider = ctx.AkshareContextProvider(tmp_path, fetcher=lambda *a: ctx.fetch_recent_disclosures(*a,
        getter=lambda *x, **k: Response({'data': {'list': [announcement(day='2026-10-29')]}})), clock=lambda: NOW)
    table = provider._table('disclosures', {}, NOW, True)
    assert ctx.for_stock({'tables': [table]}, '600008', NOW)['disclosures'] == []
    broken = announcement()
    broken.pop('codes')
    with pytest.raises(ValueError, match='security_schema'):
        ctx.fetch_recent_disclosures('stock_notice_report', {}, 1,
            getter=lambda *x, **k: Response({'data': {'list': [broken]}}))


def test_collect_includes_bounded_disclosure_source_without_per_security_calls(tmp_path, monkeypatch):
    provider = ctx.AkshareContextProvider(tmp_path, clock=lambda: NOW)
    calls = []
    def table(kind, params, cutoff, refresh):
        calls.append((kind, params))
        return {'kind': kind, 'status': 'unavailable', 'rows': []}
    monkeypatch.setattr(provider, '_table', table)
    result = provider.collect(NOW)
    assert sum(kind == 'disclosures' for kind, _ in calls) == 1
    assert any(table['kind'] == 'disclosures' for table in result['tables'])
