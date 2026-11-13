from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.services.yao_scout.market_context import build_market_brief, event_candidate_codes, prioritize_market_queue
from src.services.software_market import SoftwareMarketClient
from src.services.stock_king_display import _display_response

NOW = datetime(2026, 11, 13, 10, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


def context(now=NOW, day='2026-11-13'):
    return {'observedAt': now.isoformat(), 'events': {'status': 'observed', 'source': 'ths:concept-events',
        'observedAt': now.isoformat(), 'items': [{'eventId': 'event1', 'date': day,
        'sourceTime': now.replace(day=int(day[-2:]), hour=9).isoformat(), 'title': '测试产业新事件', 'heat': 15,
        'themes': [{'showName': '测试题材', 'indexCode': 'test'}], 'topStocks': [
            {'stockCode': '600001', 'stockName': '测试甲', 'risePercent': 99, 'reason': '事件关联业务'},
            {'stockCode': '600002', 'stockName': '测试乙', 'risePercent': 99}]}]},
        'funds': {'status': 'unavailable', 'items': []}, 'news': {'status': 'unavailable', 'items': []}}


def quotes(now=NOW):
    return {code: {'code': code, 'name': '测试股', 'price': price, 'pre_close': 10,
        'change_pct': (price/10-1)*100, 'source': 'fixture:quotes', 'source_time': now.isoformat()}
        for code, price in [('600001', 10.2), ('600002', 10.1)]}


def brief(packet=None, prices=None, now=NOW, **kwargs):
    return build_market_brief(context(now) if packet is None else packet,
        quotes(now) if prices is None else prices, now, previous_day='2026-11-12', **kwargs)


def test_event_ideas_survive_missing_full_concept_board_and_funds():
    result = brief()
    assert len(result['focusStocks']) == 2
    assert all(row['status'] == 'watch' and row['pending'] and not row['ruleRecommendation'] for row in result['focusStocks'])
    assert result['focusStocks'][0]['changePct'] == pytest.approx(2)
    assert result['mainlines'][0]['rankScope'] == '事件关联股票，非全概念成分排名'


def test_yesterday_event_is_dated_continuation_with_today_quotes():
    packet = context(day='2026-11-12')
    packet['events']['status'] = 'reference_only'
    result = brief(packet)
    assert result['mainlines'][0]['status'] == 'continuation'
    assert result['mainlines'][0]['eventDate'] == '2026-11-12'
    assert result['focusStocks'][0]['sourceTime'] == NOW.isoformat()
    assert result['focusStocks'][0]['eventSourceTime'].startswith('2026-11-12')


@pytest.mark.parametrize('mutation', ['stale_quote', 'future_quote', 'old_date', 'unknown_clock', 'conflict', 'bad_price', 'no_source', 'wrong_code', 'explicit_invalid', 'nested_invalid'])
def test_invalid_evidence_never_becomes_a_focus_stock(mutation):
    prices = quotes()
    row = prices['600001']
    if mutation == 'stale_quote': row['source_time'] = (NOW-timedelta(seconds=301)).isoformat()
    elif mutation == 'future_quote': row['source_time'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'old_date': row['source_time'] = (NOW-timedelta(days=1)).isoformat()
    elif mutation == 'unknown_clock': row.pop('source_time')
    elif mutation == 'conflict': row['conflict'] = True
    elif mutation == 'bad_price': row['price'] = 100
    elif mutation == 'no_source': row['source'] = None
    elif mutation == 'wrong_code': row['code'] = '600003'
    elif mutation == 'explicit_invalid': row['status'] = 'invalid'
    else: row['public_quote_metadata'] = {'issues': ['cross_source_price_conflict']}
    assert [row['code'] for row in brief(prices=prices)['focusStocks']] == ['600002']


@pytest.mark.parametrize('mutation', ['old_event', 'future_event', 'future_observed', 'old_observed', 'no_source'])
def test_unusable_events_do_not_pass(mutation):
    packet = context()
    event = packet['events']['items'][0]
    if mutation == 'old_event': event['date'] = '2026-11-10'
    elif mutation == 'future_event': event['sourceTime'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'future_observed': packet['events']['observedAt'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'old_observed': packet['events']['observedAt'] = (NOW-timedelta(minutes=11)).isoformat()
    else: packet['events']['source'] = None
    assert brief(packet)['focusStocks'] == []


def test_failed_current_quotes_cannot_be_replaced_by_event_rise_percent():
    assert brief(prices={})['focusStocks'] == []


def test_negative_and_st_stocks_are_not_used_to_fill_focus_list():
    prices = quotes()
    prices['600001'].update(price=9.9, change_pct=-1)
    prices['600002']['name'] = '*ST测试'
    assert brief(prices=prices)['focusStocks'] == []


def test_exact_fund_match_is_a_timestamped_observation_not_flow_confirmation():
    packet = context()
    packet['funds'] = {'status': 'observed', 'source': 'eastmoney:db', 'items': [
        {'name': '测试题材', 'netInflow': 500, 'snapshotObservedAt': NOW.isoformat(), 'sourceTime': None}]}
    result = brief(packet)
    fund = result['mainlines'][0]['funds']
    assert fund['status'] == 'snapshot_only' and fund['sourceTime'] is None and fund['netInflow'] == 500
    assert fund['referenceAgeSeconds'] == 0 and fund['stale'] is False
    assert result['focusStocks'][0]['status'] == 'watch'
    packet['funds']['items'][0]['snapshotObservedAt'] = (NOW-timedelta(minutes=22)).isoformat()
    older = brief(packet)
    assert older['mainlines'][0]['funds']['referenceAgeSeconds'] == 1320
    assert older['mainlines'][0]['funds']['stale'] is True
    assert older['mainlines'][0]['funds']['sourceTime'] is None
    assert all(row['status'] == 'watch' and not row['ruleRecommendation'] for row in older['focusStocks'])
    packet['funds']['items'][0]['snapshotObservedAt'] = (NOW+timedelta(seconds=1)).isoformat()
    assert brief(packet)['mainlines'][0]['funds']['status'] == 'unavailable'
    packet['funds']['items'][0]['snapshotObservedAt'] = (NOW-timedelta(days=1)).isoformat()
    assert brief(packet)['mainlines'][0]['funds']['status'] == 'unavailable'


def test_related_news_is_same_day_and_not_future():
    packet = context()
    packet['news'] = {'observedAt': NOW.isoformat(), 'items': [
        {'title': '测试题材最新进展', 'sourceTime': NOW.isoformat()},
        {'title': '测试题材旧消息', 'sourceTime': (NOW-timedelta(days=1)).isoformat()},
        {'title': '测试题材未来消息', 'sourceTime': (NOW+timedelta(seconds=1)).isoformat()},
        {'title': '无关新闻', 'sourceTime': NOW.isoformat()}]}
    assert [row['title'] for row in brief(packet)['mainlines'][0]['news']] == ['测试题材最新进展']


def test_only_independent_assessment_can_supply_conditions_met():
    result = brief(opportunities={'asOf': NOW.isoformat(), 'recommendations': [{'code': '600001',
        'ruleRecommendation': True, 'opportunity': {'grade': 'F-A', 'hardVetoes': [], 'failedConditions': []}}]})
    assert result['focusStocks'][0]['status'] == 'conditions_met'
    assert result['focusStocks'][1]['status'] == 'watch'


@pytest.mark.parametrize('mutation', ['bare_code', 'old_assessment', 'future_assessment', 'no_assessment_time',
                                     'not_recommended', 'failed_condition', 'hard_veto', 'wrong_grade', 'old_quote'])
def test_stale_or_unverified_opportunity_membership_cannot_claim_conditions_met(mutation):
    recommendation = {'code': '600001', 'ruleRecommendation': True,
        'opportunity': {'grade': 'F-A', 'hardVetoes': [], 'failedConditions': []}}
    channels = {'asOf': NOW.isoformat(), 'recommendations': [recommendation]}
    prices = quotes()
    if mutation == 'bare_code': channels['recommendations'] = [{'code': '600001'}]
    elif mutation == 'old_assessment': channels['asOf'] = (NOW-timedelta(seconds=31)).isoformat()
    elif mutation == 'future_assessment': channels['asOf'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'no_assessment_time': channels.pop('asOf')
    elif mutation == 'not_recommended': recommendation['ruleRecommendation'] = False
    elif mutation == 'failed_condition': recommendation['opportunity']['failedConditions'] = ['stock_flow_unavailable']
    elif mutation == 'hard_veto': recommendation['opportunity']['hardVetoes'] = ['quote_conflict']
    elif mutation == 'wrong_grade': recommendation['opportunity']['grade'] = 'B'
    else: prices['600001']['source_time'] = (NOW-timedelta(seconds=31)).isoformat()
    row = brief(prices=prices, opportunities=channels)['focusStocks'][0]
    assert row['status'] == 'watch' and row['pending'] and not row['ruleRecommendation']


def test_closed_session_prices_remain_references_not_current_buy_signals():
    now = NOW.replace(hour=15, minute=30)
    result = brief(context(now), quotes(now.replace(hour=15, minute=0)), now,
        opportunities={'recommendations': [{'code': '600001'}]})
    assert len(result['focusStocks']) == 2
    assert result['focusStocks'][0]['quoteFreshness'] == 'session_reference'
    assert result['focusStocks'][0]['status'] == 'watch'


@pytest.mark.parametrize('clock,source_clock', [('12:00', '11:30'), ('11:30', '11:30'),
                                              ('11:44', '11:43'), ('15:30', '15:00')])
def test_actual_public_adapter_lunch_and_close_reference_remain_visible_as_watch(clock, source_clock):
    from src.services.public_market_quotes import adapt_public_quote, market_phase
    hour, minute = map(int, clock.split(':'))
    source_hour, source_minute = map(int, source_clock.split(':'))
    now = NOW.replace(hour=hour, minute=minute)
    source_at = NOW.replace(hour=source_hour, minute=source_minute)
    prices = {}
    for code, raw in quotes(source_at).items():
        source = {**raw, 'previous_close': 10, 'open': 10, 'volume_shares': 100, 'amount_cny': 1000}
        issues = ['request_phase_'+market_phase(now), 'source_phase_'+market_phase(source_at)]
        if (now-source_at).total_seconds() > 30:
            issues.append('stale_quote')
        if market_phase(now) == 'post_close':
            issues.append('post_close_cannot_reconstruct_1455')
        assessment = {'code': code, 'quote': source, 'status': 'reference_only', 'phase': market_phase(source_at),
            'same_market_date': True, 'issues': issues, 'quote_usable_for_current_price_check': False,
            'execution_verified': False}
        prices[code] = adapt_public_quote({'checked_at': now.isoformat(), 'quotes': [assessment]}, assessment)
    channels = {'asOf': now.isoformat(), 'recommendations': [{'code': '600001', 'ruleRecommendation': True,
        'opportunity': {'grade': 'F-A'}}]}
    result = brief(context(now), prices, now, opportunities=channels)
    assert len(result['focusStocks']) == 2
    assert all(row['quoteFreshness'] == 'session_reference' and row['status'] == 'watch' for row in result['focusStocks'])
    prices['600001']['public_quote_metadata']['issues'].append('cross_source_price_conflict')
    assert [row['code'] for row in brief(context(now), prices, now)['focusStocks']] == ['600002']
    prices['600002']['public_quote_metadata']['checked_at'] = (now+timedelta(seconds=1)).isoformat()
    assert brief(context(now), prices, now)['focusStocks'] == []


def test_unavailable_event_packet_and_bad_top_stock_shapes_do_not_create_evidence():
    packet = context()
    packet['events']['status'] = 'unavailable'
    assert brief(packet)['focusStocks'] == []
    packet['events']['status'] = 'observed'
    packet['events']['items'][0]['topStocks'].extend([None, '600999'])
    assert len(brief(packet)['focusStocks']) == 2
    assert len(event_candidate_codes(packet, NOW)) == 2
    assert prioritize_market_queue([{'code': '600001'}], packet, NOW, -1) == []


def test_event_candidates_seed_queue_without_displacing_all_controls(monkeypatch):
    monkeypatch.setattr('src.services.yao_scout.market_context._previous_day', lambda now: '2026-11-12')
    rows = [{'code': code} for code in ['600003', '600004', '600002', '600001']]
    queued = prioritize_market_queue(rows, context(), NOW, 3)
    assert [row['code'] for row in queued] == ['600001', '600003', '600004']
    assert event_candidate_codes(context(), NOW) == ['600001', '600002']


def test_brief_is_frozen_through_display_projection_without_raw_quotes():
    original = brief()
    projected = _display_response({'adaptive': {'marketBrief': deepcopy(original)}})['adaptive']['marketBrief']
    assert projected == original


def test_shared_context_client_is_bounded_and_uses_authenticated_request(monkeypatch):
    calls = []
    monkeypatch.setattr(SoftwareMarketClient, '_request', lambda self, path, payload: calls.append((path, payload, self.read_timeout)) or {})
    SoftwareMarketClient('http://127.0.0.1:9000', 'secret').theme_context(now=NOW, timeout=6)
    assert calls == [('theme-context', {'date': '2026-11-13', 'count': 100}, 6)]


def test_real_scan_uses_market_events_without_concept_board(monkeypatch, tmp_path):
    from tests.test_opportunity_integration import cold_start
    service, tick, saved = cold_start(monkeypatch, tmp_path, NOW)
    class Market:
        available = True
        def bars(self, *args, **kwargs): return {'bars': []}
        def theme_context(self, **kwargs): return context(tick[0])
        def enrich_snapshot_quotes(self, snapshot, **kwargs): return snapshot
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Market())
    service.quote_fetcher = lambda code: {**quotes(tick[0]).get(code, {}), 'open_price': 10,
        'high': 10.5, 'low': 9.9}
    result = service._run('live')
    assert result['themeBoard']['status'] == 'unavailable'
    assert result['opportunityChannels']['recommendations'] == []
    assert [row['code'] for row in result['marketBrief']['focusStocks']] == ['600001', '600002']
    assert result['marketBrief']['focusStocks'][0]['status'] == 'watch'
