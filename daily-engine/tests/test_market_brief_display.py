"""Offline publication tests for independently sourced market watch briefs."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.services.stock_king_display import LocalPicksDisplayStore, _market_brief_valid, displayable_run
from src.services.yao_scout.market_context import build_market_brief


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
VERSION = 'opportunity-flow-20261103-v1'


@pytest.fixture(autouse=True)
def offline_calendar(monkeypatch):
    from src.services.yao_scout import daily_opportunities, premarket
    monkeypatch.setattr(daily_opportunities, 'is_market_open', lambda market, day: day.weekday() < 5)
    monkeypatch.setattr(premarket, 'previous_session', lambda now: now.date()-timedelta(days=1))


def scan(now=NOW, *, previous=False):
    source_at = now.replace(hour=11, minute=30) if now.hour == 12 else now.replace(hour=15, minute=0) if now.hour >= 15 else now
    event_at = now.replace(hour=9, minute=0) - (timedelta(days=1) if previous else timedelta())
    quote = {'code': '600001', 'name': '事件关联样本', 'source': 'fixture:quotes', 'source_time': source_at.isoformat(),
             'price': 10.2, 'pre_close': 10, 'high': 10.3, 'low': 10, 'open_price': 10.1, 'change_pct': 2}
    packet = {'observedAt': now.isoformat(), 'events': {'status': 'observed', 'observedAt': now.isoformat(),
        'source': 'ths:concept-events', 'items': [{'eventId': 'event-1', 'date': event_at.date().isoformat(),
        'sourceTime': event_at.isoformat(), 'title': '有来源的产业事件', 'heat': 3,
        'themes': [{'showName': '事件题材'}], 'topStocks': [{'stockCode': '600001', 'stockName': quote['name'], 'reason': '事件关联业务'}]}]}}
    channels = {'version': VERSION, 'asOf': now.isoformat(), 'recommendations': [], 'observations': []}
    brief = build_market_brief(packet, {'600001': quote}, now, opportunities=channels, previous_day='2026-11-12')
    return {'run_id': 'brief-only-'+now.isoformat(), 'mode': 'king_live', 'scanSlot': 'live',
        'generatedAt': now.isoformat(), 'status': 'completed_observations', 'candidates': [], 'profileCandidates': {'regular': []},
        'primarySelectionMode': 'daily_observation', 'dailyAssessment': {'status': 'unavailable'},
        'dataQuality': {'snapshot_count': 0, 'stale': True, 'quote_coverage': {'requested': 10, 'fresh': 0},
                        'minute_coverage': {'requested': 10, 'usable': 0}},
        'marketBrief': brief, 'opportunityChannels': channels}


@pytest.mark.parametrize('clock,previous', [(NOW, False), (NOW, True), (NOW.replace(hour=10, minute=30), False),
                                           (NOW.replace(hour=15, minute=30), False)])
def test_real_watch_brief_persists_without_minutes_funds_or_daily_assessment(tmp_path, monkeypatch, clock, previous):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    run = scan(clock, previous=previous)
    before = deepcopy(run)
    assert _market_brief_valid(run) and displayable_run(run)
    store = LocalPicksDisplayStore(None)
    shown = store.save(run)
    assert shown['adaptive']['marketBrief']['focusStocks'][0]['status'] == 'watch'
    assert shown['savedRecommendationCount'] == shown['opportunityRecommendationCount'] == 0
    assert LocalPicksDisplayStore(None).read()['adaptive']['marketBrief'] == run['marketBrief']
    assert run == before


@pytest.mark.parametrize('mutation', ['unknown_version', 'old_assessment', 'future_assessment', 'naive_publication',
    'naive_quote', 'future_quote', 'previous_day_quote', 'old_intraday_quote', 'fake_lunch_quote', 'bad_price',
    'boolean_price', 'missing_source', 'missing_event_source', 'old_event_day', 'future_event_time',
    'mismatched_theme', 'missing_leader', 'executable', 'watch_recommended', 'fake_conditions', 'duplicate_stock',
    'too_many_stocks', 'formal_candidates', 'formal_profile', 'unknown_opportunity_version', 'invalid_quote'])
def test_invalid_brief_cannot_replace_saved_result(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    store.save(scan())
    saved = store.path.read_bytes()
    run = scan()
    brief, row, theme = run['marketBrief'], run['marketBrief']['focusStocks'][0], run['marketBrief']['mainlines'][0]
    if mutation == 'unknown_version': brief['version'] = 'unknown'
    elif mutation == 'old_assessment': brief['asOf'] = (NOW-timedelta(seconds=31)).isoformat()
    elif mutation == 'future_assessment': brief['asOf'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'naive_publication': run['generatedAt'] = NOW.replace(tzinfo=None).isoformat()
    elif mutation == 'naive_quote': row['sourceTime'] = NOW.replace(tzinfo=None).isoformat()
    elif mutation == 'future_quote': row['sourceTime'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'previous_day_quote': row['sourceTime'] = (NOW-timedelta(days=1)).isoformat()
    elif mutation == 'old_intraday_quote': row.update(quoteFreshness='live', sourceTime=(NOW-timedelta(seconds=301)).isoformat())
    elif mutation == 'fake_lunch_quote': row['sourceTime'] = NOW.replace(hour=10).isoformat()
    elif mutation == 'bad_price': row['price'] = 0
    elif mutation == 'boolean_price': row['price'] = True
    elif mutation == 'missing_source': row['source'] = ''
    elif mutation == 'missing_event_source': theme['source'] = ''
    elif mutation == 'old_event_day': theme['eventDate'] = '2026-11-11'
    elif mutation == 'future_event_time': theme['eventSourceTime'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'mismatched_theme': row['themeId'] = 'unrelated-event'
    elif mutation == 'missing_leader': theme['leaders'] = ['600002']
    elif mutation == 'executable': row['executable'] = True
    elif mutation == 'watch_recommended': row['ruleRecommendation'] = True
    elif mutation == 'fake_conditions': row.update(status='conditions_met', ruleRecommendation=True)
    elif mutation == 'duplicate_stock': brief['focusStocks'].append(deepcopy(row))
    elif mutation == 'too_many_stocks': brief['focusStocks'] *= 6
    elif mutation == 'formal_candidates': run['candidates'] = [{'code': '600002'}]
    elif mutation == 'formal_profile': run['profileCandidates']['aggressive'] = [{'code': '600002'}]
    elif mutation == 'unknown_opportunity_version': run['opportunityChannels']['version'] = 'unknown'
    elif mutation == 'invalid_quote': row['conflict'] = True
    assert not _market_brief_valid(run)
    assert not displayable_run(run)
    with pytest.raises(ValueError):
        store.save(run)
    assert store.path.read_bytes() == saved


def with_formal_recommendation():
    now = NOW.replace(hour=10, minute=30)
    run = scan(now)
    row = run['marketBrief']['focusStocks'][0]
    row.update(status='conditions_met', ruleRecommendation=True)
    run['dataQuality']['snapshot_count'] = 100
    run['opportunityChannels']['recommendations'] = [{'code': row['code'], 'ruleRecommendation': True,
        'quote': {'code': row['code'], 'source': row['source'], 'source_time': row['sourceTime'], 'price': row['price'],
                  'pre_close': 10, 'high': 10.3, 'low': 10, 'open_price': 10.1, 'change_pct': 2},
        'opportunity': {'version': VERSION, 'grade': 'F-A', 'hardVetoes': [], 'failedConditions': [], 'buyWindow': {'status': 'observed'}}}]
    return run


def test_condition_flag_requires_valid_matching_formal_recommendation():
    run = with_formal_recommendation()
    assert _market_brief_valid(run) and displayable_run(run)
    run['marketBrief']['focusStocks'][0]['price'] = 11
    assert not _market_brief_valid(run)


@pytest.mark.parametrize('mutation', ['missing_quote', 'hard_veto', 'wrong_version', 'not_recommended', 'unverified_window', 'failed_conditions'])
def test_market_brief_cannot_launder_damaged_existing_formal_recommendations(mutation):
    run = with_formal_recommendation()
    row = run['opportunityChannels']['recommendations'][0]
    if mutation == 'missing_quote': row['quote'] = {}
    elif mutation == 'hard_veto': row['opportunity']['hardVetoes'] = ['quote_conflict']
    elif mutation == 'wrong_version': row['opportunity']['version'] = 'unknown'
    elif mutation == 'not_recommended': row['ruleRecommendation'] = False
    elif mutation == 'unverified_window': row['opportunity']['buyWindow']['status'] = 'unavailable'
    elif mutation == 'failed_conditions': row['opportunity']['failedConditions'] = ['stock_flow_unavailable']
    assert not _market_brief_valid(run)
    assert not displayable_run(run)


def test_metadata_header_recovery_finds_a_brief_only_scan(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    run = scan()
    db = SimpleNamespace(list_yao_run_headers=lambda **kwargs: [{
        'id': 1, 'run_id': run['run_id'], 'mode': run['mode'], 'status': run['status'], 'as_of': run['generatedAt']}],
        get_yao_run=lambda run_id: {'result': deepcopy(run)})
    shown = LocalPicksDisplayStore(db).read()
    assert shown['adaptive']['marketBrief'] == run['marketBrief']
    assert shown['displaySource'] == 'migration'


@pytest.mark.parametrize('source_minute', [30, 43, 44, 59])
def test_verified_lunch_reference_keeps_actual_updating_provider_clock(source_minute):
    run = scan()
    row = run['marketBrief']['focusStocks'][0]
    row['sourceTime'] = NOW.replace(hour=11, minute=source_minute).isoformat()
    assert row['quoteFreshness'] == 'session_reference'
    assert _market_brief_valid(run) and displayable_run(run)
    row.update(status='conditions_met', ruleRecommendation=True)
    assert not _market_brief_valid(run)
