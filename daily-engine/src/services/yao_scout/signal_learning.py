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


TZ = ZoneInfo("Asia/Shanghai")
LEDGER_VERSION = "king-v1.1-signal-ledger"
MODEL_VERSION = "king-v1.1-execution-model"
OFFICIAL_SLOTS = frozenset({"0920", "0940", "0955", "1030", "1455"})
ENTRY_SLOTS = OFFICIAL_SLOTS - {"0920"}
HORIZONS = (1, 3, 5)
MIN_MATURE_DAYS = 120
MIN_VALID_SAMPLES = 1000
PURGE_DAYS = 5
SHADOW_DAYS = 20
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
    )
    return {key: candidate[key] for key in keys if key in candidate}


def _source_time(candidate: Mapping[str, Any]) -> str | None:
    quote = candidate.get("quote") if isinstance(candidate.get("quote"), Mapping) else {}
    value = quote.get("provider_timestamp") or quote.get("source_time") or candidate.get("sourceTime")
    try:
        return _at(value).isoformat() if value else None
    except (TypeError, ValueError):
        return None


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
        selected = {_code(item.get("code")) for item in result.get("candidates", []) if isinstance(item, Mapping)}
        selected.discard(None)
        records: dict[str, Mapping[str, Any]] = {}
        for collection in ("candidates", "controls", "precisionResearch", "windvanes", "evidenceInsufficient"):
            for item in result.get(collection, []) or []:
                if isinstance(item, Mapping) and (code := _code(item.get("code"))):
                    # The selected version has the authoritative entry decision.
                    if code not in records or collection == "candidates":
                        records[code] = item
        scan_meta = {
            "sourceRunId": result.get("run_id") or result.get("runId"),
            "modelVersion": result.get("modelVersion") or result.get("model_version"),
            "status": result.get("status"),
            "dataQuality": result.get("dataQuality") or result.get("data_quality") or {},
            "selectedCodes": sorted(selected),
            "researchCodes": sorted(records),
            "result": result,
        }
        scan_version = str(result.get("scoreVersion") or result.get("modelVersion")
                           or result.get("model_version") or LEDGER_VERSION)
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
                        eligible = bool(official and run_usable and slot in ENTRY_SLOTS
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
                         int(eligible), int(reviewable_run), _json(_feature_values(item)), _json(snap),
                         "pending" if reviewable_run else "observation_only"),
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
        output = dict(result)
        output["learningLedger"] = {
            "version": LEDGER_VERSION, "runId": run_id, "official": official,
            "idempotentReplay": existing, "selectedCount": len(selected),
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
            where.append("EXISTS(SELECT 1 FROM signal_events x WHERE x.run_id=r.run_id AND x.code=?)")
            args.append(code)
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
                events = db.execute(
                    "SELECT * FROM signal_events WHERE run_id=? ORDER BY selected DESC, decision_at, code", (run["run_id"],)
                ).fetchall()
                signals = []
                for event in events:
                    if filters.get("symbol") and event["code"] != _code(filters["symbol"]):
                        continue
                    snapshot = json.loads(event["snapshot_json"])
                    signals.append({
                        "signalId": event["signal_id"], "code": event["code"], "name": event["name"],
                        "selected": bool(event["selected"]), "decisionAt": event["decision_at"],
                        "sourceTime": event["source_time"], "signalPrice": event["signal_price"],
                        "firstTradablePrice": None, "reviewStatus": event["review_status"],
                        "trainingEligible": bool(event["training_eligible"]),
                        "reviewEligible": bool(event["review_eligible"]),
                        "features": json.loads(event["features_json"]),
                        "reason": snapshot.get("selectionReasons") or [],
                        "snapshot": snapshot,
                    })
                items.append({
                    "runId": run["run_id"], "slot": run["slot"], "asOf": run["as_of"],
                    "official": bool(run["official"]), "modelVersion": run["version"],
                    "status": run["status"], "signals": signals,
                    "scan": json.loads(run["scan_json"]),
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
                + clause + " ORDER BY r.trading_day DESC, h.horizon, e.code LIMIT ?", (*args, limit),
            ).fetchall()
        items = [self._outcome_dict(row) for row in rows]
        return {"items": items, "count": len(items)}

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
            "reason": row["reason"], "reviewedAt": row["reviewed_at"],
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
        if source_at is None or not 0 <= (decision - source_at).total_seconds() <= 30:
            return {**base, "reason": "signal_quote_missing_or_stale"}
        if event["signal_price"] is None or _number(event["signal_price"], positive=True) is None:
            return {**base, "reason": "signal_price_missing"}
        tradability = snapshot.get("tradability") if isinstance(snapshot.get("tradability"), Mapping) else {}
        if tradability.get("status") == "unavailable" or quote.get("suspended") is True:
            return {**base, "status": "no_fill", "fill_status": "unavailable_at_signal",
                    "reason": "suspended_or_unavailable_at_signal"}
        if quote.get("no_price_limit") is True or quote.get("corporate_action") is True:
            return {**base, "reason": "price_limit_or_corporate_action_unverifiable"}
        rate = self._rate(event["code"], event["name"])
        pre_close = _number(quote.get("pre_close") or quote.get("previous_close"), positive=True)
        source_up = _number(quote.get("limit_up"), positive=True)
        if rate is None or pre_close is None or source_up is None:
            return {**base, "reason": "point_in_time_price_limit_missing"}
        up_limit = source_up
        if abs(up_limit - self._price_limit(pre_close, rate, "up")) > 0.015:
            return {**base, "reason": "source_price_limit_conflicts_with_board_rule"}
        if event["code"] not in cached_bars:
            try:
                cached_bars[event["code"]] = self._bar_rows(self.bar_fetcher(event["code"], as_of), as_of)
            except Exception:
                cached_bars[event["code"]] = []
        bars = cached_bars[event["code"]]
        same_day = [bar for bar in bars if _at(bar["end"]).date() == decision.date()]
        if not same_day:
            return {**base, "status": "pending_data", "reason": "entry_minute_bars_missing"}
        target = datetime.combine(target_day, time(15, 0), TZ)
        target_bars = [bar for bar in bars if _at(bar["end"]) == target]
        first_end = decision.replace(second=0, microsecond=0) + timedelta(
            minutes=2 if decision.second or decision.microsecond else 1)
        observation_path = [bar for bar in bars if first_end <= _at(bar["end"]) <= target]
        if not target_bars or not observation_path:
            return {**base, "status": "pending_data", "reason": "target_close_minute_missing"}
        mark_days = sessions[index:index + horizon + 1]
        observed_ends = {_at(bar["end"]) for bar in observation_path}
        for day in mark_days:
            minute = datetime.combine(day, time(9, 31), TZ)
            close_time = datetime.combine(day, time(15, 0), TZ)
            while minute <= close_time:
                if bar_session(minute) and (day != decision.date() or minute >= first_end):
                    if minute not in observed_ends:
                        return {**base, "status": "pending_data", "reason": "incomplete_minute_path"}
                minute += timedelta(minutes=1)
        marks = {day.isoformat(): next((bar["close"] for bar in observation_path
                                       if _at(bar["end"]) == datetime.combine(day, time(15, 0), TZ)), None)
                 for day in mark_days}
        if any(mark is None for mark in marks.values()):
            return {**base, "status": "pending_data", "reason": "daily_close_marks_missing"}
        signal_price = event["signal_price"]
        details = {"targetDay": target_day.isoformat(), "simulationVersion": MODEL_VERSION,
                   "exitPolicy": "15:00_close_auction_minute_proxy",
                   "dailyCloseMarks": marks,
                   "observedReturn": target_bars[0]["close"] / signal_price - 1,
                   "observedMfe": max(0.0, max(bar["high"] / signal_price - 1 for bar in observation_path)),
                   "observedMae": max(0.0, 1 - min(bar["low"] / signal_price for bar in observation_path)),
                   "pathResolution": "completed_one_minute_bars"}
        base["details_json"] = _json(details)
        window = [bar for bar in same_day if _at(bar["end"]) == first_end]
        if not window:
            return {**base, "status": "pending_data", "reason": "first_complete_minute_missing"}
        bar = window[0]
        proposed = round(bar["open"] * (1 + self.execution_config.slippage_bps / 10000), 2)
        min_lot, lot_step = _lot(event["code"])
        budget = self.execution_config.initial_cash * self.execution_config.position_weight
        quantity = min_lot + max(0, int((budget / proposed - min_lot) // lot_step)) * lot_step
        if (not _number(bar.get("volume_shares"), positive=True)
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
        # An unadjusted discontinuity can signal an ex-rights day. Avoid a false return.
        for previous, current in zip(path, path[1:]):
            if _at(previous["end"]).date() == _at(current["end"]).date():
                continue
            prior_close = previous["close"]
            if current["open"] < prior_close * (1 - rate - 0.03) or current["open"] > prior_close * (1 + rate + 0.03):
                return {**base, "reason": "possible_corporate_action_or_discontinuous_bars"}
        exit_bar = target_bars[0]
        exit_price = round(exit_bar["close"] * (1 - self.execution_config.slippage_bps / 10000), 2)
        previous_close = next((bar["close"] for bar in reversed(path)
                               if _at(bar["end"]).date() < target_day), None)
        down_limit = self._price_limit(previous_close, rate, "down") if previous_close else None
        if (exit_bar["volume_shares"] is None or exit_bar["volume_shares"] <= 0
                or (down_limit is not None and exit_bar["close"] <= down_limit + 0.005)
                or exit_price < exit_bar["low"] - 1e-8):
            return {**base, "status": "exit_blocked", "fill_status": "simulated_entry_exit_blocked",
                    "entry_at": entry_at.isoformat(), "entry_price": entry_price,
                    "reason": "suspended_limit_down_or_exit_slippage"}
        buy_notional, sell_notional = quantity * entry_price, quantity * exit_price
        buy_cost = _fees(buy_notional, "buy", entry_at.date(), self.execution_config)["total"]
        sell_cost = _fees(sell_notional, "sell", target_day, self.execution_config)["total"]
        net = (sell_notional - sell_cost) / (buy_notional + buy_cost) - 1
        return {**base, "status": "mature", "fill_status": "simulated_fill",
                "evidence_grade": "simulated_minute_proxy", "entry_at": entry_at.isoformat(),
                "entry_price": entry_price, "exit_at": _at(exit_bar["end"]).isoformat(),
                "exit_price": exit_price, "simulated_net_return": net,
                "mfe": max(0.0, max(bar["high"] / entry_price - 1 for bar in path)),
                "mae": max(0.0, 1 - min(bar["low"] / entry_price for bar in path)), "reason": None,
                "details_json": _json({**details, "entryMinuteVolume": entry_bar["volume_shares"]})}

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
                    old = db.execute("SELECT status FROM signal_outcomes WHERE signal_id=? AND horizon=?",
                                     (event["signal_id"], horizon)).fetchone()
                    if old and old["status"] in {"mature", "no_fill", "exit_blocked", "unverified"}:
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
                                 else "incomplete" if any(state in {"no_fill", "exit_blocked", "unverified"} for state in states)
                                 else "pending")
                db.execute("UPDATE signal_events SET review_status=? WHERE signal_id=?",
                           (review_status, event["signal_id"]))
        return {"items": changed, "updated": len(changed), "asOf": timestamp.isoformat()}

    def _dataset(self, as_of: datetime, *, dedupe: bool = True) -> list[dict[str, Any]]:
        """Point-in-time signals; no-fill remains a zero-return cash decision."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT e.*,r.trading_day,r.slot,r.version scan_version,o1.status h1,o3.status h3,o5.status h5,"
                "o5.simulated_net_return net,o5.mfe mfe,o5.mae mae,o5.exit_at exit_at,"
                "o5.entry_at entry_at,o5.entry_price entry_price,o5.exit_price exit_price,"
                "o5.details_json details_json "
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
            seen.add(key)
            result.append({"day": row["trading_day"], "code": row["code"], "runId": row["run_id"],
                           "signalId": row["signal_id"], "features": features,
                           "net": float(row["net"] or 0.0), "filled": row["h5"] == "mature",
                           "mfe": float(row["mfe"] or 0.0),
                           "mae": float(row["mae"] or 0.0), "exitAt": row["exit_at"],
                           "entryAt": row["entry_at"], "entryPrice": row["entry_price"],
                           "exitPrice": row["exit_price"], "marks": details.get("dailyCloseMarks") or {},
                           "entryMinuteVolume": details.get("entryMinuteVolume"),
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
        effective_version, effective_source, fallback = "rules_v1.1", "rules", None
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
            "matureDays": days, "validSamples": len(samples),
            "minimumMatureDays": MIN_MATURE_DAYS, "minimumValidSamples": MIN_VALID_SAMPLES,
            "gates": (registry.get("gates") or {}) if validation_matches else {},
            "validationContractId": contract["contractId"] if validation_matches else None,
            "historicalValidationAvailable": bool(registry.get("gates") and not validation_matches
                                                  or (registry.get("priorValidation") or {}).get("gates")),
            "lastTrainingAt": registry.get("lastTrainingAt"),
            "reason": reason, "rollbackVersion": registry.get("rollbackVersion"),
            "lastEvaluatedThrough": registry.get("lastEvaluatedThrough"),
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
        queue. A no-fill leaves its allocation in cash. Sell occurs at T+5 close.
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
        nav_values, trade_count, attempts = [], 0, 0
        previous_nav = cash
        traded_codes: set[tuple[str, str]] = set()
        for day in all_days:
            groups = sorted(by_day.get(day, {}).values(),
                            key=lambda group: min(item["decisionAt"] for _, item in group))
            for group in groups:
                slots = max(0, config.max_positions - len(positions))
                if not slots:
                    break
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
                    cash -= quantity * entry_price + fee
                    positions.append({"code": item["code"], "quantity": quantity,
                                      "exitDay": item["exitAt"][:10], "exitPrice": exit_price,
                                      "marks": item["marks"]})
                    trade_count += 1
            remaining = []
            for position in positions:
                if position["exitDay"] == day:
                    proceeds = position["quantity"] * position["exitPrice"]
                    fee = _fees(proceeds, "sell", date.fromisoformat(day), config)["total"]
                    cash += proceeds - fee
                else:
                    remaining.append(position)
            positions = remaining
            equity = cash
            for position in positions:
                mark = _number(position["marks"].get(day), positive=True)
                if mark is None:
                    return {"valid": False, "reason": "open_position_daily_close_missing"}
                equity += position["quantity"] * mark
            nav_values.append(equity)
            previous_nav = equity
        nav = np.asarray([config.initial_cash, *nav_values], dtype=float)
        returns = nav[1:] / nav[:-1] - 1
        peaks = np.maximum.accumulate(nav)
        return {"valid": True, "days": all_days, "nav": nav.tolist(), "daily": returns,
                "net": float(nav[-1] / nav[0] - 1),
                "maxDrawdown": float(np.max(1 - nav / peaks)),
                "filledPicks": trade_count, "attemptedPicks": attempts}

    @staticmethod
    def _gates(samples: list[dict[str, Any]], challenger: np.ndarray,
               logistic: np.ndarray, *, seed: int, config: AccountConfig,
               complete_universe: bool, current_scores: np.ndarray | None = None,
               calendar_days: list[str] | None = None) -> dict[str, Any]:
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
        if len(returns) < 20 or candidate["days"] != current["days"]:
            return {"passed": False, "reason": "fewer_than_20_aligned_nav_days", "days": len(returns)}
        rng = np.random.default_rng(seed)
        block = max(PURGE_DAYS, 5)
        starts = rng.integers(0, max(1, len(returns) - block + 1),
                              size=(2000, math.ceil(len(returns) / block)))
        indices = np.concatenate([starts + offset for offset in range(block)], axis=1)[:, :len(returns)]
        indices = np.clip(indices, 0, len(returns) - 1)
        sampled_net = np.prod(1 + returns[indices], axis=1) - 1
        sampled_current = np.prod(1 + current_returns[indices], axis=1) - 1
        net_lower = float(np.quantile(sampled_net, 0.05))
        advantage_lower = float(np.quantile(sampled_net - sampled_current, 0.05))
        drawdown_ok = candidate["maxDrawdown"] <= current["maxDrawdown"] + 1e-12
        passed = net_lower > 0 and advantage_lower > 0 and drawdown_ok
        return {
            "passed": bool(passed), "days": len(returns),
            "simulatedAccountNet": candidate["net"],
            "currentPolicyAccountNet": current["net"],
            "logisticAccountNet": extra["net"],
            "net95Lower": net_lower, "advantage95Lower": advantage_lower,
            "maxDrawdown": candidate["maxDrawdown"],
            "currentPolicyMaxDrawdown": current["maxDrawdown"],
            "drawdownNotWorse": bool(drawdown_ok), "filledPicks": candidate["filledPicks"],
            "attemptedPicks": candidate["attemptedPicks"],
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
        from .local_algorithm import algorithm_contract
        expected = set()
        for row in rows:
            try:
                if row[2] == algorithm_contract()["scoreVersion"] and _current_entry_eligible(json.loads(row[1])):
                    expected.add(row[0])
            except (TypeError, ValueError):
                continue
        return bool(expected) and expected == {sample["signalId"] for sample in samples}

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

    def _gate_calendar(self, samples: list[dict[str, Any]]) -> list[str]:
        if not samples:
            return []
        required = {sample["day"] for sample in samples}
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
        days = sorted({sample["day"] for sample in samples})
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
        if len(days) < MIN_MATURE_DAYS or len(samples) < MIN_VALID_SAMPLES:
            registry["reason"] = "insufficient_independent_mature_days_or_samples"
            self._save_registry(registry, now)
            return self.learning_state(now)
        active_version = registry.get("championVersion")
        if active_version and self._model_artifact(active_version) is None:
            prior = registry.get("rollbackVersion")
            registry["championVersion"] = prior if self._model_artifact(prior) else None
            registry["rollbackVersion"] = None
            registry["activeAfterDay"] = days[-1]
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
                                            calendar_days=self._gate_calendar(recent))
                    except Exception:
                        gates = {"passed": False, "reason": "active_prediction_or_nav_invalid"}
                registry["gates"] = {**registry.get("gates", {}), "liveRolling": gates}
                registry["lastEvaluatedThrough"] = days[-1]
                if not gates["passed"]:
                    registry["championVersion"] = registry.get("rollbackVersion")
                    registry["rollbackVersion"] = None
                    registry["activeAfterDay"] = days[-1]
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
            future = [sample for sample in all_samples if sample["day"] > registry.get("shadowAfterDay", "9999")]
            shadow_days = len({sample["day"] for sample in future})
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
                                            complete_universe=self._complete_universe(
                                                {sample["day"] for sample in future}, future),
                                            calendar_days=self._gate_calendar(future))
                    except Exception:
                        gates = {"passed": False, "reason": "shadow_prediction_or_nav_invalid"}
                registry["gates"] = {"holdout": registry.get("gates", {}).get("holdout"), "shadow": gates}
                if gates["passed"]:
                    registry["rollbackVersion"] = registry.get("championVersion")
                    registry["championVersion"] = shadow_version
                    registry["activeAfterDay"] = days[-1]
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
        calibration_start = int(len(days) * 0.60)
        test_start = int(len(days) * 0.80)
        train_days = set(days[:max(0, calibration_start - PURGE_DAYS)])
        calibration_days = set(days[calibration_start:max(calibration_start, test_start - PURGE_DAYS)])
        holdout_days = set(days[test_start:])
        train = [sample for sample in samples if sample["day"] in train_days]
        calibration = [sample for sample in samples if sample["day"] in calibration_days]
        holdout = [sample for sample in all_samples if sample["day"] in holdout_days]
        if (len(train) < 300 or len({item["day"] for item in calibration}) < 15
                or len({item["day"] for item in holdout}) < 20):
            registry["reason"] = "train_calibration_test_split_too_small"
            self._save_registry(registry, now)
            return self.learning_state(now)
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
                            calendar_days=self._gate_calendar(holdout))
        registry["lastCandidateTrainThrough"] = days[-1]
        registry["gates"] = {"holdout": gates}
        if not gates["passed"]:
            registry["reason"] = "challenger_holdout_gate_failed"
            self._save_registry(registry, now)
            return self.learning_state(now)
        version = f"king-v1.1-lgbm-{now.strftime('%Y%m%d%H%M%S')}-{sha256(str(days[-1]).encode()).hexdigest()[:6]}"
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
        registry["shadowAfterDay"] = days[-1]
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
                        row["learningRankVersion"] = "rules_v1.1"
                    return sorted(eligible, key=lambda item: item["learningRankScore"], reverse=True) + excluded
                except Exception:
                    continue
        for row in rows:
            row["learningRankVersion"] = "rules_v1.1"
        return sorted(rows, key=lambda item: _number(item.get("finalScore")) or -1e9, reverse=True)
