from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.services.yao_scout.daily_opportunities import TZ
from src.services.yao_scout.recommendation_evidence import research_queue
from src.services.yao_scout.research_factors import event_factor


NOW = datetime(2026, 10, 28, 10, 30, tzinfo=TZ)


def frame(at, *, prices=None, amounts=None, shapes=None, source='sina'):
    prices, amounts, shapes = prices or {}, amounts or {}, shapes or {}
    rows = []
    for number in range(1, 9):
        code = f'60000{number}'
        quote = {'code': code, 'source': source, 'source_time': at.isoformat(),
                 'price': prices.get(code, 10.), 'open': 10.,
                 'amount_cny': amounts.get(code, (9 - number) * 1e8), **shapes.get(code, {})}
        rows.append({'code': code, 'source': 'stale_directory',
                     'source_time': (at - timedelta(days=1)).isoformat(),
                     'amount': 1e12, 'volume_ratio': 99., 'market_quote': quote})
    result = pd.DataFrame(rows)
    result.attrs['captured_at'] = at.isoformat()
    return result


def forecast():
    return {'coverage': {'forecast': True}, 'forecasts': [{
        'source': 'AKShare/stock_yjyg_em', 'eventId': 'published-profit',
        'observedAt': NOW.isoformat(), 'availableAt': '2026-10-28T00:00:00+08:00',
        'firstSeenAt': '2026-10-27T16:00:00+08:00', 'publishedDate': '2026-10-27',
        'type': '预增', 'metric': '归属净利润', 'changePct': 25}]}


@pytest.fixture(autouse=True)
def local_calendar(monkeypatch):
    monkeypatch.setattr('src.services.yao_scout.research_factors._sessions',
                        lambda start, end: [x.date() for x in pd.bdate_range(start, end)])


def test_new_strength_can_displace_static_activity_without_larger_research_budget():
    previous = frame(NOW - timedelta(minutes=10))
    current = frame(NOW, prices={'600008': 10.4}, shapes={'600008': {'open': 10.5}})
    baseline = research_queue(current, 4, now=NOW)
    discovered = research_queue(current, 4, now=NOW, previous_frame=previous)
    assert len(discovered) == len(baseline) == 4
    assert '600008' not in {r['code'] for r in baseline}
    target = next(r for r in discovered if r['code'] == '600008')
    assert target['discoveryEvidence']['allocatedBy'] == 'state_change'
    assert target['discoveryEvidence']['metrics']['price_change_pct'] == pytest.approx(4)
    assert target['volume_ratio'] is None  # New quote did not redate old ratios.
    assert not any(k in target for k in ('entryEligible', 'probability', 'selected'))


@pytest.mark.parametrize('change', ['future', 'missing_capture', 'stale_previous', 'other_source', 'previous_day', 'lunch'])
def test_invalid_comparisons_never_create_dynamic_strength(change):
    prior_time = NOW - timedelta(minutes=10)
    now = NOW
    if change == 'previous_day':
        prior_time -= timedelta(days=1)
    elif change == 'lunch':
        now = NOW.replace(hour=13, minute=5)
        prior_time = NOW.replace(hour=11, minute=25)
    previous = frame(prior_time, source='tencent' if change == 'other_source' else 'sina')
    current = frame(now, prices={'600008': 10.4})
    if change == 'missing_capture':
        previous.attrs.clear()
    elif change == 'future':
        previous.attrs['captured_at'] = (now + timedelta(seconds=1)).isoformat()
    elif change == 'stale_previous':
        previous.attrs['captured_at'] = (prior_time + timedelta(seconds=31)).isoformat()
    result = research_queue(current, 8, now=now, previous_frame=previous)
    assert not any('state_change' in row['discoveryEvidence']['channels'] for row in result)


def test_rank_improvement_uses_same_comparable_security_set_and_keeps_counter_reset_unknown():
    previous = frame(NOW - timedelta(minutes=10))
    current = frame(NOW, prices={'600008': 10.2, '600007': 10.1},
                    amounts={'600008': 9e8, '600007': 1.})
    result = {r['code']: r for r in research_queue(current, 8, now=NOW, previous_frame=previous)}
    good = result['600008']['discoveryEvidence']
    assert good['metrics']['amount_rank_improvement'] == 6
    assert good['evidence']['rankComparisonSize'] == 7
    reset = result['600007']['discoveryEvidence']
    assert 'cumulative_amount_decreased_or_reset' in reset['gaps']
    assert 'interval_amount_relative_prior_pace' not in reset['metrics']
    assert 'amount_rank_improvement' not in reset['metrics']


def test_low_recovery_has_independent_capacity_but_not_a_fabricated_minute_path():
    current = frame(NOW, shapes={'600008': {'high': 10., 'low': 9.5, 'price': 10.}})
    result = research_queue(current, 4, now=NOW)
    target = next(r for r in result if r['code'] == '600008')
    packet = target['discoveryEvidence']
    assert packet['allocatedBy'] == 'low_recovery'
    assert packet['metrics']['low_recovery_fraction'] == 1
    assert '未证明分钟路径' in packet['evidence']['intradayShape']['meaning']
    current.loc[current.code == '600008', 'market_quote'].iloc[0]['source_time'] = (NOW - timedelta(seconds=31)).isoformat()
    assert '600008' not in {r['code'] for r in research_queue(current, 4, now=NOW)}


def test_real_forecast_can_enter_discovery_without_extra_quotes_or_activity_rank():
    result = research_queue(frame(NOW), 4, now=NOW, catalysts={'600008': forecast()})
    target = next(r for r in result if r['code'] == '600008')
    assert target['discoveryEvidence']['allocatedBy'] == 'disclosure'
    evidence = target['discoveryEvidence']['evidence']['catalyst']
    assert evidence['eventIds'] == ['published-profit']
    assert evidence['coverage']['disclosures'] == 'unknown'


def test_old_announcement_redownload_does_not_win_fresh_discovery_slot():
    context = forecast()
    event = context['forecasts'][0]
    event.update(publishedDate='2026-10-01', firstSeenAt='2026-10-02T00:00:00+08:00',
                 availableAt=NOW.isoformat())
    assert '600008' not in {r['code'] for r in research_queue(frame(NOW), 4, now=NOW,
                                                            catalysts={'600008': context})}


def disclosure(**updates):
    event = {'code': '600008', 'eventId': 'contract-1', 'category': 'order', 'impact': 'positive',
             'source': 'company_exchange_disclosure', 'sourceUrl': 'https://www.cninfo.com.cn/example',
             'title': '签订产品销售合同公告', 'body': '公司已签订产品销售合同，金额及履约风险详见公告。',
             'evidenceText': '公司已签订产品销售合同', 'publishedAt': '2026-10-28T08:00:00+08:00',
             'firstSeenAt': '2026-10-28T08:10:00+08:00', 'availableAt': '2026-10-28T08:10:00+08:00',
             'observedAt': NOW.isoformat(), **updates}
    return {'code': '600008', 'coverage': {'disclosures': True}, 'disclosures': [event]}


def test_linked_non_profit_disclosure_is_research_catalyst_with_auditable_original_excerpt():
    packet = event_factor(disclosure(), NOW)
    assert packet['score'] > 50 and packet['features']['positive_event_count'] == 1
    assert packet['coverage']['forecast'] == 'unknown'
    assert packet['disclosureEvidence'][0]['evidenceText'] == '公司已签订产品销售合同'
    assert packet['socialSentimentAvailable'] is False


@pytest.mark.parametrize('patch', [
    {'code': '600009'}, {'source': ''}, {'sourceUrl': ''}, {'eventId': ''},
    {'evidenceText': '政策将使利润翻倍'}, {'firstSeenAt': (NOW + timedelta(seconds=1)).isoformat()},
    {'availableAt': '2026-10-28T07:00:00+08:00'}, {'publishedAt': (NOW + timedelta(seconds=1)).isoformat()},
    {'publishedAt': '2026-10-28T08:00:00'},
])
def test_unlinked_missing_or_not_yet_knowable_disclosure_cannot_raise_catalyst(patch):
    packet = event_factor(disclosure(**patch), NOW)
    assert packet['score'] is None
    assert packet['features']['rejected_disclosure_count'] == 1


def test_disclosed_cancellation_is_not_positive_even_when_adapter_mislabels_it():
    packet = event_factor(disclosure(title='关于终止合同的公告', body='公司已签订产品销售合同现已终止。'), NOW)
    assert packet['score'] == 50
    assert packet['disclosureEvidence'][0]['disclosedRiskTerms'] == ['终止']
    assert not packet['disclosureEvidence'][0]['positiveCatalyst']


def test_absent_broad_disclosure_coverage_is_unknown_not_an_empty_confirmed_feed():
    context = disclosure()
    context['coverage'] = {}
    packet = event_factor(context, NOW)
    assert packet['score'] is None and packet['coverage']['disclosures'] == 'unknown'


def test_default_research_capacity_and_duplicate_safety_are_unchanged():
    current = frame(NOW)
    current = pd.concat([current] * 50, ignore_index=True)
    result = research_queue(current, now=NOW, catalysts={'600008': forecast()})
    assert len(result) == len({r['code'] for r in result}) == 8
    assert all(row['discoveryEvidence']['researchBudget'] == 8 for row in result)


def test_provider_utc_clocks_compare_in_exchange_session_without_redating():
    previous = frame((NOW - timedelta(minutes=10)).astimezone(timezone.utc))
    current = frame(NOW.astimezone(timezone.utc), prices={'600008': 10.4},
                    shapes={'600008': {'open': 10.5}})
    target = next(row for row in research_queue(current, 4, now=NOW, previous_frame=previous)
                  if row['code'] == '600008')
    packet = target['discoveryEvidence']
    assert packet['allocatedBy'] == 'state_change'
    assert packet['evidence']['priceChange']['before']['sourceTime'].endswith('+00:00')
