"""Champion/challenger forecasts with walk-forward validation and conformal intervals."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from src.quant.features import build_features, forward_return, normalize_daily_frame
from src.quant.master import predict_master_checkpoint, train_master_checkpoint
from src.services.history_loader import load_history_df

logger = logging.getLogger(__name__)
SUPPORTED_HORIZONS = (1, 5, 20)
MIN_BARS = 160
TRANSACTION_COST = 0.0015


@dataclass
class CandidateMetrics:
    model_name: str
    model_version: str
    horizon: int
    rank_ic: float
    positive_windows: int
    total_windows: int
    brier_score: float
    baseline_brier: float
    net_sharpe: float
    coverage: float
    qualified: bool
    reason: str = ""


class NumpyRidge:
    def __init__(self, alpha: float = 8.0):
        self.alpha = alpha
        self.mean: Optional[np.ndarray] = None
        self.scale: Optional[np.ndarray] = None
        self.coef: Optional[np.ndarray] = None

    def fit(self, x: np.ndarray, y: np.ndarray) -> "NumpyRidge":
        self.mean = np.nanmean(x, axis=0)
        self.scale = np.nanstd(x, axis=0)
        self.scale[self.scale < 1e-9] = 1.0
        z = np.nan_to_num((x - self.mean) / self.scale)
        design = np.column_stack([np.ones(len(z)), z])
        penalty = np.eye(design.shape[1]) * self.alpha
        penalty[0, 0] = 0.0
        self.coef = np.linalg.pinv(design.T @ design + penalty) @ design.T @ y
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        if self.mean is None or self.scale is None or self.coef is None:
            raise RuntimeError("model is not fitted")
        z = np.nan_to_num((x - self.mean) / self.scale)
        return np.column_stack([np.ones(len(z)), z]) @ self.coef


class QuantPredictionService:
    def __init__(self, model_dir: Optional[Path] = None, cache_dir: Optional[Path] = None):
        base_models = Path(model_dir or os.getenv("MODEL_DIR") or "data/models") / "stock-king"
        base_cache = Path(cache_dir or os.getenv("SCREENING_DATA_DIR") or "data/screening") / "quant"
        self.model_dir = base_models
        self.cache_dir = base_cache
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._forecast_cache_path = self.cache_dir / "forecasts.json"
        self._metrics_path = self.model_dir / "metrics.json"
        # Lazy import avoids a module cycle: tier_models reuses the canonical
        # A-share symbol and board helpers defined below in this module.
        from src.quant.tier_models import get_tier_model_service

        self.tier_models = get_tier_model_service(self.model_dir, self.cache_dir)

    def forecast(self, symbol: str, horizon: int, *, stock_name: str = "", force: bool = False) -> Dict[str, Any]:
        horizon = int(horizon)
        normalized = normalize_a_share_symbol(symbol)
        if horizon not in SUPPORTED_HORIZONS:
            return unavailable_forecast(symbol, horizon, "unsupported_horizon")
        if not normalized:
            return unavailable_forecast(symbol, horizon, "prediction_only_supports_a_shares")
        if any(marker in stock_name.upper() for marker in ("ST", "退市", "整理")):
            return unavailable_forecast(normalized, horizon, "risk_security_excluded")
        return self._forecast_normalized(normalized, horizon, force=force)

    def _forecast_normalized(
        self,
        normalized: str,
        horizon: int,
        *,
        force: bool,
        frame: Optional[pd.DataFrame] = None,
        source: str = "",
    ) -> Dict[str, Any]:
        cache_key = f"{normalized}:{horizon}"
        if not force:
            cached = self._load_forecast_cache().get(cache_key)
            if isinstance(cached, dict) and cached.get("generatedDate") == date.today().isoformat():
                return cached

        if frame is None:
            raw, source = load_history_df(normalized.split(".")[0], days=820)
            frame = normalize_daily_frame(raw if raw is not None else pd.DataFrame())
        if len(frame) < MIN_BARS + horizon:
            return unavailable_forecast(normalized, horizon, f"insufficient_history:{len(frame)}/{MIN_BARS + horizon}", source=source)
        latest_date = frame["date"].iloc[-1].date()
        if (date.today() - latest_date).days > 12:
            return unavailable_forecast(normalized, horizon, f"stale_data:{latest_date.isoformat()}", source=source)

        snapshot = self._fit_forecast(frame, normalized, horizon, source)
        with self._lock:
            cache = self._load_json(self._forecast_cache_path, {})
            cache[cache_key] = snapshot
            self._write_json(self._forecast_cache_path, cache)
            metrics = self._load_json(self._metrics_path, {})
            metrics[cache_key] = snapshot.get("metrics")
            self._write_json(self._metrics_path, metrics)
        return snapshot

    def forecast_all_horizons(self, symbol: str, *, stock_name: str = "", force: bool = False) -> list[Dict[str, Any]]:
        normalized = normalize_a_share_symbol(symbol)
        if not normalized:
            return [unavailable_forecast(symbol, horizon, "prediction_only_supports_a_shares") for horizon in SUPPORTED_HORIZONS]
        if any(marker in stock_name.upper() for marker in ("ST", "退市", "整理")):
            return [unavailable_forecast(normalized, horizon, "risk_security_excluded") for horizon in SUPPORTED_HORIZONS]
        if not force:
            cache = self._load_forecast_cache()
            cached = [cache.get(f"{normalized}:{horizon}") for horizon in SUPPORTED_HORIZONS]
            if all(isinstance(item, dict) and item.get("generatedDate") == date.today().isoformat() for item in cached):
                return cached
        raw, source = load_history_df(normalized.split(".")[0], days=820)
        frame = normalize_daily_frame(raw if raw is not None else pd.DataFrame())
        return [
            self._forecast_normalized(normalized, horizon, force=force, frame=frame, source=source)
            for horizon in SUPPORTED_HORIZONS
        ]

    def metrics(self) -> Dict[str, Any]:
        payload = self._load_json(self._metrics_path, {})
        registry = self.tier_models.registry()
        return {
            "models": payload,
            "modelCount": len(payload),
            "horizons": list(SUPPORTED_HORIZONS),
            "modelVersion": registry.get("modelVersion"),
            "tiers": registry.get("objectives") or {},
            "initializationRequired": registry.get("initializationRequired", True),
            "validationMode": "persisted_metrics_only",
            "trainingTriggered": False,
            "performanceKind": "research_labels_not_executable_nav",
            "cacheReview": {
                "modelReuse": True,
                "message": "仅复用已保存模型与验收报告，不下载行情、不重训；刷新不会产生新验收结果。",
                "accountBacktestAvailable": True,
                "unqualifiedPolicy": "research_only_no_ai_score_averaging",
            },
        }

    def train_universe(
        self,
        symbols: Iterable[str],
        horizons: Iterable[int] = SUPPORTED_HORIZONS,
        *,
        train_master: bool = False,
        metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        objectives: Optional[Iterable[str]] = None,
        legacy_forecasts: bool = False,
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        requested = [normalize_a_share_symbol(item) for item in symbols]
        requested = list(dict.fromkeys(item for item in requested if item))
        valid_horizons = [int(item) for item in horizons if int(item) in SUPPORTED_HORIZONS]
        if not valid_horizons:
            valid_horizons = list(SUPPORTED_HORIZONS)
        master_frames: Dict[str, pd.DataFrame] = {}
        if train_master and len(requested) >= 20:
            for index, symbol in enumerate(requested):
                raw, _ = load_history_df(symbol.split(".")[0], days=1900, allow_stale=True)
                frame = normalize_daily_frame(raw if raw is not None else pd.DataFrame())
                if len(frame) >= MIN_BARS + max(valid_horizons):
                    master_frames[symbol] = frame
                if progress and (index + 1) % 25 == 0:
                    progress(2 + int(10 * (index + 1) / len(requested)), "正在准备 MASTER 横截面训练数据")
        master_results = []
        if train_master and len(master_frames) >= 20:
            for index, horizon in enumerate(valid_horizons):
                if progress:
                    progress(12 + int(10 * index / len(valid_horizons)), f"正在训练 {horizon} 日 MASTER 挑战者")
                master_results.append(train_master_checkpoint(
                    master_frames, horizon, self.model_dir / f"master-h{horizon}.pt"
                ))

        def tier_progress(value: int, message: str) -> None:
            if progress:
                progress(24 + int(max(0, min(100, value)) * 0.73), message)

        tier_result = self.tier_models.train_universe(
            requested,
            metadata=metadata,
            objectives=objectives or ("conservative", "regular", "aggressive"),
            progress=tier_progress if train_master else progress,
        )
        if not legacy_forecasts:
            return {
                "requestedSymbols": len(requested),
                "horizons": valid_horizons,
                "tierTraining": tier_result,
                "masterRequested": train_master,
                "masterTraining": master_results,
                "legacyForecastsRequested": False,
            }

        results = []
        progress_start = 45 if train_master else 12
        for index, symbol in enumerate(requested):
            for horizon in valid_horizons:
                results.append(self.forecast(symbol, horizon, force=True))
            if progress and ((index + 1) % 10 == 0 or index + 1 == len(requested)):
                progress(
                    progress_start + int((96 - progress_start) * (index + 1) / len(requested)),
                    f"已完成 {index + 1}/{len(requested)} 只股票的滚动预测",
                )
        available = sum(1 for item in results if item.get("available"))
        return {
            "requestedSymbols": len(requested), "forecastCount": len(results),
            "availableCount": available, "unavailableCount": len(results) - available,
            "horizons": valid_horizons, "masterRequested": train_master,
            "masterTraining": master_results, "resultSample": results[:50],
            "tierTraining": tier_result,
            "legacyForecastsRequested": True,
        }

    def _fit_forecast(self, frame: pd.DataFrame, symbol: str, horizon: int, source: str) -> Dict[str, Any]:
        features = build_features(frame)
        labels = forward_return(frame, horizon)
        dataset = features.copy()
        dataset["__label"] = labels
        dataset = dataset.dropna()
        if len(dataset) < MIN_BARS:
            return unavailable_forecast(symbol, horizon, f"insufficient_feature_rows:{len(dataset)}/{MIN_BARS}", source=source)
        feature_names = [name for name in dataset.columns if name != "__label"]
        x = dataset[feature_names].to_numpy(dtype=float)
        y = dataset["__label"].to_numpy(dtype=float)
        folds = expanding_splits(len(dataset), min_train=max(100, len(dataset) // 2), folds=4)
        if len(folds) < 3:
            return unavailable_forecast(symbol, horizon, "insufficient_walk_forward_windows", source=source)

        factories = {"ridge": lambda: NumpyRidge(alpha=8.0)}
        lightgbm_factory = make_lightgbm_factory(horizon)
        if lightgbm_factory is not None:
            factories["lightgbm"] = lightgbm_factory
        predictions: Dict[str, list[np.ndarray]] = {name: [] for name in factories}
        actuals: list[np.ndarray] = []
        train_prevalence: list[np.ndarray] = []
        for train_idx, test_idx in folds:
            actual = y[test_idx]
            actuals.append(actual)
            train_prevalence.append(class_prevalence(y[train_idx], horizon))
            for name, factory in factories.items():
                model = factory()
                model.fit(x[train_idx], y[train_idx])
                predictions[name].append(np.asarray(model.predict(x[test_idx]), dtype=float))
        y_oos = np.concatenate(actuals)
        baseline_brier = prevalence_brier(actuals, train_prevalence, horizon)
        evaluated: Dict[str, CandidateMetrics] = {}
        for name, chunks in predictions.items():
            pred = np.concatenate(chunks)
            evaluated[name] = evaluate_candidate(name, pred, y_oos, chunks, actuals, horizon, baseline_brier)

        # A transparent momentum baseline participates in the same competition.
        momentum_chunks = []
        for _, test_idx in folds:
            rows = dataset.iloc[test_idx]
            momentum_chunks.append(np.clip(rows["ret_20"].to_numpy() * (horizon / 20.0), -0.25, 0.25))
        momentum = np.concatenate(momentum_chunks)
        evaluated["momentum"] = evaluate_candidate(
            "momentum", momentum, y_oos, momentum_chunks, actuals, horizon, baseline_brier
        )

        master = predict_master_checkpoint(self.model_dir / f"master-h{horizon}.pt", features, horizon)
        if master.get("available"):
            master_metrics = master.get("metrics") or {}
            evaluated["master"] = CandidateMetrics(
                model_name="master",
                model_version=str(master.get("modelVersion") or f"stock-king-master-h{horizon}-v2.0.0"),
                horizon=horizon,
                rank_ic=float(master_metrics.get("rankIc") or 0.0),
                positive_windows=int(master_metrics.get("positiveWindows") or 0),
                total_windows=int(master_metrics.get("totalWindows") or 0),
                brier_score=float(master_metrics.get("brierScore") or 1.0),
                baseline_brier=float(master_metrics.get("baselineBrier") or 1.0),
                net_sharpe=float(master_metrics.get("netSharpe") or 0.0),
                coverage=float(master_metrics.get("coverage") or 0.0),
                qualified=bool(master_metrics.get("qualified")),
                reason=str(master_metrics.get("reason") or "checkpoint_gate_failed"),
            )
        else:
            evaluated["master"] = CandidateMetrics(
                model_name="master", model_version="", horizon=horizon,
                rank_ic=0.0, positive_windows=0, total_windows=0,
                brier_score=1.0, baseline_brier=1.0, net_sharpe=0.0,
                coverage=0.0, qualified=False, reason=str(master.get("reason") or "checkpoint_unavailable"),
            )

        qualified = [item for item in evaluated.values() if item.qualified]
        if not qualified:
            reasons = "; ".join(f"{name}:{metrics.reason}" for name, metrics in evaluated.items())
            result = unavailable_forecast(symbol, horizon, "model_gate_failed:" + reasons, source=source)
            result["candidateMetrics"] = {name: asdict(metrics) for name, metrics in evaluated.items()}
            return result
        qualified.sort(key=lambda item: (item.rank_ic, -item.brier_score, item.net_sharpe), reverse=True)
        champion = qualified[0]
        weights = nonnegative_weights(qualified)

        latest_features = build_features(frame).iloc[[-1]][feature_names].to_numpy(dtype=float)
        final_predictions: Dict[str, float] = {}
        for item in qualified:
            if item.model_name == "momentum":
                final_predictions[item.model_name] = float(np.clip(features.iloc[-1]["ret_20"] * (horizon / 20.0), -0.25, 0.25))
            elif item.model_name == "master":
                final_predictions[item.model_name] = float(master["prediction"])
            else:
                model = factories[item.model_name]()
                model.fit(x, y)
                final_predictions[item.model_name] = float(model.predict(latest_features)[0])
        expected = sum(final_predictions[name] * weight for name, weight in weights.items())
        if champion.model_name == "master":
            residuals = np.asarray(master.get("validationResiduals") or [], dtype=float)
        else:
            champion_oos = np.concatenate(predictions.get(champion.model_name, momentum_chunks))
            residuals = y_oos - champion_oos
        interval_radius = adaptive_conformal_radius(residuals, coverage=0.8)
        probabilities = calibrated_probabilities(expected, residuals, horizon)
        latest_date = frame["date"].iloc[-1].date().isoformat()
        model_version = f"stock-king-{champion.model_name}-h{horizon}-v2.0.0"
        top_features = feature_importance_proxy(feature_names, x, y)
        return {
            "symbol": {"code": symbol, "market": "cn", "board": board_from_symbol(symbol)},
            "horizon": horizon,
            "asOfDate": latest_date,
            "generatedDate": date.today().isoformat(),
            "probabilityUp": probabilities["up"],
            "probabilityFlat": probabilities["flat"],
            "probabilityDown": probabilities["down"],
            "expectedExcessReturn": expected,
            "intervalLow": expected - interval_radius,
            "intervalHigh": expected + interval_radius,
            "intervalCoverage": 0.8,
            "modelVersion": model_version,
            "modelWeights": weights,
            "keyFeatures": top_features,
            "metrics": camel_metrics(champion, model_version),
            "candidateMetrics": {name: asdict(metrics) for name, metrics in evaluated.items()},
            "dataSource": source,
            "available": True,
            "unavailableReason": "",
        }

    def _load_forecast_cache(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_json(self._forecast_cache_path, {})

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return default

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)


def normalize_a_share_symbol(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")) and raw[2:].isdigit():
        raw = f"{raw[2:]}.{raw[:2]}"
    if raw.isdigit() and len(raw) == 6:
        exchange = "SH" if raw.startswith(("5", "6")) else "BJ" if raw.startswith(("4", "8", "9")) else "SZ"
        raw = f"{raw}.{exchange}"
    if not any(raw.endswith("." + suffix) for suffix in ("SH", "SZ", "BJ")):
        return ""
    code = raw.split(".")[0]
    return raw if len(code) == 6 and code.isdigit() else ""


def board_from_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    if exchange == "SH" and code.startswith(("688", "689")):
        return "star"
    if exchange == "SZ" and code.startswith("30"):
        return "chinext"
    if exchange == "BJ":
        return "bse"
    return "main"


def expanding_splits(size: int, min_train: int, folds: int) -> list[tuple[np.ndarray, np.ndarray]]:
    remaining = size - min_train
    test_size = max(10, remaining // folds)
    result = []
    start = min_train
    while start < size and len(result) < folds:
        end = size if len(result) == folds - 1 else min(size, start + test_size)
        if end - start < 5:
            break
        result.append((np.arange(0, start), np.arange(start, end)))
        start = end
    return result


def make_lightgbm_factory(horizon: int):
    try:
        from lightgbm import LGBMRegressor
    except Exception:
        return None

    def factory():
        return LGBMRegressor(
            objective="huber", n_estimators=220, learning_rate=0.025,
            num_leaves=24, max_depth=6, min_child_samples=24,
            subsample=0.85, colsample_bytree=0.8, reg_alpha=0.2,
            reg_lambda=1.5, random_state=20260820 + int(horizon), verbosity=-1,
        )
    return factory


def direction_classes(values: np.ndarray, horizon: int) -> np.ndarray:
    threshold = 0.004 * math.sqrt(horizon)
    return np.where(values > threshold, 0, np.where(values < -threshold, 2, 1))


def class_prevalence(values: np.ndarray, horizon: int) -> np.ndarray:
    classes = direction_classes(values, horizon)
    counts = np.bincount(classes, minlength=3).astype(float) + 1.0
    return counts / counts.sum()


def prediction_probabilities(predictions: np.ndarray, scale: float) -> np.ndarray:
    scale = max(float(scale), 1e-4)
    normalized = predictions / scale
    logits = np.column_stack([normalized, -np.abs(normalized) + 0.35, -normalized])
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=1, keepdims=True)


def prevalence_brier(actual_chunks: list[np.ndarray], prevalence: list[np.ndarray], horizon: int) -> float:
    scores = []
    for actual, probabilities in zip(actual_chunks, prevalence):
        target = np.eye(3)[direction_classes(actual, horizon)]
        scores.extend(np.sum((target - probabilities) ** 2, axis=1))
    return float(np.mean(scores))


def evaluate_candidate(
    name: str,
    prediction: np.ndarray,
    actual: np.ndarray,
    prediction_chunks: list[np.ndarray],
    actual_chunks: list[np.ndarray],
    horizon: int,
    baseline_brier: float,
) -> CandidateMetrics:
    window_ic = []
    for pred, truth in zip(prediction_chunks, actual_chunks):
        ic = pd.Series(pred).corr(pd.Series(truth), method="spearman")
        window_ic.append(float(ic) if pd.notna(ic) else 0.0)
    overall_ic_raw = pd.Series(prediction).corr(pd.Series(actual), method="spearman")
    overall_ic = float(overall_ic_raw) if pd.notna(overall_ic_raw) else 0.0
    scale = float(np.std(actual)) or 0.01
    probabilities = prediction_probabilities(prediction, scale)
    target = np.eye(3)[direction_classes(actual, horizon)]
    brier = float(np.mean(np.sum((target - probabilities) ** 2, axis=1)))
    positions = np.sign(prediction)
    turnover = np.abs(np.diff(np.r_[0.0, positions]))
    net = positions * actual - turnover * TRANSACTION_COST
    net_std = float(np.std(net, ddof=0))
    net_sharpe = float(np.mean(net) / net_std * math.sqrt(252 / horizon)) if net_std > 1e-12 else 0.0
    residuals = actual - prediction
    radius = adaptive_conformal_radius(residuals, coverage=0.8)
    coverage = float(np.mean(np.abs(residuals) <= radius)) if len(residuals) else 0.0
    positive = sum(1 for item in window_ic if item > 0)
    checks = {
        "majority_positive_windows": positive > len(window_ic) / 2,
        "positive_overall_rank_ic": overall_ic > 0,
        "calibration_beats_frequency": brier < baseline_brier,
        "positive_cost_adjusted_sharpe": net_sharpe > 0,
        "conformal_coverage": coverage >= 0.70,
    }
    qualified = all(checks.values())
    reason = "qualified" if qualified else ",".join(key for key, ok in checks.items() if not ok)
    return CandidateMetrics(
        model_name=name, model_version=f"candidate-{name}-v2.0.0", horizon=horizon,
        rank_ic=overall_ic, positive_windows=positive, total_windows=len(window_ic),
        brier_score=brier, baseline_brier=baseline_brier, net_sharpe=net_sharpe,
        coverage=coverage, qualified=qualified, reason=reason,
    )


def adaptive_conformal_radius(residuals: np.ndarray, coverage: float = 0.8) -> float:
    errors = np.abs(np.asarray(residuals, dtype=float))[-160:]
    errors = errors[np.isfinite(errors)]
    if not len(errors):
        return 0.0
    weights = np.exp(np.linspace(-2.5, 0.0, len(errors)))
    order = np.argsort(errors)
    sorted_errors = errors[order]
    cumulative = np.cumsum(weights[order]) / weights.sum()
    index = min(int(np.searchsorted(cumulative, coverage, side="left")), len(sorted_errors) - 1)
    return float(sorted_errors[index])


def calibrated_probabilities(expected: float, residuals: np.ndarray, horizon: int) -> Dict[str, float]:
    residuals = np.asarray(residuals, dtype=float)[-160:]
    threshold = 0.004 * math.sqrt(horizon)
    simulated = expected + residuals
    smoothing = 1.0
    up = (float(np.sum(simulated > threshold)) + smoothing) / (len(simulated) + 3 * smoothing)
    down = (float(np.sum(simulated < -threshold)) + smoothing) / (len(simulated) + 3 * smoothing)
    flat = max(0.0, 1.0 - up - down)
    total = up + flat + down
    return {"up": up / total, "flat": flat / total, "down": down / total}


def nonnegative_weights(metrics: list[CandidateMetrics]) -> Dict[str, float]:
    raw = {item.model_name: max(item.rank_ic, 1e-6) / max(item.brier_score, 1e-6) for item in metrics}
    total = sum(raw.values())
    return {name: value / total for name, value in raw.items()}


def feature_importance_proxy(names: list[str], x: np.ndarray, y: np.ndarray, limit: int = 8) -> Dict[str, float]:
    importance = []
    for index, name in enumerate(names):
        corr = pd.Series(x[:, index]).corr(pd.Series(y), method="spearman")
        importance.append((name, abs(float(corr)) if pd.notna(corr) else 0.0))
    importance.sort(key=lambda item: item[1], reverse=True)
    return {name: value for name, value in importance[:limit]}


def camel_metrics(metrics: CandidateMetrics, version: str) -> Dict[str, Any]:
    return {
        "modelName": metrics.model_name, "modelVersion": version, "horizon": metrics.horizon,
        "rankIc": metrics.rank_ic, "positiveWindows": metrics.positive_windows,
        "totalWindows": metrics.total_windows, "brierScore": metrics.brier_score,
        "baselineBrier": metrics.baseline_brier, "netSharpe": metrics.net_sharpe,
        "coverage": metrics.coverage, "qualified": metrics.qualified, "reason": metrics.reason,
    }


def unavailable_forecast(symbol: str, horizon: int, reason: str, *, source: str = "") -> Dict[str, Any]:
    return {
        "symbol": {"code": str(symbol).upper(), "market": "cn" if normalize_a_share_symbol(symbol) else "unknown"},
        "horizon": int(horizon), "asOfDate": "", "modelVersion": "",
        "keyFeatures": {}, "metrics": {"qualified": False, "reason": reason},
        "dataSource": source, "available": False, "unavailableReason": reason,
    }


_SERVICE: Optional[QuantPredictionService] = None
_SERVICE_LOCK = threading.Lock()


def get_quant_service() -> QuantPredictionService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = QuantPredictionService()
    return _SERVICE
