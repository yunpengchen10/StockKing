"""Declarative research strategies evaluated against a frozen scan's evidence."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .limit_entry import assess_limit_entry
from .next_day_watch import number, timestamp
from .potential_picks import (select_potential_picks, _candidate_rows, _blocked_codes,
                              _valid_reference_metadata, VERSION as POTENTIAL_VERSION)
from .research_policy import DEFAULT_PARAMETERS
from .theme_rotation import _invalid_quote_evidence
from src.services.public_market_quotes import market_phase

VERSION = 'strategy-channels-v1'
BUILTINS = (
    {'id': 'trend_space', 'name': '趋势空间', 'version': '1',
     'description': '复用原趋势空间规则，均线多头，已记录压力空间覆盖支撑及波动风险，按风险空间排序'},
    {'id': 'theme_lead', 'name': '题材先手', 'version': '1',
     'description': '先于同行突破，随后同行上涨扩散，排除目标自身贡献'},
    {'id': 'divergence_strength', 'name': '分歧转强', 'version': '1',
     'description': '实际回落后收复前高，同行同步改善'},
    {'id': 'countertrend_leader', 'name': '逆势领涨', 'version': '1',
     'description': '两次独立指数下探时抗跌，随后转强且超过同行'},
)
BUILTIN_IDS = frozenset(row['id'] for row in BUILTINS)
PARAMETERS = {'minimumPoints': 12, 'minimumWindowSeconds': 600, 'maximumGapSeconds': 120,
              'minimumPeersExcludingSelf': 3, 'alignmentSeconds': 60,
              'minimumExpansionRatio': 1., 'maximumCatalystAgeDays': 3,
              'anchorFractions': [0, .25, .5, .75, 1]}
REFERENCE_PARAMETERS = {
    'board': '主板非ST', 'todayLimitTouchAllowed': False, 'intradayQuoteMaximumAgeSeconds': 30,
    'intradayOfferRequired': True, 'sessionReference': '同日已核验午间或收盘价按原来源时间保留',
    'knownRiskVeto': '沿用当日触板、撤回失效、已知业绩及大额解禁风险排除',
    'maximumUnlock14dPct': DEFAULT_PARAMETERS['profiles']['regular']['max_unlock_pct'],
}
FIELDS = {
    'score': '所属通道规则分', 'rewardRisk': '趋势空间风险收益比', 'changePct': '当日涨跌幅',
    'limitDistancePct': '距当日涨停价百分比', 'amountCny': '累计成交金额元',
    'rangePosition': '当日价格区间位置0至1',
    'stockReturnPct': '观察窗个股涨跌幅', 'peerReturnPct': '剔除自身同行平均涨跌幅',
    'relativeReturnPct': '个股超同行涨跌幅百分点',
    'benchmarkReturnPct': '观察窗指数涨跌幅', 'peerAdvancingPct': '同行上涨占比',
}
OPS = {'gt', 'gte', 'lt', 'lte', 'eq'}


def _definition(rule):
    return {key: value for key, value in rule.items()
            if key in {'id', 'name', 'version', 'baseStrategy', 'conditions', 'exclusions', 'rankBy', 'description', 'parameters'}}


def validate_rule(document):
    if not isinstance(document, dict):
        raise ValueError('策略必须是 JSON 对象')
    if len(json.dumps(document, ensure_ascii=False)) > 32768:
        raise ValueError('策略文件超过 32 KB')
    allowed = {'id', 'name', 'version', 'baseStrategy', 'conditions', 'exclusions', 'rankBy', 'description'}
    if set(document) - allowed:
        raise ValueError('策略含未支持字段，只接受声明式条件和排序')
    rule = dict(document)
    if not isinstance(rule.get('id'), str) or not re.fullmatch(r'custom\.[a-z0-9][a-z0-9_-]{0,47}', rule['id']):
        raise ValueError('id 须采用 custom.xxx 格式')
    if not isinstance(rule.get('name'), str) or not 1 <= len(rule['name'].strip()) <= 60:
        raise ValueError('策略名称须为 1 至 60 个字符')
    if not isinstance(rule.get('version'), str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,32}', rule['version']):
        raise ValueError('version 须为 1 至 32 位字母、数字、点、横线或下划线')
    if not isinstance(rule.get('baseStrategy'), str) or rule['baseStrategy'] not in BUILTIN_IDS | {'eligible'}:
        raise ValueError('baseStrategy 必须是内置策略 ID 或 eligible')
    if 'description' in rule and (not isinstance(rule['description'], str) or len(rule['description']) > 500):
        raise ValueError('description 须为 500 字以内文本')
    for field in ('conditions', 'exclusions'):
        entries = rule.setdefault(field, [])
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError(f'{field} 须为最多 20 个条件的列表')
    for condition in rule['conditions'] + rule['exclusions']:
        if (not isinstance(condition, dict) or set(condition) != {'field', 'op', 'value'}
                or not isinstance(condition['field'], str) or condition['field'] not in FIELDS
                or not isinstance(condition['op'], str) or condition['op'] not in OPS
                or not isinstance(condition['value'], (int, float)) or number(condition['value']) is None):
            raise ValueError('条件须使用支持的数值字段、比较符和有限数值')
    rank = rule.setdefault('rankBy', {'field': 'score', 'direction': 'desc'})
    if (not isinstance(rank, dict) or set(rank) != {'field', 'direction'}
            or not isinstance(rank['field'], str) or rank['field'] not in FIELDS
            or not isinstance(rank['direction'], str) or rank['direction'] not in {'asc', 'desc'}):
        raise ValueError('rankBy 须包含支持的 field 和 asc/desc direction')
    return rule


def catalog_rules(custom_rules=()):
    sequence_parameters = {key: PARAMETERS[key] for key in ('minimumPoints', 'minimumWindowSeconds',
        'maximumGapSeconds', 'minimumPeersExcludingSelf', 'alignmentSeconds', 'anchorFractions')}
    parameters = {
        'trend_space': {
            'implementationVersion': POTENTIAL_VERSION,
            'implementation': 'select_potential_picks',
            'reference': {**REFERENCE_PARAMETERS, 'preopenReference': '09:25前可使用原规则核验的前收盘价'},
            'dailyBaseline': '扫描当时上一交易日已完成日线，价格口径与报价昨收一致',
            'dailyTrend': 'MA5 > MA10 > MA20 且参考价 > MA20',
            'intradayDirection': '当日参考价高于昨收且不低于累计VWAP',
            'support': 'MA20', 'riskDistance': 'max(参考价-MA20, ATR14)',
            'resistance': '已记录high20、high60和压力位中高于参考价的最近值',
            'minimumRewardRisk': 1.,
            'breakoutAlternative': '无已记录上方压力时，价格突破high20且高于VWAP',
            'ranking': ['已知压力空间优先', '风险空间比降序', '压力空间百分比降序', '股票代码升序'],
            'maximumItems': 5,
        },
        'theme_lead': {**sequence_parameters, 'reference': dict(REFERENCE_PARAMETERS),
            'maximumCatalystAgeDays': PARAMETERS['maximumCatalystAgeDays'],
            'catalyst': '具体事件关联标的及题材，真实发布时间须不晚于个股先动时点',
            'sequence': '个股第1段突破前段高点，剔除自身同行由第1段不涨转为末段上涨且上涨家数增加',
            'relativeStrength': '末段个股不低于先动价，窗口涨幅超过同行均值',
            'ranking': '个股窗口涨幅 + 超同行涨幅，降序；代码升序'},
        'divergence_strength': {**sequence_parameters, 'reference': dict(REFERENCE_PARAMETERS),
            'minimumExpansionRatio': PARAMETERS['minimumExpansionRatio'],
            'sequence': '先上冲→回落→连续两段修复，末段超过此前上冲高点',
            'volumeConfirmation': '后两段每分钟成交额均值 > 回落段；额缺失可用完整每分钟股数',
            'peerConfirmation': '同行末段较回落段改善，个股修复速度领先同行且全窗涨幅超过同行',
            'ranking': '个股窗口涨幅 + 超同行涨幅，降序；代码升序'},
        'countertrend_leader': {**sequence_parameters, 'reference': dict(REFERENCE_PARAMETERS),
            'independentDownlegs': 2, 'minimumExpansionRatio': PARAMETERS['minimumExpansionRatio'],
            'weakMarket': '指数末段低于真实昨收',
            'sequence': '指数第1段下探、第2段反弹、第3段再下探；个股两次均相对指数及同行抗跌，末段回升',
            'volumeConfirmation': '末段每分钟成交额 > 首段；额缺失可用完整每分钟股数',
            'peerConfirmation': '个股全窗涨幅超过剔除自身的同行均值',
            'ranking': '个股窗口涨幅 + 超同行涨幅，降序；代码升序'},
    }
    builtins = [{**rule, 'builtin': True, 'enabled': True, 'parameters': parameters[rule['id']]} for rule in BUILTINS]
    custom = [{**rule, 'builtin': False} for rule in custom_rules if rule.get('id') not in BUILTIN_IDS]
    return [{**rule, 'availableFields': [{'field': key, 'name': name} for key, name in FIELDS.items()]} for rule in builtins + custom]


def _rule_snapshot(rule):
    snapshot = _definition(rule)
    if rule.get('baseStrategy') == 'eligible':
        snapshot['baseRule'] = {'id': 'eligible', 'version': '1', 'name': '共同准入',
                                'parameters': dict(REFERENCE_PARAMETERS)}
    elif rule.get('baseStrategy'):
        snapshot['baseRule'] = _definition(next(base for base in catalog_rules() if base['id'] == rule['baseStrategy']))
    return snapshot


def _evidence(label, value, source, at, unit=None):
    result = {'label': label, 'value': value, 'source': source, 'sourceTime': at}
    if unit:
        result['unit'] = unit
    return result


def _rows(run):
    rows = {}
    # Rich candidates first, with frozen next-day signals merged from the view.
    for row in _candidate_rows(run):
        if row.get('code'):
            rows[row['code']] = {**rows.get(row['code'], {}), **row}
    return list(rows.values())


def _base(row, now):
    code, name = str(row.get('code', '')), str(row.get('name', ''))
    if not re.fullmatch(r'(?:000|001|002|003|600|601|603|605)\d{3}', code) or 'ST' in name.upper():
        return None
    quote = row.get('quote') or {}
    entry = assess_limit_entry(code, quote, now)
    if not entry['eligible']:
        stamp = timestamp(quote.get('provider_timestamp') or quote.get('source_time'))
        high, upper = number(quote.get('high')), number(quote.get('limit_up'))
        if (market_phase(now) not in {'post_close', 'lunch_break'}
                or not stamp or stamp.date() != now.date() or stamp > now
                or market_phase(stamp) != market_phase(now)
                or not quote.get('source') or quote.get('code') != code
                or (_invalid_quote_evidence(quote) and not _valid_reference_metadata(quote, now))
                or not high or not upper or high >= upper
                or any(quote.get(key) for key in ('suspended', 'corporate_action', 'no_price_limit', 'trading_restricted'))):
            return None
        entry.update(price=quote.get('price'), limitUp=upper, sourceTime=stamp.isoformat(),
                     limitSource=quote.get('source'), referenceOnly=True)
    price, previous, low, high, amount = [number(quote.get(key)) for key in
                                          ('price', 'pre_close', 'low', 'high', 'amount')]
    if any(v is None or v <= 0 for v in (price, previous, low, high, amount)) or not low <= price <= high:
        return None
    return {'changePct': (price / previous - 1) * 100,
            'limitDistancePct': (entry['limitUp'] / price - 1) * 100,
            'amountCny': amount, 'rangePosition': (price - low) / (high - low) if high > low else 0.,
            '_entry': entry}


def _points(packet, now):
    if not isinstance(packet, dict) or not packet.get('source') or packet.get('status', 'observed') != 'observed':
        return []
    received = timestamp(packet.get('observedAt') or packet.get('fetchedAt'))
    if received and received > now:
        return []
    result = []
    for point in packet.get('points') or []:
        at, price = timestamp(point.get('sourceTime')), number(point.get('price'))
        if at and at.date() == now.date() and at <= now and price and price > 0:
            result.append((at, price))
    # A later/duplicate point must not silently overwrite an earlier observation.
    result.sort()
    if len({at for at, _ in result}) != len(result):
        return []
    boundary = now
    if market_phase(now) == 'post_close':
        boundary = now.replace(hour=15, minute=0, second=0, microsecond=0)
    elif market_phase(now) == 'lunch_break':
        boundary = now.replace(hour=11, minute=30, second=0, microsecond=0)
    if result and (boundary - result[-1][0]).total_seconds() > 180:
        return []
    # Each series is a single continuous trading session.
    return [point for point in result if point[0].hour < 12] if boundary.hour < 12 else [point for point in result if point[0].hour >= 13]


def _aligned(points, at):
    candidates = [(abs((stamp - at).total_seconds()), price) for stamp, price in points if stamp <= at]
    if not candidates:
        return None
    seconds, price = min(candidates)
    return price if seconds <= 60 else None


def _peer_group(row, run, paths, times):
    """Compare the same members at every anchor, excluding the target itself."""
    themes = (run.get('themeBoard') or {}).get('themes') or []
    choices = []
    for theme in themes:
        members = theme.get('memberCodes') or []
        stamp = timestamp(theme.get('sourceTime'))
        if row['code'] not in members or not stamp or stamp > timestamp(run['generatedAt']):
            continue
        peers = {}
        for code in set(members) - {row['code']}:
            values = [_aligned(paths.get(code, []), at) for at in times]
            if all(value is not None for value in values):
                peers[code] = values
        if len(peers) >= 3:
            choices.append((len(peers), theme, peers))
    if not choices:
        return None
    _, theme, peers = max(choices, key=lambda item: (item[0], str(item[1].get('id'))))
    returns = [sum((values[i] / values[0] - 1) * 100 for values in peers.values()) / len(peers)
               for i in range(len(times))]
    breadth = [sum(values[i] > values[0] for values in peers.values()) / len(peers) * 100
               for i in range(len(times))]
    return {'name': theme.get('name'), 'id': theme.get('id'), 'codes': sorted(peers),
            'returns': returns, 'breadth': breadth, 'memberCount': len(set(theme['memberCodes'])) - 1,
            'sourceTime': theme['sourceTime'], 'source': theme.get('source') or (run.get('themeBoard') or {}).get('source')}


def _sequence(row, run, paths, now):
    points = paths.get(row['code'], [])
    if len(points) < 12 or (points[-1][0] - points[0][0]).total_seconds() < 600:
        return None, '缺少至少 10 分钟的连续价格序列'
    if any((later[0] - earlier[0]).total_seconds() > 120 for earlier, later in zip(points, points[1:])):
        return None, '分钟序列存在超过 2 分钟的缺口'
    # Five anchors let the decision express before/retracement/recovery order.
    indices = [round((len(points) - 1) * fraction / 4) for fraction in range(5)]
    anchors = [points[index] for index in indices]
    peers = _peer_group(row, run, paths, [at for at, _ in anchors])
    if peers is None:
        return None, '缺少同窗至少 3 名剔除自身后的题材同行对照'
    prices = [price for _, price in anchors]
    change = (prices[-1] / prices[0] - 1) * 100
    features = {'stockReturnPct': change, 'peerReturnPct': peers['returns'][-1],
                'relativeReturnPct': change - peers['returns'][-1],
                'peerAdvancingPct': peers['breadth'][-1]}
    source = (run.get('strategyMarketEvidence') or {}).get('stocks', {}).get(row['code'], {}).get('source')
    packets = (run.get('strategyMarketEvidence') or {}).get('stocks') or {}
    peer_source = ' + '.join(sorted({packets.get(code, {}).get('source', '') for code in peers['codes']}))
    evidence = [_evidence('个股观察窗涨跌', round(change, 4), source, anchors[-1][0].isoformat(), '%'),
                _evidence('同行平均涨跌（已剔除本股）', round(peers['returns'][-1], 4),
                          peer_source, anchors[-1][0].isoformat(), '%'),
                _evidence('已核验同行/题材其余成员', f"{len(peers['codes'])}/{peers['memberCount']}",
                          peers['source'], peers['sourceTime']),
                _evidence('同行股票', peers['codes'], peers['source'], peers['sourceTime']),
                _evidence('个股时序锚点', [{'sourceTime': at.isoformat(), 'price': price} for at, price in anchors],
                          source, anchors[-1][0].isoformat())]
    return {'points': points, 'anchors': anchors, 'prices': prices, 'peers': peers,
            'features': features, 'evidence': evidence}, None


def _activity(row, run, sequence):
    packet = (run.get('strategyMarketEvidence') or {}).get('stocks', {}).get(row['code'], {})
    anchors = [at for at, _ in sequence['anchors']]
    for field, unit in (('amount', 'CNY'), ('volume', 'shares')):
        if packet.get(field + 'Kind') != 'interval' or packet.get(field + 'Unit') != unit:
            continue
        points = {timestamp(point.get('sourceTime')): number(point.get(field)) for point in packet.get('points', [])}
        rates = []
        for begin, end in zip(anchors, anchors[1:]):
            values = [points.get(at) for at, _ in sequence['points'] if begin < at <= end]
            if not values or any(value is None or value < 0 for value in values):
                break
            rates.append(sum(values) / ((end - begin).total_seconds() / 60))
        if len(rates) == 4:
            return {'field': field, 'rates': rates, 'source': packet['source']}
    return None


def _catalyst(row, run, sequence):
    before = sequence['anchors'][1][0]
    events = ((row.get('discoveryEvidence') or {}).get('evidence', {}).get('catalyst') or {}).get('disclosureEvidence') or []
    for event in events:
        stamp = timestamp(event.get('publishedAt'))
        known = timestamp(event.get('availableAt') or event.get('firstSeenAt'))
        if (event.get('code') == row['code'] and event.get('positiveCatalyst') is True
                and event.get('source') and event.get('title') and stamp and known
                and stamp <= known <= before and (before-stamp).total_seconds() <= 3*86400):
            return _evidence('已关联标的催化', event['title'], event['source'], stamp.isoformat())
    for theme in (run.get('marketBrief') or {}).get('mainlines') or []:
        stamp = timestamp(theme.get('eventSourceTime'))
        theme_names = [name.strip() for name in str(theme.get('name') or '').split('/')]
        linked = sequence['peers']['name'] in theme_names or sequence['peers']['id'] == theme.get('id')
        if (row['code'] in (theme.get('leaders') or []) and linked and theme.get('source')
                and theme.get('catalyst') and stamp and stamp <= before
                and (before-stamp).total_seconds() <= 3*86400):
            return _evidence('已关联题材催化', theme['catalyst'], theme['source'], stamp.isoformat())
    return None


def _evaluate(strategy, row, base, sequence, benchmark, now, run):
    if strategy == 'eligible':
        return {'features': {**base, **((sequence or {}).get('features') or {}), 'score': 0.},
                'reasons': ['通过本策略的声明式条件和未触板参考价核验'],
                'evidence': [_evidence('当日涨跌幅', round(base['changePct'], 4),
                    base['_entry']['source'], base['_entry']['sourceTime'], '%')]}, None
    if sequence is None:
        return None, '缺少可核验的连续时序或同行对照'
    seq, peers, features = sequence['prices'], sequence['peers'], dict(sequence['features'])
    evidence = list(sequence['evidence'])
    if strategy == 'theme_lead':
        catalyst = _catalyst(row, run, sequence)
        if catalyst is None:
            return None, '缺少先动前已发布且可关联标的的具体题材催化'
        evidence.append(catalyst)
        first_move = seq[1] > seq[0] and seq[1] > max(price for _, price in sequence['points'][:len(sequence['points']) // 4])
        spreading = peers['returns'][1] <= 0 and peers['returns'][-1] > 0 and peers['breadth'][-1] > peers['breadth'][1]
        matched = first_move and spreading and seq[-1] >= seq[1] and features['relativeReturnPct'] > 0
        reasons = ['个股先突破前段高点，同行随后由弱转涨并扩大上涨家数', f"{peers['name']} 同窗对照已剔除本股"]
    elif strategy == 'divergence_strength':
        activity = _activity(row, run, sequence)
        if activity is None:
            return None, '缺少单分钟成交量额，无法核验放量修复'
        expansion = activity['rates'][2] + activity['rates'][3] > 2 * activity['rates'][1]
        matched = (seq[1] > seq[0] and seq[2] < seq[1] and seq[3] > seq[2]
                   and seq[4] > max(seq[0], seq[1], seq[3])
                   and peers['returns'][4] > peers['returns'][2] and features['relativeReturnPct'] > 0
                   and (seq[3]/seq[2]-1)*100 > peers['returns'][3]-peers['returns'][2] and expansion)
        evidence.append(_evidence('四段每分钟成交' + ('额' if activity['field'] == 'amount' else '量'),
            activity['rates'], activity['source'], sequence['anchors'][-1][0].isoformat(), '元' if activity['field'] == 'amount' else '股'))
        reasons = ['实际先上冲、回落，再放量连续修复并收复前段高点', '恢复速度超过剔除自身后的同行，同行同期改善']
    else:
        values = [_aligned(benchmark, at) for at, _ in sequence['anchors']]
        if any(value is None for value in values):
            return None, '指数与个股分钟时间无法对齐'
        activity = _activity(row, run, sequence)
        benchmark_packet = (run.get('strategyMarketEvidence') or {}).get('benchmark') or run.get('marketBenchmark') or {}
        previous = number(benchmark_packet.get('previousClose'))
        if activity is None or not previous or previous <= 0:
            return None, '缺少指数昨收或单分钟成交量额，无法核验弱市放量承接'
        # Two distinct downlegs are separated by a rebound; the same decline
        # cannot be counted twice through adjacent sample intervals.
        downlegs = values[1] < values[0] and values[2] > values[1] and values[3] < values[2]
        resistance = all((seq[i+1]/seq[i]-1) >= max(values[i+1]/values[i]-1,
                         (peers['returns'][i+1]-peers['returns'][i])/(100+peers['returns'][i])) for i in (0, 2))
        expansion = activity['rates'][3] > activity['rates'][0]
        matched = (values[-1] < previous and downlegs and resistance and seq[4] > seq[3]
                   and features['relativeReturnPct'] > 0 and expansion)
        features['benchmarkReturnPct'] = (values[-1] / values[0] - 1) * 100
        evidence.append(_evidence('指数同窗锚点', [{'sourceTime': at.isoformat(), 'price': price}
            for (at, _), price in zip(sequence['anchors'], values)], benchmark_packet.get('source'), sequence['anchors'][-1][0].isoformat()))
        evidence.append(_evidence('比较指数', {'code': benchmark_packet.get('code'), 'name': benchmark_packet.get('name')},
            benchmark_packet.get('source'), sequence['anchors'][-1][0].isoformat()))
        evidence.append(_evidence('四段每分钟成交' + ('额' if activity['field'] == 'amount' else '量'),
            activity['rates'], activity['source'], sequence['anchors'][-1][0].isoformat(), '元' if activity['field'] == 'amount' else '股'))
        reasons = ['指数偏弱且出现由反弹隔开的两次下探，个股相对指数和同行均抗跌', '末段放量回升，个股超过剔除自身后的同行平均表现']
    if not matched:
        return None, '当时序列未满足策略形态'
    features['score'] = round(features['relativeReturnPct'] + features['stockReturnPct'], 6)
    return {'features': {**base, **features}, 'reasons': reasons, 'evidence': evidence}, None


def _condition_matches(value, condition):
    observed, threshold = number(value), number(condition['value'])
    if observed is None:
        return False
    return {'gt': observed > threshold, 'gte': observed >= threshold,
            'lt': observed < threshold, 'lte': observed <= threshold, 'eq': observed == threshold}[condition['op']]


def build_strategy_publication(run, strategy_id='all', custom_rules=(), top_n=5):
    """Return a point-in-time selection; this function never writes history."""
    rules = [rule for rule in catalog_rules(custom_rules) if rule.get('enabled')]
    if strategy_id != 'all':
        rules = [rule for rule in rules if rule['id'] == strategy_id]
        if not rules:
            raise ValueError('所选策略不存在或尚未启用')
    now = timestamp(run.get('generatedAt'))
    if now is None:
        raise ValueError('扫描缺少带时区的 generatedAt')
    packet = run.get('strategyMarketEvidence') or {}
    paths = {code: _points(series, now) for code, series in (packet.get('stocks') or {}).items()}
    benchmark = _points(packet.get('benchmark') or run.get('marketBenchmark') or {}, now)
    evaluated, failures = {rule['id']: [] for rule in rules}, {rule['id']: Counter() for rule in rules}
    candidates = _rows(run)
    trend_items = {item['code']: item for item in select_potential_picks(run, None)}
    exclusions = [*list(_candidate_rows(run)), *(run.get('windvanes') or []), *(run.get('nextDayContinuationWatchlist') or []),
                  *((run.get('marketBrief') or {}).get('focusStocks') or [])]
    blocked = _blocked_codes(run, exclusions, now)
    needs_sequence = any(rule.get('baseStrategy', rule['id']) != 'trend_space' for rule in rules)
    for row in candidates:
        base = _base(row, now) if row['code'] not in blocked else None
        sequence, sequence_error = _sequence(row, run, paths, now) if base and needs_sequence else (None, None)
        cached = {}
        for rule in rules:
            base_id = rule.get('baseStrategy', rule['id'])
            if base_id == 'trend_space':
                original = trend_items.get(row['code'])
                if original is None:
                    failures[rule['id']]['原趋势空间规则未入选'] += 1
                    continue
                cached[base_id] = ({'features': {**(base or {}), 'score': 6-original['rank'],
                                               'rewardRisk': original.get('rewardRisk')},
                    'reasons': original['reasons'], 'evidence': [_evidence('趋势风险收益比', original.get('rewardRisk'),
                    original['source'], original['sourceTime'])]}, None)
            elif base is None:
                failures[rule['id']]['未触板报价或有效参考价未通过'] += 1
                continue
            if base_id not in cached:
                cached[base_id] = _evaluate(base_id, row, base, sequence, benchmark, now, run)
            result, reason = cached[base_id]
            if result is None:
                failures[rule['id']][sequence_error if base_id != 'trend_space' and sequence_error else reason] += 1
                continue
            if not all(_condition_matches(result['features'].get(test['field']), test) for test in rule.get('conditions', [])):
                failures[rule['id']]['自定义条件未满足或所需字段缺失'] += 1
                continue
            exclusions = rule.get('exclusions', [])
            if any(number(result['features'].get(test['field'])) is None or
                   _condition_matches(result['features'].get(test['field']), test) for test in exclusions):
                failures[rule['id']]['命中排除条件或排除条件所需字段缺失'] += 1
                continue
            sort_field = (rule.get('rankBy') or {}).get('field', 'score')
            value = number(result['features'].get(sort_field))
            if value is None:
                failures[rule['id']]['自定义排序字段缺失'] += 1
                continue
            evaluated[rule['id']].append((row, result, value))
    matches, channels, ranked = {}, [], []
    for rule in rules:
        values = evaluated[rule['id']]
        direction = 1 if (rule.get('rankBy') or {}).get('direction') == 'asc' else -1
        values.sort(key=lambda item: (direction * item[2], item[0]['code']))
        ranked.append([row['code'] for row, _, _ in values])
        for rank, (row, result, _) in enumerate(values, 1):
            match = {'id': rule['id'], 'name': rule['name'], 'version': rule['version'], 'rank': rank,
                     'score': result['features']['score'], 'reasons': result['reasons'],
                     'evidence': result['evidence']}
            if rule.get('conditions') or rule.get('exclusions'):
                match['evidence'] = [*match['evidence'], _evidence('声明式条件观察值',
                    {test['field']: result['features'].get(test['field'])
                     for test in rule.get('conditions', []) + rule.get('exclusions', [])},
                    'frozen-scan', run['generatedAt'])]
            matches.setdefault(row['code'], {'row': row, 'matches': []})['matches'].append(match)
        missing = failures[rule['id']]
        reason = missing.most_common(1)[0][0] if missing else '本轮没有可用候选'
        channels.append({'id': rule['id'], 'name': rule['name'], 'version': rule['version'],
                         'status': 'matched' if values else 'no_matches', 'count': len(values),
                         'reason': '' if values else reason, 'exclusions': dict(missing)})
    selected = []
    limit = max(0, min(5, int(top_n)))
    for depth in range(max((len(codes) for codes in ranked), default=0)):
        for codes in ranked:
            if depth < len(codes) and codes[depth] not in selected and len(selected) < limit:
                selected.append(codes[depth])
    items = []
    for rank, code in enumerate(selected, 1):
        row, matched = matches[code]['row'], matches[code]['matches']
        quote = row.get('quote') or {}
        original = trend_items.get(code)
        item = dict(original) if original else {
            'code': code, 'name': row.get('name') or code, 'price': quote.get('price'),
            'source': quote.get('source'), 'sourceTime': quote.get('provider_timestamp') or quote.get('source_time'),
            'basisLabel': {'post_close': '收盘价', 'lunch_break': '午间收盘价'}.get(market_phase(now), '扫描价'),
            'risk': '策略形态为当时行情观察，跌破修复低点或同行转弱时重新核验。公告、业绩或解禁覆盖待补。',
        }
        item.update(rank=rank, strategyMatches=matched, reasons=[reason for match in matched for reason in match['reasons']])
        items.append(item)
    return {'version': VERSION, 'strategyId': strategy_id,
            'strategyName': '全部策略' if strategy_id == 'all' else rules[0]['name'],
            'strategyVersion': VERSION if strategy_id == 'all' else rules[0]['version'],
            'sourceRunId': run.get('run_id'), 'sourceTime': run.get('generatedAt'),
            'publishedAt': run.get('generatedAt'), 'items': items, 'channels': channels,
            'ruleSnapshots': [_rule_snapshot(rule) for rule in rules],
            'selectionPolicy': '按策略顺序轮流取首选，再补下一名；同股合并全部命中通道，合计最多5只',
            'validationStatus': 'unvalidated_rules'}


class StrategyRuleStore:
    """Local definitions, with a preview bound to the exact imported document."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS strategy_rules (id TEXT PRIMARY KEY, definition TEXT NOT NULL, '
                       'fingerprint TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, preview TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS strategy_rule_versions (id TEXT NOT NULL, version TEXT NOT NULL, '
                       'definition TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(id,version))')
            for strategy_id, definition, fingerprint in db.execute('SELECT id, definition, fingerprint FROM strategy_rules').fetchall():
                version = json.loads(definition)['version']
                existing = db.execute('SELECT fingerprint FROM strategy_rule_versions WHERE id=? AND version=?',
                                      (strategy_id, version)).fetchone()
                if existing and existing[0] != fingerprint:
                    raise ValueError('当前策略与不可变版本登记冲突，请检查本地规则数据')
                db.execute('INSERT OR IGNORE INTO strategy_rule_versions VALUES(?,?,?,?)',
                           (strategy_id, version, definition, fingerprint))

    def list(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT definition, enabled, preview, fingerprint FROM strategy_rules ORDER BY id').fetchall()
        return [{**json.loads(definition), 'enabled': bool(enabled),
                 'preview': json.loads(preview) if preview else None, 'ruleFingerprint': fingerprint}
                for definition, enabled, preview, fingerprint in rows]

    def import_rule(self, document):
        rule = validate_rule(document)
        encoded = json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            registered = db.execute('SELECT fingerprint FROM strategy_rule_versions WHERE id=? AND version=?',
                                    (rule['id'], rule['version'])).fetchone()
            if registered and registered[0] != fingerprint:
                raise ValueError('规则内容变化时请更新 version；历史已登记版本不可覆盖')
            previous = db.execute('SELECT definition, fingerprint FROM strategy_rules WHERE id=?', (rule['id'],)).fetchone()
            if previous and previous[1] != fingerprint and json.loads(previous[0])['version'] == rule['version']:
                raise ValueError('规则内容变化时请更新 version；已存在的版本定义不可覆盖')
            if previous and previous[1] == fingerprint:
                return next(row for row in self.list() if row['id'] == rule['id'])
            db.execute('INSERT OR IGNORE INTO strategy_rule_versions VALUES(?,?,?,?)',
                       (rule['id'], rule['version'], encoded, fingerprint))
            db.execute('INSERT INTO strategy_rules(id,definition,fingerprint) VALUES(?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,fingerprint=excluded.fingerprint,enabled=0,preview=NULL',
                       (rule['id'], encoded, fingerprint))
        return {**rule, 'enabled': False, 'preview': None, 'ruleFingerprint': fingerprint}

    def preview(self, run, strategy_id):
        rule = next((row for row in self.list() if row['id'] == strategy_id), None)
        if rule is None:
            raise ValueError('自定义策略不存在')
        result = build_strategy_publication(run, strategy_id, [{**rule, 'enabled': True}])
        encoded = json.dumps(_definition(rule), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        receipt = {'runId': run.get('run_id'), 'ruleVersion': rule['version'], 'fingerprint': fingerprint,
                   'ruleFingerprint': fingerprint,
                   'validatedAt': datetime.now().astimezone().isoformat(), 'matchCount': len(result['items'])}
        with closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute('UPDATE strategy_rules SET preview=? WHERE id=? AND fingerprint=?',
                                (json.dumps(receipt), strategy_id, fingerprint))
            if cursor.rowcount != 1:
                raise ValueError('试跑期间规则版本发生变化，请重新试跑')
        return {**result, 'preview': True, 'status': 'previewed', 'previewReceipt': receipt}

    def set_enabled(self, strategy_id, enabled, *, expected_version=None, expected_fingerprint=None):
        if not isinstance(enabled, bool):
            raise ValueError('enabled 必须是布尔值')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT preview, fingerprint, definition FROM strategy_rules WHERE id=?', (strategy_id,)).fetchone()
            if row is None:
                raise ValueError('自定义策略不存在')
            version = json.loads(row[2])['version']
            if ((expected_version is not None and expected_version != version)
                    or (expected_fingerprint is not None and expected_fingerprint != row[1])):
                raise ValueError('策略定义已经变化，请重新查看该版本试跑结果后启用')
            receipt = json.loads(row[0]) if row[0] else {}
            if enabled and (receipt.get('fingerprint') != row[1]
                            or receipt.get('ruleVersion') != version):
                raise ValueError('启用前需先对归档扫描试跑并查看结果')
            db.execute('UPDATE strategy_rules SET enabled=? WHERE id=?', (int(enabled), strategy_id))
        return next(row for row in self.list() if row['id'] == strategy_id)
