"""A-share board limits and non-leaking 3/5/10-session labels."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import pandas as pd


def board_limit_ratio(code: str, trade_date: date, *, is_st: bool = False) -> float:
    """Return the historical daily price-limit ratio for the supported universe."""
    normalized = str(code or "").strip().zfill(6)
    if is_st:
        return 0.05
    if normalized.startswith("68"):
        return 0.20
    if normalized.startswith("30"):
        return 0.20 if trade_date >= date(2020, 8, 24) else 0.10
    return 0.10


def calculate_limit_price(previous_close: float, code: str, trade_date: date, *, is_st: bool = False) -> float:
    """Calculate exchange-style limit price using half-up cent rounding."""
    ratio = Decimal(str(board_limit_ratio(code, trade_date, is_st=is_st)))
    price = Decimal(str(previous_close)) * (Decimal("1") + ratio)
    return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_limit_touch(high: float, previous_close: float, code: str, trade_date: date, *, is_st: bool = False) -> bool:
    if previous_close <= 0 or high <= 0:
        return False
    return float(high) >= calculate_limit_price(previous_close, code, trade_date, is_st=is_st) - 0.005


def is_one_price_limit(row: pd.Series, previous_close: float, code: str, trade_date: date) -> bool:
    values = [_float(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return False
    limit_price = calculate_limit_price(previous_close, code, trade_date)
    return max(abs(float(value) - limit_price) for value in values if value is not None) <= 0.011


def evaluate_outcome_labels(
    history: pd.DataFrame,
    *,
    code: str,
    observation_date: date,
    entry_price: float | None = None,
) -> dict[str, Any]:
    """Evaluate future-only labels; missing sessions remain partial/unavailable."""
    frame = _normalize_history(history)
    if frame.empty:
        return _unavailable("history_unavailable")
    prior = frame.loc[frame["date"] <= observation_date]
    future = frame.loc[frame["date"] > observation_date].head(10).copy()
    if entry_price is None and not prior.empty:
        entry_price = _float(prior.iloc[-1].get("close"))
    if not entry_price or entry_price <= 0:
        return _unavailable("entry_price_unavailable")
    if future.empty:
        return {
            **_unavailable("future_sessions_not_available"),
            "maturity_status": "deferred",
        }

    touches: list[bool] = []
    one_price: list[bool] = []
    previous_close = float(entry_price)
    for _, row in future.iterrows():
        trade_date = row["date"]
        high = _float(row.get("high")) or 0.0
        touched = is_limit_touch(high, previous_close, code, trade_date)
        touches.append(touched)
        one_price.append(touched and is_one_price_limit(row, previous_close, code, trade_date))
        close = _float(row.get("close"))
        if close and close > 0:
            previous_close = close

    highs = pd.to_numeric(future.get("high"), errors="coerce").dropna()
    lows = pd.to_numeric(future.get("low"), errors="coerce").dropna()
    max_return = ((float(highs.max()) / entry_price) - 1.0) * 100.0 if not highs.empty else None
    max_drawdown = ((float(lows.min()) / entry_price) - 1.0) * 100.0 if not lows.empty else None

    ignition = any(touches[:3]) if len(future) >= 3 else None
    continuation = (
        sum(touches[:5]) >= 2 or (max_return is not None and max_return >= 20.0)
        if len(future) >= 5
        else None
    )
    strong = (
        sum(touches[:10]) >= 3 or (max_return is not None and max_return >= 30.0)
        if len(future) >= 10
        else None
    )
    maturity = "mature" if len(future) >= 10 else "partial"
    touched_count = sum(touches)
    tradability_status = "available"
    if touched_count and all(one_price[index] for index, touched in enumerate(touches) if touched):
        tradability_status = "unavailable_one_price_limit"
    return {
        "maturity_status": maturity,
        "ignition_3d": ignition,
        "continuation_5d": continuation,
        "strong_10d": strong,
        "max_return_pct": round(max_return, 4) if max_return is not None else None,
        "max_drawdown_pct": round(max_drawdown, 4) if max_drawdown is not None else None,
        "future_session_count": len(future),
        "limit_touch_count": touched_count,
        "tradability": {
            "status": tradability_status,
            "one_price_limit_count": sum(one_price),
        },
    }


def _normalize_history(history: pd.DataFrame) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    frame = history.copy()
    rename = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "换手率": "turnover_rate",
    }
    frame = frame.rename(columns={key: value for key, value in rename.items() if key in frame.columns})
    if "date" not in frame.columns:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index().rename(columns={frame.index.name or "index": "date"})
        else:
            return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame = frame.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last")
    return frame.reset_index(drop=True)


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "maturity_status": "unavailable",
        "ignition_3d": None,
        "continuation_5d": None,
        "strong_10d": None,
        "max_return_pct": None,
        "max_drawdown_pct": None,
        "future_session_count": 0,
        "limit_touch_count": 0,
        "tradability": {"status": "unavailable", "reason": reason},
    }


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if pd.notna(number) else None
    except (TypeError, ValueError):
        return None
