from __future__ import annotations

import copy
import csv
import io
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.quant.executable_backtest import BacktestRequest, example_dataset, run_backtest


def fixture():
    data = example_dataset()
    data["provenance"] = "user_supplied_point_in_time"
    data["config"].update(initial_cash=10000, position_weight=1, holding_sessions=1,
                           slippage_bps=0, commission_rate=0, minimum_commission=0,
                           transfer_fee_rate=0, stamp_tax_rate=0)
    for bar in data["bars"]:
        bar.update(open=10, close=10, high=11, low=9, limit_up=11, limit_down=9)
    return data


def test_account_conserves_cash_and_values_positions_every_session():
    data = fixture()
    data["config"].update(commission_rate=0.0003, minimum_commission=5, stamp_tax_rate=0.0005,
                           transfer_fee_rate=0.00001, slippage_bps=10)
    result = run_backtest(data)
    cash = 10000
    shares = 0
    for trade in result["trades"]:
        if trade["side"] == "buy":
            cash -= trade["notional"] + trade["fees"]["total"]
            shares += trade["quantity"]
            assert trade["fees"]["stamp_tax"] == 0
        else:
            cash += trade["notional"] - trade["fees"]["total"]
            shares -= trade["quantity"]
            assert trade["fees"]["stamp_tax"] == 4.5
        assert cash == pytest.approx(trade["cash_after"])
        assert cash >= 0
    assert shares == 0
    assert cash == pytest.approx(9967.32)
    assert result["summary"]["final_equity"] == pytest.approx(cash)
    for point in result["equity_curve"]:
        assert point["equity"] == pytest.approx(point["cash"] + point["market_value"])
    assert result["summary"]["realized_pnl"] == pytest.approx(cash - 10000)


def test_same_day_information_never_trades_at_earlier_open_and_t1_exit():
    data = fixture()
    data["signals"][0]["available_at"] = "2026-08-04T15:30:00+08:00"
    result = run_backtest(data)
    assert [trade["date"] for trade in result["trades"]] == ["2026-08-05", "2026-08-06"]
    assert result["closed_positions"][0]["holding_sessions"] == 1
    assert result["equity_curve"][1]["positions"] == 0


def test_session_delay_uses_supplied_calendar_and_crosses_weekend():
    data = fixture()
    data["signals"][0].update(signal_date="2026-08-06", available_at="2026-08-06T15:30:00+08:00")
    data["config"]["delay_sessions"] = 1
    result = run_backtest(data)
    assert result["trades"][0]["date"] == "2026-08-10"
    assert result["trades"][1]["date"] == "2026-08-11"


@pytest.mark.parametrize("change,reason", [({"open": 11, "high": 11, "close": 11}, "open_at_limit_up"),
    ({"volume": 0}, "suspended_or_zero_volume"), ({"suspended": True}, "suspended_or_zero_volume")])
def test_unfillable_entry_expires_without_chasing_later_prices(change, reason):
    data = fixture()
    data["bars"][1].update(change)
    result = run_backtest(data)
    assert result["trades"] == []
    assert result["summary"]["final_equity"] == 10000
    assert result["summary"]["entry_fill_rate"] == 0
    assert result["rejected_orders"][0]["reason"] == reason


def test_limit_down_exit_retries_and_does_not_invent_forced_liquidation():
    data = fixture()
    for bar in data["bars"][2:]:
        bar.update(open=9, high=9, low=9, close=9)
    result = run_backtest(data)
    assert len(result["trades"]) == 1
    assert result["summary"]["open_positions"] == 1
    assert result["summary"]["closed_trades"] == 0
    assert result["summary"]["win_rate"] is None
    assert result["summary"]["final_equity"] == 9000
    assert all(order["reason"] == "open_at_limit_down" for order in result["rejected_orders"])


def test_exit_can_fill_later_and_keeps_original_holding_reason():
    data = fixture()
    data["bars"][2].update(open=9, high=9, low=9, close=9)
    result = run_backtest(data)
    assert result["trades"][1]["date"] == "2026-08-06"
    assert result["trades"][1]["reason"] == "holding_period"
    assert result["closed_positions"][0]["holding_sessions"] == 2


def test_missing_bar_and_missing_benchmark_fail_closed():
    data = fixture()
    data["bars"].pop(3)
    with pytest.raises(ValueError, match="行情不完整"):
        run_backtest(data)
    data = fixture()
    data["benchmark"].pop(3)
    with pytest.raises(ValueError, match="基准必须覆盖"):
        run_backtest(data)


def test_zero_holding_period_is_rejected_instead_of_allowing_t0():
    data = fixture()
    data["config"]["holding_sessions"] = 0
    with pytest.raises(ValidationError):
        run_backtest(data)


def test_timestamp_and_provenance_are_required():
    data = fixture()
    data["signals"][0]["available_at"] = "2026-08-03T15:30:00"
    with pytest.raises(ValueError, match="时区"):
        run_backtest(data)
    data = fixture()
    data["point_in_time_confirmed"] = False
    with pytest.raises(ValueError, match="事前记录"):
        run_backtest(data)
    data = fixture()
    data["provenance"] = "today_recommendations_backfilled"
    with pytest.raises(ValidationError):
        run_backtest(data)


def test_no_usable_signal_refuses_to_generate_performance():
    data = fixture()
    data["signals"][0]["available_at"] = "2026-08-14T16:00:00+08:00"
    with pytest.raises(ValueError, match="可执行的信号"):
        run_backtest(data)
    data["signals"] = []
    with pytest.raises(ValueError, match="缺少事前信号"):
        run_backtest(data)


def test_future_price_changes_do_not_affect_earlier_executions():
    data = fixture()
    original = run_backtest(data)
    data["bars"][-1].update(open=10.5, close=10.6)
    changed = run_backtest(data)
    assert changed["trades"] == original["trades"]
    assert changed["equity_curve"][:-1] == original["equity_curve"][:-1]
    assert changed["input_sha256"] != original["input_sha256"]
    assert run_backtest(data) == changed


def test_minimum_lot_and_cash_constraints_do_not_create_fractional_shares():
    data = fixture()
    data["config"]["initial_cash"] = 999
    result = run_backtest(data)
    assert not result["trades"]
    assert result["rejected_orders"][0]["reason"] == "insufficient_cash_or_minimum_lot"
    data["config"]["initial_cash"] = 2510
    for signal in data["signals"]:
        signal["symbol"] = "688001.SH"
    for bar in data["bars"]:
        bar["symbol"] = "688001.SH"
    result = run_backtest(data)
    assert result["trades"][0]["quantity"] == 251  # STAR minimum 200, increments of 1.


def test_overlapping_signal_does_not_double_buy_existing_position():
    data = fixture()
    data["config"]["holding_sessions"] = 5
    duplicate = {**data["signals"][0], "signal_id": "duplicate-date"}
    data["signals"].append(duplicate)
    result = run_backtest(data)
    assert sum(trade["side"] == "buy" for trade in result["trades"]) == 1
    assert result["rejected_orders"][0]["reason"] == "existing_position_or_exit_signal"


def test_cash_and_max_positions_apply_across_symbols():
    data = fixture()
    second = {**data["signals"][0], "signal_id": "second", "symbol": "000001.SZ"}
    data["signals"].append(second)
    data["bars"] += [{**bar, "symbol": "000001.SZ"} for bar in data["bars"]]
    data["config"].update(max_positions=1, position_weight=0.5)
    result = run_backtest(data)
    assert result["rejected_orders"][0]["reason"] == "max_positions"
    data["config"].update(max_positions=2, position_weight=1)
    result = run_backtest(data)
    assert result["rejected_orders"][0]["reason"] == "insufficient_cash_or_minimum_lot"


def test_stop_loss_uses_previous_close_and_never_sells_on_purchase_day():
    data = fixture()
    data["config"].update(holding_sessions=9, stop_loss_pct=0.08)
    data["bars"][1].update(close=9)
    result = run_backtest(data)
    assert result["trades"][0]["date"] == "2026-08-04"
    assert result["trades"][1]["date"] == "2026-08-05"
    assert result["trades"][1]["reason"] == "previous_close_stop_loss"


def test_explicit_exit_signal_executes_after_availability():
    data = fixture()
    data["config"]["holding_sessions"] = 9
    data["signals"].append({**data["signals"][0], "signal_id": "exit", "side": "sell",
                            "signal_date": "2026-08-05", "available_at": "2026-08-05T16:00:00+08:00"})
    result = run_backtest(data)
    assert result["trades"][1]["date"] == "2026-08-06"
    assert result["trades"][1]["reason"] == "signal_exit"


def test_csv_and_json_have_identical_performance_and_fingerprint():
    data = fixture()
    csv_data = copy.deepcopy(data)
    for key in ("signals", "bars", "calendar", "benchmark"):
        rows = csv_data.pop(key)
        if key == "calendar":
            rows = [{"date": item} for item in rows]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        csv_data[f"{key}_csv"] = output.getvalue()
    assert run_backtest(csv_data) == run_backtest(data)


@pytest.mark.parametrize("field,value", [("close", float("nan")), ("volume", -1), ("corporate_action", True), ("limit_up", None)])
def test_bad_price_inputs_do_not_produce_plausible_nav(field, value):
    data = fixture()
    data["bars"][1][field] = value
    with pytest.raises(ValidationError):
        run_backtest(data)


def test_endpoint_persists_replayable_report_and_rejects_path_traversal(monkeypatch, tmp_path):
    from api.v1.endpoints import quant
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    result = quant.run_executable_backtest(BacktestRequest.model_validate(fixture()))
    assert result["report_id"] == result["input_sha256"]
    saved = quant.get_executable_report(result["report_id"])
    assert saved["result"] == result
    replayed = run_backtest(saved["input"])
    assert replayed["summary"] == result["summary"]
    assert replayed["input_sha256"] == result["input_sha256"]
    assert quant.list_executable_reports(10)["reports"][0]["report_id"] == result["report_id"]
    with pytest.raises(HTTPException) as caught:
        quant.get_executable_report("../tasks/secret")
    assert caught.value.status_code == 422
