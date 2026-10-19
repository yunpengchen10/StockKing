"""Opening scans publish daily-backed plans without manufacturing minute signals."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pandas as pd
import pytest

from tests.test_local_quote_integration import make_service
from src.services.yao_scout import daily_opportunities as base
from src.services.stock_king_display import LocalPicksDisplayStore, displayable_run


def opening_service(monkeypatch, tmp_path, hour=9, minute=20):
    start = datetime(2026, 9, 14, hour, minute, tzinfo=base.TZ)
    service, tick, events, saved = make_service(monkeypatch, start, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('opening scans must not fetch unavailable intraday windows')
    service.minute_fetcher = service.sector_fetcher = service.fund_fetcher = forbidden
    return service, tick, events, saved


@pytest.mark.parametrize('hour,minute,phase', [
    (8,55,'overnight_plan'), (9,15,'auction_cancellable'),
    (9,20,'auction_non_cancellable'), (9,25,'auction_priced'),
    (9,30,'opening_handoff'), (9,34,'opening_handoff')])
def test_scan_and_saved_display_work_without_today_minutes(monkeypatch, tmp_path, hour, minute, phase):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path / 'display'))
    service, _, _, _ = opening_service(monkeypatch, tmp_path, hour, minute)
    result = service.run('live')
    assert result['status'] == 'completed_observations', result.get('message')
    assert result['premarketAssessment']['phase'] == phase
    assert result['premarketAssessment']['baselineSession'] == '2026-09-11'
    assert result['premarketCandidates']
    assert not result['candidates']
    assert not any(result['profileCandidates'].values())
    assert result['dataQuality']['minute_coverage']['requested'] == 0
    assert result['learningLedger']['selectedCount'] == 0
    store = LocalPicksDisplayStore(None)
    shown = store.save(result)
    assert shown['premarketCandidateCount'] == 1
    assert shown['tradeCandidateCount'] == 0
    assert store.read() == shown
    assert json.loads(store.path.read_text(encoding='utf-8'))['adaptive'] == result


def test_ordinary_quote_missing_ohlc_and_stale_market_directory_do_not_block_plan(monkeypatch, tmp_path):
    service, tick, _, _ = opening_service(monkeypatch, tmp_path)
    original = service.yao._fetch_snapshot
    def old_directory():
        frame = original()
        frame.attrs['stale'] = True
        return frame
    service.yao._fetch_snapshot = old_directory
    service.quote_fetcher = lambda code: {'code': code, 'source': 'tencent', 'price': 10,
        'pre_close': 9.8, 'provider_timestamp': tick[0].isoformat()}
    result = service.run('live')
    assert displayable_run(result), result.get('message')
    pick = result['premarketCandidates'][0]
    assert pick['quoteStatus'] == 'fresh'
    assert pick['auctionConfirmed'] is False
    assert pick['openingTradeObserved'] is False


def test_missing_quote_keeps_explicit_overnight_plan_but_not_execution(monkeypatch, tmp_path):
    service, _, _, _ = opening_service(monkeypatch, tmp_path)
    service.quote_fetcher = lambda code: {}
    result = service.run('live')
    assert displayable_run(result)
    pick = result['premarketCandidates'][0]
    assert pick['referencePriceKind'] == 'previous_close'
    assert pick['quote'] == {}
    assert pick['executable'] is False
    assert pick['entryEligible'] is False


def test_unavailable_daily_history_cannot_replace_saved_display(monkeypatch, tmp_path):
    service, _, _, _ = opening_service(monkeypatch, tmp_path)
    service.yao.history_fetcher = lambda *a, **k: pd.DataFrame()
    result = service.run('live')
    assert result['premarketCandidates'] == []
    assert result['premarketAssessment']['dailyUsableCount'] == 0
    assert not displayable_run(result)


def test_scan_crossing_open_handoffs_without_waiting_for_minutes(monkeypatch, tmp_path):
    service, tick, _, _ = opening_service(monkeypatch, tmp_path, 9, 29)
    original = service.quote_fetcher
    def opening_quote(code):
        tick[0] = tick[0].replace(hour=9, minute=30, second=1)
        return original(code)
    service.quote_fetcher = opening_quote
    result = service.run('live')
    assert result['premarketAssessment']['phase'] == 'opening_handoff'
    assert result['premarketCandidates'][0]['openingTradeObserved'] is True
    assert displayable_run(result)


def test_scan_crossing_0935_cannot_publish_opening_plan(monkeypatch, tmp_path):
    service, tick, _, _ = opening_service(monkeypatch, tmp_path, 9, 34)
    original = service.quote_fetcher
    def late_quote(code):
        tick[0] = tick[0].replace(minute=35)
        return original(code)
    service.quote_fetcher = late_quote
    result = service.run('live')
    assert result['status'] == 'unavailable'
    assert not displayable_run(result)


def test_daily_valid_empty_selection_is_successful(monkeypatch, tmp_path):
    service, _, _, _ = opening_service(monkeypatch, tmp_path)
    original = service.yao.history_fetcher
    def falling(*a, **kw):
        history = original(*a, **kw).copy()
        history['close'] = list(reversed(history.close.tolist()))
        history['high'], history['low'] = history.close + .2, history.close - .2
        return history
    service.yao.history_fetcher = falling
    result = service.run('live')
    assert result['premarketAssessment']['dailyUsableCount'] == 1
    assert result['premarketCandidates'] == []
    assert displayable_run(result)


def test_storage_header_restores_opening_plan_after_restart(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage import DatabaseManager, YaoRun
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path / 'display'))
    service, _, _, _ = opening_service(monkeypatch, tmp_path)
    result = service.run('live')
    engine = create_engine(f"sqlite:///{(tmp_path / 'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    try:
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), [{
                'run_id': result['run_id'], 'mode': 'king_live', 'status': result['status'],
                'as_of_at': datetime(2026,9,14,9,20), 'cutoff_at': datetime(2026,9,14,9,20),
                'model_version': result['modelVersion'], 'result_json': json.dumps(result)}])
        shown = LocalPicksDisplayStore(db).read()['adaptive']
        assert shown['premarketCandidates'] == result['premarketCandidates']
        assert shown['premarketAssessment'] == result['premarketAssessment']
        assert shown['candidates'] == []
    finally:
        engine.dispose()


def test_opening_metadata_never_bypasses_intraday_outage(monkeypatch, tmp_path):
    service, _, _, _ = opening_service(monkeypatch, tmp_path)
    result = service.run('live')
    broken = deepcopy(result)
    broken['generatedAt'] = '2026-09-14T10:30:00+08:00'
    assert not displayable_run(broken)
    broken = deepcopy(result)
    broken['premarketAssessment']['baselineSession'] = '2026-09-10'
    assert not displayable_run(broken)


@pytest.mark.parametrize('empty_response', [False, True])
def test_previous_session_names_are_refreshed_when_snapshot_is_down(monkeypatch, tmp_path, empty_response):
    service, _, _, _ = opening_service(monkeypatch, tmp_path)
    service.db.list_yao_runs = lambda **kw: [{'mode': 'king_1455', 'as_of': '2026-09-11T14:55:00+08:00',
        'result': {'generatedAt': '2026-09-11T14:55:00+08:00', 'candidates': [],
                   'nextDayWatchlist': [{'code': '600001', 'name': '测试股'}]}}]
    def down():
        if empty_response:
            return pd.DataFrame()
        raise OSError('snapshot provider down')
    service.yao._fetch_snapshot = down
    result = service.run('live')
    assert result['dataQuality']['snapshot_fallback'] == 'overnight_directory_only'
    assert result['premarketAssessment']['overnightSeedCount'] == 1
    assert result['premarketCandidates']
    assert displayable_run(result)
