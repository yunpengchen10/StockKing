"""Leakage-safe Alpha158-style daily features used by Stock King.

Every feature at row *t* is calculated from rows ``<= t``. Labels are created
separately and are never returned by :func:`build_features`.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

PRICE_COLUMNS = ("open", "high", "low", "close")


def normalize_daily_frame(raw: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame(raw).copy()
    aliases = {
        "date": ("date", "trade_date", "datetime", "日期"),
        "open": ("open", "开盘"),
        "high": ("high", "最高"),
        "low": ("low", "最低"),
        "close": ("close", "收盘", "price"),
        "volume": ("volume", "vol", "成交量"),
        "amount": ("amount", "成交额"),
    }
    normalized = pd.DataFrame(index=frame.index)
    for target, candidates in aliases.items():
        source = next((name for name in candidates if name in frame.columns), None)
        if source is not None:
            normalized[target] = frame[source]
    if "close" not in normalized:
        return pd.DataFrame(columns=["date", *PRICE_COLUMNS, "volume", "amount"])
    for column in PRICE_COLUMNS:
        if column not in normalized:
            normalized[column] = normalized["close"]
    for column in ("volume", "amount"):
        if column not in normalized:
            normalized[column] = 0.0
    if "date" not in normalized:
        normalized["date"] = pd.RangeIndex(len(normalized))
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in (*PRICE_COLUMNS, "volume", "amount"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=["date", "close"])
    normalized = normalized[normalized["close"] > 0]
    return normalized.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def build_features(raw: pd.DataFrame) -> pd.DataFrame:
    frame = normalize_daily_frame(raw)
    if frame.empty:
        return pd.DataFrame(index=frame.index)
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    open_ = frame["open"].astype(float)
    volume = frame["volume"].astype(float).clip(lower=0)
    amount = frame["amount"].astype(float).clip(lower=0)
    result = pd.DataFrame(index=frame.index)

    for window in (1, 2, 3, 5, 10, 20, 60):
        result[f"ret_{window}"] = close.pct_change(window, fill_method=None)
    for window in (5, 10, 20, 60, 120):
        mean = close.rolling(window, min_periods=window).mean()
        result[f"close_sma_{window}"] = close / mean - 1.0
        result[f"range_pos_{window}"] = (
            (close - low.rolling(window, min_periods=window).min())
            / (high.rolling(window, min_periods=window).max() - low.rolling(window, min_periods=window).min()).replace(0, np.nan)
        )
    daily_return = close.pct_change(fill_method=None)
    for window in (5, 10, 20, 60):
        result[f"volatility_{window}"] = daily_return.rolling(window, min_periods=window).std(ddof=0)
        result[f"downside_vol_{window}"] = daily_return.clip(upper=0).rolling(window, min_periods=window).std(ddof=0)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    result["atr_14"] = true_range.rolling(14, min_periods=14).mean() / close
    result["intraday_range"] = (high - low) / close
    result["open_gap"] = open_ / previous_close - 1.0
    result["close_location"] = (close - low) / (high - low).replace(0, np.nan)

    for window in (5, 20, 60):
        vol_mean = volume.rolling(window, min_periods=window).mean()
        vol_std = volume.rolling(window, min_periods=window).std(ddof=0)
        result[f"volume_ratio_{window}"] = volume / vol_mean.replace(0, np.nan)
        result[f"volume_z_{window}"] = (volume - vol_mean) / vol_std.replace(0, np.nan)
        amount_mean = amount.rolling(window, min_periods=window).mean()
        result[f"amount_ratio_{window}"] = amount / amount_mean.replace(0, np.nan)
    result["amihud_20"] = (daily_return.abs() / amount.replace(0, np.nan)).rolling(20, min_periods=20).mean() * 1e8

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    result["rsi_14"] = 100.0 - 100.0 / (1.0 + gain / loss.replace(0, np.nan))
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    result["macd_norm"] = macd / close
    result["macd_signal_norm"] = macd.ewm(span=9, adjust=False, min_periods=9).mean() / close
    signed_volume = np.sign(delta.fillna(0)) * volume
    obv = signed_volume.cumsum()
    result["obv_momentum_20"] = obv.diff(20) / volume.rolling(20, min_periods=20).sum().replace(0, np.nan)

    result = result.replace([np.inf, -np.inf], np.nan)
    return result.astype(float)


def forward_return(raw: pd.DataFrame, horizon: int) -> pd.Series:
    """Return the close-to-close label without placing it in feature data."""
    frame = normalize_daily_frame(raw)
    return frame["close"].shift(-int(horizon)) / frame["close"] - 1.0


def assert_no_future_dependency(raw: pd.DataFrame, cut_points: Iterable[int]) -> None:
    """Test helper: changing future rows must not alter earlier features."""
    frame = normalize_daily_frame(raw)
    baseline = build_features(frame)
    for cut in cut_points:
        if cut <= 0 or cut >= len(frame):
            continue
        changed = frame.copy()
        changed.loc[cut:, "close"] *= 7.0
        changed.loc[cut:, "high"] *= 7.0
        changed.loc[cut:, "low"] *= 7.0
        changed.loc[cut:, "open"] *= 7.0
        candidate = build_features(changed)
        pd.testing.assert_frame_equal(baseline.iloc[:cut], candidate.iloc[:cut])

