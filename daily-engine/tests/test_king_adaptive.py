from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from src.services.yao_scout.adaptive import (
    SHANGHAI_TZ,
    candidate_changes,
    classify_result_label,
    compute_history_signature,
    compute_nls,
    learn_from_outcomes,
    official_timing_status,
    score_adaptive_candidate,
    select_candidates,
)
from src.storage import DatabaseManager


def _candidate(code: str, score: float, model: str) -> dict:
    return {"code": code, "score": score, "modelBranch": model, "pool": "B" if model.startswith("M6") else "A"}


def test_m6_gets_one_reserved_seat_when_it_passes_the_same_threshold() -> None:
    values = [_candidate(f"60000{i}", 90 - i, "M3") for i in range(1, 6)]
    values.append(_candidate("600099", 60, "M6-A"))
    selected = select_candidates(values, top_n=5, threshold=58)
    assert len(selected) == 5
    assert any(item["modelBranch"] == "M6-A" for item in selected)


def test_top_five_reserves_a_pool_majority_and_caps_m6_share() -> None:
    values = [
        _candidate("600001", 99, "M6-A"),
        _candidate("600002", 98, "M6-B"),
        _candidate("600003", 97, "M6-A"),
        _candidate("600011", 82, "M1"),
        _candidate("600012", 81, "M2"),
        _candidate("600013", 80, "M3"),
        _candidate("600014", 79, "M3"),
    ]
    selected = select_candidates(values, top_n=5, threshold=70)
    assert len(selected) == 5
    assert sum(not item["modelBranch"].startswith("M6") for item in selected) >= 3
    assert sum(item["modelBranch"].startswith("M6") for item in selected) <= 2
    assert {item["modelBranch"] for item in selected}.issuperset({"M1", "M2", "M3"})


def test_m6_only_pool_is_capped_instead_of_filling_the_board() -> None:
    values = [_candidate(f"6000{index:02d}", 95 - index, "M6-A") for index in range(5)]
    selected = select_candidates(values, top_n=5, threshold=70)
    assert len(selected) == 2
    assert all(item["modelBranch"].startswith("M6") for item in selected)


def test_nls_has_95_positive_points_plus_5_safety_and_rejects_unconfirmed_tail_pulse() -> None:
    strong = compute_nls(
        change=8, turnover=18, volume_ratio=4, recovery=1, breakout=5,
        distance_to_limit=2, limit_count=3, positive_hits=3, negative_hits=0,
        vwap_state="above", scan_slot="1455",
    )
    assert strong["positiveDimensions"] <= 95
    assert strong["riskSafety"] <= 5
    assert strong["total"] <= 100
    pulse = compute_nls(
        change=9, turnover=1, volume_ratio=0.5, recovery=1, breakout=0,
        distance_to_limit=1, limit_count=0, positive_hits=0, negative_hits=0,
        vwap_state="above", scan_slot="1455",
    )
    assert pulse["latePulseRejected"] is True
    assert pulse["total"] <= 69


def test_official_timing_records_missed_instead_of_backfilling() -> None:
    on_time = datetime(2026, 8, 28, 9, 22, tzinfo=SHANGHAI_TZ)
    late = datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ)
    assert official_timing_status("0922", on_time)["status"] == "on_time"
    assert official_timing_status("0922", late)["status"] == "missed"


def test_hard_risk_and_limit_distance_never_learn_away() -> None:
    base = {
        "code": "600127", "name": "测试", "price": 10.0, "amount": 300_000_000,
        "change_pct": 5.0, "turnover_rate": 8.0, "volume_ratio": 2.0,
        "daily_data_points": 80, "breakout_20d_pct": 2.0,
    }
    state = {"modelOffsets": {"M1": 8}, "failureProtection": {}}
    assert score_adaptive_candidate(
        base, context={"announcement": "公司被立案调查"}, signature={"historyPoints": 80},
        scan_slot="1030", state=state, source_meta={"source": "fixture"},
    ) is None
    locked = {**base, "change_pct": 9.8, "turnover_rate": 0.2}
    assert score_adaptive_candidate(
        locked, context={}, signature={"historyPoints": 80}, scan_slot="1030",
        state=state, source_meta={"source": "fixture"},
    ) is None


@pytest.mark.parametrize(
    ("context", "signature", "overrides", "expected"),
    [
        ({"announcement": "控制权变更并推进重大重组"}, {"historyPoints": 80}, {}, "M1"),
        ({"news": "政策催化形成产业主线"}, {"historyPoints": 80}, {}, "M2"),
        ({}, {"historyPoints": 80, "amountMultiple5d": 3}, {}, "M3"),
        ({}, {"historyPoints": 80, "recentLimitUps20d": 1}, {"breakout_20d_pct": 3}, "M4"),
        ({"announcement": "重大订单中标且利润增长"}, {"historyPoints": 80}, {"ma_bullish": True}, "M5"),
        ({}, {"historyPoints": 80, "recentLimitUps10d": 2, "recoveryRatio": 0.8}, {}, "M6-A"),
        ({}, {"historyPoints": 80, "recentLimitUps10d": 2, "recoveryRatio": 0.8}, {"change_pct": -8}, "M6-B"),
    ],
)
def test_m1_to_m6_branches_are_auditable(context: dict, signature: dict, overrides: dict, expected: str) -> None:
    row = {
        "code": "600127", "name": "测试", "price": 10.0, "amount": 300_000_000,
        "change_pct": 4.0, "turnover_rate": 10.0, "volume_ratio": 2.2,
        "daily_data_points": 80, "breakout_20d_pct": 0.0,
        **overrides,
    }
    candidate = score_adaptive_candidate(
        row, context=context, signature=signature, scan_slot="1030",
        state={"modelOffsets": {}, "failureProtection": {}}, source_meta={"source": "fixture"},
    )
    assert candidate is not None
    assert candidate["modelBranch"] == expected


def test_explicit_m1_evidence_is_not_overwritten_by_m6_memory() -> None:
    row = {
        "code": "600127", "name": "测试", "price": 10.0, "amount": 300_000_000,
        "change_pct": 4.0, "turnover_rate": 10.0, "volume_ratio": 2.2,
        "daily_data_points": 80, "breakout_20d_pct": 3.0,
    }
    candidate = score_adaptive_candidate(
        row,
        context={"announcement": "控制权变更并推进重大重组"},
        signature={"historyPoints": 80, "recentLimitUps10d": 2, "recoveryRatio": 0.8},
        scan_slot="1030",
        state={"modelOffsets": {}, "failureProtection": {}},
        source_meta={"source": "fixture"},
    )
    assert candidate is not None
    assert candidate["modelBranch"] == "M1"


def test_old_negative_news_decays_only_after_market_absorption() -> None:
    row = {
        "code": "600127", "name": "测试", "price": 10.0, "amount": 300_000_000,
        "change_pct": 4.0, "turnover_rate": 10.0, "volume_ratio": 2.2,
        "daily_data_points": 80, "breakout_20d_pct": 3.0,
    }
    candidate = score_adaptive_candidate(
        row, context={"news": "此前预亏风险提示"},
        signature={"historyPoints": 80, "recentLimitUps20d": 1}, scan_slot="1030",
        state={"modelOffsets": {}, "failureProtection": {}}, source_meta={"source": "fixture"},
    )
    assert candidate is not None
    assert "递减" in candidate["risks"][0]


def test_history_signature_counts_limit_memory_and_recovery() -> None:
    closes = [10.0] * 18 + [11.0, 12.1]
    frame = pd.DataFrame({
        "close": closes, "open": [9.9] * 20, "high": [value * 1.01 for value in closes],
        "low": [value * 0.97 for value in closes], "amount": [100.0] * 19 + [300.0],
    })
    signature = compute_history_signature(frame, "600127")
    assert signature["recentLimitUps10d"] == 2
    assert signature["amountMultiple5d"] == pytest.approx(3.0)
    assert 0 <= signature["recoveryRatio"] <= 1


def test_live_runs_are_persisted_but_never_create_training_outcomes(tmp_path: Path) -> None:
    previous = DatabaseManager._instance
    if previous is not None and getattr(previous, "_engine", None) is not None:
        previous._engine.dispose()
    DatabaseManager._instance = None
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'adaptive.sqlite').as_posix()}")
    candidate = {
        "code": "600127", "name": "测试", "rank": 1, "tier": "A", "score": 80,
        "features": {"price": 10, "model_branch": "M3", "scan_slot": "live"},
        "scores": {"selectedModel": "M3"},
    }
    payload = {
        "run_id": "king-live-test", "mode": "king_live", "status": "completed",
        "as_of": "2026-08-28T10:30:00+08:00", "cutoff_at": "2026-08-28T10:30:00+08:00",
        "model_version": "king-adaptive-skill-v2.3.1", "training_eligible": False,
        "candidates": [candidate],
    }
    db.save_yao_run(payload)
    assert db.get_latest_yao_run(mode="king_live") is not None
    history = db.list_yao_runs(mode="king_live", include_result=True)
    assert history[0]["result"]["candidates"][0]["code"] == "600127"
    assert db.get_yao_candidate_history(model_prefix="M") == []
    payload.update({"run_id": "king-1030-test", "mode": "king_1030", "training_eligible": True})
    payload["candidates"][0]["features"]["scan_slot"] = "1030"
    db.save_yao_run(payload)
    assert len(db.get_yao_candidate_history(model_prefix="M")) == 1
    db._engine.dispose()
    DatabaseManager._instance = None


def test_v22_database_is_extended_without_destroying_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE legacy_preferences (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO legacy_preferences VALUES ('theme', 'dark')")
    connection.commit()
    connection.close()
    DatabaseManager._instance = None
    db = DatabaseManager(db_url=f"sqlite:///{path.as_posix()}")
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT value FROM legacy_preferences WHERE key='theme'").fetchone()[0] == "dark"
        tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"yao_runs", "yao_dlm", "yao_adaptive_states"}.issubset(tables)
    db._engine.dispose()
    DatabaseManager._instance = None


def test_failure_protection_uses_skill_bounds() -> None:
    failures = [{
        "scan_slot": "1030", "observation_date": f"2026-08-{day:02d}", "maturity_status": "mature",
        "model_branch": "M3", "code": f"6000{day:02d}", "max_return_pct": 1, "max_drawdown_pct": -8,
    } for day in (24, 25, 26)]
    learned = learn_from_outcomes({"modelOffsets": {}}, failures, date(2026, 8, 28))
    assert learned["stage"] == "calibration"
    assert abs(learned["pendingAdjustments"]["M3"]) <= 2
    assert learned["failureProtection"]["M3"]["paused"] is True
    assert learned["failureProtection"]["M3"]["thresholdAdd"] == 5


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ignition_3d": True, "max_return_pct": 10, "max_drawdown_pct": -2}, "S"),
        ({"max_return_pct": 7.2, "max_drawdown_pct": -2}, "A"),
        ({"max_return_pct": 3.1, "max_drawdown_pct": -2}, "B"),
        ({"max_return_pct": 1.0, "max_drawdown_pct": -2}, "N"),
        ({"max_return_pct": 8.0, "max_drawdown_pct": -8}, "F"),
    ],
)
def test_result_labels_cover_s_a_b_n_f(payload: dict, expected: str) -> None:
    assert classify_result_label(payload) == expected


def test_candidate_changes_tracks_added_removed_rank_and_score() -> None:
    prior = {"run_id": "old", "candidates": [{"code": "600001", "rank": 1, "score": 80}, {"code": "600002", "rank": 2, "score": 70}]}
    current = [{"code": "600001", "rank": 2, "score": 81}, {"code": "600003", "rank": 1, "score": 85}]
    changes = candidate_changes(prior, current)
    assert changes["added"] == ["600003"]
    assert changes["removed"] == ["600002"]
    assert changes["changed"] is True
