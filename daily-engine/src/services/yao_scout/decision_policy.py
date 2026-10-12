"""Predeclared research allocation limits shared by live selection and replay.

These are risk preferences, not fitted return estimates or broker instructions.
Freeze caps for every candidate before selection; planned weights must never be
used to constrain a counterfactual ranking in validation.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from .research_policy import _freeze, _plain

PORTFOLIO_POLICY_VERSION = "king-portfolio-v1"
POLICY_PARAMETERS = _freeze({
    "exposureByRegime": {"normal": 1.0, "weak": .5, "hot": .75, "unknown": .5},
    "maxPositionWeight": .2,
    "maxIndustryWeight": .4,
    "unknownIndustryPositionWeight": .1,
    "maxPositions": 5,
    "validationStatus": "predeclared_risk_preferences_not_fitted",
})


def portfolio_parameters():
    return _plain(POLICY_PARAMETERS)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def prediction_entry_allowed(candidate):
    """Cold-start rules may collect research; a rejected model cannot fall back.

An active prediction must have a positive, finite cost-adjusted mean and utility.
Its release/contract/lineage checks belong to the model registry. Missing fields
on a claimed active prediction fail closed instead of silently using rule rank.
"""
    prediction = candidate.get("prediction")
    if not isinstance(prediction, Mapping):
        return True
    status = prediction.get("status")
    if prediction.get("entryEligible") is False or status in {
        "data_rejected", "contract_rejected", "prediction_failed", "no_edge",
    }:
        return False
    if status in {"active", "model_active", "validated_model"}:
        mean, utility = _number(prediction.get("expectedNetReturn")), _number(prediction.get("utility"))
        return bool(prediction.get("entryEligible") is True
                    and mean is not None and mean > 0 and utility is not None and utility > 0)
    return status in {None, "rules_unvalidated", "shadow"}


def _industry(candidate):
    quote = candidate.get("quote") or {}
    value = candidate.get("industry") or quote.get("industry")
    if isinstance(value, str) and value.strip() and value.strip() not in {"未知", "--", "其他", "unknown"}:
        return value.strip()
    return None


def annotate_portfolio(candidates):
    """Freeze the same market budget and per-name caps for selected and controls."""
    rows = list(candidates)
    regimes = [(row.get("marketRegime") or {}).get("state", "unknown") for row in rows]
    regimes = [value if value in POLICY_PARAMETERS["exposureByRegime"] else "unknown" for value in regimes]
    regime = min(regimes or ["unknown"], key=lambda key: POLICY_PARAMETERS["exposureByRegime"][key])
    total = POLICY_PARAMETERS["exposureByRegime"][regime]
    annotated = []
    for row in rows:
        industry = _industry(row)
        cap = POLICY_PARAMETERS["maxPositionWeight"] if industry else POLICY_PARAMETERS["unknownIndustryPositionWeight"]
        policy = {
            "version": PORTFOLIO_POLICY_VERSION, "marketRegime": regime,
            "totalExposureCap": total, "maxPositionWeight": cap,
            "maxIndustryWeight": POLICY_PARAMETERS["maxIndustryWeight"],
            "maxPositions": POLICY_PARAMETERS["maxPositions"], "industry": industry,
            "validationStatus": POLICY_PARAMETERS["validationStatus"],
            "meaning": "事前研究资金上限；保留现金，不代表券商下单或已验证收益",
        }
        annotated.append({**row, "portfolioPolicy": policy, "portfolioWeight": cap,
                          "portfolioWeightKind": "cap"})
    return annotated


def allocate_portfolio(candidates):
    """Allocate a sorted candidate list under frozen caps; never force a full list.

This is a fresh research allocation, not a rebalance of the user's holdings.
Account replay additionally applies these same caps to its open positions.
"""
    rows = list(candidates)
    if not all((row.get("portfolioPolicy") or {}).get("version") == PORTFOLIO_POLICY_VERSION for row in rows):
        rows = annotate_portfolio(rows)
    selected, seen, industry_used = [], set(), {}
    used = 0.0
    total_cap = min((_number((row.get("portfolioPolicy") or {}).get("totalExposureCap")) or 0 for row in rows), default=0)
    for row in rows:
        code = str(row.get("code") or "")
        policy = row["portfolioPolicy"]
        if not code or code in seen or not prediction_entry_allowed(row):
            continue
        if len(selected) >= POLICY_PARAMETERS["maxPositions"]:
            break
        # Unknown industry is one conservative exposure bucket, not one per stock.
        industry = policy.get("industry") or "__unknown__"
        cap = _number(policy.get("maxPositionWeight")) or 0
        industry_cap = _number(policy.get("maxIndustryWeight")) or 0
        weight = min(cap, max(0., total_cap-used), max(0., industry_cap-industry_used.get(industry, 0.)))
        if weight <= 1e-9:
            continue
        weight = round(weight, 10)
        selected.append({**row, "plannedWeight": weight})
        seen.add(code)
        used += weight
        industry_used[industry] = industry_used.get(industry, 0.)+weight
    return {"candidates": selected, "policy": {
        "version": PORTFOLIO_POLICY_VERSION, "totalExposureCap": total_cap,
        "plannedExposure": round(used, 10), "cashWeight": round(1-used, 10),
        "industryExposure": industry_used, "maxPositions": POLICY_PARAMETERS["maxPositions"],
        "validationStatus": POLICY_PARAMETERS["validationStatus"],
        "meaning": "本轮研究配置；账户验证另计已有持仓及实际成交，允许零入选",
    }}
