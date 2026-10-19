from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.services.yao_scout.daily_opportunities import TZ
from src.services.yao_scout.premarket import build_premarket_candidates, opening_phase, previous_session
from src.services.yao_scout.recommendation_evidence import daily_evidence


def at(clock='09:20:00'):
    return datetime.fromisoformat('2026-09-15T' + clock).replace(tzinfo=TZ)


def history():
    data = pd.DataFrame({'date': pd.bdate_range(end='2026-09-14', periods=60),
                         'close': [9.2+i*.6/59 for i in range(60)]})
    data['high'], data['low'] = data.close+.7, data.close-.2
    data.attrs['daily_source'] = 'fixture_completed_daily'
    data.attrs['daily_adjustment'] = 'unadjusted'
    return data


def quote(now=None, **changes):
    now = now or at()
    result = {'code': '600001', 'price': 9.9, 'pre_close': 9.8, 'open_price': 9.85,
              'high': 9.95, 'low': 9.8, 'change_pct': (9.9/9.8-1)*100,
              'volume': 100000, 'amount': 985000, 'source': 'ordinary_quote',
              'provider_timestamp': now.isoformat(), 'limit_up': 10.78}
    result.update(changes)
    return result


def run(now=None, q=None, daily=None, contexts=None):
    now = now or at()
    daily = daily if daily is not None else daily_evidence(history(), now)
    return build_premarket_candidates([{'code': '600001', 'name': '测试股份'}],
        {'600001': daily}, {'600001': q} if q is not None else {}, now, contexts=contexts)


@pytest.mark.parametrize('clock,expected', [
    ('09:14:59', 'overnight_plan'), ('09:15:00', 'auction_cancellable'),
    ('09:19:59', 'auction_cancellable'), ('09:20:00', 'auction_non_cancellable'),
    ('09:24:59', 'auction_non_cancellable'), ('09:25:00', 'auction_priced'),
    ('09:29:59', 'auction_priced'), ('09:30:00', 'opening_handoff'),
    ('09:34:59', 'opening_handoff'), ('09:35:00', None), ('10:30:00', None),
])
def test_beijing_phase_boundaries(clock, expected):
    phase = opening_phase(at(clock))
    assert (phase or {}).get('code') == expected
    assert opening_phase(at(clock).astimezone(timezone.utc)) == phase


def test_closed_day_and_previous_friday():
    assert opening_phase(datetime(2026, 9, 19, 9, 20, tzinfo=TZ)) is None
    assert previous_session(datetime(2026, 9, 21, 9, 20, tzinfo=TZ)).isoformat() == '2026-09-18'


def test_no_current_quote_or_minutes_still_produces_inspectable_overnight_plan():
    result = run()
    row = result['premarketCandidates'][0]
    assert row['planStatus'] == 'overnight_plan'
    assert row['quoteStatus'] == 'missing' and row['quote'] == {}
    assert row['referencePriceKind'] == 'previous_close' and row['price'] == pytest.approx(9.8)
    assert row['support'] > 0 and row['atr14'] > 0 and row['triggerPrice'] > 0
    assert not row['executable'] and not row['entryEligible'] and not row['auctionConfirmed']
    assert any('量额不完整' in gap for gap in row['dataGaps'])
    assessment = result['premarketAssessment']
    assert assessment['dailyUsableCount'] == assessment['candidateCount'] == 1
    assert assessment['baselineSession'] == '2026-09-14'
    assert assessment['expiresAt'] == at('09:35:00').isoformat()
    assert not assessment['minuteDataRequired']


@pytest.mark.parametrize('seconds,status', [(30, 'fresh'), (31, 'stale'), (-1, 'future')])
def test_quote_timestamp_boundary_never_backfills_or_uses_future(seconds, status):
    result = run(q=quote(provider_timestamp=(at()-timedelta(seconds=seconds)).isoformat()))
    row = result['premarketCandidates'][0]
    assert row['quoteStatus'] == status
    assert row['referencePriceKind'] == ('current_quote' if status == 'fresh' else 'previous_close')
    assert not row['auctionConfirmed']


@pytest.mark.parametrize('changes', [
    {'code': '600002'}, {'provider_timestamp': None}, {'source': None},
    {'high': 9.5, 'low': 9.4}, {'price': 0}, {'amount': -10},
])
def test_invalid_quote_keeps_only_last_session_plan(changes):
    row = run(q=quote(**changes))['premarketCandidates'][0]
    assert row['quoteStatus'] == 'invalid' and row['quote'] == {}
    assert row['price'] == pytest.approx(9.8) and not row['executable']


def test_ordinary_quotes_and_unverified_fields_never_confirm_auction():
    row = run(q=quote(open_price=0, high=0, low=0, amount=0, volume=0,
                      auction_price=10, matched_volume=99999999, auction_confirmed=True))['premarketCandidates'][0]
    assert row['quoteStatus'] == 'fresh'
    assert row['auctionConfirmed'] is False
    assert row['auctionEvidence']['status'] == 'unavailable'
    assert row['openingGapPct'] is None and row['openingPrice'] is None


@pytest.mark.parametrize('change', [
    {'asOf': '2026-09-11'}, {'asOf': '2026-09-15'}, {'asOf': '2026-09-16'},
    {'source': None}, {'observedAt': '2026-09-15T09:20:01+08:00'},
])
def test_stale_future_or_untraceable_daily_cannot_form_a_plan(change):
    daily = {**daily_evidence(history(), at()), **change}
    result = run(daily=daily)
    assert result['premarketCandidates'] == []
    assert result['premarketAssessment']['dailyUsableCount'] == 0


def test_daily_coverage_is_not_the_number_passing_trend():
    daily = daily_evidence(history(), at())
    daily['metrics']['ma5'] = daily['metrics']['ma10'] - .1
    result = run(daily=daily)
    assert result['premarketCandidates'] == []
    assert result['premarketAssessment']['dailyUsableCount'] == 1


def test_short_history_and_quote_basis_mismatch_are_excluded():
    daily = daily_evidence(history(), at())
    daily['metrics']['history_sessions'] = 19
    assert not run(daily=daily)['premarketCandidates']
    result = run(q=quote(pre_close=9.5, change_pct=(9.9/9.5-1)*100))
    assert not result['premarketCandidates']
    assert result['premarketAssessment']['excludedCounts']['daily_quote_price_basis_mismatch'] == 1


@pytest.mark.parametrize('clock', ['09:25:00', '09:30:00', '09:34:59'])
def test_opening_observation_requires_current_real_price_and_turnover(clock):
    now = at(clock)
    row = run(now, quote(now))['premarketCandidates'][0]
    assert row['openingTradeObserved']
    assert row['openingGapPct'] == pytest.approx((9.85/9.8-1)*100)
    assert not row['executable'] and not row['auctionConfirmed']
    absent = run(now, quote(now, volume=0, amount=0))['premarketCandidates'][0]
    assert not absent['openingTradeObserved']
    assert absent['openingGapPct'] is None
    if clock >= '09:30:00':
        assert absent['planStatus'] == 'awaiting_open_trade'


def test_auction_print_is_not_opening_continuous_trade():
    now = at('09:30:00')
    row = run(now, quote(at('09:29:59')))['premarketCandidates'][0]
    assert row['quoteStatus'] == 'fresh'
    assert not row['openingTradeObserved']


@pytest.mark.parametrize('changes', [
    {'locked_limit_up': True}, {'suspended': True},
    {'price': 10.78, 'high': 10.78, 'low': 10.78, 'open_price': 10.78,
     'amount': 0, 'volume': 0, 'change_pct': 10},
    {'price': 10.78, 'high': 10.78, 'low': 10.78, 'open_price': 10.78,
     'amount': 1078000, 'volume': 100000, 'change_pct': 10},
])
def test_suspended_locked_or_one_price_limit_up_cannot_take_a_plan_slot(changes):
    now = at('09:30:00')
    assert not run(now, quote(now, **changes))['premarketCandidates']


def test_known_negative_disclosure_blocks_but_future_disclosure_is_not_used():
    event = {'type': '预减', 'metric': '净利润', 'source': 'disclosure',
             'observedAt': at('09:00:00').isoformat(), 'firstSeenAt': at('09:00:00').isoformat(),
             'availableAt': at('09:00:00').isoformat()}
    context = {'forecasts': [event], 'coverage': {'forecast': True, 'unlock': True}}
    assert not run(contexts={'600001': context})['premarketCandidates']
    event['availableAt'] = at('09:25:00').isoformat()
    row = run(contexts={'600001': context})['premarketCandidates'][0]
    assert any('可知时间' in gap for gap in row['dataGaps'])


def test_deduplication_five_plan_cap_and_cutoff():
    items = [{'code': f'60000{i}', 'name': f'测试{i}'} for i in range(8)]
    daily = {item['code']: daily_evidence(history(), at()) for item in items}
    result = build_premarket_candidates(items+items, daily, {}, at(), top_n=99)
    assert len(result['premarketCandidates']) == 5
    assert result['premarketAssessment']['researchedCount'] == 8
    assert build_premarket_candidates(items, daily, {}, at('09:35:00'))['premarketCandidates'] == []


def test_optional_completed_volume_ratios_are_prior_only_and_do_not_invent_units():
    data = history()
    data['volume'], data['amount'] = 100, 1000
    data.loc[data.index[-1], ['volume', 'amount']] = [200, 3000]
    evidence = daily_evidence(data, at())
    assert evidence['metrics']['relative_volume_last_vs_prior20'] == 2
    assert evidence['metrics']['relative_amount_last_vs_prior20'] == 3
    assert evidence['liquidityEvidence']['volumeUnit'] == 'provider_native'
    extra = pd.DataFrame([{'date': '2026-09-15', 'close': 99, 'high': 100, 'low': 90, 'volume': 100000, 'amount': 1e9}])
    assert daily_evidence(pd.concat([data, extra]), at())['metrics'] == evidence['metrics']
    data.loc[data.index[-2], 'volume'] = 0
    assert daily_evidence(data, at())['metrics']['relative_volume_last_vs_prior20'] is None
    assert daily_evidence(data.tail(20), at())['metrics']['relative_amount_last_vs_prior20'] is None


def test_no_chase_ceiling_preserves_pressure_and_atr_geometry():
    daily = daily_evidence(history(), at())
    daily['metrics'].update(high20=12, high60=12, atr14=.3)
    row = run(q=quote(), daily=daily)['premarketCandidates'][0]
    assert row['noChaseAbove'] == pytest.approx(min((12+row['support'])/2, 12-.3))
    assert 12-row['noChaseAbove'] >= row['noChaseAbove']-row['support']-1e-8
    assert 12-row['noChaseAbove'] >= .3
    expensive = quote(price=11, high=11, change_pct=(11/9.8-1)*100, limit_up=None)
    row = run(q=expensive, daily=daily)['premarketCandidates'][0]
    assert row['planStatus'] == 'avoid_chasing'


def test_quote_coverage_includes_rejected_trends_and_excess_candidates():
    items = [{'code': f'60000{i}', 'name': f'测试{i}'} for i in range(8)]
    daily = {item['code']: daily_evidence(history(), at()) for item in items}
    daily['600007']['metrics']['ma5'] = 1
    quotes = {item['code']: quote(code=item['code']) for item in items}
    result = build_premarket_candidates(items, daily, quotes, at())
    assert len(result['premarketCandidates']) == 5
    assert result['premarketAssessment']['quoteCoverage']['freshCount'] == 8


def test_monday_daily_validation_uses_completed_friday():
    data = history()
    data['date'] = pd.bdate_range(end='2026-09-18', periods=60)
    now = datetime(2026, 9, 21, 9, 20, tzinfo=TZ)
    daily = daily_evidence(data, now)
    assert daily['asOf'] == '2026-09-18'
    assert len(run(now=now, daily=daily)['premarketCandidates']) == 1


def test_existing_gateway_cache_columns_supply_liquidity_with_explicit_units():
    data = history()
    data['volume_shares'], data['amount_cny'] = 10000, 100000
    data.loc[data.index[-1], ['volume_shares', 'amount_cny']] = [20000, 300000]
    evidence = daily_evidence(data, at())
    assert evidence['metrics']['last_volume'] == 20000
    assert evidence['metrics']['last_amount'] == 300000
    assert evidence['metrics']['relative_volume_last_vs_prior20'] == 2
    assert evidence['metrics']['relative_amount_last_vs_prior20'] == 3
    assert evidence['liquidityEvidence']['volumeUnit'] == 'shares'
    assert evidence['liquidityEvidence']['amountUnit'] == 'CNY'
    row = run(daily=evidence)['premarketCandidates'][0]
    shown = {item['key']: item for item in row['indicatorEvidence']}
    assert shown['last_volume']['unit'] == 'shares'
    assert shown['last_amount']['unit'] == 'CNY'


def test_gateway_units_do_not_mix_with_conflicting_native_columns():
    data = history()
    data['volume_shares'], data['amount_cny'] = 10000, 100000
    data['volume'], data['amount'] = 100, 1000
    data.loc[data.index[-1], ['volume_shares', 'amount_cny']] = [20000, 300000]
    evidence = daily_evidence(data, at())
    assert evidence['metrics']['last_volume'] == 20000
    assert evidence['metrics']['relative_amount_last_vs_prior20'] == 3
    data.loc[data.index[-1], 'volume_shares'] = None
    assert daily_evidence(data, at())['metrics']['last_volume'] is None
