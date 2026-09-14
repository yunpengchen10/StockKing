"""Chronological diagnostics for overlapping A-share holding-period labels."""
from __future__ import annotations

import numpy as np
import pandas as pd


def block_mean_interval(values, *, block_size: int, seed: int = 20260907):
    """Circular block bootstrap, preserving dependence within holding periods.

    Return no interval below five blocks; an absence of evidence is not zero risk.
    The interval is a percentile interval for the mean, not a return forecast.
    """
    values = np.asarray(values, dtype=float)
    if block_size < 1:
        raise ValueError("block_size must be positive")
    if not np.isfinite(values).all() or len(values) < max(20, 5 * block_size):
        return None
    rng = np.random.default_rng(seed)
    blocks = int(np.ceil(len(values) / block_size))
    starts = rng.integers(0, len(values), size=(1000, blocks, 1))
    indices = (starts + np.arange(block_size)) % len(values)
    samples = values[indices.reshape(1000, -1)[:, :len(values)]].mean(axis=1)
    return [float(value) for value in np.quantile(samples, [0.025, 0.975])]


def holding_period_report(dates, targets, returns, predictions, *, horizon: int, cost: float):
    """Evaluate one combined ranking against each holding period on held-out days.

    Returns are overlapping close-to-close label returns, not an executable NAV.
    Only days with at least six securities can measure Top-5 selection advantage.
    """
    frame = pd.DataFrame({"date": dates, "target": targets, "return": returns, "score": predictions})
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    rows = []
    for day, group in frame.groupby("date", sort=True):
        if len(group) < 6 or group.score.nunique() < 2 or group.target.nunique() < 2:
            continue
        selected = group.nlargest(5, "score")
        net = float(selected["return"].mean() - cost)
        benchmark = float(group["return"].mean())
        rows.append({"date": str(pd.Timestamp(day).date()),
                     "rankIc": float(group.score.corr(group.target, method="spearman")),
                     "net": net, "excess": net - benchmark,
                     "win": float((selected["return"] > cost).mean())})
    daily = pd.DataFrame(rows)
    intervals = {key: block_mean_interval(daily[key].to_numpy(), block_size=horizon)
                 if rows else None for key in ("rankIc", "net", "excess")}
    enough = all(value is not None for value in intervals.values())
    failures = []
    if not enough:
        failures.append("insufficient_independent_blocks")
    else:
        if intervals["rankIc"][0] <= 0:
            failures.append("rank_ic_not_significant")
        if intervals["net"][0] <= 0:
            failures.append("net_return_not_significant")
        if intervals["excess"][0] <= 0:
            failures.append("no_selection_advantage")
    return {
        "horizon": horizon, "evaluationDays": len(rows), "blockSize": horizon,
        "approximateBlocks": len(rows) // horizon,
        "rankIc": float(daily.rankIc.mean()) if rows else None,
        "rankIcInterval95": intervals["rankIc"],
        "top5NetReturn": float(daily.net.mean()) if rows else None,
        "netReturnInterval95": intervals["net"],
        "top5ExcessReturn": float(daily.excess.mean()) if rows else None,
        "excessReturnInterval95": intervals["excess"],
        "top5WinRate": float(daily.win.mean()) if rows else None,
        "costPerHolding": cost,
        "from": rows[0]["date"] if rows else None,
        "through": rows[-1]["date"] if rows else None,
        "qualified": not failures, "failures": failures,
        "method": "circular_block_bootstrap_1000",
        "returnBasis": "overlapping_close_to_close_labels",
        "benchmark": "same_day_eligible_panel_equal_weight_gross",
    }
