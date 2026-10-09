"""Independent, point-in-time ledger and release gate for King V1.1 signals.

This is deliberately separate from ``yao_outcomes`` and the three daily-bar
tiers.  A saved scan is immutable; subsequent reviews append *simulated* order
and path observations.  Neither a quote nor a minute OHLC bar is a broker fill.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
from time import monotonic
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo
import zlib

import numpy as np

from src.quant.executable_backtest import AccountConfig, _fees, _lot
from src.services.yao_scout.minute_history import bar_session, normalize_bars
from src.services.yao_scout.research_policy import DEFAULT_PARAMETERS


TZ = ZoneInfo("Asia/Shanghai")
LEDGER_VERSION = "king-v1.1-signal-ledger"
_VALIDATION = DEFAULT_PARAMETERS["validation"]
MODEL_VERSION = _VALIDATION["executionModel"]
OFFICIAL_SLOTS = frozenset({"0920", "0940", "0955", "1030", "1455"})
ENTRY_SLOTS = OFFICIAL_SLOTS - {"0920"}
HORIZONS = (1, 3, 5)
MIN_MATURE_DAYS = _VALIDATION["minimumMatureDays"]
MIN_VALID_SAMPLES = _VALIDATION["minimumValidSamples"]
MIN_HOLDOUT_DAYS = _VALIDATION["minimumHoldoutDays"]
MIN_HOLDOUT_TRADES = _VALIDATION["minimumHoldoutTrades"]
PURGE_DAYS = _VALIDATION["purgeDays"]
SHADOW_DAYS = _VALIDATION["shadowDays"]
MAX_HISTORY = 500
_ARTIFACT_PROBES: dict[str, tuple[float, bool]] = {}


def _at(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("King V1.1 timestamps must include an offset")
    return parsed.astimezone(TZ)


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(output) or (positive and output <= 0):
        return None
    return output


def _json(value: Any) -> str:
    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): clean(part) for key, part in item.items()}
        if isinstance(item, (tuple, list)):
            return [clean(part) for part in item]
        if isinstance(item, (float, np.floating)) and not math.isfinite(float(item)):
            return None
        if isinstance(item, np.integer):
            return int(item)
        if isinstance(item, np.floating):
            return float(item)
        return item
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _code(value: Any) -> str | None:
    raw = str(value or "").split(".")[0].strip()
    return raw if re.fullmatch(r"\d{6}", raw) else None


def _feature_values(candidate: Mapping[str, Any]) -> dict[str, float]:
    """Freeze numeric, signal-time values only; never learn from review fields."""
    result: dict[str, float] = {}
    for prefix, key in (("", "v11Inputs"), ("factor_", "factorScores")):
        source = candidate.get(key)
        if not isinstance(source, Mapping):
            continue
        for name, value in source.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(name)):
                continue
            parsed = _number(value)
            if parsed is not None:
                result[prefix + str(name)] = parsed
    for name in ("earlyScore", "mainRiseScore", "finalScore", "distributionRisk", "dataConfidence"):
        parsed = _number(candidate.get(name))
        if parsed is not None:
            result[name] = parsed
    return result


def _snapshot(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Persist the research decision without provider credentials or future data."""
    keys = (
        "code", "name", "decision_at", "signalPrice", "referencePrice", "sourceTime", "quote",
        "entryPlan", "data_quality", "status", "evidenceEligible", "precisionDecision",
        "risks", "tradability", "factorScores", "v11Inputs", "earlyScore", "mainRiseScore",
        "finalScore", "distributionRisk", "dataConfidence", "baselineHistoryDays",
        "scoreVersion", "minuteArchivePath", "firstTradablePrice", "selectionReasons",
        "invalidationConditions", "indicatorEvidence",
        "algorithmContractId", "learningRankVersion", "learningRankScore",
        "rank", "rankingTieBreakOrder",
        "researchFactors", "dataEligibility", "priceHistoricalCoverageDays",
        "factorCoverage", "missingFactors", "scoreStage", "selectedScoreBranch",
        "nextDaySignal", "dailyLastClose", "dailyPreviousSession", "nextDaySelected", "nextDayContinuationSelected", "recommendationKinds",
    )
    return {key: candidate[key] for key in keys if key in candidate}


def _source_time(candidate: Mapping[str, Any]) -> str | None:
    quote = candidate.get("quote") if isinstance(candidate.get("quote"), Mapping) else {}
    value = quote.get("provider_timestamp") or quote.get("source_time") or candidate.get("sourceTime")
    try:
        return _at(value).isoformat() if value else None
    except (TypeError, ValueError):
        return None


def _scan_records(result: Mapping[str, Any]):
    """Freeze each stock once while preserving separate execution/research choices."""
    selected = {_code(item.get("code")) for item in result.get("candidates", []) if isinstance(item, Mapping)}
    selected.discard(None)
    records: dict[str, dict[str, Any]] = {}
    for collection in ("candidates", "controls", "precisionResearch", "windvanes", "evidenceInsufficient"):
        for item in result.get(collection, []) or []:
            if isinstance(item, Mapping) and (code := _code(item.get("code"))):
                if code not in records or collection == "candidates":
                    records[code] = dict(item)
    # Research-only rows do not gain execution/training eligibility by existing
    # in a watchlist. Existing candidates/controls keep their original contract.
    execution_sources = set(records)
    watch, continuation = set(), set()
    for collection, selection in (("nextDayWatchlist", watch), ("nextDayContinuationWatchlist", continuation)):
        for item in result.get(collection, []) or []:
            if not isinstance(item, Mapping) or not (code := _code(item.get("code"))):
                continue
            selection.add(code)
            record = records.setdefault(code, dict(item))
            signal = item.get("nextDaySignal")
            if isinstance(signal, Mapping):
                # Copy only the frozen research signal; never replace a trade's
                # entry status, execution plan, features, or original version.
                record["nextDaySignal"] = {**signal, "selected": True}
    for code, record in records.items():
        signal = record.get("nextDaySignal") or {}
        if isinstance(signal, Mapping) and signal.get("selected") is True:
            (continuation if signal.get("queue") == "continuation" else watch).add(code)
        record["nextDaySelected"] = code in watch
        record["nextDayContinuationSelected"] = code in continuation
        record["recommendationKinds"] = (["execution"] if code in selected else []) + (
            ["next_day_watch"] if code in watch else []) + (["next_day_continuation"] if code in continuation else [])
    return selected, records, watch, continuation, execution_sources


def _current_entry_eligible(candidate: Mapping[str, Any]) -> bool:
    from .local_algorithm import algorithm_contract, is_entry_eligible
    return bool(isinstance(candidate, Mapping)
                and candidate.get("algorithmContractId") == algorithm_contract()["contractId"]
                and is_entry_eligible(candidate))


def _signal_preflight(candidate: Mapping[str, Any], published_at: datetime) -> bool:
    """Admit only signal-time usable examples; later no-fills stay in the sample."""
    if not _current_entry_eligible(candidate):
        return False
    code = _code(candidate.get("code"))
    if code is None or not _feature_values(candidate):
        return False
    source = _source_time(candidate)
    if source is None or not 0 <= (published_at - _at(source)).total_seconds() <= 30:
        return False
    if _number(candidate.get("signalPrice") or candidate.get("referencePrice"), positive=True) is None:
        return False
    quote = candidate.get("quote") if isinstance(candidate.get("quote"), Mapping) else {}
    if quote.get("no_price_limit") is True or quote.get("corporate_action") is True:
        return False
    previous = _number(quote.get("pre_close") or quote.get("previous_close"), positive=True)
    source_up = _number(quote.get("limit_up"), positive=True)
    rate = SignalLearningService._rate(code, str(candidate.get("name") or code))
    return bool(previous and source_up and rate is not None and
                abs(source_up - SignalLearningService._price_limit(previous, rate, "up")) <= 0.015)


def _default_calendar(start: date, end: date) -> list[date]:
    """Fail closed when an exchange calendar is unavailable."""
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        return [stamp.date() for stamp in calendar.sessions_in_range(start, end)]
    except Exception:
        return []


def _needs_observation(outcome: Mapping[str, Any]) -> bool:
    """An execution rejection does not settle its independent price review."""
    if outcome["status"] not in {"no_fill", "unverified"}:
        return False
    try:
        details = json.loads(outcome["details_json"])
    except (TypeError, ValueError):
        details = {}
    if details.get("observationStatus") == "pending_data":
        return True
    return (details.get("observedReturn") is None
            and details.get("observationStatus") != "unavailable"
            and outcome["reason"] not in {
                "signal_price_missing", "price_limit_or_corporate_action_unverifiable",
                "possible_corporate_action_or_discontinuous_bars"})


class SignalLearningService:
    """Store scans, mature conservative minute-bar labels, and gate challengers."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        db_path: str | Path | None = None,
        bar_fetcher: Callable[[str, datetime], Iterable[Mapping[str, Any]]] | None = None,
        calendar_provider: Callable[[date, date], Iterable[date]] | None = None,
        clock: Callable[[], datetime] | None = None,
        execution_config: AccountConfig | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or self.data_dir / "signal-learning-v1.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.bar_fetcher = bar_fetcher or self._archive_bars
        self.calendar_provider = calendar_provider or _default_calendar
        self.clock = clock or (lambda: datetime.now(TZ))
        self.execution_config = execution_config or AccountConfig()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_runs(
                    run_id TEXT PRIMARY KEY, slot TEXT NOT NULL, as_of TEXT NOT NULL,
                    trading_day TEXT NOT NULL, official INTEGER NOT NULL,
                    version TEXT NOT NULL, status TEXT NOT NULL, scan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_events(
                    signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES signal_runs(run_id),
                    code TEXT NOT NULL, name TEXT NOT NULL, selected INTEGER NOT NULL,
                    decision_at TEXT NOT NULL, source_time TEXT, signal_price REAL,
                    training_eligible INTEGER NOT NULL, review_eligible INTEGER NOT NULL DEFAULT 0,
                    features_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, review_status TEXT NOT NULL,
                    UNIQUE(run_id, code)
                );
                CREATE INDEX IF NOT EXISTS signal_events_code_date ON signal_events(code, decision_at);
                CREATE TABLE IF NOT EXISTS signal_outcomes(
                    signal_id TEXT NOT NULL REFERENCES signal_events(signal_id), horizon INTEGER NOT NULL,
                    status TEXT NOT NULL, fill_status TEXT NOT NULL, evidence_grade TEXT NOT NULL,
                    entry_at TEXT, entry_price REAL, exit_at TEXT, exit_price REAL,
                    simulated_net_return REAL, mfe REAL, mae REAL, reason TEXT,
                    reviewed_at TEXT NOT NULL, details_json TEXT NOT NULL,
                    PRIMARY KEY(signal_id, horizon)
                );
                CREATE TABLE IF NOT EXISTS learning_registry(
                    registry_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_versions(
                    version TEXT PRIMARY KEY, stage TEXT NOT NULL, artifact_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_predictions(
                    signal_id TEXT NOT NULL REFERENCES signal_events(signal_id),
                    model_version TEXT NOT NULL, rank_score REAL NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(signal_id,model_version)
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(signal_events)")}
            if "review_eligible" not in columns:
                db.execute("ALTER TABLE signal_events ADD COLUMN review_eligible INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE signal_events SET review_eligible=training_eligible")

    def persist_scan(self, result: Mapping[str, Any], slot: str, as_of: datetime | str, official: bool) -> dict[str, Any]:
        """First official run for a slot/day wins; manual runs are audit only."""
        timestamp = _at(as_of)
        slot = str(slot).strip().lower()
        if slot not in OFFICIAL_SLOTS and slot != "live":
            raise ValueError(f"Unsupported V1.1 scan slot: {slot}")
        official = bool(official and slot in OFFICIAL_SLOTS)
        run_usable = str(result.get("status") or "") not in {
            "missed_slot", "unavailable", "not_due", "expired", "skipped_non_trading_day",
            "retired_slot", "audit_only"}
        reviewable_run = bool(run_usable and (slot in ENTRY_SLOTS or (
            slot == "live" and time(9, 30) <= timestamp.time() <= time(14, 57))))
        published_at = _at(self.clock())
        day = timestamp.date().isoformat()
        if official:
            run_id = f"{LEDGER_VERSION}:{day}:{slot}:official"
        else:
            source_id = str(result.get("run_id") or result.get("runId") or timestamp.isoformat())
            run_id = f"{LEDGER_VERSION}:{day}:{slot}:manual:{sha256(source_id.encode()).hexdigest()[:16]}"
        selected, records, next_day, continuation, execution_sources = _scan_records(result)
        scan_meta = {
            "sourceRunId": result.get("run_id") or result.get("runId"),
            "modelVersion": result.get("modelVersion") or result.get("model_version"),
            "status": result.get("status"),
            "dataQuality": result.get("dataQuality") or result.get("data_quality") or {},
            "selectedCodes": sorted(selected),
            "nextDaySelectedCodes": sorted(next_day),
            "nextDayContinuationSelectedCodes": sorted(continuation),
            "researchCodes": sorted(records),
            "result": result,
        }
        scan_version = str(result.get("scoreVersion") or result.get("modelVersion")
                           or result.get("model_version") or LEDGER_VERSION)
        from .local_algorithm import algorithm_contract
        contract = algorithm_contract()
        # Also freeze the contract of an empty (cash-only) official scan.
        scan_meta["algorithmContractId"] = (contract["contractId"]
                                             if scan_version == contract["scoreVersion"] else None)
        registry = self._registry()
        forward_models = {}
        if official and run_usable and slot in ENTRY_SLOTS:
            for version in (registry.get("championVersion"), registry.get("shadowVersion"),
                            registry.get("rollbackVersion")):
                if version:
                    artifact = self._model_artifact(version)
                    if artifact:
                        forward_models[version] = artifact
        existing = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM signal_runs WHERE run_id=?", (run_id,)).fetchone() is not None
            if not existing:
                db.execute(
                    "INSERT INTO signal_runs VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, slot, timestamp.isoformat(), day, int(official), scan_version,
                     str(result.get("status") or "unknown"), _json(scan_meta), published_at.isoformat()),
                )
                for code, item in records.items():
                    supplied_decision = item.get("decision_at")
                    try:
                        decision = _at(supplied_decision or timestamp)
                    except (TypeError, ValueError):
                        decision = timestamp
                    if (not supplied_decision or decision.date() != timestamp.date()
                            or decision > timestamp + timedelta(minutes=5)):
                        # Keep an auditable, unusable row without backdating it.
                        decision = timestamp
                        eligible = False
                    else:
                        eligible = bool(code in execution_sources and official and run_usable and slot in ENTRY_SLOTS
                                        and _signal_preflight(item, published_at))
                    signal_id = sha256(f"{run_id}:{code}".encode()).hexdigest()[:32]
                    snap = _snapshot(item)
                    db.execute(
                        "INSERT INTO signal_events(signal_id,run_id,code,name,selected,decision_at,source_time,"
                        "signal_price,training_eligible,review_eligible,features_json,snapshot_json,review_status) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (signal_id, run_id, code, str(item.get("name") or code), int(code in selected),
                         decision.isoformat(), _source_time(item),
                         _number(item.get("signalPrice") or item.get("referencePrice"), positive=True),
                         int(eligible), int(reviewable_run and code in execution_sources), _json(_feature_values(item)), _json(snap),
                         "pending" if reviewable_run and code in execution_sources else "observation_only"),
                    )
                    if eligible:
                        for version, artifact in forward_models.items():
                            try:
                                score, _ = self._predict_artifact(
                                    artifact, [{"features": _feature_values(item)}])
                                db.execute("INSERT INTO forward_predictions VALUES(?,?,?,?)",
                                           (signal_id, version, float(score[0]), _at(self.clock()).isoformat()))
                            except (ImportError, ValueError):
                                continue
            else:
                saved = db.execute("SELECT scan_json FROM signal_runs WHERE run_id=?", (run_id,)).fetchone()
                canonical = json.loads(saved[0]).get("result") if saved else None
                if isinstance(canonical, Mapping):
                    result = canonical
                    selected, records, next_day, continuation, execution_sources = _scan_records(result)
        output = dict(result)
        output["learningLedger"] = {
            "version": LEDGER_VERSION, "runId": run_id, "official": official,
            "idempotentReplay": existing, "selectedCount": len(selected),
            "nextDaySelectedCount": len(next_day), "continuationSelectedCount": len(continuation),
            "savedRecommendationCount": len(selected | next_day),
            "researchCount": len(records), "trainingEligible": bool(official and run_usable and slot in ENTRY_SLOTS),
            "reviewEligible": reviewable_run,
        }
        return output

    def _archive_bars(self, code: str, cutoff: datetime) -> list[dict[str, Any]]:
        """Read existing minute archive without constructing its mutating helper."""
        path = self.data_dir / "minute_history" / "minute-bars-v2.sqlite3"
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            for (payload,) in db.execute(
                "SELECT payload FROM sessions WHERE code=? AND day<=? ORDER BY day",
                (code, cutoff.date().isoformat()),
            ):
                rows.extend(json.loads(zlib.decompress(payload)))
        return list(normalize_bars(rows, cutoff).values())

    def review_codes(self, as_of: datetime | str) -> list[str]:
        return list(self.review_requirements(as_of))

    def review_requirements(self, as_of: datetime | str) -> dict[str, list[str]]:
        """Prioritize minute capture for unfinished execution and T+1 reviews.

        Include today's signals even before their labels are due: their entry
        path and closing minute must be archived while providers still serve
        them. This is a read-only queue, not a claim that a review completed.
        """
        timestamp = _at(as_of)
        from .next_day_watch import SUPPORTED_VERSIONS

        with self._connect() as db:
            rows = db.execute(
                "SELECT e.*,r.created_at published_at FROM signal_events e "
                "JOIN signal_runs r ON r.run_id=e.run_id WHERE r.trading_day<=? "
                "ORDER BY CASE WHEN e.selected=1 OR json_extract(e.snapshot_json,'$.nextDaySignal.selected')=1 "
                "THEN 0 ELSE 1 END,r.trading_day,e.decision_at,e.code",
                (timestamp.date().isoformat(),),
            ).fetchall()
            execution = defaultdict(dict)
            for row in db.execute("SELECT signal_id,horizon,status,reason,details_json FROM signal_outcomes"):
                execution[row["signal_id"]][row["horizon"]] = row
            next_day = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='next_day_outcomes'").fetchone():
                for row in db.execute("SELECT signal_id,result_json FROM next_day_outcomes"):
                    try:
                        next_day[row["signal_id"]] = json.loads(row["result_json"]).get("status")
                    except (TypeError, ValueError, AttributeError):
                        continue

        codes = {}
        session_cache = {}
        def require(code, start, *, horizon):
            if start not in session_cache:
                session_cache[start] = self._sessions(self.calendar_provider, start, timestamp.date())
            days = session_cache[start]
            codes.setdefault(code, set()).update(day.isoformat() for day in days[:horizon+1])
        terminal = {"mature", "no_fill", "unverified"}
        for row in rows:
            if max(_at(row["decision_at"]), _at(row["published_at"])) > timestamp:
                continue
            signal_id = row["signal_id"]
            outcomes = execution[signal_id]
            if row["review_eligible"] and any(
                    horizon not in outcomes or outcomes[horizon]["status"] not in terminal
                    or _needs_observation(outcomes[horizon]) for horizon in HORIZONS):
                # Capture existing sessions for pending horizons, including T
                # before T+1 matures. An open exit may need sessions after T+5.
                open_exit = any(item['status'] == 'exit_blocked' or 'post_target' in str(item['reason'])
                                for item in outcomes.values())
                require(row['code'], _at(row['decision_at']).date(),
                        horizon=10000 if open_exit else max(HORIZONS))
                continue
            try:
                signal = json.loads(row["snapshot_json"]).get("nextDaySignal") or {}
                signal_day = date.fromisoformat(signal.get("signalSession", ""))
            except (TypeError, ValueError, AttributeError):
                continue
            if (signal.get("version") in SUPPORTED_VERSIONS and signal_day <= timestamp.date()
                    and next_day.get(signal_id) != "mature_price_pattern"):
                require(row['code'], signal_day, horizon=1)
        return {code: sorted(days) for code, days in codes.items()}

    def history(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(filters or {})
        limit = max(1, min(int(filters.get("limit") or 30), MAX_HISTORY))
        where, args = [], []
        if filters.get("date"):
            where.append("r.trading_day=?")
            args.append(str(filters["date"]))
        if filters.get("symbol"):
            code = _code(filters["symbol"])
            if code is None:
                return {"items": [], "count": 0}
            where.append("(EXISTS(SELECT 1 FROM signal_events x WHERE x.run_id=r.run_id AND x.code=?) "
                         "OR EXISTS(SELECT 1 FROM json_each(r.scan_json,'$.result.nextDayWatchlist') x "
                         "WHERE substr(json_extract(x.value,'$.code'),1,6)=?) "
                         "OR EXISTS(SELECT 1 FROM json_each(r.scan_json,'$.result.nextDayContinuationWatchlist') x "
                         "WHERE substr(json_extract(x.value,'$.code'),1,6)=?))")
            args.extend([code, code, code])
        if filters.get("version"):
            where.append("r.version=?")
            args.append(str(filters["version"]))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as db:
            runs = db.execute(
                "SELECT r.* FROM signal_runs r" + clause + " ORDER BY r.as_of DESC LIMIT ?", (*args, limit)
            ).fetchall()
            items = []
            for run in runs:
                scan = json.loads(run["scan_json"])
                frozen_result = scan.get("result") or {}
                selected, frozen_records, next_day, continuation, _ = _scan_records(frozen_result)
                events = db.execute(
                    "SELECT * FROM signal_events WHERE run_id=? ORDER BY selected DESC, decision_at, code", (run["run_id"],)
                ).fetchall()
                signals = []
                for event in events:
                    if filters.get("symbol") and event["code"] != _code(filters["symbol"]):
                        continue
                    snapshot = json.loads(event["snapshot_json"])
                    frozen = frozen_records.get(event["code"]) or {}
                    # Older ledgers may have stored the stock as a control but
                    # omitted the watch selection. Read its original scan only;
                    # never re-run today's algorithm or rewrite historical rows.
                    if "nextDaySignal" not in snapshot and isinstance(frozen.get("nextDaySignal"), Mapping):
                        snapshot["nextDaySignal"] = frozen["nextDaySignal"]
                    signal = snapshot.get("nextDaySignal") or {}
                    recorded_research = isinstance(signal, Mapping) and signal.get("selected") is True
                    watch_selected = event["code"] in next_day or (recorded_research and signal.get("queue") != "continuation")
                    continuation_selected = event["code"] in continuation or (recorded_research and signal.get("queue") == "continuation")
                    if (watch_selected or continuation_selected) and isinstance(signal, Mapping) and signal:
                        snapshot["nextDaySignal"] = {**signal, "selected": True}
                    signals.append({
                        "signalId": event["signal_id"], "code": event["code"], "name": event["name"],
                        "selected": bool(event["selected"]), "decisionAt": event["decision_at"],
                        "nextDaySelected": watch_selected, "nextDayContinuationSelected": continuation_selected,
                        "recommendationKinds": (["execution"] if event["selected"] else []) + (
                            ["next_day_watch"] if watch_selected else []) + (
                            ["next_day_continuation"] if continuation_selected else []),
                        "sourceTime": event["source_time"], "signalPrice": event["signal_price"],
                        "firstTradablePrice": None, "reviewStatus": event["review_status"],
                        "trainingEligible": bool(event["training_eligible"]),
                        "reviewEligible": bool(event["review_eligible"]),
                        "features": json.loads(event["features_json"]),
                        "reason": snapshot.get("selectionReasons") or [],
                        "snapshot": snapshot,
                    })
                recorded_codes = {event["code"] for event in events}
                for code in sorted((next_day | continuation) - recorded_codes):
                    if filters.get("symbol") and code != _code(filters["symbol"]):
                        continue
                    frozen = frozen_records[code]
                    # Display omitted research rows from the immutable run.
                    # No database migration and no retroactive trading sample.
                    signals.append({
                        "signalId": sha256(f"{run['run_id']}:{code}".encode()).hexdigest()[:32],
                        "code": code, "name": str(frozen.get("name") or code), "selected": False,
                        "nextDaySelected": code in next_day, "nextDayContinuationSelected": code in continuation,
                        "recommendationKinds": [kind for kind in frozen["recommendationKinds"] if kind != "execution"],
                        "decisionAt": frozen.get("decision_at") or run["as_of"],
                        "sourceTime": _source_time(frozen),
                        "signalPrice": _number(frozen.get("signalPrice") or frozen.get("referencePrice"), positive=True),
                        "firstTradablePrice": None, "reviewStatus": "observation_only",
                        "trainingEligible": False, "reviewEligible": False,
                        "features": _feature_values(frozen), "reason": frozen.get("selectionReasons") or [],
                        "snapshot": _snapshot(frozen), "ledgerRecordOrigin": "saved_scan_projection",
                    })
                items.append({
                    "runId": run["run_id"], "slot": run["slot"], "asOf": run["as_of"],
                    "official": bool(run["official"]), "modelVersion": run["version"],
                    "status": run["status"], "signals": signals,
                    "scan": scan,
                })
        return {"items": items, "count": len(items)}

    def reviews(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(filters or {})
        limit = max(1, min(int(filters.get("limit") or 1000), 5000))
        where, args = ["e.review_eligible=1"], []
        if filters.get("date"):
            where.append("r.trading_day=?")
            args.append(str(filters["date"]))
        if filters.get("symbol"):
            code = _code(filters["symbol"])
            if code is None:
                return {"items": [], "count": 0}
            where.append("e.code=?")
            args.append(code)
        if filters.get("version"):
            where.append("r.version=?")
            args.append(str(filters["version"]))
        if filters.get("horizon") in HORIZONS:
            where.append("h.horizon=?")
            args.append(int(filters["horizon"]))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as db:
            rows = db.execute(
                "WITH h(horizon) AS (SELECT 1 UNION ALL SELECT 3 UNION ALL SELECT 5) "
                "SELECT e.signal_id,h.horizon,COALESCE(o.status,'pending') status,"
                "COALESCE(o.fill_status,'pending') fill_status,"
                "COALESCE(o.evidence_grade,'none') evidence_grade,"
                "o.entry_at,o.entry_price,o.exit_at,o.exit_price,o.simulated_net_return,"
                "o.mfe,o.mae,o.reason,o.reviewed_at,o.details_json,e.code,e.selected,"
                "r.trading_day,r.version FROM signal_events e "
                "JOIN signal_runs r ON r.run_id=e.run_id CROSS JOIN h "
                "LEFT JOIN signal_outcomes o ON o.signal_id=e.signal_id AND o.horizon=h.horizon"
                + clause + " ORDER BY e.selected DESC, r.trading_day DESC, h.horizon, e.code LIMIT ?", (*args, limit),
            ).fetchall()
        items = [self._outcome_dict(row) for row in rows]
        from .next_day_watch import read_next_day_reviews
        return {"items": items, "count": len(items), "nextDayReview": read_next_day_reviews(self, filters)}

    @staticmethod
    def _outcome_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        details = json.loads(row["details_json"]) if row["details_json"] else {}
        return {
            "signalId": row["signal_id"], "code": row["code"], "signalDate": row["trading_day"],
            "modelVersion": row["version"] if "version" in row.keys() else LEDGER_VERSION,
            "selected": bool(row["selected"]), "horizon": row["horizon"], "status": row["status"],
            "fillStatus": row["fill_status"], "evidenceGrade": row["evidence_grade"],
            "entryAt": row["entry_at"], "entryPrice": row["entry_price"],
            "exitAt": row["exit_at"], "exitPrice": row["exit_price"],
            "simulatedNetReturn": row["simulated_net_return"], "mfe": row["mfe"],
            "mae": row["mae"], "observedReturn": details.get("observedReturn"),
            "observedMfe": details.get("observedMfe"), "observedMae": details.get("observedMae"),
            "observationStatus": details.get("observationStatus"),
            "observationReason": details.get("observationReason"),
            "reason": row["reason"], "reviewedAt": row["reviewed_at"],
            "positionOpen": details.get("positionOpen", False),
            "markAt": details.get("markAt"), "markPrice": details.get("markPrice"),
            "unrealizedPriceReturn": details.get("unrealizedPriceReturn"),
            "deferredExitSessions": details.get("deferredExitSessions", 0),
        }

    @staticmethod
    def _price_limit(pre_close: float, rate: float, side: str) -> float:
        multiplier = 1 + rate if side == "up" else 1 - rate
        return float((Decimal(str(pre_close)) * Decimal(str(multiplier))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _rate(code: str, name: str) -> float | None:
        if "ST" in name.upper() or code.startswith(("4", "8", "9")):
            return None
        if code.startswith(("30", "68")):
            return 0.20
        if code.startswith(("00", "60")):
            return 0.10
        return None

    @staticmethod
    def _sessions(provider: Callable[[date, date], Iterable[date]], start: date, end: date) -> list[date]:
        try:
            result = sorted({day if isinstance(day, date) else date.fromisoformat(str(day))
                             for day in provider(start, end)})
        except Exception:
            return []
        return [day for day in result if start <= day <= end]

    @staticmethod
    def _bar_rows(rows: Iterable[Mapping[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
        return [value for _, value in sorted(normalize_bars(rows, cutoff).items())]

    def _simulate_event(self, event: Mapping[str, Any], horizon: int, as_of: datetime,
                        cached_bars: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
        decision = max(_at(event["decision_at"]), _at(event["published_at"]))
        sessions = self._sessions(self.calendar_provider, decision.date(), as_of.date())
        if decision.date() not in sessions:
            return None
        index = sessions.index(decision.date())
        if len(sessions) <= index + horizon:
            return None
        target_day = sessions[index + horizon]
        if as_of < datetime.combine(target_day, time(15, 1), TZ):
            return None
        base = {
            "status": "unverified", "fill_status": "unverified", "evidence_grade": "insufficient",
            "entry_at": None, "entry_price": None, "exit_at": None, "exit_price": None,
            "simulated_net_return": None, "mfe": None, "mae": None, "reason": None,
            "details_json": _json({"targetDay": target_day.isoformat(), "simulationVersion": MODEL_VERSION}),
        }
        snapshot = json.loads(event["snapshot_json"])
        quote = snapshot.get("quote") if isinstance(snapshot.get("quote"), Mapping) else {}
        source = event["source_time"]
        try:
            source_at = _at(source) if source else None
        except (TypeError, ValueError):
            source_at = None
        execution_failure = None
        if source_at is None or not 0 <= (decision - source_at).total_seconds() <= 30:
            execution_failure = {**base, "reason": "signal_quote_missing_or_stale"}
        if event["signal_price"] is None or _number(event["signal_price"], positive=True) is None:
            return {**base, "reason": "signal_price_missing"}
        quote_price = _number(quote.get("price"), positive=True)
        observation_issue = (
            "signal_reference_source_time_missing" if source_at is None else
            "signal_reference_source_time_in_future" if source_at > decision else
            "signal_reference_price_unverified" if quote_price is None or not math.isclose(
                quote_price, event["signal_price"], abs_tol=1e-6) else None)
        if observation_issue:
            return {**(execution_failure or base),
                    "reason": (execution_failure or {}).get("reason") or observation_issue,
                    "details_json": _json({"targetDay": target_day.isoformat(),
                        "simulationVersion": MODEL_VERSION, "observationStatus": "unavailable",
                        "observationReason": observation_issue})}
        tradability = snapshot.get("tradability") if isinstance(snapshot.get("tradability"), Mapping) else {}
        if not execution_failure and (tradability.get("status") == "unavailable" or quote.get("suspended") is True):
            execution_failure = {**base, "status": "no_fill", "fill_status": "unavailable_at_signal",
                                 "reason": "suspended_or_unavailable_at_signal"}
        if quote.get("corporate_action") is True:
            return {**(execution_failure or base),
                    "reason": (execution_failure or {}).get("reason") or "price_limit_or_corporate_action_unverifiable",
                    "details_json": _json({"targetDay": target_day.isoformat(),
                        "simulationVersion": MODEL_VERSION, "observationStatus": "unavailable",
                        "observationReason": "price_limit_or_corporate_action_unverifiable"})}
        if not execution_failure and quote.get("no_price_limit") is True:
            execution_failure = {**base, "reason": "price_limit_or_corporate_action_unverifiable"}
        rate = self._rate(event["code"], event["name"])
        pre_close = _number(quote.get("pre_close") or quote.get("previous_close"), positive=True)
        source_up = _number(quote.get("limit_up"), positive=True)
        if not execution_failure and (rate is None or pre_close is None or source_up is None):
            execution_failure = {**base, "reason": "point_in_time_price_limit_missing"}
        up_limit = source_up
        if not execution_failure and abs(up_limit - self._price_limit(pre_close, rate, "up")) > 0.015:
            execution_failure = {**base, "reason": "source_price_limit_conflicts_with_board_rule"}

        def incomplete_observation(reason: str) -> dict[str, Any]:
            # Preserve the immutable execution failure while allowing later
            # minute backfills to complete this separate price observation.
            return {**(execution_failure or {**base, "status": "pending_data", "reason": reason}),
                    "details_json": _json({"targetDay": target_day.isoformat(),
                        "simulationVersion": MODEL_VERSION, "observationStatus": "pending_data",
                        "observationReason": reason})}

        if event["code"] not in cached_bars:
            try:
                cached_bars[event["code"]] = self._bar_rows(self.bar_fetcher(event["code"], as_of), as_of)
            except Exception:
                cached_bars[event["code"]] = []
        bars = cached_bars[event["code"]]
        same_day = [bar for bar in bars if _at(bar["end"]).date() == decision.date()]
        if not same_day:
            return incomplete_observation("entry_minute_bars_missing")
        target = datetime.combine(target_day, time(15, 0), TZ)
        target_bars = [bar for bar in bars if _at(bar["end"]) == target]
        first_end = decision.replace(second=0, microsecond=0) + timedelta(
            minutes=2 if decision.second or decision.microsecond else 1)
        observation_path = [bar for bar in bars if first_end <= _at(bar["end"]) <= target]
        if not target_bars or not observation_path:
            return incomplete_observation("target_close_minute_missing")
        mark_days = sessions[index:index + horizon + 1]
        observed_ends = {_at(bar["end"]) for bar in observation_path}
        for day in mark_days:
            minute = datetime.combine(day, time(9, 31), TZ)
            close_time = datetime.combine(day, time(15, 0), TZ)
            while minute <= close_time:
                if bar_session(minute) and (day != decision.date() or minute >= first_end):
                    if minute not in observed_ends:
                        return incomplete_observation("incomplete_minute_path")
                minute += timedelta(minutes=1)
        marks = {day.isoformat(): next((bar["close"] for bar in observation_path
                                       if _at(bar["end"]) == datetime.combine(day, time(15, 0), TZ)), None)
                 for day in mark_days}
        if any(mark is None for mark in marks.values()):
            return incomplete_observation("daily_close_marks_missing")
        discontinuous = any(
            _at(previous["end"]).date() != _at(current["end"]).date()
            and (current["open"] < previous["close"] * (1 - rate - 0.03)
                 or current["open"] > previous["close"] * (1 + rate + 0.03))
            for previous, current in zip(observation_path, observation_path[1:])) if rate is not None else False
        if discontinuous or any(bar.get("corporate_action") for bar in observation_path):
            return {**(execution_failure or base),
                    "reason": (execution_failure or {}).get("reason") or "possible_corporate_action_or_discontinuous_bars",
                    "details_json": _json({"targetDay": target_day.isoformat(),
                        "simulationVersion": MODEL_VERSION, "observationStatus": "unavailable",
                        "observationReason": "possible_corporate_action_or_discontinuous_bars"})}
        signal_price = event["signal_price"]
        target_volumes = [bar.get("volume_shares") for bar in observation_path
                          if _at(bar["end"]).date() == target_day]
        inactive_observation = (any(bar.get("suspended") is True for bar in observation_path)
                                or bool(target_volumes) and all(
                                    _number(volume) is not None and _number(volume) <= 0
                                    for volume in target_volumes))
        details = {"targetDay": target_day.isoformat(), "simulationVersion": MODEL_VERSION,
                   "exitPolicy": "15:00_close_auction_minute_proxy",
                   "dailyCloseMarks": marks,
                   "observationStatus": "unavailable" if inactive_observation else "complete",
                   "observationMeaning": "冻结参考价至目标日的未复权价格变化；非成交收益、非训练标签",
                   "observationReferenceAt": source_at.isoformat(),
                   "observationReferencePrice": signal_price,
                   "pathResolution": "completed_one_minute_bars"}
        if inactive_observation:
            details["observationReason"] = "suspended_path_or_inactive_target_session"
        else:
            details.update(
                observedReturn=target_bars[0]["close"] / signal_price - 1,
                observedMfe=max(0.0, max(bar["high"] / signal_price - 1 for bar in observation_path)),
                observedMae=max(0.0, 1 - min(bar["low"] / signal_price for bar in observation_path)))
        base["details_json"] = _json(details)
        if execution_failure:
            return {**execution_failure, "details_json": base["details_json"]}
        window = [bar for bar in same_day if _at(bar["end"]) == first_end]
        if not window:
            return {**base, "status": "pending_data", "reason": "first_complete_minute_missing"}
        bar = window[0]
        if bar.get("no_price_limit") or bar.get("corporate_action"):
            return {**base, "reason": "price_limit_or_corporate_action_unverifiable"}
        proposed = round(bar["open"] * (1 + self.execution_config.slippage_bps / 10000), 2)
        min_lot, lot_step = _lot(event["code"])
        budget = self.execution_config.initial_cash * self.execution_config.position_weight
        quantity = min_lot + max(0, int((budget / proposed - min_lot) // lot_step)) * lot_step
        if (bar.get("suspended") is True or not _number(bar.get("volume_shares"), positive=True)
                or bar["open"] >= up_limit - 0.005
                or proposed > min(bar["high"], up_limit) + 1e-8
                or quantity <= 0 or quantity * proposed > budget
                or bar["volume_shares"] < quantity * 10):
            return {**base, "status": "no_fill", "fill_status": "first_minute_not_tradable",
                    "reason": "limit_up_queue_liquidity_or_slippage"}
        entry_bar, entry_price = bar, proposed
        entry_at = _at(entry_bar["end"])
        path = [bar for bar in bars if entry_at <= _at(bar["end"]) <= target]
        if not path or not target_bars:
            return {**base, "status": "pending_data", "fill_status": "simulated_fill",
                    "entry_at": entry_at.isoformat(), "entry_price": entry_price,
                    "reason": "exit_minute_bars_missing"}
        # An unfilled sell is still an open position. Check every subsequent
        # completed minute in order; never skip a data gap to find a good exit.
        details["exitPolicy"] = _VALIDATION["exitPolicy"]
        details["entryMinuteVolume"] = entry_bar["volume_shares"]
        details["blockedExitMinutes"] = 0
        by_end = {_at(item["end"]): item for item in bars}
        last_close = next((item["close"] for item in reversed(path)
                           if _at(item["end"]).date() < target_day), None)

        def open_position(status: str, reason: str) -> dict[str, Any]:
            last = path[-1]
            return {**base, "status": status, "fill_status": "simulated_entry_exit_pending",
                    "evidence_grade": "simulated_minute_proxy", "entry_at": entry_at.isoformat(),
                    "entry_price": entry_price, "reason": reason,
                    "mfe": max(0.0, max(item["high"] / entry_price - 1 for item in path)),
                    "mae": max(0.0, 1 - min(item["low"] / entry_price for item in path)),
                    "details_json": _json({**details, "positionOpen": True,
                        "markAt": last["end"], "markPrice": last["close"],
                        "unrealizedPriceReturn": last["close"] / entry_price - 1})}

        exit_bar, exit_price = None, None
        for day in sessions[index + horizon:]:
            minute = target if day == target_day else datetime.combine(day, time(9, 31), TZ)
            close_time = min(datetime.combine(day, time(15, 0), TZ), as_of.replace(second=0, microsecond=0))
            while minute <= close_time:
                if not bar_session(minute):
                    minute += timedelta(minutes=1)
                    continue
                current = by_end.get(minute)
                if current is None:
                    return open_position("pending_data", "post_target_minute_path_missing")
                if current.get("corporate_action") or current.get("no_price_limit"):
                    return open_position("pending_data", "price_limit_or_corporate_action_unverifiable")
                if minute > target:
                    previous = path[-1]
                    if (_at(previous["end"]).date() != day
                            and (current["open"] < last_close * (1 - rate - 0.03)
                                 or current["open"] > last_close * (1 + rate + 0.03))):
                        return open_position("pending_data", "possible_corporate_action_or_discontinuous_bars")
                    path.append(current)
                if minute.time() == time(15, 0):
                    marks[day.isoformat()] = current["close"]
                proposed_exit = round(current["close"] * (1 - self.execution_config.slippage_bps / 10000), 2)
                down_limit = self._price_limit(last_close, rate, "down") if last_close else None
                if (current.get("suspended") is True or not _number(current.get("volume_shares"), positive=True)
                        or current["volume_shares"] < quantity * 10
                        or down_limit is None or current["close"] <= down_limit + 0.005
                        or proposed_exit < current["low"] - 1e-8):
                    details["blockedExitMinutes"] += 1
                else:
                    exit_bar, exit_price = current, proposed_exit
                    # The account sells before the close on a deferred exit day;
                    # no end-of-day position mark is required on that day.
                    marks[day.isoformat()] = current["close"]
                    break
                minute += timedelta(minutes=1)
            if exit_bar is not None:
                break
            if day.isoformat() in marks:
                last_close = marks[day.isoformat()]
        if exit_bar is None:
            return open_position("exit_blocked", "suspended_limit_down_or_exit_slippage")
        exit_day = _at(exit_bar["end"]).date()
        buy_notional, sell_notional = quantity * entry_price, quantity * exit_price
        buy_cost = _fees(buy_notional, "buy", entry_at.date(), self.execution_config)["total"]
        sell_cost = _fees(sell_notional, "sell", exit_day, self.execution_config)["total"]
        net = (sell_notional - sell_cost) / (buy_notional + buy_cost) - 1
        return {**base, "status": "mature", "fill_status": "simulated_fill",
                "evidence_grade": "simulated_minute_proxy", "entry_at": entry_at.isoformat(),
                "entry_price": entry_price, "exit_at": _at(exit_bar["end"]).isoformat(),
                "exit_price": exit_price, "simulated_net_return": net,
                "mfe": max(0.0, max(bar["high"] / entry_price - 1 for bar in path)),
                "mae": max(0.0, 1 - min(bar["low"] / entry_price for bar in path)), "reason": None,
                "details_json": _json({**details, "positionOpen": False,
                                       "labelEndAt": exit_bar["end"],
                                       "exitMinuteVolume": exit_bar["volume_shares"],
                                       "deferredExitSessions": sessions.index(exit_day) - (index + horizon)})}

    def review_due(self, as_of: datetime | str) -> dict[str, Any]:
        """Mature each official label only after that horizon's complete trading day."""
        timestamp = _at(as_of)
        cache: dict[str, list[dict[str, Any]]] = {}
        changed: list[dict[str, Any]] = []
        with self._connect() as db:
            events = db.execute(
                "SELECT e.*, r.trading_day, r.created_at published_at "
                "FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
                "WHERE e.review_eligible=1 AND r.trading_day<=? ORDER BY r.trading_day,e.decision_at",
                (timestamp.date().isoformat(),),
            ).fetchall()
            for event in events:
                for horizon in HORIZONS:
                    old = db.execute("SELECT status,reason,details_json FROM signal_outcomes WHERE signal_id=? AND horizon=?",
                                     (event["signal_id"], horizon)).fetchone()
                    if old and old["status"] in {"mature", "no_fill", "unverified"} and not _needs_observation(old):
                        continue
                    result = self._simulate_event(event, horizon, timestamp, cache)
                    if result is None:
                        continue
                    db.execute(
                        "INSERT INTO signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(signal_id,horizon) DO UPDATE SET "
                        "status=excluded.status,fill_status=excluded.fill_status,"
                        "evidence_grade=excluded.evidence_grade,entry_at=excluded.entry_at,"
                        "entry_price=excluded.entry_price,exit_at=excluded.exit_at,exit_price=excluded.exit_price,"
                        "simulated_net_return=excluded.simulated_net_return,mfe=excluded.mfe,mae=excluded.mae,"
                        "reason=excluded.reason,reviewed_at=excluded.reviewed_at,details_json=excluded.details_json",
                        (event["signal_id"], horizon, result["status"], result["fill_status"],
                         result["evidence_grade"], result["entry_at"], result["entry_price"],
                         result["exit_at"], result["exit_price"], result["simulated_net_return"],
                         result["mfe"], result["mae"], result["reason"], timestamp.isoformat(),
                         result["details_json"]),
                    )
                    changed.append({"signalId": event["signal_id"], "code": event["code"],
                                    "signalDate": event["trading_day"], "horizon": horizon,
                                    "modelVersion": LEDGER_VERSION,
                                    "status": result["status"], "fillStatus": result["fill_status"],
                                    "evidenceGrade": result["evidence_grade"],
                                    "entryAt": result["entry_at"], "entryPrice": result["entry_price"],
                                    "exitAt": result["exit_at"], "exitPrice": result["exit_price"],
                                    "simulatedNetReturn": result["simulated_net_return"],
                                    "mfe": result["mfe"], "mae": result["mae"],
                                    "observedReturn": json.loads(result["details_json"]).get("observedReturn"),
                                    "observedMfe": json.loads(result["details_json"]).get("observedMfe"),
                                    "observedMae": json.loads(result["details_json"]).get("observedMae"),
                                    "reason": result["reason"], "reviewedAt": timestamp.isoformat()})
                states = [row[0] for row in db.execute(
                    "SELECT status FROM signal_outcomes WHERE signal_id=? ORDER BY horizon", (event["signal_id"],))]
                review_status = ("mature" if len(states) == 3 and all(state == "mature" for state in states)
                                 else "incomplete" if any(state in {"no_fill", "unverified"} for state in states)
                                 else "pending")
                db.execute("UPDATE signal_events SET review_status=? WHERE signal_id=?",
                           (review_status, event["signal_id"]))
        from .next_day_watch import review_next_day_signals
        next_day = review_next_day_signals(self, timestamp, cache)
        return {"items": changed, "updated": len(changed), "asOf": timestamp.isoformat(),
                "nextDayReview": next_day}

    def _dataset(self, as_of: datetime, *, dedupe: bool = True) -> list[dict[str, Any]]:
        """Point-in-time signals; no-fill remains a zero-return cash decision."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT e.*,r.trading_day,r.slot,r.version scan_version,o1.status h1,o3.status h3,o5.status h5,"
                "o5.simulated_net_return net,o5.mfe mfe,o5.mae mae,o5.exit_at exit_at,"
                "o5.entry_at entry_at,o5.entry_price entry_price,o5.exit_price exit_price,"
                "o5.details_json details_json,o1.exit_at h1_exit_at,o3.exit_at h3_exit_at,"
                "o1.reviewed_at h1_reviewed_at,o3.reviewed_at h3_reviewed_at,o5.reviewed_at h5_reviewed_at "
                "FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
                "JOIN signal_outcomes o1 ON o1.signal_id=e.signal_id AND o1.horizon=1 "
                "JOIN signal_outcomes o3 ON o3.signal_id=e.signal_id AND o3.horizon=3 "
                "JOIN signal_outcomes o5 ON o5.signal_id=e.signal_id AND o5.horizon=5 "
                "WHERE e.training_eligible=1 AND r.trading_day<=? "
                "ORDER BY r.trading_day,e.code,e.selected DESC,e.decision_at",
                (as_of.date().isoformat(),),
            ).fetchall()
        result, seen = [], set()
        from .local_algorithm import algorithm_contract
        contract = algorithm_contract()
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (TypeError, ValueError):
                continue
            if row["scan_version"] != contract["scoreVersion"] or not _current_entry_eligible(snapshot):
                continue
            key = (row["trading_day"], row["code"])
            if dedupe and key in seen:
                continue
            states = (row["h1"], row["h3"], row["h5"])
            if any(state not in {"mature", "no_fill"} for state in states):
                continue
            features = json.loads(row["features_json"])
            if not features or row["h5"] == "mature" and row["net"] is None:
                continue
            details = json.loads(row["details_json"])
            if row["h5"] == "mature" and not details.get("dailyCloseMarks"):
                continue
            target_day = details.get("targetDay")
            label_ends = [value for value in (row["h1_exit_at"], row["h3_exit_at"], row["exit_at"]) if value]
            if target_day:
                label_ends.append(datetime.combine(date.fromisoformat(target_day), time(15, 1), TZ).isoformat())
            if not label_ends:
                continue
            label_end = max(_at(value) for value in label_ends)
            if label_end > as_of or any(_at(row[key]) > as_of for key in (
                    "h1_reviewed_at", "h3_reviewed_at", "h5_reviewed_at")):
                continue
            seen.add(key)
            result.append({"day": row["trading_day"], "code": row["code"], "runId": row["run_id"],
                           "signalId": row["signal_id"], "features": features,
                           "net": float(row["net"] or 0.0), "filled": row["h5"] == "mature",
                           "mfe": float(row["mfe"] or 0.0),
                           "mae": float(row["mae"] or 0.0), "exitAt": row["exit_at"],
                           "entryAt": row["entry_at"], "entryPrice": row["entry_price"],
                           "exitPrice": row["exit_price"], "marks": details.get("dailyCloseMarks") or {},
                           "entryMinuteVolume": details.get("entryMinuteVolume"),
                           "exitMinuteVolume": details.get("exitMinuteVolume"),
                           "labelEndAt": label_end.isoformat(),
                           "deferredExitSessions": details.get("deferredExitSessions", 0),
                           "selected": bool(row["selected"]), "decisionAt": row["decision_at"],
                           "slot": row["slot"], "algorithmContractId": contract["contractId"],
                           "entryEligibleAtSignal": True,
                           "selectionRank": _number(snapshot.get("rank"), positive=True),
                           "recordedRankScore": _number(snapshot.get("learningRankScore")),
                           "rankingTieBreakOrder": _number(snapshot.get("rankingTieBreakOrder"))})
        return result

    def _registry(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM learning_registry WHERE registry_key='v11'").fetchone()
        try:
            payload = json.loads(row[0]) if row else {}
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _save_registry(self, registry: Mapping[str, Any], at: datetime) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO learning_registry VALUES('v11',?,?) "
                "ON CONFLICT(registry_key) DO UPDATE SET payload_json=excluded.payload_json,"
                "updated_at=excluded.updated_at", (_json(registry), at.isoformat()))

    def learning_state(self, as_of: datetime | str | None = None) -> dict[str, Any]:
        as_of = _at(as_of or self.clock())
        samples = self._dataset(as_of)
        registry = self._registry()
        from .local_algorithm import algorithm_contract
        contract = algorithm_contract()
        effective_version, effective_source, fallback = "rules_v1.2", "rules", None
        for key, source in (("championVersion", "champion"), ("rollbackVersion", "rollback")):
            version = registry.get(key)
            if not version:
                continue
            artifact = self._model_artifact(version, active_only=True)
            if artifact is None:
                fallback = "排序模型缺失、损坏或与当前算法契约不一致，使用可用后备排序"
                continue
            try:
                # The median-feature row only verifies the frozen artifact can
                # execute. It never becomes a signal, sample or reported return.
                fingerprint = sha256(_json(artifact).encode()).hexdigest()
                probe = _ARTIFACT_PROBES.get(fingerprint)
                if probe is None or monotonic()-probe[0] >= 30:
                    try:
                        self._predict_artifact(artifact, [{"features": {}}])
                    except Exception:
                        _ARTIFACT_PROBES[fingerprint] = (monotonic(), False)
                        raise
                    if len(_ARTIFACT_PROBES) > 32:
                        _ARTIFACT_PROBES.clear()
                    _ARTIFACT_PROBES[fingerprint] = (monotonic(), True)
                elif not probe[1]:
                    raise ValueError("cached_artifact_probe_failed")
            except Exception:
                fallback = "排序模型执行校验失败，使用可用后备排序"
                continue
            effective_version, effective_source = version, source
            break
        valid_shadow = registry.get("shadowVersion") if self._model_artifact(registry.get("shadowVersion")) else None
        validation_matches = registry.get("algorithmContractId") == contract["contractId"]
        days = len({sample["day"] for sample in samples})
        stage = ("active" if effective_source != "rules" else
                 "shadow" if valid_shadow else "rules_cold_start")
        reason = registry.get("reason")
        if days < MIN_MATURE_DAYS or len(samples) < MIN_VALID_SAMPLES:
            reason = (f"待积累独立成熟交易日/有效样本：{days}/{MIN_MATURE_DAYS} 天，"
                      f"{len(samples)}/{MIN_VALID_SAMPLES} 条")
        return {
            "stage": stage, "championVersion": effective_version if effective_source != "rules" else None,
            "shadowVersion": valid_shadow,
            "algorithm": contract, "effectiveRankVersion": effective_version,
            "effectiveRankSource": effective_source, "rankingFallbackReason": fallback,
            "configuredChampionVersion": registry.get("championVersion"),
            "configuredShadowVersion": registry.get("shadowVersion"),
            "shadowDays": registry.get("shadowDays", 0) if valid_shadow and validation_matches else 0,
            "shadowRequiredDays": SHADOW_DAYS,
            "minimumHoldoutDays": MIN_HOLDOUT_DAYS, "minimumHoldoutCompletedTrades": MIN_HOLDOUT_TRADES,
            "validationRequirements": {"holdout": {"independentSignalDays": MIN_HOLDOUT_DAYS,
                "completedAccountTrades": MIN_HOLDOUT_TRADES},
                "forwardShadow": {"independentSignalDays": SHADOW_DAYS},
                "purge": "actual_label_end_before_next_split_and_minimum_five_session_gap"},
            "matureDays": days, "validSamples": len(samples),
            "minimumMatureDays": MIN_MATURE_DAYS, "minimumValidSamples": MIN_VALID_SAMPLES,
            "gates": (registry.get("gates") or {}) if validation_matches else {},
            "validationContractId": contract["contractId"] if validation_matches else None,
            "historicalValidationAvailable": bool(registry.get("gates") and not validation_matches
                                                  or (registry.get("priorValidation") or {}).get("gates")),
            "lastTrainingAt": registry.get("lastTrainingAt"),
            "reason": reason, "rollbackVersion": registry.get("rollbackVersion"),
            "lastEvaluatedThrough": registry.get("lastEvaluatedThrough"),
            "cohortAudit": registry.get("cohortAudit") if validation_matches else None,
            "splitAudit": registry.get("splitAudit") if validation_matches else None,
            "validationAttempts": len(registry.get("validationHistory") or []),
        }

    @staticmethod
    def _matrix(samples: list[dict[str, Any]], features: list[str], medians: list[float]) -> np.ndarray:
        return np.asarray([[float(sample["features"].get(name, medians[index]))
                            for index, name in enumerate(features)] for sample in samples], dtype=float)

    @staticmethod
    def _account_nav(samples: list[dict[str, Any]], scores: np.ndarray | None,
                     config: AccountConfig, calendar_days: list[str] | None = None) -> dict[str, Any]:
        """A daily marked account with five simultaneous positions and actual cash use.

        A scan may attempt at most five signals. The official selected set is the
        current rule/champion comparator; model alternatives rank the same scan
        queue. A no-fill leaves its allocation in cash. A blocked T+5 sell keeps
        its position until the first evidenced exit, including intraday exits.
        """
        if scores is not None and len(scores) != len(samples):
            return {"valid": False, "reason": "rank_score_count_mismatch"}
        all_days = (calendar_days if calendar_days is not None else
                    sorted({day for item in samples for day in item["marks"]} |
                           {item["day"] for item in samples}))
        if not all_days:
            return {"valid": False, "reason": "daily_nav_marks_missing"}
        by_day: dict[str, dict[str, list[tuple[float, dict[str, Any]]]]] = defaultdict(lambda: defaultdict(list))
        for index, item in enumerate(samples):
            score = float(scores[index]) if scores is not None else float(item["features"].get("finalScore", 0))
            by_day[item["day"]][item["runId"]].append((score, item))
        cash = float(config.initial_cash)
        positions: list[dict[str, Any]] = []
        nav_values, trade_count, attempts, trade_returns = [], 0, 0, []
        previous_nav = cash
        traded_codes: set[tuple[str, str]] = set()

        def settle_due(cutoff: datetime) -> None:
            nonlocal cash, positions
            remaining = []
            for position in positions:
                if _at(position["exitAt"]) <= cutoff:
                    proceeds = position["quantity"] * position["exitPrice"]
                    fee = _fees(proceeds, "sell", _at(position["exitAt"]).date(), config)["total"]
                    cash += proceeds - fee
                    trade_returns.append((proceeds - fee) / position["buyCost"] - 1)
                else:
                    remaining.append(position)
            positions = remaining

        for day in all_days:
            groups = sorted(by_day.get(day, {}).values(),
                            key=lambda group: min(item["decisionAt"] for _, item in group))
            for group in groups:
                settle_due(min(_at(item["decisionAt"]) for _, item in group))
                slots = max(0, config.max_positions - len(positions))
                if not slots:
                    continue
                pool = ([(score, item) for score, item in group if item["selected"]]
                        if scores is None else group)
                def selection_order(pair):
                    score, item = pair
                    tie = _number(item.get("rankingTieBreakOrder"))
                    tie = tie if tie is not None else math.inf
                    if scores is None:
                        rank = _number(item.get("selectionRank"), positive=True)
                        if rank is not None:
                            return (0, rank, tie, item["code"])
                        recorded = _number(item.get("recordedRankScore"))
                        return (1, -(recorded if recorded is not None else score), tie, item["code"])
                    # Calibrated percentiles often tie. Live sorting is stable
                    # over the original rules/evidence queue, frozen at scanning.
                    return (0, -score, tie, item["code"])
                choices = sorted(pool, key=selection_order)[:slots]
                for _, item in choices:
                    attempts += 1
                    key = (day, item["code"])
                    if key in traded_codes or any(position["code"] == item["code"] for position in positions):
                        continue
                    traded_codes.add(key)
                    if not item["filled"]:
                        continue
                    if (not item["entryAt"] or not item["exitAt"] or item["entryAt"][:10] != day
                            or item["exitAt"][:10] not in item["marks"]):
                        return {"valid": False, "reason": "entry_exit_timestamp_missing"}
                    entry_price = _number(item["entryPrice"], positive=True)
                    exit_price = _number(item["exitPrice"], positive=True)
                    if entry_price is None or exit_price is None:
                        return {"valid": False, "reason": "entry_exit_price_missing"}
                    allocation = min(cash, previous_nav * config.position_weight)
                    min_lot, step = _lot(item["code"])
                    quantity = min_lot + max(0, int((allocation / entry_price - min_lot) // step)) * step
                    if quantity < min_lot:
                        continue
                    fee = _fees(quantity * entry_price, "buy", date.fromisoformat(day), config)["total"]
                    while quantity >= min_lot and quantity * entry_price + fee > allocation:
                        quantity -= step
                        fee = (_fees(quantity * entry_price, "buy", date.fromisoformat(day), config)["total"]
                               if quantity >= min_lot else 0)
                    if quantity < min_lot or quantity * entry_price + fee > cash:
                        continue
                    volume = _number(item.get("entryMinuteVolume"), positive=True)
                    if volume is None or quantity > volume * 0.1:
                        return {"valid": False, "reason": "entry_participation_evidence_missing_or_exceeded"}
                    exit_volume = _number(item.get("exitMinuteVolume"), positive=True)
                    if exit_volume is None or quantity > exit_volume * .1:
                        return {"valid": False, "reason": "exit_participation_evidence_missing_or_exceeded"}
                    cash -= quantity * entry_price + fee
                    positions.append({"code": item["code"], "quantity": quantity,
                                      "exitAt": item["exitAt"], "exitPrice": exit_price,
                                      "marks": item["marks"], "buyCost": quantity * entry_price + fee})
                    trade_count += 1
            settle_due(datetime.combine(date.fromisoformat(day), time(15, 0), TZ))
            equity = cash
            for position in positions:
                mark = _number(position["marks"].get(day), positive=True)
                if mark is None:
                    return {"valid": False, "reason": "open_position_daily_close_missing"}
                equity += position["quantity"] * mark
            nav_values.append(equity)
            previous_nav = equity
        nav = np.asarray([config.initial_cash, *nav_values], dtype=float)
        if positions:
            return {"valid": False, "reason": "open_positions_at_validation_end"}
        returns = nav[1:] / nav[:-1] - 1
        peaks = np.maximum.accumulate(nav)
        return {"valid": True, "days": all_days, "nav": nav.tolist(), "daily": returns,
                "net": float(nav[-1] / nav[0] - 1),
                "maxDrawdown": float(np.max(1 - nav / peaks)),
                "filledPicks": trade_count, "attemptedPicks": attempts,
                "completedTrades": len(trade_returns),
                "completedTradeWinRate": float(np.mean(np.asarray(trade_returns) > 0)) if trade_returns else None,
                "tradeReturnP05": float(np.quantile(trade_returns, .05)) if trade_returns else None,
                "fillRate": trade_count / attempts if attempts else 0.0}

    @staticmethod
    def _gates(samples: list[dict[str, Any]], challenger: np.ndarray,
               logistic: np.ndarray, *, seed: int, config: AccountConfig,
               complete_universe: bool, current_scores: np.ndarray | None = None,
               calendar_days: list[str] | None = None, evaluation_days: list[str] | None = None,
               minimum_days: int = MIN_HOLDOUT_DAYS,
               minimum_completed_trades: int = MIN_HOLDOUT_TRADES) -> dict[str, Any]:
        if not complete_universe:
            return {"passed": False, "reason": "incomplete_same_queue_or_forward_predictions",
                    "days": len({sample["day"] for sample in samples})}
        if calendar_days is not None and not calendar_days:
            return {"passed": False, "reason": "exchange_calendar_unavailable_or_inconsistent"}
        candidate = SignalLearningService._account_nav(samples, challenger, config, calendar_days)
        current = SignalLearningService._account_nav(samples, current_scores, config, calendar_days)
        extra = SignalLearningService._account_nav(samples, logistic, config, calendar_days)
        if not candidate["valid"] or not current["valid"] or not extra["valid"]:
            return {"passed": False, "reason": "account_nav_unverifiable",
                    "candidateReason": candidate.get("reason"), "currentReason": current.get("reason"),
                    "logisticReason": extra.get("reason")}
        returns = candidate["daily"]
        current_returns = current["daily"]
        independent_days = len(evaluation_days if evaluation_days is not None else {sample["day"] for sample in samples})
        counts = {"days": len(returns), "independentSignalDays": independent_days,
                  "minimumSignalDays": minimum_days, "minimumCompletedTrades": minimum_completed_trades,
                  "completedTrades": candidate["completedTrades"], "filledPicks": candidate["filledPicks"],
                  "attemptedPicks": candidate["attemptedPicks"]}
        if (independent_days < minimum_days or candidate["completedTrades"] < minimum_completed_trades
                or candidate["days"] != current["days"]):
            return {"passed": False, "reason": "insufficient_independent_days_or_completed_trades", **counts}
        rng = np.random.default_rng(seed)
        block = max(PURGE_DAYS, 5)
        starts = rng.integers(0, max(1, len(returns) - block + 1),
                              size=(2000, math.ceil(len(returns) / block)))
        indices = (starts[:, :, None] + np.arange(block)).reshape(2000, -1)[:, :len(returns)]
        indices = np.clip(indices, 0, len(returns) - 1)
        sampled_net = np.prod(1 + returns[indices], axis=1) - 1
        sampled_current = np.prod(1 + current_returns[indices], axis=1) - 1
        net_lower = float(np.quantile(sampled_net, 0.05))
        advantage_lower = float(np.quantile(sampled_net - sampled_current, 0.05))
        drawdown_ok = candidate["maxDrawdown"] <= current["maxDrawdown"] + 1e-12
        passed = net_lower > 0 and advantage_lower > 0 and drawdown_ok
        periods = []
        for start in range(0, len(returns), SHADOW_DAYS):
            end = min(len(returns), start + SHADOW_DAYS)
            period_nav = np.cumprod(np.r_[1.0, 1 + returns[start:end]])
            periods.append({"from": candidate["days"][start], "through": candidate["days"][end - 1],
                            "navDays": end - start, "net": float(period_nav[-1] - 1),
                            "currentPolicyNet": float(np.prod(1 + current_returns[start:end]) - 1),
                            "maxDrawdown": float(np.max(1 - period_nav / np.maximum.accumulate(period_nav)))})
        return {
            "passed": bool(passed), **counts,
            "simulatedAccountNet": candidate["net"],
            "currentPolicyAccountNet": current["net"],
            "logisticAccountNet": extra["net"],
            "net95Lower": net_lower, "advantage95Lower": advantage_lower,
            "maxDrawdown": candidate["maxDrawdown"],
            "currentPolicyMaxDrawdown": current["maxDrawdown"],
            "drawdownNotWorse": bool(drawdown_ok), "filledPicks": candidate["filledPicks"],
            "attemptedPicks": candidate["attemptedPicks"],
            "completedTradeWinRate": candidate["completedTradeWinRate"],
            "fillRate": candidate["fillRate"], "tradeReturnP05": candidate["tradeReturnP05"],
            "periodDiagnostics": periods,
            "periodDiagnosticsMeaning": "non_overlapping_nav_windows_of_the_same_frozen_holdout_not_independent_retraining",
            "method": "same_point_in_time_scan_queue;five_position_cash_account;"
                      "daily_15:00_marks;five_trading_day_moving_block_bootstrap_95pct",
        }

    @staticmethod
    def _predict_artifact(artifact: Mapping[str, Any], samples: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
        from lightgbm import Booster
        x = SignalLearningService._matrix(samples, artifact["features"], artifact["medians"])
        net = np.asarray(Booster(model_str=artifact["netRegressor"]).predict(x, num_threads=1), dtype=float)
        mae = np.maximum(0.0, np.asarray(Booster(model_str=artifact["maeRegressor"])
                                         .predict(x, num_threads=1), dtype=float))
        utility = net - float(artifact["riskCoefficient"]) * mae
        candidate = np.interp(utility, artifact["calibrationKnots"],
                              artifact["calibrationPercentiles"]).astype(float)
        z = (x - np.asarray(artifact["logisticMean"])) / np.asarray(artifact["logisticScale"])
        linear = z @ np.asarray(artifact["logisticCoef"]) + float(artifact["logisticIntercept"])
        baseline = 1 / (1 + np.exp(-np.clip(linear, -30, 30)))
        if (len(candidate) != len(samples) or not np.all(np.isfinite(candidate))
                or not np.all(np.isfinite(baseline)) or np.any(candidate < 0)
                or np.any(candidate > 100)):
            raise ValueError("model_prediction_invalid")
        return candidate, baseline

    def _model_artifact(self, version: str, *, active_only: bool = False) -> dict[str, Any] | None:
        if not version:
            return None
        with self._connect() as db:
            row = db.execute("SELECT artifact_json,stage,gates_json FROM model_versions WHERE version=?", (version,)).fetchone()
        if not row:
            return None
        try:
            artifact = json.loads(row[0])
            from .local_algorithm import algorithm_contract
            gates = json.loads(row[2])
            if (row[1] not in ({"active"} if active_only else {"active", "shadow"})
                    or not isinstance(gates, dict) or gates.get("passed") is not True
                    or not isinstance(artifact, dict) or artifact.get("version") != version
                    or artifact.get("executionModel") != MODEL_VERSION
                    or artifact.get("algorithmContractId") != algorithm_contract()["contractId"]
                    or not all(key in artifact for key in (
                        "features", "medians", "netRegressor", "maeRegressor",
                        "calibrationKnots", "calibrationPercentiles", "riskCoefficient",
                        "logisticMean", "logisticScale", "logisticCoef", "logisticIntercept"))):
                return None
            return artifact
        except (TypeError, ValueError):
            return None

    def _evaluation_days(self, as_of: datetime) -> list[str]:
        """Freeze the date cohort from recorded scans, never from surviving labels.

        A day with zero completed labels (or an explicit cash-only scan) remains
        in the cohort. The calendar determines when its five-session labels are
        due; blocked exits do not silently move the day out of validation.
        """
        from .local_algorithm import algorithm_contract
        contract = algorithm_contract()
        with self._connect() as db:
            rows = db.execute(
                "SELECT trading_day,created_at,slot,status,scan_json FROM signal_runs "
                "WHERE official=1 AND version=? AND trading_day<=? ORDER BY trading_day",
                (contract["scoreVersion"], as_of.date().isoformat())).fetchall()
        recorded = set()
        for row in rows:
            if row["slot"] not in ENTRY_SLOTS or _at(row["created_at"]) > as_of:
                continue
            if row["status"] in {"missed_slot", "unavailable", "not_due", "expired",
                                 "skipped_non_trading_day", "retired_slot", "audit_only"}:
                continue
            try:
                if json.loads(row["scan_json"]).get("algorithmContractId") == contract["contractId"]:
                    recorded.add(row["trading_day"])
            except (ValueError, TypeError):
                continue
        if not recorded:
            return []
        sessions = self._sessions(self.calendar_provider, date.fromisoformat(min(recorded)), as_of.date())
        result = []
        for index, day in enumerate(sessions):
            if (day.isoformat() in recorded and index + max(HORIZONS) < len(sessions)
                    and datetime.combine(sessions[index + max(HORIZONS)], time(15, 1), TZ) <= as_of):
                result.append(day.isoformat())
        return result

    @staticmethod
    def _purge_before(samples: list[dict[str, Any]], boundary_day: str) -> list[dict[str, Any]]:
        """Purge actual label intervals, including exits deferred beyond T+5."""
        boundary = datetime.combine(date.fromisoformat(boundary_day), time(0), TZ)
        result = []
        for sample in samples:
            try:
                if sample.get("labelEndAt") and _at(sample["labelEndAt"]) < boundary:
                    result.append(sample)
            except (TypeError, ValueError):
                continue
        return result

    def _complete_universe(self, days: set[str], samples: list[dict[str, Any]]) -> bool:
        if not days:
            return False
        placeholders = ",".join("?" for _ in days)
        with self._connect() as db:
            rows = db.execute(
                "SELECT e.signal_id,e.snapshot_json,r.version FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
                f"WHERE e.training_eligible=1 AND r.trading_day IN ({placeholders})",
                tuple(sorted(days)),
            ).fetchall()
            runs = db.execute(
                f"SELECT trading_day,scan_json,version FROM signal_runs WHERE official=1 AND trading_day IN ({placeholders})",
                tuple(sorted(days))).fetchall()
        from .local_algorithm import algorithm_contract
        contract = algorithm_contract()
        recorded_days = set()
        for run in runs:
            try:
                if (run["version"] == contract["scoreVersion"]
                        and json.loads(run["scan_json"]).get("algorithmContractId") == contract["contractId"]):
                    recorded_days.add(run["trading_day"])
            except (TypeError, ValueError):
                continue
        expected = set()
        for row in rows:
            try:
                if row[2] == algorithm_contract()["scoreVersion"] and _current_entry_eligible(json.loads(row[1])):
                    expected.add(row[0])
            except (TypeError, ValueError):
                continue
        return days <= recorded_days and expected == {sample["signalId"] for sample in samples}

    def _forward_scores(self, version: str, samples: list[dict[str, Any]]) -> np.ndarray | None:
        if not samples:
            return None
        ids = [sample["signalId"] for sample in samples]
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                "SELECT signal_id,rank_score,created_at FROM forward_predictions "
                f"WHERE model_version=? AND signal_id IN ({placeholders})", (version, *ids)
            ).fetchall()
        matched = {row["signal_id"]: row for row in rows}
        if len(matched) != len(ids):
            return None
        scores = []
        for sample in samples:
            row = matched[sample["signalId"]]
            created = _at(row["created_at"])
            decision = _at(sample["decisionAt"])
            if not decision <= created <= decision + timedelta(minutes=5):
                return None
            score = float(row["rank_score"])
            if not math.isfinite(score) or not 0 <= score <= 100:
                return None
            scores.append(score)
        return np.asarray(scores, dtype=float)

    def _gate_calendar(self, samples: list[dict[str, Any]], evaluation_days: Iterable[str] = ()) -> list[str]:
        required = set(evaluation_days)
        if not samples and not required:
            return []
        required.update(sample["day"] for sample in samples)
        required.update(day for sample in samples for day in sample["marks"])
        sessions = self._sessions(self.calendar_provider, date.fromisoformat(min(required)),
                                  date.fromisoformat(max(required)))
        available = {day.isoformat() for day in sessions}
        return sorted(available) if sessions and required <= available else []

    def train_and_evaluate(self, as_of: datetime | str | None = None) -> dict[str, Any]:
        """Chronological, purged challenger gate; shadow and rollback are independent of old tiers."""
        now = _at(as_of or self.clock())
        samples = self._dataset(now)
        all_samples = self._dataset(now, dedupe=False)
        days = self._evaluation_days(now)
        samples = [sample for sample in samples if sample["day"] in set(days)]
        all_samples = [sample for sample in all_samples if sample["day"] in set(days)]
        registry = self._registry()
        from .local_algorithm import algorithm_contract
        current_contract = algorithm_contract()["contractId"]
        if registry.get("algorithmContractId") != current_contract:
            # Preserve old audit results, but never relabel their pass/fail or
            # returns as validation of newly changed rules or entry eligibility.
            registry["priorValidation"] = {
                "algorithmContractId": registry.get("algorithmContractId"),
                "gates": registry.get("gates") or {},
                "lastTrainingAt": registry.get("lastTrainingAt"),
            }
            registry.update(algorithmContractId=current_contract, gates={}, shadowDays=0)
            registry.pop("lastCandidateTrainThrough", None)
            registry.pop("lastEvaluatedThrough", None)
        registry["lastTrainingAt"] = now.isoformat()
        complete_cohort = self._complete_universe(set(days), all_samples) if days else False
        registry["cohortAudit"] = {"frozenSignalDays": days,
            "completedSignalCount": len(all_samples), "completeQueue": complete_cohort,
            "cohortSource": "immutable_official_scan_ledger_not_surviving_outcomes",
            "knownThrough": now.isoformat()}
        if len(days) < MIN_MATURE_DAYS or len(samples) < MIN_VALID_SAMPLES:
            registry["reason"] = "insufficient_independent_mature_days_or_samples"
            self._save_registry(registry, now)
            return self.learning_state(now)
        if not complete_cohort:
            registry["reason"] = "incomplete_frozen_cohort_pending_or_missing_outcomes"
            self._save_registry(registry, now)
            return self.learning_state(now)
        active_version = registry.get("championVersion")
        if active_version and self._model_artifact(active_version) is None:
            prior = registry.get("rollbackVersion")
            registry["championVersion"] = prior if self._model_artifact(prior) else None
            registry["rollbackVersion"] = None
            registry["activeAfterDay"] = now.date().isoformat()
            registry["reason"] = "active_artifact_invalid_automatic_rollback"
            self._save_registry(registry, now)
            return self.learning_state(now)
        if active_version and registry.get("lastEvaluatedThrough") != days[-1]:
            artifact = self._model_artifact(active_version)
            forward_days = sorted(day for day in days if day > registry.get("activeAfterDay", "9999"))
            if artifact and len(forward_days) >= SHADOW_DAYS:
                recent_days = set(forward_days[-SHADOW_DAYS:])
                recent = [sample for sample in all_samples if sample["day"] in recent_days]
                active_scores = self._forward_scores(active_version, recent)
                if active_scores is None:
                    gates = {"passed": False, "reason": "active_forward_predictions_missing"}
                else:
                    try:
                        _, logistic_scores = self._predict_artifact(artifact, recent)
                        rule_scores = np.asarray([float(item["features"].get("finalScore", 0))
                                                  for item in recent], dtype=float)
                        gates = self._gates(recent, active_scores, logistic_scores, seed=917,
                                            config=self.execution_config,
                                            complete_universe=self._complete_universe(recent_days, recent),
                                            current_scores=rule_scores,
                                            calendar_days=self._gate_calendar(recent, recent_days),
                                            evaluation_days=sorted(recent_days), minimum_days=SHADOW_DAYS,
                                            minimum_completed_trades=_VALIDATION["minimumShadowTrades"])
                    except Exception:
                        gates = {"passed": False, "reason": "active_prediction_or_nav_invalid"}
                registry["gates"] = {**registry.get("gates", {}), "liveRolling": gates}
                registry["lastEvaluatedThrough"] = days[-1]
                if not gates["passed"]:
                    registry["championVersion"] = registry.get("rollbackVersion")
                    registry["rollbackVersion"] = None
                    registry["activeAfterDay"] = now.date().isoformat()
                    registry["reason"] = "active_gate_failed_automatic_rollback"
                    with self._connect() as db:
                        db.execute("UPDATE model_versions SET stage='rolled_back' WHERE version=?", (active_version,))
                    self._save_registry(registry, now)
                    return self.learning_state(now)
        shadow_version = registry.get("shadowVersion")
        if shadow_version:
            artifact = self._model_artifact(shadow_version)
            if artifact is None:
                registry["shadowVersion"] = None
                registry["shadowDays"] = 0
                registry["reason"] = "shadow_artifact_invalid_rejected"
                self._save_registry(registry, now)
                return self.learning_state(now)
            future_days = {day for day in days if day > registry.get("shadowAfterDay", "9999")}
            future = [sample for sample in all_samples if sample["day"] in future_days]
            shadow_days = len(future_days)
            registry["shadowDays"] = shadow_days
            if artifact and shadow_days >= SHADOW_DAYS:
                candidate = self._forward_scores(shadow_version, future)
                if candidate is None:
                    gates = {"passed": False, "reason": "shadow_forward_predictions_missing"}
                else:
                    try:
                        _, logistic = self._predict_artifact(artifact, future)
                        gates = self._gates(future, candidate, logistic, seed=617,
                                            config=self.execution_config,
                                            complete_universe=self._complete_universe(future_days, future),
                                            calendar_days=self._gate_calendar(future, future_days),
                                            evaluation_days=sorted(future_days), minimum_days=SHADOW_DAYS,
                                            minimum_completed_trades=_VALIDATION["minimumShadowTrades"])
                    except Exception:
                        gates = {"passed": False, "reason": "shadow_prediction_or_nav_invalid"}
                registry["gates"] = {"holdout": registry.get("gates", {}).get("holdout"), "shadow": gates}
                if gates["passed"]:
                    registry["rollbackVersion"] = registry.get("championVersion")
                    registry["championVersion"] = shadow_version
                    registry["activeAfterDay"] = now.date().isoformat()
                    registry["reason"] = "shadow_passed_promoted"
                    with self._connect() as db:
                        db.execute("UPDATE model_versions SET stage='active' WHERE version=?", (shadow_version,))
                else:
                    registry["reason"] = "shadow_failed_kept_rules_or_prior_champion"
                    with self._connect() as db:
                        db.execute("UPDATE model_versions SET stage='rejected' WHERE version=?", (shadow_version,))
                registry["shadowVersion"] = None
                registry["shadowDays"] = 0
            else:
                registry["reason"] = "shadow_collecting_forward_mature_days"
            self._save_registry(registry, now)
            return self.learning_state(now)
        if registry.get("lastCandidateTrainThrough") == days[-1]:
            self._save_registry(registry, now)
            return self.learning_state(now)
        # Reserve a real sixty-day untouched holdout even during cold start.
        # With a longer history this converges to the previous 60/20/20 split.
        test_start = len(days) - max(MIN_HOLDOUT_DAYS, math.ceil(len(days) * _VALIDATION["holdoutFraction"]))
        calibration_start = min(int(test_start * _VALIDATION["preHoldoutTrainFraction"]),
                                test_start - (_VALIDATION["minimumCalibrationDays"] + PURGE_DAYS))
        train_days = set(days[:max(0, calibration_start - PURGE_DAYS)])
        calibration_days = set(days[calibration_start:max(calibration_start, test_start - PURGE_DAYS)])
        holdout_days = set(days[test_start:])
        raw_train = [sample for sample in samples if sample["day"] in train_days]
        raw_calibration = [sample for sample in samples if sample["day"] in calibration_days]
        train = self._purge_before(raw_train, days[calibration_start])
        calibration = self._purge_before(raw_calibration, days[test_start])
        holdout = [sample for sample in all_samples if sample["day"] in holdout_days]
        if (len(train) < _VALIDATION["minimumTrainSamples"]
                or len({item["day"] for item in calibration}) < _VALIDATION["minimumCalibrationDays"]
                or len(holdout_days) < MIN_HOLDOUT_DAYS):
            registry["reason"] = "train_calibration_test_split_too_small"
            self._save_registry(registry, now)
            return self.learning_state(now)
        registry["splitAudit"] = {
            "trainDays": sorted(train_days), "calibrationDays": sorted(calibration_days),
            "holdoutDays": sorted(holdout_days), "trainSamples": len(train),
            "calibrationSamples": len(calibration), "holdoutSamples": len(holdout),
            "purgedByActualLabelEnd": {"train": len(raw_train) - len(train),
                                       "calibration": len(raw_calibration) - len(calibration)},
            "holdoutSignalIds": sorted(sample["signalId"] for sample in holdout),
            "minimumGapSessions": PURGE_DAYS, "splitPolicy": _VALIDATION["splitPolicy"]}
        present = defaultdict(int)
        for sample in train:
            for key in sample["features"]:
                present[key] += 1
        features = sorted(key for key, count in present.items() if count >= len(train) * 0.6)
        if not features:
            registry["reason"] = "features_insufficient"
            self._save_registry(registry, now)
            return self.learning_state(now)
        medians = [float(np.median([sample["features"][key] for sample in train
                                    if key in sample["features"]])) for key in features]
        x_train = self._matrix(train, features, medians)
        x_calibration = self._matrix(calibration, features, medians)
        x_test = self._matrix(holdout, features, medians)
        target = np.asarray([sample["net"] > 0 for sample in train], dtype=int)
        if len(set(target)) != 2:
            registry["reason"] = "single_class_training_labels"
            self._save_registry(registry, now)
            return self.learning_state(now)
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from lightgbm import LGBMRegressor
            scaler = StandardScaler().fit(x_train)
            logistic = LogisticRegression(max_iter=500, class_weight="balanced", random_state=23)
            logistic.fit(scaler.transform(x_train), target)
            baseline_score = logistic.predict_proba(scaler.transform(x_test))[:, 1]
            model_args = dict(n_estimators=80, num_leaves=7, min_child_samples=30,
                              learning_rate=0.04, max_depth=4, random_state=23,
                              verbosity=-1, n_jobs=1)
            net_model = LGBMRegressor(**model_args).fit(
                x_train, np.asarray([sample["net"] for sample in train], dtype=float))
            risk_model = LGBMRegressor(**model_args).fit(
                x_train, np.asarray([sample["mae"] for sample in train], dtype=float))
            risk_coefficient = 1.0
            cal_utility = (net_model.predict(x_calibration)
                           - risk_coefficient * np.maximum(0.0, risk_model.predict(x_calibration)))
            ordered_utility = np.sort(np.asarray(cal_utility, dtype=float))
            if len(ordered_utility) < 2 or not np.all(np.isfinite(ordered_utility)):
                raise ValueError("calibration_utility_invalid")
            knots = np.unique(ordered_utility)
            percentiles = (np.searchsorted(ordered_utility, knots, side="right")
                           / len(ordered_utility) * 100).astype(float)
            if len(knots) == 1:
                knots = np.asarray([knots[0] - 1e-9, knots[0] + 1e-9])
                percentiles = np.asarray([50.0, 50.0])
            test_utility = (net_model.predict(x_test)
                            - risk_coefficient * np.maximum(0.0, risk_model.predict(x_test)))
            challenger_score = np.interp(test_utility, knots, percentiles)
        except (ImportError, ValueError) as exc:
            registry["reason"] = f"model_training_unavailable:{type(exc).__name__}"
            self._save_registry(registry, now)
            return self.learning_state(now)
        gates = self._gates(holdout, challenger_score, baseline_score, seed=317,
                            config=self.execution_config,
                            complete_universe=self._complete_universe(holdout_days, holdout),
                            calendar_days=self._gate_calendar(holdout, holdout_days),
                            evaluation_days=sorted(holdout_days))
        registry["lastCandidateTrainThrough"] = days[-1]
        registry["gates"] = {"holdout": gates}
        registry.setdefault("validationHistory", []).append({
            "at": now.isoformat(), "contractId": current_contract, "gates": gates,
            "splitAudit": registry["splitAudit"], "modelParameters": model_args,
            "riskCoefficient": risk_coefficient})
        if not gates["passed"]:
            registry["reason"] = "challenger_holdout_gate_failed"
            self._save_registry(registry, now)
            return self.learning_state(now)
        version = f"king-v1.2-lgbm-{now.strftime('%Y%m%d%H%M%S')}-{sha256(str(days[-1]).encode()).hexdigest()[:6]}"
        artifact = {
            "version": version, "features": features, "medians": medians,
            "netRegressor": net_model.booster_.model_to_string(),
            "maeRegressor": risk_model.booster_.model_to_string(),
            "riskCoefficient": risk_coefficient,
            "calibrationKnots": knots.tolist(),
            "calibrationPercentiles": percentiles.tolist(),
            "logisticCoef": logistic.coef_[0].tolist(),
            "logisticIntercept": float(logistic.intercept_[0]),
            "logisticMean": scaler.mean_.tolist(), "logisticScale": scaler.scale_.tolist(),
            "trainedThrough": max(train_days), "calibrationThrough": max(calibration_days),
            "holdoutThrough": days[-1], "purgeTradingDays": PURGE_DAYS,
            "purgePolicy": _VALIDATION["purgePolicy"],
            "validationRequirements": {"holdoutDays": MIN_HOLDOUT_DAYS,
                "holdoutCompletedTrades": MIN_HOLDOUT_TRADES, "shadowDays": SHADOW_DAYS},
            "rankMeaning": "net_return_minus_MAE_calibrated_percentile_not_probability",
            "priceBasis": "unadjusted_minute_bar",
            "executionModel": MODEL_VERSION,
        }
        from .local_algorithm import algorithm_contract
        artifact["algorithmContractId"] = algorithm_contract()["contractId"]
        with self._connect() as db:
            db.execute("INSERT INTO model_versions VALUES(?,?,?,?,?)",
                       (version, "shadow", _json(artifact), _json(gates), now.isoformat()))
        registry["shadowVersion"] = version
        # Forecasts cannot exist before this artifact did. Begin the next full
        # official day, not at the latest already-mature signal date (T-5).
        registry["shadowAfterDay"] = now.date().isoformat()
        registry["shadowStartedAt"] = now.isoformat()
        registry["shadowDays"] = 0
        registry["reason"] = "holdout_passed_shadow_started"
        self._save_registry(registry, now)
        return self.learning_state(now)

    def rank_candidates(self, candidates: Iterable[Mapping[str, Any]],
                        as_of: datetime | str | None = None) -> list[dict[str, Any]]:
        """Use only an active champion; otherwise keep the transparent V1.1 rule ranking."""
        rows = [dict(candidate) for candidate in candidates]
        for index, row in enumerate(rows):
            row["rankingTieBreakOrder"] = index
        registry = self._registry()
        eligible = [row for row in rows if _current_entry_eligible(row)]
        excluded = [row for row in rows if not _current_entry_eligible(row)]
        for version in (registry.get("championVersion"), registry.get("rollbackVersion")):
            artifact = self._model_artifact(version, active_only=True) if version else None
            if artifact and eligible:
                sample_rows = [{"features": _feature_values(row)} for row in eligible]
                try:
                    scores, _ = self._predict_artifact(artifact, sample_rows)
                    if len(scores) != len(eligible) or not np.all(np.isfinite(scores)):
                        raise ValueError("model_rank_invalid")
                    for row, score in zip(eligible, scores):
                        # Calibrated percentile ranking, never a main-rise probability.
                        row["learningRankScore"] = float(score)
                        row["learningRankVersion"] = version
                    for row in excluded:
                        row["learningRankVersion"] = "rules_v1.2"
                    return sorted(eligible, key=lambda item: item["learningRankScore"], reverse=True) + excluded
                except Exception:
                    continue
        for row in rows:
            row["learningRankVersion"] = "rules_v1.2"
        return sorted(rows, key=lambda item: _number(item.get("finalScore")) or -1e9, reverse=True)
