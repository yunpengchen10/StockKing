from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.services.yao_scout.opportunity_channels import VERSION, build_opportunity_channels

NOW = datetime(2026, 10, 28, 9, 40, 15, tzinfo=ZoneInfo('Asia/Shanghai'))
MINUTE = NOW.replace(second=0)-timedelta(minutes=1)


def candidate(code='600001'):
    return {'code': code, 'name': '测试股', 'referencePrice': 10.,
        'quote': {'code': code, 'price': 10., 'pre_close': 10.2, 'open_price': 9.7,
            'high': 10.1, 'low': 9.6, 'change_pct': (10/10.2-1)*100,
            'source': 'fixture_quote', 'source_time': NOW.isoformat(),
            'active_buy_share_pct': 60, 'active_flow_source': 'fixture_classification',
            'active_flow_as_of': NOW.isoformat(),
            'order_book': {'source_time': NOW.isoformat(),
                'asks': [{'price': 10.01, 'volume_shares': 10000}],
                'bids': [{'price': 9.99, 'volume_shares': 10000}]}},
        'dataLineage': {'status': 'unknown'}, 'evidenceEligible': False,
        'precisionDecision': {'profiles': {'regular': {'entryEligible': False}}},
        'prediction': {'status': 'rules_unvalidated'},
        'dataEligibility': {'status': 'insufficient'}}


def minute(**changes):
    result = {'source': 'fixture_completed_bars', 'asOf': MINUTE.isoformat(),
        'sourceTimePrecision': 'bar_end', 'currentPrice': 10., 'historyDays': 0,
        'metrics': {'speed_1m_pct': .4, 'speed_3m_pct': .3, 'speed_5m_pct': -.2,
                    'local_high_5m': 10.02, 'local_low_5m': 9.65, 'vwap': 10.05,
                    'amount_3m': 1000000},
        'v11Inputs': {'r1_pct': .4, 'r3_pct': .3, 'r5_pct': -.2, 'r15_pct': -1.,
                      'a1_pct_per_min': .44, 'a3_pct_per_min': .16666667,
                      'amount_3m': 1000000, 'amount_15m': 3000000}}
    result.update(changes)
    return result


def build(row=None, packet=None, **options):
    row = candidate() if row is None else row
    return build_opportunity_channels([row], NOW,
        minute_packets={row['code']: minute() if packet is None else packet}, **options)


def test_real_early_reversal_survives_missing_history_model_sector_and_vwap():
    result = build()
    row = result['recommendations'][0]
    assert row['opportunity']['grade'] == 'E-A'
    assert row['opportunity']['metrics']['r5_pct'] < 0
    assert row['opportunity']['triggerAt'] is None
    assert row['opportunity']['sourceEvidence']['minuteAsOf'] == MINUTE.isoformat()
    assert row['opportunity']['firstDetectionAt'] == NOW.isoformat()
    assert row['opportunity']['spaceStatus'] == 'unknown'
    assert not row['formalEligible'] and not row['trainingEligible'] and not row['executable']
    assert row['ruleRecommendation'] is True
    assert any('20日' in x for x in row['opportunity']['missing'])
    assert any('尚未站回VWAP' in x for x in row['opportunity']['counterEvidence'])


def test_individual_can_lead_declining_sector_with_no_fund_estimate():
    row = candidate()
    row['sectorEvidence'] = {'source': 'fixture_sector', 'asOf': MINUTE.isoformat(),
        'metrics': {'sector_return_5m_pct': -.5, 'sector_relative_5m_pct': .3}}
    row['quote'].pop('active_buy_share_pct')
    packet = minute()
    packet['v11Inputs'].pop('amount_15m')
    selected = build(row, packet)['recommendations'][0]['opportunity']
    assert selected['grade'] == 'E-A'
    assert any('领先行业' in x for x in selected['support'])
    assert '行业同窗尚未转强' in selected['counterEvidence']


def test_new_high_does_not_need_invented_historical_resistance_or_reward_risk():
    history = {'600001': {'metrics': {'last_close': 10.2, 'high20': 9.8, 'high60': 9.9}}}
    row = build(daily_packets=history)['recommendations'][0]
    assert row['opportunity']['remainingSpacePct'] is None
    assert row['opportunity']['spaceStatus'] == 'unknown'
    assert row['opportunity']['probability'] is None


def test_flat_big_day_snapshot_is_not_a_state_transition():
    row = candidate()
    row['quote'].update(pre_close=9.2, change_pct=(10/9.2-1)*100)
    row.update(evidenceEligible=True, finalScore=99, earlyScore=99)
    result = build(row, {})
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['grade'] == 'B'


def test_initial_push_now_fading_is_observation_not_recommendation():
    packet = minute()
    packet['v11Inputs'].update(r1_pct=-.2, r3_pct=.1, r5_pct=.6,
                              r15_pct=.7, a1_pct_per_min=-.32, a3_pct_per_min=-.01)
    packet['currentPrice'] = 9.99
    result = build(packet=packet)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['grade'] == 'B'
    assert any('回落' in x for x in result['observations'][0]['opportunity']['counterEvidence'])


def test_verified_distribution_requires_multiple_independent_adverse_observations():
    row, packet = candidate(), minute()
    row['quote']['active_buy_share_pct'] = 30
    packet['v11Inputs'].update(r1_pct=-.5, r3_pct=-1., r5_pct=-.7,
                              a1_pct_per_min=-.36, a3_pct_per_min=-.2, failed_breakout_risk=100)
    result = build(row, packet)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['grade'] == 'C'
    assert any('派发' in x for x in result['observations'][0]['opportunity']['hardVetoes'])


@pytest.mark.parametrize('mutation', ['stale', 'future', 'wrong_code', 'conflicting_price', 'tainted', 'no_asks', 'locked'])
def test_bad_quotes_tainted_data_and_missing_buy_window_never_recommend(mutation):
    row = candidate()
    if mutation == 'stale':
        row['quote']['source_time'] = (NOW-timedelta(seconds=31)).isoformat()
    elif mutation == 'future':
        row['quote']['source_time'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'wrong_code':
        row['quote']['code'] = '600999'
    elif mutation == 'conflicting_price':
        row['quote']['high'] = 9.9
    elif mutation == 'tainted':
        row['dataLineage']['status'] = 'tainted'
    elif mutation == 'no_asks':
        row['quote']['order_book']['asks'] = []
    elif mutation == 'locked':
        row['quote']['locked_limit_up'] = True
    result = build(row)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['grade'] == 'C'


def test_missing_book_is_unknown_not_claimed_no_offer_or_executable():
    row = candidate()
    row['quote'].pop('order_book')
    item = build(row)['observations'][0]
    assert item['opportunity']['grade'] == 'B'
    assert item['opportunity']['buyWindow']['status'] == 'unknown'
    assert item['opportunity']['hardVetoes'] == []


def test_future_or_unfinished_minute_does_not_support_recommendation():
    future = minute(asOf=(NOW+timedelta(minutes=1)).isoformat())
    assert build(packet=future)['observations'][0]['opportunity']['grade'] == 'C'
    unfinished = minute(asOf=NOW.replace(second=0).isoformat(), sourceTimePrecision='minute_label')
    assert build(packet=unfinished)['recommendations'] == []


def test_confirmed_continuation_is_separate_m_channel_without_old_model_gate():
    packet = minute()
    packet['v11Inputs'].update(r1_pct=.2, r3_pct=.6, r5_pct=1., r15_pct=1.5,
                              a1_pct_per_min=0., a3_pct_per_min=.1)
    packet['metrics']['vwap'] = 9.8
    packet['trendEvidence'] = {'asOf': MINUTE.isoformat(),
        'metrics': {'signed_log_efficiency_15m': .8}}
    item = build(packet=packet)['recommendations'][0]
    assert item['opportunity']['channel'] == 'M'
    assert item['opportunity']['grade'] == 'M-A'
    assert item['prediction']['status'] == 'rules_unvalidated'


def test_catalyst_must_be_sourced_available_and_known_before_decision():
    row, packet = candidate(), minute()
    row['quote'].pop('active_buy_share_pct')
    packet['v11Inputs'].pop('amount_15m')
    event = {'source': 'fixture_disclosure', 'publishedAt': (NOW-timedelta(hours=1)).isoformat(),
             'firstSeenAt': (NOW-timedelta(minutes=5)).isoformat(),
             'availableAt': (NOW-timedelta(minutes=4)).isoformat(),
             'direction': 'positive', 'title': '可核验新订单'}
    assert build(row, packet)['recommendations'] == []
    assert build(row, packet, contexts={'600001': {'catalysts': [event]}})['recommendations']
    event['availableAt'] = (NOW+timedelta(seconds=1)).isoformat()
    assert build(row, packet, contexts={'600001': {'catalysts': [event]}})['recommendations'] == []


def test_known_major_event_veto_is_not_inferred_from_missing_context():
    event = {'source': 'fixture_disclosure', 'publishedAt': (NOW-timedelta(hours=1)).isoformat(),
             'firstSeenAt': (NOW-timedelta(minutes=5)).isoformat(),
             'availableAt': (NOW-timedelta(minutes=4)).isoformat(),
             'severity': 'major', 'title': '明确交易限制'}
    result = build(contexts={'600001': {'regulatoryRisks': [event]}})
    assert result['observations'][0]['opportunity']['grade'] == 'C'
    event.pop('source')
    assert build(contexts={'600001': {'regulatoryRisks': [event]}})['recommendations']


def test_first_detection_is_not_backdated_to_source_time_and_requires_legal_previous_record():
    first = build()
    later = NOW+timedelta(minutes=1)
    row = candidate()
    row['quote']['source_time'] = later.isoformat()
    row['quote']['order_book']['source_time'] = later.isoformat()
    row['quote']['active_flow_as_of'] = later.isoformat()
    packet = minute(asOf=NOW.replace(second=0).isoformat())
    result = build_opportunity_channels([row], later, minute_packets={'600001': packet}, previous=first)
    observed = result['recommendations'][0]['opportunity']
    assert observed['firstDetectionAt'] == NOW.isoformat()
    assert observed['detectedAt'] == later.isoformat()
    assert observed['triggerAt'] is None
    first['researchCandidates'][0]['opportunity']['version'] = 'old'
    result = build_opportunity_channels([row], later, minute_packets={'600001': packet}, previous=first)
    assert result['recommendations'][0]['opportunity']['firstDetectionAt'] == later.isoformat()


def test_copies_preserve_existing_formal_pipeline_and_do_not_mutate_input():
    row = candidate()
    row.update(formalEligible=True, trainingEligible=True, executable=True)
    original = deepcopy(row)
    result = build(row)
    assert row == original
    result['recommendations'][0]['quote']['price'] = 99
    assert row['quote']['price'] == 10
    assert result['recommendations'][0]['formalEligible'] is False


def test_cap_is_five_deduplicated_and_never_padded_with_unqualified_rows():
    rows = [candidate(f'60000{i}') for i in range(1, 8)]
    packets = {row['code']: minute() for row in rows}
    result = build_opportunity_channels(rows+rows, NOW, top_n=20, minute_packets=packets)
    assert len(result['recommendations']) == 5
    assert len(result['researchCandidates']) == 7
    assert len(result['observations']) == 2
    assert all(row['opportunity']['rankScore'] is not None for row in result['recommendations'])
    assert build_opportunity_channels(rows, NOW, minute_packets={})['recommendations'] == []


def test_decision_clock_must_be_timezone_aware():
    with pytest.raises(ValueError):
        build_opportunity_channels([], datetime(2026, 10, 28, 9, 40))


def test_tainted_old_history_does_not_block_independently_verified_current_path():
    row, packet = candidate(), minute()
    lineage = {'status': 'tainted', 'sessions': [
        {'code': '600001', 'day': '2026-10-27', 'status': 'tainted'},
        {'code': '600001', 'day': '2026-10-28', 'status': 'verified',
         'verifiedThrough': MINUTE.isoformat()}]}
    row['dataLineage'] = deepcopy(lineage)
    packet['dataLineage'] = deepcopy(lineage)
    packet['v11Inputs'].update(z_r3_pct=999, v3_ratio=999, va_ratio=999)
    item = build(row, packet)['recommendations'][0]['opportunity']
    assert item['grade'] == 'E-A'
    assert 'z_r3_pct' not in item['metrics']
    assert 'v3_ratio' not in item['metrics']
    packet['dataLineage']['sessions'][-1]['status'] = 'tainted'
    assert build(row, packet)['recommendations'] == []


@pytest.mark.parametrize('bad_value', [float('nan'), float('inf'), True])
def test_malformed_optional_quote_prices_are_data_conflicts(bad_value):
    row = candidate()
    row['quote']['high'] = bad_value
    assert build(row)['observations'][0]['opportunity']['grade'] == 'C'


def test_minute_price_conflicting_with_today_quote_is_not_an_early_opportunity():
    assert build(packet=minute(currentPrice=100))['observations'][0]['opportunity']['grade'] == 'C'


def test_broken_amount_volume_units_do_not_pass_on_fresh_quote_time():
    row = candidate()
    row['quote'].update(amount=100000000, volume=1000)
    assert build(row)['observations'][0]['opportunity']['grade'] == 'C'


def test_first_impulse_shallow_pullback_retains_early_opportunity_with_counter_evidence():
    packet = minute()
    packet['v11Inputs'].update(r1_pct=-.03, r3_pct=.9, r5_pct=1.2, r15_pct=.5,
                              a1_pct_per_min=-.27, a3_pct_per_min=.26666667)
    item = build(packet=packet)['recommendations'][0]['opportunity']
    assert item['grade'] == 'E-A'
    assert any('回落' in text for text in item['counterEvidence'])
    assert item['metrics']['recentPullbackRankAdjustment'] == -.03
    assert item['metrics']['rankingChangeRate'] < item['metrics']['accelerationPerMinute']


def test_confirmed_continuation_survives_one_minute_pullback():
    packet = minute()
    packet['v11Inputs'].update(r1_pct=-.04, r3_pct=.6, r5_pct=1., r15_pct=1.5,
                              a1_pct_per_min=-.24, a3_pct_per_min=.1)
    packet['metrics']['vwap'] = 9.8
    packet['trendEvidence'] = {'asOf': MINUTE.isoformat(),
        'metrics': {'signed_log_efficiency_15m': .8}}
    item = build(packet=packet)['recommendations'][0]['opportunity']
    assert item['grade'] == 'M-A'
    assert item['hardVetoes'] == []
    assert any('回落' in text for text in item['counterEvidence'])


def test_all_windows_weak_without_verified_distribution_remain_observation():
    packet = minute()
    packet['v11Inputs'].update(r1_pct=-.3, r3_pct=-.8, r5_pct=-1., r15_pct=-1.5,
                              a1_pct_per_min=-.1, a3_pct_per_min=-.16666667)
    packet['currentPrice'] = 9.9
    item = build(packet=packet)['observations'][0]['opportunity']
    assert item['grade'] == 'B'
    assert item['hardVetoes'] == []


def test_neutral_disclosure_title_is_visible_but_cannot_supply_positive_catalyst():
    row, packet = candidate(), minute()
    row['quote'].pop('active_buy_share_pct')
    packet['v11Inputs'].pop('amount_15m')
    event = {'code': '600001', 'source': 'fixture_public_listing', 'eventId': 'notice-1',
        'title': '重大合同公告', 'category': 'order', 'impact': 'neutral', 'evidenceScope': 'title_only',
        'publishedDate': '2026-10-28', 'firstSeenAt': (NOW-timedelta(minutes=2)).isoformat(),
        'availableAt': (NOW-timedelta(minutes=2)).isoformat(),
        'observedAt': (NOW-timedelta(minutes=1)).isoformat()}
    result = build(row, packet, contexts={'600001': {'disclosures': [event]}})
    assert result['recommendations'] == []
    opportunity = result['observations'][0]['opportunity']
    assert len(opportunity['disclosureEvidence']) == 1
    assert any('仅标题，不作正催化' in text for text in opportunity['missing'])
    event['firstSeenAt'] = (NOW+timedelta(minutes=1)).isoformat()
    result = build(row, packet, contexts={'600001': {'disclosures': [event]}})
    assert result['observations'][0]['opportunity']['disclosureEvidence'] == []


@pytest.mark.parametrize('hour,minute_value,allowed', [(14, 58, True), (15, 0, False)])
def test_manual_research_does_not_inherit_1455_scheduled_slot_expiry(hour, minute_value, allowed):
    now = NOW.replace(hour=hour, minute=minute_value)
    row = candidate()
    row['quote']['source_time'] = now.isoformat()
    row['quote']['order_book']['source_time'] = now.isoformat()
    row['quote']['active_flow_as_of'] = now.isoformat()
    packet = minute(asOf=(now.replace(second=0)-timedelta(minutes=1)).isoformat())
    result = build_opportunity_channels([row], now, minute_packets={'600001': packet})
    assert bool(result['recommendations']) is allowed
    if not allowed:
        assert result['observations'][0]['opportunity']['grade'] == 'C'


def test_one_minute_bounce_in_falling_windows_does_not_become_recommendation():
    packet = minute()
    packet['v11Inputs'].update(r1_pct=.1, r3_pct=-.8, r5_pct=-3., r15_pct=-5.,
                              a1_pct_per_min=.7, a3_pct_per_min=.0666667)
    result = build(packet=packet)
    assert result['recommendations'] == []
    item = result['observations'][0]['opportunity']
    assert item['metrics']['repairConfirmed'] is False
    assert any('单分钟反弹' in value for value in item['missing'])


def test_latest_quote_below_frozen_window_low_invalidates_positive_old_minute():
    row, packet = candidate(), minute()
    packet['metrics']['local_low_5m'] = 9.99
    row['quote'].update(price=9.98, change_pct=(9.98/10.2-1)*100)
    result = build(row, packet)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['metrics']['structureHeldAtDecision'] is False
    assert any('局部低点' in value for value in result['observations'][0]['opportunity']['counterEvidence'])


def test_new_quote_cannot_preserve_an_already_lost_minute_breakout():
    packet = minute(currentPrice=10.05)
    packet['metrics']['local_high_5m'] = 10.02
    result = build(packet=packet)
    assert result['recommendations'] == []
    assert any('跌回分钟突破位' in value for value in result['observations'][0]['opportunity']['counterEvidence'])


@pytest.mark.parametrize('risk', ['failed_breakout_risk', 'lower_high_risk'])
def test_unrepaired_structure_is_not_overridden_by_positive_one_minute_or_cumulative_flow(risk):
    packet = minute()
    packet['v11Inputs'][risk] = 100
    result = build(packet=packet)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['metrics']['structureHeldAtDecision'] is False


def test_completed_reclaim_can_confirm_reversal_before_three_minutes_turn_positive():
    row, packet = candidate(), minute()
    packet['v11Inputs'].update(r3_pct=-.2, vwap_reclaim=1)
    packet['metrics']['vwap'] = 9.98
    assert row['quote']['change_pct'] < 0
    item = build(row, packet)['recommendations'][0]['opportunity']
    assert item['grade'] == 'E-A'
    assert item['metrics']['repairConfirmed'] is True


def test_price_breakout_and_acceleration_cannot_supply_their_own_independent_support():
    row, packet = candidate(), minute()
    row['quote'].pop('active_buy_share_pct')
    packet['v11Inputs'].pop('amount_15m')
    packet['metrics']['local_high_5m'] = 9.99
    result = build(row, packet)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['metrics']['independentInitiative'] is False


def test_unsigned_volume_and_cumulative_buying_do_not_override_negative_current_flow():
    row, packet = candidate(), minute()
    row['fundEvidence'] = {'source': 'fixture_fund', 'asOf': MINUTE.isoformat(),
                           'metrics': {'main_net_flow_3m': -1000000}}
    result = build(row, packet)
    assert result['recommendations'] == []
    assert result['observations'][0]['opportunity']['metrics']['independentInitiative'] is False


def test_negative_comparison_window_cannot_artificially_boost_rebound_priority():
    rebounding, advancing = candidate('600001'), candidate('600002')
    rebound, advance = minute(), minute()
    rebound['v11Inputs'].update(r1_pct=.1, r3_pct=.1, r5_pct=-10., r15_pct=-12.,
                               a1_pct_per_min=2.1, a3_pct_per_min=.833333)
    advance['v11Inputs'].update(r1_pct=.4, r3_pct=.8, r5_pct=1., r15_pct=1.,
                               a1_pct_per_min=.2, a3_pct_per_min=.2)
    result = build_opportunity_channels([rebounding, advancing], NOW,
        minute_packets={'600001': rebound, '600002': advance})
    assert [row['code'] for row in result['recommendations']] == ['600002', '600001']
    assert result['recommendations'][1]['opportunity']['metrics']['rankingChangeRate'] == pytest.approx(.1)


def test_saved_oct29_decision_quotes_cannot_recommend_after_their_structure_has_broken():
    fixtures = json.loads((Path(__file__).parent/'fixtures/opportunity_20261029_regressions.json').read_text(encoding='utf-8'))
    assert {item['candidate']['code'] for item in fixtures} == {'600506', '601188'}
    for saved in fixtures:
        row = saved['candidate']
        assert saved['originalGrade'] == 'E-A'
        assert row['quote']['price'] < saved['minute']['v11Inputs']['local_low_5m']
        result = build_opportunity_channels([row], datetime.fromisoformat(saved['asOf']),
            minute_packets={row['code']: saved['minute']})
        assert result['recommendations'] == []
        assessed = result['observations'][0]['opportunity']
        assert assessed['metrics']['structureHeldAtDecision'] is False
        assert any('局部低点' in value for value in assessed['counterEvidence'])
