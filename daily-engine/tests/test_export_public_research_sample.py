"""Offline checks for the checked-in public-data research example."""
from datetime import date
import importlib.util
import json
import re
from pathlib import Path

import pandas as pd


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "daily-engine/scripts/export_public_research_sample.py"
SAMPLE = REPO / "docs/research/stockking-v11-market-sample.json"
spec = importlib.util.spec_from_file_location("public_research_export", SCRIPT)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def test_checked_in_sample_is_real_public_observation_with_missingness():
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert payload["asOfMarketDate"] == "2026-09-29"
    assert payload["historicalRecommendation"] is False
    assert payload["tradeAdvice"] is False
    assert {item["code"] for item in payload["instruments"]} == {"600000", "000001", "600519"}
    for item in payload["instruments"]:
        bars = item["daily"]["bars"]
        assert len(bars) == 60
        assert bars[0]["date"] == "2026-07-07" and bars[-1]["date"] == "2026-09-29"
        assert all(0 < bar["low"] <= min(bar["open"], bar["close"])
                   <= max(bar["open"], bar["close"]) <= bar["high"] for bar in bars)
        assert all("amount_cny" in bar and "amountSource" in bar for bar in bars)
        assert item["daily"]["amountMissingBars"] == 59
        assert bars[-1]["amountSource"] == "tencent_post_close_cumulative_quote"
        assert item["quote"]["currentPriceCheckUsable"] is False
        assert item["quote"]["executionVerified"] is False
        assert len(item["minute"]["sampleBars"]) == 12
        assert item["modelOutputs"]["pMainrise"] is None
        assert item["modelOutputs"]["expectedMfe5"] is None
        assert item["modelOutputs"]["expectedMae5"] is None
    raw = SAMPLE.read_text(encoding="utf-8")
    assert "AppData" not in raw and ".sqlite" not in raw
    assert re.search(r"[A-Za-z]:[\\/]+Users[\\/]+", raw, re.IGNORECASE) is None


def test_formatter_keeps_unavailable_amount_null_and_returns_price_only():
    days = pd.date_range("2026-08-01", periods=21, freq="B")
    frame = pd.DataFrame({
        "date": [day.date().isoformat() for day in days],
        "open": [10 + index * .1 for index in range(21)],
        "high": [10.2 + index * .1 for index in range(21)],
        "low": [9.8 + index * .1 for index in range(21)],
        "close": [10 + index * .1 for index in range(21)],
        "volume": [100000] * 21,
        "amount": [float("nan")] * 21,
    })
    frame.attrs["daily_source"] = "sina"
    last = days[-1].date()
    quote_row = {
        "quote": {"source": "tencent", "source_time": last.isoformat() + "T16:00:00+08:00",
                  "price": 12, "previous_close": 11.9, "amount_cny": 1234567},
        "phase": "post_close", "status": "reference_only",
        "quote_usable_for_current_price_check": False,
    }
    result = export.build_instrument("600000", frame, [], quote_row,
                                     {"checked_at": last.isoformat() + "T17:00:00+08:00"}, last)
    assert result["daily"]["bars"][0]["amount_cny"] is None
    assert result["daily"]["bars"][-1]["amount_cny"] == 1234567
    assert result["daily"]["amountMissingBars"] == 20
    assert result["metrics"]["twentySessionCloseReturnPct"] == 20.0
    assert result["modelOutputs"]["pMainrise"] is None
