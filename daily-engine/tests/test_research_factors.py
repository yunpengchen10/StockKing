from datetime import date, datetime, timedelta
import math

import pandas as pd
import pytest

from src.services.yao_scout.research_factors import (
    MarketEmotionArchive, TZ, character_factor, event_factor,
)

NOW = datetime(2026, 9, 24, 10, 30, tzinfo=TZ)


def calendar(start, end):
    return [stamp.date() for stamp in pd.bdate_range(start, end)]


@pytest.fixture(autouse=True)
def fixed_test_calendar(monkeypatch):
    monkeypatch.setattr('src.services.yao_scout.research_factors._sessions', calendar)


def event(**overrides):
    return {'source': 'AKShare/stock_yjyg_em', 'eventId': 'same-disclosure',
            'observedAt': NOW.isoformat(), 'availableAt': '2026-09-23T00:00:00+08:00',
            'firstSeenAt': '2026-09-22T16:00:00+08:00', 'publishedDate': '2026-09-22',
            'reportPeriod': '20260930', 'type': '预增', 'metric': '归属净利润',
            'changePct': 30, **overrides}


def context(*events):
    return {'coverage': {'forecast': True}, 'forecasts': list(events)}


def assert_packet(packet):
    assert packet['status'] in {'observed', 'insufficient', 'unavailable'}
    assert packet['score'] is None or 0 <= packet['score'] <= 100
    assert all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in packet['features'].values())


def test_events_are_deduplicated_and_downloads_do_not_renew_decay():
    one = event_factor(context(event()), NOW, calendar_provider=calendar)
    duplicate = event_factor(context(event(), event(observedAt=NOW.isoformat())), NOW,
                             calendar_provider=calendar)
    assert one['score'] == duplicate['score'] == 75
    assert duplicate['features']['positive_event_count'] == 1
    assert duplicate['features']['duplicate_event_count'] == 1
    tomorrow = NOW + timedelta(days=1)
    refreshed = event_factor(context(event(observedAt=tomorrow.isoformat(),
                                           availableAt=tomorrow.isoformat())), tomorrow,
                             calendar_provider=calendar)
    assert refreshed['score'] == 62.5
    assert_packet(refreshed)


@pytest.mark.parametrize('patch', [
    {'source': ''}, {'firstSeenAt': None}, {'observedAt': (NOW + timedelta(seconds=1)).isoformat()},
    {'availableAt': (NOW + timedelta(seconds=1)).isoformat()},
    {'firstSeenAt': (NOW + timedelta(seconds=1)).isoformat()},
    {'publishedDate': NOW.date().isoformat()}, {'observedAt': '2026-09-24T10:30:00'},
])
def test_unknown_and_future_event_evidence_cannot_be_positive(patch):
    result = event_factor(context(event(**patch)), NOW, calendar_provider=calendar)
    assert result['status'] == 'unavailable' and result['score'] is None
    assert_packet(result)


def test_only_positive_net_profit_forecasts_are_catalysts():
    for row in (event(type='首亏'), event(type='续盈'), event(metric='营业收入')):
        result = event_factor(context(row), NOW, calendar_provider=calendar)
        assert result['score'] == 50
    assert event_factor({'coverage': {}, 'forecasts': []}, NOW)['score'] is None
    assert event_factor(context(), NOW)['score'] == 50
    assert event_factor(context(event()), NOW, calendar_provider=lambda *_: [])['score'] is None


def daily_rows(count=70):
    rows, previous = [], 10.
    for index, day in enumerate(pd.bdate_range(end=NOW.date()-timedelta(days=1), periods=count)):
        close = round(previous * (1.1 if index % 3 == 1 else 1.01), 2)
        rows.append({'date': str(day.date()), 'open': previous, 'high': close,
                     'low': previous * .99, 'close': close, 'pre_close': previous,
                     'limit_up': round(previous * 1.1, 2), 'volume': 100000})
        previous = close
    return pd.DataFrame(rows)


def test_character_requires_unadjusted_prices_and_twenty_mature_events():
    rows = daily_rows()
    assert character_factor(rows, NOW)['score'] is None
    assert character_factor(rows, NOW, price_basis='qfq')['score'] is None
    complete = character_factor(rows, NOW, price_basis='unadjusted')
    assert complete['status'] == 'observed'
    assert complete['features']['mature_event_count'] >= 20
    assert complete['features']['next_session_positive_pct'] == 100
    assert_packet(complete)
    short = character_factor(rows.tail(20), NOW, price_basis='unadjusted')
    assert short['status'] == 'insufficient' and short['score'] is None
    assert 'last_close_location_pct' in short['features']


def test_character_ignores_future_outcomes_and_rejects_conflicting_bars():
    rows = daily_rows()
    baseline = character_factor(rows, NOW, price_basis='unadjusted')
    future = rows.tail(1).copy()
    future['date'] = NOW.date().isoformat()
    future['close'] = 999999
    result = character_factor(pd.concat([rows, future]), NOW, price_basis='unadjusted')
    assert result == baseline
    conflicting = rows.tail(1).copy()
    conflicting['open'] = conflicting['open'] * .999
    assert character_factor(pd.concat([rows, conflicting]), NOW,
                            price_basis='unadjusted')['score'] is None


def test_character_missing_next_session_is_not_assumed_to_be_next_day():
    rows = daily_rows()
    result = character_factor(rows.drop(index=range(2, len(rows), 3)), NOW,
                              price_basis='unadjusted')
    assert result['score'] is None and result['features']['mature_event_count'] == 0
    assert character_factor(rows, NOW, price_basis='unadjusted',
                            calendar_provider=lambda *_: [])['score'] is None


def test_character_old_bars_cannot_extend_the_120_session_event_window():
    rows = daily_rows()
    rows['date'] = [str(day.date()) for day in pd.bdate_range('2019-01-01', periods=len(rows))]
    result = character_factor(rows, NOW, price_basis='unadjusted')
    assert result['status'] == 'insufficient' and result['score'] is None
    assert result['features']['mature_event_count'] == 0
    assert result['eventDates'] == []


def test_character_calendar_without_120_prior_sessions_remains_insufficient():
    result = character_factor(daily_rows(), NOW, price_basis='unadjusted',
        calendar_provider=lambda start, end: calendar(end-timedelta(days=10), end))
    assert result['status'] == 'insufficient' and result['score'] is None


def test_character_exact_calendar_window_keeps_only_mature_event_days():
    rows, previous = [], 10.
    for day in pd.bdate_range(end=NOW.date()-timedelta(days=1), periods=7):
        close = round(previous * 1.1, 2)
        rows.append({'date': str(day.date()), 'open': previous, 'low': previous,
                     'high': close, 'close': close, 'pre_close': previous, 'limit_up': close})
        previous = close
    result = character_factor(pd.DataFrame(rows), NOW, price_basis='unadjusted',
                              lookback=3, min_events=1)
    assert result['status'] == 'observed'
    # 9/23 lies in the three-session window but its next-session outcome is not mature.
    assert result['eventDates'] == ['2026-09-21', '2026-09-22']


@pytest.mark.parametrize('reference,expected_count', [
    ({}, 0), ({'pre_close': 0, 'limit_up': -1}, 0),
    ({'pre_close': 10}, 1), ({'previous_close': 10}, 1), ({'limit_up': 11}, 1),
    ({'pre_close': 10.8}, 0),
])
def test_missing_preceding_session_requires_a_valid_provider_reference(reference, expected_count):
    rows = pd.DataFrame([
        {'date': '2026-09-18', 'open': 10, 'high': 10, 'low': 9.9, 'close': 10},
        # 9/21 is absent; a 10% gain since Friday is not necessarily a one-day limit.
        {'date': '2026-09-22', 'open': 10.5, 'high': 11, 'low': 10.5, 'close': 11, **reference},
        {'date': '2026-09-23', 'open': 11, 'high': 11.22, 'low': 10.9, 'close': 11.22},
    ])
    result = character_factor(rows, NOW, price_basis='unadjusted', min_events=1)
    assert result['features']['mature_event_count'] == expected_count
    assert (result['score'] is not None) == bool(expected_count)


def snapshot(at, changes=(10., 1., -1.), *, highs=True, row_times=False):
    prices = [round(10 * (1 + change / 100), 2) for change in changes]
    result = pd.DataFrame({'code': ['600001', '600002', '600003'], 'name': ['甲', '乙', '丙'],
                           'price': prices, 'previous_close': 10., 'change_pct': changes})
    if highs:
        result['high'] = [max(10., price) for price in prices]
    result.attrs['snapshot_source'] = 'recorded-provider'
    if row_times:
        result['source_time'] = at.isoformat()
    else:
        result.attrs['source_time'] = at.isoformat()
    return result


def archive(tmp_path):
    return MarketEmotionArchive(tmp_path, calendar_provider=calendar,
        policy={'minimumUniverse': 3, 'historySessions': 2})


def test_market_never_substitutes_receipt_date_or_fetch_time_for_source_clock(tmp_path):
    store = archive(tmp_path)
    frame = snapshot(NOW)
    frame.attrs.pop('source_time')
    frame.attrs['fetched_at'] = NOW.isoformat()
    assert store.observe(frame, NOW, received_at=NOW)['score'] is None
    assert not store.path.exists()
    frame['source_time'] = NOW.date().isoformat()
    assert store.observe(frame, NOW, received_at=NOW)['status'] == 'unavailable'
    frame.attrs['source_time'] = NOW.isoformat()
    assert store.observe(frame, NOW, received_at=NOW)['status'] == 'unavailable'
    assert store.observe(snapshot(NOW), NOW)['status'] == 'unavailable'


def test_market_rejects_future_stale_or_partial_clock_coverage(tmp_path):
    store = archive(tmp_path)
    for frame in (snapshot(NOW + timedelta(seconds=1)), snapshot(NOW - timedelta(minutes=6))):
        assert store.observe(frame, NOW, received_at=NOW)['status'] == 'unavailable'
    frame = snapshot(NOW, row_times=True)
    frame.loc[0, 'source_time'] = (NOW + timedelta(minutes=1)).isoformat()
    result = store.observe(frame, NOW, received_at=NOW)
    assert result['status'] == 'unavailable'
    assert result['features']['source_coverage'] == pytest.approx(2/3)


def test_missing_high_or_previous_close_never_invents_board_paths(tmp_path):
    store = archive(tmp_path)
    result = store.observe(snapshot(NOW, highs=False), NOW, received_at=NOW)
    assert 'broken_board_pct' not in result['features']
    assert 'previous_limit_premium_pct' not in result['features']
    frame = snapshot(NOW)
    frame = frame.drop(columns='previous_close')
    result = store.observe(frame, NOW, received_at=NOW)
    assert 'limit_up_pct' not in result['features']
    assert 'broken_board_pct' not in result['features']
    assert_packet(result)


def test_market_needs_distinct_previous_sessions_same_clock_and_is_downward_only(tmp_path):
    store = archive(tmp_path)
    old = NOW - timedelta(days=2)
    assert store.observe(snapshot(old), old, received_at=old)['score'] is None
    # Repeated downloads of one day are not sixty independent historical sessions.
    for _ in range(3):
        assert store.observe(snapshot(old), old, received_at=old)['score'] is None
    yesterday = NOW - timedelta(days=1)
    assert store.observe(snapshot(yesterday), yesterday, received_at=yesterday)['score'] is None
    current = store.observe(snapshot(NOW, changes=(-10., -10., -10.)), NOW, received_at=NOW)
    assert current['status'] == 'observed'
    assert current['regime'] == 'weak' and current['exposureMultiplier'] == .5
    assert current['features']['same_slot_history_sessions'] == 2
    assert_packet(current)
    late = NOW + timedelta(minutes=5)
    assert store.observe(snapshot(late), late, received_at=late)['score'] is None


def test_yesterday_cohort_requires_actual_previous_close_archive(tmp_path):
    store = archive(tmp_path)
    previous_close = (NOW - timedelta(days=1)).replace(hour=15, minute=0)
    # A 15:30 review can retain a real 15:00 close without refreshing its asOf.
    received = previous_close + timedelta(minutes=30)
    recorded = store.observe(snapshot(previous_close), received, received_at=received)
    assert recorded['asOf'] == previous_close.isoformat() and recorded['score'] is None
    current = snapshot(NOW, changes=(10., 0., 0.))
    current.loc[0, 'previous_close'] = 11.
    current.loc[0, ['price', 'high']] = 12.1
    result = store.observe(current, NOW, received_at=NOW)
    assert result['features']['previous_limit_cohort_count'] == 1
    assert result['features']['previous_limit_premium_pct'] == pytest.approx(10)
    assert result['features']['previous_limit_promotion_pct'] == 100
    # A historical decision before the archived close cannot see that cohort.
    earlier = NOW - timedelta(days=1)
    replay = store.observe(snapshot(earlier), earlier, received_at=earlier)
    assert 'previous_limit_premium_pct' not in replay['features']


def test_cohort_corporate_action_gap_and_incomplete_universe_remain_unknown(tmp_path):
    store = archive(tmp_path)
    close = (NOW - timedelta(days=1)).replace(hour=15, minute=0)
    store.observe(snapshot(close), close, received_at=close)
    # Yesterday's limit stock closed 11, today's exchange reference is 5.5.
    # A split must not be reported as a -50% sentiment collapse.
    current = snapshot(NOW)
    current.loc[0, ['previous_close', 'price', 'high']] = [5.5, 5.6, 5.6]
    result = store.observe(current, NOW, received_at=NOW)
    assert 'previous_limit_premium_pct' not in result['features']
    current.attrs['complete'] = False
    assert store.observe(current, NOW, received_at=NOW)['status'] == 'unavailable'
