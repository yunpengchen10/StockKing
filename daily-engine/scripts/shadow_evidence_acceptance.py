#!/usr/bin/env python3
"""Exercise the real local recommendation entry with synthetic, isolated inputs.

This writes JSON fixtures under daily-engine/output only. It never opens a real
database, sends mail, fetches the network, or calls an AI. Every output identifies
itself as synthetic and must not be used as a stock recommendation.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests

from src.services.yao_scout import daily_opportunities as base
from src.services.yao_scout.local_opportunities import LocalOpportunityService


class MemoryRepository:
    """Only the repository surface the actual local scanner needs."""

    def __init__(self):
        self.saved = []

    def list_yao_runs(self, **kwargs):
        return []

    def list_yao_dlm(self, **kwargs):
        return []

    def save_yao_run(self, run):
        self.saved.append(deepcopy(run))

    def get_yao_adaptive_state(self, *args, **kwargs):
        return None


SCENARIOS = {
    "600001": {"name": "合成·创新高突破", "price": 10.7, "previous": 9.8, "open": 10.4, "low": 10.1, "high": 10.8, "vwap": 10.6, "minute": "fresh"},
    "600002": {"name": "合成·深水局部修复", "price": 9.8, "previous": 10.2, "open": 9.6, "low": 9.2, "high": 10, "vwap": 9.9, "minute": "fresh"},
    "600003": {"name": "合成·仅全天热度", "price": 10.5, "previous": 9.8, "open": 10.4, "low": 10.2, "high": 10.6, "vwap": 10.4, "minute": "missing"},
    "600004": {"name": "合成·陈旧分钟", "price": 10.7, "previous": 9.8, "open": 10.4, "low": 10.1, "high": 10.8, "vwap": 10.6, "minute": "stale"},
    "600005": {"name": "合成·报价时间缺失", "price": 10.7, "previous": 9.8, "open": 10.4, "low": 10.1, "high": 10.8, "vwap": 10.6, "minute": "fresh", "missing_quote_time": True},
    "600006": {"name": "合成·封板风向标", "price": 10.78, "previous": 9.8, "open": 10.4, "low": 10.1, "high": 10.78, "vwap": 10.6, "minute": "fresh", "locked": True},
    "600007": {"name": "合成·未来分钟", "price": 10.7, "previous": 9.8, "open": 10.4, "low": 10.1, "high": 10.8, "vwap": 10.6, "minute": "future"},
}


def fixture_run(profile: Path, slot="1030", *, mismatched_premarket=False):
    target = datetime(2026, 9, 15, 9 if slot == "0920" else 10, 20 if slot == "0920" else 30, tzinfo=base.TZ)
    tick = [target - timedelta(minutes=6)]
    events = []
    repository = MemoryRepository()
    scenarios = {"600001": SCENARIOS["600001"]} if slot == "0920" else SCENARIOS

    def snapshot():
        events.append({"kind": "snapshot", "at": tick[0].isoformat()})
        frame = pd.DataFrame([{
            "code": code, "name": row["name"], "price": row["price"], "open": row["open"],
            "change_pct": (row["price"] / row["previous"] - 1) * 100,
            "amount": 1000000000, "volume_ratio": 3.5, "turnover_rate": 12,
        } for code, row in scenarios.items()])
        frame.attrs.update(snapshot_source="synthetic://cross-section", source_time=tick[0].isoformat(), stale=False)
        return frame

    def history(code, **kwargs):
        previous = scenarios[code]["previous"]
        frame = pd.DataFrame({"date": pd.bdate_range(end="2026-09-14", periods=60),
                              "close": [previous - .6 + index * .6 / 59 for index in range(60)]})
        frame["high"], frame["low"] = frame.close + .2, frame.close - .2
        frame.attrs["daily_source"] = "synthetic://completed-daily-bars"
        return frame

    def quote(code):
        events.append({"kind": "quote", "code": code, "at": tick[0].isoformat()})
        row = scenarios[code]
        result = {"code": "600999" if mismatched_premarket else code, "price": row["price"],
                  "pre_close": row["previous"], "open_price": row["open"], "high": row["high"], "low": row["low"],
                  "change_pct": (row["price"] / row["previous"] - 1) * 100,
                  "amount": row["vwap"] * 100000000, "volume": 100000000,
                  "source": "synthetic://current-quote", "provider_timestamp": tick[0].isoformat(),
                  "execution_verified": False, "locked_limit_up": row.get("locked", False)}
        if row.get("missing_quote_time"):
            result.pop("provider_timestamp")
        if slot == "0920":
            result.update(amount=0, volume=0)
        return result

    def minutes(code, cutoff, cache_dir):
        row = scenarios[code]
        if slot == "0920" or row["minute"] == "missing":
            return {"metrics": {}, "source": "synthetic://missing-minute", "asOf": None, "gaps": ["合成场景：完整分钟证据缺失"]}
        age = 360 if row["minute"] == "stale" else -300 if row["minute"] == "future" else 60
        return {"metrics": {"speed_1m_pct": .1, "speed_3m_pct": .35, "speed_5m_pct": .7,
                            "local_high_5m": row["price"] - .02, "local_low_5m": row["price"] - .09,
                            "amount_3m": 5000000, "relative_amount_3m_20d": None},
                "source": "synthetic://minute-sequence", "asOf": (cutoff - timedelta(seconds=age)).isoformat(),
                "gaps": ["合成场景：历史20日相同时段基准缺失"]}

    yao = SimpleNamespace(db=repository, _fetch_snapshot=snapshot,
                          _snapshot_meta=lambda frame, **kwargs: dict(frame.attrs),
                          history_fetcher=history, data_dir=profile)
    service = LocalOpportunityService(yao, quote_fetcher=quote, minute_fetcher=minutes,
                                     clock=lambda: tick[0],
                                     sleeper=lambda seconds: tick.__setitem__(0, tick[0] + timedelta(seconds=seconds)))
    run = service.run(slot, official=True)
    run.update(shadowOnly=True, synthetic=True, dataNotice="全部行情为合成验收样本，不是实际推荐", isolation="in-memory repository; no production DB")
    return run, events, len(repository.saved)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output" / "shadow-evidence-acceptance"))
    args = parser.parse_args()
    profile = Path(args.output_dir).resolve()
    if not profile.is_relative_to((PROJECT_ROOT / "output").resolve()):
        raise ValueError("Shadow output must stay inside daily-engine/output")
    profile.mkdir(parents=True, exist_ok=True)
    prohibited_calls = []

    def blocked(kind):
        def deny(*args, **kwargs):
            prohibited_calls.append(kind)
            raise AssertionError(f"Shadow acceptance forbids {kind}")
        return deny

    with ExitStack() as guards:
        guards.enter_context(patch.object(requests.sessions.Session, "request", side_effect=blocked("network")))
        guards.enter_context(patch.object(socket.socket, "connect", side_effect=blocked("network socket")))
        guards.enter_context(patch.object(sqlite3, "connect", side_effect=blocked("sqlite database")))
        guards.enter_context(patch.object(base.HighClient, "ask", side_effect=blocked("AI")))
        intraday, intraday_events, saves = fixture_run(profile)
        premarket, premarket_events, pre_saves = fixture_run(profile, "0920")
        bad_identity, _, _ = fixture_run(profile, "0920", mismatched_premarket=True)

    selected = {row["code"] for row in intraday.get("candidates", [])}
    records = intraday.get("candidates", []) + intraday.get("windvanes", []) + intraday.get("evidenceInsufficient", [])
    checks = {
        "real_local_entry_completed": intraday.get("status") == "completed_observations",
        "new_high_and_underwater_repair_selected": selected == {"600001", "600002"},
        "heat_stale_missing_time_future_not_selected": not selected.intersection({"600003", "600004", "600005", "600007"}),
        "locked_stock_only_windvane": [row["code"] for row in intraday.get("windvanes", [])] == ["600006"],
        "no_execution_claim": all(row.get("status") != "triggered" and row.get("received_at") is None and not row.get("probabilities") for row in records),
        "no_AI_or_external_calls": intraday.get("llmUsed") is False and not prohibited_calls,
        "unknown_sector_stays_missing": all(next(e for e in row["indicatorEvidence"] if e["key"] == "sector_relative_5m_pct")["value"] is None for row in records),
        "premarket_without_turnover_is_conditional": bool(premarket.get("candidates")) and all(row["status"] == "premarket" for row in premarket["candidates"]),
        "mismatched_symbol_never_supplies_reference_price": not bad_identity.get("candidates") or all(
            row.get("referencePrice") is None or (row.get("quote") or {}).get("code") == row["code"]
            for row in bad_identity["candidates"]),
    }
    summary = {"shadowOnly": True, "synthetic": True, "entry": "LocalOpportunityService.run",
               "productionDatabaseUsed": False, "externalCalls": prohibited_calls,
               "memorySaves": saves + pre_saves, "checks": checks,
               "passed": all(checks.values()), "events": {"intraday": intraday_events, "premarket": premarket_events}}
    for name, data in [("intraday.json", intraday), ("premarket.json", premarket),
                       ("invalid-premarket-identity.json", bad_identity), ("acceptance.json", summary)]:
        (profile / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(profile), "passed": summary["passed"], "checks": checks}, ensure_ascii=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
