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
_DAILY_OBSERVATION_MODES = {"daily_observation", "mixed_intraday_and_daily_observation"}


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
                if isinstance(row, dict) and row.get("code") and (key != "candidates" or not (
                    row.get("observationOnly") is True or row.get("status") == "daily_observation"))}
    trade, watch, continuation = (codes(key) for key in
                                  ("candidates", "nextDayWatchlist", "nextDayContinuationWatchlist"))
    return {
        "displayRunId": run.get("run_id") or run.get("runId"),
        "tradeCandidateCount": len(trade),
        "dailyCandidateCount": len(trade) if run.get("primarySelectionMode") == "daily_conditional"
            else len(codes("dailyCandidates")) if run.get("primarySelectionMode") in _DAILY_OBSERVATION_MODES else 0,
        "dailyObservationCount": len(codes("dailyCandidates")) if run.get("primarySelectionMode") in _DAILY_OBSERVATION_MODES else 0,
        "nextDayCandidateCount": len(watch),
        "continuationCandidateCount": len(continuation),
        "premarketCandidateCount": len(codes("premarketCandidates")),
        # Preserve legacy counters as recorded; under the primary-admission
        # contract, research observations never inflate recommendation counts.
        "savedRecommendationCount": len(trade) if (run.get('recommendationPolicy')
            or run.get("primarySelectionMode") in _DAILY_OBSERVATION_MODES | {"daily_conditional"}) else len(trade | watch),
    }


def _display_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep full scans on disk, but omit unused duplicate research from the UI."""
    run = payload.get("adaptive")
    if not isinstance(run, dict):
        return payload
    shown = {key: value for key, value in run.items() if key not in {"controls", "windvanes", "morningResearch", "dailyObservationResearch"}}
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


def _daily_assessment_valid(run: dict[str, Any], *, historical: bool = False) -> bool:
    """Validate at publication; opening an old saved result never refreshes it."""
    from src.services.yao_scout.daily_primary import TASK_ID, VERSION
    from src.services.yao_scout.daily_opportunities import is_market_open
    from src.services.yao_scout.premarket import previous_session

    assessment = run.get("dailyAssessment")
    if not isinstance(assessment, dict):
        return False
    # Frozen daily scans retain their own evidence and version after an upgrade.
    # Only history reads accept that known contract; new publications use VERSION.
    versions = {VERSION, "king-daily-primary-v2", "king-daily-primary-v3", "king-daily-primary-v4"} if historical else {VERSION}
    statuses = {"completed", "no_daily_candidates"}
    if run.get("primarySelectionMode") in _DAILY_OBSERVATION_MODES:
        statuses.add("completed_observations")

    def period(stamp):
        clock = stamp.time().replace(tzinfo=None)
        return ("morning" if time(9, 30) <= clock <= time(11, 30) else
                "afternoon" if time(13) <= clock < time(14, 57) else None)

    try:
        decision = datetime.fromisoformat(assessment.get("asOf") or "")
        if decision.tzinfo is None:
            return False
        decision = decision.astimezone(_SHANGHAI_TZ)
        generated = _generated_at(run).astimezone(_SHANGHAI_TZ)
        daily, quotes, count = (assessment.get(key) for key in
                                ("dailyUsableCount", "quoteUsableCount", "count"))
        return bool((run.get("primaryObjective") == TASK_ID or run.get("primarySelectionMode") in _DAILY_OBSERVATION_MODES)
            and assessment.get("version") in versions and assessment.get("taskId") == TASK_ID
            and assessment.get("status") in statuses
            and assessment.get("minuteDataRequired") is False
            and all(type(value) is int for value in (daily, quotes, count))
            and daily >= quotes > 0 and 0 <= count <= quotes
            and decision.date() == generated.date()
            and 0 <= (generated-decision).total_seconds() <= 300
            and period(decision) and period(decision) == period(generated)
            and is_market_open("cn", decision.date())
            and assessment.get("baselineAsOf") == previous_session(decision).isoformat())
    except (TypeError, ValueError, OverflowError):
        return False


def displayable_run(run: Any, *, historical: bool = False) -> bool:
    """A source outage is not an empty selection. Old snapshots do not expire here."""
    if not isinstance(run, dict) or run.get("status") not in _VALID_STATUSES:
        return False
    if run.get("scanSlot") in {"review", "weekly", "0925"}:
        return False
    if not isinstance(run.get("candidates"), list):
        return False
    quality = run.get("dataQuality") or run.get("data_quality") or {}
    if run.get('premarketAssessment'):
        # A validated previous-session plan can be saved before trades/minutes
        # exist. Merely labelling a broken intraday scan "premarket" cannot
        # bypass its normal source/coverage checks.
        from src.services.yao_scout.premarket import VERSION, opening_phase, previous_session
        assessment = run['premarketAssessment']
        try:
            decision = datetime.fromisoformat(assessment.get('decisionAt') or '')
            generated = _generated_at(run).astimezone(_SHANGHAI_TZ)
            expires = datetime.fromisoformat(assessment.get('expiresAt') or '')
            return bool(assessment.get('version') == VERSION and decision.tzinfo and expires.tzinfo
                and opening_phase(decision) and decision <= generated < expires
                and expires.astimezone(_SHANGHAI_TZ).time() == time(9, 35)
                and expires.astimezone(_SHANGHAI_TZ).date() == generated.date()
                and assessment.get('baselineSession') == previous_session(decision).isoformat()
                and assessment.get('dailyUsableCount', 0) > 0
                and assessment.get('snapshotUsage') == 'stock_directory_and_research_priority_only'
                and not run['candidates'])
        except (TypeError, ValueError, OverflowError):
            return False
    if quality.get("stale") or quality.get("snapshot_count") == 0:
        return False
    coverage = quality.get("quote_coverage") or {}
    if coverage.get("requested", 0) > 0 and not coverage.get("fresh", 0):
        return False
    if run.get("primarySelectionMode") == "daily_conditional":
        # Minute availability cannot veto this independent daily task. The
        # branch label alone is insufficient: its own evidence must be usable.
        return _daily_assessment_valid(run, historical=historical)
    if run.get("primarySelectionMode") in _DAILY_OBSERVATION_MODES:
        # A completed daily observation is a valid new snapshot, including a
        # deliberately empty formal list. Never retain yesterday's formal picks
        # merely because today's minute/market/sector/fund admission failed.
        if _daily_assessment_valid(run, historical=historical):
            return True
        if not run["candidates"]:
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
                    and displayable_run(value.get("adaptive"), historical=True)):
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
                         if row.get("mode") in _SCAN_MODES and displayable_run(row.get("result"), historical=True)]
                newest = max(valid, key=lambda row: _generated_at(row["result"]), default=None)
                with _display_lock:
                    saved = self._read_saved()
                if newest and (not saved or _generated_at(newest["result"]) > _generated_at(saved["adaptive"])):
                    row = self.db.get_yao_run(newest["run_id"])
                    result = (row or {}).get("result")
                    if displayable_run(result, historical=True):
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
        if not displayable_run(run, historical=source in {"history", "migration"}):
            message = run.get("message") or {
                "skipped_non_trading_day": "当前不是交易日，本轮未刷新，已保留上次结果",
            }.get(run.get("status")) or "行情或证据不可用，本轮未刷新，已保留上次结果"
            coverage = (run.get("dataQuality") or run.get("data_quality") or {}).get("quote_coverage") or {}
            minutes = (run.get("dataQuality") or run.get("data_quality") or {}).get("minute_coverage") or {}
            quote_missing = coverage.get("requested", 0) > 0 and not coverage.get("fresh", 0)
            minute_missing = minutes.get("requested", 0) > 0 and not minutes.get("usable", 0)
            if run.get('premarketAssessment'):
                message = '昨夜已完成日线不足或开盘预案窗口已结束，本轮未生成有效预案；已保留上次结果'
            elif run.get('primarySelectionMode') in _DAILY_OBSERVATION_MODES | {'daily_conditional'}:
                message = '本轮日线、当日报价或本时段日线评估尚未核验完成；已保留上次结果'
            elif quote_missing or minute_missing:
                message = ("本轮全部实时报价缺失或过期，无法核验推荐；已保留上次结果" if quote_missing
                           else "本轮全部有效分钟价格结构缺失，无法核验推荐；已保留上次结果")
                try:
                    stamp = datetime.fromisoformat(run.get("generatedAt") or run.get("as_of") or "")
                    if stamp.tzinfo is not None and stamp.astimezone(_SHANGHAI_TZ).time() < time(9, 30):
                        message = "盘前预案所需的昨夜数据尚未核验完成；已保留上次结果"
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
