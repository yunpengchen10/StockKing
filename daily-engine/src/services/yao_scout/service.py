"""Auditable Stock King yao-scout orchestration service."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from src.config import Config
from src.core.trading_calendar import is_market_open
from src.notification import NotificationService
from src.services.screening.candidate_context import collect_candidate_context
from src.services.screening.config import Config as ScreeningConfig
from src.services.screening.daily import enrich_daily_features, fetch_daily_history
from src.services.screening.snapshot import (
    fetch_snapshot_with_fallback,
    snapshot_source_health_snapshot,
)
from src.storage import DatabaseManager

from .historical import CASE_STUDIES, append_validation_results, write_historical_report
from .labels import evaluate_outcome_labels
from .model import predict_probabilities, train_challenger

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MODEL_VERSION = "yao-audit-v1"
SUPPORTED_MODES = {"preopen", "postclose", "intraday"}
POSITIVE_EVENT_WORDS = ("控制权", "中标", "订单", "合作", "回购", "增持", "扭亏", "预增", "数字人民币", "政策")
NEGATIVE_EVENT_WORDS = ("减持", "预亏", "亏损", "立案", "调查", "处罚", "问询", "退市", "冻结")
_CONTEXT_TIMESTAMP_RE = re.compile(
    r"^\s*(?P<date>\d{4}-\d{2}-\d{2})(?:\s+(?P<time>\d{2}:\d{2}(?::\d{2})?))?\s+"
)


class YaoScoutService:
    """Build, persist and notify high-risk A-share research watchlists."""

    def __init__(
        self,
        *,
        config: Config,
        db_manager: DatabaseManager,
        data_dir: Path | None = None,
        snapshot_fetcher: Callable[..., pd.DataFrame] | None = None,
        history_fetcher: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        self.config = config
        self.db = db_manager
        self.screening = ScreeningConfig.from_env()
        self.data_dir = Path(data_dir or os.getenv("YAO_SCOUT_DATA_DIR") or "data/yao_scout")
        self.report_dir = self.data_dir / "reports"
        self.prefetch_dir = self.data_dir / "prefetch"
        self.backfill_dir = self.data_dir / "backfill"
        self.model_dir = self.data_dir / "models"
        self.context_cache_dir = self.data_dir / "candidate_context"
        self.snapshot_fetcher = snapshot_fetcher or fetch_snapshot_with_fallback
        self.history_fetcher = history_fetcher or fetch_daily_history
        for path in (self.report_dir, self.prefetch_dir, self.backfill_dir, self.model_dir, self.context_cache_dir):
            path.mkdir(parents=True, exist_ok=True)

    def prefetch(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        """Fetch the immutable preopen snapshot/evidence pack before the 08:55 cutoff."""
        local_now = _local_datetime(as_of)
        if not _is_cn_trading_day(local_now.date()):
            return {"status": "skipped_non_trading_day", "as_of": local_now.isoformat()}
        snapshot = self._fetch_snapshot()
        filtered = self._filter_universe(snapshot)
        preliminary = self._preliminary_rank(filtered).head(30)
        contexts, context_errors = collect_candidate_context(
            preliminary,
            max_rows=30,
            providers=["news", "announcement", "fund_flow", "quote"],
            news_limit=3,
            announcement_limit=3,
            cache_dir=self.context_cache_dir,
            cache_ttl_hours=1,
        )
        payload = {
            "created_at": local_now.isoformat(),
            "snapshot_source": str(snapshot.attrs.get("snapshot_source") or "unknown"),
            "source_errors": list(snapshot.attrs.get("source_errors") or []),
            "fallback_used": bool(snapshot.attrs.get("fallback_used", False)),
            "stale": bool(snapshot.attrs.get("stale", False)),
            "snapshot": snapshot.to_dict(orient="records"),
            "contexts": contexts,
            "context_errors": context_errors,
        }
        path = self._prefetch_path(local_now.date())
        _write_json(path, payload)
        return {
            "status": "completed",
            "as_of": local_now.isoformat(),
            "path": str(path.resolve()),
            "snapshot_count": len(snapshot),
            "shortlist_count": len(preliminary),
            "context_count": len(contexts),
            "source": payload["snapshot_source"],
            "errors": [*payload["source_errors"], *context_errors],
        }

    def run(
        self,
        mode: str,
        *,
        top_n: int = 5,
        as_of: datetime | None = None,
        notify: bool = False,
        allow_late: bool = False,
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in SUPPORTED_MODES:
            raise ValueError(f"unsupported yao-scout mode: {mode}")
        local_now = _local_datetime(as_of)
        cutoff = self._cutoff_for(normalized_mode, local_now)
        run_id = f"yao-{local_now:%Y%m%d-%H%M%S}-{normalized_mode}-{uuid.uuid4().hex[:8]}"
        if not _is_cn_trading_day(local_now.date()):
            return self._persist_terminal_run(
                run_id=run_id,
                mode=normalized_mode,
                as_of=local_now,
                cutoff=cutoff,
                status="skipped_non_trading_day",
                message="今日为非交易日，未生成观察名单。",
                notify=notify,
            )

        late = normalized_mode == "preopen" and local_now.time() > time(9, 10)
        if late and not allow_late:
            return self._persist_terminal_run(
                run_id=run_id,
                mode=normalized_mode,
                as_of=local_now,
                cutoff=cutoff,
                status="late_suppressed",
                message="09:10后恢复，已抑制过期盘前观察名单。",
                notify=notify,
                late=True,
            )

        snapshot, contexts, source_meta = self._load_run_inputs(normalized_mode, local_now, cutoff)
        point_in_time_ok = bool(source_meta.get("point_in_time_ok", True))
        if snapshot.empty or (normalized_mode == "preopen" and not point_in_time_ok):
            reason = source_meta.get("reason") or "关键盘前时点数据不可用"
            return self._persist_terminal_run(
                run_id=run_id,
                mode=normalized_mode,
                as_of=local_now,
                cutoff=cutoff,
                status="no_qualified_signal",
                message=f"今日无合格信号：{reason}。",
                notify=notify,
                late=late,
                data_quality=source_meta,
            )

        filtered = self._filter_universe(snapshot)
        ranked = self._preliminary_rank(filtered).head(max(25, top_n * 5))
        enriched = enrich_daily_features(
            ranked,
            max_rows=len(ranked),
            lookback_days=160,
            source="auto",
            fetch_retries=1,
            cache_dir=self.data_dir / "daily_history",
            cache_ttl_seconds=6 * 60 * 60,
            max_workers=4,
            history_fetcher=self.history_fetcher,
        )
        context_errors: list[str] = []
        if not contexts and normalized_mode != "preopen":
            contexts, context_errors = collect_candidate_context(
                enriched,
                max_rows=min(len(enriched), max(10, top_n * 2)),
                providers=["news", "announcement", "fund_flow", "quote"],
                cache_dir=self.context_cache_dir,
                cache_ttl_hours=1,
            )
        contexts, context_items_after_cutoff_dropped = _truncate_contexts_to_cutoff(contexts, cutoff)
        context_by_code = {str(item.get("code") or "").zfill(6): item for item in contexts if isinstance(item, dict)}
        scored = [
            self._score_candidate(row, context_by_code.get(str(row.get("code") or "").zfill(6)), source_meta)
            for row in enriched.to_dict(orient="records")
        ]
        scored = [item for item in scored if item["score"] >= 55.0 and item["tradability"]["status"] == "available"]
        scored.sort(key=lambda item: (-item["score"], item["code"]))
        candidates = scored[: max(1, min(int(top_n), 20))]
        for rank, candidate in enumerate(candidates, 1):
            candidate["rank"] = rank

        if normalized_mode == "postclose":
            outcome_update = self.mature_outcomes()
        else:
            outcome_update = {"evaluated": 0, "updated": 0}
        status = "completed" if candidates else "no_qualified_signal"
        message = f"生成{len(candidates)}只高风险异动观察标的。" if candidates else "今日无合格信号。"
        data_quality = {
            **source_meta,
            "snapshot_count": len(snapshot),
            "after_filter_count": len(filtered),
            "daily_enriched_count": int(enriched.attrs.get("daily_success_count", 0)),
            "daily_errors": list(enriched.attrs.get("daily_errors") or []),
            "context_errors": context_errors,
            "context_items_after_cutoff_dropped": context_items_after_cutoff_dropped,
            "critical_complete": point_in_time_ok and not snapshot.empty,
        }
        qualified_model = any(item.get("probability_status") == "qualified" for item in candidates)
        run_model_version = next((str(item.get("model_version")) for item in candidates if item.get("model_version")), MODEL_VERSION)
        payload: dict[str, Any] = {
            "run_id": run_id,
            "mode": normalized_mode,
            "status": status,
            "as_of": local_now.isoformat(),
            "cutoff_at": cutoff.isoformat(),
            "late": late,
            "message": message,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "model_version": run_model_version,
            "model_status": "qualified" if qualified_model else "research_observation",
            "probability_status": "qualified" if qualified_model else "withheld_until_model_gate_passes",
            "data_quality": data_quality,
            "outcome_update": outcome_update,
            "metrics": self.db.get_yao_metrics(),
            "risk_notice": "仅为高风险异动研究观察，不构成投资建议，不自动下单，不承诺收益。",
            "notification_status": "not_requested",
        }
        if normalized_mode == "intraday":
            payload["intraday_alerts"] = self._record_intraday_transitions(payload)
            payload["training_eligible_codes"] = [
                str(item.get("code") or "").zfill(6)
                for item in payload["intraday_alerts"]
                if isinstance(item, dict) and str(item.get("code") or "").strip()
            ]
        report = self._render_run_report(payload)
        report_path = self.report_dir / f"{local_now:%Y-%m-%d}-{normalized_mode}-{run_id[-8:]}.md"
        report_path.write_text(report, encoding="utf-8")
        payload["report_path"] = str(report_path.resolve())
        should_notify = notify and (
            normalized_mode != "intraday" or bool(payload.get("intraday_alerts"))
        )
        payload["notification_status"] = (
            self._notify(payload, report)
            if should_notify
            else "suppressed_no_major_transition" if notify and normalized_mode == "intraday" else "not_requested"
        )
        self.db.save_yao_run(payload)
        return payload

    def mature_outcomes(self) -> dict[str, Any]:
        pending = self.db.list_pending_yao_outcomes(limit=500)
        updated = 0
        unavailable = 0
        for item in pending:
            try:
                history = self.history_fetcher(
                    item["code"],
                    lookback_days=80,
                    source="auto",
                    retries=1,
                    cache_dir=self.data_dir / "daily_history",
                    cache_ttl_seconds=30 * 60,
                )
                outcome = evaluate_outcome_labels(
                    history,
                    code=item["code"],
                    observation_date=date.fromisoformat(item["observation_date"]),
                    entry_price=_safe_float(item.get("entry_price")),
                )
            except Exception as exc:
                outcome = {
                    "maturity_status": "unavailable",
                    "ignition_3d": None,
                    "continuation_5d": None,
                    "strong_10d": None,
                    "tradability": {"status": "unavailable", "reason": str(exc)},
                }
            unavailable += int(outcome.get("maturity_status") in {"unavailable", "deferred"})
            updated += self.db.update_yao_outcome(int(item["candidate_id"]), outcome)
        return {"evaluated": len(pending), "updated": updated, "unavailable_or_deferred": unavailable}

    def backfill(
        self,
        *,
        symbols: Iterable[str] | None = None,
        years: int = 5,
        max_symbols: int = 200,
    ) -> dict[str, Any]:
        """Build a resumable point-in-time research sample without promoting a model."""
        requested = [str(item).zfill(6) for item in (symbols or []) if str(item).strip()]
        if not requested:
            snapshot = self._filter_universe(self._fetch_snapshot())
            requested = [str(value).zfill(6) for value in snapshot["code"].head(max_symbols).tolist()]
        requested = list(dict.fromkeys(requested))[: max(1, min(int(max_symbols), 5000))]
        task_id = f"backfill-{datetime.now(SHANGHAI_TZ):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        output_path = self.backfill_dir / f"{task_id}.jsonl"
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        lookback_days = max(400, int(years) * 260 + 30)
        for code in requested:
            try:
                history = self.history_fetcher(
                    code,
                    lookback_days=lookback_days,
                    source="auto",
                    retries=1,
                    cache_dir=self.data_dir / "daily_history",
                    cache_ttl_seconds=24 * 60 * 60,
                )
                frame = _normalize_history_for_backfill(history)
                if len(frame) < 80:
                    errors.append(f"{code}: insufficient_history:{len(frame)}")
                    continue
                frame["return_1d"] = frame["close"].pct_change() * 100.0
                frame["change_60d"] = frame["close"].pct_change(60) * 100.0
                frame["volume_ratio_20d"] = frame["volume"] / frame["volume"].rolling(20).mean().shift(1)
                frame["volatility_20d_pct"] = frame["close"].pct_change().rolling(20).std() * (252 ** 0.5) * 100.0
                frame["max_drawdown_20d_pct"] = frame["close"].rolling(20).apply(
                    lambda values: float(np.min(values / np.maximum.accumulate(values) - 1.0) * 100.0),
                    raw=True,
                )
                frame["previous_high_20d"] = frame["high"].rolling(20).max().shift(1)
                frame["breakout_20d_pct"] = (frame["close"] / frame["previous_high_20d"] - 1.0) * 100.0
                anchors = frame.iloc[60:-10]
                anchors = anchors.loc[
                    (anchors["return_1d"].abs() >= 4.0)
                    | (anchors["volume_ratio_20d"] >= 1.5)
                    | ((anchors.index % 20) == 0)
                ]
                for _, anchor in anchors.iterrows():
                    obs_date = anchor["date"]
                    outcome = evaluate_outcome_labels(
                        frame,
                        code=code,
                        observation_date=obs_date,
                        entry_price=float(anchor["close"]),
                    )
                    records.append({
                        "code": code,
                        "as_of": obs_date.isoformat(),
                        "features": {
                            "change_1d": _round_or_none(anchor.get("return_1d")),
                            "change_60d": _round_or_none(anchor.get("change_60d")),
                            "volume_ratio_20d": _round_or_none(anchor.get("volume_ratio_20d")),
                            "volatility_20d_pct": _round_or_none(anchor.get("volatility_20d_pct")),
                            "max_drawdown_20d_pct": _round_or_none(anchor.get("max_drawdown_20d_pct")),
                            "breakout_20d_pct": _round_or_none(anchor.get("breakout_20d_pct")),
                            "close": _round_or_none(anchor.get("close")),
                        },
                        "labels": outcome,
                    })
            except Exception as exc:
                errors.append(f"{code}: {exc}")
        output_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
        research_paths = write_historical_report(self.report_dir)
        mature = [row for row in records if row["labels"].get("maturity_status") == "mature"]
        label_rates = {}
        for label in ("ignition_3d", "continuation_5d", "strong_10d"):
            values = [bool(row["labels"].get(label)) for row in mature if row["labels"].get(label) is not None]
            label_rates[label] = round(sum(values) / len(values), 4) if values else None
        model_record = train_challenger(
            records,
            symbol_count=len(requested),
            years=years,
            output_dir=self.model_dir,
        )
        model_record.setdefault("metrics", {})["label_rates"] = label_rates
        append_validation_results(research_paths, model_record)
        self.db.save_yao_model_version(model_record)
        return {
            "task_id": task_id,
            "status": "completed",
            "symbol_count": len(requested),
            "sample_count": len(records),
            "error_count": len(errors),
            "errors": errors[:100],
            "dataset_path": str(output_path.resolve()),
            "research_reports": research_paths,
            "model": model_record,
        }

    def _fetch_snapshot(self) -> pd.DataFrame:
        return self.snapshot_fetcher(
            list(self.screening.snapshot_source_priority),
            required_columns=["code", "name", "price", "amount", "change_pct"],
            fallback_snapshot_path=self.screening.fallback_snapshot_path,
            fallback_max_age_hours=self.screening.snapshot_fallback_max_age_hours,
            cache_ttl_seconds=self.screening.snapshot_cache_ttl_seconds,
            market="cn",
        )

    def _load_run_inputs(
        self,
        mode: str,
        local_now: datetime,
        cutoff: datetime,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, Any]]:
        if mode != "preopen":
            snapshot = self._fetch_snapshot()
            return snapshot, [], self._snapshot_meta(snapshot, point_in_time_ok=True)
        path = self._prefetch_path(local_now.date())
        if not path.is_file():
            return pd.DataFrame(), [], {
                "point_in_time_ok": False,
                "reason": "08:55前预取文件缺失",
                "prefetch_path": str(path.resolve()),
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_at = _local_datetime(datetime.fromisoformat(str(payload.get("created_at"))))
        except Exception as exc:
            return pd.DataFrame(), [], {"point_in_time_ok": False, "reason": f"预取文件损坏:{exc}"}
        age_minutes = (local_now - created_at).total_seconds() / 60.0
        point_in_time_ok = created_at <= cutoff and 0 <= age_minutes <= 40 and not payload.get("stale", False)
        frame = pd.DataFrame(payload.get("snapshot") or []) if point_in_time_ok else pd.DataFrame()
        meta = {
            "point_in_time_ok": point_in_time_ok,
            "source": payload.get("snapshot_source") or "prefetch",
            "source_errors": payload.get("source_errors") or [],
            "fallback_used": bool(payload.get("fallback_used", False)),
            "stale": bool(payload.get("stale", False)),
            "prefetched_at": created_at.isoformat(),
            "stale_age_minutes": round(age_minutes, 2),
            "reason": "" if point_in_time_ok else "预取数据晚于08:55、过旧或已标记陈旧",
        }
        return frame, list(payload.get("contexts") or []), meta

    def _snapshot_meta(self, snapshot: pd.DataFrame, *, point_in_time_ok: bool) -> dict[str, Any]:
        sources = list(self.screening.snapshot_source_priority)
        return {
            "point_in_time_ok": point_in_time_ok,
            "source": str(snapshot.attrs.get("snapshot_source") or "unknown"),
            "source_errors": list(snapshot.attrs.get("source_errors") or []),
            "fallback_used": bool(snapshot.attrs.get("fallback_used", False)),
            "stale": bool(snapshot.attrs.get("stale", False)),
            "stale_age_hours": snapshot.attrs.get("stale_age_hours"),
            "source_health": snapshot_source_health_snapshot(sources),
            "fetched_at": datetime.now(SHANGHAI_TZ).isoformat(),
        }

    @staticmethod
    def _filter_universe(snapshot: pd.DataFrame) -> pd.DataFrame:
        if snapshot is None or snapshot.empty:
            return pd.DataFrame(columns=list(snapshot.columns) if snapshot is not None else [])
        frame = snapshot.copy()
        for column in ("price", "change_pct", "amount", "total_mv", "circ_mv", "volume_ratio", "turnover_rate"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["code"] = frame["code"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("")
        frame["name"] = frame["name"].fillna("").astype(str)
        supported = frame["code"].str.startswith(("00", "30", "60", "68"))
        risk_name = frame["name"].str.upper().str.contains(r"\*?ST|退市|整理", regex=True, na=False)
        active = (frame.get("price", 0) >= 2.0) & (frame.get("amount", 0) >= 20_000_000)
        return frame.loc[supported & ~risk_name & active].copy()

    @staticmethod
    def _preliminary_rank(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame.copy()
        result = frame.copy()
        change = result.get("change_pct", pd.Series(0.0, index=result.index)).fillna(0.0).clip(-10, 20)
        turnover = result.get("turnover_rate", pd.Series(0.0, index=result.index)).fillna(0.0).clip(0, 40)
        volume_ratio = result.get("volume_ratio", pd.Series(1.0, index=result.index)).fillna(1.0).clip(0, 8)
        amount = result.get("amount", pd.Series(0.0, index=result.index)).fillna(0.0).clip(lower=0)
        result["_pre_score"] = (
            (change + 2.0).clip(0, 14) / 14 * 40
            + turnover.clip(0, 20) / 20 * 25
            + volume_ratio.clip(0, 4) / 4 * 20
            + amount.map(lambda value: min(math.log10(max(float(value), 1)) / 10, 1.0)) * 15
        )
        return result.sort_values(["_pre_score", "code"], ascending=[False, True])

    def _score_candidate(
        self,
        row: dict[str, Any],
        context: dict[str, Any] | None,
        source_meta: dict[str, Any],
    ) -> dict[str, Any]:
        code = str(row.get("code") or "").zfill(6)
        change = _safe_float(row.get("change_pct")) or 0.0
        turnover = _safe_float(row.get("turnover_rate")) or 0.0
        volume_ratio = _safe_float(row.get("volume_ratio")) or _safe_float(row.get("volume_ratio_20d")) or 1.0
        breakout = _safe_float(row.get("breakout_20d_pct")) or 0.0
        volatility = _safe_float(row.get("volatility_20d_pct")) or 0.0
        drawdown = _safe_float(row.get("max_drawdown_20d_pct")) or 0.0
        momentum = _clamp((change + 1.0) * 1.4 + max(breakout, 0.0) * 0.7 + (6 if bool(row.get("ma_bullish")) else 0), 0, 30)
        liquidity = _clamp(turnover * 1.1 + min(volume_ratio, 4.0) * 4.0, 0, 25)
        volatility_score = _clamp(6 + volatility * 0.8 - max(volatility - 12, 0) * 1.2, 0, 15)
        context_text = " ".join(str((context or {}).get(key) or "") for key in ("news", "announcement", "fund_flow", "summary"))
        positive_hits = sum(word in context_text for word in POSITIVE_EVENT_WORDS)
        negative_hits = sum(word in context_text for word in NEGATIVE_EVENT_WORDS)
        theme_score = _clamp(3 + positive_hits * 3 + min(len(context_text) / 300, 1) * 4, 0, 15)
        event_score = _clamp(positive_hits * 3.5 - negative_hits * 2.5 + (4 if (context or {}).get("announcement") else 0), 0, 15)
        risks: list[str] = []
        risk_penalty = 0.0
        if turnover > 28:
            risks.append("换手过热，次日分歧和回撤风险高")
            risk_penalty += 8
        if change >= 9.5 and turnover < 0.8:
            risks.append("疑似一字板或极低换手封板，账面强势但无法保证成交")
            risk_penalty += 30
        if negative_hits:
            risks.append("公告/新闻包含减持、亏损或监管类风险词，需核对原文")
            risk_penalty += min(negative_hits * 4, 12)
        if drawdown < -18:
            risks.append("近20日回撤较大，结构稳定性不足")
            risk_penalty += 5
        daily_points = _safe_float(row.get("daily_data_points")) or 0
        if daily_points < 60:
            risks.append("有效历史不足60个交易日，上市早期或数据缺口")
            risk_penalty += 30
        total = _clamp(momentum + liquidity + volatility_score + theme_score + event_score - risk_penalty, 0, 100)
        tradability_status = "available"
        reason = "流动性与价格状态通过基础门槛"
        if change >= 9.5 and turnover < 0.8:
            tradability_status, reason = "unavailable", "疑似低换手一字板"
        elif daily_points < 60:
            tradability_status, reason = "unavailable", "上市早期或行情历史不足"
        tier = "A" if total >= 75 else "B" if total >= 62 else "C"
        evidence = self._evidence_for_context(context, source_meta)
        current_price = _safe_float(row.get("price")) or 0.0
        feature_snapshot = _json_safe_dict({
            "price": current_price,
            "change_1d": change,
            "change_pct": change,
            "change_60d": row.get("change_60d"),
            "amount": row.get("amount"),
            "total_mv": row.get("total_mv"),
            "circ_mv": row.get("circ_mv"),
            "turnover_rate": turnover,
            "volume_ratio": volume_ratio,
            "volume_ratio_20d": _safe_float(row.get("volume_ratio_20d")) or volume_ratio,
            "breakout_20d_pct": breakout,
            "volatility_20d_pct": volatility,
            "max_drawdown_20d_pct": drawdown,
            "daily_data_points": daily_points,
            "daily_source": row.get("daily_source"),
        })
        probabilities, probability_status, model_version = predict_probabilities(
            feature_snapshot,
            champion_path=self.model_dir / "champion.json",
        )
        return {
            "rank": 0,
            "code": code,
            "name": str(row.get("name") or ""),
            "tier": tier,
            "score": round(total, 2),
            "probabilities": probabilities,
            "probability_status": probability_status,
            "scores": {
                "price_momentum": round(momentum, 2),
                "liquidity_turnover": round(liquidity, 2),
                "volatility_regime": round(volatility_score, 2),
                "theme_heat": round(theme_score, 2),
                "event_catalyst": round(event_score, 2),
                "risk_penalty": round(risk_penalty, 2),
            },
            "features": feature_snapshot,
            "evidence": evidence,
            "tradability": {"status": tradability_status, "reason": reason},
            "triggers": [
                "仅在成交可用且价格突破前高时继续观察",
                "换手保持在可承接区间且板块同步扩散",
                "公告或新闻催化可在原始披露中核验",
            ],
            "invalidations": [
                "跌回20日突破位或放量长阴破坏结构",
                "题材热度降温且同板块无扩散",
                "出现停牌、一字板不可交易、减持/监管/业绩重大负面",
            ],
            "risks": risks or ["高波动观察标的，可能快速回撤或无法成交"],
            "data_quality": {
                "snapshot_source": source_meta.get("source"),
                "daily_source": str(row.get("daily_source") or ""),
                "daily_quality_flags": str(row.get("daily_quality_flags") or ""),
                "evidence_available": bool(evidence),
                "point_in_time_ok": bool(source_meta.get("point_in_time_ok", True)),
            },
            "model_version": model_version,
        }

    @staticmethod
    def _evidence_for_context(context: dict[str, Any] | None, source_meta: dict[str, Any]) -> list[dict[str, Any]]:
        if not context:
            return []
        fetched_at = source_meta.get("prefetched_at") or source_meta.get("fetched_at") or datetime.now(SHANGHAI_TZ).isoformat()
        evidence: list[dict[str, Any]] = []
        mappings = (
            ("announcement", "公告", "https://www.cninfo.com.cn/new/disclosure"),
            ("news", "新闻", "https://finance.eastmoney.com/"),
            ("fund_flow", "资金流", "https://quote.eastmoney.com/"),
        )
        for key, label, url in mappings:
            text = str(context.get(key) or "").strip()
            if text:
                evidence.append({
                    "type": key,
                    "title": f"{label}摘要",
                    "summary": text,
                    "url": url,
                    "published_at": None,
                    "fetched_at": fetched_at,
                    "source": "AkShare/公开披露入口",
                    "freshness": "point_in_time_prefetch" if source_meta.get("prefetched_at") else "live",
                })
        return evidence

    def _render_run_report(self, payload: dict[str, Any]) -> str:
        title = {"preopen": "09:00盘前观察", "postclose": "16:30收盘复盘", "intraday": "盘中重大跃迁"}[payload["mode"]]
        lines = [
            f"# 妖股雷达 · {title}",
            "",
            f"- 运行：`{payload['run_id']}`",
            f"- 时点：{payload['as_of']}；信息截止：{payload['cutoff_at']}",
            f"- 模型：{payload['model_version']}（研究观察，未通过正式门禁）",
            f"- 状态：{payload['message']}",
            "",
            "> 仅为高风险异动研究观察，不构成投资建议；不自动下单，不承诺收益。",
            "",
        ]
        candidates = payload.get("candidates") or []
        if not candidates:
            lines.extend(["## 今日结果", "", "今日无合格信号。关键数据不完整时系统选择不推荐。", ""])
        else:
            lines.extend([
                "## Top 5观察名单",
                "",
                "| 排名 | 层级 | 代码 | 名称 | 审计分 | 可交易 | 3/5/10日概率 |",
                "|---:|:---:|---|---|---:|---|---|",
            ])
            for item in candidates:
                lines.append(
                    f"| {item['rank']} | {item['tier']} | {item['code']} | {item['name']} | {item['score']:.2f} | "
                    f"{item['tradability']['status']} | 门禁前不展示 |"
                )
            for item in candidates:
                lines.extend([
                    "",
                    f"### {item['rank']}. {item['name']}（{item['code']}） · {item['tier']}级",
                    "",
                    f"- 分项：量价 {item['scores']['price_momentum']}，换手/流动性 {item['scores']['liquidity_turnover']}，波动 {item['scores']['volatility_regime']}，题材 {item['scores']['theme_heat']}，事件 {item['scores']['event_catalyst']}，风险扣分 {item['scores']['risk_penalty']}。",
                    f"- 触发：{'；'.join(item['triggers'])}",
                    f"- 失效：{'；'.join(item['invalidations'])}",
                    f"- 风险：{'；'.join(item['risks'])}",
                ])
                for evidence in item.get("evidence") or []:
                    lines.append(f"- 证据：[{evidence['title']}]({evidence['url']}) — {evidence['summary']}")
        quality = payload.get("data_quality") or {}
        lines.extend([
            "",
            "## 数据质量",
            "",
            f"- 行情源：{quality.get('source') or 'unknown'}；快照 {quality.get('snapshot_count', 0)} 条；过滤后 {quality.get('after_filter_count', 0)} 条。",
            f"- 时点完整：{quality.get('point_in_time_ok')}；关键数据完整：{quality.get('critical_complete')}；降级错误数：{len(quality.get('source_errors') or []) + len(quality.get('daily_errors') or [])}。",
            "",
        ])
        return "\n".join(lines) + "\n"

    def _notify(self, payload: dict[str, Any], report: str) -> str:
        if not getattr(self.config, "email_sender", None) or not getattr(self.config, "email_password", None):
            return "config_missing"
        subject_prefix = {"preopen": "09:00盘前", "postclose": "16:30复盘", "intraday": "盘中重大信号"}[payload["mode"]]
        success = NotificationService(self.config).send_to_email(
            report,
            subject=f"Stock King 妖股雷达｜{subject_prefix}｜{payload['as_of'][:10]}",
            timeout_seconds=20,
        )
        return "sent" if success else "failed"

    def _persist_terminal_run(
        self,
        *,
        run_id: str,
        mode: str,
        as_of: datetime,
        cutoff: datetime,
        status: str,
        message: str,
        notify: bool,
        late: bool = False,
        data_quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "mode": mode,
            "status": status,
            "as_of": as_of.isoformat(),
            "cutoff_at": cutoff.isoformat(),
            "late": late,
            "message": message,
            "candidate_count": 0,
            "candidates": [],
            "model_version": MODEL_VERSION,
            "model_status": "research_observation",
            "probability_status": "withheld_until_model_gate_passes",
            "data_quality": data_quality or {"critical_complete": False},
            "notification_status": "not_requested",
            "risk_notice": "仅为高风险异动研究观察，不构成投资建议。",
        }
        report = self._render_run_report(payload)
        report_path = self.report_dir / f"{as_of:%Y-%m-%d}-{mode}-{run_id[-8:]}.md"
        report_path.write_text(report, encoding="utf-8")
        payload["report_path"] = str(report_path.resolve())
        if notify and mode == "intraday":
            payload["notification_status"] = "suppressed_no_major_transition"
        else:
            payload["notification_status"] = self._notify(payload, report) if notify else "not_requested"
        self.db.save_yao_run(payload)
        return payload

    def _record_intraday_transitions(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        path = self.data_dir / "intraday_state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except Exception:
            state = {}
        now = _local_datetime(datetime.fromisoformat(payload["as_of"]))
        alerts: list[dict[str, Any]] = []
        for item in payload.get("candidates") or []:
            previous = state.get(item["code"]) if isinstance(state.get(item["code"]), dict) else {}
            last_at_text = str(previous.get("alerted_at") or "")
            try:
                last_at = _local_datetime(datetime.fromisoformat(last_at_text))
            except ValueError:
                last_at = now - timedelta(days=1)
            evidence_signature = "|".join(
                str(evidence.get("summary") or "")
                for evidence in (item.get("evidence") or [])
                if isinstance(evidence, dict) and evidence.get("type") == "announcement"
            )
            crossed = item.get("tier") == "A" and previous.get("tier") != "A"
            new_announcement = bool(evidence_signature) and evidence_signature != previous.get("announcement_signature")
            cooled_down = (now - last_at).total_seconds() >= 30 * 60
            if (crossed or new_announcement) and cooled_down:
                reasons = [reason for active, reason in ((crossed, "跨入A级"), (new_announcement, "高可信新公告")) if active]
                alerts.append({"code": item["code"], "name": item["name"], "reason": "、".join(reasons), "score": item["score"]})
                state[item["code"]] = {
                    "tier": item.get("tier"),
                    "alerted_at": now.isoformat(),
                    "run_id": payload["run_id"],
                    "announcement_signature": evidence_signature,
                }
            else:
                state[item["code"]] = {
                    **previous,
                    "tier": item.get("tier"),
                    "run_id": payload["run_id"],
                    "announcement_signature": evidence_signature or previous.get("announcement_signature", ""),
                }
        _write_json(path, state)
        return alerts

    def _prefetch_path(self, target_date: date) -> Path:
        return self.prefetch_dir / f"{target_date.isoformat()}.json"

    @staticmethod
    def _cutoff_for(mode: str, local_now: datetime) -> datetime:
        if mode == "preopen":
            return datetime.combine(local_now.date(), time(8, 55), tzinfo=SHANGHAI_TZ)
        if mode == "postclose":
            return datetime.combine(local_now.date(), time(15, 0), tzinfo=SHANGHAI_TZ)
        return local_now


def _local_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _is_cn_trading_day(target_date: date) -> bool:
    """Use the packaged AkShare session list when exchange-calendars is absent."""
    if target_date.weekday() >= 5:
        return False
    try:
        from src.core import trading_calendar

        if bool(getattr(trading_calendar, "_XCALS_AVAILABLE", False)):
            return is_market_open("cn", target_date)
    except Exception:
        pass
    try:
        import akshare

        calendar_path = Path(akshare.__file__).resolve().parent / "file_fold" / "calendar.json"
        sessions = json.loads(calendar_path.read_text(encoding="utf-8"))
        if isinstance(sessions, list):
            return target_date.strftime("%Y%m%d") in set(str(item) for item in sessions)
    except Exception:
        pass
    return target_date.weekday() < 5


def _truncate_contexts_to_cutoff(
    contexts: Iterable[dict[str, Any]],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Remove individually timestamped news/announcement items published after cutoff."""
    normalized_cutoff = _local_datetime(cutoff)
    filtered_contexts: list[dict[str, Any]] = []
    dropped_total = 0
    for raw in contexts or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        for key in ("news", "announcement"):
            text, dropped = _truncate_context_text_to_cutoff(item.get(key), normalized_cutoff)
            item[key] = text
            dropped_total += dropped
        filtered_contexts.append(item)
    return filtered_contexts, dropped_total


def _truncate_context_text_to_cutoff(value: Any, cutoff: datetime) -> tuple[str, int]:
    parts = [part.strip() for part in str(value or "").split("|") if part.strip()]
    kept: list[str] = []
    dropped = 0
    for part in parts:
        match = _CONTEXT_TIMESTAMP_RE.match(part)
        if not match:
            kept.append(part)
            continue
        raw_time = match.group("time") or "00:00:00"
        if len(raw_time) == 5:
            raw_time += ":00"
        published_at = datetime.fromisoformat(f"{match.group('date')}T{raw_time}").replace(tzinfo=SHANGHAI_TZ)
        if published_at > cutoff:
            dropped += 1
            continue
        kept.append(part)
    return " | ".join(kept), dropped


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any) -> float | None:
    number = _safe_float(value)
    return round(number, 6) if number is not None else None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(float(value), maximum))


def _json_safe_dict(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, bool)) or value is None:
            result[key] = value
        else:
            number = _safe_float(value)
            result[key] = number if number is not None else str(value)
    return result


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def _normalize_history_for_backfill(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    frame = frame.rename(columns={"日期": "date", "收盘": "close", "成交量": "volume", "最高": "high", "最低": "low", "开盘": "open"})
    if "date" not in frame.columns:
        raise ValueError("daily history missing date")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for key in ("open", "high", "low", "close", "volume"):
        if key not in frame.columns:
            frame[key] = pd.NA
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    return frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
