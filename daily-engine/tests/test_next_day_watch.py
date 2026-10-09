from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import pytest

from src.services.yao_scout.next_day_watch import (
    TZ, VERSION, build_next_day_watchlist, next_day_outcome, review_next_day_signals,
)
from src.services.yao_scout.signal_learning import SignalLearningService
from src.services.yao_scout.minute_history import MinuteArchive, normalize_bars


DAY = date(2026, 9, 18)  # Friday; target is Monday, not the next natural date.
NEXT = date(2026, 9, 21)
NOW = datetime(2026, 9, 18, 14, 55, tzinfo=TZ)


def calendar(start, end):
    return [day for day in (date(2026, 9, 16), date(2026, 9, 17), DAY, NEXT) if start <= day <= end]


def candidate(code='600001', price=10.5):
    indicators = [{'key': key, 'value': value, 'source': 'unadjusted-daily', 'asOf': '2026-09-17'}
                  for key, value in {'ma5': 10., 'ma10': 9.8, 'ma20': 9.6, 'high20': 10.5, 'atr14': .3}.items()]
    indicators.append({'key': 'speed_5m_pct', 'value': .5, 'source': 'minute', 'status': 'observed', 'asOf': NOW.isoformat()})
    return {'code': code, 'name': '研究样本', 'status': 'evidence_insufficient',
            'decision_at': NOW.isoformat(), 'referencePrice': price, 'dailyLastClose': 10.,
            'quote': {'code': code, 'source': 'fixture', 'provider_timestamp': NOW.isoformat(),
                      'price': price, 'pre_close': 10., 'open_price': 10.1, 'high': max(price, 10.6),
                      'low': 10., 'volume': 1000000, 'amount': 10200000},
            'dataEligibility': {'status': 'observation', 'amountHistoryDays': 8, 'priceHistoryDays': 8},
            'precisionDecision': {'profiles': {'regular': {'entryEligible': False}},
                                  'sourceContext': {'coverage': {'forecast': False, 'unlock': False}}},
            'indicatorEvidence': indicators}


def test_independent_next_day_target_survives_missing_entry_baselines_without_promoting_entry():
    row = candidate()
    original = deepcopy(row)
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert row == original
    pick = result['nextDayWatchlist'][0]
    assert pick['nextDaySignal']['targetSession'] == NEXT.isoformat()
    assert pick['nextDaySignal']['probability'] is None
    assert pick['nextDaySignal']['executionEligible'] is False
    assert pick['precisionDecision']['profiles']['regular']['entryEligible'] is False
    assert pick['dataEligibility']['amountHistoryDays'] == 8
    assert pick['status'] == 'next_day_watch'


def test_stale_future_and_missing_identity_quotes_do_not_enter_research():
    rows = [candidate(f'60000{i}') for i in range(1, 4)]
    rows[0]['quote']['provider_timestamp'] = (NOW-timedelta(seconds=31)).isoformat()
    rows[1]['quote']['provider_timestamp'] = (NOW+timedelta(seconds=1)).isoformat()
    rows[2]['quote']['code'] = '600009'
    result = build_next_day_watchlist(rows, NOW, calendar_provider=calendar)
    assert not result['nextDayWatchlist']
    assert result['nextDayResearch']['exclusionCounts']['quote_identity_or_timestamp'] == 3


def test_future_or_stale_daily_prices_are_never_features():
    for day in ('2026-09-18', '2026-09-16'):
        row = candidate()
        row['indicatorEvidence'][0]['asOf'] = day
        assert not build_next_day_watchlist([row], NOW, calendar_provider=calendar)['nextDayWatchlist']


def test_no_filler_and_unknown_snapshot_heat_does_not_change_ranking():
    weak = candidate('600002', price=10.05)
    strong = candidate()
    first = build_next_day_watchlist([weak, strong], NOW, calendar_provider=calendar)
    strong.update(volume_ratio=999, turnover_rate=100, change_pct=999, finalScore=100)
    second = build_next_day_watchlist([weak, strong], NOW, calendar_provider=calendar)
    assert len(first['nextDayWatchlist']) == 1
    assert first['nextDayWatchlist'][0]['nextDaySignal']['score'] == second['nextDayWatchlist'][0]['nextDaySignal']['score']


def test_locked_limit_is_researchable_but_suspension_and_known_adverse_event_are_not():
    row = candidate(price=11.)
    row['quote']['locked_limit_up'] = True
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert not result['nextDayWatchlist']
    assert '触板形态延续（10%价位）' in result['nextDayContinuationWatchlist'][0]['nextDaySignal']['branches']
    assert not result['nextDayContinuationWatchlist'][0]['nextDaySignal']['executionEligible']
    row['quote']['suspended'] = True
    assert not build_next_day_watchlist([row], NOW, calendar_provider=calendar)['researchCandidates']
    row['quote']['suspended'] = False
    row['precisionDecision']['sourceContext']['forecasts'] = [{'type': '首亏', 'metric': '净利润'}]
    assert not build_next_day_watchlist([row], NOW, calendar_provider=calendar)['researchCandidates']


def bars():
    rows = [{'end': datetime.combine(DAY, datetime.min.time(), TZ).replace(hour=15).isoformat(),
             'open': 10.5, 'high': 10.5, 'low': 10.5, 'close': 10.5}]
    for hour, minute in ((9, 31), (13, 1)):
        start = datetime(NEXT.year, NEXT.month, NEXT.day, hour, minute, tzinfo=TZ)
        for index in range(120):
            rows.append({'end': (start+timedelta(minutes=index)).isoformat(),
                         'open': 11., 'high': 11.55, 'low': 11., 'close': 11.3,
                         'volume_shares': 1000, 'amount_cny': 11300, 'source': 'unadjusted-minute'})
    return rows


def signal():
    return {'version': VERSION, 'code': '600001', 'signalSession': DAY.isoformat(), 'selected': True}


def test_next_session_touch_and_close_are_separate_and_use_final_close():
    rows = bars()
    cutoff = datetime(2026, 9, 21, 15, 1, tzinfo=TZ)
    outcome = next_day_outcome(signal(), rows, cutoff, calendar_provider=calendar)
    assert outcome['status'] == 'mature_price_pattern'
    assert outcome['limitPrice'] == 11.55  # T close10.5, not quote preclose10.
    assert outcome['touchLimit'] is True and outcome['closeAtLimit'] is False
    assert outcome['executionVerified'] is False
    rows[-1]['close'] = 11.55
    assert next_day_outcome(signal(), rows, cutoff, calendar_provider=calendar)['closeAtLimit'] is True


def test_missing_minutes_and_incomplete_target_do_not_become_negative_labels():
    before_close = datetime(2026, 9, 21, 14, 59, tzinfo=TZ)
    assert next_day_outcome(signal(), bars(), before_close, calendar_provider=calendar)['status'] == 'pending'
    result = next_day_outcome(signal(), bars()[1:], before_close+timedelta(minutes=2), calendar_provider=calendar)
    assert result['status'] == 'incomplete' and result['touchLimit'] is None
    result = next_day_outcome(signal(), bars()[:-1], before_close+timedelta(minutes=2), calendar_provider=calendar)
    assert result['status'] == 'incomplete' and result['closeAtLimit'] is None


def test_frozen_t1_ledger_is_independent_of_entry_training_and_review_is_idempotent(tmp_path):
    row = candidate()
    row['nextDaySignal'] = signal()
    row['dailyPreviousSession'] = {'asOf': '2026-09-17', 'preCloseAsOf': '2026-09-16',
        'source': 'daily', 'priceBasis': 'unadjusted', 'preClose': 9.9, 'close': 10., 'high': 10.1}
    service = SignalLearningService(tmp_path, bar_fetcher=lambda *_: bars(), calendar_provider=calendar, clock=lambda: NOW)
    service.persist_scan({'run_id': 'new-t1', 'status': 'completed_observations', 'candidates': [], 'controls': [row]}, 'live', NOW, False)
    cutoff = datetime(2026, 9, 21, 15, 1, tzinfo=TZ)
    result = review_next_day_signals(service, cutoff)
    assert result['maturePatterns'] == 1
    assert result['items'][0]['selected'] is True
    assert review_next_day_signals(service, cutoff)['updated'] == 0
    with service._connect() as db:
        saved = db.execute('SELECT training_eligible,snapshot_json FROM signal_events').fetchone()
        assert not saved['training_eligible']
        assert json.loads(saved['snapshot_json'])['nextDaySignal'] == signal()
        assert json.loads(saved['snapshot_json'])['dailyPreviousSession'] == row['dailyPreviousSession']
    assert service.reviews()['nextDayReview']['items'][0]['status'] == 'mature_price_pattern'


def test_calendar_failure_is_a_visible_gap_not_a_scan_exception():
    def failed(*_):
        raise RuntimeError('calendar unavailable')
    result = build_next_day_watchlist([candidate()], NOW, calendar_provider=failed)
    assert result['nextDayResearch']['exclusionCounts'] == {'calendar_unavailable': 1}
    assert not result['nextDayWatchlist']


def test_old_whole_decision_and_unverified_minute_value_cannot_create_new_signal():
    row = candidate()
    assert not build_next_day_watchlist([row], NOW+timedelta(days=3), calendar_provider=calendar)['nextDayWatchlist']
    row['indicatorEvidence'][-1]['status'] = 'unavailable'
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert result['nextDayWatchlist'][0]['nextDaySignal']['components']['minuteStrength'] == 0


def test_three_percent_untouched_setup_is_primary_and_limit_leaders_cannot_crowd_it_out():
    latent = candidate('600099', price=10.3)
    latent['quote']['high'] = 10.4
    leaders = [candidate(f'60000{i}', price=11.) for i in range(1, 7)]
    result = build_next_day_watchlist([*leaders, latent], NOW, calendar_provider=calendar)
    assert [row['code'] for row in result['nextDayWatchlist']] == ['600099']
    assert len(result['nextDayContinuationWatchlist']) == 5
    signal = result['nextDayWatchlist'][0]['nextDaySignal']
    assert signal['queue'] == 'first_board' and signal['queueLabel'] == '未触板潜伏'
    assert abs(signal['features']['changePct']-3.) < 1e-8
    assert signal['touchedLimitToday'] is False and signal['selected'] is True
    assert all(row['nextDaySignal']['queue'] == 'continuation' for row in result['nextDayContinuationWatchlist'])
    assert {row['code'] for row in result['nextDayWatchlist']}.isdisjoint(
        row['code'] for row in result['nextDayContinuationWatchlist'])


def test_limit_touch_then_retreat_to_three_percent_never_enters_latent_pool():
    row = candidate(price=10.3)
    row['quote']['high'] = 11.
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert not result['nextDayWatchlist']
    assert result['nextDayResearch']['populationCounts']['todayTouched'] == 1


def test_seven_percent_untouched_is_not_disguised_as_ordinary_or_continuation():
    row = candidate(price=10.7)
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert not result['nextDayWatchlist'] and not result['nextDayContinuationWatchlist']
    assert result['nextDayResearch']['exclusionCounts']['outside_ordinary_change_band'] == 1


def test_price_tick_limit_and_ordinary_move_boundaries_are_not_integer_percentage_tests():
    row = candidate(price=6.75)
    row['dailyLastClose'] = row['quote']['pre_close'] = 6.14
    row['quote'].update(open_price=6.2, high=6.75, low=6.14, amount=6400000)
    for item in row['indicatorEvidence'][:-1]:
        item['value'] = {'ma5': 6.2, 'ma10': 6.1, 'ma20': 6., 'high20': 6.7, 'atr14': .2}[item['key']]
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert result['nextDayContinuationWatchlist'][0]['nextDaySignal']['touchedLimitToday'] is True
    assert not result['nextDayWatchlist']  # 9.93%, but the rounded limit really is 6.75.
    result = build_next_day_watchlist([candidate(price=10.5)], NOW, calendar_provider=calendar)
    assert len(result['nextDayWatchlist']) == 1  # 5.000000000000004% from IEEE arithmetic.


def test_yesterday_limit_close_cannot_be_called_first_board_and_unknown_history_is_disclosed():
    row = candidate(price=10.3)
    row['quote']['high'] = 10.4
    row['dailyPreviousSession'] = {'asOf': '2026-09-17', 'preCloseAsOf': '2026-09-16',
        'source': 'daily', 'priceBasis': 'unadjusted', 'preClose': 9.09, 'close': 10., 'high': 10.}
    result = build_next_day_watchlist([row], NOW, calendar_provider=calendar)
    assert not result['nextDayWatchlist']
    assert result['nextDayResearch']['exclusionCounts']['previous_session_limit_close'] == 1
    row['dailyPreviousSession']['priceBasis'] = 'adjusted'
    pick = build_next_day_watchlist([row], NOW, calendar_provider=calendar)['nextDayWatchlist'][0]
    assert pick['nextDaySignal']['previousSessionPattern']['status'] == 'unknown'
    assert pick['nextDaySignal']['queueLabel'] == '未触板潜伏'
    assert any('昨日' in gap for gap in pick['nextDaySignal']['gaps'])


def test_pool_specific_scoring_prefers_preparation_over_greater_price_extension():
    ready = candidate('600001', price=10.3)
    ready['quote']['high'] = 10.4
    extended = candidate('600002', price=10.5)
    extended['quote']['high'] = 10.5
    result = build_next_day_watchlist([extended, ready], NOW, calendar_provider=calendar)
    assert result['nextDayWatchlist'][0]['code'] == '600001'
    assert result['nextDayWatchlist'][0]['nextDaySignal']['components']['lowExtension'] > result['nextDayWatchlist'][1]['nextDaySignal']['components']['lowExtension']


def test_previous_v1_outcomes_keep_version_and_are_not_reclassified_into_new_pools(tmp_path):
    row = candidate()
    row['nextDaySignal'] = {**signal(), 'version': 'next-day-limit-watch-v1'}
    service = SignalLearningService(tmp_path, bar_fetcher=lambda *_: bars(), calendar_provider=calendar, clock=lambda: NOW)
    service.persist_scan({'run_id': 'v1-history', 'status': 'completed_observations', 'candidates': [], 'controls': [row]}, 'live', NOW, False)
    result = review_next_day_signals(service, datetime(2026, 9, 21, 15, 1, tzinfo=TZ))
    assert result['items'][0]['version'] == 'next-day-limit-watch-v1'
    assert result['items'][0]['queue'] == 'legacy_mixed'
    assert result['items'][0]['status'] == 'mature_price_pattern'


def test_limit_move_capacity_uses_quoted_price_and_observed_atr_not_stock_identity():
    row = candidate(price=10.3)
    row['quote']['high'] = 10.4
    original = build_next_day_watchlist([row], NOW, calendar_provider=calendar)['nextDayWatchlist'][0]
    assert original['nextDaySignal']['components']['rangeCapacity'] > 0
    row['name'] = '另一个名称'
    renamed = build_next_day_watchlist([row], NOW, calendar_provider=calendar)['nextDayWatchlist'][0]
    assert renamed['nextDaySignal']['score'] == original['nextDaySignal']['score']
    # Keep extension within its separate maximum while making 10% much larger
    # than the historical daily range; capacity must fall to zero.
    for indicator in row['indicatorEvidence']:
        if indicator['key'] == 'ma5':
            indicator['value'] = 10.25
        if indicator['key'] == 'atr14':
            indicator['value'] = .10
    calm = build_next_day_watchlist([row], NOW, calendar_provider=calendar)['nextDayWatchlist'][0]
    assert calm['nextDaySignal']['components']['rangeCapacity'] == 0
    assert calm['nextDaySignal']['features']['limitMoveInAtr'] == 10
    row['indicatorEvidence'] = [item for item in row['indicatorEvidence'] if item['key'] != 'atr14']
    assert not build_next_day_watchlist([row], NOW, calendar_provider=calendar)['nextDayWatchlist']


@pytest.mark.parametrize('flag', ['corporate_action', 'no_price_limit'])
def test_special_session_flags_survive_normalization_archive_and_real_review_chain(tmp_path, flag):
    cutoff = datetime(2026, 9, 21, 15, 1, tzinfo=TZ)
    special = bars()
    special[-1][flag] = True
    assert next_day_outcome(signal(), special, cutoff, calendar_provider=calendar)['status'] == 'incomplete'
    # A duplicate with later receipt and missing/false metadata cannot erase it.
    duplicate = {**special[-1], flag: False, 'fetched_at': cutoff.isoformat()}
    normalized = list(normalize_bars([*special, duplicate], cutoff).values())
    assert normalized[-1][flag] is True
    assert next_day_outcome(signal(), normalized, cutoff, calendar_provider=calendar)['status'] == 'incomplete'
    archive = MinuteArchive(tmp_path/'archive')
    archive.save('600001', special, cutoff)
    archive.save('600001', bars(), cutoff)
    stored = archive.read('600001', cutoff)
    assert stored[-1][flag] is True
    row = candidate()
    row['nextDaySignal'] = signal()
    service = SignalLearningService(tmp_path, bar_fetcher=lambda *_: stored, calendar_provider=calendar, clock=lambda: NOW)
    service.persist_scan({'run_id': 'special-day-'+flag, 'status': 'completed_observations', 'candidates': [], 'controls': [row]}, 'live', NOW, False)
    reviewed = review_next_day_signals(service, cutoff)['items'][0]
    assert reviewed['status'] == 'incomplete'
    assert reviewed['touchLimit'] is None and reviewed['closeAtLimit'] is None
