"""Panel models for the three Stock King recommendation objectives.

The module intentionally separates the product objectives:

* ``conservative`` estimates lower return bounds and downside risk.
* ``regular`` learns a cross-sectional, neutral excess-return rank.
* ``aggressive`` estimates the first limit-touch hazard over three days.

All fitted weights are learned from chronological out-of-sample predictions.
The public score is an empirical percentile, never a probability.
"""
from __future__ import annotations

import json
import math
import os
import pickle
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from src.quant.features import build_features, normalize_daily_frame
from src.quant.validation import holding_period_report
from src.quant.master import (
    predict_master_checkpoint_batch,
    read_checkpoint_payload,
)
from src.quant.service import board_from_symbol, normalize_a_share_symbol
from src.services.history_loader import load_history_df


TIER_MODEL_VERSION = "king-three-objective-v2.3.4"
TIER_OBJECTIVES = ("conservative", "regular", "aggressive")
TIER_HORIZONS = {
    "conservative": (20, 60),
    "regular": (5, 20),
    "aggressive": (1, 2, 3),
}
PANEL_HISTORY_DAYS = max(1260, int(os.getenv("STOCK_KING_PANEL_HISTORY_DAYS", "1900")))
PANEL_MAX_ROWS = max(100_000, int(os.getenv("STOCK_KING_PANEL_MAX_ROWS", "1200000")))
MIN_PANEL_SYMBOLS = max(8, int(os.getenv("STOCK_KING_PANEL_MIN_SYMBOLS", "20")))
MIN_PANEL_DATES = max(180, int(os.getenv("STOCK_KING_PANEL_MIN_DATES", "360")))
TRANSACTION_COST = 0.0015
MIN_PRICE_TICK = Decimal("0.01")


@dataclass
class TierArtifact:
    tier: str
    model_version: str = TIER_MODEL_VERSION
    trained_through: str = ""
    calibrated_through: str = ""
    feature_names: list[str] = field(default_factory=list)
    models: Dict[str, Any] = field(default_factory=dict)
    learned_weights: Dict[str, float] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    qualified: bool = False
    reason: str = "not_trained"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def registry_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("models", None)
        return payload


def forward_return(values: Sequence[float], horizon: int) -> np.ndarray:
    prices = np.asarray(values, dtype=float)
    result = np.full(len(prices), np.nan, dtype=float)
    horizon = int(horizon)
    if horizon > 0 and len(prices) > horizon:
        result[:-horizon] = prices[horizon:] / prices[:-horizon] - 1.0
    return result


def forward_realized_volatility(values: Sequence[float], horizon: int = 20) -> np.ndarray:
    prices = np.asarray(values, dtype=float)
    result = np.full(len(prices), np.nan, dtype=float)
    returns = np.full(len(prices), np.nan, dtype=float)
    returns[1:] = prices[1:] / prices[:-1] - 1.0
    for index in range(0, max(0, len(prices) - int(horizon))):
        window = returns[index + 1 : index + 1 + int(horizon)]
        if len(window) == int(horizon) and np.all(np.isfinite(window)):
            result[index] = float(np.std(window, ddof=0) * math.sqrt(252.0))
    return result


def forward_max_drawdown(values: Sequence[float], horizon: int = 20) -> np.ndarray:
    """Worst peak-to-trough drawdown after each row, without including row t."""
    prices = np.asarray(values, dtype=float)
    result = np.full(len(prices), np.nan, dtype=float)
    horizon = int(horizon)
    for index in range(0, max(0, len(prices) - horizon)):
        future = prices[index + 1 : index + 1 + horizon]
        if len(future) != horizon or not np.all(np.isfinite(future)) or np.any(future <= 0):
            continue
        peaks = np.maximum.accumulate(future)
        result[index] = float(np.min(future / peaks - 1.0))
    return result


def limit_ratio_for_symbol(symbol: str, stock_name: str = "") -> float:
    name = str(stock_name or "").upper()
    if "ST" in name or any(marker in name for marker in ("退市", "整理")):
        return 0.05
    board = board_from_symbol(normalize_a_share_symbol(symbol) or str(symbol).upper())
    if board in {"star", "chinext"}:
        return 0.20
    if board == "bse":
        return 0.30
    return 0.10


def limit_ratio_for_symbol_on_date(symbol: str, trade_date: Any, stock_name: str = "") -> float:
    """Point-in-time daily price-limit ratio used to construct event labels."""
    normalized = normalize_a_share_symbol(symbol) or str(symbol).upper()
    ratio = limit_ratio_for_symbol(normalized, stock_name)
    try:
        day = pd.Timestamp(trade_date).normalize()
    except (TypeError, ValueError):
        return ratio
    # ChiNext moved from 10% to 20% with the registration-based reform on
    # 2020-08-24.  Applying today's 20% rule to older rows leaks the rule era
    # into historical touch labels.
    if normalized.startswith("300") and day < pd.Timestamp("2020-08-24"):
        return 0.10
    return ratio


def rounded_limit_price(previous_close: float, ratio: float) -> float:
    if not math.isfinite(float(previous_close)) or float(previous_close) <= 0:
        return math.nan
    value = (Decimal(str(previous_close)) * (Decimal("1") + Decimal(str(ratio)))).quantize(
        MIN_PRICE_TICK, rounding=ROUND_HALF_UP
    )
    return float(value)


def first_limit_touch_labels(
    close: Sequence[float],
    high: Sequence[float],
    *,
    ratio: float | Sequence[float],
    horizon: int = 3,
) -> np.ndarray:
    """Return 0=no touch and 1..horizon=the first future limit-touch day."""
    closes = np.asarray(close, dtype=float)
    highs = np.asarray(high, dtype=float)
    ratios = np.asarray(ratio, dtype=float) if not np.isscalar(ratio) else np.full(len(closes), float(ratio))
    if ratios.shape != closes.shape:
        raise ValueError("ratio sequence must have the same length as close")
    output = np.full(len(closes), np.nan, dtype=float)
    horizon = int(horizon)
    for index in range(0, max(0, len(closes) - horizon)):
        output[index] = 0.0
        for step in range(1, horizon + 1):
            previous = closes[index + step - 1]
            limit_price = rounded_limit_price(previous, float(ratios[index + step]))
            if math.isfinite(limit_price) and highs[index + step] + 1e-9 >= limit_price:
                output[index] = float(step)
                break
    return output


def percentile_scores(values: Sequence[float]) -> np.ndarray:
    series = pd.Series(np.asarray(values, dtype=float))
    if series.empty:
        return np.asarray([], dtype=float)
    valid = series.replace([np.inf, -np.inf], np.nan)
    ranks = valid.rank(method="average", pct=True) * 100.0
    return ranks.fillna(0.0).to_numpy(dtype=float)


def daily_percentile(values: pd.Series, dates: pd.Series) -> pd.Series:
    work = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"), "date": dates})
    return work.groupby("date", sort=False)["value"].rank(method="average", pct=True)


def unique_column_names(*groups: Iterable[str]) -> list[str]:
    """Keep feature/label selections ordered without creating duplicate columns."""
    return list(dict.fromkeys(name for group in groups for name in group))


def nested_meta_gate_masks(dates: Sequence[Any], *, minimum_dates: int = 20,
                          purge: int = 0, label_end_dates=None) -> tuple[np.ndarray, np.ndarray]:
    """Split outer-fold predictions into weight/calibration and release gates."""
    normalized = pd.to_datetime(pd.Series(dates), errors="coerce").dt.normalize()
    ordered = np.asarray(sorted(normalized.dropna().unique()))
    if purge < 0:
        raise ValueError("purge must be nonnegative")
    if len(ordered) < minimum_dates * 2 + purge:
        return np.zeros(len(normalized), dtype=bool), np.zeros(len(normalized), dtype=bool)
    boundary = max(minimum_dates + purge, min(len(ordered) - minimum_dates, int(len(ordered) * 0.60)))
    meta_dates = set(ordered[:boundary - purge])
    gate_dates = set(ordered[boundary:])
    meta = normalized.isin(meta_dates).to_numpy()
    if label_end_dates is not None:
        ends = pd.to_datetime(pd.Series(label_end_dates), errors="coerce").to_numpy()
        if len(ends) != len(meta):
            raise ValueError("label_end_dates must align with dates")
        meta = meta & (ends < ordered[boundary])
    if normalized[meta].nunique() < minimum_dates:
        return np.zeros(len(normalized), dtype=bool), np.zeros(len(normalized), dtype=bool)
    return meta, normalized.isin(gate_dates).to_numpy()


def _safe_logit(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(values, dtype=float), 1e-5, 1.0 - 1e-5)
    return np.log(clipped / (1.0 - clipped))


def cumulative_hazard_probability(*hazards: Sequence[float]) -> np.ndarray:
    """Convert conditional first-event hazards into cumulative event probability."""
    if not hazards:
        return np.asarray([], dtype=float)
    matrix = np.column_stack([np.clip(np.asarray(item, dtype=float), 0.0, 1.0) for item in hazards])
    return 1.0 - np.prod(1.0 - matrix, axis=1)


def cross_sectional_neutralize(
    frame: pd.DataFrame,
    label: str,
    *,
    date_column: str = "date",
    industry_column: str = "industry",
    size_column: str = "log_size",
    beta_column: str = "beta_60",
) -> pd.Series:
    """OLS residualise a label by date using only cross-sectional exposures."""
    output = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby(date_column, sort=False):
        y = pd.to_numeric(group[label], errors="coerce")
        exposures = pd.DataFrame(index=group.index)
        exposures["intercept"] = 1.0
        if size_column in group:
            exposures[size_column] = pd.to_numeric(group[size_column], errors="coerce")
        if beta_column in group:
            exposures[beta_column] = pd.to_numeric(group[beta_column], errors="coerce")
        if industry_column in group:
            industries = group[industry_column].fillna("unknown").astype(str)
            dummies = pd.get_dummies(industries, prefix="industry", dtype=float, drop_first=True)
            dummies.index = group.index
            exposures = pd.concat([exposures, dummies], axis=1)
        valid = y.notna() & np.isfinite(exposures.to_numpy(dtype=float)).all(axis=1)
        if int(valid.sum()) < max(5, exposures.shape[1] + 1):
            output.loc[group.index] = y - y.mean()
            continue
        x = exposures.loc[valid].to_numpy(dtype=float)
        target = y.loc[valid].to_numpy(dtype=float)
        coefficients = np.linalg.pinv(x.T @ x + np.eye(x.shape[1]) * 1e-6) @ x.T @ target
        output.loc[y.loc[valid].index] = target - x @ coefficients
    return output


def purged_walk_forward_splits(
    dates: Sequence[Any],
    *,
    folds: int = 3,
    purge: int = 60,
    min_train_dates: int = 180,
) -> list[tuple[np.ndarray, np.ndarray]]:
    values = pd.to_datetime(pd.Series(dates), errors="coerce").dt.normalize()
    ordered = np.asarray(sorted(values.dropna().unique()))
    if len(ordered) < min_train_dates + purge + folds * 20:
        return []
    remaining = len(ordered) - min_train_dates - purge
    test_size = max(20, remaining // folds)
    result: list[tuple[np.ndarray, np.ndarray]] = []
    for fold in range(folds):
        test_start = min_train_dates + purge + fold * test_size
        if test_start >= len(ordered):
            break
        test_end = len(ordered) if fold == folds - 1 else min(len(ordered), test_start + test_size)
        train_end = test_start - purge
        train_dates = set(ordered[:train_end])
        test_dates = set(ordered[test_start:test_end])
        train_idx = np.flatnonzero(values.isin(train_dates).to_numpy())
        test_idx = np.flatnonzero(values.isin(test_dates).to_numpy())
        if len(train_idx) and len(test_idx):
            result.append((train_idx, test_idx))
    return result


def learn_nonnegative_weights(
    predictions: Mapping[str, Sequence[float]],
    target: Sequence[float],
    *,
    objective: str = "mse",
    steps: int = 400,
) -> Dict[str, float]:
    """Learn simplex weights with projected gradient; weak models can reach zero."""
    names = list(predictions)
    if not names:
        return {}
    matrix = np.column_stack([np.asarray(predictions[name], dtype=float) for name in names])
    y = np.asarray(target, dtype=float)
    valid = np.isfinite(y) & np.isfinite(matrix).all(axis=1)
    matrix, y = matrix[valid], y[valid]
    if not len(y):
        return {names[0]: 1.0}
    weights = np.full(len(names), 1.0 / len(names), dtype=float)
    rate = 0.08 / max(1.0, float(np.linalg.norm(matrix, ord=2) ** 2 / len(y)))
    for _ in range(max(20, int(steps))):
        combined = matrix @ weights
        if objective == "logloss":
            clipped = np.clip(combined, 1e-5, 1 - 1e-5)
            gradient = matrix.T @ ((clipped - y) / (clipped * (1 - clipped))) / len(y)
        else:
            gradient = 2.0 * matrix.T @ (combined - y) / len(y)
        weights = _project_simplex(weights - rate * gradient)
    weights[weights < 1e-4] = 0.0
    if weights.sum() <= 0:
        best = int(np.argmin([np.mean((matrix[:, i] - y) ** 2) for i in range(matrix.shape[1])]))
        weights[best] = 1.0
    weights /= weights.sum()
    return {name: float(weight) for name, weight in zip(names, weights) if weight > 0}


def _project_simplex(values: np.ndarray) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    ordered = np.sort(vector)[::-1]
    cumulative = np.cumsum(ordered) - 1.0
    indices = np.arange(1, len(vector) + 1)
    condition = ordered - cumulative / indices > 0
    rho = int(np.flatnonzero(condition)[-1]) if np.any(condition) else len(vector) - 1
    theta = cumulative[rho] / float(rho + 1)
    return np.maximum(vector - theta, 0.0)


def combine_predictions(predictions: Mapping[str, np.ndarray], weights: Mapping[str, float]) -> np.ndarray:
    usable = [(name, float(weight)) for name, weight in weights.items() if name in predictions and weight > 0]
    if not usable:
        first = next(iter(predictions.values()), np.asarray([], dtype=float))
        return np.asarray(first, dtype=float)
    total = sum(weight for _, weight in usable)
    return sum(np.asarray(predictions[name], dtype=float) * (weight / total) for name, weight in usable)


def _pinball(y: np.ndarray, prediction: np.ndarray, alpha: float) -> float:
    error = np.asarray(y, dtype=float) - np.asarray(prediction, dtype=float)
    return float(np.mean(np.maximum(alpha * error, (alpha - 1.0) * error)))


def _rank_ic(y: np.ndarray, prediction: np.ndarray, dates: Sequence[Any]) -> tuple[float, float]:
    work = pd.DataFrame({"date": dates, "y": y, "p": prediction}).dropna()
    daily = []
    for _, group in work.groupby("date", sort=False):
        if len(group) < 5:
            continue
        value = group["p"].corr(group["y"], method="spearman")
        if pd.notna(value):
            daily.append(float(value))
    return (float(np.mean(daily)) if daily else 0.0, float(np.std(daily)) if daily else 0.0)


def _bootstrap_mean_lower(values: Sequence[float], *, seed: int = 20260825) -> float:
    daily = np.asarray(values, dtype=float)
    if not len(daily):
        return 0.0
    rng = np.random.default_rng(seed)
    estimates = [float(np.mean(rng.choice(daily, size=len(daily), replace=True))) for _ in range(400)]
    return float(np.quantile(estimates, 0.025))


def _rank_ic_lower_bound(y: np.ndarray, prediction: np.ndarray, dates: Sequence[Any]) -> float:
    work = pd.DataFrame({"date": dates, "y": y, "p": prediction}).dropna()
    values = []
    for _, group in work.groupby("date", sort=False):
        if len(group) < 5:
            continue
        correlation = group["p"].corr(group["y"], method="spearman")
        if pd.notna(correlation):
            values.append(float(correlation))
    return _bootstrap_mean_lower(values)


def _ndcg_at_k(y: np.ndarray, prediction: np.ndarray, dates: Sequence[Any], k: int = 5) -> float:
    work = pd.DataFrame({"date": dates, "y": y, "p": prediction}).dropna()
    scores: list[float] = []
    for _, group in work.groupby("date", sort=False):
        if len(group) < 2:
            continue
        relevance = percentile_scores(group["y"].to_numpy()) / 100.0
        order = np.argsort(-group["p"].to_numpy())[:k]
        ideal = np.argsort(-relevance)[:k]
        discount = 1.0 / np.log2(np.arange(2, len(order) + 2))
        dcg = float(np.sum((2 ** relevance[order] - 1) * discount))
        idcg = float(np.sum((2 ** relevance[ideal] - 1) * discount))
        scores.append(dcg / idcg if idcg > 0 else 0.0)
    return float(np.mean(scores)) if scores else 0.0


def _topk_return(y: np.ndarray, prediction: np.ndarray, dates: Sequence[Any], k: int = 5) -> float:
    work = pd.DataFrame({"date": dates, "y": y, "p": prediction}).dropna()
    values = [float(group.nlargest(k, "p")["y"].mean()) for _, group in work.groupby("date", sort=False)]
    return float(np.mean(values) - TRANSACTION_COST) if values else 0.0


def _topk_return_lower_bound(y: np.ndarray, prediction: np.ndarray, dates: Sequence[Any], k: int = 5) -> float:
    work = pd.DataFrame({"date": dates, "y": y, "p": prediction}).dropna()
    values = [float(group.nlargest(k, "p")["y"].mean() - TRANSACTION_COST) for _, group in work.groupby("date", sort=False)]
    return _bootstrap_mean_lower(values, seed=20260826)


def _binary_metrics(y: np.ndarray, probability: np.ndarray, dates: Sequence[Any], k: int = 5) -> Dict[str, float]:
    from sklearn.metrics import average_precision_score, brier_score_loss, log_loss

    target = np.asarray(y, dtype=int)
    prob = np.clip(np.asarray(probability, dtype=float), 1e-5, 1 - 1e-5)
    prevalence = float(np.mean(target)) if len(target) else 0.0
    baseline = np.full(len(target), prevalence)
    brier = float(brier_score_loss(target, prob)) if len(target) else 1.0
    baseline_brier = float(brier_score_loss(target, baseline)) if len(target) else 1.0
    work = pd.DataFrame({"date": dates, "y": target, "p": prob})
    precisions = []
    for _, group in work.groupby("date", sort=False):
        if len(group):
            precisions.append(float(group.nlargest(k, "p")["y"].mean()))
    precision = float(np.mean(precisions)) if precisions else 0.0
    lift = precision / prevalence if prevalence > 0 else 0.0
    lift_lower = 0.0
    if precisions and prevalence > 0:
        # Resample complete trading dates so the interval preserves each day's
        # cross-sectional dependence instead of treating stocks as IID rows.
        rng = np.random.default_rng(20260825)
        daily = np.asarray(precisions, dtype=float)
        bootstrap_lift = np.asarray([
            float(np.mean(rng.choice(daily, size=len(daily), replace=True)) / prevalence)
            for _ in range(400)
        ])
        lift_lower = float(np.quantile(bootstrap_lift, 0.025))
    return {
        "prAuc": float(average_precision_score(target, prob)) if len(np.unique(target)) > 1 else 0.0,
        "prevalence": prevalence,
        "brierScore": brier,
        "baselineBrier": baseline_brier,
        "brierSkill": 1.0 - brier / baseline_brier if baseline_brier > 0 else 0.0,
        "logLoss": float(log_loss(target, prob, labels=[0, 1])) if len(target) else 1.0,
        "baselineLogLoss": float(log_loss(target, np.clip(baseline, 1e-5, 1 - 1e-5), labels=[0, 1])) if len(target) else 1.0,
        "precisionAt5": precision,
        "liftAt5": lift,
        "liftAt5Lower95": lift_lower,
    }


def _fit_sigmoid(scores: np.ndarray, target: np.ndarray) -> Any:
    from sklearn.linear_model import LogisticRegression

    valid = np.isfinite(scores) & np.isfinite(target)
    if int(valid.sum()) < 30 or len(np.unique(target[valid])) < 2:
        return None
    model = LogisticRegression(C=1.0, max_iter=500, random_state=20260825)
    model.fit(np.asarray(scores[valid], dtype=float).reshape(-1, 1), np.asarray(target[valid], dtype=int))
    return model


def _calibrated_probability(calibrator: Any, scores: np.ndarray) -> np.ndarray:
    values = np.asarray(scores, dtype=float)
    if calibrator is None:
        return np.clip(1.0 / (1.0 + np.exp(-np.clip(values, -30, 30))), 1e-5, 1 - 1e-5)
    return calibrator.predict_proba(values.reshape(-1, 1))[:, 1]


def _lgbm_regressor(*, objective: str = "huber", alpha: float = 0.5, seed: int = 20260825):
    from lightgbm import LGBMRegressor

    kwargs: Dict[str, Any] = {
        "objective": objective,
        "n_estimators": 260,
        "learning_rate": 0.025,
        "num_leaves": 28,
        "max_depth": 7,
        "min_child_samples": 32,
        "subsample": 0.85,
        "colsample_bytree": 0.78,
        "reg_alpha": 0.3,
        "reg_lambda": 2.0,
        "random_state": seed,
        "verbosity": -1,
        "n_jobs": max(1, min(8, (os.cpu_count() or 2) // 2)),
    }
    if objective == "quantile":
        kwargs["alpha"] = float(alpha)
    return LGBMRegressor(**kwargs)


def _lgbm_classifier(seed: int = 20260825, positive_weight: float = 1.0):
    from lightgbm import LGBMClassifier

    return LGBMClassifier(
        objective="binary", n_estimators=280, learning_rate=0.025,
        num_leaves=24, max_depth=7, min_child_samples=36,
        subsample=0.85, colsample_bytree=0.8, reg_alpha=0.3,
        reg_lambda=2.0, scale_pos_weight=max(1.0, float(positive_weight)),
        random_state=seed, verbosity=-1,
        n_jobs=max(1, min(8, (os.cpu_count() or 2) // 2)),
    )


def _lgbm_ranker(seed: int = 20260825):
    from lightgbm import LGBMRanker

    return LGBMRanker(
        objective="lambdarank", metric="ndcg", ndcg_at=[5],
        n_estimators=280, learning_rate=0.025, num_leaves=28,
        max_depth=7, min_child_samples=32, subsample=0.85,
        colsample_bytree=0.78, reg_alpha=0.3, reg_lambda=2.0,
        random_state=seed, verbosity=-1,
        n_jobs=max(1, min(8, (os.cpu_count() or 2) // 2)),
    )


def _relevance_labels(values: pd.Series, dates: pd.Series) -> np.ndarray:
    percentiles = daily_percentile(values, dates).fillna(0.0)
    return np.minimum(4, np.floor(percentiles.to_numpy(dtype=float) * 5)).astype(int)


def _group_sizes(dates: Sequence[Any]) -> list[int]:
    return pd.Series(dates).value_counts(sort=False).sort_index().astype(int).tolist()


class TierModelService:
    """Train, register and score the three panel objectives."""

    def __init__(self, model_dir: Path, cache_dir: Path):
        self.model_dir = Path(model_dir)
        self.cache_dir = Path(cache_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.panel_dir = self.cache_dir / "panel-store"
        self.panel_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_path = self.model_dir / "tier-models-v2.2.0.pkl"
        self.previous_artifact_path = self.model_dir / "tier-models-v2.2.0.previous.pkl"
        self.registry_path = self.model_dir / "tier-model-registry-v2.2.0.json"
        self.previous_registry_path = self.model_dir / "tier-model-registry-v2.2.0.previous.json"
        self._lock = threading.RLock()
        self._artifacts: Optional[Dict[str, TierArtifact]] = None

    def registry(self) -> Dict[str, Any]:
        artifacts = self._load_artifacts()
        return {
            "modelVersion": TIER_MODEL_VERSION,
            "objectives": {tier: artifacts.get(tier, TierArtifact(tier)).registry_payload() for tier in TIER_OBJECTIVES},
            "initializationRequired": not any(item.qualified for item in artifacts.values()),
            "rollbackAvailable": self.previous_artifact_path.exists(),
        }

    def train_universe(
        self,
        symbols: Iterable[str],
        *,
        metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
        objectives: Iterable[str] = TIER_OBJECTIVES,
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        requested = list(dict.fromkeys(normalize_a_share_symbol(item) for item in symbols))
        requested = [item for item in requested if item]
        selected_objectives = [item for item in objectives if item in TIER_OBJECTIVES] or list(TIER_OBJECTIVES)
        meta = {normalize_a_share_symbol(key): dict(value) for key, value in (metadata or {}).items() if normalize_a_share_symbol(key)}
        if progress:
            progress(5, f"正在读取所选 {len(requested)} 只股票面板，优先复用持久化缓存")
        rows: list[pd.DataFrame] = []
        loaded_symbols = 0
        for index, symbol in enumerate(requested):
            symbol_meta = meta.get(symbol) or {}
            allow_stale = bool(symbol_meta.get("delist_date")) or str(symbol_meta.get("list_status") or "").upper() == "D"
            cached = self._load_cached_symbol(symbol, allow_stale=allow_stale)
            if cached is None:
                raw, source = load_history_df(
                    symbol.split(".")[0], days=PANEL_HISTORY_DAYS, allow_stale=True
                )
                frame = normalize_daily_frame(raw if raw is not None else pd.DataFrame())
                if len(frame) >= 180:
                    cached = self._build_symbol_panel(symbol, frame, symbol_meta, source)
                    self._save_cached_symbol(symbol, cached)
            if cached is not None and not cached.empty:
                rows.append(cached)
                loaded_symbols += 1
            if progress and ((index + 1) % 10 == 0 or index + 1 == len(requested)):
                remaining = max(0, len(requested) - index - 1)
                progress(
                    5 + int(30 * (index + 1) / max(1, len(requested))),
                    f"面板数据 {index + 1}/{len(requested)}，有效 {loaded_symbols}，预计剩余 {remaining} 只",
                )
        if loaded_symbols < MIN_PANEL_SYMBOLS or not rows:
            reason = f"insufficient_panel_symbols:{loaded_symbols}/{MIN_PANEL_SYMBOLS}"
            # A transient data outage must not replace a previously qualified
            # champion. Report the failed attempt and leave the registry intact.
            return {
                "qualified": False,
                "reason": reason,
                "loadedSymbols": loaded_symbols,
                "objectives": self.registry()["objectives"],
                "championPreserved": True,
            }

        panel = pd.concat(rows, ignore_index=True, sort=False)
        panel = self._finalize_panel(panel)
        if len(panel) > PANEL_MAX_ROWS:
            panel = self._bounded_panel_sample(panel, PANEL_MAX_ROWS)
        artifacts = self._load_artifacts()
        self._backup_current_artifacts()
        training_outcomes: Dict[str, Any] = {}
        stage_ranges = {"conservative": (38, 56), "regular": (57, 76), "aggressive": (77, 96)}
        for tier in selected_objectives:
            start, end = stage_ranges[tier]
            if progress:
                progress(start, f"正在训练 {tier} 所选股票池面板模型（{loaded_symbols} 只）")
            try:
                if tier == "conservative":
                    artifact = self._train_conservative(panel)
                elif tier == "regular":
                    artifact = self._train_regular(panel)
                else:
                    artifact = self._train_aggressive(panel)
            except Exception as exc:
                artifact = TierArtifact(tier=tier, reason=f"training_failed:{type(exc).__name__}:{exc}")
            artifact.metadata = {
                **artifact.metadata,
                "trainingScope": {
                    "requestedSymbols": len(requested), "loadedSymbols": loaded_symbols,
                    "symbols": sorted(panel["symbol"].astype(str).unique().tolist()), "panelRows": len(panel),
                    "panelFrom": str(pd.Timestamp(panel["date"].min()).date()),
                    "panelThrough": str(pd.Timestamp(panel["date"].max()).date()),
                    "scopeNote": "验收仅代表所选股票池；当前成分回测仍可能受幸存者偏差影响。",
                },
            }
            previous = artifacts.get(tier)
            if not artifact.qualified and previous is not None and previous.qualified:
                previous.metadata = {
                    **(previous.metadata or {}),
                    "lastChallenger": artifact.registry_payload(),
                }
                previous.reason = "qualified_champion_preserved"
                artifacts[tier] = previous
                training_outcomes[tier] = {
                    "published": False,
                    "championPreserved": True,
                    "challenger": artifact.registry_payload(),
                }
                published_artifact = previous
            else:
                artifacts[tier] = artifact
                training_outcomes[tier] = {
                    "published": artifact.qualified,
                    "championPreserved": False,
                    "challenger": artifact.registry_payload(),
                }
                published_artifact = artifact
            self._save_artifacts(artifacts, create_backup=False)
            if progress:
                status = "通过并发布" if artifact.qualified else "未通过，保留原冠军" if published_artifact.qualified else "未通过"
                progress(end, f"{tier} 模型门槛：{status}")
        if progress:
            progress(98, "三档模型注册表已原子更新")
        registry = self.registry()
        return {
            "qualified": any(item.qualified for item in artifacts.values()),
            "loadedSymbols": loaded_symbols,
            "panelRows": len(panel),
            "objectives": registry["objectives"],
            "trainingOutcomes": training_outcomes,
        }

    def score_candidates(self, tier: str, candidates: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        if not candidates:
            return []
        artifact = self._load_artifacts().get(tier)
        if artifact is None or not artifact.qualified:
            return self._rule_fallback(tier, candidates, artifact)
        trained_symbols = set((artifact.metadata.get("trainingScope") or {}).get("symbols") or [])
        if trained_symbols and any(normalize_a_share_symbol(str(item.get("code") or "")) not in trained_symbols for item in candidates):
            # A scope-specific validation does not qualify a different stock
            # universe. Do not mix model and fallback percentiles in one rank.
            return self._rule_fallback(tier, candidates, replace(artifact, reason="outside_validated_training_scope"))
        feature_rows = []
        feature_histories: Dict[str, pd.DataFrame] = {}
        prepared: list[Dict[str, Any]] = []
        for candidate in candidates:
            symbol = normalize_a_share_symbol(str(candidate.get("code") or ""))
            if not symbol:
                continue
            raw, source = load_history_df(symbol.split(".")[0], days=180)
            frame = normalize_daily_frame(raw if raw is not None else pd.DataFrame())
            features = build_features(frame)
            if features.empty:
                continue
            row = features.iloc[-1].to_dict()
            row.update(self._candidate_exposures(candidate, frame))
            feature_rows.append(row)
            feature_histories[symbol] = features
            item = dict(candidate)
            item["quantDataSource"] = source
            prepared.append(item)
        if not prepared:
            return self._rule_fallback(tier, candidates, artifact)
        matrix = pd.DataFrame(feature_rows).reindex(columns=artifact.feature_names)
        matrix = matrix.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
        master_horizons = (5, 20) if tier == "regular" else (1,) if tier == "aggressive" else ()
        for horizon in master_horizons:
            batch = predict_master_checkpoint_batch(
                self.model_dir / f"master-h{horizon}.pt", feature_histories, horizon
            )
            predictions = batch.get("predictions") or {}
            for item in prepared:
                symbol = normalize_a_share_symbol(str(item.get("code") or ""))
                if symbol in predictions:
                    item.setdefault("_masterPredictions", {})[str(horizon)] = float(predictions[symbol])
                elif (artifact.learned_weights or artifact.models.get("modelWeights")):
                    item.setdefault("_masterUnavailable", {})[str(horizon)] = str(batch.get("reason") or "prediction_missing")
        if tier == "conservative":
            self._score_conservative(artifact, prepared, matrix)
        elif tier == "regular":
            self._score_regular(artifact, prepared, matrix)
        else:
            self._score_aggressive(artifact, prepared, matrix)
        self._assign_percentiles(prepared, tier)
        return sorted(prepared, key=lambda item: float(item.get("tierScore") or 0.0), reverse=True)

    def score_local_five_day(self, candidates):
        artifact = self._load_artifacts().get('regular')
        gate = (artifact.metrics if artifact else {}).get('localFiveDay', {})
        if not gate.get('qualified') or gate.get('version') != 'king-local-5d-v1':
            fallback = replace(artifact, reason='local_five_day_gate_not_passed') if artifact else None
            return self._rule_fallback('regular', candidates, fallback)
        ranked = self.score_candidates('regular', candidates)
        if not ranked or any(x.get('modelStatus') != 'qualified' for x in ranked):
            return ranked
        for item in ranked:
            item['tierRawSignal'] = item['rawRankSignal5d']
            item['expectedHoldingRange'] = 'T至T+5'
        self._assign_percentiles(ranked, 'regular')
        return sorted(ranked, key=lambda x: (-x['rawRankSignal5d'], str(x.get('code'))))

    def analyze_symbol(
        self,
        symbol: str,
        *,
        stock_name: str = "",
        candidate: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Explain all three objectives for one stock without inventing a percentile."""
        normalized = normalize_a_share_symbol(symbol)
        if not normalized:
            return {"available": False, "reason": "prediction_only_supports_a_shares", "tiers": {}}
        raw, source = load_history_df(normalized.split(".")[0], days=180)
        frame = normalize_daily_frame(raw if raw is not None else pd.DataFrame())
        features = build_features(frame)
        if features.empty:
            return {"available": False, "reason": "insufficient_feature_history", "dataSource": source, "tiers": {}}
        base = dict(candidate or {})
        base.update({"code": normalized, "name": stock_name or base.get("name") or normalized})
        master_predictions: Dict[str, float] = {}
        for horizon in (1, 5, 20):
            batch = predict_master_checkpoint_batch(
                self.model_dir / f"master-h{horizon}.pt", {normalized: features}, horizon
            )
            value = (batch.get("predictions") or {}).get(normalized)
            if value is not None:
                master_predictions[str(horizon)] = float(value)
        if master_predictions:
            base["_masterPredictions"] = master_predictions
        row = features.iloc[-1].to_dict()
        row.update(self._candidate_exposures(base, frame))
        artifacts = self._load_artifacts()
        analyses: Dict[str, Any] = {}
        for tier in TIER_OBJECTIVES:
            artifact = artifacts.get(tier, TierArtifact(tier=tier))
            item = dict(base)
            trained_symbols = set((artifact.metadata.get("trainingScope") or {}).get("symbols") or [])
            if artifact.qualified and (not trained_symbols or normalized in trained_symbols):
                matrix = pd.DataFrame([row]).reindex(columns=artifact.feature_names)
                values = matrix.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
                if tier == "conservative":
                    holder = [item]
                    self._score_conservative(artifact, holder, values)
                elif tier == "regular":
                    self._score_regular(artifact, [item], values)
                else:
                    self._score_aggressive(artifact, [item], values)
            else:
                fallback_artifact = replace(artifact, reason="outside_validated_training_scope") if artifact.qualified else artifact
                item = self._rule_fallback(tier, [item], fallback_artifact)[0]
            item.pop("tierScore", None)
            item["scoreMeaning"] = "单股原始信号；未与当日同板块候选做横截面比较，因此不生成 0–100 百分位。"
            analyses[tier] = self._analysis_payload(tier, item, artifact)
        return {
            "available": True,
            "symbol": normalized,
            "dataSource": source,
            "dataDate": frame["date"].iloc[-1].date().isoformat(),
            "modelVersion": TIER_MODEL_VERSION,
            "tiers": analyses,
        }

    @staticmethod
    def _analysis_payload(tier: str, item: Mapping[str, Any], artifact: TierArtifact) -> Dict[str, Any]:
        common = {
            "tier": tier,
            "modelStatus": item.get("modelStatus"),
            "modelVersion": item.get("modelVersion"),
            "trainedThrough": item.get("trainedThrough"),
            "calibratedThrough": item.get("calibratedThrough"),
            "tierScore": None,
            "scoreMeaning": item.get("scoreMeaning"),
            "rawSignal": item.get("tierRawSignal"),
            "learnedModelWeights": item.get("learnedModelWeights") or {},
            "topContributors": item.get("topContributors") or [],
            "degradedReasons": item.get("degradedReasons") or [],
            "releaseMetrics": artifact.metrics,
        }
        if tier == "conservative":
            common.update({
                "label": "稳健 SafeBound",
                "horizon": "20–60 个交易日",
                "result": {
                    "returnLowerBound20d": item.get("returnLowerBound20d"),
                    "returnLowerBound60d": item.get("returnLowerBound60d"),
                    "predictedVolatility20d": item.get("predictedVolatility20d"),
                    "predictedVolatilityUpper20d": item.get("predictedVolatilityUpper20d"),
                    "drawdownQuantile20d": item.get("drawdownQuantile20d"),
                    "riskGateResults": item.get("riskGateResults") or {},
                },
                "formula": "U_safe=min[ln(1+LCB20)/20, ln(1+LCB60)/60]；预测波动上界≤30%且回撤Q10≥-8%才通过。",
                "calculationSteps": [
                    "分别用 10% 分位回归估计未来 20/60 日收益下界，并用滚动残差做时间序列共形修正。",
                    "波动挑战者（LightGBM、EWMA、HAR）只按样本外误差学习非负权重；预测上界用于硬门槛。",
                    "两个周期取更弱的日均对数收益下界作为原始排序信号。",
                ],
            })
        elif tier == "regular":
            common.update({
                "label": "常规 BalancedRank",
                "horizon": "5–20 个交易日",
                "result": {
                    "rawRankSignal5d": item.get("rawRankSignal5d"),
                    "rawRankSignal20d": item.get("rawRankSignal20d"),
                    "riskAdjustedSignal": item.get("riskAdjustedSignal"),
                    "industryNeutral": item.get("industryNeutral"),
                },
                "formula": "S_regular=w5·S5+w20·S20，w≥0 且 Σw=1；模型与周期权重均由滚动样本外排序指标学习。",
                "calculationSteps": [
                    "未来收益先按行业、对数流通规模和 Beta 做逐日横截面中性化，再按预测波动缩放。",
                    "LambdaRank、DoubleEnsemble 式难样本重加权模型和 Ridge 基线分别产生 5/20 日信号；通过独立门槛的 MASTER 才会作为时序/横截面挑战者加入。",
                    "外层滚动预测再按时间拆成元排名学习段和未参与权重选择的发布门槛段；无样本外增益模型权重归零。",
                ],
            })
        else:
            common.update({
                "label": "激进 LimitPulse",
                "horizon": "未来 1–3 个交易日",
                "result": {
                    "hazards": item.get("touchHazards") or {},
                    "touchProbability1d": item.get("touchProbability1d"),
                    "touchProbability3d": item.get("touchProbability3d"),
                    "probabilityInterval": item.get("probabilityInterval"),
                    "baseRate3d": item.get("baseRate3d"),
                    "probabilityLift": item.get("probabilityLift"),
                },
                "formula": "P3d=1-(1-h1)(1-h2)(1-h3)；R=logit(P3d)-logit(板块基准率)。",
                "calculationSteps": [
                    "h1 是第 1 日首次触板概率；h2/h3 分别以此前尚未触板为条件。",
                    "稀有事件 LightGBM、透明 Logistic，以及门槛合格时由 MASTER 产生并经逐风险率校准的时序信号，共同参加 Log Loss 元组合。",
                    "校准/权重学习段严格早于最终发布门槛段；MASTER 的收益信号先校准成条件风险率，绝不直接显示为触板概率。",
                    "最终用相对板块基准率的对数优势差排序；概率只在发布门槛全部通过时展示。",
                ],
            })
        return common

    def _build_symbol_panel(self, symbol: str, frame: pd.DataFrame, metadata: Mapping[str, Any], source: str) -> pd.DataFrame:
        features = build_features(frame)
        output = features.copy()
        output["date"] = frame["date"].to_numpy()
        output["symbol"] = symbol
        output["industry"] = str(metadata.get("industry") or metadata.get("bk_name") or "unknown")
        output["board"] = board_from_symbol(symbol)
        output["data_source"] = source
        output["limit_rule_version"] = "point-in-time-v2.2.0"
        close = frame["close"].to_numpy(dtype=float)
        high = frame["high"].to_numpy(dtype=float)
        amount = frame["amount"].to_numpy(dtype=float)
        output["log_size"] = np.log1p(pd.Series(amount).rolling(20, min_periods=5).median().to_numpy())
        for horizon in (5, 20, 60):
            output[f"forward_return_{horizon}"] = forward_return(close, horizon)
        output["label_end_20"] = frame["date"].shift(-20).to_numpy()
        output["forward_volatility_20"] = forward_realized_volatility(close, 20)
        output["forward_drawdown_20"] = forward_max_drawdown(close, 20)
        current_name = str(metadata.get("name") or "").upper()
        dates = frame["date"].to_numpy()
        ratios = np.asarray([limit_ratio_for_symbol_on_date(symbol, value) for value in dates], dtype=float)
        output["first_limit_touch"] = first_limit_touch_labels(close, high, ratio=ratios, horizon=3)
        if "ST" in current_name:
            # The current ST name must not be projected backward through the
            # whole history. Until point-in-time ST status is available, keep
            # the stock for return/risk panels but exclude its touch labels.
            output["first_limit_touch"] = np.nan
        # Same-day market breadth is observable at t. Never derive a feature
        # from ``first_limit_touch``, which is explicitly a future label.
        touched_today = np.zeros(len(close), dtype=float)
        touched_today[0] = np.nan
        for index in range(1, len(close)):
            limit_price = rounded_limit_price(close[index - 1], ratios[index])
            touched_today[index] = float(math.isfinite(limit_price) and high[index] + 1e-9 >= limit_price)
        output["limit_touched_today"] = touched_today
        return output.replace([np.inf, -np.inf], np.nan)

    def _finalize_panel(self, panel: pd.DataFrame) -> pd.DataFrame:
        panel = panel.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
        market_returns = panel.groupby("date", sort=False)["ret_1"].transform("median")
        panel["market_return_1"] = market_returns
        panel["market_breadth"] = panel.groupby("date", sort=False)["ret_1"].transform(lambda x: float((x > 0).mean()))
        panel["market_volatility"] = panel.groupby("date", sort=False)["ret_1"].transform("std").fillna(0.0)
        panel["limit_breadth"] = panel.groupby("date", sort=False)["limit_touched_today"].transform("mean").fillna(0.0)
        panel["beta_60"] = 1.0
        for _, positions in panel.groupby("symbol", sort=False).groups.items():
            idx = np.asarray(list(positions), dtype=int)
            stock = panel.loc[idx, "ret_1"].astype(float)
            market = panel.loc[idx, "market_return_1"].astype(float)
            covariance = stock.rolling(60, min_periods=20).cov(market)
            variance = market.rolling(60, min_periods=20).var().replace(0, np.nan)
            panel.loc[idx, "beta_60"] = (covariance / variance).clip(-3, 5).fillna(1.0).to_numpy()
        return panel

    @staticmethod
    def _bounded_panel_sample(panel: pd.DataFrame, limit: int) -> pd.DataFrame:
        positives = panel[panel["first_limit_touch"].fillna(0) > 0]
        remainder_limit = max(0, int(limit) - len(positives))
        remainder = panel.drop(index=positives.index)
        if len(remainder) > remainder_limit:
            remainder = remainder.sample(n=remainder_limit, random_state=20260825)
        return pd.concat([positives, remainder], ignore_index=True).sort_values(["date", "symbol"], kind="stable")

    @staticmethod
    def _feature_names(panel: pd.DataFrame) -> list[str]:
        excluded = {
            "date", "symbol", "industry", "board", "data_source", "first_limit_touch",
            "forward_return_5", "forward_return_20", "forward_return_60",
            "forward_volatility_20", "forward_drawdown_20",
        }
        return [name for name in panel.columns if name not in excluded and pd.api.types.is_numeric_dtype(panel[name])]

    def _master_oos_vector(self, data: pd.DataFrame, horizon: int) -> tuple[np.ndarray, Optional[dict]]:
        """Align a qualified MASTER validation cross-section to panel rows."""
        payload = read_checkpoint_payload(self.model_dir / f"master-h{int(horizon)}.pt", int(horizon))
        output = np.full(len(data), np.nan, dtype=float)
        if payload is None:
            return output, None
        stored = payload.get("oos_predictions") or {}
        dates = pd.to_datetime(data["date"], errors="coerce")
        for position, (symbol, current_date) in enumerate(zip(data["symbol"].astype(str), dates)):
            if pd.isna(current_date):
                continue
            value = (stored.get(symbol) or {}).get(current_date.date().isoformat())
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed):
                output[position] = parsed
        return output, payload

    def _train_conservative(self, panel: pd.DataFrame) -> TierArtifact:
        feature_names = self._feature_names(panel)
        needed = feature_names + ["date", "forward_return_20", "forward_return_60", "forward_volatility_20", "forward_drawdown_20"]
        data = panel[needed].replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
        splits = purged_walk_forward_splits(data["date"], purge=60, min_train_dates=min(260, max(120, data["date"].nunique() // 2)))
        if len(splits) < 2:
            return TierArtifact(tier="conservative", reason="insufficient_walk_forward_splits")
        oos: Dict[str, np.ndarray] = {key: np.full(len(data), np.nan) for key in ("q20", "q60", "vol_lgbm", "vol_ewma", "vol_har", "drawdown")}
        for fold, (train_idx, test_idx) in enumerate(splits):
            x_train = data.iloc[train_idx][feature_names].fillna(0).to_numpy(dtype=float)
            x_test = data.iloc[test_idx][feature_names].fillna(0).to_numpy(dtype=float)
            for horizon in (20, 60):
                model = _lgbm_regressor(objective="quantile", alpha=0.10, seed=20260825 + fold + horizon)
                model.fit(x_train, data.iloc[train_idx][f"forward_return_{horizon}"])
                oos[f"q{horizon}"][test_idx] = model.predict(x_test)
            vol_model = _lgbm_regressor(objective="huber", seed=20260920 + fold)
            vol_model.fit(x_train, data.iloc[train_idx]["forward_volatility_20"])
            oos["vol_lgbm"][test_idx] = np.clip(vol_model.predict(x_test), 0, 3)
            oos["vol_ewma"][test_idx] = np.clip(data.iloc[test_idx]["volatility_20"].to_numpy() * math.sqrt(252), 0, 3)
            from sklearn.linear_model import Ridge
            har_names = [name for name in ("volatility_5", "volatility_20", "volatility_60") if name in feature_names]
            har = Ridge(alpha=4.0).fit(data.iloc[train_idx][har_names].fillna(0), data.iloc[train_idx]["forward_volatility_20"])
            oos["vol_har"][test_idx] = np.clip(har.predict(data.iloc[test_idx][har_names].fillna(0)), 0, 3)
            drawdown = _lgbm_regressor(objective="quantile", alpha=0.10, seed=20261020 + fold)
            drawdown.fit(x_train, data.iloc[train_idx]["forward_drawdown_20"])
            oos["drawdown"][test_idx] = drawdown.predict(x_test)
        valid = np.isfinite(oos["q20"]) & np.isfinite(oos["q60"]) & np.isfinite(oos["drawdown"])
        if int(valid.sum()) < 100:
            return TierArtifact(tier="conservative", reason="insufficient_oos_rows")
        meta_mask, gate_mask = nested_meta_gate_masks(data.loc[valid, "date"], minimum_dates=20, purge=60)
        if int(meta_mask.sum()) < 50 or int(gate_mask.sum()) < 50:
            return TierArtifact(tier="conservative", reason="insufficient_nested_validation_rows")
        all_vol_predictions = {name: values[valid] for name, values in oos.items() if name.startswith("vol_")}
        vol_truth = data.loc[valid, "forward_volatility_20"].to_numpy()
        base_mae = float(np.mean(np.abs(vol_truth[meta_mask] - all_vol_predictions["vol_lgbm"][meta_mask])))
        vol_predictions = {"vol_lgbm": all_vol_predictions["vol_lgbm"]}
        for name in ("vol_ewma", "vol_har"):
            challenger_mae = float(np.mean(np.abs(vol_truth[meta_mask] - all_vol_predictions[name][meta_mask])))
            if challenger_mae < base_mae:
                vol_predictions[name] = all_vol_predictions[name]
        vol_weights = learn_nonnegative_weights(
            {name: values[meta_mask] for name, values in vol_predictions.items()}, vol_truth[meta_mask]
        )
        combined_vol = combine_predictions(vol_predictions, vol_weights)
        vol_error = np.abs(vol_truth - combined_vol)
        vol_radius = float(np.quantile(vol_error[meta_mask], 0.90))
        corrections = {}
        pinball = {}
        baseline_pinball = {}
        for horizon in (20, 60):
            truth = data.loc[valid, f"forward_return_{horizon}"].to_numpy()
            prediction = oos[f"q{horizon}"][valid]
            corrections[str(horizon)] = max(0.0, float(np.quantile(prediction[meta_mask] - truth[meta_mask], 0.90)))
            calibrated = prediction - corrections[str(horizon)]
            pinball[str(horizon)] = _pinball(truth[gate_mask], calibrated[gate_mask], 0.10)
            baseline_value = float(np.quantile(truth[meta_mask], 0.10))
            baseline_pinball[str(horizon)] = _pinball(
                truth[gate_mask], np.full(int(gate_mask.sum()), baseline_value), 0.10
            )
        coverage20 = float(np.mean(
            data.loc[valid, "forward_return_20"].to_numpy()[gate_mask]
            >= (oos["q20"][valid] - corrections["20"])[gate_mask]
        ))
        safe_oos = np.minimum(
            np.log1p(np.clip(oos["q20"][valid] - corrections["20"], -0.95, None)) / 20.0,
            np.log1p(np.clip(oos["q60"][valid] - corrections["60"], -0.95, None)) / 60.0,
        )
        safe_evaluation = pd.DataFrame({
            "date": data.loc[valid, "date"].to_numpy()[gate_mask],
            "signal": safe_oos[gate_mask],
            "volatility": data.loc[valid, "forward_volatility_20"].to_numpy()[gate_mask],
            "drawdown": data.loc[valid, "forward_drawdown_20"].to_numpy()[gate_mask],
            "return20": data.loc[valid, "forward_return_20"].to_numpy()[gate_mask],
        })
        vol_improvements: list[float] = []
        drawdown_improvements: list[float] = []
        top_returns: list[float] = []
        for _, group in safe_evaluation.groupby("date", sort=False):
            if len(group) < 5:
                continue
            top = group.nlargest(5, "signal")
            vol_improvements.append(float(group["volatility"].mean() - top["volatility"].mean()))
            drawdown_improvements.append(float(top["drawdown"].mean() - group["drawdown"].mean()))
            top_returns.append(float(top["return20"].median()))
        vol_improvement_lower = _bootstrap_mean_lower(vol_improvements, seed=20260827)
        drawdown_improvement_lower = _bootstrap_mean_lower(drawdown_improvements, seed=20260828)
        top_return_median = float(np.median(top_returns)) if top_returns else -1.0
        qualified = (
            pinball["20"] < baseline_pinball["20"]
            and pinball["60"] < baseline_pinball["60"]
            and coverage20 >= 0.80
            and vol_improvement_lower >= 0
            and drawdown_improvement_lower >= 0
            and top_return_median >= 0
        )
        x_all = data[feature_names].fillna(0).to_numpy(dtype=float)
        models: Dict[str, Any] = {}
        for horizon in (20, 60):
            for alpha in (0.10, 0.50, 0.90):
                model = _lgbm_regressor(objective="quantile", alpha=alpha, seed=20260825 + horizon + int(alpha * 100))
                model.fit(x_all, data[f"forward_return_{horizon}"])
                models[f"return_q{int(alpha * 100)}_{horizon}"] = model
        models["vol_lgbm"] = _lgbm_regressor(objective="huber", seed=20260920).fit(x_all, data["forward_volatility_20"])
        from sklearn.linear_model import Ridge
        har_names = [name for name in ("volatility_5", "volatility_20", "volatility_60") if name in feature_names]
        models["vol_har"] = Ridge(alpha=4.0).fit(data[har_names].fillna(0), data["forward_volatility_20"])
        models["vol_har_names"] = har_names
        models["drawdown_q10"] = _lgbm_regressor(objective="quantile", alpha=0.10, seed=20261020).fit(x_all, data["forward_drawdown_20"])
        last_date = pd.to_datetime(data["date"]).max().date().isoformat()
        return TierArtifact(
            tier="conservative", trained_through=last_date, calibrated_through=last_date,
            feature_names=feature_names, models=models, learned_weights={f"volatility:{k}": v for k, v in vol_weights.items()},
            metrics={
                "pinballLoss": pinball,
                "baselinePinballLoss": baseline_pinball,
                "coverage20d": coverage20,
                "volatilityMae": float(np.mean(vol_error[gate_mask])),
                "top5VolatilityImprovementLower95": vol_improvement_lower,
                "top5DrawdownImprovementLower95": drawdown_improvement_lower,
                "top5ReturnMedian": top_return_median,
            },
            qualified=qualified, reason="qualified" if qualified else "release_gate_failed",
            metadata={
                "returnCorrections": corrections,
                "volatilityRadius": vol_radius,
                "nestedValidation": {
                    "calibrationRows": int(meta_mask.sum()),
                    "releaseGateRows": int(gate_mask.sum()),
                },
            },
        )

    def _train_regular(self, panel: pd.DataFrame) -> TierArtifact:
        feature_names = self._feature_names(panel)
        # ``log_size`` and ``beta_60`` are numeric model features as well as
        # explicit neutralisation exposures. Selecting them twice creates a
        # pandas frame with duplicate labels and makes the OLS exposure lookup
        # ambiguous during a real full-panel training run.
        needed = unique_column_names(
            feature_names,
            ["date", "symbol", "industry", "log_size", "beta_60", "forward_return_5", "forward_return_20", "label_end_20"],
        )
        data = panel[needed].replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
        for horizon in (5, 20):
            neutral = cross_sectional_neutralize(data, f"forward_return_{horizon}")
            ex_ante_vol = pd.to_numeric(data.get("volatility_20"), errors="coerce").clip(lower=0.005)
            data[f"regular_target_{horizon}"] = neutral / ex_ante_vol
        data = data.dropna(subset=["regular_target_5", "regular_target_20"]).reset_index(drop=True)
        splits = purged_walk_forward_splits(data["date"], purge=20, min_train_dates=min(220, max(100, data["date"].nunique() // 2)))
        if len(splits) < 2:
            return TierArtifact(tier="regular", reason="insufficient_walk_forward_splits")
        oos: Dict[str, np.ndarray] = {}
        candidate_names: Dict[int, list[str]] = {5: ["ranker", "ridge", "double"], 20: ["ranker", "ridge", "double"]}
        for horizon in (5, 20):
            for name in candidate_names[horizon]:
                oos[f"{name}_{horizon}"] = np.full(len(data), np.nan)
        for fold, (train_idx, test_idx) in enumerate(splits):
            # Sparse histories can make 20 bars span more than 20 market dates.
            test_start = data.iloc[test_idx]["date"].min()
            train_idx = train_idx[(data.iloc[train_idx]["label_end_20"] < test_start).to_numpy()]
            train = data.iloc[train_idx].sort_values("date", kind="stable")
            test = data.iloc[test_idx].sort_values("date", kind="stable")
            x_train = train[feature_names].fillna(0)
            x_test = test[feature_names].fillna(0)
            from sklearn.linear_model import Ridge
            for horizon in (5, 20):
                target_name = f"regular_target_{horizon}"
                relevance = _relevance_labels(train[target_name], train["date"])
                ranker = _lgbm_ranker(20260825 + fold + horizon)
                ranker.fit(x_train, relevance, group=_group_sizes(train["date"]))
                rank_prediction = ranker.predict(x_test)
                ridge = Ridge(alpha=8.0).fit(x_train, train[target_name])
                ridge_prediction = ridge.predict(x_test)
                importance = np.asarray(ranker.feature_importances_, dtype=float)
                keep_count = max(8, int(len(feature_names) * 0.60))
                keep_idx = np.argsort(-importance)[:keep_count]
                selected_names = [feature_names[index] for index in keep_idx]
                train_error = np.abs(train[target_name].to_numpy() - ranker.predict(x_train))
                sample_weight = 1.0 + percentile_scores(train_error) / 100.0
                double = _lgbm_ranker(20261825 + fold + horizon)
                double.fit(train[selected_names].fillna(0), relevance, group=_group_sizes(train["date"]), sample_weight=sample_weight)
                original_positions = test.index.to_numpy()
                oos[f"ranker_{horizon}"][original_positions] = rank_prediction
                oos[f"ridge_{horizon}"][original_positions] = ridge_prediction
                oos[f"double_{horizon}"][original_positions] = double.predict(test[selected_names].fillna(0))
        master_payloads: Dict[str, dict] = {}
        base_valid = np.ones(len(data), dtype=bool)
        for values in oos.values():
            base_valid &= np.isfinite(values)
        for horizon in (5, 20):
            master_values, payload = self._master_oos_vector(data, horizon)
            coverage = int(np.sum(base_valid & np.isfinite(master_values)))
            if payload is not None and coverage >= 100:
                oos[f"master_{horizon}"] = master_values
                candidate_names[horizon].append("master")
                master_payloads[str(horizon)] = payload
        valid = np.ones(len(data), dtype=bool)
        for values in oos.values():
            valid &= np.isfinite(values)
        if int(valid.sum()) < 100:
            return TierArtifact(tier="regular", reason="insufficient_oos_rows")
        meta_mask, gate_mask = nested_meta_gate_masks(
            data.loc[valid, "date"], minimum_dates=100, purge=20,
            label_end_dates=data.loc[valid, "label_end_20"],
        )
        if int(meta_mask.sum()) < 50 or int(gate_mask.sum()) < 50:
            return TierArtifact(tier="regular", reason="insufficient_nested_validation_rows")
        model_weights: Dict[str, Dict[str, float]] = {}
        horizon_predictions: Dict[str, np.ndarray] = {}
        valid_dates = data.loc[valid, "date"].reset_index(drop=True)
        for horizon in (5, 20):
            candidates = {
                name: daily_percentile(
                    pd.Series(oos[f"{name}_{horizon}"][valid]), valid_dates
                ).fillna(0.0).to_numpy(dtype=float)
                for name in candidate_names[horizon]
            }
            target_rank = daily_percentile(data.loc[valid, f"regular_target_{horizon}"], data.loc[valid, "date"]).to_numpy()
            model_weights[str(horizon)] = learn_nonnegative_weights(
                {name: values[meta_mask] for name, values in candidates.items()}, target_rank[meta_mask]
            )
            horizon_predictions[str(horizon)] = combine_predictions(candidates, model_weights[str(horizon)])
        horizon_weights = self._learn_horizon_weights(
            {name: values[meta_mask] for name, values in horizon_predictions.items()},
            data.loc[valid, "regular_target_5"].to_numpy()[meta_mask],
            data.loc[valid, "regular_target_20"].to_numpy()[meta_mask],
            data.loc[valid, "date"].to_numpy()[meta_mask],
        )
        combined = combine_predictions(horizon_predictions, horizon_weights)
        target20 = data.loc[valid, "regular_target_20"].to_numpy()[gate_mask]
        dates = data.loc[valid, "date"].to_numpy()[gate_mask]
        gate_prediction = combined[gate_mask]
        horizon_reports = {
            str(horizon): holding_period_report(
                dates, data.loc[valid, f"regular_target_{horizon}"].to_numpy()[gate_mask],
                data.loc[valid, f"forward_return_{horizon}"].to_numpy()[gate_mask],
                gate_prediction, horizon=horizon, cost=TRANSACTION_COST,
            ) for horizon in (5, 20)
        }
        local_five_day = holding_period_report(
            dates, data.loc[valid, 'regular_target_5'].to_numpy()[gate_mask],
            data.loc[valid, 'forward_return_5'].to_numpy()[gate_mask],
            horizon_predictions['5'][gate_mask], horizon=5, cost=TRANSACTION_COST)
        local_five_day['version'] = 'king-local-5d-v1'
        rank_ic, rank_std = _rank_ic(target20, gate_prediction, dates)
        ndcg = _ndcg_at_k(target20, gate_prediction, dates)
        top5 = _topk_return(data.loc[valid, "forward_return_20"].to_numpy()[gate_mask], gate_prediction, dates)
        qualified = ndcg > 0.50 and all(report["qualified"] for report in horizon_reports.values())
        # Keep existing API names, but expose the dependence-aware lower bounds.
        rank_ic_lower = (horizon_reports["20"]["rankIcInterval95"] or [None])[0]
        top5_lower = (horizon_reports["20"]["netReturnInterval95"] or [None])[0]
        full = data.sort_values("date", kind="stable")
        models: Dict[str, Any] = {"modelWeights": model_weights, "horizonWeights": horizon_weights, "selectedFeatures": {}}
        from sklearn.linear_model import Ridge
        for horizon in (5, 20):
            target_name = f"regular_target_{horizon}"
            relevance = _relevance_labels(full[target_name], full["date"])
            ranker = _lgbm_ranker(20260825 + horizon).fit(full[feature_names].fillna(0), relevance, group=_group_sizes(full["date"]))
            ridge = Ridge(alpha=8.0).fit(full[feature_names].fillna(0), full[target_name])
            importance = np.asarray(ranker.feature_importances_, dtype=float)
            selected = [feature_names[index] for index in np.argsort(-importance)[: max(8, int(len(feature_names) * 0.60))]]
            error = np.abs(full[target_name].to_numpy() - ranker.predict(full[feature_names].fillna(0)))
            double = _lgbm_ranker(20261825 + horizon).fit(
                full[selected].fillna(0), relevance, group=_group_sizes(full["date"]),
                sample_weight=1.0 + percentile_scores(error) / 100.0,
            )
            models[f"ranker_{horizon}"] = ranker
            models[f"ridge_{horizon}"] = ridge
            models[f"double_{horizon}"] = double
            models["selectedFeatures"][str(horizon)] = selected
        last_date = pd.to_datetime(data["date"]).max().date().isoformat()
        flattened = {f"{horizon}:{name}": value for horizon, weights in model_weights.items() for name, value in weights.items()}
        flattened.update({f"horizon:{name}": value for name, value in horizon_weights.items()})
        return TierArtifact(
            tier="regular", trained_through=last_date, calibrated_through=last_date,
            feature_names=feature_names, models=models, learned_weights=flattened,
            metrics={
                "rankIc": rank_ic,
                "rankIcStd": rank_std,
                "rankIcLower95": rank_ic_lower,
                "ndcgAt5": ndcg,
                "top5NetReturn": top5,
                "top5NetReturnLower95": top5_lower,
                "holdingPeriods": horizon_reports,
                "localFiveDay": local_five_day,
            },
            qualified=qualified, reason="qualified" if qualified else "release_gate_failed",
            metadata={
                "industryNeutral": True,
                "expectedHoldingRange": "5–20d",
                "validationVersion": "balancedrank-block-v1",
                "returnBasis": "收盘到收盘的重叠标签收益；不是可成交组合净值",
                "masterChallengers": {
                    horizon: {
                        "modelVersion": payload.get("model_version"),
                        "validationThrough": payload.get("validation_through"),
                        "metrics": payload.get("metrics") or {},
                    }
                    for horizon, payload in master_payloads.items()
                },
                "nestedValidation": {
                    "weightRows": int(meta_mask.sum()),
                    "releaseGateRows": int(gate_mask.sum()),
                    "purgeTradingDates": 20,
                    "weightThrough": str(pd.Timestamp(valid_dates[meta_mask].max()).date()),
                    "gateFrom": str(pd.Timestamp(valid_dates[gate_mask].min()).date()),
                    "gateThrough": str(pd.Timestamp(valid_dates[gate_mask].max()).date()),
                },
            },
        )

    @staticmethod
    def _learn_horizon_weights(predictions: Mapping[str, np.ndarray], y5: np.ndarray, y20: np.ndarray, dates: np.ndarray) -> Dict[str, float]:
        names = list(predictions)
        if len(names) == 1:
            return {names[0]: 1.0}
        best_key: Optional[tuple[float, ...]] = None
        best = {names[0]: 0.5, names[1]: 0.5}
        for step in range(21):
            weight = step / 20.0
            combined = predictions[names[0]] * weight + predictions[names[1]] * (1.0 - weight)
            ic5, _ = _rank_ic(y5, combined, dates)
            ic20, _ = _rank_ic(y20, combined, dates)
            ndcg5 = _ndcg_at_k(y5, combined, dates)
            ndcg20 = _ndcg_at_k(y20, combined, dates)
            key = (min(ic5, ic20), (ic5 + ic20) / 2.0, min(ndcg5, ndcg20), (ndcg5 + ndcg20) / 2.0)
            if best_key is None or key > best_key:
                best_key = key
                best = {names[0]: weight, names[1]: 1.0 - weight}
        return {name: value for name, value in best.items() if value > 0}

    def _train_aggressive(self, panel: pd.DataFrame) -> TierArtifact:
        feature_names = self._feature_names(panel)
        needed = unique_column_names(feature_names, ["date", "symbol", "board", "first_limit_touch"])
        data = panel[needed].replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
        data["event_3d"] = (data["first_limit_touch"] > 0).astype(int)
        if int(data["event_3d"].sum()) < 30:
            return TierArtifact(tier="aggressive", reason="insufficient_limit_events")
        splits = purged_walk_forward_splits(data["date"], purge=3, min_train_dates=min(220, max(100, data["date"].nunique() // 2)))
        if len(splits) < 2:
            return TierArtifact(tier="aggressive", reason="insufficient_walk_forward_splits")
        oos: Dict[str, Dict[int, np.ndarray]] = {
            name: {day: np.full(len(data), np.nan) for day in (1, 2, 3)}
            for name in ("lgbm", "logistic")
        }
        from sklearn.linear_model import LogisticRegression
        for fold, (train_idx, test_idx) in enumerate(splits):
            for day in (1, 2, 3):
                train_touch = data.iloc[train_idx]["first_limit_touch"].to_numpy(dtype=int)
                train_at_risk = (train_touch == 0) | (train_touch >= day)
                y_train = (train_touch[train_at_risk] == day).astype(int)
                if len(np.unique(y_train)) < 2 or int(y_train.sum()) < 5:
                    continue
                x_train = data.iloc[train_idx[train_at_risk]][feature_names].fillna(0).to_numpy(dtype=float)
                x_test = data.iloc[test_idx][feature_names].fillna(0).to_numpy(dtype=float)
                positive_weight = float((len(y_train) - y_train.sum()) / max(1, y_train.sum()))
                lgbm = _lgbm_classifier(20260825 + fold * 10 + day, positive_weight).fit(x_train, y_train)
                logistic = LogisticRegression(
                    C=0.3, class_weight="balanced", max_iter=600, random_state=20261825 + fold * 10 + day
                ).fit(x_train, y_train)
                oos["lgbm"][day][test_idx] = lgbm.predict_proba(x_test)[:, 1]
                oos["logistic"][day][test_idx] = logistic.predict_proba(x_test)[:, 1]
        base_valid = np.ones(len(data), dtype=bool)
        for name in oos:
            for day in (1, 2, 3):
                base_valid &= np.isfinite(oos[name][day])
        master_values, master_payload = self._master_oos_vector(data, 1)
        if master_payload is not None and int(np.sum(base_valid & np.isfinite(master_values))) >= 60:
            oos["master"] = {day: master_values.copy() for day in (1, 2, 3)}
        else:
            master_payload = None
        valid = np.ones(len(data), dtype=bool)
        for name in oos:
            for day in (1, 2, 3):
                valid &= np.isfinite(oos[name][day])
        if int(valid.sum()) < 60:
            return TierArtifact(tier="aggressive", reason="insufficient_hazard_oos_predictions")
        meta_mask, gate_mask = nested_meta_gate_masks(data.loc[valid, "date"], minimum_dates=20, purge=3)
        if int(meta_mask.sum()) < 30 or int(gate_mask.sum()) < 30:
            return TierArtifact(tier="aggressive", reason="insufficient_nested_validation_rows")
        target = data.loc[valid, "event_3d"].to_numpy(dtype=int)
        calibrators: Dict[str, Any] = {}
        family_p3: Dict[str, np.ndarray] = {}
        touch_all = data["first_limit_touch"].to_numpy(dtype=int)
        for name in oos:
            family_hazards = []
            for day in (1, 2, 3):
                valid_touch = touch_all[valid]
                at_risk = (valid_touch == 0) | (valid_touch >= day)
                calibration_mask = meta_mask & at_risk
                day_target = (valid_touch[calibration_mask] == day).astype(int)
                key = f"{name}:{day}"
                raw_score = (
                    oos[name][day][valid]
                    if name == "master"
                    else _safe_logit(oos[name][day][valid])
                )
                calibrators[key] = _fit_sigmoid(raw_score[calibration_mask], day_target)
                family_hazards.append(
                    _calibrated_probability(calibrators[key], raw_score)
                )
            family_p3[name] = cumulative_hazard_probability(*family_hazards)
        weights = learn_nonnegative_weights(
            {name: values[meta_mask] for name, values in family_p3.items()},
            target[meta_mask],
            objective="logloss",
        )
        combined_hazards: Dict[int, np.ndarray] = {}
        for day in (1, 2, 3):
            combined_hazards[day] = combine_predictions(
                {
                    name: _calibrated_probability(
                        calibrators[f"{name}:{day}"],
                        oos[name][day][valid] if name == "master" else _safe_logit(oos[name][day][valid]),
                    )
                    for name in oos
                },
                weights,
            )
        probability = cumulative_hazard_probability(*(combined_hazards[day] for day in (1, 2, 3)))
        probability_radius = float(np.quantile(np.abs(target[meta_mask] - probability[meta_mask]), 0.80))
        metrics = _binary_metrics(
            target[gate_mask], probability[gate_mask], data.loc[valid, "date"].to_numpy()[gate_mask]
        )
        qualified = (
            metrics["prAuc"] > metrics["prevalence"]
            and metrics["brierSkill"] > 0
            and metrics["logLoss"] < metrics["baselineLogLoss"]
            and metrics["liftAt5Lower95"] > 1.0
        )
        x_all = data[feature_names].fillna(0).to_numpy(dtype=float)
        y_all = data["event_3d"].to_numpy(dtype=int)
        hazard_models: Dict[str, Any] = {}
        touch = data["first_limit_touch"].to_numpy(dtype=int)
        for day in (1, 2, 3):
            at_risk = (touch == 0) | (touch >= day)
            y_day = (touch[at_risk] == day).astype(int)
            positive_weight = float((len(y_day) - y_day.sum()) / max(1, y_day.sum()))
            hazard_models[f"lgbm_{day}"] = _lgbm_classifier(20260825 + day, positive_weight).fit(x_all[at_risk], y_day)
            hazard_models[f"logistic_{day}"] = LogisticRegression(
                C=0.3, class_weight="balanced", max_iter=600, random_state=20261825 + day
            ).fit(x_all[at_risk], y_day)
        base_rates = data.groupby("board", sort=False)["event_3d"].mean().to_dict()
        last_date = pd.to_datetime(data["date"]).max().date().isoformat()
        return TierArtifact(
            tier="aggressive", trained_through=last_date, calibrated_through=last_date,
            feature_names=feature_names,
            models={**hazard_models, "calibrators": calibrators},
            learned_weights=weights, metrics=metrics, qualified=qualified,
            reason="qualified" if qualified else "release_gate_failed",
            metadata={
                "baseRates": base_rates,
                "eventCount": int(y_all.sum()),
                "sampleCount": len(y_all),
                "probabilityRadius80": probability_radius,
                "masterChallenger": {
                    "modelVersion": master_payload.get("model_version"),
                    "validationThrough": master_payload.get("validation_through"),
                    "metrics": master_payload.get("metrics") or {},
                } if master_payload is not None else {},
                "nestedValidation": {
                    "calibrationRows": int(meta_mask.sum()),
                    "releaseGateRows": int(gate_mask.sum()),
                },
            },
        )

    def _score_conservative(self, artifact: TierArtifact, candidates: list[Dict[str, Any]], matrix: np.ndarray) -> None:
        q20 = artifact.models["return_q10_20"].predict(matrix) - float((artifact.metadata.get("returnCorrections") or {}).get("20", 0.0))
        q60 = artifact.models["return_q10_60"].predict(matrix) - float((artifact.metadata.get("returnCorrections") or {}).get("60", 0.0))
        vol_predictions = {
            "vol_lgbm": np.clip(artifact.models["vol_lgbm"].predict(matrix), 0, 3),
            "vol_ewma": np.asarray([max(0.0, float(item.get("volatility_20d_pct") or 0.0) / 100.0) for item in candidates]),
        }
        har_names = artifact.models.get("vol_har_names") or []
        if har_names:
            indices = [artifact.feature_names.index(name) for name in har_names]
            vol_predictions["vol_har"] = np.clip(artifact.models["vol_har"].predict(matrix[:, indices]), 0, 3)
        weights = {key.split(":", 1)[1]: value for key, value in artifact.learned_weights.items() if key.startswith("volatility:")}
        vol = combine_predictions(vol_predictions, weights)
        vol_upper = vol + float(artifact.metadata.get("volatilityRadius") or 0.0)
        drawdown = artifact.models["drawdown_q10"].predict(matrix)
        safe_signal = np.minimum(np.log1p(np.clip(q20, -0.95, None)) / 20.0, np.log1p(np.clip(q60, -0.95, None)) / 60.0)
        for index, item in enumerate(candidates):
            gates = {
                "predictedVolatilityUpperLte30": bool(vol_upper[index] <= 0.30),
                "drawdownQ10GteMinus8": bool(drawdown[index] >= -0.08),
            }
            item.update({
                "modelStatus": "qualified", "modelVersion": artifact.model_version,
                "trainedThrough": artifact.trained_through, "calibratedThrough": artifact.calibrated_through,
                "returnLowerBound20d": round(float(q20[index]), 6),
                "returnLowerBound60d": round(float(q60[index]), 6),
                "predictedVolatility20d": round(float(vol[index]), 6),
                "predictedVolatilityUpper20d": round(float(vol_upper[index]), 6),
                "drawdownQuantile20d": round(float(drawdown[index]), 6),
                "riskGateResults": gates, "tierRawSignal": float(safe_signal[index]),
                "learnedModelWeights": artifact.learned_weights,
                "confidenceGrade": "A" if all(gates.values()) else "C",
                "topContributors": self._feature_contributors(artifact, matrix[index], "return_q10_20"),
                "degradedReasons": [] if all(gates.values()) else ["预测风险门槛未通过，不进入稳健正式榜。"],
            })
        candidates[:] = [item for item in candidates if all((item.get("riskGateResults") or {}).values())]

    def _score_regular(self, artifact: TierArtifact, candidates: list[Dict[str, Any]], matrix: np.ndarray) -> None:
        horizon_predictions: Dict[str, np.ndarray] = {}
        model_weights = artifact.models.get("modelWeights") or {}
        degraded: list[str] = []
        for horizon in (5, 20):
            selected = artifact.models["selectedFeatures"][str(horizon)]
            indices = [artifact.feature_names.index(name) for name in selected]
            predictions = {
                "ranker": artifact.models[f"ranker_{horizon}"].predict(matrix),
                "ridge": artifact.models[f"ridge_{horizon}"].predict(matrix),
                "double": artifact.models[f"double_{horizon}"].predict(matrix[:, indices]),
            }
            predictions = {name: percentile_scores(values) / 100.0 for name, values in predictions.items()}
            weights = dict(model_weights[str(horizon)])
            if weights.get("master", 0) > 0:
                master_values = np.asarray([
                    float((item.get("_masterPredictions") or {}).get(str(horizon), np.nan))
                    for item in candidates
                ])
                if np.isfinite(master_values).all():
                    predictions["master"] = percentile_scores(master_values) / 100.0
                else:
                    weights.pop("master", None)
                    degraded.append(f"{horizon}日 MASTER 当前横截面不可用，按已验证基础模型权重重新归一。")
            horizon_predictions[str(horizon)] = combine_predictions(predictions, weights)
        combined = combine_predictions(horizon_predictions, artifact.models.get("horizonWeights") or {"5": 0.5, "20": 0.5})
        rank5 = percentile_scores(horizon_predictions["5"])
        rank20 = percentile_scores(horizon_predictions["20"])
        for index, item in enumerate(candidates):
            item.update({
                "modelStatus": "qualified", "modelVersion": artifact.model_version,
                "trainedThrough": artifact.trained_through, "calibratedThrough": artifact.calibrated_through,
                "excessRank5d": round(float(rank5[index]), 4), "excessRank20d": round(float(rank20[index]), 4),
                "rawRankSignal5d": round(float(horizon_predictions["5"][index]), 6),
                "rawRankSignal20d": round(float(horizon_predictions["20"][index]), 6),
                "industryNeutral": True, "riskAdjustedSignal": round(float(combined[index]), 6),
                "expectedHoldingRange": "5–20d", "tierRawSignal": float(combined[index]),
                "learnedModelWeights": artifact.learned_weights, "confidenceGrade": "B" if degraded else "A",
                "topContributors": self._feature_contributors(artifact, matrix[index], "ranker_20"),
                "degradedReasons": list(dict.fromkeys(degraded)),
            })
            item.pop("_masterPredictions", None)
            item.pop("_masterUnavailable", None)

    def _score_aggressive(self, artifact: TierArtifact, candidates: list[Dict[str, Any]], matrix: np.ndarray) -> None:
        calibrators = artifact.models.get("calibrators") or {}
        family_hazards: Dict[str, Dict[int, np.ndarray]] = {name: {} for name in ("lgbm", "logistic")}
        effective_weights = dict(artifact.learned_weights)
        degraded: list[str] = []
        if effective_weights.get("master", 0) > 0:
            master_values = np.asarray([
                float((item.get("_masterPredictions") or {}).get("1", np.nan)) for item in candidates
            ])
            if np.isfinite(master_values).all():
                family_hazards["master"] = {}
            else:
                effective_weights.pop("master", None)
                degraded.append("MASTER 当前横截面不可用，触板概率按已验证基础模型权重重新归一。")
        if not any(value > 0 for value in effective_weights.values()):
            effective_weights = {"lgbm": 0.5, "logistic": 0.5}
        hazards: Dict[int, np.ndarray] = {}
        for day in (1, 2, 3):
            for name in family_hazards:
                raw = (
                    master_values
                    if name == "master"
                    else _safe_logit(artifact.models[f"{name}_{day}"].predict_proba(matrix)[:, 1])
                )
                family_hazards[name][day] = _calibrated_probability(
                    calibrators.get(f"{name}:{day}"), raw
                )
            hazards[day] = combine_predictions(
                {name: family_hazards[name][day] for name in family_hazards}, effective_weights
            )
        p1 = hazards[1]
        p3 = cumulative_hazard_probability(*(hazards[day] for day in (1, 2, 3)))
        family_p3 = {
            name: cumulative_hazard_probability(*(values[day] for day in (1, 2, 3)))
            for name, values in family_hazards.items()
        }
        dispersion = np.std(np.column_stack(list(family_p3.values())), axis=1)
        radius = np.maximum(1.28 * dispersion, float(artifact.metadata.get("probabilityRadius80") or 0.0))
        lower = np.clip(p3 - radius, 0, 1)
        upper = np.clip(p3 + radius, 0, 1)
        base_rates = artifact.metadata.get("baseRates") or {}
        for index, item in enumerate(candidates):
            symbol = normalize_a_share_symbol(str(item.get("code") or ""))
            board = board_from_symbol(symbol) if symbol else "main"
            base = max(1e-5, float(base_rates.get(board) or artifact.metrics.get("prevalence") or 1e-5))
            raw_signal = float(_safe_logit(np.asarray([p3[index]]))[0] - _safe_logit(np.asarray([base]))[0])
            item.update({
                "modelStatus": "qualified", "modelVersion": artifact.model_version,
                "trainedThrough": artifact.trained_through, "calibratedThrough": artifact.calibrated_through,
                "touchProbability1d": round(float(p1[index]), 6), "touchProbability3d": round(float(p3[index]), 6),
                "touchHazards": {str(day): round(float(hazards[day][index]), 6) for day in (1, 2, 3)},
                "probabilityInterval": {"low": round(float(lower[index]), 6), "high": round(float(upper[index]), 6)},
                "baseRate3d": round(base, 6), "probabilityLift": round(float(p3[index] / base), 4),
                "tierRawSignal": raw_signal, "learnedModelWeights": artifact.learned_weights,
                "confidenceGrade": "B" if degraded or dispersion[index] > 0.08 else "A",
                "topContributors": self._feature_contributors(artifact, matrix[index], "lgbm_1"),
                "degradedReasons": list(degraded),
            })
            item.pop("_masterPredictions", None)
            item.pop("_masterUnavailable", None)

    @staticmethod
    def _feature_contributors(artifact: TierArtifact, row: np.ndarray, model_key: str, limit: int = 5) -> list[Dict[str, Any]]:
        model = artifact.models.get(model_key)
        importance = getattr(model, "feature_importances_", None)
        if importance is None:
            coefficients = getattr(model, "coef_", None)
            if coefficients is not None:
                importance = np.abs(np.asarray(coefficients).reshape(-1))
        if importance is None or len(importance) != len(artifact.feature_names):
            order = np.argsort(-np.abs(row))[:limit]
        else:
            order = np.argsort(-np.asarray(importance, dtype=float))[:limit]
        return [{"feature": artifact.feature_names[index], "value": round(float(row[index]), 6)} for index in order]

    @staticmethod
    def _candidate_exposures(candidate: Mapping[str, Any], frame: pd.DataFrame) -> Dict[str, float]:
        amount = pd.to_numeric(frame.get("amount"), errors="coerce") if "amount" in frame else pd.Series(dtype=float)
        return {
            "log_size": float(np.log1p(amount.tail(20).median())) if len(amount) else 0.0,
            "beta_60": float(candidate.get("beta_60") or 1.0),
            "market_return_1": float(candidate.get("market_return_1") or 0.0),
            "market_breadth": float(candidate.get("market_breadth") or 0.5),
            "market_volatility": float(candidate.get("market_volatility") or 0.0),
            "limit_breadth": float(candidate.get("limit_breadth") or 0.0),
        }

    def _rule_fallback(self, tier: str, candidates: list[Dict[str, Any]], artifact: Optional[TierArtifact]) -> list[Dict[str, Any]]:
        output = [dict(item) for item in candidates]
        reason = artifact.reason if artifact is not None else "model_not_initialized"
        for item in output:
            rule_value = None
            for key in ("score", "final_score", "screen_score"):
                try:
                    parsed = float(item.get(key))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed):
                    rule_value = parsed
                    break
            degraded = [f"三档面板模型未通过发布门槛：{reason}；当前仅显示规则观察分。"]
            if rule_value is None:
                degraded.append("该股票没有当日规则候选分，单股建议不补造观察分。")
            item.update({
                "modelStatus": "rule_fallback", "modelVersion": artifact.model_version if artifact else TIER_MODEL_VERSION,
                "trainedThrough": artifact.trained_through if artifact else "", "calibratedThrough": artifact.calibrated_through if artifact else "",
                "tierRawSignal": rule_value,
                "learnedModelWeights": {}, "confidenceGrade": "D", "topContributors": [],
                "degradedReasons": degraded,
            })
            if tier == "regular":
                item.update({"industryNeutral": False, "expectedHoldingRange": "5–20d"})
        self._assign_percentiles(output, tier)
        return sorted(output, key=lambda item: float(item.get("tierScore") or 0.0), reverse=True)

    @staticmethod
    def _assign_percentiles(candidates: list[Dict[str, Any]], tier: str) -> None:
        for group_name in ("kechuang", "nonKeChuang"):
            grouped = [item for item in candidates if ((board_from_symbol(normalize_a_share_symbol(str(item.get("code") or ""))) == "star") == (group_name == "kechuang"))]
            scores = percentile_scores([float(item.get("tierRawSignal") or 0.0) for item in grouped])
            for item, score in zip(grouped, scores):
                item["tierScore"] = round(float(score), 4)
                item["scoreMeaning"] = {
                    "conservative": "稳健候选中未来收益下界的当日百分位",
                    "regular": "均衡候选中行业中性风险调整超额收益的当日百分位",
                    "aggressive": "激进候选中相对板块基准触板能力的当日百分位",
                }[tier]

    def _load_cached_symbol(self, symbol: str, *, allow_stale: bool = False) -> Optional[pd.DataFrame]:
        path = self.panel_dir / f"{symbol.replace('.', '_')}.pkl"
        try:
            frame = pd.read_pickle(path)
            if not {"first_limit_touch", "limit_touched_today", "forward_return_60", "limit_rule_version", "label_end_20"}.issubset(frame.columns):
                return None
            if not (frame["limit_rule_version"].astype(str) == "point-in-time-v2.2.0").all():
                return None
            latest = pd.to_datetime(frame["date"], errors="coerce").max()
            if pd.notna(latest) and (allow_stale or (pd.Timestamp(date.today()) - latest.normalize()).days <= 7):
                return frame
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return None

    def _save_cached_symbol(self, symbol: str, frame: pd.DataFrame) -> None:
        path = self.panel_dir / f"{symbol.replace('.', '_')}.pkl"
        temporary = path.with_suffix(".tmp")
        frame.to_pickle(temporary)
        temporary.replace(path)

    def _load_artifacts(self) -> Dict[str, TierArtifact]:
        with self._lock:
            if self._artifacts is not None:
                return self._artifacts
            try:
                payload = pickle.loads(self.artifact_path.read_bytes())
                if isinstance(payload, dict):
                    self._artifacts = payload
                    return payload
            except (OSError, ValueError, TypeError, pickle.PickleError):
                pass
            self._artifacts = {tier: TierArtifact(tier=tier) for tier in TIER_OBJECTIVES}
            return self._artifacts

    def _backup_current_artifacts(self) -> None:
        if self.artifact_path.exists():
            previous_tmp = self.previous_artifact_path.with_suffix(".tmp")
            previous_tmp.write_bytes(self.artifact_path.read_bytes())
            previous_tmp.replace(self.previous_artifact_path)
        if self.registry_path.exists():
            previous_registry_tmp = self.previous_registry_path.with_suffix(".tmp")
            previous_registry_tmp.write_bytes(self.registry_path.read_bytes())
            previous_registry_tmp.replace(self.previous_registry_path)

    def _save_artifacts(self, artifacts: Dict[str, TierArtifact], *, create_backup: bool = True) -> None:
        with self._lock:
            if create_backup:
                self._backup_current_artifacts()
            temporary = self.artifact_path.with_suffix(".tmp")
            temporary.write_bytes(pickle.dumps(artifacts, protocol=pickle.HIGHEST_PROTOCOL))
            temporary.replace(self.artifact_path)
            registry = {
                "modelVersion": TIER_MODEL_VERSION,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "objectives": {tier: artifact.registry_payload() for tier, artifact in artifacts.items()},
            }
            registry_tmp = self.registry_path.with_suffix(".tmp")
            registry_tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            registry_tmp.replace(self.registry_path)
            self._artifacts = artifacts


_SERVICE: Optional[TierModelService] = None
_SERVICE_LOCK = threading.Lock()


def get_tier_model_service(model_dir: Optional[Path] = None, cache_dir: Optional[Path] = None) -> TierModelService:
    global _SERVICE
    if model_dir is not None or cache_dir is not None:
        return TierModelService(
            Path(model_dir or "data/models/stock-king"),
            Path(cache_dir or "data/screening/quant"),
        )
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                models = Path(os.getenv("MODEL_DIR") or "data/models") / "stock-king"
                cache = Path(os.getenv("SCREENING_DATA_DIR") or "data/screening") / "quant"
                _SERVICE = TierModelService(models, cache)
    return _SERVICE
