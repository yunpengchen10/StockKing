from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.config import Config
from src.services.yao_scout.labels import LABEL_SCHEMA_VERSION, calculate_limit_price, evaluate_outcome_labels
from src.services.yao_scout.model import predict_probabilities, train_challenger
from src.services.yao_scout.service import (
    SHANGHAI_TZ,
    YaoScoutService,
    _is_cn_trading_day,
    _truncate_contexts_to_cutoff,
)
from src.storage import DatabaseManager


@pytest.fixture()
def yao_db(tmp_path: Path):
    previous = DatabaseManager._instance
    if previous is not None and getattr(previous, "_engine", None) is not None:
        previous._engine.dispose()
    DatabaseManager._instance = None
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'yao.sqlite').as_posix()}")
    yield db
    if getattr(db, "_engine", None) is not None:
        db._engine.dispose()
    DatabaseManager._instance = None


def _history(rows: int = 100, *, end: date = date(2026, 8, 27)) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=rows)
    close = pd.Series([8.0 + index * 0.03 for index in range(rows)])
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": [10_000_000 + index * 20_000 for index in range(rows)],
        "amount": [100_000_000 for _ in range(rows)],
    })


def _snapshot() -> pd.DataFrame:
    frame = pd.DataFrame([
        {"code": "600127", "name": "金健米业", "price": 12.0, "change_pct": 8.2, "amount": 900_000_000, "total_mv": 8_000_000_000, "circ_mv": 7_000_000_000, "volume_ratio": 2.8, "turnover_rate": 12.0},
        {"code": "003040", "name": "楚天龙", "price": 21.0, "change_pct": 7.1, "amount": 700_000_000, "total_mv": 10_000_000_000, "circ_mv": 7_500_000_000, "volume_ratio": 2.2, "turnover_rate": 8.0},
        {"code": "002084", "name": "海鸥住工", "price": 8.0, "change_pct": 10.0, "amount": 30_000_000, "total_mv": 5_000_000_000, "circ_mv": 5_000_000_000, "volume_ratio": 0.2, "turnover_rate": 0.2},
        {"code": "600001", "name": "*ST测试", "price": 4.0, "change_pct": 5.0, "amount": 100_000_000, "total_mv": 2_000_000_000, "circ_mv": 2_000_000_000, "volume_ratio": 2.0, "turnover_rate": 3.0},
        {"code": "430001", "name": "北交测试", "price": 10.0, "change_pct": 20.0, "amount": 100_000_000, "total_mv": 2_000_000_000, "circ_mv": 2_000_000_000, "volume_ratio": 3.0, "turnover_rate": 5.0},
    ])
    frame.attrs["snapshot_source"] = "fixture"
    frame.attrs["source_errors"] = []
    frame.attrs["stale"] = False
    return frame


def test_historical_board_limit_price_and_chinext_cutover() -> None:
    assert calculate_limit_price(10.05, "600127", date(2026, 8, 27)) == 11.06
    assert calculate_limit_price(10.00, "300001", date(2020, 8, 21)) == 11.00
    assert calculate_limit_price(10.00, "300001", date(2020, 8, 24)) == 12.00
    assert calculate_limit_price(10.00, "688001", date(2026, 8, 27)) == 12.00
    assert calculate_limit_price(10.00, "600001", date(2026, 8, 27), is_st=True) == 10.50


def test_one_price_limits_are_strong_but_unavailable() -> None:
    rows = [{"date": "2026-08-20", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1_000_000}]
    previous = 10.0
    for day in (21, 24, 25, 26, 27, 28, 31, 1, 2, 3):
        current_date = date(2026, 8, day) if day >= 20 else date(2026, 9, day)
        previous = calculate_limit_price(previous, "002084", current_date)
        rows.append({"date": current_date.isoformat(), "open": previous, "high": previous, "low": previous, "close": previous, "volume": 10_000})
    outcome = evaluate_outcome_labels(pd.DataFrame(rows), code="002084", observation_date=date(2026, 8, 20), entry_price=10.0)
    assert outcome["ignition_3d"] is True
    assert outcome["strong_10d"] is True
    assert outcome["tradability"]["status"] == "unavailable_one_price_limit"


def test_missing_future_sessions_are_deferred_not_failure() -> None:
    outcome = evaluate_outcome_labels(_history(20, end=date(2026, 8, 27)), code="600127", observation_date=date(2026, 8, 27))
    assert outcome["maturity_status"] == "deferred"
    assert outcome["ignition_3d"] is None


def test_five_day_continuation_does_not_use_sixth_to_tenth_day_highs() -> None:
    days = pd.bdate_range("2026-08-20", periods=11)
    rows = [{"date": day, "open": 10, "high": 10.1, "low": 9.9, "close": 10} for day in days]
    rows[6].update(open=10, high=14, low=9.9, close=10)
    result = evaluate_outcome_labels(pd.DataFrame(rows), code="600127",
                                    observation_date=days[0].date(), entry_price=10)
    assert result["continuation_5d"] is False
    assert result["strong_10d"] is True


def test_cn_calendar_skips_weekends_and_known_holiday() -> None:
    assert _is_cn_trading_day(date(2026, 8, 29)) is False
    assert _is_cn_trading_day(date(2026, 10, 1)) is False


def test_universe_excludes_st_bse_and_marks_low_turnover_limit_unavailable(tmp_path: Path, yao_db: DatabaseManager) -> None:
    service = YaoScoutService(config=Config(), db_manager=yao_db, data_dir=tmp_path)
    filtered = service._filter_universe(_snapshot())
    assert set(filtered["code"]) == {"600127", "003040", "002084"}
    row = {**filtered.loc[filtered["code"] == "002084"].iloc[0].to_dict(), "daily_data_points": 100}
    candidate = service._score_candidate(row, None, {"source": "fixture", "point_in_time_ok": True})
    assert candidate["tradability"]["status"] == "unavailable"
    assert any("无法保证成交" in risk for risk in candidate["risks"])


def test_preopen_rejects_snapshot_created_after_0855(tmp_path: Path, yao_db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    service = YaoScoutService(config=Config(), db_manager=yao_db, data_dir=tmp_path)
    prefetch_path = service._prefetch_path(date(2026, 8, 27))
    prefetch_path.parent.mkdir(parents=True, exist_ok=True)
    prefetch_path.write_text(
        '{"created_at":"2026-08-27T08:56:00+08:00","snapshot_source":"fixture","snapshot":[],"contexts":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr("src.services.yao_scout.service.is_market_open", lambda *_: True)
    result = service.run("preopen", as_of=datetime(2026, 8, 27, 9, 0, tzinfo=SHANGHAI_TZ))
    assert result["status"] == "no_qualified_signal"
    assert result["candidate_count"] == 0
    assert result["data_quality"]["point_in_time_ok"] is False


def test_context_news_after_cutoff_is_removed() -> None:
    contexts, dropped = _truncate_contexts_to_cutoff(
        [{
            "code": "300364",
            "news": (
                "2026-08-31 14:13:08 盘中涨停 | "
                "2026-08-31 16:14:00 收盘后报道"
            ),
            "announcement": "2026-08-29 公司公告",
        }],
        datetime(2026, 8, 31, 15, 0, tzinfo=SHANGHAI_TZ),
    )
    assert dropped == 1
    assert "14:13:08" in contexts[0]["news"]
    assert "16:14:00" not in contexts[0]["news"]
    assert contexts[0]["announcement"] == "2026-08-29 公司公告"


def test_run_persistence_is_idempotent(tmp_path: Path, yao_db: DatabaseManager) -> None:
    payload = {
        "run_id": "yao-fixed",
        "mode": "preopen",
        "status": "completed",
        "as_of": "2026-08-27T09:00:00+08:00",
        "cutoff_at": "2026-08-27T08:55:00+08:00",
        "model_version": "yao-audit-v1",
        "candidates": [{
            "rank": 1,
            "code": "600127",
            "name": "金健米业",
            "tier": "A",
            "score": 80,
            "features": {"price": 10},
        }],
    }
    assert yao_db.save_yao_run(payload) == 1
    assert yao_db.save_yao_run(payload) == 1
    assert len(yao_db.list_yao_runs()) == 1
    assert len(yao_db.list_pending_yao_outcomes()) == 1


def test_intraday_outcomes_only_include_major_transition_codes(yao_db: DatabaseManager) -> None:
    payload = {
        "run_id": "yao-intraday-filtered",
        "mode": "intraday",
        "status": "completed",
        "as_of": "2026-09-01T09:35:00+08:00",
        "cutoff_at": "2026-09-01T09:35:00+08:00",
        "model_version": "yao-audit-v1",
        "training_eligible_codes": ["600127"],
        "candidates": [
            {"rank": 1, "code": "600127", "name": "金健米业", "score": 65, "features": {"price": 10}},
            {"rank": 2, "code": "002084", "name": "海鸥住工", "score": 60, "features": {"price": 8}},
        ],
    }
    assert yao_db.save_yao_run(payload) == 1
    pending = yao_db.list_pending_yao_outcomes()
    assert [item["code"] for item in pending] == ["600127"]


def test_challenger_uses_time_holdout_and_cannot_promote_small_case_sample(tmp_path: Path) -> None:
    records = []
    start = date(2024, 1, 1)
    for index in range(420):
        strength = (index % 10) / 10
        features = {
            "change_1d": strength * 8,
            "change_60d": strength * 30,
            "volume_ratio_20d": 0.8 + strength * 3,
            "volatility_20d_pct": 20 + strength * 40,
            "max_drawdown_20d_pct": -15 + strength * 10,
            "breakout_20d_pct": -3 + strength * 12,
        }
        hit = strength >= 0.7
        records.append({
            "code": f"{600000 + index % 20:06d}",
            "as_of": (start + timedelta(days=index)).isoformat(),
            "features": features,
            "labels": {
                "labelSchemaVersion": LABEL_SCHEMA_VERSION,
                "maturity_status": "mature",
                "ignition_3d": hit,
                "continuation_5d": hit,
                "strong_10d": hit,
                "max_drawdown_pct": -8.0 if hit else -12.0,
            },
        })
    result = train_challenger(records, symbol_count=20, years=5, output_dir=tmp_path)
    assert result["metrics"]["split_date"] < records[-1]["as_of"]
    assert result["gates"]["coverage_gate"] is False
    assert result["gates"]["qualified"] is False
    assert Path(result["artifact_path"]).is_file()


def test_legacy_champion_and_old_label_records_cannot_validate_fixed_labels(tmp_path: Path) -> None:
    import json
    champion = tmp_path / "champion.json"
    champion.write_text(json.dumps({"model_version": "old", "gates": {"qualified": True}}), encoding="utf-8")
    probabilities, status, version = predict_probabilities({}, champion_path=champion)
    assert status == "withheld_incompatible_label_schema" and version == "old"
    assert all(value is None for value in probabilities.values())
    result = train_challenger([{"labels": {"maturity_status": "mature"}}] * 200,
                             symbol_count=1000, years=5, output_dir=tmp_path)
    assert result["metrics"]["sample_count"] == 0
    assert result["labelSchemaVersion"] == LABEL_SCHEMA_VERSION
    assert json.loads(champion.read_text(encoding="utf-8"))["model_version"] == "old"
