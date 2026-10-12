from datetime import datetime, timedelta
import json

import pandas as pd

from src.services.akshare_context import AkshareContextProvider, TZ, for_stock

NOW = datetime(2026, 9, 24, 10, 30, tzinfo=TZ)


def forecast_fetch(endpoint, params, timeout):
    assert endpoint == 'stock_yjyg_em'
    return pd.DataFrame([{'股票代码': '600001', '公告日期': '2026-09-22',
                          '预测指标': '归属净利润', '预告类型': '预增', '业绩变动幅度': 30}])


def test_identical_downloads_preserve_first_observation_and_event_identity(tmp_path):
    clock = [NOW]
    provider = AkshareContextProvider(tmp_path, fetcher=forecast_fetch, clock=lambda: clock[0])
    first = provider._table('forecast', {'date': '20260930'}, NOW, True)
    clock[0] = NOW + timedelta(days=1)
    second = provider._table('forecast', {'date': '20260930'}, clock[0], True)
    assert first['rows'][0]['_eventId'] == second['rows'][0]['_eventId']
    assert second['rows'][0]['_firstSeenAt'] == NOW.isoformat()
    actual = for_stock({'tables': [second]}, '600001', clock[0])['forecasts'][0]
    assert actual['firstSeenAt'] == actual['availableAt'] == NOW.isoformat()
    assert actual['observedAt'] == clock[0].isoformat()
    assert not for_stock({'tables': [second]}, '600001', NOW)['forecasts']


def test_revised_content_gets_new_identity_and_does_not_claim_prior_knowledge(tmp_path):
    clock = [NOW]
    def fetch(*args):
        frame = forecast_fetch(*args)
        if clock[0] > NOW:
            frame['业绩变动幅度'] = 50
        return frame
    provider = AkshareContextProvider(tmp_path, fetcher=fetch, clock=lambda: clock[0])
    first = provider._table('forecast', {'date': '20260930'}, NOW, True)
    clock[0] += timedelta(days=1)
    second = provider._table('forecast', {'date': '20260930'}, clock[0], True)
    assert first['rows'][0]['_eventId'] != second['rows'][0]['_eventId']
    assert second['rows'][0]['_firstSeenAt'] == clock[0].isoformat()


def test_old_archives_without_first_seen_metadata_are_read_without_future_backfill(tmp_path):
    clock = [NOW]
    provider = AkshareContextProvider(tmp_path, fetcher=forecast_fetch, clock=lambda: clock[0])
    provider._table('forecast', {'date': '20260930'}, NOW, True)
    clock[0] += timedelta(days=1)
    provider._table('forecast', {'date': '20260930'}, clock[0], True)
    for path in tmp_path.glob('*/*.json'):
        packet = json.loads(path.read_text(encoding='utf-8'))
        for row in packet['rows']:
            row.pop('_firstSeenAt', None)
            row.pop('_eventId', None)
        path.write_text(json.dumps(packet), encoding='utf-8')
    cached = provider._table('forecast', {'date': '20260930'}, clock[0], True)
    assert cached['rows'][0]['_firstSeenAt'] == NOW.isoformat()
    original = provider._table('forecast', {'date': '20260930'}, NOW, False)
    assert original['observedAt'] == original['rows'][0]['_firstSeenAt'] == NOW.isoformat()
