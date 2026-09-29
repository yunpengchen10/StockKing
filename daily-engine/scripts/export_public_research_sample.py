"""Export a small, source-labelled public market-data research example.

Run from the repository root:
    python daily-engine/scripts/export_public_research_sample.py \
        --as-of 2026-09-29 --output docs/research/stockking-v11-market-sample.json

Only the application's existing public Tencent/Sina quote, Sina daily-K-line,
and Sina minute-K-line adapters are called. No user DB, API key, watchlist,
historical recommendation, or model prediction is read or exported.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any
from zoneinfo import ZoneInfo


ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

SHANGHAI = ZoneInfo("Asia/Shanghai")
REPO_DIR = ENGINE_DIR.parent
DEFAULT_CODES = ("600000", "000001", "600519")
NAMES = {"600000": "浦发银行", "000001": "平安银行", "600519": "贵州茅台"}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, places: int = 4) -> float | None:
    return round(value, places) if value is not None else None


def _price_metrics(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Historical price-only observations, computed without a future label."""
    closes = [bar["close"] for bar in bars if bar.get("close") is not None and bar["close"] > 0]
    if len(closes) != len(bars):
        return {"status": "insufficient_valid_close_history"}

    def change(sessions: int) -> float | None:
        return 100 * (closes[-1] / closes[-sessions - 1] - 1) if len(closes) > sessions else None

    log_returns = [math.log(closes[index] / closes[index - 1])
                   for index in range(max(1, len(closes) - 20), len(closes))]
    volatility = (statistics.stdev(log_returns) * math.sqrt(252) * 100
                  if len(log_returns) == 20 else None)
    return {
        "status": "observed_price_only", "throughDate": bars[-1]["date"] if bars else None,
        "oneSessionCloseReturnPct": _round(change(1), 3),
        "fiveSessionCloseReturnPct": _round(change(5), 3),
        "twentySessionCloseReturnPct": _round(change(20), 3),
        "ma5CloseCny": _round(statistics.mean(closes[-5:]), 2) if len(closes) >= 5 else None,
        "ma20CloseCny": _round(statistics.mean(closes[-20:]), 2) if len(closes) >= 20 else None,
        "twentyReturnAnnualizedVolatilityPct": _round(volatility, 2),
        "returnBasis": "unadjusted_close_to_close_price_only; excludes dividends, fees, and execution",
        "volatilityBasis": "sample standard deviation of 20 daily log returns, annualized by sqrt(252)",
    }


def _minute_grid(day: date) -> set[str]:
    result: set[str] = set()
    for start, end in ((time(9, 31), time(11, 30)), (time(13, 1), time(15, 0))):
        current = datetime.combine(day, start, SHANGHAI)
        finish = datetime.combine(day, end, SHANGHAI)
        while current <= finish:
            result.add(current.isoformat())
            current += timedelta(minutes=1)
    return result


def _quote_record(row: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    quote = row.get("quote") or {}
    return {
        "source": quote.get("source"), "sourceTime": quote.get("source_time"),
        "checkedAt": batch.get("checked_at"), "marketPhase": row.get("phase"),
        "status": row.get("status"), "currentPriceCheckUsable": bool(
            row.get("quote_usable_for_current_price_check")),
        "executionVerified": False, "priceCny": _finite(quote.get("price")),
        "previousCloseCny": _finite(quote.get("previous_close")),
        "openCny": _finite(quote.get("open")), "highCny": _finite(quote.get("high")),
        "lowCny": _finite(quote.get("low")), "volumeShares": _finite(quote.get("volume_shares")),
        "cumulativeAmountCny": _finite(quote.get("amount_cny")),
        "issues": [str(issue) for issue in row.get("issues") or []],
    }


def build_instrument(code: str, daily_frame: Any, minute_rows: list[dict[str, Any]],
                     quote_row: dict[str, Any], quote_batch: dict[str, Any],
                     as_of: date) -> dict[str, Any]:
    """Pure formatter so tests can validate provenance without a network call."""
    from src.services.yao_scout.minute_history import normalize_bars

    quote = _quote_record(quote_row, quote_batch)
    bars: list[dict[str, Any]] = []
    gaps: list[str] = []
    daily_source = str(getattr(daily_frame, "attrs", {}).get("daily_source") or "unknown")
    for item in daily_frame.to_dict("records"):
        day = str(item.get("date"))[:10]
        if day > as_of.isoformat():
            continue
        values = {key: _finite(item.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if any(values[key] is None for key in ("open", "high", "low", "close", "volume")):
            gaps.append(f"invalid_daily_bar:{day}")
            continue
        bars.append({"date": day, **values, "amount_cny": None, "amountSource": None})
    bars = sorted(bars, key=lambda bar: bar["date"])[-60:]
    if not bars:
        gaps.append("daily_history_unavailable")

    cutoff = datetime.combine(as_of, time(15, 0), SHANGHAI)
    normalized = normalize_bars(minute_rows, cutoff)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for stamp, bar in sorted(normalized.items()):
        by_date.setdefault(stamp.date().isoformat(), []).append(bar)
    coverage = []
    for day_text, rows in sorted(by_date.items()):
        expected = _minute_grid(date.fromisoformat(day_text))
        observed = {row["end"] for row in rows}
        complete = expected == observed and all(row.get("amount_cny") is not None for row in rows)
        coverage.append({"date": day_text, "observedBars": len(rows), "expectedBars": len(expected),
                         "completeGrid": complete,
                         "observedPartialAmountCny": _round(sum(row.get("amount_cny") or 0 for row in rows), 2)})
        if complete:
            matching = next((bar for bar in bars if bar["date"] == day_text), None)
            if matching is not None and matching["volume"] > 0:
                minute_volume = sum(row.get("volume_shares") or 0 for row in rows)
                if abs(minute_volume / matching["volume"] - 1) <= 0.01:
                    matching["amount_cny"] = _round(sum(row["amount_cny"] for row in rows), 2)
                    matching["amountSource"] = "Sina_complete_1m_amount_sum"
                else:
                    gaps.append(f"minute_daily_volume_disagreement:{day_text}")

    # The daily K-line endpoint has no amount. A same-day post-close public
    # cumulative quote can supply only that day's figure, with explicit source.
    quote_day = str(quote.get("sourceTime") or "")[:10]
    if (quote_day == as_of.isoformat() and quote.get("marketPhase") == "post_close"
            and quote.get("cumulativeAmountCny") is not None):
        matching = next((bar for bar in bars if bar["date"] == quote_day), None)
        if matching is not None and matching["amount_cny"] is None:
            matching["amount_cny"] = quote["cumulativeAmountCny"]
            matching["amountSource"] = f"{quote.get('source') or 'public'}_post_close_cumulative_quote"
    missing_amount = sum(bar["amount_cny"] is None for bar in bars)
    if missing_amount:
        gaps.append(f"daily_amount_unavailable_for_{missing_amount}_of_{len(bars)}_bars")
    if not quote["currentPriceCheckUsable"]:
        gaps.append("quote_not_current_executable_price")
    today_minutes = by_date.get(as_of.isoformat(), [])
    minute_sample = [{"end": row["end"], "open": row["open"], "high": row["high"],
                      "low": row["low"], "close": row["close"],
                      "volume_shares": row["volume_shares"], "amount_cny": row["amount_cny"]}
                     for row in today_minutes[-12:]]
    return {
        "code": code, "name": NAMES[code], "quote": quote,
        "daily": {"source": daily_source, "priceAdjustment": "none",
                  "volumeUnit": "source_reported_shares", "bars": bars,
                  "amountMissingBars": missing_amount},
        "minute": {"source": "Sina 1m unadjusted", "barTimeMeaning": "minute_end",
                   "coverageByDate": coverage, "sampleBars": minute_sample},
        "metrics": _price_metrics(bars),
        "modelOutputs": {"pMainrise": None, "expectedMfe5": None, "expectedMae5": None,
                         "reason": "no_historical_point_in_time_scan_or_calibrated_model"},
        "gaps": gaps,
    }


def fetch_sample(as_of: date) -> dict[str, Any]:
    from src.services.public_market_quotes import get_public_market_quotes
    from src.services.screening.daily import fetch_daily_history
    from src.services.yao_scout.minute_history import fetch_sina_bars

    batch = get_public_market_quotes(list(DEFAULT_CODES))
    quote_rows = {row["code"]: row for row in batch["quotes"]}
    instruments = []
    for code in DEFAULT_CODES:
        frame = fetch_daily_history(code, lookback_days=60, source="sina", retries=0)
        minutes = fetch_sina_bars(code, count=1970)
        instruments.append(build_instrument(code, frame, minutes, quote_rows[code], batch, as_of))
    return {
        "schemaVersion": "stockking-v11-public-market-research-sample/1",
        "asOfMarketDate": as_of.isoformat(),
        "retrievedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "purpose": "public_data_research_replay_not_desktop_screen_recording",
        "historicalRecommendation": False, "tradeAdvice": False,
        "sources": {
            "quote": "Stock King public_market_quotes: Tencent/Sina quote assessment",
            "daily": "Stock King screening.daily.fetch_daily_history(source='sina'): Sina 240-minute K-line, unadjusted",
            "minute": "Stock King yao_scout.minute_history.fetch_sina_bars: Sina 1-minute K-line, unadjusted",
            "amount": "Sina daily amount missing; only complete minute sums or same-day post-close public cumulative quote are populated",
        },
        "instruments": instruments,
        "limitations": [
            "This is an after-the-fact public market observation, never a historical King scan or recommendation.",
            "Quote provider source_time is not order execution time; stale post-close quotes are reference only.",
            "Daily price returns are unadjusted price-only observations, not dividend-adjusted or executable returns.",
            "Minute coverage is short and may have gaps; missing daily amount stays null rather than being estimated.",
            "No model probability, MFE/MAE forecast, or strategy performance is inferred from these three securities.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat,
                        default=datetime.now(SHANGHAI).date(), help="market date YYYY-MM-DD")
    parser.add_argument("--output", type=Path,
                        default=REPO_DIR / "docs/research/stockking-v11-market-sample.json")
    args = parser.parse_args()
    result = fetch_sample(args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(f"Wrote {args.output}: {len(result['instruments'])} symbols; as-of {args.as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
