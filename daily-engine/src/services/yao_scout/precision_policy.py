"""Three explicit entry policies; no fitted probability or fabricated target.

These overlays can veto an entry, never upgrade missing market evidence. Model
champions retain their existing independent out-of-sample release gates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from src.services.akshare_context import number
from src.services.yao_scout.v11_factors import SCORE_VERSION
from .research_policy import DEFAULT_PARAMETERS, VALIDATION_STATUS

VERSION = "king-precision-v1"
V11_VERSION = "king-precision-v1.2"


def execution_quote_data(candidate):
    """Required provider prices for a reviewable signal, never inferred limits."""
    quote = candidate.get('quote') or {}
    fields = ('pre_close', 'limit_up', 'limit_down')
    values = {key: None if isinstance(quote.get(key), bool) else number(quote.get(key)) for key in fields}
    missing = [key for key, value in values.items() if value is None or value <= 0]
    valid_order = bool(not missing and values['limit_down'] < values['pre_close'] < values['limit_up'])
    conflicts = [] if missing or valid_order else ['price_limit_order']
    code = str(candidate.get('code') or '')
    rate = .2 if code.startswith(('30', '68')) else .1 if code.startswith(('00', '60')) else None
    if valid_order and rate and any(abs(values[key] - round(values['pre_close'] * ratio, 2)) > .015
                                   for key, ratio in (('limit_up', 1 + rate), ('limit_down', 1 - rate))):
        conflicts.append('price_limit_board_rule')
    if any(quote.get(key) is True for key in ('no_price_limit', 'corporate_action', 'suspended')):
        conflicts.append('exceptional_trading_session')
    return {'status': 'complete' if valid_order and not conflicts else 'incomplete',
            'requiredFields': list(fields), 'missingFields': missing,
            'conflicts': conflicts,
            'source': (quote.get('price_limit_evidence') or {}).get('source') or quote.get('source'),
            'sourceTime': (quote.get('price_limit_evidence') or {}).get('source_time')
                          or quote.get('provider_timestamp') or quote.get('source_time')}


def v11_entry_ready(candidate):
    """Canonical V1.2 data gate shared by live selection and validation."""
    formal = DEFAULT_PARAMETERS['history']['formalDays']
    factors = candidate.get('factorScores') or {}
    amount_days = number(candidate.get('baselineHistoryDays'))
    price_days = number(candidate.get('priceHistoricalCoverageDays'))
    return bool(candidate.get('scoreVersion') == SCORE_VERSION
                and candidate.get('evidenceEligible')
                and number(candidate.get('finalScore')) is not None
                and amount_days is not None and amount_days >= formal
                and price_days is not None and price_days >= formal
                and all(number(factors.get(key)) is not None and 0 <= number(factors.get(key)) <= 100
                        for key in DEFAULT_PARAMETERS['history']['criticalFactors'])
                and (candidate.get('dataEligibility') or {}).get('status') == 'formal'
                and execution_quote_data(candidate)['status'] == 'complete'
                and candidate.get('status') == 'conditional'
                and '盘前' not in str(candidate.get('strategyBranch', '')))


@dataclass(frozen=True)
class Policy:
    label: str
    max_bias_pct: float
    max_atr_extension: float
    min_reward_risk: float
    max_unlock_pct: float
    require_financial_quality: bool


POLICIES = MappingProxyType({key: Policy(**dict(value))
                           for key, value in DEFAULT_PARAMETERS['profiles'].items()})


def evaluate(candidate, context, daily=None):
    d = (daily or {}).get("metrics") or {}
    indicators = {r.get("key"): number(r.get("value")) for r in candidate.get("indicatorEvidence", [])}
    for name in ("ma5", "ma10", "ma20", "atr14", "last_close"):
        if name not in d and indicators.get(name) is not None:
            d = {**d, name: indicators[name]}
    quote = candidate.get("quote") or {}
    price = number(quote.get("price"))
    previous = number(quote.get("pre_close"))
    aligned = bool(previous and d.get("last_close") and abs(previous - d["last_close"]) <= DEFAULT_PARAMETERS['entry']['previousCloseToleranceCny'])
    ma5, atr = number(d.get("ma5")), number(d.get("atr14"))
    bias = (price / ma5 - 1) * 100 if aligned and price and ma5 and ma5 > 0 else None
    extension = (price - ma5) / atr if bias is not None and atr and atr > 0 else None
    trend = bool(aligned and price and d.get("ma5", 0) > d.get("ma10", 0) > d.get("ma20", 0) > 0
                 and price >= d["ma20"])
    rr = number(candidate.get("structureRewardRisk"))
    f = context.get("financials") or {}
    financial_known = all(number(f.get(k)) is not None for k in ("profit", "eps", "cashPerShare"))
    quality = bool(financial_known and f["profit"] > 0 and f["eps"] > 0 and f["cashPerShare"] > 0)
    negative_forecast = any(r.get("type") in DEFAULT_PARAMETERS['entry']['negativeForecastTypes']
                            and "净利润" in r.get("metric", "") for r in context.get("forecasts", []))
    unlock_values = [number(r.get('floatRatioPct')) for r in context.get('unlocks', [])]
    unlock_known = all(value is not None and value >= 0 for value in unlock_values)
    unlock = sum(value for value in unlock_values if value is not None and value >= 0)
    event_covered = (all(context.get("coverage", {}).get(k) for k in DEFAULT_PARAMETERS['entry']['eventCoverage'])
                     and unlock_known)
    premarket = candidate.get("status") == "premarket" or "盘前" in str(candidate.get("strategyBranch", ""))
    current_rules = candidate.get('scoreVersion') == SCORE_VERSION
    execution_data = execution_quote_data(candidate)
    policies = {}
    for key, rule in POLICIES.items():
        blocked, waiting = [], []
        uncertainties = []
        if current_rules and not v11_entry_ready(candidate):
            waiting.append('正式候选须20/20日量价基准、M/V/W/B关键因子及新鲜完整分钟结构；5—19日仅观察')
        if current_rules and execution_data['status'] != 'complete':
            waiting.append('推荐时点前收盘及供应商涨跌停价必须完整导入且一致，才能形成可复盘的正式候选')
        if not candidate.get("evidenceEligible"):
            waiting.append("独立价量、行业或资金证据尚未通过")
        if candidate.get("status") in {"expired", "data_insufficient", "windvane"}:
            waiting.append("报价或成交条件不支持当前入场")
        if premarket:
            waiting.append("盘前仅作条件观察，开盘后重新确认")
        if bias is None or extension is None:
            waiting.append("日线与报价未对齐，乖离及波动距离待核验")
        elif bias > rule.max_bias_pct or extension > rule.max_atr_extension:
            blocked.append("价格离MA5过远，等待回踩或均线跟进")
        if rr is not None and rr < rule.min_reward_risk:
            blocked.append("压力空间不足以覆盖支撑风险")
        elif rr is None:
            local_high = next((number(row.get('value')) for row in candidate.get('indicatorEvidence', [])
                               if row.get('key') == 'local_high_5m' and row.get('status') == 'observed'), None)
            breakout_exception = bool(current_rules and key == 'aggressive'
                and DEFAULT_PARAMETERS['entry']['unknownRewardRiskAggressiveBreakoutException']
                and (candidate.get('evidenceChecks') or {}).get('minute_structure')
                and '突破' in str(candidate.get('strategyBranch', ''))
                and price is not None and local_high is not None and price > local_high > 0)
            if breakout_exception:
                uncertainties.append('激进档分钟突破已核验；上方历史压力/结构盈亏比未知，仅条件观察入选，不生成目标价或收益承诺')
            elif current_rules or key != 'aggressive':
                waiting.append("上方空间或失效支撑尚未量化")
        if key != "aggressive" and not trend:
            waiting.append("日线多头趋势未确认")
        if key == "aggressive" and not (candidate.get("evidenceChecks") or {}).get("minute_structure"):
            waiting.append("突破或分钟延续信号未确认")
        if negative_forecast:
            blocked.append("已披露净利润预告存在恶化，等待重新评估")
        if unlock >= rule.max_unlock_pct:
            blocked.append("未来14日各批次解禁比例合计达到本档风险限额")
        if not event_covered:
            waiting.append("业绩预告或解禁接口覆盖不完整，风险尚未排清")
        if rule.require_financial_quality and not quality:
            (blocked if financial_known else waiting).append("稳健档要求盈利及经营现金流均为正")
        if rule.require_financial_quality and not context.get('coverage', {}).get('financials'):
            waiting.append('财报查询覆盖不完整，不能用旧报告排除新风险')
        state = "avoid" if blocked else "watch" if waiting else "conditional"
        policies[key] = {"label": rule.label, "state": state, "entryEligible": state == "conditional",
                         "reasons": blocked + waiting or ["满足本档条件；仍须确认可成交价格与失效线"],
                         "uncertainties": uncertainties,
                         "thresholds": asdict(rule)}
    return {"version": V11_VERSION if current_rules else VERSION, "validationStatus": VALIDATION_STATUS, "probability": None,
            "executionQuoteData": execution_data,
            "metrics": {"biasMa5Pct": bias, "atrExtension": extension, "trendConfirmed": trend,
                        "rewardRisk": rr, "unlock14dPct": unlock if context.get("coverage", {}).get("unlock") else None},
            "profiles": policies, "sourceContext": context,
            "meaning": "三档实际执行数据、价格位置、空间与事件风险门槛；覆盖度不是概率；参数待样本外检验"}
