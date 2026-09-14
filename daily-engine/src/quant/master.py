"""MIT-compatible Stock King reimplementation of the MASTER architecture.

No upstream checkpoint or reported metric is copied. Checkpoints produced by
Stock King are horizon-specific and carry their own feature schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
import os

import numpy as np
import pandas as pd

from src.quant.features import build_features, forward_return, normalize_daily_frame


@dataclass(frozen=True)
class MasterCheckpointInfo:
    path: Path
    horizon: int
    feature_count: int
    model_version: str


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def build_master_model(feature_count: int, market_feature_count: int = 8, hidden_size: int = 128) -> Any:
    """Build a market-guided temporal/cross-sectional attention model."""
    import torch
    from torch import nn

    class MarketGuidedGate(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.market = nn.Sequential(
                nn.Linear(market_feature_count, hidden_size), nn.GELU(), nn.Linear(hidden_size, feature_count), nn.Sigmoid()
            )

        def forward(self, stock_features, market_features):
            return stock_features * self.market(market_features).unsqueeze(1)

    class StockKingMASTER(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.gate = MarketGuidedGate()
            self.project = nn.Linear(feature_count, hidden_size)
            temporal_layer = nn.TransformerEncoderLayer(
                d_model=hidden_size, nhead=4, dim_feedforward=hidden_size * 2,
                dropout=0.1, batch_first=True, norm_first=True,
            )
            self.temporal = nn.TransformerEncoder(temporal_layer, num_layers=2)
            self.cross_section = nn.MultiheadAttention(hidden_size, 4, dropout=0.1, batch_first=True)
            self.head = nn.Sequential(nn.LayerNorm(hidden_size), nn.Linear(hidden_size, 1))

        def forward(self, stock_features, market_features):
            # stock_features: [stocks, time, features]
            gated = self.gate(stock_features, market_features)
            encoded = self.temporal(self.project(gated))[:, -1, :]
            cross, _ = self.cross_section(encoded.unsqueeze(0), encoded.unsqueeze(0), encoded.unsqueeze(0))
            return self.head(cross.squeeze(0)).squeeze(-1)

    return StockKingMASTER()


def load_checkpoint(path: Path, feature_count: int, horizon: int) -> Optional[tuple[Any, dict]]:
    if not path.exists() or not torch_available():
        return None
    import torch

    payload = torch.load(path, map_location="cpu", weights_only=True)
    if int(payload.get("horizon", -1)) != int(horizon):
        return None
    if list(payload.get("feature_names") or []) and len(payload["feature_names"]) != feature_count:
        return None
    model = build_master_model(
        feature_count,
        int(payload.get("market_feature_count", 8)),
        int(payload.get("hidden_size", 128)),
    )
    model.load_state_dict(payload["state_dict"])
    model.eval()
    return model, payload


def read_checkpoint_payload(path: Path, horizon: int) -> Optional[dict]:
    """Read a locally produced MASTER checkpoint without constructing a model.

    Tier-model training uses the stored validation predictions as an optional
    challenger.  Keeping this small reader separate makes it explicit that
    only a qualified, horizon-compatible checkpoint may enter an ensemble.
    """
    if not path.exists() or not torch_available():
        return None
    import torch

    try:
        payload = torch.load(path, map_location="cpu", weights_only=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if int(payload.get("horizon", -1)) != int(horizon):
        return None
    if not payload.get("qualified") or not (payload.get("metrics") or {}).get("qualified"):
        return None
    return payload


def train_master_checkpoint(
    frames: Mapping[str, pd.DataFrame],
    horizon: int,
    checkpoint_path: Path,
    *,
    lookback: int = 8,
    epochs: int = 4,
) -> Dict[str, Any]:
    """Train MASTER on per-date stock cross-sections and save its own checkpoint.

    Features are robust-normalized using training dates only. Validation dates
    are never passed to the optimizer and are used for the release gate.
    """
    if not torch_available():
        return {"qualified": False, "reason": "torch_unavailable", "horizon": horizon}
    if int(horizon) not in (1, 5, 20):
        return {"qualified": False, "reason": "unsupported_horizon", "horizon": horizon}

    prepared: Dict[str, Dict[str, Any]] = {}
    feature_names: Optional[list[str]] = None
    all_dates: set[pd.Timestamp] = set()
    for symbol, raw in frames.items():
        frame = normalize_daily_frame(raw)
        if len(frame) < 180 + horizon:
            continue
        features = build_features(frame)
        names = list(features.columns)
        if feature_names is None:
            feature_names = names
        if names != feature_names:
            continue
        labels = forward_return(frame, horizon)
        dates = pd.to_datetime(frame["date"]).dt.normalize()
        date_to_position = {value: index for index, value in enumerate(dates)}
        prepared[symbol] = {
            "features": features,
            "labels": labels,
            "dates": dates,
            "positions": date_to_position,
        }
        all_dates.update(dates.iloc[120 + lookback : -horizon].tolist())

    if not feature_names or len(prepared) < 20:
        return {
            "qualified": False,
            "reason": f"insufficient_cross_section:{len(prepared)}/20",
            "horizon": horizon,
        }
    ordered_dates = sorted(all_dates)[-520:]
    if len(ordered_dates) < 180:
        return {"qualified": False, "reason": "insufficient_training_dates", "horizon": horizon}
    train_end = max(120, int(len(ordered_dates) * 0.72))
    validation_start = max(train_end + 20, int(len(ordered_dates) * 0.84))
    train_dates = ordered_dates[:train_end]
    gate_dates = ordered_dates[validation_start:]
    if len(gate_dates) < 40:
        return {"qualified": False, "reason": "insufficient_gate_dates", "horizon": horizon}

    # Bound memory while retaining every symbol and the complete training span.
    normalization_rows = []
    training_cutoff = train_dates[-1]
    for item in prepared.values():
        mask = item["dates"] <= training_cutoff
        values = item["features"].loc[mask, feature_names].tail(160).to_numpy(dtype=float)
        normalization_rows.append(values)
    normalization = np.concatenate(normalization_rows, axis=0)
    center = np.nanmedian(normalization, axis=0)
    mad = np.nanmedian(np.abs(normalization - center), axis=0) * 1.4826
    mad[~np.isfinite(mad) | (mad < 1e-8)] = 1.0

    def date_batch(current_date: pd.Timestamp):
        symbols = []
        sequences = []
        labels = []
        for symbol, item in prepared.items():
            position = item["positions"].get(current_date)
            if position is None or position + horizon >= len(item["features"]) or position < lookback - 1:
                continue
            label = item["labels"].iloc[position]
            raw_sequence = item["features"].iloc[position - lookback + 1 : position + 1][feature_names].to_numpy(dtype=float)
            if not np.isfinite(label) or raw_sequence.shape != (lookback, len(feature_names)):
                continue
            sequence = np.nan_to_num((raw_sequence - center) / mad, nan=0.0, posinf=3.0, neginf=-3.0)
            sequences.append(np.clip(sequence, -3.0, 3.0))
            labels.append(float(label))
            symbols.append(symbol)
        if len(symbols) < 12:
            return None
        stock = np.asarray(sequences, dtype=np.float32)
        last = stock[:, -1, :]
        market = np.r_[last[:, :4].mean(axis=0), last[:, :4].std(axis=0)].astype(np.float32)
        market_rows = np.repeat(market[None, :], len(stock), axis=0)
        target = np.asarray(labels, dtype=np.float32)
        low, high = np.quantile(target, [0.025, 0.975])
        target = np.clip(target, low, high)
        return symbols, stock, market_rows, target, market

    import torch

    thread_limit = max(1, min(8, (os.cpu_count() or 2) // 2))
    torch.set_num_threads(thread_limit)
    try:
        torch.set_num_interop_threads(max(1, min(2, thread_limit)))
    except RuntimeError:
        # PyTorch only allows this setting before inter-op work starts.
        pass
    torch.manual_seed(20260820 + int(horizon))
    model = build_master_model(len(feature_names), hidden_size=96)
    optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-4)
    model.train()
    usable_train_dates = 0
    for _ in range(max(1, int(epochs))):
        for current_date in train_dates:
            batch = date_batch(current_date)
            if batch is None:
                continue
            _, stock, market, target, _ = batch
            optimizer.zero_grad(set_to_none=True)
            prediction = model(torch.from_numpy(stock), torch.from_numpy(market))
            loss = torch.nn.functional.smooth_l1_loss(prediction, torch.from_numpy(target), beta=0.02)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            usable_train_dates += 1

    model.eval()
    daily_ic: list[float] = []
    all_prediction: list[float] = []
    all_actual: list[float] = []
    oos_predictions: Dict[str, Dict[str, float]] = {}
    last_market = np.zeros(8, dtype=float)
    with torch.inference_mode():
        for current_date in gate_dates:
            batch = date_batch(current_date)
            if batch is None:
                continue
            symbols, stock, market, target, last_market = batch
            prediction = model(torch.from_numpy(stock), torch.from_numpy(market)).cpu().numpy().astype(float)
            ic = pd.Series(prediction).corr(pd.Series(target), method="spearman")
            daily_ic.append(float(ic) if pd.notna(ic) else 0.0)
            all_prediction.extend(prediction.tolist())
            all_actual.extend(target.astype(float).tolist())
            date_key = current_date.date().isoformat()
            for symbol, value in zip(symbols, prediction):
                oos_predictions.setdefault(symbol, {})[date_key] = float(value)

    metrics = _master_gate_metrics(np.asarray(all_prediction), np.asarray(all_actual), daily_ic, horizon)
    metrics.update({
        "trainingDates": len(train_dates),
        "usableTrainingSteps": usable_train_dates,
        "validationDates": len(daily_ic),
        "stockCount": len(prepared),
    })
    payload = {
        "state_dict": model.state_dict(),
        "horizon": int(horizon),
        "feature_names": feature_names,
        "feature_center": center.tolist(),
        "feature_scale": mad.tolist(),
        "market_feature_count": 8,
        "last_market_features": np.asarray(last_market).tolist(),
        "lookback": int(lookback),
        "hidden_size": 96,
        "model_version": f"stock-king-master-h{horizon}-v2.0.0",
        "trained_through": train_dates[-1].date().isoformat(),
        "validation_through": gate_dates[-1].date().isoformat(),
        "metrics": metrics,
        "qualified": bool(metrics["qualified"]),
        "oos_predictions": oos_predictions,
        "validation_residuals": (np.asarray(all_actual) - np.asarray(all_prediction))[-320:].tolist(),
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_path.with_suffix(".tmp")
    torch.save(payload, temporary)
    temporary.replace(checkpoint_path)
    return {**metrics, "checkpoint": str(checkpoint_path), "horizon": horizon, "modelVersion": payload["model_version"]}


def predict_master_checkpoint(
    checkpoint_path: Path,
    features: pd.DataFrame,
    horizon: int,
) -> Dict[str, Any]:
    batch = predict_master_checkpoint_batch(checkpoint_path, {"__single__": features}, horizon)
    if not batch.get("available"):
        return batch
    prediction = (batch.get("predictions") or {}).get("__single__")
    if prediction is None:
        return {"available": False, "reason": "insufficient_sequence", "metrics": batch.get("metrics") or {}}
    return {
        **{key: value for key, value in batch.items() if key != "predictions"},
        "prediction": float(prediction),
    }


def predict_master_checkpoint_batch(
    checkpoint_path: Path,
    feature_frames: Mapping[str, pd.DataFrame],
    horizon: int,
) -> Dict[str, Any]:
    """Predict one cross-section so MASTER retains its stock-attention path."""
    usable_frames = {str(key): value for key, value in feature_frames.items() if isinstance(value, pd.DataFrame) and not value.empty}
    if not usable_frames:
        return {"available": False, "reason": "empty_cross_section"}
    names = list(next(iter(usable_frames.values())).columns)
    loaded = load_checkpoint(checkpoint_path, len(names), horizon)
    if loaded is None:
        return {"available": False, "reason": "checkpoint_missing_or_incompatible"}
    model, payload = loaded
    if list(payload.get("feature_names") or []) != names:
        return {"available": False, "reason": "feature_schema_mismatch"}
    metrics = dict(payload.get("metrics") or {})
    if not payload.get("qualified") or not metrics.get("qualified"):
        return {"available": False, "reason": "checkpoint_gate_failed", "metrics": metrics}
    validation_date = str(payload.get("validation_through") or "")
    try:
        if (date.today() - date.fromisoformat(validation_date)).days > 45:
            return {"available": False, "reason": "checkpoint_stale", "metrics": metrics}
    except ValueError:
        return {"available": False, "reason": "checkpoint_date_invalid", "metrics": metrics}
    lookback = int(payload.get("lookback", 8))
    center = np.asarray(payload.get("feature_center"), dtype=float)
    scale = np.asarray(payload.get("feature_scale"), dtype=float)
    if center.shape != (len(names),) or scale.shape != (len(names),):
        return {"available": False, "reason": "normalization_schema_mismatch", "metrics": metrics}
    symbols: list[str] = []
    sequences: list[np.ndarray] = []
    for symbol, features in usable_frames.items():
        if list(features.columns) != names or len(features) < lookback:
            continue
        raw = features.iloc[-lookback:].to_numpy(dtype=float)
        sequence = np.clip(np.nan_to_num((raw - center) / scale, nan=0.0, posinf=3.0, neginf=-3.0), -3.0, 3.0)
        symbols.append(symbol)
        sequences.append(sequence)
    if not sequences:
        return {"available": False, "reason": "insufficient_sequence", "metrics": metrics}
    stock = np.asarray(sequences, dtype=np.float32)
    last = stock[:, -1, :]
    market_vector = np.r_[last[:, :4].mean(axis=0), last[:, :4].std(axis=0)].astype(np.float32)
    market = np.repeat(market_vector[None, :], len(stock), axis=0)
    import torch
    with torch.inference_mode():
        prediction = model(torch.from_numpy(stock), torch.from_numpy(market)).cpu().numpy().astype(float)
    return {
        "available": True,
        "predictions": {symbol: float(value) for symbol, value in zip(symbols, prediction)},
        "metrics": metrics,
        "modelVersion": payload.get("model_version"),
        "validationResiduals": payload.get("validation_residuals") or [],
        "validationThrough": validation_date,
    }


def _master_gate_metrics(prediction: np.ndarray, actual: np.ndarray, daily_ic: list[float], horizon: int) -> Dict[str, Any]:
    from src.quant.service import direction_classes, prediction_probabilities, TRANSACTION_COST

    if len(prediction) < 120 or len(daily_ic) < 20:
        return {"qualified": False, "reason": "insufficient_validation_coverage"}
    overall_raw = pd.Series(prediction).corr(pd.Series(actual), method="spearman")
    rank_ic = float(overall_raw) if pd.notna(overall_raw) else 0.0
    positive = sum(value > 0 for value in daily_ic)
    scale = float(np.std(actual)) or 0.01
    probabilities = prediction_probabilities(prediction, scale)
    classes = direction_classes(actual, horizon)
    target = np.eye(3)[classes]
    brier = float(np.mean(np.sum((target - probabilities) ** 2, axis=1)))
    prevalence = (np.bincount(classes, minlength=3).astype(float) + 1.0)
    prevalence /= prevalence.sum()
    baseline_brier = float(np.mean(np.sum((target - prevalence) ** 2, axis=1)))
    positions = np.sign(prediction)
    turnover = np.abs(np.diff(np.r_[0.0, positions]))
    net = positions * actual - turnover * TRANSACTION_COST
    net_std = float(np.std(net))
    net_sharpe = float(np.mean(net) / net_std * np.sqrt(252 / horizon)) if net_std > 1e-12 else 0.0
    checks = {
        "majority_positive_windows": positive > len(daily_ic) / 2,
        "positive_overall_rank_ic": rank_ic > 0,
        "calibration_beats_frequency": brier < baseline_brier,
        "positive_cost_adjusted_sharpe": net_sharpe > 0,
    }
    qualified = all(checks.values())
    return {
        "modelName": "master", "rankIc": rank_ic,
        "positiveWindows": positive, "totalWindows": len(daily_ic),
        "brierScore": brier, "baselineBrier": baseline_brier,
        "netSharpe": net_sharpe, "coverage": 1.0,
        "qualified": qualified,
        "reason": "qualified" if qualified else ",".join(name for name, ok in checks.items() if not ok),
    }
