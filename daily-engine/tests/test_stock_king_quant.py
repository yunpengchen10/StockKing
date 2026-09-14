from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.quant.features import assert_no_future_dependency, build_features, forward_return
from src.quant.service import (
    adaptive_conformal_radius,
    calibrated_probabilities,
    expanding_splits,
    normalize_a_share_symbol,
)
from src.services.stock_king_board import board_group, metadata_board
from src.services.stock_king_service import (
    STOCK_KING_SCHEMA_VERSION,
    StockKingService,
    _aggressive_potential,
    _buyability,
    _conservative_eligible,
    _industry_diverse,
    _risk_excluded,
    _select_tiers,
)


def synthetic_daily(rows: int = 320) -> pd.DataFrame:
    rng = np.random.default_rng(20260820)
    returns = rng.normal(0.0005, 0.015, rows)
    close = 100 * np.cumprod(1 + returns)
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=rows),
        "open": close * (1 + rng.normal(0, 0.002, rows)),
        "high": close * (1 + rng.uniform(0, 0.015, rows)),
        "low": close * (1 - rng.uniform(0, 0.015, rows)),
        "close": close,
        "volume": rng.integers(1_000_000, 8_000_000, rows),
        "amount": rng.integers(100_000_000, 900_000_000, rows),
    })


def test_alpha_features_do_not_read_future_rows():
    frame = synthetic_daily()
    assert_no_future_dependency(frame, (121, 200, 280))
    features = build_features(frame)
    assert "ret_20" in features
    assert "amihud_20" in features


def test_forward_labels_are_isolated_from_features():
    frame = synthetic_daily()
    labels = forward_return(frame, 20)
    assert labels.tail(20).isna().all()
    assert "label" not in " ".join(build_features(frame).columns).lower()


def test_time_splits_are_expanding_and_non_overlapping():
    splits = expanding_splits(320, min_train=160, folds=4)
    assert len(splits) == 4
    previous_train = 0
    for train, test in splits:
        assert train[-1] < test[0]
        assert len(train) > previous_train
        previous_train = len(train)


def test_adaptive_conformal_and_probabilities_are_valid():
    residuals = np.linspace(-0.08, 0.08, 161)
    radius = adaptive_conformal_radius(residuals, coverage=0.8)
    assert 0 < radius < 0.08
    probabilities = calibrated_probabilities(0.01, residuals, 5)
    assert set(probabilities) == {"up", "flat", "down"}
    assert abs(sum(probabilities.values()) - 1.0) < 1e-12
    assert all(0 <= value <= 1 for value in probabilities.values())


def test_prediction_scope_rejects_hk_and_us():
    assert normalize_a_share_symbol("600519") == "600519.SH"
    assert normalize_a_share_symbol("688981.SH") == "688981.SH"
    assert normalize_a_share_symbol("00700.HK") == ""
    assert normalize_a_share_symbol("AAPL.US") == ""


def test_star_board_prefers_security_metadata():
    metadata_candidate = {"code": "600000.SH", "raw": {"board": "科创板"}}
    assert metadata_board(metadata_candidate) == "star"
    assert board_group(metadata_candidate) == "kechuang"
    fallback_candidate = {"code": "688981.SH", "raw": {}}
    assert metadata_board(fallback_candidate) == "star_code_fallback"


def test_ai_advice_explains_comprehensive_kline_evidence(monkeypatch, tmp_path):
    import src.services.stock_king_service as service_module

    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        service_module,
        "get_dsa_candidate_context",
        lambda *args, **kwargs: {"warnings": [], "fundamentals": {"available": True}},
    )

    class FakeQuant:
        @staticmethod
        def forecast_all_horizons(symbol):
            return [{
                "horizon": 5,
                "available": True,
                "probabilityUp": 0.61,
                "probabilityDown": 0.22,
            }]

    monkeypatch.setattr(service_module, "get_quant_service", lambda: FakeQuant())
    result = StockKingService(config=object()).advice(
        "300750.SZ",
        research_note={"core_logic": "产业需求", "observation_plan": "观察放量突破"},
        technical_snapshot={
            "available": True,
            "close": 210.0,
            "trend": "多头排列",
            "ma5": 208.0,
            "ma10": 204.0,
            "ma20": 198.0,
            "ma60": 180.0,
            "aboveMa20": True,
            "return20": 0.08,
            "macdDif": 1.2,
            "macdDea": 0.8,
            "macdHistogram": 0.8,
            "macdBullish": True,
            "rsi14": 62.0,
            "kdjK": 68.0,
            "kdjD": 59.0,
            "kdjJ": 86.0,
            "bollPosition": 0.75,
            "volumeRatio5To20": 1.35,
            "atrPercent": 0.021,
            "volatility20": 0.018,
            "support20": 188.0,
            "resistance20": 216.0,
        },
    )

    combined = "\n".join(result["bullArguments"] + result["observationTriggers"])
    assert "均线呈多头排列" in combined
    assert "MACD" in combined
    assert "RSI14" in combined
    assert "KDJ" in combined
    assert "量价方向" in combined
    assert "20 日压力" in combined
    assert "technicalIndicatorsUsed" in result
    assert "BOLL" in result["technicalIndicatorsUsed"]
    assert "tierModelAnalysis" in result
    assert len(result["calculationNotes"]) == 3


def king_candidate(code="600001.SH", score=80.0, industry="电子", board="主板", **overrides):
    raw = {
        "code": code,
        "name": f"测试{code[:3]}",
        "score": score,
        "screen_score": score,
        "price": 10.5,
        "change_pct": 3.0,
        "amount": 300_000_000,
        "industry": industry,
        "board": board,
        "change_60d": 12.0,
        "price_above_ma20": True,
        "volatility_20d_pct": 24.0,
        "max_drawdown_20d_pct": -6.0,
        "atr_20_pct": 3.5,
        "daily_quality_score": 92.0,
        "signal_score": 65.0,
        "factor_scores": {
            "momentum": 72.0,
            "activity": 68.0,
            "theme_heat": 62.0,
            "liquidity": 74.0,
            "stability": 88.0,
            "value": 60.0,
        },
    }
    raw.update(overrides.pop("raw", {}))
    candidate = {**raw, "raw": raw, "dsa_context": {
        "quote": {"price": 10.5, "amount": 300_000_000, "pre_close": 10.0, "is_stale": False},
    }}
    candidate.update(overrides)
    return candidate


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("change_60d", -0.1),
        ("price_above_ma20", False),
        ("volatility_20d_pct", 30.1),
        ("max_drawdown_20d_pct", -8.1),
        ("atr_20_pct", 4.6),
        ("daily_quality_score", 84.9),
    ],
)
def test_conservative_hard_gates_never_relax(field, value):
    assert _conservative_eligible(king_candidate()) is True
    assert _conservative_eligible(king_candidate(raw={field: value}, **{field: value})) is False


def test_aggressive_potential_weights_and_quant_degradation():
    candidate = king_candidate(
        score=80,
        signal_score=50,
        factor_scores={"momentum": 70, "activity": 60, "theme_heat": 40, "liquidity": 30},
        raw={"score": 80, "signal_score": 50, "factor_scores": {"momentum": 70, "activity": 60, "theme_heat": 40, "liquidity": 30}},
    )
    deterministic = _aggressive_potential(candidate, [])
    assert deterministic["score"] == pytest.approx(64.5)
    assert deterministic["quantSupportScore"] is None
    assert "不扣分" in deterministic["degradedReasons"][0]

    qualified = _aggressive_potential(candidate, [
        {"horizon": 1, "available": True, "probabilityUp": 0.8, "metrics": {"qualified": True}},
        {"horizon": 5, "available": True, "probabilityUp": 0.6, "metrics": {"qualified": True}},
        {"horizon": 20, "available": True, "probabilityUp": 0.9, "metrics": {"qualified": True}},
    ])
    assert qualified["quantSupportScore"] == pytest.approx(74.0)
    assert qualified["score"] == pytest.approx(65.45)


@pytest.mark.parametrize(
    ("code", "board", "expected_limit", "permission"),
    [
        ("600001.SH", "主板", 11.0, False),
        ("688001.SH", "科创板", 12.0, True),
        ("300001.SZ", "创业板", 12.0, True),
        ("920001.BJ", "北交所", 13.0, True),
    ],
)
def test_buyability_limit_price_rules(code, board, expected_limit, permission):
    candidate = king_candidate(code=code, board=board)
    result = _buyability(candidate)
    assert result["status"] == "tradable_at_generation"
    assert result["limitUpPrice"] == expected_limit
    assert result["permissionRequired"] is permission


@pytest.mark.parametrize(
    ("quote", "status"),
    [
        ({"price": 11.0, "amount": 100, "pre_close": 10.0}, "at_limit_up"),
        ({"price": 10.0, "amount": 0, "pre_close": 10.0}, "zero_turnover"),
        ({"price": 10.0, "amount": 100, "pre_close": 10.0, "suspended": True}, "suspended"),
        ({"price": 10.0, "amount": 100, "pre_close": 10.0, "is_stale": True}, "stale_quote"),
        ({"price": 10.0, "amount": 100}, "missing_previous_close"),
    ],
)
def test_buyability_rejects_unbuyable_quotes(monkeypatch, quote, status):
    import src.services.stock_king_service as service_module

    candidate = king_candidate()
    candidate["dsa_context"] = {"quote": quote}
    monkeypatch.setattr(service_module, "get_dsa_realtime_quote", lambda _code: quote)
    assert _buyability(candidate)["status"] == status


def test_cross_tier_dedup_and_strict_industry_cap():
    shared = king_candidate(code="600010.SH", score=80, industry="银行")
    aggressive_shared = {**shared, "potentialScore": 90}
    same_industry = [king_candidate(code=f"6000{index:02d}.SH", score=100 - index, industry="银行") for index in range(1, 5)]
    assert len(_industry_diverse(same_industry, 5)) == 2

    selected = _select_tiers(
        conservative_pool=[shared],
        regular_pool=[shared, king_candidate(code="600020.SH", industry="机械")],
        aggressive_pool=[aggressive_shared],
        max_per_board=5,
    )
    all_tiers = {
        tier: {item["code"] for group in values.values() for item in group}
        for tier, values in selected.items()
    }
    assert "600010.SH" not in all_tiers["conservative"]
    assert "600010.SH" in all_tiers["aggressive"]
    assert all_tiers["conservative"].isdisjoint(all_tiers["regular"])
    assert all_tiers["aggressive"].isdisjoint(all_tiers["regular"])


def test_three_tier_contract_and_old_cache_recompute(monkeypatch, tmp_path):
    import src.services.stock_king_service as service_module

    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    cache_path = tmp_path / "stock-king-picks.json"
    cache_path.write_text(json.dumps({"asOfDate": "2026-08-25", "kechuang": [], "nonKeChuang": []}), encoding="utf-8")
    calls = []

    class FakeScreeningService:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def screen(strategy, **_kwargs):
            calls.append(strategy)
            candidates = {
                "low_volatility_quality": [king_candidate(code="600101.SH", score=91)],
                "balanced_alpha": [king_candidate(code="600102.SH", score=86, industry="机械")],
                "momentum_quality": [king_candidate(code="688102.SH", score=82, industry="半导体", board="科创板")],
                "volume_breakout": [king_candidate(code="600103.SH", score=88, industry="传媒")],
                "capital_heat": [king_candidate(code="688103.SH", score=84, industry="软件", board="科创板")],
            }[strategy]
            return {"strategy": strategy, "candidates": candidates, "candidate_count": len(candidates)}

    class FakeQuant:
        @staticmethod
        def forecast_all_horizons(_symbol, stock_name=""):
            del stock_name
            return []

    monkeypatch.setattr(service_module, "ScreeningService", FakeScreeningService)
    monkeypatch.setattr(service_module, "get_quant_service", lambda: FakeQuant())
    result = StockKingService(config=object()).picks(max_per_board=5)
    assert result["schemaVersion"] == STOCK_KING_SCHEMA_VERSION
    assert result["schemaVersion"] == 4
    assert result["methodologyVersion"] == "king-local-5d-v1"
    assert result["classicMethodologyVersion"] == "king-three-objective-v2.3.4"
    assert result["adaptive"]["status"] == "unavailable"
    assert set(result["tiers"]) == {"conservative", "regular", "aggressive"}
    assert set(calls) == {"low_volatility_quality", "balanced_alpha", "momentum_quality", "volume_breakout", "capital_heat"}
    assert result["kechuang"] == result["tiers"]["regular"]["kechuang"]
    assert result["nonKeChuang"] == result["tiers"]["regular"]["nonKeChuang"]
    assert result["tiers"]["conservative"]["shortfall"]["kechuang"] == 5
    aggressive = result["tiers"]["aggressive"]["nonKeChuang"][0]
    assert aggressive["potentialHorizon"] == "1-3_trading_days"
    assert aggressive["buyability"]["tradableAtGeneration"] is True
    assert aggressive["modelStatus"] == "rule_fallback"
    assert "百分位" in aggressive["scoreMeaning"]
    assert "touchProbability3d" not in aggressive


def test_endpoint_defaults_to_five_per_board():
    from api.v1.endpoints.stock_king import PicksRequest

    assert PicksRequest().max_per_board == 5


@pytest.mark.parametrize(
    "candidate",
    [
        king_candidate(name="*ST测试"),
        king_candidate(name="退市测试"),
        king_candidate(name="N新股"),
        king_candidate(raw={"listing_date": "20260801"}, listing_date="20260801"),
    ],
)
def test_risk_filter_excludes_special_and_newly_listed_stocks(candidate):
    assert _risk_excluded(candidate) is True
