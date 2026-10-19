"""Fresh-install daily conditions cannot inherit minute data or model gates."""
from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.services.yao_scout.daily_opportunities import TZ
from src.services.yao_scout.daily_primary import build_daily_candidates, TASK_ID
from src.services.yao_scout.recommendation_evidence import daily_evidence


NOW = datetime(2026, 9, 15, 10, 30, 20, tzinfo=TZ)
CODE = '600001'


def daily():
    frame = pd.DataFrame({'date': pd.bdate_range(end='2026-09-14', periods=60),
                          'close': [8.8 + index*1.2/59 for index in range(60)]})
    frame['high'], frame['low'] = frame.close+.1, frame.close-.1
    frame.loc[50, 'high'] = 11.2
    frame.attrs.update(daily_source='fixture_daily_provider', daily_adjustment='unadjusted')
    return daily_evidence(frame, NOW)


def quote(**changes):
    return {'code': CODE, 'price': 10.05, 'pre_close': 10., 'open_price': 10.,
            'high': 10.1, 'low': 9.95, 'change_pct': .5, 'volume': 1_000_000,
            'amount': 10_000_000, 'limit_up': 11., 'limit_down': 9.,
            'source': 'fixture_quote', 'provider_timestamp': NOW.isoformat(), **changes}


def known(**changes):
    return {'source': 'fixture_disclosure', 'observedAt': NOW.isoformat(),
            'availableAt': (NOW-timedelta(hours=1)).isoformat(),
            'firstSeenAt': (NOW-timedelta(hours=1)).isoformat(), **changes}


def context():
    return {'coverage': {'financials': True, 'forecast': True, 'unlock': True},
            'financials': known(profit=1000, eps=1, cashPerShare=.5),
            'forecasts': [], 'unlocks': [], 'gaps': [], 'decisionAt': NOW.isoformat()}


def run(*, packet=None, q=None, ctx=None, market=None, item=None, now=NOW, top_n=5):
    return build_daily_candidates([item or {'code': CODE, 'name': '测试股份'}],
        {CODE: packet if packet is not None else daily()}, {CODE: q if q is not None else quote()},
        now, contexts={CODE: ctx if ctx is not None else context()}, market_evidence=market, top_n=top_n)


def test_real_downloaded_daily_history_works_with_no_minute_archive_or_ml_model():
    result = run(item={'code': CODE, 'name': '测试股份', 'dataLineage': {'status': 'tainted'},
                      'morningSignal': {'selectionEligible': False, 'learningStatus': 'rules_unvalidated'},
                      'v11Inputs': {'failed_breakout_risk': 100}})
    row = result['dailyCandidates'][0]
    assert row['stateLabel'] == '日线条件推荐'
    assert row['dailySignal']['taskId'] == TASK_ID and row['horizonId'] == 'next_session'
    assert row['probability'] is None and row['probabilities'] == {}
    assert row['validationStatus'] == 'unvalidated_rules'
    assert row['minuteModelTrainingEligible'] is False and row['trainingEligible'] is False
    assert row['entryEligible'] is True and row['executable'] is False
    assert 'morningSignal' not in row and 'dataLineage' not in row and 'scoreVersion' not in row
    assert result['dailyAssessment']['marketBreadth']['passed'] is None
    assert any('环境保持未知' in gap for gap in row['dataGaps'])
    assert len(result['dailyCandidates']) == 1  # Never fill the requested five slots.


@pytest.mark.parametrize('change', ['short', 'stale', 'adjusted', 'source_unknown', 'prior_mismatch'])
def test_daily_history_itself_must_be_complete_aligned_and_traceable(change):
    packet = daily()
    if change == 'short':
        packet['metrics']['history_sessions'] = 19
    elif change == 'stale':
        packet['asOf'] = '2026-09-11'
    elif change == 'adjusted':
        packet['previousSession']['priceBasis'] = 'qfq'
    elif change == 'source_unknown':
        packet['source'] = packet['previousSession']['source'] = 'unknown'
    else:
        packet['previousSession']['close'] = 9.
    assert run(packet=packet)['dailyCandidates'] == []


@pytest.mark.parametrize('changes', [
    {'provider_timestamp': (NOW-timedelta(seconds=31)).isoformat()},
    {'provider_timestamp': (NOW+timedelta(seconds=1)).isoformat()},
    {'limit_down': None}, {'limit_up': 11.2}, {'suspended': True},
    {'locked_limit_up': True}, {'amount': 0}, {'amount': 1000}, {'code': '600002'},
    {'pre_close': 9.9, 'change_pct': (10.05/9.9-1)*100, 'limit_up': 10.89, 'limit_down': 8.91},
])
def test_current_quote_and_provider_limits_remain_mandatory(changes):
    assert run(q=quote(**changes))['dailyCandidates'] == []


def test_auxiliary_price_limits_keep_their_own_source_time_and_price_basis():
    evidence = {'status': 'observed', 'method': 'explicit_provider_fields',
                'source': 'tencent', 'source_time': NOW.isoformat(), 'limit_up': 11.,
                'limit_down': 9., 'previous_close': 10.}
    assert run(q=quote(price_limit_evidence=evidence))['dailyCandidates']
    evidence['source_time'] = (NOW-timedelta(seconds=31)).isoformat()
    assert not run(q=quote(price_limit_evidence=evidence))['dailyCandidates']
    evidence['source_time'], evidence['previous_close'] = NOW.isoformat(), 9.9
    assert not run(q=quote(price_limit_evidence=evidence))['dailyCandidates']


@pytest.mark.parametrize('change', ['coverage', 'future_disclosure', 'missing_financial', 'loss', 'forecast', 'unlock'])
def test_unknown_event_coverage_or_known_adverse_risks_cannot_be_relabelled_as_ready(change):
    ctx = context()
    if change == 'coverage':
        ctx['coverage']['forecast'] = False
    elif change == 'future_disclosure':
        ctx['financials']['availableAt'] = (NOW+timedelta(days=1)).isoformat()
    elif change == 'missing_financial':
        ctx['financials'] = None
    elif change == 'loss':
        ctx['financials']['profit'] = -1
    elif change == 'forecast':
        ctx['forecasts'] = [known(type='预减', metric='净利润')]
    else:
        ctx['unlocks'] = [known(eventDate='2026-09-20', floatRatioPct=8)]
    result = run(ctx=ctx)
    assert not result['dailyCandidates']
    assert all(not rows for rows in result['dailyProfileCandidates'].values())


def test_three_profiles_keep_their_own_declared_unlock_and_cashflow_limits():
    ctx = context()
    ctx['unlocks'] = [known(eventDate='2026-09-20', floatRatioPct=4)]
    profiles = run(ctx=ctx)['dailyProfileCandidates']
    assert profiles['conservative'] == [] and len(profiles['regular']) == len(profiles['aggressive']) == 1
    ctx['unlocks'][0]['floatRatioPct'] = 6
    profiles = run(ctx=ctx)['dailyProfileCandidates']
    assert profiles['regular'] == [] and len(profiles['aggressive']) == 1


def test_known_weak_market_vetoes_but_unavailable_breadth_is_explicitly_unknown():
    market = {'source': 'whole_market_snapshot', 'asOf': NOW.isoformat(),
              'features': {'source_coverage': 1., 'universe_count': 3000, 'advancing_pct': 40}}
    result = run(market=market)
    assert result['dailyCandidates'] == []
    assert result['dailyAssessment']['excludedCounts']['verified_weak_market'] == 1
    market['features']['advancing_pct'] = 65
    result = run(market=market)
    assert len(result['dailyCandidates']) == 1
    assert result['dailyAssessment']['marketBreadth']['passed'] is True


def test_unknown_upside_does_not_receive_an_invented_target_or_bypass_rr():
    packet = daily()
    packet['metrics'].update(high20=10., high60=10.)
    result = run(packet=packet)
    assert result['dailyCandidates'] == []
    assert result['dailyAssessment']['excludedCounts']['daily_support_or_upside_space_unknown'] == 1
    packet = daily()
    packet['metrics'].update(high20=10.1, high60=10.1)
    result = run(packet=packet)
    assert result['dailyCandidates'] == []
    assert result['dailyAssessment']['profileExcludedCounts']['regular']['daily_upside_reward_risk_insufficient'] == 1


def test_daily_lane_does_not_mutate_inputs_or_run_outside_trading_hours():
    packet, q, ctx = daily(), quote(), context()
    originals = deepcopy((packet, q, ctx))
    run(packet=packet, q=q, ctx=ctx)
    assert (packet, q, ctx) == originals
    assert run(now=NOW.replace(hour=12))['dailyAssessment']['status'] == 'outside_continuous_trading_window'
