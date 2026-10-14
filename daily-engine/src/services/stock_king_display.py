"""Durable local-picks display reconciled with completed scan history."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.services.stock_king_service import (
    STOCK_KING_METHODOLOGY_VERSION,
    STOCK_KING_SCHEMA_VERSION,
)

_display_lock = threading.RLock()
logger = logging.getLogger(__name__)
_SCAN_MODES = tuple(f"king_{slot}" for slot in ("live", "0920", "0922", "0940", "0955", "1030", "1455"))
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_VALID_STATUSES = {
    "completed_observations", "no_candidates_with_coverage_limits",
    "completed", "no_qualified_signal",
}


def _generated_at(run: dict[str, Any]) -> datetime:
    """Compare completion times across UTC, offset and legacy local timestamps."""
    for key in ("generatedAt", "as_of", "cutoff_at"):
        try:
            stamp = datetime.fromisoformat(run.get(key) or "")
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=_SHANGHAI_TZ)
            return stamp.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


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


def _display_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep full scans on disk, but omit unused duplicate research from the UI."""
    run = payload.get("adaptive")
    if not isinstance(run, dict):
        return payload
    shown = {key: value for key, value in run.items() if key not in {"controls", "windvanes"}}
    if isinstance(run.get("precisionWatchlist"), list):
        shown["precisionWatchlist"] = []
        for pick in run["precisionWatchlist"]:
            if not isinstance(pick, dict):
                continue
            item = {key: pick[key] for key in ("code", "name", "symbol", "stock_code", "symbol_code", "stock_name", "ts_code") if key in pick}
            profiles = (pick.get("precisionDecision") or {}).get("profiles") or {}
            item["precisionDecision"] = {"profiles": {
                profile: {key: value[key] for key in ("entryEligible", "reasons") if key in value}
                for profile, value in profiles.items() if isinstance(value, dict)
            }}
            shown["precisionWatchlist"].append(item)
    return {**payload, "adaptive": shown}


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

    def _read_saved(self) -> dict[str, Any]:
        """Read under _display_lock; compatibility summaries never rewrite a run."""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(value, dict) and value.get("displaySnapshotVersion") == 1
                    and value.get("schemaVersion") == STOCK_KING_SCHEMA_VERSION
                    and displayable_run(value.get("adaptive"))):
                return {**value, **display_counts(value["adaptive"])}
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def read(self) -> dict[str, Any]:
        # Reading history never starts a live scan. Filter at the database so
        # unrelated/review runs cannot consume its bounded history window.
        chosen = None
        sync_error = False
        if self.db is not None:
            try:
                rows = self.db.list_yao_display_runs(limit=100, modes=_SCAN_MODES,
                                                    statuses=tuple(sorted(_VALID_STATUSES)))
                valid = [row for row in rows
                         if row.get("mode") in _SCAN_MODES and displayable_run(row.get("result"))]
                newest = max(valid, key=lambda row: _generated_at(row["result"]), default=None)
                with _display_lock:
                    saved = self._read_saved()
                if newest and (not saved or _generated_at(newest["result"]) > _generated_at(saved["adaptive"])):
                    row = self.db.get_yao_run(newest["run_id"])
                    result = (row or {}).get("result")
                    if displayable_run(result):
                        chosen = result
                    else:
                        sync_error = True
            except Exception:
                sync_error = True
                logger.warning("Unable to reconcile local picks with scan history; retaining saved display", exc_info=True)

        with _display_lock:
            # A manual refresh may have finished while the database was read.
            # Re-read inside the write lock so it cannot be replaced by an older
            # history result. Equal timestamps retain the existing snapshot.
            saved = self._read_saved()
            if sync_error:
                return _display_response({**saved, "displaySyncError": "最新推荐暂时读取失败，当前保留上次已保存结果"})
            if chosen is None or (saved and _generated_at(chosen) <= _generated_at(saved["adaptive"])):
                return _display_response(saved)
            return self.save(chosen, source="history" if saved else "migration")

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
            saved = self._read_saved()
            if saved and _generated_at(saved["adaptive"]) > _generated_at(run):
                return _display_response(saved)
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
        return _display_response(payload)
