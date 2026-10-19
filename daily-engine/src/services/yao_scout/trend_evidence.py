"""Causal, descriptive minute-path features; no ranking or fund-identity claims.

Input bars are normalized, completed, end-labelled one-minute bars. ``close``
is CNY/share and ``amount_cny`` is the *interval* turnover in CNY, never a
cumulative field. The caller owns source validation, freshness and cumulative
conversion. Each supplied VWAP must use only observations through its own key.
This function cannot verify the provenance of an externally supplied VWAP.

Only exact same-session windows ending at ``end`` are accessed. A 15-minute
return path requires 16 closes; a 30-minute path requires 31. No interpolation,
look-ahead, lunch bridging, thresholds or inferred probability is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any


TREND_EVIDENCE_VERSION = "minute-trend-evidence-v1"
SHANGHAI = timezone(timedelta(hours=8))
HORIZONS = (15, 30)

_DEFINITIONS = {
    "signed_log_efficiency_{n}m": {
        "unit": "ratio [-1,1]",
        "formula": "(log(P_n)-log(P_0))/sum(i=1..n,abs(log(P_i)-log(P_(i-1)))); flat path=0",
        "meaning": "有方向的净涨跌占全部价格路径长度的比例；不是收益概率",
    },
    "close_max_drawdown_{n}m_pct": {
        "unit": "percent",
        "formula": "100*max(j=0..n,1-P_j/max(i=0..j,P_i))",
        "meaning": "窗口内分钟收盘价的最大回撤幅度；不含分钟内未采样的低点",
    },
    "close_pullback_to_rise_{n}m": {
        "unit": "ratio, may exceed 1",
        "formula": "(max(log(P_i))-log(P_n))/(max(log(P_i))-log(P_0)); undefined if denominator=0",
        "meaning": "从窗口收盘价最高点回吐的幅度占此前上涨幅度；没有正向上涨时缺失",
    },
    "signed_amount_proxy_{n}m": {
        "unit": "ratio [-1,1]",
        "formula": "sum(i=1..n,sign(P_i-P_(i-1))*A_i)/sum(i=1..n,A_i); sign(0)=0",
        "meaning": "按收盘价涨跌方向标记成交额的代理量；不是主动买卖额或主力净流入",
    },
    "vwap_occupancy_{n}m": {
        "unit": "fraction [0,1]",
        "formula": "sum(i=1..n,1[P_i>=VWAP_i])/n; each VWAP_i uses data through i only",
        "meaning": "最近n个分钟收盘价处于各自当时累计VWAP及以上的时间比例",
    },
    "normalized_log_slope_{n}m": {
        "unit": "dimensionless ratio, not constrained to [-1,1]",
        "formula": "n*OLS_slope(i,log(P_i))/sum(i=1..n,abs(log(P_i)-log(P_(i-1)))); flat path=0",
        "meaning": "以全部价格路径长度归一化的对数价格回归斜率；不是显著性或概率",
    },
}


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None


def _session(stamp: datetime) -> tuple | None:
    minute = stamp.hour * 60 + stamp.minute
    name = "am" if 570 < minute <= 690 else "pm" if 780 < minute <= 900 else None
    return (stamp.date(), name) if name else None


def compute_trend_evidence(
    current: dict[datetime, dict],
    end: datetime,
    vwap_series: dict[datetime, float],
) -> dict:
    """Return versioned metrics and per-metric reasons for unavailable values.

    ``end`` must be an aware datetime on a completed minute boundary; invalid
    endpoints raise ValueError. Dictionary keys must be aware minute-end times.
    Naive/off-minute keys cannot fill an exact required timestamp and are not
    substituted. Other bars, including future bars, are never accessed.

    Flat-price efficiency and slope are defined as zero. A pullback/rise ratio
    without any positive rise is unavailable, not zero. Missing interval amount
    or VWAP affects only its dependent metric. ``volume_shares`` is not needed
    for a supplied VWAP or the signed-turnover proxy.
    """
    if not isinstance(end, datetime) or end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be a timezone-aware minute-end datetime")
    end = end.astimezone(SHANGHAI)
    if end.second or end.microsecond:
        raise ValueError("end must be aligned to a minute boundary")

    definitions = {
        template.format(n=n): {**definition, "windowMinutes": n, "requiredCloses": n + 1}
        for n in HORIZONS for template, definition in _DEFINITIONS.items()
    }
    result = {
        "version": TREND_EVIDENCE_VERSION,
        "asOf": end.isoformat(),
        "validationStatus": "unvalidated_descriptive",
        "selectionAuthority": False,
        "mainFundsIdentified": False,
        "metrics": dict.fromkeys(definitions),
        "metricDefinitions": definitions,
        "missingReasons": {},
        "basis": "仅描述已完成分钟价格与成交额路径；不识别资金身份，不代表未来上涨概率，也不提高入选资格",
    }
    metrics, reasons = result["metrics"], result["missingReasons"]

    for n in HORIZONS:
        keys = [template.format(n=n) for template in _DEFINITIONS]
        stamps = [end - timedelta(minutes=i) for i in range(n, -1, -1)]
        session = _session(end)
        if session is None or any(_session(at) != session for at in stamps):
            reasons.update({key: "window_crosses_session_boundary" for key in keys})
            continue
        bars = [current.get(at) for at in stamps]
        if any(not isinstance(bar, dict) for bar in bars):
            reasons.update({key: "missing_contiguous_minute_bars" for key in keys})
            continue
        closes = [_number(bar.get("close"), positive=True) for bar in bars]
        if any(close is None for close in closes):
            reasons.update({key: "invalid_or_missing_close" for key in keys})
            continue

        logs = [math.log(close) for close in closes]
        changes = [right - left for left, right in zip(logs, logs[1:])]
        variation = math.fsum(abs(change) for change in changes)
        metrics[f"signed_log_efficiency_{n}m"] = (logs[-1] - logs[0]) / variation if variation else 0.0
        peak, drawdown = closes[0], 0.0
        for close in closes:
            peak = max(peak, close)
            drawdown = max(drawdown, 1 - close / peak)
        metrics[f"close_max_drawdown_{n}m_pct"] = 100 * drawdown
        rise = max(logs) - logs[0]
        if rise > 0:
            metrics[f"close_pullback_to_rise_{n}m"] = (max(logs) - logs[-1]) / rise
        else:
            reasons[f"close_pullback_to_rise_{n}m"] = "no_positive_close_rise_in_window"

        # Center the log values as well as time to reduce floating-point loss.
        centre = math.fsum(logs) / len(logs)
        numerator = math.fsum((i - n / 2) * (value - centre) for i, value in enumerate(logs))
        denominator = math.fsum((i - n / 2) ** 2 for i in range(n + 1))
        slope = numerator / denominator
        metrics[f"normalized_log_slope_{n}m"] = n * slope / variation if variation else 0.0

        amounts = [_number(bar.get("amount_cny")) for bar in bars[1:]]
        proxy_key = f"signed_amount_proxy_{n}m"
        if any(amount is None for amount in amounts):
            reasons[proxy_key] = "missing_or_invalid_interval_amount_cny"
        elif max(amounts) == 0:
            reasons[proxy_key] = "zero_interval_turnover"
        else:
            # Scaling leaves the ratio unchanged and prevents turnover overflow.
            scale = max(amounts)
            weights = [amount / scale for amount in amounts]
            signed = math.fsum(
                weight * ((right > left) - (right < left))
                for left, right, weight in zip(closes, closes[1:], weights)
            )
            metrics[proxy_key] = signed / math.fsum(weights)

        vwaps = [_number(vwap_series.get(at), positive=True) for at in stamps[1:]]
        occupancy_key = f"vwap_occupancy_{n}m"
        if any(vwap is None for vwap in vwaps):
            reasons[occupancy_key] = "missing_or_invalid_point_in_time_vwap"
        else:
            metrics[occupancy_key] = sum(close >= vwap for close, vwap in zip(closes[1:], vwaps)) / n

    return result
