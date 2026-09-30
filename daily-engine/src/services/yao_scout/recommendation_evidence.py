"""Local, inspectable evidence rules. These rules are not a calibrated return model.

No gain filter, weighted heat score, assumed fund inflow, or fabricated target.
The shortlist is a bounded research queue, not a claim to have studied all stocks.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

from .daily_opportunities import finite, quote_check, is_market_open

VERSION = 'king-evidence-20260920'


def research_queue_limit(universe_size: int) -> int:
    """Research at least 300 unique stocks, or 10% of a larger universe."""
    if universe_size < 0:
        raise ValueError('universe_size must not be negative')
    return min(universe_size, max(300, (universe_size + 9) // 10))


def research_queue(frame, limit=None):
    """Prioritize activity/recovery leaders within a universe-sized research budget."""
    if limit is not None and limit < 0:
        raise ValueError('research limit must not be negative')
    if frame.empty:
        return []
    # Repeated provider rows must not crowd out another stock or inflate the budget.
    data = frame.drop_duplicates('code').copy()
    if limit is None:
        limit = research_queue_limit(len(data))
    if limit == 0:
        return []
    ranks = []
    for key in ('amount', 'volume_ratio', 'turnover_rate'):
        values = pd.to_numeric(data.get(key, pd.Series(index=data.index, dtype=float)), errors='coerce')
        ranks.append(values.where(values > 0).rank(ascending=False, method='min'))
    if {'price', 'open'}.issubset(data):
        recovery = pd.to_numeric(data.price, errors='coerce') / pd.to_numeric(data.open, errors='coerce') - 1
        ranks.append(recovery.where(recovery > 0).rank(ascending=False, method='min'))
    data['_research_rank'] = pd.concat(ranks, axis=1).min(axis=1)
    # Missing activity evidence stays missing; code order only resolves equal queue ranks.
    data = data.sort_values(['_research_rank', 'code'], na_position='last')
    return data.head(limit).drop(columns=['_research_rank']).to_dict('records')


def daily_evidence(frame, cutoff):
    result = {'metrics': {}, 'source': None, 'asOf': None, 'gaps': []}
    if frame is None or frame.empty or not {'date', 'close', 'high', 'low'}.issubset(frame):
        result['gaps'].append('未取得20—60日历史，股性、支撑和压力未核验')
        return result
    result['source'] = frame.attrs.get('daily_source') or '历史行情（来源未记录）'
    data = frame.copy()
    data['date'] = pd.to_datetime(data.date, errors='coerce', utc=True).dt.tz_convert('Asia/Shanghai').dt.date
    data = data[data.date < cutoff.date()].sort_values('date')
    if data.date.duplicated().any():
        result['gaps'].append('历史交易日重复，未用于结构判断')
        return result
    data = data.tail(60)
    if len(data) < 20:
        result['gaps'].append(f'仅{len(data)}个已完成交易日，20日结构不足')
        return result
    previous_session = cutoff.date() - timedelta(days=1)
    while not is_market_open('cn', previous_session):
        previous_session -= timedelta(days=1)
    if data.date.iloc[-1] != previous_session or frame.attrs.get('daily_stale'):
        result['gaps'].append('历史行情陈旧，未用于当前结构判断')
        return result
    for key in ('close', 'high', 'low'):
        data[key] = pd.to_numeric(data[key], errors='coerce')
    if data[['close', 'high', 'low']].isna().any().any() or (data[['close', 'high', 'low']] <= 0).any().any():
        result['gaps'].append('历史价格缺失或无效，未用于结构判断')
        return result
    if ((data.low > data.close) | (data.close > data.high)).any():
        result['gaps'].append('历史高低价不一致，未用于结构判断')
        return result
    close = data.close
    tr = pd.concat([data.high-data.low, (data.high-close.shift()).abs(), (data.low-close.shift()).abs()], axis=1).max(axis=1)
    result['asOf'] = data.date.iloc[-1].isoformat()
    m = result['metrics']
    m.update(history_sessions=len(data), last_close=float(close.iloc[-1]),
             ma5=float(close.tail(5).mean()), ma10=float(close.tail(10).mean()), ma20=float(close.tail(20).mean()),
             high20=float(data.high.tail(20).max()), high60=float(data.high.max()) if len(data) == 60 else None,
             low20=float(data.low.tail(20).min()), atr14=float(tr.tail(14).mean()),
             volatility20_pct=float(close.pct_change().tail(20).std() * math.sqrt(252) * 100),
             drawdown20_pct=float((close.tail(20)/close.tail(20).cummax()-1).min()*100))
    for days in (5, 20):
        m[f'return{days}_pct'] = float((close.iloc[-1]/close.iloc[-days-1]-1)*100) if len(close)>days else None
    return result


def explain_candidate(item, quote, daily, intraday, now, slot, snapshot_meta=None, *, sector=None, funds=None, v11=False):
    snapshot_meta = snapshot_meta or {}
    evidence, reasons, risks = [], [], []
    source = quote.get('source')
    stamp = quote.get('provider_timestamp') or quote.get('source_time')
    d = (daily or {}).get('metrics') or {}
    m = (intraday or {}).get('metrics') or {}
    sector, funds = sector or {}, funds or {}
    sm, fm = dict(sector.get('metrics') or {}), dict(funds.get('metrics') or {})
    independent_gaps = list(sector.get('gaps') or []) + list(funds.get('gaps') or [])
    for data, metrics, label in ((sector,sm,'板块'),(funds,fm,'资金')):
        try:
            at = datetime.fromisoformat(data.get('asOf') or '')
            if at.tzinfo is None or not 0 <= (now-at).total_seconds() < 300:
                raise ValueError()
        except (ValueError,TypeError):
            metrics.clear()
            independent_gaps.append(label+'源时间缺失、超前或过期')
        if data.get('asOf') != (intraday or {}).get('asOf'):
            metrics.clear()
            independent_gaps.append(label+'分钟与个股计算窗口未对齐')
    minute_gaps = list((intraday or {}).get('gaps') or [])
    try:
        minute_at = datetime.fromisoformat((intraday or {}).get('asOf') or '')
        if minute_at.tzinfo is None or not 0 <= (now-minute_at).total_seconds() < 300:
            raise ValueError()
    except (ValueError, TypeError):
        m = {}
        minute_gaps.append('决策时分钟源时间未知、超前或已陈旧，分钟信号不参与入选')
    p, prev, op, high, low, amount, volume = [finite(quote.get(k)) for k in
        ('price', 'pre_close', 'open_price', 'high', 'low', 'amount', 'volume')]

    def add(key, label, value, unit='', threshold='', role='观察', origin=None, at=None):
        value = finite(value) if value is not None else None
        evidence.append({'key': key, 'label': label, 'value': round(value, 6) if value is not None else None,
            'unit': unit, 'source': origin if origin is not None else source,
            'asOf': at if at is not None else stamp, 'threshold': threshold,
            'status': 'observed' if value is not None else 'missing', 'role': role})

    add('price', '推荐参考价', p, '元', '源时间距决策≤30秒', '价格基准')
    add('change_pct', '当日涨跌幅', quote.get('change_pct'), '%', '不设涨幅入场门槛')
    add('amount', '当日累计成交额', amount, '元', '累计成交不等于主力净流入', '流动性')
    vwap = amount/volume if amount and volume and amount > 0 and volume > 0 else None
    invalid_cumulative = False
    if vwap and (not low or not high or not low-.01 <= vwap <= high+.01):
        vwap = None
        invalid_cumulative = True
        risks.append('成交额/成交股数的VWAP与日内价格范围不一致，未使用')
    add('vwap', '最新累计VWAP', vwap, '元', '成交额÷成交股数；用于承接参考', '结构')
    for key, label in [('turnover_rate', '快照换手率'), ('volume_ratio', '快照量比')]:
        add(key, label, item.get(key), '%' if key == 'turnover_rate' else '倍', '初筛线索；不代替3分钟即时放量',
            '初筛', snapshot_meta.get('snapshot_source') or '全市场初筛快照', snapshot_meta.get('source_time') or '源时间未记录')
    for key, label, unit in [('speed_1m_pct','1分钟涨速','%'), ('speed_3m_pct','3分钟涨速','%'),
                            ('speed_5m_pct','5分钟涨速','%'), ('amount_3m','近3分钟成交额','元'),
                            ('relative_amount_3m_20d','3分钟成交额/20日同刻中位数','倍'),
                            ('local_high_5m','此前5分钟局部高点','元'), ('local_low_5m','此前5分钟局部低点','元')]:
        definition = ((intraday or {}).get('metricDefinitions') or {}).get(key,'连续分钟证据')
        add(key, label, m.get(key), unit, definition+'；涨速>0、放量倍数>1为方向性条件，未校准为胜率', '即时信号',
            (intraday or {}).get('source') or '分钟行情未取得', (intraday or {}).get('asOf') or '源时间未取得')
    sector_name = (sector.get('sector') or {}).get('name') or '行业待核实'
    for key,label,unit,rule in [
        ('sector_return_5m_pct','行业同刻5分钟涨幅','%','剔除自身后成分股等权平均；须>0'),
        ('sector_relative_5m_pct','5分钟相对行业强度','百分点','个股涨速减行业平均；须>0'),
        ('sector_breadth','同刻行业上涨比例','%','剔除自身后同刻上涨成分占比；须≥50%'),
        ('sector_coverage_pct','同刻成分覆盖率','%','至少3只同业且覆盖≥80%；数据质量门槛'),
        ('sector_peer_count','同刻有效同业数','只','对全部成分取数，不只抽取上涨股'),
        ('sector_rank_5m','行业5分钟涨速名次','名','在有效成分与个股中比较；不等于收益预测')]:
        add(key,label,sm.get(key),unit,rule,'板块联动',sector_name+'；'+(sector.get('source') or '未取得'),sector.get('asOf') or '未取得')
    for key,label in [('main_net_flow','当日主力净额（供应商估算）'),('main_net_flow_3m','近3分钟主力净额（供应商估算）')]:
        add(key,label,fm.get(key),'元','大单+超大单净额；非机构账户流入；近3分钟须>0','资金方向',funds.get('source') or '未取得',funds.get('asOf') or '未取得')
    add('active_buy_share_pct','主动买入量占比（外内盘口径）',quote.get('active_buy_share_pct'),'%',
        '供应商外盘÷(外盘+内盘)；累计成交股数分类；结合近3分钟资金变化判断修复', '主动性',quote.get('active_flow_source') or '未取得',quote.get('active_flow_as_of') or '未取得')
    add('active_volume_imbalance_pct','主动买卖量差占比（外内盘口径）',quote.get('active_volume_imbalance_pct'),'%',
        '(外盘-内盘)÷(外盘+内盘)；不是账户资金净流入','主动性',quote.get('active_flow_source') or '未取得',quote.get('active_flow_as_of') or '未取得')
    add('baseline_history_days','同刻基准有效交易日数',(intraday or {}).get('historyDays'), '日',
        '严格20/20；缺失不以全天量比替代','数据完整性',(intraday or {}).get('baselineSource') or '历史分钟缓存',(intraday or {}).get('asOf') or '未取得')
    add('baseline_median_amount','20日同刻3分钟成交额中位数',(intraday or {}).get('baselineMedianAmount'),'元',
        '不含当日；同一3分钟区间','放量基准',(intraday or {}).get('baselineSource') or '未取得',(intraday or {}).get('asOf') or '未取得')
    for key, label, unit in [('ma5','MA5','元'), ('ma10','MA10','元'), ('ma20','MA20','元'),
                             ('high20','20日压力参考','元'), ('high60','60日压力参考','元'), ('low20','20日低点','元'),
                             ('atr14','ATR14','元'), ('volatility20_pct','20日年化波动率','%'),
                             ('drawdown20_pct','20日收盘最大回撤','%'), ('return5_pct','此前5日涨跌幅','%'),
                             ('return20_pct','此前20日涨跌幅','%')]:
        add(key, label, d.get(key), unit, '仅已完成交易日；压力位不等于预测目标', '历史结构',
            (daily or {}).get('source') or '日线未取得', (daily or {}).get('asOf') or '未取得')
    quote_gaps = quote_check(quote, item['code'], now)
    valid_quote = not quote_gaps
    # Premarket may lack auction OHLC, but never relax identity, provider time,
    # source or basic price integrity merely because this is a watchlist.
    premarket_quote_ok = bool(p and p > 0 and prev and prev > 0
        and not [gap for gap in quote_gaps if gap != '必要价格字段不足']
        and all(value is None or value >= 0 for value in (op, high, low))
        and (not high or not low or low <= p <= high))
    aligned = bool(prev and d.get('last_close') and abs(prev-d['last_close']) <= .011)
    if d and not aligned:
        risks.append('日线末收盘与实时前收盘未对齐，复权或缺日未核实，历史价位不用于买点')
    support = None
    resistance = None
    if p and aligned:
        levels = [finite(d.get(k)) for k in ('ma20', 'low20')]
        support = max((x for x in levels if x and x < p), default=None)
        resistance = min((x for x in [d.get('high20'), d.get('high60')] if x and x > p), default=None)
    if p and finite(m.get('local_low_5m')) and 0 < m['local_low_5m'] < p:
        support = m['local_low_5m']
    space = (resistance/p-1)*100 if resistance and p else None
    risk = (1-support/p)*100 if support and p else None
    rr = space/risk if space is not None and risk and risk > 0 else None
    derived_source = f'日线:{(daily or {}).get("source") or "未取得"}；分钟:{(intraday or {}).get("source") or "未取得"}；报价:{source or "未取得"}'
    derived_at = f'日线截至{(daily or {}).get("asOf") or "未知"}；分钟{(intraday or {}).get("asOf") or "未知"}；报价{stamp or "未知"}'
    add('support', '失效参考支撑', support, '元', '跌破后原结构失效，不保证可按此价卖出', '风险', derived_source, derived_at)
    add('resistance', '上方历史压力', resistance, '元', '尚未突破前不假设越过', '空间', derived_source, derived_at)
    add('remaining_space_pct', '距历史压力空间', space, '%', '不是预期收益或涨停空间', '空间', derived_source, derived_at)
    add('structure_reward_risk', '压力空间/支撑距离', rr, '倍', '只展示，不设统一入选门槛，不按此单项排序', '空间', derived_source, derived_at)
    amount_ok = bool(amount and amount > 0 and volume and volume > 0 and not invalid_cumulative)
    above_vwap = bool(p and vwap and p > vwap)
    repair = bool(p and op and p > op and above_vwap)
    historical_trend = bool(aligned and d['ma5'] > d['ma10'] > d['ma20'])
    trend = bool(historical_trend and p and p > d['ma20'])
    minute_push = bool((finite(m.get('speed_3m_pct')) or 0) > 0 and (finite(m.get('speed_5m_pct')) or 0) > 0)
    minute_breakout = bool(minute_push and p and finite(m.get('local_high_5m')) and p > m['local_high_5m'])
    immediate = bool(minute_push and (above_vwap or minute_breakout))
    sector_ok = bool((finite(sm.get('sector_coverage_pct')) or 0)>=80 and (finite(sm.get('sector_peer_count')) or 0)>=3
        and (finite(sm.get('sector_return_5m_pct')) or 0)>0 and (finite(sm.get('sector_breadth')) or 0)>=50
        and (finite(sm.get('sector_relative_5m_pct')) or 0)>0)
    baseline_ok = bool((intraday or {}).get('historyDays') == 20 and finite(m.get('relative_amount_3m_20d')) is not None)
    volume_confirmed = baseline_ok and m['relative_amount_3m_20d']>1
    active_share = finite(quote.get('active_buy_share_pct'))
    try:
        active_at = datetime.fromisoformat(quote.get('active_flow_as_of') or '')
        active_valid = bool(quote.get('active_flow_source') and active_at.tzinfo and 0 <= (now-active_at).total_seconds() <= 30
                            and active_share is not None and 0 <= active_share <= 100)
    except (ValueError,TypeError):
        active_valid = False
    funds_ok = bool(active_valid and (finite(fm.get('main_net_flow_3m')) or 0)>0)
    premarket = slot == '0920'
    branch = ('盘前趋势条件观察' if premarket else '分钟局部突破观察' if minute_breakout else
              '分歧修复观察' if immediate and (quote.get('change_pct') or 0) <= 0 else
              '分钟趋势延续观察' if immediate else '历史趋势·等待分钟触发')
    if amount_ok:
        reasons.append(f'实际累计成交{amount/1e8:.2f}亿元；成交总额不代替资金方向')
    if sector_ok:
        reasons.append(f'{sector_name}同刻5分钟上涨{sm["sector_return_5m_pct"]:.2f}%，上涨同业占{sm["sector_breadth"]:.1f}%；个股领先行业{sm["sector_relative_5m_pct"]:.2f}个百分点（覆盖{sector.get("peerCount",sm["sector_peer_count"])}/{sector.get("totalPeers","未知")}只）')
    if baseline_ok:
        reasons.append(f'近3分钟成交额为前20个交易日同刻中位数的{m["relative_amount_3m_20d"]:.2f}倍，基准20/20日齐全')
    if funds_ok:
        direction = '累计买方占优' if active_share>50 else '累计卖压仍在，按近期资金转向修复观察'
        reasons.append(f'外内盘口径主动买入量占{active_share:.1f}%，近3分钟供应商主力净额{fm["main_net_flow_3m"]/1e4:.2f}万元；{direction}，均为行情分类估算')
        if active_share<=50:
            risks.append('累计外内盘未显示买方占优，近期净流入能否持续需要复核')
    if above_vwap:
        reasons.append(f'参考价{p:.2f}元高于最新累计VWAP {vwap:.2f}元，当前位于日内成交成本上方')
    if repair:
        reasons.append(f'较开盘价{op:.2f}元修复{(p/op-1)*100:.2f}%，低开或未翻红不排除观察')
    if trend or (premarket and historical_trend):
        reasons.append(f'已完成日线MA5 {d["ma5"]:.2f} > MA10 {d["ma10"]:.2f} > MA20 {d["ma20"]:.2f}，趋势结构向上')
    if minute_push:
        reasons.append(f'近3/5分钟涨速分别{m["speed_3m_pct"]:.2f}%/{m["speed_5m_pct"]:.2f}%，具备即时回升线索')
    if minute_breakout:
        reasons.append(f'最新价{p:.2f}元越过此前5分钟高点{m["local_high_5m"]:.2f}元，局部突破已有价格证据')
    if minute_push and finite(m.get('amount_3m')) and m['amount_3m'] > 0:
        reasons.append(f'近3分钟实际成交额{m["amount_3m"]/1e4:.2f}万元；同刻20日基准是否放量另行标注')
    if p and aligned and d.get('high20') and p > d['high20'] and resistance is None:
        reasons.append(f'价格已越过已知20日高点{d["high20"]:.2f}元；可继续观察突破承接，上方空间未量化，不设目标价')
    if rr is not None:
        reasons.append(f'至历史压力{resistance:.2f}元尚有{space:.2f}%，至支撑{support:.2f}元为{risk:.2f}%，结构比{rr:.2f}倍')
    if high and low and d.get('atr14') and high-low > d['atr14']:
        branch = '高波动·' + branch
        risks.append(f'当日振幅金额{high-low:.2f}元高于历史ATR14 {d["atr14"]:.2f}元，须防冲高回落；高波动本身不加分或淘汰')
    gaps = list((daily or {}).get('gaps') or []) + minute_gaps + independent_gaps
    gaps.append('催化事件及预期差尚未独立核验，不作为本轮入选理由')
    if not sector_ok:
        gaps.append('行业同刻共振未完整确认；降低置信度，不作为单项否决' if v11 else
                    '未同时满足行业同刻上涨、半数同业上涨及个股相对领先；不按孤立脉冲推荐')
    if not baseline_ok:
        gaps.append('同刻20日基准尚未齐全；5—19日仅低置信排序，少于5日不生成V1.1分数' if v11 else
                    '同刻20日基准尚未齐全，盘中推荐资格未开放')
    elif not volume_confirmed:
        risks.append('近3分钟成交额未超过20日同刻中位数，即时放量未确认')
    if not funds_ok:
        gaps.append('新鲜主动买卖量分类或近3分钟主力估算净额>0未核实；降低置信度' if v11 else
                    '新鲜主动买卖量分类或近3分钟主力估算净额>0未核实')
    if resistance is None:
        gaps.append('上方可比压力/剩余空间未量化，不编造目标价')
    if support is None:
        gaps.append('可核验失效支撑不足')
    if rr is not None and rr <= 1:
        risks.append('当前压力空间不大于到支撑的距离，买点空间不足')
    if slot == '1455':
        risks.append('尾盘脉冲不能单独证明隔夜延续；次日开盘与消息风险未消除')
    if slot == '0920':
        gaps.append('普通报价缺少完整竞价字段，仅列盘前条件线索')
    # Distinct conditional branches avoid a single numeric gate excluding new
    # highs or deep-water recovery. A daily trend alone is a premarket lead;
    # during the session it waits outside the recommendation list for minutes.
    history_days = int((intraday or {}).get('historyDays') or 0)
    eligible = bool(p and ((premarket and premarket_quote_ok and aligned and d.get('ma5', 0) > d.get('ma10', 0) > d.get('ma20', 0))
                          or (not premarket and valid_quote and amount_ok and immediate and
                              (True if v11 else sector_ok and volume_confirmed and funds_ok))))
    if not immediate and not premarket:
        gaps.append('未取得3/5分钟持续转强及VWAP承接或局部突破的独立证据，本轮不因日涨幅高而入选')
    triggers = [f'复核VWAP {vwap:.2f}元附近承接及连续3/5分钟转强；板块联动作为加分证据' if vwap else '开盘后取得连续分钟突破证据，并核验正常可成交性'] if v11 else [f'复核VWAP {vwap:.2f}元附近承接、3/5分钟持续转强及板块联动后再评估' if vwap else '开盘后取得有效VWAP和连续分钟成交证据，再核验板块联动']
    invalidations = [f'跌破{support:.2f}元则原支撑逻辑失效' if support else '失效价位未核实前不形成买入计划',
                     '板块联动消失、放量下跌或报价过期时重新评估']
    no_chase = f'接近{resistance:.2f}元且剩余空间不再大于失效距离时不追' if resistance else '未核实上方空间时不追；封死涨停仅作风向标'
    risks = list(dict.fromkeys(risks + gaps))
    return {'recommendationLogicVersion': VERSION, 'strategyBranch': branch, 'selectionReasons': reasons,
            'riskReasons': risks, 'indicatorEvidence': evidence, 'evidenceEligible': eligible,
            'evidenceGaps': list(dict.fromkeys(gaps)), 'structureRewardRisk': rr,
            'evidenceChecks': {'minute_structure':immediate,'sector_resonance':sector_ok,
                'history_5d':history_days >= 5,'history_20d':baseline_ok,
                'relative_volume':volume_confirmed,'fund_direction':funds_ok},
            'sectorEvidence':sector,'fundEvidence':funds,
            'baselineEvidence':{key:(intraday or {}).get(key) for key in ('historyDays','baselineReady','baselineMedianAmount','baselineDates','missingHistoryDates','baselineSource','historyAccess')},
            'evidencePriority': 'minute_price_and_turnover' if immediate and (finite(m.get('amount_3m')) or 0)>0 else 'minute_price' if immediate else 'premarket_structure',
            'rankMeaning': '按即时价量证据完整性安排观察，其次参考累计成交额；不是预期收益或胜率排名',
            'entryPlan': {'trigger': triggers[0], 'invalidations': invalidations, 'noChase': no_chase,
                          'holdingWindow': 'T至T+5；逐时复核，不能把条件观察当已成交'},
            'triggers': triggers, 'invalidations': invalidations, 'noChase': no_chase,
            'thesis': '；'.join(reasons) if eligible else '独立买点证据不足：' + '；'.join(risks),
            'risks': risks, 'probabilities': {}}


def evidence_order(candidate):
    """Evidence before uncalibrated model signal; no additive factor weights."""
    priority = {'minute_price_and_turnover':0, 'minute_price':1, 'premarket_structure':2}
    return (not candidate.get('evidenceEligible'), priority.get(candidate.get('evidencePriority'), 3),
            -(finite((candidate.get('quote') or {}).get('amount')) or 0),
            str(candidate.get('code')))
