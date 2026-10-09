"""Durable, explicitly refreshed local-picks display, separate from scan history."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any

from src.services.stock_king_service import (
    STOCK_KING_METHODOLOGY_VERSION,
    STOCK_KING_SCHEMA_VERSION,
)

_display_lock = threading.RLock()
_VALID_STATUSES = {
    "completed_observations", "no_candidates_with_coverage_limits",
    "completed", "no_qualified_signal",
}


def has_candidates(run: dict[str, Any]) -> bool:
    return bool(run.get("candidates") or run.get("nextDayWatchlist") or run.get("nextDayContinuationWatchlist")
                or any((run.get("profileCandidates") or {}).values()))


def display_counts(run: dict[str, Any]) -> dict[str, Any]:
    """Count frozen selections, keeping execution and research meanings separate."""
    def codes(key):
        return {str(row.get("code")).split(".")[0].strip() for row in run.get(key) or []
                if isinstance(row, dict) and row.get("code")}
    trade, watch, continuation = (codes(key) for key in
                                  ("candidates", "nextDayWatchlist", "nextDayContinuationWatchlist"))
    return {
        "displayRunId": run.get("run_id") or run.get("runId"),
        "tradeCandidateCount": len(trade),
        "nextDayCandidateCount": len(watch),
        "continuationCandidateCount": len(continuation),
        # Continuations are an auxiliary queue, not a first-board recommendation.
        "savedRecommendationCount": len(trade | watch),
    }


def displayable_run(run: Any) -> bool:
    """A source outage is not an empty selection. Old snapshots do not expire here."""
    if not isinstance(run, dict) or run.get("status") not in _VALID_STATUSES:
        return False
    if run.get("scanSlot") in {"review", "weekly", "0925"}:
        return False
    if not isinstance(run.get("candidates"), list):
        return False
    quality = run.get("dataQuality") or run.get("data_quality") or {}
    if quality.get("stale") or quality.get("snapshot_count") == 0:
        return False
    coverage = quality.get("quote_coverage") or {}
    if coverage.get("requested", 0) > 0 and not coverage.get("fresh", 0):
        return False
    minutes = quality.get("minute_coverage") or {}
    if (minutes.get("requested", 0) > 0 and not minutes.get("usable", 0)
            and not run.get("nextDayWatchlist") and not run.get("nextDayContinuationWatchlist")):
        return False
    return True


class LocalPicksDisplayStore:
    def __init__(self, db_manager: Any):
        # The desktop supplies its persistent per-user YAO_SCOUT_DATA_DIR.
        # A stand-alone server still gets a stable location independent of cwd.
        root = Path(os.getenv("YAO_SCOUT_DATA_DIR") or os.getenv("SCREENING_DATA_DIR")
                    or Path.home() / ".stock-king" / "data" / "picks").expanduser().resolve()
        self.path = root / "local-picks-display-v1.json"
        self.db = db_manager

    @property
    def storage_key(self) -> str:
        return str(self.path)

    def read(self) -> dict[str, Any]:
        with _display_lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if (isinstance(value, dict) and value.get("displaySnapshotVersion") == 1
                        and value.get("schemaVersion") == STOCK_KING_SCHEMA_VERSION
                        and displayable_run(value.get("adaptive"))):
                    # Response-only compatibility: opening a saved page never
                    # rewrites its immutable selection or upgrades its rules.
                    return {**value, **display_counts(value["adaptive"])}
            except (OSError, ValueError, TypeError):
                pass
            if self.db is None:
                return {}
            # Initial migration only. Once saved, scheduled runs cannot change
            # this display, and merely opening the page never invokes a scan.
            rows = self.db.list_yao_runs(limit=100, include_result=True)
            valid = [row["result"] for row in rows
                     if str(row.get("mode") or "").startswith("king_")
                     and displayable_run(row.get("result"))]
            chosen = next((run for run in valid if has_candidates(run)), valid[0] if valid else None)
            return self.save(chosen, source="migration") if chosen is not None else {}

    def save(self, run: dict[str, Any], *, source: str = "manual") -> dict[str, Any]:
        if not displayable_run(run):
            message = run.get("message") or {
                "skipped_non_trading_day": "当前不是交易日，本轮未刷新，已保留上次结果",
            }.get(run.get("status")) or "行情或证据不可用，本轮未刷新，已保留上次结果"
            coverage = (run.get("dataQuality") or run.get("data_quality") or {}).get("quote_coverage") or {}
            minutes = (run.get("dataQuality") or run.get("data_quality") or {}).get("minute_coverage") or {}
            quote_missing = coverage.get("requested", 0) > 0 and not coverage.get("fresh", 0)
            minute_missing = minutes.get("requested", 0) > 0 and not minutes.get("usable", 0)
            if quote_missing or minute_missing:
                message = ("本轮全部实时报价缺失或过期，无法核验推荐；已保留上次结果" if quote_missing
                           else "本轮全部有效分钟价格结构缺失，无法核验推荐；已保留上次结果")
                try:
                    stamp = datetime.fromisoformat(run.get("generatedAt") or run.get("as_of") or "")
                    if stamp.time() < time(9, 30):
                        message = "09:30 开盘前缺少当日分钟信号，当前本地规则无法形成盘中推荐；已保留上次结果"
                except (TypeError, ValueError):
                    pass
            raise ValueError(message)
        generated_at = run.get("generatedAt") or run.get("as_of") or run.get("cutoff_at")
        payload = {
            "schemaVersion": STOCK_KING_SCHEMA_VERSION,
            "methodologyVersion": run.get("modelVersion") or STOCK_KING_METHODOLOGY_VERSION,
            "displaySnapshotVersion": 1,
            "displaySource": source,
            "generatedAt": generated_at,
            "asOfDate": str(generated_at or "")[:10],
            "scanSlot": run.get("scanSlot", "live"),
            "candidateCount": len(run.get("candidates") or []),
            **display_counts(run),
            "adaptive": run,
            "tiers": {}, "kechuang": [], "nonKeChuang": [],
            "cacheHit": False,
        }
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with _display_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".local-picks-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return payload
