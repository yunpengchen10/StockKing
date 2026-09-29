"""Three explicit entry policies; no fitted probability or fabricated target.

These overlays can veto an entry, never upgrade missing market evidence. Model
champions retain their existing independent out-of-sample release gates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from src.services.akshare_context import number

VERSION = "king-precision-v1"


@dataclass(frozen=True)
class Policy:
    label: str
    max_bias_pct: float
    max_atr_extension: float
    min_reward_risk: float
    max_unlock_pct: float
    require_financial_quality: bool


POLICIES = {
    "conservative": Policy("稳健", 3, 1.5, 2, 3, True),
    "regular": Policy("均衡", 5, 2, 1.5, 5, False),
    "aggressive": Policy("激进", 7.5, 2.5, 1.2, 8, False),
}


def evaluate(candidate, context, daily=None):
    d = (daily or {}).get("metrics") or {}
    indicators = {r.get("key"): number(r.get("value")) for r in candidate.get("indicatorEvidence", [])}
    for name in ("ma5", "ma10", "ma20", "atr14", "last_close"):
        if name not in d and indicators.get(name) is not None:
            d = {**d, name: indicators[name]}
    quote = candidate.get("quote") or {}
    price = number(quote.get("price"))
    previous = number(quote.get("pre_close"))
    aligned = bool(previous and d.get("last_close") and abs(previous - d["last_close"]) <= .011)
    ma5, atr = number(d.get("ma5")), number(d.get("atr14"))
    bias = (price / ma5 - 1) * 100 if aligned and price and ma5 and ma5 > 0 else None
    extension = (price - ma5) / atr if bias is not None and atr and atr > 0 else None
    trend = bool(aligned and price and d.get("ma5", 0) > d.get("ma10", 0) > d.get("ma20", 0) > 0
                 and price >= d["ma20"])
    rr = number(candidate.get("structureRewardRisk"))
    f = context.get("financials") or {}
    financial_known = all(number(f.get(k)) is not None for k in ("profit", "eps", "cashPerShare"))
    quality = bool(financial_known and f["profit"] > 0 and f["eps"] > 0 and f["cashPerShare"] > 0)
    negative_forecast = any(r.get("type") in {"首亏", "续亏", "增亏", "预减"}
                            and "净利润" in r.get("metric", "") for r in context.get("forecasts", []))
    unlock = sum(number(r.get("floatRatioPct")) or 0 for r in context.get("unlocks", []))
    event_covered = all(context.get("coverage", {}).get(k) for k in ("forecast", "unlock"))
    premarket = candidate.get("status") == "premarket" or "盘前" in str(candidate.get("strategyBranch", ""))
    if candidate.get('scoreVersion') == 'stockking-v1.1-rules':
        # V1.1 treats independently missing research factors as uncertainty.
        # The quote/identity, normal-trade and completed-minute structure gates
        # are already captured by evidenceEligible and candidate.status.
        ready = bool(candidate.get('evidenceEligible') and candidate.get('finalScore') is not None
                     and candidate.get('status') == 'conditional' and not premarket)
        limitations = []
        if bias is None or extension is None:
            limitations.append('日线乖离或ATR缺失，未参与排序')
        if rr is None:
            limitations.append('历史压力空间未量化，剩余收益预测暂不发布')
        if not event_covered:
            limitations.append('事件接口覆盖不足，催化因子为空')
        if not (candidate.get('evidenceChecks') or {}).get('sector_resonance'):
            limitations.append('行业共振未确认')
        if not (candidate.get('evidenceChecks') or {}).get('fund_direction'):
            limitations.append('主动资金方向未确认')
        reason = ('满足有效报价、正常交易和完整分钟结构；历史样本不足已降低置信，实际成交仍待验证'
                  if ready else '报价、正常交易或完整分钟结构不足')
        policies = {key:{'label':rule.label,'state':'conditional' if ready else 'watch',
                         'entryEligible':ready,'reasons':[reason],
                         'uncertainties':limitations,'thresholds':asdict(rule)}
                    for key,rule in POLICIES.items()}
        return {'version':'king-precision-v1.1','validationStatus':'uncalibrated_rules',
                'probability':None,'metrics':{'biasMa5Pct':bias,'atrExtension':extension,
                    'trendConfirmed':trend,'rewardRisk':rr,
                    'unlock14dPct':unlock if context.get('coverage',{}).get('unlock') else None},
                'profiles':policies,'sourceContext':context,
                'meaning':'缺失研究因子降低置信度；正式候选依赖有效报价、正常交易与可计算分钟结构'}
    policies = {}
    for key, rule in POLICIES.items():
        blocked, waiting = [], []
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
        elif rr is None and key != "aggressive":
            waiting.append("上方空间或失效支撑尚未量化")
        if key != "aggressive" and not trend:
            waiting.append("日线多头趋势未确认")
        if key == "aggressive" and not (candidate.get("evidenceChecks") or {}).get("minute_structure"):
            waiting.append("突破或分钟延续信号未确认")
        if negative_forecast:
            blocked.append("已披露净利润预告存在恶化，等待重新评估")
        if unlock >= rule.max_unlock_pct:
            blocked.append("未来14日各批次解禁比例合计超过本档风险限额")
        if not event_covered:
            waiting.append("业绩预告或解禁接口覆盖不完整，风险尚未排清")
        if rule.require_financial_quality and not quality:
            (blocked if financial_known else waiting).append("稳健档要求盈利及经营现金流均为正")
        if rule.require_financial_quality and not context.get('coverage', {}).get('financials'):
            waiting.append('财报查询覆盖不完整，不能用旧报告排除新风险')
        state = "avoid" if blocked else "watch" if waiting else "conditional"
        policies[key] = {"label": rule.label, "state": state, "entryEligible": state == "conditional",
                         "reasons": blocked + waiting or ["满足本档条件；仍须确认可成交价格与失效线"],
                         "thresholds": asdict(rule)}
    return {"version": VERSION, "validationStatus": "rules_unvalidated", "probability": None,
            "metrics": {"biasMa5Pct": bias, "atrExtension": extension, "trendConfirmed": trend,
                        "rewardRisk": rr, "unlock14dPct": unlock if context.get("coverage", {}).get("unlock") else None},
            "profiles": policies, "sourceContext": context,
            "meaning": "入场纪律检查，不是收益预测；新因子待样本外检验，不改写已训练模型权重"}
