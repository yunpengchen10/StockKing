from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from src.services.yao_scout.potential_picks import select_potential_picks
from src.services.yao_scout.strategy_channels import (
    StrategyRuleStore, build_strategy_publication, validate_rule,
)


STAMP = '2026-11-19T10:30:00+08:00'


def candidate(code, ceiling=13):
    return {'code': code, 'name': code, 'dailyLastClose': 10,
        'quote': {'code': code, 'source': 'test', 'source_time': STAMP,
            'price': 10.4, 'pre_close': 10, 'open_price': 10.1,
            'high': 10.5, 'low': 10, 'limit_up': 11, 'amount': 102500, 'volume': 10000,
            'order_book': {'source': 'test', 'source_time': STAMP,
                'asks': [{'price': 10.41, 'volume_shares': 100}],
                'bids': [{'price': 10.4, 'volume_shares': 100}]}},
        'dailyEvidence': {'source': 'daily', 'asOf': '2026-11-18', 'metrics': {
            'ma5': 10.2, 'ma10': 10.1, 'ma20': 10, 'atr14': .3,
            'high20': ceiling, 'high60': ceiling}}}


def series(values, *, amounts=True):
    start = datetime.fromisoformat(STAMP) - timedelta(minutes=20)
    points = []
    for index in range(21):
        section = min(index // 5, 3)
        fraction = (index - section * 5) / 5
        price = values[section] + fraction * (values[section+1] - values[section])
        points.append({'sourceTime': (start+timedelta(minutes=index)).isoformat(), 'price': price,
                       'amount': (1000 if index <= 10 else 2000) if amounts else None})
    return {'source': 'test', 'sourceTime': STAMP, 'points': points,
            'amountKind': 'interval', 'amountUnit': 'CNY'}


def run_for(rows, shapes=None, peers=(10, 9.99, 9.98, 10, 10.01)):
    codes = [row['code'] for row in rows]
    peer_codes = ['600101', '600102', '600103']
    stocks = {code: series(shape) for code, shape in (shapes or {}).items()}
    stocks.update({code: series(peers) for code in peer_codes})
    return {'run_id': 'fixture-1', 'generatedAt': '2026-11-19T10:30:10+08:00',
        'controls': rows, 'dailyAssessment': {'baselineAsOf': '2026-11-18'},
        'strategyMarketEvidence': {'stocks': stocks},
        'themeBoard': {'themes': [{'id': 'theme-1', 'name': '通信', 'sourceTime': STAMP,
                                  'memberCodes': codes+peer_codes}]}}


def test_trend_reuses_exact_existing_ranking_and_compact_item_shape():
    run = run_for([candidate(f'60000{i}', 12+i) for i in range(7)])
    original = select_potential_picks(run, None)
    result = build_strategy_publication(run, 'trend_space')
    assert [r['code'] for r in result['items']] == [r['code'] for r in original]
    for expected, actual in zip(original, result['items']):
        assert actual['rewardRisk'] == expected['rewardRisk']
        assert actual['reasons'] == expected['reasons']
        assert 'quote' not in actual and 'dailyEvidence' not in actual
        assert actual['strategyMatches'][0]['rank'] == expected['rank']
    parameters = result['ruleSnapshots'][0]['parameters']
    assert parameters['implementationVersion'] == 'potential-picks-v1'
    assert parameters['minimumRewardRisk'] == 1
    assert 'minimumPeersExcludingSelf' not in parameters
    assert len(json.dumps(result)) < 25000


def test_trend_keeps_after_close_reference_and_rejects_intraday_price():
    row = candidate('600001')
    run = run_for([row])
    run['generatedAt'] = '2026-11-19T20:30:00+08:00'
    assert not build_strategy_publication(run, 'trend_space')['items']
    row['quote']['source_time'] = '2026-11-19T15:00:00+08:00'
    item, = build_strategy_publication(run, 'trend_space')['items']
    assert item['basisLabel'] == '收盘价'


def test_divergence_requires_actual_rally_pullback_volume_and_peers():
    row = candidate('600001')
    row['dailyEvidence']['metrics']['ma5'] = 9  # independent of trend admission
    run = run_for([row], {row['code']: (10, 10.2, 10.05, 10.25, 10.4)})
    result = build_strategy_publication(run, 'divergence_strength')
    assert [r['code'] for r in result['items']] == ['600001']
    assert not build_strategy_publication(run, 'trend_space')['items']
    paths = run['strategyMarketEvidence']['stocks']
    paths[row['code']]['points'][12]['amount'] = None
    assert not build_strategy_publication(run, 'divergence_strength')['items']
    paths[row['code']] = series((10, 10.2, 10.05, 10.25, 10.4))
    del paths['600103']
    assert not build_strategy_publication(run, 'divergence_strength')['items']


def test_theme_lead_requires_catalyst_and_later_peer_expansion():
    row = candidate('600001')
    run = run_for([row], {row['code']: (10, 10.2, 10.21, 10.3, 10.4)})
    assert not build_strategy_publication(run, 'theme_lead')['items']
    run['marketBrief'] = {'mainlines': [{'id': 'theme-1', 'name': '通信',
        'leaders': [row['code']], 'source': 'official-news', 'catalyst': '已核验具体事件',
        'eventSourceTime': '2026-11-19T10:00:00+08:00'}]}
    assert len(build_strategy_publication(run, 'theme_lead')['items']) == 1
    run['marketBrief']['mainlines'][0]['eventSourceTime'] = '2026-11-19T10:31:00+08:00'
    assert not build_strategy_publication(run, 'theme_lead')['items']


def test_countertrend_requires_two_distinct_downlegs_weak_market_and_volume():
    row = candidate('600001')
    run = run_for([row], {row['code']: (10, 10.01, 10.02, 10.05, 10.4)})
    packet = run['strategyMarketEvidence']
    packet['benchmark'] = {**series((4000, 3990, 3998, 3980, 3985)), 'previousClose': 4010}
    assert len(build_strategy_publication(run, 'countertrend_leader')['items']) == 1
    packet['benchmark'] = {**series((4000, 3990, 3985, 3980, 3985)), 'previousClose': 4010}
    assert not build_strategy_publication(run, 'countertrend_leader')['items']
    packet['benchmark'] = {**series((4000, 3990, 3998, 3980, 3985)), 'previousClose': 3900}
    assert not build_strategy_publication(run, 'countertrend_leader')['items']


def test_touch_elsewhere_and_stale_or_missing_sequences_do_not_match():
    row = candidate('600001')
    run = run_for([row], {row['code']: (10, 10.2, 10.05, 10.25, 10.4)})
    run['windvanes'] = [deepcopy(row)]
    run['windvanes'][0]['quote']['high'] = 11
    assert not build_strategy_publication(run)['items']
    del run['windvanes']
    run['strategyMarketEvidence']['stocks'][row['code']]['points'] = []
    assert not build_strategy_publication(run, 'divergence_strength')['items']


def test_round_robin_preserves_multichannel_matches_and_five_limit():
    rows = [candidate(f'60000{i}', 20-i) for i in range(7)]
    run = run_for(rows, {'600006': (10, 10.2, 10.05, 10.25, 10.4),
                         '600000': (10, 10.2, 10.05, 10.25, 10.4)})
    result = build_strategy_publication(run)
    assert len(result['items']) == 5
    assert len({r['code'] for r in result['items']}) == 5
    assert '600006' in {r['code'] for r in result['items']}
    first = next(r for r in result['items'] if r['code'] == '600000')
    assert {m['id'] for m in first['strategyMatches']} == {'trend_space', 'divergence_strength'}


def test_import_preview_enable_and_version_change_are_bound(tmp_path):
    store = StrategyRuleStore(tmp_path/'rules.sqlite3')
    rule = {'id': 'custom.mine', 'name': '我的策略', 'version': '1', 'baseStrategy': 'eligible',
            'conditions': [{'field': 'amountCny', 'op': 'gt', 'value': 10000}],
            'exclusions': [{'field': 'changePct', 'op': 'gt', 'value': 5}],
            'rankBy': {'field': 'changePct', 'direction': 'desc'}}
    assert store.import_rule(rule)['enabled'] is False
    with pytest.raises(ValueError, match='试跑'):
        store.set_enabled(rule['id'], True)
    run = run_for([candidate('600001')])
    original = deepcopy(run)
    preview = store.preview(run, rule['id'])
    assert preview['status'] == 'previewed' and len(preview['items']) == 1
    assert preview['ruleSnapshots'][0]['baseRule']['id'] == 'eligible'
    assert run == original and 'publishedPicks' not in run
    store.set_enabled(rule['id'], True)
    assert len(build_strategy_publication(run, rule['id'], store.list())['items']) == 1
    rule['conditions'][0]['value'] = 999999
    with pytest.raises(ValueError, match='version'):
        store.import_rule(rule)
    rule['version'] = '2'
    assert store.import_rule(rule)['preview'] is None
    assert store.preview(run, rule['id'])['items'] == []
    assert store.set_enabled(rule['id'], True)['enabled'] is True


def test_historical_version_cannot_be_redefined_and_enable_checks_reviewed_version(tmp_path):
    store = StrategyRuleStore(tmp_path/'rules.sqlite3')
    rule = {'id': 'custom.mine', 'name': '我的策略', 'version': '1', 'baseStrategy': 'eligible'}
    initial = store.import_rule(rule)
    run = run_for([candidate('600001')])
    store.preview(run, rule['id'])
    store.import_rule({**rule, 'version': '2'})
    store.preview(run, rule['id'])
    with pytest.raises(ValueError, match='已经变化'):
        store.set_enabled(rule['id'], True, expected_version='1', expected_fingerprint=initial['ruleFingerprint'])
    with pytest.raises(ValueError, match='历史已登记版本'):
        store.import_rule({**rule, 'name': '更改了规则'})
    assert store.list()[0]['version'] == '2'


def test_volume_fallback_when_minute_amount_is_missing():
    row = candidate('600001')
    run = run_for([row], {row['code']: (10, 10.2, 10.05, 10.25, 10.4)})
    packet = run['strategyMarketEvidence']['stocks'][row['code']]
    packet.update(volumeKind='interval', volumeUnit='shares')
    for index, point in enumerate(packet['points']):
        point['amount'] = None
        point['volume'] = 100 if index <= 10 else 200
    item, = build_strategy_publication(run, 'divergence_strength')['items']
    assert any(e['label'] == '四段每分钟成交量' and e['unit'] == '股'
               for e in item['strategyMatches'][0]['evidence'])
    packet['points'][12]['volume'] = None
    assert not build_strategy_publication(run, 'divergence_strength')['items']


@pytest.mark.parametrize('change', [{'script': 'print(1)'}, {'baseStrategy': '__import__'},
    {'conditions': [{'field': '__dict__', 'op': 'eq', 'value': 1}]},
    {'conditions': [{'field': 'score', 'op': 'gt', 'value': float('nan')}]}])
def test_declarative_rules_reject_unknown_or_nonfinite_input(change):
    rule = {'id': 'custom.mine', 'name': 'mine', 'version': '1', 'baseStrategy': 'eligible'}
    with pytest.raises(ValueError):
        validate_rule({**rule, **change})
