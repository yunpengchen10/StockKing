"""Independent, point-in-time ledger and task-specific release gate for King signals.

This is deliberately separate from ``yao_outcomes`` and the three daily-bar
tiers.  A saved scan is immutable; subsequent reviews append *simulated* order
and path observations.  Neither a quote nor a minute OHLC bar is a broker fill.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
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

import numpy as np

from src.quant.executable_backtest import AccountConfig, _fees, _lot
from src.services.yao_scout.minute_history import bar_session, normalize_bars
from .task_learning import scoped_lineage_validation
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


class _ReviewBarCache(OrderedDict):
    """Bound the minute-path working set while reviewing a whole stock pool."""
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > 8:
            self.popitem(last=False)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value


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


def _quantity_with_fees(code: str, price: float, budget: float, day: date, config: AccountConfig) -> tuple[int, float]:
    """One conservative lot/fee sizing function for labels and account replay."""
    minimum, step = _lot(code)
    quantity = minimum + max(0, int((budget / price - minimum) // step)) * step
    while quantity >= minimum:
        fee = _fees(quantity * price, 'buy', day, config)['total']
        if quantity * price + fee <= budget:
            return quantity, fee
        quantity -= step
    return 0, 0.


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
    # Optional, signal-time features keep missingness; future/backfilled
    # indicators never enter a frozen training vector.
    decision = candidate.get('decision_at')
    wanted = {'speed_5m_pct', 'speed_15m_pct', 'speed_30m_pct', 'atr14',
              'sector_relative_5m_pct', 'sector_return_5m_pct', 'vwap_slope_5m_pct',
              'bias_ma5_pct', 'atr_extension', 'spread_pct', 'ask_depth_notional'}
    for row in candidate.get('indicatorEvidence') or []:
        if not isinstance(row, Mapping):
            continue
        key, value = row.get('key'), _number(row.get('value'))
        if key not in wanted or value is None or row.get('status') not in {'observed', None}:
            continue
        try:
            stamp = str(row.get('asOf') or '')
            legal = (bool(decision) and bool(stamp) and
                     (stamp < str(decision)[:10] if len(stamp) == 10 else _at(stamp) <= _at(decision)))
        except (ValueError, TypeError):
            legal = False
        if legal:
            result['observed_' + key] = value
    for key in ('biasMa5Pct', 'atrExtension'):
        value = _number(((candidate.get('precisionDecision') or {}).get('metrics') or {}).get(key))
        if value is not None:
            result[key] = value
    book = candidate.get('tradability') or {}
    for key in ('askDepthNotionalCny', 'bidAskSpreadPct'):
        value = _number(book.get(key))
        if value is not None:
            result[key] = value
    if 'r3_pct' in result and 'r15_pct' in result:
        result['short_rebound_in_long_weak'] = float(result['r3_pct'] > 0 and result['r15_pct'] < 0)
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
        "nextDaySignal", "morningSignal", "firstReferenceTime", "firstReferencePrice", "currentSignalState", "primaryAdmission",
        "dailyLastClose", "dailyPreviousSession", "nextDaySelected", "nextDayContinuationSelected", "recommendationKinds",
        "rawStructureRewardRisk", "entryRiskDistance", "entryRiskFloorAtr", "entryRiskBasis",
        "prediction", "learningDecision", "learningTaskContractId", "dataLineage",
        "learningAlgorithmContractId", "entryExecution", "factorExperiments",
        "learningDataContractId", "dataContractId", "featureContractId", "labelContractId", "modelContractId", "compatibilityPolicy",
        "portfolioWeight", "portfolioWeightKind", "portfolioPolicy", "plannedWeight",
        "factorCutoffAt", "factorAvailability", "marketRegime", "learningDataEligibility",
        "minuteSourceTime", "sectorEvidence", "fundEvidence", "evidenceChecks",
        "dailySignal", "dailyEvidence", "dailyEvidenceStatus", "dailyProfileDecision",
        "dailyFirstReferenceTime", "dailyFirstReferencePrice", "dailyCurrentSignalState",
        "stateLabel", "strategyId", "strategyBranch", "validationStatus", "horizon", "horizonId",
        "support", "resistance", "atr14", "structureRewardRisk", "biasMa5Pct", "atrExtension",
        "eventAssessment", "marketBreadth", "rankMeaning", "riskProfile", "executionQuoteData",
        "trainingEligible", "minuteModelTrainingEligible", "entryEligible", "executable",
        "trendEvidence", "intradayDirectionEvidence", "dailyRankEvidence",
        "observationOnly", "formalEligible", "dailyObservationEligible", "observationSelected",
    )
    return {key: candidate[key] for key in keys if key in candidate}


_COMPACT_KEYS = (
    "code", "name", "decision_at", "signalPrice", "referencePrice", "sourceTime", "quote",
    "entryPlan", "status", "evidenceEligible", "precisionDecision", "risks", "tradability",
    "factorScores", "v11Inputs", "earlyScore", "mainRiseScore", "finalScore", "distributionRisk",
    "dataConfidence", "baselineHistoryDays", "scoreVersion", "selectionReasons", "riskReasons",
    "triggerConditions", "invalidationConditions", "algorithmContractId", "learningRankVersion",
    "learningRankScore", "rank", "rankingTieBreakOrder", "dataEligibility", "factorCoverage",
    "missingFactors", "scoreStage", "selectedScoreBranch", "nextDaySignal", "morningSignal",
    "firstReferenceTime", "firstReferencePrice", "currentSignalState", "primaryAdmission", "nextDaySelected",
    "nextDayContinuationSelected", "recommendationKinds",
    "rawStructureRewardRisk", "entryRiskDistance", "entryRiskFloorAtr", "entryRiskBasis",
    "prediction", "learningDecision", "learningTaskContractId", "dataLineage",
    "learningAlgorithmContractId", "entryExecution", "factorExperiments",
    "learningDataContractId", "dataContractId", "featureContractId", "labelContractId", "modelContractId", "compatibilityPolicy",
    "portfolioWeight", "portfolioWeightKind", "portfolioPolicy", "plannedWeight", "marketRegime",
    "dailySignal", "dailyEvidence", "dailyEvidenceStatus", "dailyProfileDecision",
    "dailyFirstReferenceTime", "dailyFirstReferencePrice", "dailyCurrentSignalState",
    "stateLabel", "strategyId", "strategyBranch", "validationStatus", "horizon", "horizonId",
    "support", "resistance", "atr14", "structureRewardRisk", "biasMa5Pct", "atrExtension",
    "eventAssessment", "marketBreadth", "rankMeaning", "riskProfile",
    "trendEvidence", "intradayDirectionEvidence", "dailyRankEvidence",
    "observationOnly", "formalEligible", "dailyObservationEligible", "observationSelected",
)


def _json_projection(column: str, keys: Iterable[str]) -> str:
    """Project known fields inside SQLite; never transport raw minute evidence."""
    # Older SQLite builds cap function arguments at 127. Keep each JSON call
    # below that bound even as independently versioned signal fields grow.
    keys = list(keys)
    result = "json_object()"
    for start in range(0, len(keys), 50):
        fields = ",".join(f"'$.{key}',json_extract({column},'$.{key}')"
                          for key in keys[start:start + 50])
        # Merge Patch recursively removes null members. Set whole fields instead
        # so explicit unknown values in frozen evidence survive compact reads.
        result = f"json_set({result},{fields})"
    return result


def _compact_features(payload: Mapping[str, Any]) -> dict[str, float]:
    # Old releases accidentally persisted the whole candidate here. Current
    # releases persist the already flattened numeric feature vector.
    if any(isinstance(value, Mapping) for value in payload.values()):
        return _feature_values(payload)
    return {str(key): value for key, raw in payload.items()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(key))
            and (value := _number(raw)) is not None}


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
    selected.difference_update(_code(item.get('code')) for item in result.get('candidates', [])
                               if isinstance(item, Mapping) and item.get('observationOnly') is True)
    records: dict[str, dict[str, Any]] = {}
    for collection in ("candidates", "controls", "precisionResearch", "morningResearch", "invalidatedCandidates", "windvanes", "evidenceInsufficient"):
        for item in result.get(collection, []) or []:
            if isinstance(item, Mapping) and (code := _code(item.get("code"))):
                if code not in records or collection == "candidates":
                    records[code] = dict(item)
    # Research-only rows do not gain execution/training eligibility by existing
    # in a watchlist. Existing candidates/controls keep their original contract.
    execution_sources = {code for code, row in records.items() if not _is_daily_signal(row)}
    # A profile-only daily recommendation must be frozen too. A daily record
    # takes precedence over a same-code minute control: this ledger has one
    # row per stock/run and must never combine the two training contracts.
    daily_profiles: dict[str, set[str]] = defaultdict(set)
    for mapping in (result.get('profileCandidates'), result.get('dailyProfileCandidates')):
        if not isinstance(mapping, Mapping):
            continue
        for profile, rows in mapping.items():
            for row in rows or []:
                if isinstance(row, Mapping) and _is_daily_signal(row) and (code := _code(row.get('code'))):
                    daily_profiles[code].add(str(profile))
                    if not _is_daily_signal(records.get(code) or {}):
                        records[code] = dict(row)
    for row in result.get('dailyCandidates') or []:
        if isinstance(row, Mapping) and _is_daily_signal(row) and (code := _code(row.get('code'))):
            daily_profiles[code].add('regular')
            if not _is_daily_signal(records.get(code) or {}):
                records[code] = dict(row)
    for code, record in records.items():
        if _is_daily_signal(record):
            execution_sources.discard(code)
            if record.get('observationOnly') is True:
                selected.discard(code)
            # These fields belong to the separate minute contract, even if an
            # upstream caller accidentally copied them onto a daily candidate.
            for key in ('morningSignal', 'algorithmContractId', 'learningTaskContractId',
                        'prediction', 'learningDecision', 'dataLineage', 'v11Inputs', 'factorScores'):
                record.pop(key, None)
            record['dailySignal'] = {**record['dailySignal'],
                'selected': bool(record.get('observationOnly') is not True and
                                 (code in selected or daily_profiles.get(code))),
                'selectedProfiles': [] if record.get('observationOnly') else sorted(daily_profiles.get(code, set())),
                'observationProfiles': sorted(daily_profiles.get(code, set())) if record.get('observationOnly') else [],
                'trainingEligibleAtSignal': False}
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
        record["recommendationKinds"] = _primary_kinds(record, code in selected) + (
            ["next_day_watch"] if code in watch else []) + (["next_day_continuation"] if code in continuation else [])
    return selected, records, watch, continuation, execution_sources


def _is_daily_signal(candidate: Mapping[str, Any]) -> bool:
    return isinstance(candidate.get('dailySignal'), Mapping)


def _primary_kinds(candidate: Mapping[str, Any], selected: bool) -> list[str]:
    if _is_daily_signal(candidate):
        return ['daily_condition'] if selected or candidate['dailySignal'].get('selected') is True else []
    return ['execution'] if selected else []


def _freeze_daily_signal(candidate, published_at, *, previous=None):
    """Freeze daily reference prices independently of all morning queues."""
    signal = dict(candidate['dailySignal'])
    prior = (previous or {}).get('dailySignal') or {}
    selected = signal.get('selected') is True
    repeated = bool(prior.get('selected') is True and prior.get('firstReferenceTime')
                    and _number(prior.get('firstReferencePrice'), positive=True))
    reference = _number(candidate.get('referencePrice') or candidate.get('signalPrice'), positive=True)
    source = _source_time(candidate)
    quote_price = _number((candidate.get('quote') or {}).get('price'), positive=True)
    verified = bool(reference and quote_price and abs(reference - quote_price) <= .000001
                    and source and 0 <= (published_at - _at(source)).total_seconds() <= 30)
    return {**signal, 'publishedAt': published_at.isoformat(),
        'firstReferenceTime': prior['firstReferenceTime'] if repeated else published_at.isoformat() if selected else None,
        'firstReferencePrice': prior['firstReferencePrice'] if repeated else reference if selected else None,
        'currentSignalState': ('continued' if repeated else 'new') if selected else 'invalidated' if repeated else 'observation',
        'isRepeatedSignal': repeated, 'referencePrice': reference, 'referenceSourceTime': source,
        'referenceVerifiedAtPublication': verified, 'trainingEligibleAtSignal': False,
        'validationStatus': 'unvalidated_rules', 'probability': None,
        'reviewStatus': 'daily_observation_only'}


def _current_entry_eligible(candidate: Mapping[str, Any]) -> bool:
    if _is_daily_signal(candidate):
        return False
    from .local_algorithm import algorithm_contract, is_research_eligible
    # Training controls are defined before the learned admission decision. A
    # model rejecting an otherwise tradable candidate cannot erase its label.
    prior = {key: value for key, value in candidate.items() if key not in {'prediction', 'learningDecision'}}
    return bool(isinstance(candidate, Mapping)
                and candidate.get("algorithmContractId") == algorithm_contract()["contractId"]
                and is_research_eligible(prior))


def _historical_entry_eligibility(candidate, manifest=None):
    """Check compatible frozen controls without editing their original identity."""
    from .learning_contracts import compatibility, learning_contracts
    from .task_learning import task_contract
    audit = compatibility({**candidate, 'algorithmManifest': manifest})
    task = task_contract()
    labels_match = (candidate['learningDataContractId'] == learning_contracts(task)['learningDataContractId']
                    if candidate.get('learningDataContractId') else
                    not manifest or task['taskContractId'] in (manifest.get('learningTaskContracts') or []))
    # Eligibility and score-policy checks remain unchanged. Only a verified
    # compatible algorithm reference is normalized in this temporary copy.
    eligible = bool(audit['compatible'] and labels_match and _current_entry_eligible(
        {**candidate, 'algorithmContractId': audit['currentAlgorithmContractId']}))
    return eligible, audit


def _signal_preflight(candidate: Mapping[str, Any], published_at: datetime) -> bool:
    """Admit only signal-time usable examples; later no-fills stay in the sample."""
    if not _current_entry_eligible(candidate):
        return False
    from .primary_policy import evaluate_primary_admission
    if evaluate_primary_admission(candidate, published_at).get('eligible') is not True:
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
                CREATE TABLE IF NOT EXISTS morning_outcomes(
                    signal_id TEXT PRIMARY KEY REFERENCES signal_events(signal_id),
                    reviewed_at TEXT NOT NULL, result_json TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(signal_events)")}
            if "review_eligible" not in columns:
                db.execute("ALTER TABLE signal_events ADD COLUMN review_eligible INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE signal_events SET review_eligible=training_eligible")
            from .daily_observation_outcomes import init_schema
            init_schema(db)

    @scoped_lineage_validation
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
        from .learning_contracts import learning_algorithm_contract, frozen_learning_fields, learning_contracts
        from .task_learning import task_contract, NEXT_DAY_TASK_ID
        entry_contract_fields = frozen_learning_fields(task_contract(), contract)
        # Also freeze the contract of an empty (cash-only) official scan.
        scan_meta["algorithmContractId"] = (contract["contractId"]
                                             if scan_version == contract["scoreVersion"] else None)
        if scan_meta['algorithmContractId']:
            scan_meta['algorithmManifest'] = contract
            scan_meta['learningAlgorithmContractId'] = learning_algorithm_contract(contract)['contractId']
            scan_meta['entryDataContractId'] = entry_contract_fields['learningDataContractId']
        if (result.get('primarySelectionMode') in {'daily_conditional', 'daily_observation'}
                or records and all(_is_daily_signal(row) for row in records.values())):
            # A daily-only publication is not an observed minute-task session.
            scan_meta['algorithmContractId'] = None
        from .morning_task import MORNING_TASK_ID, MORNING_QUEUES, MORNING_VERSION, morning_queue, freeze_morning_signal
        from .task_learning import task_contract
        morning_slot = morning_queue(slot, published_at)
        if morning_slot and official and run_usable and scan_meta['algorithmContractId']:
            scan_meta['morningTaskContractId'] = task_contract(MORNING_TASK_ID, morning_slot)['taskContractId']
            scan_meta['morningDataContractId'] = learning_contracts(task_contract(MORNING_TASK_ID, morning_slot), contract)['learningDataContractId']
        registry = self._registry()
        forward_models = {}
        if official and run_usable and slot in ENTRY_SLOTS:
            for version in (registry.get("championVersion"), registry.get("shadowVersion"),
                            registry.get("rollbackVersion")):
                if version:
                    artifact = self._model_artifact(version)
                    if artifact:
                        forward_models[version] = artifact
            from .task_learning import TaskLearner, NEXT_DAY_TASK_ID
            specs = [(NEXT_DAY_TASK_ID, queue) for queue in ('first_board', 'continuation')]
            if morning_slot:
                specs.append((MORNING_TASK_ID, morning_slot))
            for task_id, queue in specs:
                task = TaskLearner(self, task_id, queue)
                task_registry = task.registry()
                for key in ('championVersion', 'shadowVersion', 'rollbackVersion'):
                    version = task_registry.get(key)
                    artifact = self._model_artifact(version) if version else None
                    if artifact:
                        forward_models[version] = artifact
        existing = False
        frozen_morning = {}
        frozen_daily = {}
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
                    daily = _is_daily_signal(item)
                    if not daily and item.get('algorithmContractId') == contract['contractId']:
                        item = {**item, **entry_contract_fields}
                    quality = ({'eligible': False, 'qualityKnownAt': None,
                        'checkedAt': published_at.isoformat(), 'immutable': True,
                        'reason': 'independent_daily_task_not_minute_training'} if daily
                        else self._learning_quality(item, published_at))
                    item = {**item,'learningDataEligibility':quality}
                    if item.get('nextDaySignal'):
                        next_signal = item['nextDaySignal']
                        next_contract = task_contract(NEXT_DAY_TASK_ID, next_signal.get('queue')) if next_signal.get('queue') in {'first_board', 'continuation'} else None
                        next_fields = (frozen_learning_fields(next_contract, contract) if next_contract
                            and next_signal.get('learningTaskContractId') == next_contract['taskContractId'] else {})
                        item['nextDaySignal'] = {**item['nextDaySignal'],
                            **next_fields,
                            'trainingEligibleAtSignal':bool(quality['eligible'] and item['nextDaySignal'].get('sourceUsableAtCompletion') is not False),
                            'qualityKnownAt':quality.get('qualityKnownAt')}
                    previous = None
                    if not daily and (item.get('morningSignal') or morning_slot):
                        previous = db.execute("SELECT snapshot_json FROM signal_events WHERE code=? "
                            "AND substr(decision_at,1,10)=? "
                            "AND json_extract(snapshot_json,'$.morningSignal.selected')=1 "
                            "ORDER BY json_extract(snapshot_json,'$.morningSignal.publishedAt'),signal_id LIMIT 1",
                            (code, day)).fetchone()
                    if previous and morning_slot and not item.get('morningSignal'):
                        # A formerly selected name can now fail before ranking.
                        # Preserve that invalidation, without manufacturing a
                        # qualified training signal or erasing its first price.
                        item['morningSignal'] = {'version': MORNING_VERSION,
                            'queue': morning_slot, 'learningTaskId': MORNING_TASK_ID,
                            'learningTaskContractId': task_contract(MORNING_TASK_ID, morning_slot)['taskContractId'],
                            'researchEligibleAtDecision': False, 'selectionEligible': False,
                            'learningStatus': 'data_rejected', 'upProbability': None,
                            'expectedPriceReturn': None, 'expectedMae': None, 'utility': None,
                            'ruleBaselineSelected': False, 'ruleBaselineRank': None}
                    if item.get('morningSignal'):
                        prior = json.loads(previous[0]) if previous else None
                        morning_quality = {**quality, 'eligible': bool(quality['eligible'] and _signal_preflight(item, published_at))}
                        item['morningSignal'] = freeze_morning_signal(item, published_at, slot, morning_quality,
                            selected=code in selected, official=official and run_usable,
                            previous=prior)
                        for key in ('firstReferenceTime', 'firstReferencePrice', 'currentSignalState'):
                            item[key] = item['morningSignal'][key]
                        frozen_morning[code] = {key: item[key] for key in
                            ('morningSignal', 'firstReferenceTime', 'firstReferencePrice', 'currentSignalState')}
                    if daily:
                        prior = db.execute("SELECT snapshot_json FROM signal_events WHERE code=? "
                            "AND substr(decision_at,1,10)=? "
                            "AND json_extract(snapshot_json,'$.dailySignal.selected')=1 "
                            "AND json_extract(snapshot_json,'$.dailySignal.taskId') IS ? "
                            "AND json_extract(snapshot_json,'$.dailySignal.version') IS ? "
                            "ORDER BY json_extract(snapshot_json,'$.dailySignal.publishedAt'),signal_id LIMIT 1",
                            (code, day, item['dailySignal'].get('taskId'), item['dailySignal'].get('version'))).fetchone()
                        item['dailySignal'] = _freeze_daily_signal(item, published_at,
                            previous=json.loads(prior[0]) if prior else None)
                        for key in ('firstReferenceTime', 'firstReferencePrice', 'currentSignalState'):
                            item[key] = item['dailySignal'][key]
                            item['daily' + key[0].upper() + key[1:]] = item['dailySignal'][key]
                        item.update(trainingEligible=False, training_eligible=False, minuteModelTrainingEligible=False)
                        frozen_daily[code] = {key: item[key] for key in (
                            'dailySignal', 'firstReferenceTime', 'firstReferencePrice', 'currentSignalState',
                            'dailyFirstReferenceTime', 'dailyFirstReferencePrice', 'dailyCurrentSignalState',
                            'trainingEligible', 'training_eligible', 'minuteModelTrainingEligible', 'learningDataEligibility')}
                        if item.get('nextDaySignal'):
                            frozen_daily[code]['nextDaySignal'] = item['nextDaySignal']
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
                        eligible = bool(not daily and code in execution_sources and official and run_usable and slot in ENTRY_SLOTS
                                        and _signal_preflight(item, published_at) and quality['eligible'])
                    signal_id = sha256(f"{run_id}:{code}".encode()).hexdigest()[:32]
                    snap = _snapshot(item)
                    db.execute(
                        "INSERT INTO signal_events(signal_id,run_id,code,name,selected,decision_at,source_time,"
                        "signal_price,training_eligible,review_eligible,features_json,snapshot_json,review_status) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (signal_id, run_id, code, str(item.get("name") or code), int(code in selected),
                         decision.isoformat(), _source_time(item),
                         _number(item.get("signalPrice") or item.get("referencePrice"), positive=True),
                         int(eligible), int(not daily and reviewable_run and code in execution_sources),
                         _json({} if daily else _feature_values(item)), _json(snap),
                         "daily_observation_only" if daily else "pending" if reviewable_run and code in execution_sources else "observation_only"),
                    )
                    if not daily and (eligible or official and run_usable and slot in ENTRY_SLOTS and (item.get('nextDaySignal') or item.get('morningSignal'))):
                        for version, artifact in forward_models.items():
                            try:
                                from .task_learning import ENTRY_TASK_ID, lineage_valid
                                is_entry = artifact.get('taskId') == ENTRY_TASK_ID
                                is_morning = artifact.get('taskId') == MORNING_TASK_ID
                                signal = item.get('morningSignal' if is_morning else 'nextDaySignal') or {}
                                if (is_entry and not eligible or not is_entry and
                                        (signal.get('learningTaskContractId') != artifact.get('taskContractId')
                                         or signal.get('trainingEligibleAtSignal') is not True
                                         or not lineage_valid(self, item.get('dataLineage')))):
                                    continue
                                score, _ = self._predict_artifact(
                                    artifact, [{"features": _feature_values(item) if is_entry or is_morning else self._next_features(item)}])
                                predicted_at = _at(self.clock())
                                if is_morning and predicted_at >= _at(signal['observationStartAt']):
                                    continue  # A shadow prediction must precede its outcome path.
                                db.execute("INSERT INTO forward_predictions VALUES(?,?,?,?)",
                                           (signal_id, version, float(score[0]), predicted_at.isoformat()))
                            except (ImportError, ValueError):
                                continue
                if frozen_morning or frozen_daily:
                    result = dict(result)
                    def frozen_row(row):
                        code = _code(row.get('code'))
                        if _is_daily_signal(row):
                            # Use the sanitized daily record, preserving local
                            # profile rank while excluding copied minute state.
                            clean = dict(row)
                            for key in ('morningSignal', 'algorithmContractId', 'learningTaskContractId',
                                        'prediction', 'learningDecision', 'dataLineage', 'v11Inputs', 'factorScores'):
                                clean.pop(key, None)
                            return {**clean, **frozen_daily.get(code, {})}
                        return {**row, **frozen_morning.get(code, {})}
                    for collection in ('candidates', 'controls', 'precisionResearch', 'morningResearch',
                                       'invalidatedCandidates', 'evidenceInsufficient', 'windvanes', 'dailyCandidates'):
                        if collection in result:
                            result[collection] = [frozen_row(row) for row in result[collection]]
                    for mapping in ('profileCandidates', 'dailyProfileCandidates'):
                        if isinstance(result.get(mapping), Mapping):
                            result[mapping] = {profile: [frozen_row(row) for row in candidates]
                                for profile, candidates in result[mapping].items()}
                    if frozen_morning:
                        result['invalidatedCandidates'] = [{**records[code], **frozen}
                            for code, frozen in frozen_morning.items() if frozen['currentSignalState'] == 'invalidated']
                    scan_meta['result'] = result
                    db.execute('UPDATE signal_runs SET scan_json=? WHERE run_id=?', (_json(scan_meta), run_id))
                from .daily_observation_outcomes import persist_scan as persist_daily_observations
                persist_daily_observations(db, result, run_id, day, published_at)
            else:
                saved = db.execute("SELECT scan_json FROM signal_runs WHERE run_id=?", (run_id,)).fetchone()
                canonical = json.loads(saved[0]).get("result") if saved else None
                if isinstance(canonical, Mapping):
                    result = canonical
                    selected, records, next_day, continuation, execution_sources = _scan_records(result)
            daily_observation_count = db.execute('SELECT COUNT(*) FROM daily_observation_appearances WHERE run_id=?',
                                                (run_id,)).fetchone()[0]
        output = dict(result)
        daily_selected = {code for code, row in records.items()
                          if _is_daily_signal(row) and row['dailySignal'].get('selected') is True}
        output["learningLedger"] = {
            "version": LEDGER_VERSION, "runId": run_id, "official": official,
            "idempotentReplay": existing, "selectedCount": len(selected),
            "nextDaySelectedCount": len(next_day), "continuationSelectedCount": len(continuation),
            "dailySelectedCount": len(daily_selected), "minuteSelectedCount": len(selected & execution_sources),
            "dailyObservationCount": daily_observation_count,
            "savedRecommendationCount": len(selected | next_day | daily_selected),
            "researchCount": len(records), "trainingEligible": bool(execution_sources and official and run_usable and slot in ENTRY_SLOTS),
            "reviewEligible": bool(execution_sources and reviewable_run),
        }
        return output

    def _learning_quality(self, candidate, published_at):
        from .task_learning import lineage_known_at
        known = lineage_known_at(self,candidate.get('dataLineage'))
        eligible = bool(known and _at(known) <= published_at)
        return {'eligible':eligible,'qualityKnownAt':known,'checkedAt':published_at.isoformat(),
                'reason': 'verified_at_publication' if eligible else 'lineage_not_verified_at_publication',
                'immutable':True}

    def _archive_bars(self, code: str, cutoff: datetime) -> list[dict[str, Any]]:
        """Read existing minute archive without constructing its mutating helper."""
        from .minute_history import read_archive_rows
        return read_archive_rows(self.data_dir / 'minute_history', code, cutoff)

    def review_codes(self, as_of: datetime | str) -> list[str]:
        return list(self.review_requirements(as_of))

    def daily_observation_reviews(self, *, as_of=None, limit=500, filters=None):
        """Independent reference observations; never promoted to training labels."""
        from .daily_observation_outcomes import reviews
        return reviews(self, as_of=as_of, limit=limit, filters=filters)

    def review_daily_observations(self, as_of, *, max_labels=300, budget_seconds=4.0):
        """Explicit local settlement, independent of slow executable reviews."""
        from .daily_observation_outcomes import review
        return review(self, as_of, max_labels=max_labels, budget_seconds=budget_seconds, retry_seconds=300)

    def daily_observation_status(self, as_of=None):
        """Read-only backlog and worker health; never starts settlement on GET."""
        from .daily_observation_queue import status
        return status(self, as_of)

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
            morning = {row['signal_id']: json.loads(row['result_json']).get('status')
                       for row in db.execute('SELECT signal_id,result_json FROM morning_outcomes')}
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
            morning_signal = json.loads(row['snapshot_json']).get('morningSignal') or {}
            if morning_signal.get('publishedAt') and morning.get(signal_id) != 'mature_price_observation':
                require(row['code'], _at(morning_signal['publishedAt']).date(), horizon=0)
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
        from .daily_observation_outcomes import review_requirements as daily_review_requirements
        for code, days in daily_review_requirements(self, timestamp).items():
            codes.setdefault(code, set()).update(days)
        return {code: sorted(days) for code, days in codes.items()}

    def history(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(filters or {})
        if filters.get("compact"):
            return self._compact_history(filters)
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
                        "dailySelected": bool(_primary_kinds(snapshot, bool(event['selected'])) == ['daily_condition']),
                        "recommendationKinds": _primary_kinds(snapshot, bool(event['selected'])) + (
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
                if filters.get("include_controls") is False:
                    items[-1]["signals"] = [item for item in signals if item["recommendationKinds"]]
        return {"items": items, "count": len(items)}

    def _compact_history(self, filters: Mapping[str, Any]) -> dict[str, Any]:
        """Read a bounded ledger page without decoding each full scan result.

        Older watch selections omitted from signal_events are projected from
        their frozen scan arrays. This remains a read: no historical eligibility
        or selection is reconstructed with today's algorithm.
        """
        limit = max(1, min(int(filters.get("limit") or 30), MAX_HISTORY))
        include_controls = filters.get("include_controls") is not False
        where, args = [], []
        for key, column in (("date", "r.trading_day"), ("version", "r.version")):
            if filters.get(key):
                where.append(column + "=?")
                args.append(str(filters[key]))
        code_filter = _code(filters.get("symbol")) if filters.get("symbol") else None
        if filters.get("symbol") and code_filter is None:
            return {"items": [], "count": 0, "compact": True}
        if code_filter:
            where.append("(EXISTS(SELECT 1 FROM signal_events x WHERE x.run_id=r.run_id AND x.code=?) "
                         "OR EXISTS(SELECT 1 FROM json_each(r.scan_json,'$.result.nextDayWatchlist') x "
                         "WHERE substr(json_extract(x.value,'$.code'),1,6)=?) "
                         "OR EXISTS(SELECT 1 FROM json_each(r.scan_json,'$.result.nextDayContinuationWatchlist') x "
                         "WHERE substr(json_extract(x.value,'$.code'),1,6)=?))")
            args.extend([code_filter] * 3)
        clause = " WHERE " + " AND ".join(where) if where else ""
        snapshot_sql = _json_projection("e.snapshot_json", _COMPACT_KEYS)
        frozen_sql = _json_projection("w.value", _COMPACT_KEYS)
        # Numeric vectors are small. For legacy whole-candidate vectors, select
        # just the fields from which the numeric signal-time vector is derived.
        feature_sql = ("CASE WHEN json_type(e.features_json,'$.v11Inputs')='object' "
                       "OR json_type(e.features_json,'$.factorScores')='object' THEN "
                       + _json_projection("e.features_json", (
                           "v11Inputs", "factorScores", "earlyScore", "mainRiseScore", "finalScore",
                           "distributionRisk", "dataConfidence")) + " ELSE e.features_json END")
        items = []
        with self._connect() as db:
            runs = db.execute(
                "SELECT r.run_id,r.slot,r.as_of,r.trading_day,r.official,r.version,r.status "
                "FROM signal_runs r" + clause + " ORDER BY r.as_of DESC LIMIT ?", (*args, limit)).fetchall()
            for run in runs:
                frozen_rows = db.execute(
                    "SELECT x.key queue," + frozen_sql + " payload FROM signal_runs r,"
                    "json_each(json_extract(r.scan_json,'$.result.nextDayWatchlist',"
                    "'$.result.nextDayContinuationWatchlist')) x,json_each(x.value) w WHERE r.run_id=?",
                    (run["run_id"],)).fetchall()
                frozen, next_day, continuation = {}, set(), set()
                for row in frozen_rows:
                    candidate = {key: value for key, value in json.loads(row["payload"]).items() if value is not None}
                    if code := _code(candidate.get("code")):
                        frozen[code] = candidate
                        (continuation if row["queue"] == 1 else next_day).add(code)
                event_where, event_args = ["e.run_id=?"], [run["run_id"]]
                if code_filter:
                    event_where.append("e.code=?")
                    event_args.append(code_filter)
                if not include_controls:
                    chosen = sorted(next_day | continuation)
                    watch_clause = " OR e.code IN (" + ",".join("?" for _ in chosen) + ")" if chosen else ""
                    event_where.append("(e.selected=1 OR json_extract(e.snapshot_json,'$.nextDaySignal.selected')=1"
                                       " OR json_extract(e.snapshot_json,'$.dailySignal.selected')=1"
                                       + watch_clause + ")")
                    event_args.extend(chosen)
                events = db.execute(
                    "SELECT e.signal_id,e.code,e.name,e.selected,e.decision_at,e.source_time,e.signal_price,"
                    "e.review_status,e.training_eligible,e.review_eligible," + snapshot_sql + " snapshot_json,"
                    + feature_sql + " features_json FROM signal_events e WHERE " + " AND ".join(event_where)
                    + " ORDER BY e.selected DESC,e.decision_at,e.code", event_args).fetchall()
                signals = []
                for event in events:
                    snapshot = {key: value for key, value in json.loads(event["snapshot_json"]).items() if value is not None}
                    if "nextDaySignal" not in snapshot and frozen.get(event["code"], {}).get("nextDaySignal"):
                        snapshot["nextDaySignal"] = frozen[event["code"]]["nextDaySignal"]
                    signal = snapshot.get("nextDaySignal") or {}
                    recorded = signal.get("selected") is True
                    watch = event["code"] in next_day or recorded and signal.get("queue") != "continuation"
                    follow = event["code"] in continuation or recorded and signal.get("queue") == "continuation"
                    if (watch or follow) and signal:
                        snapshot["nextDaySignal"] = {**signal, "selected": True}
                    signals.append({"signalId": event["signal_id"], "code": event["code"], "name": event["name"],
                        "selected": bool(event["selected"]), "decisionAt": event["decision_at"],
                        "nextDaySelected": bool(watch), "nextDayContinuationSelected": bool(follow),
                        "dailySelected": bool(_primary_kinds(snapshot, bool(event['selected'])) == ['daily_condition']),
                        "recommendationKinds": _primary_kinds(snapshot, bool(event['selected']))
                            + (["next_day_watch"] if watch else []) + (["next_day_continuation"] if follow else []),
                        "sourceTime": event["source_time"], "signalPrice": event["signal_price"],
                        "firstTradablePrice": None, "reviewStatus": event["review_status"],
                        "trainingEligible": bool(event["training_eligible"]), "reviewEligible": bool(event["review_eligible"]),
                        "features": _compact_features(json.loads(event["features_json"])),
                        "reason": snapshot.get("selectionReasons") or [], "snapshot": snapshot})
                recorded_codes = {row[0] for row in db.execute("SELECT code FROM signal_events WHERE run_id=?", (run["run_id"],))}
                for code in sorted((next_day | continuation) - recorded_codes):
                    if code_filter and code != code_filter:
                        continue
                    candidate = frozen[code]
                    signals.append({"signalId": sha256(f"{run['run_id']}:{code}".encode()).hexdigest()[:32],
                        "code": code, "name": str(candidate.get("name") or code), "selected": False,
                        "nextDaySelected": code in next_day, "nextDayContinuationSelected": code in continuation,
                        "recommendationKinds": (["next_day_watch"] if code in next_day else [])
                            + (["next_day_continuation"] if code in continuation else []),
                        "decisionAt": candidate.get("decision_at") or run["as_of"], "sourceTime": _source_time(candidate),
                        "signalPrice": _number(candidate.get("signalPrice") or candidate.get("referencePrice"), positive=True),
                        "firstTradablePrice": None, "reviewStatus": "observation_only", "trainingEligible": False,
                        "reviewEligible": False, "features": _feature_values(candidate),
                        "reason": candidate.get("selectionReasons") or [], "snapshot": candidate,
                        "ledgerRecordOrigin": "saved_scan_projection"})
                items.append({"runId": run["run_id"], "slot": run["slot"], "asOf": run["as_of"],
                    "tradingDay": run["trading_day"], "official": bool(run["official"]), "modelVersion": run["version"],
                    "status": run["status"], "signals": signals, "scan": {"status": run["status"],
                        "selectedCodes": [row["code"] for row in signals if row["selected"]],
                        "nextDaySelectedCodes": sorted(next_day), "nextDayContinuationSelectedCodes": sorted(continuation)}})
        return {"items": items, "count": len(items), "compact": True}

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
        return {"items": items, "count": len(items), "nextDayReview": read_next_day_reviews(self, filters),
                "morningReview": self.morning_reviews(filters),
                "dailyObservationReview": self.daily_observation_reviews(filters=filters, limit=limit)}

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
                        cached_bars: dict[str, list[dict[str, Any]]],
                        sessions: list[date] | None = None) -> dict[str, Any] | None:
        decision = max(_at(event["decision_at"]), _at(event["published_at"]))
        if sessions is None:
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
        from .entry_execution_policy import replay_entry_check
        entry_plan_issue = replay_entry_check(snapshot.get('entryExecution'), proposed, bar['end'])
        if entry_plan_issue:
            details['entryExecution'] = snapshot.get('entryExecution')
            return {**base, 'status': 'no_fill', 'fill_status': 'entry_plan_not_executable',
                    'reason': entry_plan_issue, 'details_json': _json(details)}
        cap = _number(snapshot.get('portfolioWeight'))
        cap = min(self.execution_config.position_weight, max(0., cap)) if cap is not None else self.execution_config.position_weight
        budget = self.execution_config.initial_cash * cap
        quantity, _ = _quantity_with_fees(event['code'], proposed, budget, decision.date(), self.execution_config)
        details.update(labelPositionCap=cap,labelQuantity=quantity,labelBudget=budget,
                       labelSizingPolicy='frozen_single_name_cap_including_buy_fee')
        base['details_json'] = _json(details)
        if _number(bar.get('volume_shares')) is None:
            return {**base,'status':'pending_data','reason':'entry_volume_unknown',
                    'fill_status':'unknown'}
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
        """Checkpoint each stock outside slow I/O; use one frozen calendar per pass."""
        timestamp = _at(as_of)
        # Commit this small independent checkpoint before minute/next-day I/O.
        # Their failure or interruption cannot prevent daily observations.
        daily_observations = self.review_daily_observations(timestamp)
        cache = _ReviewBarCache()
        changed: list[dict[str, Any]] = []
        waiting = calendar_gaps = 0
        outcomes = defaultdict(dict)
        with self._connect() as db:
            events = db.execute(
                "SELECT e.signal_id,e.code,e.name,e.selected,e.decision_at,e.source_time,e.signal_price,"
                "e.review_status," + _json_projection("e.snapshot_json", ("quote", "tradability", "portfolioWeight", "portfolioPolicy", "entryExecution"))
                + " snapshot_json,r.version,r.trading_day,r.created_at published_at "
                "FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
                "WHERE e.review_eligible=1 AND r.trading_day<=? ORDER BY e.selected DESC,e.code,r.trading_day,e.decision_at",
                (timestamp.date().isoformat(),),
            ).fetchall()
            for row in db.execute("SELECT signal_id,horizon,status,reason,details_json FROM signal_outcomes"):
                outcomes[row["signal_id"]][row["horizon"]] = row
        earliest = min((max(_at(row["decision_at"]), _at(row["published_at"])).date() for row in events),
                       default=timestamp.date())
        sessions = self._sessions(self.calendar_provider, earliest, timestamp.date())
        for event in events:
            results = []
            for horizon in HORIZONS:
                old = outcomes[event["signal_id"]].get(horizon)
                if old and old["status"] in {"mature", "no_fill", "unverified"} and not _needs_observation(old):
                    continue
                result = self._simulate_event(event, horizon, timestamp, cache, sessions)
                if result is None:
                    decision_day = max(_at(event["decision_at"]), _at(event["published_at"])).date()
                    if decision_day not in sessions:
                        calendar_gaps += 1
                    else:
                        waiting += 1
                    continue
                details = json.loads(result['details_json'])
                target_at = result.get('exit_at') or (details.get('targetDay') or '') + 'T15:01:00+08:00'
                if details.get('targetDay') or result.get('exit_at'):
                    details['dataLineage'] = self._label_lineage(event['code'], event['decision_at'], target_at,sessions=sessions)
                    result['details_json'] = _json(details)
                results.append((horizon, result))
            if not results:
                continue
            # The fetcher, calendar and simulation never run under a write lock.
            # Commit this event's horizons before moving to the next stock, so
            # interruption preserves the reviews already completed.
            with self._connect() as db:
                for horizon, result in results:
                    latest = db.execute("SELECT status,reason,details_json FROM signal_outcomes "
                                        "WHERE signal_id=? AND horizon=?", (event["signal_id"], horizon)).fetchone()
                    if latest and latest["status"] in {"mature", "no_fill", "unverified"} and not _needs_observation(latest):
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
                    changed.append(self._outcome_dict({**dict(event), **result, "horizon": horizon,
                                                      "reviewed_at": timestamp.isoformat()}))
                states = [row[0] for row in db.execute(
                    "SELECT status FROM signal_outcomes WHERE signal_id=? ORDER BY horizon", (event["signal_id"],))]
                review_status = ("mature" if len(states) == 3 and all(state == "mature" for state in states)
                                 else "incomplete" if any(state in {"no_fill", "unverified"} for state in states)
                                 else "pending")
                db.execute("UPDATE signal_events SET review_status=? WHERE signal_id=?",
                           (review_status, event["signal_id"]))
        from .next_day_watch import review_next_day_signals
        next_day = review_next_day_signals(self, timestamp, cache)
        from .morning_task import review_morning_signals
        morning = review_morning_signals(self, timestamp, cache)
        return {"items": changed, "updated": len(changed), "asOf": timestamp.isoformat(),
                "dailyObservations": daily_observations,
                "nextDayReview": next_day, "morningReview": morning, "statusCounts": dict(Counter(row["status"] for row in changed)),
                "waitingHorizons": waiting, "calendarUnavailableHorizons": calendar_gaps,
                "scope": "按交易日历分别结算T+1/T+3/T+5；缺数据保持待补，无法成交保持原拒绝；不改变历史训练资格"}

    def _dataset(self, as_of: datetime, *, dedupe: bool = True) -> list[dict[str, Any]]:
        """Primary labels are T+1; unrelated T+3/T+5 maturity cannot delay them."""
        from .local_algorithm import algorithm_contract
        with self._connect() as db:
            rows = db.execute(
                "SELECT e.*,r.trading_day,r.slot,r.version scan_version,o.status outcome_status,"
                "o.simulated_net_return net,o.mfe mfe,o.mae mae,o.exit_at exit_at,"
                "o.entry_at entry_at,o.entry_price entry_price,o.exit_price exit_price,"
                "o.details_json details_json,o.reviewed_at label_known_at,"
                "COALESCE(json_extract(r.scan_json,'$.algorithmManifest'),json_extract(r.scan_json,'$.result.algorithm')) manifest_json "
                "FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
                "JOIN signal_outcomes o ON o.signal_id=e.signal_id AND o.horizon=1 "
                "WHERE e.training_eligible=1 AND r.trading_day<=? "
                "ORDER BY r.trading_day,e.code,e.decision_at,e.selected DESC",
                (as_of.date().isoformat(),)).fetchall()
        contract = algorithm_contract()
        result, seen = [], set()
        for row in rows:
            snapshot = json.loads(row['snapshot_json'])
            eligible, compatibility_audit = _historical_entry_eligibility(snapshot,
                json.loads(row['manifest_json']) if row['manifest_json'] else None)
            if row['scan_version'] != contract['scoreVersion'] or not eligible:
                continue
            key = (row['trading_day'], row['code'])
            if dedupe and key in seen:
                continue
            if row['outcome_status'] not in {'mature', 'no_fill'}:
                continue
            details = json.loads(row['details_json'])
            filled = row['outcome_status'] == 'mature'
            if filled and (row['net'] is None or not details.get('dailyCloseMarks')):
                continue
            target = details.get('targetDay')
            label_end = row['exit_at'] or (datetime.combine(date.fromisoformat(target), time(15,1), TZ).isoformat() if target else None)
            if not label_end or _at(label_end) > as_of or _at(row['label_known_at']) > as_of:
                continue
            features = json.loads(row['features_json'])
            if not features:
                continue
            seen.add(key)
            result.append({'day':row['trading_day'],'code':row['code'],'runId':row['run_id'],
                'signalId':row['signal_id'],'features':features,'net':float(row['net']) if filled else 0.,
                'filled':filled,'mfe':float(row['mfe'] or 0),'mae':float(row['mae'] or 0),
                'exitAt':row['exit_at'],'entryAt':row['entry_at'],'entryPrice':row['entry_price'],
                'exitPrice':row['exit_price'],'marks':details.get('dailyCloseMarks') or {},
                'entryMinuteVolume':details.get('entryMinuteVolume'),'exitMinuteVolume':details.get('exitMinuteVolume'),
                'labelEndAt':label_end,'labelKnownAt':row['label_known_at'],
                'deferredExitSessions':details.get('deferredExitSessions',0),'selected':bool(row['selected']),
                'decisionAt':row['decision_at'],'slot':row['slot'],'algorithmContractId':snapshot.get('algorithmContractId'),
                'contractCompatibility':compatibility_audit,
                'entryEligibleAtSignal':True,'selectionRank':_number(snapshot.get('rank'),positive=True),
                'recordedRankScore':_number(snapshot.get('learningRankScore')),
                'rankingTieBreakOrder':_number(snapshot.get('rankingTieBreakOrder')),
                'dataLineage':snapshot.get('dataLineage'),'labelDataLineage':details.get('dataLineage'),
                'portfolioWeight':snapshot.get('portfolioWeight'),'portfolioPolicy':snapshot.get('portfolioPolicy'),
                'portfolioWeightKind':snapshot.get('portfolioWeightKind')})
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

    @scoped_lineage_validation
    def learning_state(self, as_of: datetime | str | None = None) -> dict[str, Any]:
        from .task_learning import TaskLearner, NEXT_DAY_TASK_ID, MORNING_TASK_ID, MORNING_QUEUES
        now = _at(as_of or self.clock())
        states = [TaskLearner(self).state(now)] + [TaskLearner(self, NEXT_DAY_TASK_ID, queue).state(now)
                    for queue in ('first_board', 'continuation')] + [TaskLearner(self, MORNING_TASK_ID, queue).state(now)
                    for queue in MORNING_QUEUES]
        return {**states[0], 'taskStates': states}

    @staticmethod
    def _matrix(samples: list[dict[str, Any]], features: list[str], medians: list[float]) -> np.ndarray:
        return np.asarray([[float(sample["features"].get(name, medians[index]))
                            for index, name in enumerate(features)] for sample in samples], dtype=float)

    @staticmethod
    def _account_nav(samples: list[dict[str, Any]], scores: np.ndarray | None,
                     config: AccountConfig, calendar_days: list[str] | None = None) -> dict[str, Any]:
        """A daily marked account with five simultaneous positions and actual cash use.

        A scan may allocate at most five signals. The official selected set is the
        current rule/champion comparator; model alternatives rank the same scan
        queue. A no-fill leaves its allocation in cash. A blocked T+1 sell keeps
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
                        if scores is None else [(score, item) for score, item in group if score > 0])
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
                choices = sorted(pool, key=selection_order)
                allocated = 0
                unfilled_reserved = 0.
                unfilled_sectors = defaultdict(float)
                for _, item in choices:
                    if allocated >= slots:
                        break
                    key = (day, item["code"])
                    if key in traded_codes or any(position["code"] == item["code"] for position in positions):
                        continue
                    if item['filled'] and (not item["entryAt"] or not item["exitAt"] or item["entryAt"][:10] != day
                            or item["exitAt"][:10] not in item["marks"]):
                        return {"valid": False, "reason": "entry_exit_timestamp_missing"}
                    policy = item.get('portfolioPolicy') or {}
                    cap = _number(item.get('portfolioWeight'))
                    cap = min(config.position_weight, cap) if cap is not None else config.position_weight
                    total_cap = _number(policy.get('totalExposureCap'))
                    industry_cap = _number(policy.get('maxIndustryWeight'))
                    industry = policy.get('industry') or '__unknown__'
                    # Do not use today's future close to size an intraday order.
                    # Value existing holdings at their latest prior-day close or
                    # entry proxy and preserve them across every scan in the day.
                    def committed(position):
                        prior = [key for key in position['marks'] if key < day]
                        mark = position['marks'][max(prior)] if prior else position['entryPrice']
                        return position['quantity'] * mark
                    exposure = sum(committed(position) for position in positions)
                    sector_exposure = sum(committed(position) for position in positions
                                          if position.get('industry') == industry)
                    allocation = min(cash, previous_nav * cap,
                        max(0., previous_nav * (total_cap if total_cap is not None else 1.)-exposure-unfilled_reserved),
                        max(0., previous_nav * (industry_cap if industry_cap is not None else 1.)-sector_exposure-unfilled_sectors[industry]))
                    if allocation <= 0:
                        continue
                    # Allocate before examining the outcome: an evidenced
                    # no-fill consumes this scan's planned slot and leaves cash.
                    if not item['filled']:
                        allocated += 1
                        attempts += 1
                        traded_codes.add(key)
                        unfilled_reserved += allocation
                        unfilled_sectors[industry] += allocation
                        continue
                    entry_price = _number(item["entryPrice"], positive=True)
                    exit_price = _number(item["exitPrice"], positive=True)
                    if entry_price is None or exit_price is None:
                        return {"valid": False, "reason": "entry_exit_price_missing"}
                    quantity, fee = _quantity_with_fees(item['code'],entry_price,allocation,date.fromisoformat(day),config)
                    if quantity <= 0 or quantity * entry_price + fee > cash:
                        continue
                    allocated += 1
                    attempts += 1
                    traded_codes.add(key)
                    volume = _number(item.get("entryMinuteVolume"), positive=True)
                    if volume is None or quantity > volume * 0.1:
                        return {"valid": False, "reason": "entry_participation_evidence_missing_or_exceeded"}
                    exit_volume = _number(item.get("exitMinuteVolume"), positive=True)
                    if exit_volume is None or quantity > exit_volume * .1:
                        return {"valid": False, "reason": "exit_participation_evidence_missing_or_exceeded"}
                    cash -= quantity * entry_price + fee
                    positions.append({"code": item["code"], "quantity": quantity,
                                      "exitAt": item["exitAt"], "exitPrice": exit_price,
                                      "marks": item["marks"], "buyCost": quantity * entry_price + fee,
                                      "industry": industry, "entryPrice": entry_price})
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
        sampled_baseline = np.prod(1 + extra['daily'][indices], axis=1) - 1
        net_lower = float(np.quantile(sampled_net, 0.05))
        advantage_lower = float(np.quantile(sampled_net - sampled_current, 0.05))
        baseline_advantage_lower = float(np.quantile(sampled_net - sampled_baseline, 0.05))
        drawdown_ok = candidate["maxDrawdown"] <= current["maxDrawdown"] + 1e-12
        passed = net_lower > 0 and advantage_lower > 0 and baseline_advantage_lower > 0 and drawdown_ok
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
            "ridgeLogisticAdvantage95Lower": baseline_advantage_lower,
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
        from .task_learning import predict_model
        result = predict_model(artifact, samples)
        return result['rank'], result['baselineRank']

    def _model_artifact(self, version: str, *, active_only: bool = False) -> dict[str, Any] | None:
        from .task_learning import load_artifact
        return load_artifact(self, version, active_only)

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
                "SELECT trading_day,created_at,slot,status,json_extract(scan_json,'$.algorithmContractId') contract_id,"
                "json_extract(scan_json,'$.entryDataContractId') data_contract_id,"
                "COALESCE(json_extract(scan_json,'$.algorithmManifest'),json_extract(scan_json,'$.result.algorithm')) manifest_json FROM signal_runs "
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
                from .learning_contracts import compatibility, run_task_data_compatible
                from .task_learning import task_contract
                source_contract = {'algorithmContractId': row['contract_id'], 'learningDataContractId': row['data_contract_id'],
                    'algorithmManifest': json.loads(row['manifest_json']) if row['manifest_json'] else None}
                if (compatibility(source_contract, contract)['compatible']
                        and run_task_data_compatible(source_contract, task_contract(), contract)):
                    recorded.add(row["trading_day"])
            except (ValueError, TypeError):
                continue
        if not recorded:
            return []
        sessions = self._sessions(self.calendar_provider, date.fromisoformat(min(recorded)), as_of.date())
        result = []
        for index, day in enumerate(sessions):
            if (day.isoformat() in recorded and index + 1 < len(sessions)
                    and datetime.combine(sessions[index + 1], time(15, 1), TZ) <= as_of):
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
                "SELECT e.signal_id,e.snapshot_json,r.version,COALESCE(json_extract(r.scan_json,'$.algorithmManifest'),"
                "json_extract(r.scan_json,'$.result.algorithm')) manifest_json FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id "
                f"WHERE e.training_eligible=1 AND r.trading_day IN ({placeholders})",
                tuple(sorted(days)),
            ).fetchall()
            runs = db.execute(
                f"SELECT trading_day,json_extract(scan_json,'$.algorithmContractId') contract_id,version,"
                f"json_extract(scan_json,'$.entryDataContractId') data_contract_id,"
                f"COALESCE(json_extract(scan_json,'$.algorithmManifest'),json_extract(scan_json,'$.result.algorithm')) manifest_json FROM signal_runs WHERE official=1 AND trading_day IN ({placeholders})",
                tuple(sorted(days))).fetchall()
        from .local_algorithm import algorithm_contract
        from .learning_contracts import compatibility, run_task_data_compatible
        from .task_learning import task_contract
        contract = algorithm_contract()
        recorded_days = set()
        for run in runs:
            try:
                source_contract = {'algorithmContractId': run['contract_id'], 'learningDataContractId': run['data_contract_id'],
                    'algorithmManifest': json.loads(run['manifest_json']) if run['manifest_json'] else None}
                if (run["version"] == contract["scoreVersion"]
                        and compatibility(source_contract, contract)['compatible']
                        and run_task_data_compatible(source_contract, task_contract(), contract)):
                    recorded_days.add(run["trading_day"])
            except (TypeError, ValueError):
                continue
        expected = set()
        for row in rows:
            try:
                eligible, _ = _historical_entry_eligibility(json.loads(row['snapshot_json']),
                    json.loads(row['manifest_json']) if row['manifest_json'] else None)
                if row['version'] == contract["scoreVersion"] and eligible:
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
            if sample.get('observationStartAt') and not (
                    _at(sample['publishedAt']) <= created < _at(sample['observationStartAt'])):
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

    @scoped_lineage_validation
    def train_and_evaluate(self, as_of: datetime | str | None = None, *, task_id=None, queue=None) -> dict[str, Any]:
        from .task_learning import TaskLearner, NEXT_DAY_TASK_ID, MORNING_TASK_ID, MORNING_QUEUES
        now = _at(as_of or self.clock())
        if task_id is not None:
            return TaskLearner(self, task_id, queue).train(now)
        states = [TaskLearner(self).train(now)] + [TaskLearner(self, NEXT_DAY_TASK_ID, item).train(now)
                  for item in ('first_board', 'continuation')] + [TaskLearner(self, MORNING_TASK_ID, item).train(now)
                  for item in MORNING_QUEUES]
        return {**states[0], 'taskStates': states}

    @scoped_lineage_validation
    def rank_candidates(self, candidates: Iterable[Mapping[str, Any]],
                        as_of: datetime | str | None = None, *, task_id=None, queue=None) -> list[dict[str, Any]]:
        from .task_learning import TaskLearner, ENTRY_TASK_ID, NEXT_DAY_TASK_ID, MORNING_TASK_ID, predict_model, lineage_valid, TARGET_HEAD_FAILURE_REASONS
        if task_id == NEXT_DAY_TASK_ID:
            return self.rank_next_day_candidates(candidates, as_of)
        if task_id == MORNING_TASK_ID:
            return self.rank_morning_candidates(candidates, as_of, slot=queue or 'live')
        learner = TaskLearner(self, task_id or ENTRY_TASK_ID, queue)
        artifact = learner.artifact(learner.registry())
        rows = [dict(row) for row in candidates]
        for index, row in enumerate(rows):
            row['rankingTieBreakOrder'] = index
            row['learningTaskContractId'] = learner.contract['taskContractId']
            tainted = (row.get('dataLineage') or {}).get('status') == 'tainted'
            row['learningRankVersion'] = 'rules_v1.2'
            row['prediction'] = {'status':'data_rejected' if tainted else 'rules_unvalidated',
                'taskId':learner.task_id,'expectedNetReturn':None,'bigMoveProbability':None,
                'winProbability':None,'targetReturnProbability':None,
                'lowerReturnQuantile':None,'utility':None,'entryEligible':False if tainted else None,
                'modelVersion':None,
                'probabilityMeaning':'T+1模拟扣费后净盈利概率；包含已核实未成交的零收益，不代表保证成交',
                'winProbabilityMeaning':learner.contract.get('winProbabilityMeaning'),
                'targetReturnProbabilityMeaning':learner.contract.get('targetReturnProbabilityMeaning'),
                'bigMoveProbabilityMeaning':'T+1模拟扣费后收益至少5%的概率；不是净盈利胜率'}
        eligible = [row for row in rows if _current_entry_eligible(row)]
        if artifact and eligible:
            usable = [row for row in eligible if lineage_valid(self,row.get('dataLineage'))]
            for row in eligible:
                if row not in usable:
                    row['prediction'].update(status='data_rejected',entryEligible=False)
            if usable:
                try:
                    predictions = predict_model(artifact,[{'features':_feature_values(row)} for row in usable])
                    for index,row in enumerate(usable):
                        target_available = bool(artifact.get('targetReturnProbabilityValidated')
                            and predictions.get('targetReturnProbabilityAvailable', [False]*len(usable))[index])
                        target_probability = float(predictions['targetReturnProbability'][index]) if target_available else None
                        target_failure = int(predictions.get('targetReturnProbabilityFailureCode', [0]*len(usable))[index])
                        target_status = ('optional_prediction_unavailable' if target_failure > 1
                            or (artifact.get('targetReturnProbabilityValidated') and not target_available) else
                            artifact.get('targetReturnModelStatus', 'auxiliary_head_unavailable'))
                        row['learningRankVersion'] = artifact['version']
                        row['learningRankScore'] = float(predictions['rank'][index])
                        row['prediction'].update(status='active',modelVersion=artifact['version'],
                            expectedNetReturn=float(predictions['net'][index]),
                            bigMoveProbability=target_probability,
                            winProbability=float(predictions['winProbability'][index]),
                            targetReturnProbability=target_probability,
                            targetReturnProbabilityStatus=target_status,
                            targetReturnProbabilityFailureCode=target_failure,
                            targetReturnProbabilityFailureReason=TARGET_HEAD_FAILURE_REASONS.get(target_failure),
                            lowerReturnQuantile=float(predictions['tail'][index]),
                            utility=float(predictions['utility'][index]),entryEligible=bool(predictions['eligible'][index]))
                except (ValueError,KeyError,ImportError):
                    for row in usable:
                        row['prediction'].update(status='prediction_failed',entryEligible=False)
        for row in rows:
            row['learningDecision'] = {'selectionEligible':row['prediction']['entryEligible'],**row['prediction']}
        eligible_ids = {id(row) for row in eligible}
        def rank_value(row):
            value = _number(row.get('learningRankScore'))
            if value is None:
                value = _number(row.get('finalScore'))
            return value if value is not None else -1e9
        return sorted(rows,key=lambda row:(id(row) not in eligible_ids,row['prediction']['entryEligible'] is False,
            -rank_value(row)))

    @scoped_lineage_validation
    def rank_morning_candidates(self, candidates, as_of=None, *, slot='live'):
        """Independent T0 admission; cold-start remains research, never main picks.

        Preserve qualified controls irrespective of this model's prediction. A
        frozen rule top-five is a comparator, explicitly not a recommendation.
        """
        from .task_learning import TaskLearner, MORNING_TASK_ID, predict_model, lineage_valid
        from .morning_task import MORNING_VERSION, morning_queue, morning_signal_state
        now = _at(as_of or self.clock())
        queue = morning_queue(slot, now)
        rows = [dict(row) for row in candidates]
        if queue is None:
            prior = self._morning_first_references(now)
            for row in rows:
                state = morning_signal_state(row, prior.get(str(row.get('code'))), selected=False)
                row['morningSignal'] = {'version': MORNING_VERSION, 'queue': None,
                    'actualSlot': str(slot), 'learningTaskId': MORNING_TASK_ID,
                    'learningTaskContractId': None, 'selectionEligible': False,
                    'researchEligibleAtDecision': False, 'learningStatus': 'morning_window_closed',
                    'learningRankVersion': None, 'ruleBaselineSelected': False,
                    'upProbability': None, 'expectedPriceReturn': None, 'expectedMae': None,
                    'returnQ10': None, 'utility': None, 'selected': False,
                    'reason': 'completion_outside_morning_session', **state}
                row.update({key: state[key] for key in ('firstReferenceTime', 'firstReferencePrice', 'currentSignalState')})
            return rows
        learner = TaskLearner(self, MORNING_TASK_ID, queue)
        artifact = learner.artifact(learner.registry())
        prior = self._morning_first_references(now)
        qualified = []
        for index, row in enumerate(rows):
            admission = row.get('primaryAdmission')
            admitted = (admission is None or admission.get('researchEligible', admission.get('eligible')) is True)
            research = bool(admitted and _signal_preflight(row, now)
                            and self._learning_quality(row, now).get('eligible') is True)
            row['morningSignal'] = {'version': MORNING_VERSION, 'queue': queue,
                'actualSlot': str(slot), 'learningTaskId': MORNING_TASK_ID,
                'learningTaskContractId': learner.contract['taskContractId'],
                'researchEligibleAtDecision': research, 'learningStatus': 'rules_unvalidated' if research else 'data_rejected',
                'learningRankVersion': 'morning_rules_unvalidated', 'selectionEligible': False,
                'expectedPriceReturn': None, 'upProbability': None, 'returnQ10': None,
                'expectedMae': None, 'utility': None, 'ruleScore': _number(row.get('finalScore')),
                'ruleBaselineSelected': False, 'ruleBaselineRank': None, 'selected': False,
                'rankMeaning': '上午发布后至当日收盘的独立模型；未验证时不发布正式推荐',
                'probabilityMeaning': '发布时冻结的推荐参考价至当日收盘上涨概率；非成交盈利概率',
                'T1RiskRequiredSeparately': True, 'executionEligible': False,
                'tieBreakOrder': index}
            if research:
                qualified.append(row)
        rule_order = sorted(qualified, key=lambda row: (-(_number(row.get('finalScore')) or 0),
                                                       row['morningSignal']['tieBreakOrder']))
        for rank, row in enumerate(rule_order, 1):
            row['morningSignal'].update(ruleBaselineRank=rank, ruleBaselineSelected=rank <= 5)
        if artifact and qualified:
            usable = [row for row in qualified if lineage_valid(self, row.get('dataLineage'))]
            for row in qualified:
                if row not in usable:
                    row['morningSignal'].update(learningStatus='data_rejected', selectionEligible=False)
            if usable:
                try:
                    prediction = predict_model(artifact, [{'features': _feature_values(row)} for row in usable])
                    for index, row in enumerate(usable):
                        row['morningSignal'].update(learningStatus='active', learningRankVersion=artifact['version'],
                            learningRankScore=float(prediction['rank'][index]),
                            expectedPriceReturn=float(prediction['net'][index]),
                            upProbability=float(prediction['probability'][index]), returnQ10=float(prediction['tail'][index]),
                            expectedMae=float(prediction['expectedMae'][index]), utility=float(prediction['utility'][index]),
                            selectionEligible=bool(prediction['eligible'][index]))
                except (ValueError, KeyError, ImportError):
                    for row in usable:
                        row['morningSignal'].update(learningStatus='prediction_failed', selectionEligible=False)
        for row in rows:
            previous = prior.get(str(row.get('code')))
            state = morning_signal_state(row, previous,
                                         selected=row['morningSignal']['selectionEligible'] is True)
            if not previous:
                # Ranking has not chosen the final list. Publication owns the
                # first actual reference; prospective candidates have none yet.
                state.update(firstReferenceTime=None, firstReferencePrice=None)
            row.update({key: state[key] for key in ('firstReferenceTime', 'firstReferencePrice', 'currentSignalState')})
            row['morningSignal'].update(state)
        return sorted(rows, key=lambda row: (row['morningSignal']['selectionEligible'] is not True,
            -(row['morningSignal'].get('learningRankScore') or 0),
            row['morningSignal'].get('ruleBaselineRank') or math.inf,
            row['morningSignal']['tieBreakOrder']))

    def _morning_first_references(self, as_of):
        with self._connect() as db:
            rows = db.execute("SELECT code,snapshot_json FROM signal_events WHERE substr(decision_at,1,10)=? "
                "AND json_extract(snapshot_json,'$.morningSignal.selected')=1 "
                "AND json_extract(snapshot_json,'$.morningSignal.publishedAt')<=? "
                "ORDER BY json_extract(snapshot_json,'$.morningSignal.publishedAt'),signal_id",
                (as_of.date().isoformat(), as_of.isoformat())).fetchall()
        first = {}
        for row in rows:
            first.setdefault(row['code'], json.loads(row['snapshot_json']))
        return first

    def morning_reviews(self, filters=None):
        from .morning_task import MORNING_VERSION, summarize_morning_records
        filters = dict(filters or {})
        where, args = ["json_extract(e.snapshot_json,'$.morningSignal.version')=?"], [MORNING_VERSION]
        if filters.get('date'):
            where.append('substr(e.decision_at,1,10)=?')
            args.append(str(filters['date']))
        if filters.get('symbol'):
            where.append('e.code=?')
            args.append(_code(filters['symbol']) or '')
        with self._connect() as db:
            rows = db.execute("SELECT e.signal_id,e.code,e.decision_at,e.snapshot_json,o.result_json,o.reviewed_at "
                "FROM signal_events e LEFT JOIN morning_outcomes o ON o.signal_id=e.signal_id WHERE "
                + ' AND '.join(where) + ' ORDER BY e.decision_at,e.code', args).fetchall()
        items = []
        for row in rows:
            signal = json.loads(row['snapshot_json'])['morningSignal']
            result = json.loads(row['result_json']) if row['result_json'] else {'status': 'pending'}
            items.append({**signal, **result, 'signalId': row['signal_id'], 'code': row['code'],
                          'decisionAt': row['decision_at'], 'reviewedAt': row['reviewed_at']})
        return {'items': items, 'count': len(items), 'summary': summarize_morning_records(items)}

    @scoped_lineage_validation
    def rank_next_day_candidates(self, candidates, as_of=None):
        from .task_learning import TaskLearner, NEXT_DAY_TASK_ID, predict_model, lineage_valid
        rows = [{**row,'nextDaySignal':dict(row.get('nextDaySignal') or {})} for row in candidates]
        for queue in ('first_board','continuation'):
            learner = TaskLearner(self,NEXT_DAY_TASK_ID,queue)
            artifact = learner.artifact(learner.registry())
            group = [row for row in rows if row['nextDaySignal'].get('queue') == queue]
            for row in group:
                signal = row['nextDaySignal']
                tainted = (row.get('dataLineage') or {}).get('status') == 'tainted'
                signal.update(learningTaskId=NEXT_DAY_TASK_ID,learningTaskContractId=learner.contract['taskContractId'],
                    learningRankVersion='next_day_rules_unvalidated',learningStatus='data_rejected' if tainted else 'rules_unvalidated',
                    learningRankScore=signal.get('score'),probability=None,expectedPriceReturn=None,returnQ10=None,
                    selectionEligible=not tainted,selected=False,
                    probabilityMeaning='下一交易日收盘10%价格模式概率；非成交收益，非交易所已核实涨停')
            if artifact and group:
                usable = [row for row in group if lineage_valid(self,row.get('dataLineage'))]
                for row in group:
                    if row not in usable:
                        row['nextDaySignal'].update(learningStatus='data_rejected',selectionEligible=False)
                if usable:
                    try:
                        prediction = predict_model(artifact,[{'features':self._next_features(row)} for row in usable])
                        for index,row in enumerate(usable):
                            row['nextDaySignal'].update(learningStatus='active',learningRankVersion=artifact['version'],
                                learningRankScore=float(prediction['rank'][index]),probability=float(prediction['probability'][index]),
                                expectedPriceReturn=float(prediction['net'][index]),returnQ10=float(prediction['tail'][index]),
                                selectionEligible=bool(prediction['eligible'][index]),utility=float(prediction['utility'][index]))
                    except (ValueError,KeyError,ImportError):
                        for row in usable:
                            row['nextDaySignal'].update(learningStatus='prediction_failed',selectionEligible=False)
        return sorted(rows,key=lambda row:(row['nextDaySignal'].get('queue',''),
            row['nextDaySignal'].get('selectionEligible') is False,-float(row['nextDaySignal'].get('learningRankScore') or 0),row.get('code','')))

    @staticmethod
    def _next_features(candidate):
        signal = candidate.get('nextDaySignal') or {}
        features = _feature_values(candidate)
        features.update({'next_'+key:float(value) for key,value in (signal.get('features') or {}).items()
                         if _number(value) is not None})
        features['ruleScore'] = _number(signal.get('score')) or 0.
        return features

    def _label_lineage(self, code, decision_at, target_at, *, sessions=None):
        from .minute_history import read_archive_lineage
        start,end = _at(decision_at).date(),_at(target_at).date()
        days = ([day for day in sessions if start <= day <= end] if sessions is not None
                else self._sessions(self.calendar_provider,start,end))
        return read_archive_lineage(self.data_dir/'minute_history',code=code,days=[day.isoformat() for day in days],cutoff=target_at)
