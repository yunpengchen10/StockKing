from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.tier_models import (
    TierArtifact,
    TierModelService,
    cross_sectional_neutralize,
    cumulative_hazard_probability,
    first_limit_touch_labels,
    forward_max_drawdown,
    forward_return,
    learn_nonnegative_weights,
    limit_ratio_for_symbol,
    limit_ratio_for_symbol_on_date,
    nested_meta_gate_masks,
    percentile_scores,
    purged_walk_forward_splits,
    rounded_limit_price,
    unique_column_names,
)
from src.quant.master import build_master_model, predict_master_checkpoint_batch


def test_small_universe_validation_does_not_qualify_unseen_stocks(tmp_path, monkeypatch):
    service = TierModelService(tmp_path / "models", tmp_path / "cache")
    champion = TierArtifact(tier="regular", qualified=True, reason="qualified",
                            metadata={"trainingScope": {"symbols": ["600000.SH"]}})
    monkeypatch.setattr(service, "_load_artifacts", lambda: {"regular": champion})
    monkeypatch.setattr("src.quant.tier_models.load_history_df",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unvalidated universe scored")))
    result = service.score_candidates("regular", [{"code": "600000.SH", "score": 40},
                                                   {"code": "000001.SZ", "score": 50}])
    assert all(row["modelStatus"] == "rule_fallback" for row in result)
    assert all("outside_validated_training_scope" in row["degradedReasons"][0] for row in result)
    assert champion.qualified is True
    assert champion.reason == "qualified"


def test_forward_targets_only_use_rows_after_prediction_time():
    prices = np.asarray([10.0, 11.0, 9.0, 12.0, 12.6])
    result = forward_return(prices, 2)
    assert result[0] == pytest.approx(-0.1)
    assert result[1] == pytest.approx(12 / 11 - 1)
    assert np.isnan(result[-1]) and np.isnan(result[-2])


def test_forward_drawdown_excludes_the_prediction_day():
    prices = [100, 80, 120, 90, 100]
    result = forward_max_drawdown(prices, 3)
    # Future window for row zero is [80, 120, 90], whose peak-to-trough
    # drawdown is 90/120-1. The starting 100 is intentionally excluded.
    assert result[0] == pytest.approx(-0.25)


@pytest.mark.parametrize(
    ("symbol", "ratio", "limit"),
    [
        ("600001.SH", 0.10, 11.0),
        ("688001.SH", 0.20, 12.0),
        ("300001.SZ", 0.20, 12.0),
        ("920001.BJ", 0.30, 13.0),
    ],
)
def test_exchange_limit_rules_and_tick_rounding(symbol, ratio, limit):
    assert limit_ratio_for_symbol(symbol) == ratio
    assert rounded_limit_price(10.0, ratio) == limit


def test_chinext_historical_limit_rule_is_point_in_time():
    assert limit_ratio_for_symbol_on_date("300001.SZ", "2020-08-21") == 0.10
    assert limit_ratio_for_symbol_on_date("300001.SZ", "2020-08-24") == 0.20


def test_first_touch_accepts_a_point_in_time_ratio_series():
    close = [10.0, 10.0, 10.0, 10.0]
    high = [10.0, 11.0, 11.0, 12.0]
    labels = first_limit_touch_labels(close, high, ratio=[0.10, 0.10, 0.20, 0.20], horizon=2)
    assert labels[0] == 1
    assert labels[1] == 2


def test_regular_training_columns_are_unique_when_exposures_are_features():
    columns = unique_column_names(
        ["ret_1", "log_size", "beta_60"],
        ["date", "industry", "log_size", "beta_60", "forward_return_5"],
    )
    assert columns == ["ret_1", "log_size", "beta_60", "date", "industry", "forward_return_5"]


def test_first_limit_touch_is_a_discrete_first_event_label():
    close = [10.0, 10.0, 10.0, 10.0, 10.0]
    high = [10.0, 10.5, 11.0, 11.2, 10.5]
    labels = first_limit_touch_labels(close, high, ratio=0.10, horizon=3)
    assert labels[0] == 2
    assert labels[1] == 1


def test_hazard_formula_uses_conditional_survival_product():
    result = cumulative_hazard_probability([0.10], [0.20], [0.30])
    assert result[0] == pytest.approx(1 - 0.9 * 0.8 * 0.7)


def test_cross_sectional_neutralization_removes_size_and_industry_exposure():
    rows = []
    for day in pd.date_range("2025-01-02", periods=3):
        for index in range(20):
            industry = "A" if index < 10 else "B"
            size = float(index + 1)
            label = 0.03 * size + (0.8 if industry == "B" else -0.4)
            rows.append({"date": day, "industry": industry, "log_size": size, "beta_60": 1.0, "label": label})
    frame = pd.DataFrame(rows)
    residual = cross_sectional_neutralize(frame, "label")
    # The synthetic label is exactly explained. Correlation is numerically
    # unstable when residual variance is ~1e-14, so assert the actual residual.
    assert float(residual.abs().max()) < 1e-5
    assert abs(float(residual.groupby(frame["industry"]).mean().diff().dropna().iloc[0])) < 1e-6


def test_purged_walk_forward_never_overlaps_the_longest_horizon():
    dates = pd.Series(pd.bdate_range("2022-01-03", periods=420).repeat(5))
    splits = purged_walk_forward_splits(dates, folds=3, purge=60, min_train_dates=180)
    assert len(splits) >= 2
    for train, test in splits:
        train_dates = pd.to_datetime(dates.iloc[train]).drop_duplicates().sort_values()
        test_dates = pd.to_datetime(dates.iloc[test]).drop_duplicates().sort_values()
        all_dates = pd.to_datetime(dates).drop_duplicates().sort_values().reset_index(drop=True)
        train_last = int(all_dates[all_dates == train_dates.iloc[-1]].index[0])
        test_first = int(all_dates[all_dates == test_dates.iloc[0]].index[0])
        assert test_first - train_last > 60


def test_nested_meta_learning_precedes_the_release_gate():
    dates = pd.Series(pd.bdate_range("2024-01-02", periods=80).repeat(3))
    meta, gate = nested_meta_gate_masks(dates, minimum_dates=20)
    assert meta.any() and gate.any()
    assert not np.any(meta & gate)
    assert pd.to_datetime(dates[meta]).max() < pd.to_datetime(dates[gate]).min()


def test_master_oos_predictions_align_by_symbol_and_date(tmp_path, monkeypatch):
    service = TierModelService(tmp_path / "models", tmp_path / "cache")
    payload = {
        "oos_predictions": {
            "600001.SH": {"2026-08-20": 0.12},
            "000001.SZ": {"2026-08-20": -0.04},
        }
    }
    monkeypatch.setattr("src.quant.tier_models.read_checkpoint_payload", lambda *_: payload)
    data = pd.DataFrame({
        "symbol": ["000001.SZ", "600001.SH", "600001.SH"],
        "date": ["2026-08-20", "2026-08-20", "2026-08-21"],
    })
    values, loaded = service._master_oos_vector(data, 5)
    assert loaded is payload
    assert values[:2].tolist() == pytest.approx([-0.04, 0.12])
    assert np.isnan(values[2])


def test_master_batch_prediction_keeps_the_candidate_cross_section(tmp_path):
    torch = pytest.importorskip("torch")
    feature_names = ["f1", "f2", "f3", "f4"]
    model = build_master_model(len(feature_names), hidden_size=16)
    checkpoint = tmp_path / "master-h5.pt"
    torch.save({
        "state_dict": model.state_dict(),
        "horizon": 5,
        "feature_names": feature_names,
        "feature_center": [0.0] * 4,
        "feature_scale": [1.0] * 4,
        "market_feature_count": 8,
        "lookback": 8,
        "hidden_size": 16,
        "model_version": "test-master",
        "validation_through": pd.Timestamp.today().date().isoformat(),
        "metrics": {"qualified": True},
        "qualified": True,
    }, checkpoint)
    frames = {
        "600001.SH": pd.DataFrame(np.ones((8, 4)), columns=feature_names),
        "000001.SZ": pd.DataFrame(np.full((8, 4), 2.0), columns=feature_names),
    }
    result = predict_master_checkpoint_batch(checkpoint, frames, 5)
    assert result["available"] is True
    assert set(result["predictions"]) == set(frames)


def test_oos_weight_learning_can_zero_a_harmful_challenger():
    target = np.linspace(-1, 1, 300)
    predictions = {"champion": target.copy(), "harmful": -target}
    weights = learn_nonnegative_weights(predictions, target, steps=1200)
    assert weights.get("champion", 0) > 0.99
    assert weights.get("harmful", 0) < 0.01


def test_percentile_is_rank_not_probability():
    assert percentile_scores([1.0, 2.0, 3.0]).tolist() == pytest.approx([100 / 3, 200 / 3, 100])


def test_unpublished_aggressive_model_exposes_rule_observation_without_probability(tmp_path):
    service = TierModelService(tmp_path / "models", tmp_path / "cache")
    result = service.score_candidates("aggressive", [{"code": "600001.SH", "score": 88.0}])
    assert result[0]["modelStatus"] == "rule_fallback"
    assert result[0]["tierScore"] == 100
    assert "touchProbability3d" not in result[0]
    assert "规则观察分" in result[0]["degradedReasons"][0]
