"""Time-isolated continuation challenger training and conservative promotion gates."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .labels import LABEL_SCHEMA_VERSION

FEATURE_NAMES = (
    "change_1d",
    "change_60d",
    "volume_ratio_20d",
    "volatility_20d_pct",
    "max_drawdown_20d_pct",
    "breakout_20d_pct",
)
LABEL_NAMES = ("ignition_3d", "continuation_5d", "strong_10d")


def train_challenger(
    records: list[dict[str, Any]],
    *,
    symbol_count: int,
    years: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Train on the older 70% of dates and evaluate on the newest 30%."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss, log_loss
    from sklearn.preprocessing import StandardScaler

    mature = [
        item for item in records
        if item.get("labels", {}).get("maturity_status") == "mature"
        and item.get("labels", {}).get("labelSchemaVersion") == LABEL_SCHEMA_VERSION
        and all(_finite(item.get("features", {}).get(name)) for name in FEATURE_NAMES)
    ]
    dates = sorted({str(item.get("as_of") or "") for item in mature if item.get("as_of")})
    version = f"yao-logit-{datetime.now():%Y%m%d-%H%M%S}"
    coverage_gate = symbol_count >= 1000 and len(mature) >= 20_000 and int(years) >= 5 and len(dates) >= 900
    if len(dates) < 20 or len(mature) < 200:
        return _insufficient(version, len(mature), symbol_count, years, "insufficient_time_isolated_samples")
    split_date = dates[max(1, int(len(dates) * 0.70)) - 1]
    train_rows = [item for item in mature if str(item["as_of"]) <= split_date]
    test_rows = [item for item in mature if str(item["as_of"]) > split_date]
    if len(train_rows) < 100 or len(test_rows) < 50:
        return _insufficient(version, len(mature), symbol_count, years, "insufficient_walk_forward_holdout")

    train_x = np.asarray([[_number(item["features"][name]) for name in FEATURE_NAMES] for item in train_rows], dtype=float)
    test_x = np.asarray([[_number(item["features"][name]) for name in FEATURE_NAMES] for item in test_rows], dtype=float)
    scaler = StandardScaler().fit(train_x)
    train_z = scaler.transform(train_x)
    test_z = scaler.transform(test_x)
    model_payloads: dict[str, Any] = {}
    gates: dict[str, Any] = {}
    metrics: dict[str, Any] = {
        "sample_count": len(mature),
        "train_count": len(train_rows),
        "test_count": len(test_rows),
        "split_date": split_date,
        "symbol_count": symbol_count,
        "coverage_gate": coverage_gate,
    }
    for label in LABEL_NAMES:
        train_y = np.asarray([int(bool(item["labels"].get(label))) for item in train_rows], dtype=int)
        test_y = np.asarray([int(bool(item["labels"].get(label))) for item in test_rows], dtype=int)
        base_rate = float(np.mean(test_y)) if len(test_y) else 0.0
        if len(set(train_y.tolist())) < 2 or len(set(test_y.tolist())) < 2:
            gates[label] = {"qualified": False, "reason": "single_class_split"}
            metrics[label] = {"base_rate": round(base_rate, 6)}
            continue
        model = LogisticRegression(C=0.25, max_iter=1000, class_weight="balanced", random_state=42)
        model.fit(train_z, train_y)
        probabilities = np.clip(model.predict_proba(test_z)[:, 1], 1e-6, 1 - 1e-6)
        baseline = np.full(len(test_y), np.clip(base_rate, 1e-6, 1 - 1e-6))
        pr_auc = float(average_precision_score(test_y, probabilities))
        brier = float(brier_score_loss(test_y, probabilities))
        baseline_brier = float(brier_score_loss(test_y, baseline))
        loss = float(log_loss(test_y, probabilities, labels=[0, 1]))
        baseline_loss = float(log_loss(test_y, baseline, labels=[0, 1]))
        top_count = max(5, int(math.ceil(len(test_y) * 0.05)))
        top_index = np.argsort(probabilities)[-top_count:]
        precision_top = float(np.mean(test_y[top_index]))
        lift = precision_top / base_rate if base_rate > 0 else 0.0
        selected_drawdowns = [
            _number(test_rows[index]["labels"].get("max_drawdown_pct"))
            for index in top_index.tolist()
            if _finite(test_rows[index]["labels"].get("max_drawdown_pct"))
        ]
        mean_drawdown = float(np.mean(selected_drawdowns)) if selected_drawdowns else None
        metric = {
            "pr_auc": round(pr_auc, 6),
            "baseline_pr_auc": round(base_rate, 6),
            "brier": round(brier, 6),
            "baseline_brier": round(baseline_brier, 6),
            "log_loss": round(loss, 6),
            "baseline_log_loss": round(baseline_loss, 6),
            "precision_top_5pct": round(precision_top, 6),
            "lift_at_5": round(lift, 6),
            "mean_selected_max_drawdown_pct": round(mean_drawdown, 6) if mean_drawdown is not None else None,
        }
        passed = (
            pr_auc > base_rate
            and brier < baseline_brier
            and loss < baseline_loss
            and lift > 1.0
            and mean_drawdown is not None
            and mean_drawdown >= -18.0
        )
        metrics[label] = metric
        gates[label] = {"qualified": passed, "reason": "passed" if passed else "metric_gate_failed"}
        model_payloads[label] = {
            "intercept": float(model.intercept_[0]),
            "coefficients": [float(value) for value in model.coef_[0]],
        }

    target_gates_passed = all(bool(gates.get(label, {}).get("qualified")) for label in LABEL_NAMES)
    qualified = coverage_gate and target_gates_passed
    matched = _matched_counterexample_summary(test_rows)
    metrics["matched_counterexamples"] = matched
    gate_payload = {
        "qualified": qualified,
        "coverage_gate": coverage_gate,
        "target_gates_passed": target_gates_passed,
        "reason": "passed" if qualified else "full_universe_coverage_or_metric_gate_failed",
        "targets": gates,
    }
    artifact = {
        "labelSchemaVersion": LABEL_SCHEMA_VERSION,
        "model_version": version,
        "status": "champion" if qualified else "challenger",
        "trained_at": datetime.now().astimezone().isoformat(),
        "feature_names": list(FEATURE_NAMES),
        "scaler": {"mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist()},
        "models": model_payloads,
        "metrics": metrics,
        "gates": gate_payload,
        "config": {"years": years, "symbol_count": symbol_count, "point_in_time": True, "transaction_cost_bps": 15},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / f"{version}.json"
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact["artifact_path"] = str(artifact_path.resolve())
    if qualified:
        (output_dir / "champion.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


def predict_probabilities(features: dict[str, Any], *, champion_path: Path) -> tuple[dict[str, float | None], str, str]:
    empty = {label: None for label in LABEL_NAMES}
    if not champion_path.is_file():
        return empty, "withheld_until_model_gate_passes", "yao-audit-v1"
    try:
        artifact = json.loads(champion_path.read_text(encoding="utf-8"))
        if artifact.get("labelSchemaVersion") != LABEL_SCHEMA_VERSION:
            return empty, "withheld_incompatible_label_schema", str(artifact.get("model_version") or "yao-audit-v1")
        if not artifact.get("gates", {}).get("qualified"):
            return empty, "withheld_until_model_gate_passes", str(artifact.get("model_version") or "yao-audit-v1")
        raw = np.asarray([_number(features[name]) for name in artifact["feature_names"]], dtype=float)
        mean = np.asarray(artifact["scaler"]["mean"], dtype=float)
        scale = np.asarray(artifact["scaler"]["scale"], dtype=float)
        z = (raw - mean) / np.where(scale == 0, 1.0, scale)
        result: dict[str, float | None] = {}
        for label in LABEL_NAMES:
            model = artifact["models"].get(label)
            if not model:
                result[label] = None
                continue
            logit = float(model["intercept"]) + float(np.dot(z, np.asarray(model["coefficients"], dtype=float)))
            result[label] = round(1.0 / (1.0 + math.exp(-max(min(logit, 30), -30))), 6)
        return result, "qualified", str(artifact["model_version"])
    except Exception:
        return empty, "unavailable_model_artifact", "yao-audit-v1"


def _matched_counterexample_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in rows if row.get("labels", {}).get("strong_10d") is True]
    negatives = [row for row in rows if row.get("labels", {}).get("strong_10d") is False]
    if not positives or not negatives:
        return {"matched_pairs": 0, "status": "insufficient_classes"}
    matrix = np.asarray([[_number(item["features"][name]) for name in FEATURE_NAMES] for item in rows], dtype=float)
    scale = np.nanstd(matrix, axis=0)
    scale[scale < 1e-9] = 1.0
    neg_matrix = np.asarray([[_number(item["features"][name]) for name in FEATURE_NAMES] for item in negatives], dtype=float)
    distances: list[float] = []
    for positive in positives[:1000]:
        vector = np.asarray([_number(positive["features"][name]) for name in FEATURE_NAMES], dtype=float)
        distance = np.sqrt(np.sum(((neg_matrix - vector) / scale) ** 2, axis=1))
        distances.append(float(np.min(distance)))
    return {
        "matched_pairs": len(distances),
        "median_standardized_distance": round(float(np.median(distances)), 6),
        "status": "completed",
    }


def _insufficient(version: str, samples: int, symbols: int, years: int, reason: str) -> dict[str, Any]:
    return {
        "labelSchemaVersion": LABEL_SCHEMA_VERSION,
        "model_version": version,
        "status": "research_observation",
        "trained_at": datetime.now().astimezone().isoformat(),
        "metrics": {"sample_count": samples, "symbol_count": symbols},
        "gates": {"qualified": False, "reason": reason},
        "config": {"years": years, "symbol_count": symbols, "point_in_time": True},
    }


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any) -> float:
    return float(value) if _finite(value) else 0.0
