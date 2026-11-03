"""Inspectably fixed flow/trend research policy; no fitting, I/O or trade claims.

The numerical cutoffs below are explicit, uncalibrated research constraints.
They must be compared prospectively, not interpreted as learned win rates.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
from statistics import median


VERSION = 'opportunity-flow-20261103-v1'
PARAMETERS = {
    'windowMinutes': 30,
    'auxiliaryWindowMinutes': 60,
    'recentFlowSubwindowMinutes': 5,
    'minimumStockPositiveMinuteFraction': .5,
    'minimumSectorPositiveMemberFraction': .5,
    'minimumSignedPathEfficiency': .2,
    'maximumPullbackToRise': .5,
    'maximumCloseDrawdownAtr': .75,
    'maximumEntryRiskAtr': 1.5,
    'maximumSpreadPct': .3,
    'maximumRecommendations': 5,
    'maximumPerSector': 2,
    'minimumSectorPeers': 3,
    'minimumSectorCoverage': .8,
    'quoteMaximumAgeSeconds': 30,
    'minuteMaximumAgeSeconds': 299,
    'dailyCloseAlignmentToleranceCny': .011,
}
DIMENSIONS = {
    'sectorNetFlowRatio': {'weight': .2, 'direction': 'higher', 'peerUnit': 'unique_sector'},
    'stockNetFlowRatio': {'weight': .2, 'direction': 'higher', 'peerUnit': 'qualified_stock'},
    'positiveMinuteFraction': {'weight': .2, 'direction': 'higher', 'peerUnit': 'qualified_stock'},
    'pathEfficiency30m': {'weight': .2, 'direction': 'higher', 'peerUnit': 'qualified_stock'},
    'entryRiskAtr': {'weight': .2, 'direction': 'lower', 'peerUnit': 'qualified_stock'},
}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def stamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def policy_contract():
    return {
        'version': VERSION, 'validationStatus': 'prospective_rules_unvalidated',
        'channels': {'F': '同窗板块及个股资金支持的持续趋势或正常整理'},
        'selectionModes': ['flow_trend', 'flow_pullback', 'observation'],
        'parameters': deepcopy(PARAMETERS),
        'thresholdSource': 'explicit_uncalibrated_research_constraints_not_fitted_or_profit_validated',
        'required': ['verified_completed_30m_path', 'same_provider_version_stock_and_sector_flow',
                     'positive_stock_and_sector_net_flow',
                     'aligned_previous_session_unadjusted_atr14', 'fresh_normal_order_book'],
        'notRequired': ['positive_1m_3m_5m_returns', 'positive_recent_5m_net_flow', 'price_acceleration', 'trained_model', 'complete_60m_window'],
        'missingPolicy': 'missing_required_evidence_means_observation_never_fabricated_qualification',
        'hardVetoes': ['stale_or_conflicting_quote', 'tainted_current_market_data', 'no_buy_window',
                       'verified_major_adverse_event_or_restriction'],
        'windowPolicy': 'fixed_30m_same_session; 60m_descriptive_only; never_select_best_window',
        'flowMeaning': '供应商大单分类估算净额；不是可识别机构账户资金，也不是价格涨跌标记成交额代理',
        'ranking': 'equal_weight_midrank_percentiles_of_five_declared_dimensions',
        'rankingDimensions': deepcopy(DIMENSIONS),
        'sectorRankingAggregation': 'median_of_qualified_members_leave_self_out_sector_ratios; one_rank_value_per_sector',
        'recentFlowPolicy': 'latest_5m_and_preceding_5m_diagnostic_only_no_veto_or_ranking_authority',
        'percentileFormula': '(count_strictly_worse + 0.5 * count_equal) / peer_count; singleton=0.5',
        'rankingTieBreak': 'security_code_ascending_only; no_price_speed_or_absolute_money_tiebreak',
        'rankScoreMeaning': '规则顺序；旧rankScore不再生成，rankingComposite为0至1等权中位名次，不是质量、胜率或收益预测',
        'maximumRecommendations': PARAMETERS['maximumRecommendations'],
        'diversification': {'maximumPerSector': PARAMETERS['maximumPerSector'], 'fillQuota': False},
        'probability': None, 'formalRecommendationAllowed': False,
        'trainingEligible': False, 'executionVerified': False,
        'entryRiskMeaning': '卖一价至完整30分钟最低价的ATR距离；不是保证止损价，T+1及跳空可能扩大损失',
        'meaning': '固定30分钟资金、持续性和入场位置研究规则；尚未证明未来收益改善。',
    }


def _flow_valid(packet, code, cutoff, *, sector=False):
    if not isinstance(packet, dict) or cutoff is None:
        return False
    if (packet.get('status') != 'observed' or str(packet.get('code')) != code
            or stamp(packet.get('asOf')) != cutoff
            or stamp(packet.get('windowStart')) != cutoff - timedelta(minutes=PARAMETERS['windowMinutes'])
            or packet.get('horizonMinutes') != PARAMETERS['windowMinutes']
            or not packet.get('source') or not packet.get('provider') or not packet.get('measurementVersion')):
        return False
    if sector:
        coverage = packet.get('coverage') or {}
        total, usable = number(coverage.get('totalPeers')), number(coverage.get('usablePeers'))
        fraction = number(coverage.get('fraction'))
        if (not (packet.get('sector') or {}).get('code') or coverage.get('excludedSelf') is not True
                or total is None or usable is None or total <= 0 or usable > total
                or usable < PARAMETERS['minimumSectorPeers'] or fraction is None
                or fraction < PARAMETERS['minimumSectorCoverage']
                or abs(fraction - usable / total) > 1e-8):
            return False
    return True


def assess_flow_policy(row, minute, daily, quote, buy, trend, cutoff, now, stock_flow, sector_flow):
    """Assess observed inputs. Missing and observed counter-evidence stay distinct."""
    missing, counter, support, failures = [], [], [], []
    code = str(row.get('code') or '')
    minute = minute or {}
    valid_stock = _flow_valid(stock_flow, code, cutoff)
    valid_sector = _flow_valid(sector_flow, code, cutoff, sector=True)
    if not valid_stock:
        missing.append('完整同窗30分钟个股资金证据不可用，不能用涨速、外内盘或催化替代')
        failures.append('stock_flow_unavailable')
    if not valid_sector:
        missing.append('剔除自身后、同窗同来源且足够覆盖的板块资金证据不可用')
        failures.append('sector_flow_unavailable')
    same_measurement = bool(valid_stock and valid_sector and all(
        stock_flow.get(key) == sector_flow.get(key) for key in ('provider', 'measurementVersion', 'source')))
    if valid_stock and valid_sector and not same_measurement:
        missing.append('个股与板块资金供应商或计量版本不一致，不能拼接确认')
        failures.append('flow_measurement_mismatch')
    stock = stock_flow.get('metrics', {}) if valid_stock else {}
    sector = sector_flow.get('metrics', {}) if valid_sector else {}
    stock_ratio, sector_ratio = number(stock.get('netFlowRatio')), number(sector.get('netFlowRatio'))
    positive, member_positive = number(stock.get('positiveMinuteFraction')), number(sector.get('positiveMemberFraction'))
    for label, metrics, ratio in (('个股', stock, stock_ratio), ('板块', sector, sector_ratio)):
        net, amount = number(metrics.get('netFlowCny')), number(metrics.get('turnoverCny'))
        if net is None or amount is None or amount <= 0 or ratio is None or not -1 <= ratio <= 1 or abs(ratio-net/amount) > 1e-8:
            failures.append(label + '_flow_metrics_unverified')
            missing.append(label + '资金净额、成交额或净流入比例不完整/不一致')
        elif net <= 0:
            failures.append(label + '_net_flow_not_positive')
            counter.append(label + '30分钟估算净额不为正')
        else:
            support.append(label + '30分钟估算净额为正，按同期成交额归一化')
    for label, value, minimum in (
        ('个股资金正向分钟占比', positive, PARAMETERS['minimumStockPositiveMinuteFraction']),
        ('同行资金正向成员占比', member_positive, PARAMETERS['minimumSectorPositiveMemberFraction']),
    ):
        if value is None or not 0 <= value <= 1:
            missing.append(label + '未知')
            failures.append(label + '_unavailable')
        elif value < minimum:
            counter.append(label + '未达到公开研究约束')
            failures.append(label + '_below_constraint')
    recent = number(stock.get('recentNetFlowCny'))
    if (recent is None or not isinstance(stock.get('recentOutflow'), bool)
            or stock.get('recentSubwindowMinutes') != PARAMETERS['recentFlowSubwindowMinutes']):
        missing.append('个股最近5分钟资金状态未知')
    elif recent < 0 or stock.get('recentOutflow'):
        counter.append('个股最近5分钟已净流出，仅作短期减弱提示；不单独否决30分钟资金持续性')
    if stock.get('fading') is True:
        counter.append('最近5分钟净流入弱于紧前5分钟，继续记录资金减弱')

    lineage = minute.get('dataLineage') or row.get('dataLineage') or {}
    sessions = [part for part in lineage.get('sessions') or []
                if str(part.get('code')) == code and part.get('day') == now.date().isoformat()]
    verified_current = lineage.get('status') == 'verified' or bool(sessions and cutoff and all(
        part.get('status') == 'verified' and stamp(part.get('verifiedThrough'))
        and stamp(part['verifiedThrough']) >= cutoff for part in sessions))
    if not verified_current:
        missing.append('当日分钟身份与价格路径尚未独立核验')
        failures.append('current_minute_lineage_unverified')
    efficiency = number(trend.get('signed_log_efficiency_30m'))
    net_return = number(trend.get('net_return_30m_pct'))
    drawdown = number(trend.get('close_max_drawdown_30m_pct'))
    drawdown_cny = number(trend.get('close_max_drawdown_30m_cny'))
    pullback = number(trend.get('close_pullback_to_rise_30m'))
    structural_low = number(trend.get('window_low_30m'))
    if (cutoff is None or any(value is None for value in (efficiency, net_return, drawdown, drawdown_cny, pullback, structural_low))
            or not -1 <= (efficiency or 0) <= 1 or (drawdown is not None and not 0 <= drawdown < 100)
            or (drawdown_cny is not None and drawdown_cny < 0)
            or (pullback is not None and pullback < 0) or (structural_low is not None and structural_low <= 0)):
        missing.append('完整连续30分钟推进、回撤或结构低点不足；早盘/午后不足窗口仅观察')
        failures.append('thirty_minute_path_unavailable')
    else:
        if net_return <= 0 or efficiency < PARAMETERS['minimumSignedPathEfficiency']:
            counter.append('固定30分钟尚无足够持续推进，单分钟脉冲不能替代')
            failures.append('persistent_trend_not_confirmed')
        if pullback > PARAMETERS['maximumPullbackToRise']:
            counter.append('固定30分钟上涨回吐超过公开研究约束')
            failures.append('excessive_giveback')

    daily = daily or {}
    dm, previous = daily.get('metrics') or {}, daily.get('previousSession') or {}
    atr, last_close, quote_close = number(dm.get('atr14')), number(dm.get('last_close')), number(quote.get('pre_close'))
    from .premarket import previous_session
    expected_previous = previous_session(now).isoformat()
    daily_valid = bool(daily.get('source') and daily.get('asOf') == expected_previous
        and previous.get('asOf') == expected_previous and previous.get('priceBasis') == 'unadjusted'
        and atr and atr > 0 and last_close and last_close > 0 and quote_close and quote_close > 0
        and abs(last_close - quote_close) <= PARAMETERS['dailyCloseAlignmentToleranceCny'])
    for key in ('observedAt', 'availableAt'):
        if daily.get(key) is not None and (stamp(daily[key]) is None or stamp(daily[key]) > now):
            daily_valid = False
    if not daily_valid:
        missing.append('最近已完成交易日日线ATR14或未复权昨收对齐证据不足')
        failures.append('daily_atr_unverified')
    price, ask, spread = number(quote.get('price')), number(buy.get('askPrice')), number(buy.get('spreadPct'))
    entry_risk = (ask - structural_low) / atr if daily_valid and ask and structural_low else None
    drawdown_atr = drawdown_cny / atr if daily_valid and drawdown_cny is not None else None
    if structural_low and price and price <= structural_low:
        counter.append('现价已到达或跌破完整30分钟结构低点')
        failures.append('long_window_structure_broken')
    if entry_risk is None or entry_risk <= 0:
        missing.append('卖一价至30分钟结构低点的风险距离不可核验')
        failures.append('entry_risk_unknown')
    elif entry_risk > PARAMETERS['maximumEntryRiskAtr']:
        counter.append('当前入场离30分钟结构低点过远，超过1.5 ATR研究约束')
        failures.append('entry_overextended')
    if drawdown_atr is not None and drawdown_atr > PARAMETERS['maximumCloseDrawdownAtr']:
        counter.append('30分钟收盘路径最大回撤超过0.75 ATR研究约束')
        failures.append('excessive_close_drawdown')
    if buy.get('status') != 'observed' or spread is None:
        missing.append('正常买卖盘口或点差未知')
        failures.append('book_spread_unverified')
    elif spread > PARAMETERS['maximumSpreadPct'] or spread < 0:
        counter.append('盘口点差超过0.3%研究约束或为负')
        failures.append('spread_outside_constraint')
    if not failures:
        support.append('30分钟资金与趋势通过公开约束；允许短暂回落，不要求加速')
    return {
        'qualified': not failures, 'selectionMode': ('flow_pullback' if pullback and pullback > 0 else 'flow_trend') if not failures else 'observation',
        'missing': list(dict.fromkeys(missing)), 'counterEvidence': counter, 'support': support,
        'failedConditions': list(dict.fromkeys(failures)),
        'sectorKey': str((sector_flow.get('sector') or {}).get('code') or '') if valid_sector else None,
        'sector': deepcopy(sector_flow.get('sector')) if valid_sector else None,
        'rankingValues': {'sectorNetFlowRatio': sector_ratio, 'stockNetFlowRatio': stock_ratio,
                          'positiveMinuteFraction': positive, 'pathEfficiency30m': efficiency, 'entryRiskAtr': entry_risk},
        'metrics': {'netReturn30mPct': net_return, 'pathEfficiency30m': efficiency, 'pullbackToRise30m': pullback,
                    'closeDrawdown30mPct': drawdown, 'closeDrawdownAtr': drawdown_atr, 'entryRiskAtr': entry_risk,
                    'structuralLow30m': structural_low, 'atr14': atr if daily_valid else None,
                    'pathEfficiency60m': number(trend.get('signed_log_efficiency_60m')),
                    'auxiliary60mSelectionAuthority': False},
    }


def rank_qualified(rows):
    """Equal-weight empirical midranks; a lone eligible stock receives 0.5, never 100."""
    for key, definition in DIMENSIONS.items():
        if definition['peerUnit'] == 'unique_sector':
            sectors = {}
            for row in rows:
                opportunity = row['opportunity']
                sectors.setdefault(opportunity['sectorKey'], []).append(opportunity['rankingValues'][key])
            sectors = {sector: median(values) for sector, values in sectors.items()}
            peers = list(sectors.values())
        else:
            peers = [row['opportunity']['rankingValues'][key] for row in rows]
        for row in rows:
            opportunity = row['opportunity']
            raw = opportunity['rankingValues'][key]
            rank_value = sectors[opportunity['sectorKey']] if definition['peerUnit'] == 'unique_sector' else raw
            worse = sum(value < rank_value if definition['direction'] == 'higher' else value > rank_value for value in peers)
            equal = sum(value == rank_value for value in peers)
            percentile = (worse + .5 * equal) / len(peers)
            opportunity.setdefault('rankingComponents', {})[key] = {
                **definition, 'rawValue': raw, 'rankValue': rank_value, 'percentile': percentile, 'peerCount': len(peers)}
    for row in rows:
        opportunity = row['opportunity']
        opportunity['rankingComposite'] = sum(part['weight'] * part['percentile'] for part in opportunity['rankingComponents'].values())
    return sorted(rows, key=lambda row: (-row['opportunity']['rankingComposite'], str(row['code'])))
