from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.services.yao_scout.daily_opportunities import TZ
from src.services.yao_scout.recommendation_evidence import (
    daily_evidence, explain_candidate, research_queue, research_queue_limit,
)

NOW = datetime(2026, 9, 15, 10, 30, tzinfo=TZ)


def history():
    data = pd.DataFrame({'date':pd.bdate_range(end='2026-09-14', periods=60),
                         'close':[9.2+i*.6/59 for i in range(60)]})
    data['high'], data['low'] = data.close+1, data.close-.2
    data.attrs['daily_source'] = 'test_daily'
    return data


def quote(**changes):
    result = {'code':'600001', 'price':10, 'pre_close':9.8, 'high':10.05, 'low':9.65,
              'open_price':9.7, 'change_pct':100*(10/9.8-1), 'volume':100000000,
              'amount':980000000, 'source':'test_quote', 'provider_timestamp':NOW.isoformat(),
              'active_buy_share_pct':60,'active_flow_source':'fixture','active_flow_as_of':NOW.isoformat()}
    result.update(changes)
    return result


def explain(q=None, hist=None, minutes=None):
    if minutes is None:
        minutes = {'metrics':{'speed_3m_pct':.3,'speed_5m_pct':.6,'local_high_5m':9.99,
                             'local_low_5m':9.7,'amount_3m':5000000,'relative_amount_3m_20d':2},'historyDays':20,
                   'asOf':(NOW-timedelta(minutes=1)).isoformat(), 'source':'test_minutes','gaps':[]}
    return explain_candidate({'code':'600001','volume_ratio':2.4,'turnover_rate':12}, q or quote(),
        daily_evidence(history() if hist is None else hist, NOW), minutes, NOW, '1030',
        sector={'metrics':{'sector_coverage_pct':100,'sector_peer_count':10,'sector_return_5m_pct':.2,'sector_breadth':80,'sector_relative_5m_pct':.4},
                'asOf':(NOW-timedelta(minutes=1)).isoformat(),'sector':{'name':'测试行业'},'source':'fixture'},
        funds={'metrics':{'main_net_flow':-1e6,'main_net_flow_3m':1e5},'asOf':(NOW-timedelta(minutes=1)).isoformat(),'source':'fixture'})


def test_evidence_and_units_are_real_not_a_model_failure_message():
    result = explain()
    assert result['evidenceEligible'] is True
    evidence = {row['key']:row for row in result['indicatorEvidence']}
    assert evidence['vwap']['value'] == 9.8
    assert evidence['vwap']['unit'] == '元'
    assert evidence['vwap']['asOf'] == NOW.isoformat()
    assert evidence['relative_amount_3m_20d']['value'] == 2
    assert evidence['sector_relative_5m_pct']['status'] == 'observed'
    assert evidence['main_net_flow']['value'] == -1e6
    assert '成交总额不代替资金方向' in result['selectionReasons'][0]
    assert result['probabilities'] == {}
    assert not any('门槛' in reason or '降级' in reason for reason in result['selectionReasons'])


def test_rising_intraday_but_still_red_can_be_observed():
    data = history()
    data.loc[data.index[-1], 'close'] = 10.2
    q = quote(pre_close=10.2, change_pct=100*(10/10.2-1))
    result = explain(q, data)
    assert result['evidenceEligible'] is True
    assert '分钟' in result['strategyBranch']


def test_hot_stock_with_no_space_cannot_fill_a_recommendation_slot():
    result = explain(quote(price=10.79, high=10.8, change_pct=100*(10.79/9.8-1)), minutes={})
    assert result['evidenceEligible'] is False
    assert any('空间不足' in risk for risk in result['risks'])


def test_no_history_or_adjustment_mismatch_does_not_fabricate_space():
    result = explain(hist=pd.DataFrame())
    assert result['evidenceEligible']  # Minutes can support observation without a daily target.
    assert result['structureRewardRisk'] is None
    result = explain(quote(pre_close=9.0, change_pct=100*(10/9.0-1)))
    assert result['evidenceEligible']
    assert any('复权' in risk for risk in result['risks'])


def test_future_current_day_and_duplicates_cannot_change_history_features():
    original = daily_evidence(history(), NOW)
    extra = pd.DataFrame([{'date':'2026-09-15','close':100,'high':110,'low':90},
                          {'date':'2026-09-16','close':200,'high':210,'low':190}])
    assert daily_evidence(pd.concat([history(), extra]), NOW)['metrics'] == original['metrics']
    duplicated = pd.concat([history(), history().tail(1)])
    assert daily_evidence(duplicated, NOW)['metrics'] == {}


def test_bad_vwap_stale_quote_and_zero_volume_are_not_confirmed_support():
    for q in [quote(amount=1e12), quote(volume=0), quote(provider_timestamp=(NOW-timedelta(seconds=31)).isoformat())]:
        assert not explain(q)['evidenceEligible']


def test_high_volatility_has_specific_risk_and_is_not_automatically_excluded():
    result = explain(quote(high=12, low=8))
    assert result['evidenceEligible']
    assert '高波动' in result['strategyBranch']
    assert any('ATR14' in reason for reason in result['riskReasons'])


def test_research_queue_has_no_gain_filter_and_does_not_reward_gain_alone():
    rows = pd.DataFrame([{'code':'600001','change_pct':9.99,'amount':10,'volume_ratio':1,'turnover_rate':1},
                         {'code':'600002','change_pct':-5,'amount':1000,'volume_ratio':3,'turnover_rate':10}])
    assert research_queue(rows, 1)[0]['code'] == '600002'
    assert len(research_queue(rows)) == 2


@pytest.mark.parametrize('size, expected', [
    (0, 0), (30, 30), (80, 80), (300, 300), (301, 300),
    (3000, 300), (3052, 306), (5000, 500),
])
def test_research_queue_budget_scales_with_unique_universe(size, expected):
    rows = pd.DataFrame({'code': [f'{600000 + i:06d}' for i in range(size)],
                         'amount': list(range(size, 0, -1))})
    assert research_queue_limit(size) == expected
    queued = research_queue(rows)
    assert len(queued) == expected
    assert [row['code'] for row in queued] == rows.code.head(expected).tolist()


def test_research_queue_explicit_budget_can_be_small_zero_or_larger_than_universe():
    rows = pd.DataFrame({'code': ['600001', '600002', '600003'], 'amount': [1, 3, 2]})
    assert [row['code'] for row in research_queue(rows, 1)] == ['600002']
    assert research_queue(rows, 0) == []
    assert len(research_queue(rows, 1000)) == 3
    with pytest.raises(ValueError, match='negative'):
        research_queue(rows, -1)
    with pytest.raises(ValueError, match='negative'):
        research_queue(pd.DataFrame(), -1)
    with pytest.raises(ValueError, match='negative'):
        research_queue_limit(-1)


def test_research_queue_keeps_missing_activity_and_negative_returns_in_coverage():
    rows = pd.DataFrame([
        {'code': '600001', 'amount': 100, 'change_pct': -9.5},
        {'code': '600002', 'change_pct': -4},
        {'code': '600003'},
    ])
    queued = research_queue(rows)
    assert [row['code'] for row in queued] == ['600001', '600002', '600003']
    assert pd.isna(queued[1]['amount']) and pd.isna(queued[2]['amount'])


def test_research_queue_remains_a_union_of_independent_activity_leaders():
    rows = pd.DataFrame([
        {'code': '600001', 'amount': 1000, 'volume_ratio': 1, 'turnover_rate': 1, 'open': 10, 'price': 10},
        {'code': '600002', 'amount': 1, 'volume_ratio': 10, 'turnover_rate': 1, 'open': 10, 'price': 10},
        {'code': '600003', 'amount': 1, 'volume_ratio': 1, 'turnover_rate': 10, 'open': 10, 'price': 10},
        {'code': '600004', 'amount': 1, 'volume_ratio': 1, 'turnover_rate': 1, 'open': 10, 'price': 11},
        {'code': '600000', 'amount': 2, 'volume_ratio': 2, 'turnover_rate': 2, 'open': 10, 'price': 10.5},
    ])
    assert [row['code'] for row in research_queue(rows, 4)] == ['600001', '600002', '600003', '600004']


def test_research_queue_duplicates_cannot_consume_slots_or_expand_default_budget():
    unique = pd.DataFrame({'code': [f'{600000 + i:06d}' for i in range(3052)],
                          'amount': list(range(3052, 0, -1))})
    rows = pd.concat([unique.iloc[:1]] * 3000 + [unique], ignore_index=True)
    queued = research_queue(rows)
    assert len(queued) == research_queue_limit(3052) == 306
    assert len({row['code'] for row in queued}) == 306
    assert [row['code'] for row in research_queue(rows, 2)] == ['600000', '600001']


def test_fallback_score_label_does_not_claim_a_qualified_model(tmp_path):
    from src.quant.tier_models import TierModelService
    service = TierModelService(tmp_path/'models', tmp_path/'cache')
    result = service._rule_fallback('regular', [{'code':'600001','score':90}], None)
    assert '未验证规则观察' in result[0]['scoreMeaning']
    assert '不是模型收益预测' in result[0]['scoreMeaning']


def test_new_high_is_not_excluded_for_lack_of_a_target_or_rr():
    result = explain(quote(price=12, high=12, change_pct=100*(12/9.8-1)))
    assert result['evidenceEligible']
    assert result['structureRewardRisk'] is None
    assert any('上方空间未量化' in reason for reason in result['selectionReasons'])


def test_minutes_are_rechecked_at_decision_and_not_replaced_by_daily_heat():
    minutes = {'metrics':{'speed_3m_pct':3,'speed_5m_pct':5,'local_high_5m':9.5},
               'asOf':(NOW-timedelta(minutes=5)).isoformat()}
    assert not explain(minutes=minutes)['evidenceEligible']


def test_rr_does_not_control_observation_order():
    from src.services.yao_scout.recommendation_evidence import evidence_order
    daily = {'code':'600001','evidenceEligible':True,'structureRewardRisk':99999,
             'evidencePriority':'premarket_structure','quote':{'amount':1e9}}
    minute = {'code':'600002','evidenceEligible':True,'structureRewardRisk':None,
              'evidencePriority':'minute_price_and_turnover','quote':{'amount':1e8}}
    assert sorted([daily,minute], key=evidence_order)[0] is minute


@pytest.mark.parametrize('changes', [
    {'code':'600002'}, {'source':None}, {'provider_timestamp':None},
    {'provider_timestamp':(NOW-timedelta(days=1)).isoformat()}, {'open_price':-1},
    {'high':9,'low':8,'open_price':0}, {'price':0},
])
def test_premarket_cannot_bypass_quote_identity_or_integrity(changes):
    result = explain_candidate({'code':'600001'}, quote(**changes), daily_evidence(history(), NOW), {}, NOW, '0920')
    assert not result['evidenceEligible']


def test_premarket_can_observe_without_claiming_auction_ohlc():
    result = explain_candidate({'code':'600001'}, quote(open_price=0,high=0,low=0,amount=0,volume=0),
                               daily_evidence(history(), NOW), {}, NOW, '0920')
    assert result['evidenceEligible']
    assert any('竞价字段' in gap for gap in result['evidenceGaps'])


def test_incomplete_twenty_day_history_cannot_recommend_even_with_strong_prices():
    result = explain(minutes={'metrics':{'speed_3m_pct':3,'speed_5m_pct':5,'local_high_5m':9.9,
        'relative_amount_3m_20d':9},'asOf':(NOW-timedelta(minutes=1)).isoformat(),'historyDays':19})
    assert not result['evidenceEligible']
    assert not result['evidenceChecks']['history_20d']


def test_stale_active_classification_does_not_pass_fund_gate():
    assert not explain(quote(active_flow_as_of=(NOW-timedelta(seconds=31)).isoformat()))['evidenceEligible']


def test_recovery_is_not_excluded_only_by_cumulative_sell_pressure():
    result=explain(quote(active_buy_share_pct=40))
    assert result['evidenceEligible']
    assert any('资金转向修复' in reason for reason in result['selectionReasons'])
