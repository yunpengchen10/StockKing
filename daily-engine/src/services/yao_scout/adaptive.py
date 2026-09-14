"""Adaptive King Picks engine built on the auditable yao-scout data pipeline.

The module deliberately keeps selection deterministic.  A ``live`` request fetches
new evidence and recomputes the list, but never shuffles it.  Only official scans
are eligible for outcome maturation and model learning.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.core.trading_calendar import is_market_open
from src.services.screening.candidate_context import collect_candidate_context
from src.services.screening.daily import enrich_daily_features

from .service import (
    NEGATIVE_EVENT_WORDS,
    POSITIVE_EVENT_WORDS,
    YaoScoutService,
    _safe_float,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ADAPTIVE_METHOD_VERSION = "king-adaptive-skill-v2.3.1"
ADAPTIVE_SCHEMA_VERSION = 1
SCAN_SLOTS = {"auto", "live", "0922", "0925", "1030", "1455", "review", "weekly"}
OFFICIAL_CANDIDATE_SLOTS = {"0922", "1030", "1455"}
MODEL_BRANCHES = ("M1", "M2", "M3", "M4", "M5", "M6-A", "M6-B")
BASE_MODEL_THRESHOLD = 58.0
BASE_NLS_THRESHOLD = 72.0
MIN_NLS_THRESHOLD = 70.0
HARD_RISK_WORDS = ("立案", "重大减持", "退市", "重大诉讼", "正式否认", "经营风险", "处罚")
M1_EVENT_WORDS = ("控制权", "重组", "并购", "资产注入", "股权转让", "产业投资")
M2_THEME_WORDS = ("政策", "产业", "主线", "数字人民币", "行业景气")
M5_TREND_WORDS = ("订单", "中标", "预增", "扭亏", "利润增长", "业务落地")

_SLOT_TIME = {
    "0922": time(9, 22),
    "0925": time(9, 25),
    "1030": time(10, 30),
    "1455": time(14, 55),
    "review": time(15, 30),
    "weekly": time(15, 45),
}


class AdaptiveKingService:
    """Run M1-M6/NLS scans while sharing Yao Scout data, labels and storage."""

    def __init__(self, yao: YaoScoutService) -> None:
        self.yao = yao
        self.db = yao.db

    def run(
        self,
        scan_slot: str = "live",
        *,
        top_n: int = 5,
        as_of: datetime | None = None,
        official: bool | None = None,
        allow_missed: bool = False,
    ) -> dict[str, Any]:
        slot = normalize_scan_slot(scan_slot)
        local_now = _local_datetime(as_of)
        is_official = slot in OFFICIAL_CANDIDATE_SLOTS if official is None else bool(official)
        if slot == "auto":
            slot, is_official = "live", False
        if slot == "0925":
            return self.confirm_auction(as_of=local_now, allow_missed=allow_missed)
        if slot == "review":
            return self.review(as_of=local_now, allow_missed=allow_missed)
        if slot == "weekly":
            return self.weekly_calibration(as_of=local_now, allow_missed=allow_missed)

        run_id = f"king-{local_now:%Y%m%d-%H%M%S}-{slot}-{uuid.uuid4().hex[:8]}"
        timing = official_timing_status(slot, local_now) if is_official else {"status": "live"}
        if not is_market_open("cn", local_now.date()):
            return self._terminal_run(
                run_id, slot, local_now, "skipped_non_trading_day", "今日为非交易日，未生成正式候选。",
                training_eligible=False, timing=timing,
            )
        if is_official and timing["status"] == "missed" and not allow_missed:
            return self._terminal_run(
                run_id, slot, local_now, "missed_official_slot",
                "已错过官方时点，仅记录缺失；不会使用未来数据补算。",
                training_eligible=False, timing=timing,
            )

        state = self.get_learning_state()
        prior = self._historical_prior()
        previous = self._latest_display_run()
        snapshot = self.yao._fetch_snapshot()
        source_meta = self.yao._snapshot_meta(snapshot, point_in_time_ok=True)
        filtered = self.yao._filter_universe(snapshot)
        ranked = self.yao._preliminary_rank(filtered).head(max(25, min(int(top_n), 5) * 6))
        enriched = enrich_daily_features(
            ranked,
            max_rows=len(ranked),
            lookback_days=160,
            source="auto",
            fetch_retries=1,
            cache_dir=self.yao.data_dir / "daily_history",
            cache_ttl_seconds=20 * 60 if slot == "live" else 60 * 60,
            max_workers=4,
            history_fetcher=self.yao.history_fetcher,
        )
        contexts, context_errors = collect_candidate_context(
            enriched,
            max_rows=min(len(enriched), 25),
            providers=["news", "announcement", "fund_flow", "quote"],
            news_limit=3,
            announcement_limit=3,
            cache_dir=self.yao.context_cache_dir,
            cache_ttl_hours=0.25 if slot == "live" else 1,
        )
        context_by_code = {
            str(item.get("code") or "").zfill(6): item
            for item in contexts
            if isinstance(item, dict)
        }

        scored: list[dict[str, Any]] = []
        history_errors: list[str] = []
        for row in enriched.to_dict(orient="records"):
            code = str(row.get("code") or "").zfill(6)
            context = context_by_code.get(code) or {}
            signature, signature_error = self._history_signature(code)
            if signature_error:
                history_errors.append(f"{code}: {signature_error}")
            candidate = score_adaptive_candidate(
                row,
                context=context,
                signature=signature,
                scan_slot=slot,
                state=state,
                source_meta=source_meta,
            )
            if candidate is None:
                continue
            candidate["similarHistoryFeedback"] = self._similar_history(candidate)
            candidate["probabilityStatus"] = probability_display_status(
                {**state, "availableTradingDays": prior["availableTradingDays"], "historyPoints": signature.get("historyPoints", 0)},
                candidate["similarHistoryFeedback"],
            )
            if candidate["probabilityStatus"] != "research_rate_available":
                candidate["similarHistoryFeedback"].update({
                    "status": "insufficient_history", "touchWithin3dRate": None,
                    "touchWithin3dInterval95": None,
                    "message": "需20个已成熟官方交易日、60日行情及30个有效相似案例，历史频率才可展示。",
                })
            candidate["probabilities"] = {}
            candidate["model_version"] = ADAPTIVE_METHOD_VERSION
            candidate["features"] = candidate.pop("featureSnapshot")
            candidate["scores"] = candidate.pop("scoreBreakdown")
            candidate["triggers"] = candidate.pop("upgradeConditions")
            candidate["invalidations"] = candidate.pop("invalidationConditions")
            scored.append(candidate)

        threshold = adaptive_threshold(slot, state)
        selected = select_candidates(scored, top_n=min(max(int(top_n), 0), 5), threshold=threshold)
        for rank_value, candidate in enumerate(selected, 1):
            candidate["rank"] = rank_value
        changes = candidate_changes(previous, selected)
        quality = {
            **source_meta,
            "snapshot_count": len(snapshot),
            "after_hard_filter_count": len(filtered),
            "daily_enriched_count": int(enriched.attrs.get("daily_success_count", 0)),
            "daily_errors": list(enriched.attrs.get("daily_errors") or []),
            "history_errors": history_errors,
            "context_errors": context_errors,
            "critical_complete": not snapshot.empty,
            "training_eligible": is_official,
        }
        status = "completed" if selected else "no_qualified_signal"
        payload = {
            "schemaVersion": ADAPTIVE_SCHEMA_VERSION,
            "run_id": run_id,
            "mode": f"king_{slot}",
            "scanSlot": slot,
            "official": is_official,
            "trainingEligible": is_official,
            "training_eligible": is_official,
            "status": status,
            "as_of": local_now.isoformat(),
            "cutoff_at": local_now.isoformat(),
            "generatedAt": local_now.isoformat(),
            "timing": timing,
            "candidate_count": len(selected),
            "candidateCount": len(selected),
            "candidates": selected,
            "historicalPrior": prior,
            "changes": changes,
            "calibration": calibration_view(state),
            "dlmSummary": self._dlm_summary(),
            "threshold": threshold,
            "model_version": ADAPTIVE_METHOD_VERSION,
            "modelVersion": ADAPTIVE_METHOD_VERSION,
            "data_quality": quality,
            "dataQuality": quality,
            "notification_status": "not_requested",
            "riskNotice": "仅用于A股短线量化研究；不自动交易，不构成投资建议，不承诺收益。",
        }
        self.db.save_yao_run(payload)
        return payload

    def confirm_auction(
        self,
        *,
        as_of: datetime | None = None,
        allow_missed: bool = False,
    ) -> dict[str, Any]:
        local_now = _local_datetime(as_of)
        timing = official_timing_status("0925", local_now)
        run_id = f"king-{local_now:%Y%m%d-%H%M%S}-0925-{uuid.uuid4().hex[:8]}"
        if not is_market_open("cn", local_now.date()):
            return self._terminal_run(run_id, "0925", local_now, "skipped_non_trading_day", "今日为非交易日。", training_eligible=False, timing=timing)
        if timing["status"] == "missed" and not allow_missed:
            return self._terminal_run(run_id, "0925", local_now, "missed_official_slot", "已错过09:25竞价确认时点，不补算。", training_eligible=False, timing=timing)
        prior = self.db.get_latest_yao_run(mode="king_0922")
        prior_result = (prior or {}).get("result") or {}
        if not prior_result or str(prior_result.get("as_of", ""))[:10] != local_now.date().isoformat():
            return self._terminal_run(run_id, "0925", local_now, "missing_0922_scan", "缺少当日09:22扫描，无法伪造竞价确认。", training_eligible=False, timing=timing)
        snapshot = self.yao._filter_universe(self.yao._fetch_snapshot())
        by_code = {
            str(row.get("code") or "").zfill(6): row
            for row in snapshot.to_dict(orient="records")
        }
        confirmations = []
        for item in prior_result.get("candidates") or []:
            current = by_code.get(str(item.get("code") or "").zfill(6))
            confirmations.append({
                "code": item.get("code"),
                "name": item.get("name"),
                "status": "confirmed" if current else "unavailable",
                "auctionPrice": _round(_safe_float((current or {}).get("price"))),
                "auctionChangePct": _round(_safe_float((current or {}).get("change_pct"))),
                "instruction": "09:25最终竞价已确认；仍须满足升级条件" if current else "行情缺失，禁止依据09:22参考价行动",
            })
        payload = {
            "run_id": run_id,
            "mode": "king_0925",
            "scanSlot": "0925",
            "official": True,
            "trainingEligible": False,
            "training_eligible": False,
            "status": "completed",
            "as_of": local_now.isoformat(),
            "cutoff_at": local_now.isoformat(),
            "model_version": ADAPTIVE_METHOD_VERSION,
            "candidate_count": 0,
            "candidates": [],
            "confirmations": confirmations,
            "timing": timing,
            "data_quality": {"source_0922_run_id": prior_result.get("run_id")},
            "notification_status": "not_requested",
        }
        self.db.save_yao_run(payload)
        return payload

    def review(self, *, as_of: datetime | None = None, allow_missed: bool = False) -> dict[str, Any]:
        local_now = _local_datetime(as_of)
        timing = official_timing_status("review", local_now)
        run_id = f"king-{local_now:%Y%m%d-%H%M%S}-review-{uuid.uuid4().hex[:8]}"
        if not is_market_open("cn", local_now.date()):
            return self._terminal_run(run_id, "review", local_now, "skipped_non_trading_day", "今日为非交易日。", training_eligible=False, timing=timing)
        if timing["status"] == "missed" and not allow_missed:
            return self._terminal_run(run_id, "review", local_now, "missed_official_slot", "15:30复盘时点未到或已错过。", training_eligible=False, timing=timing)
        matured = self.yao.mature_outcomes()
        history = self.db.get_yao_candidate_history(limit=500, model_prefix="M")
        state = learn_from_outcomes(self.get_learning_state(), history, local_now.date())
        self.db.save_yao_adaptive_state("king_adaptive", state)
        memo_count = 0
        for item in history:
            if item.get("maturity_status") != "mature" or not item.get("candidate_id"):
                continue
            label = classify_result_label(item)
            root_cause = classify_root_cause(item, label)
            memo = {
                "recordKey": f"candidate-{item['candidate_id']}",
                "date": item.get("observation_date"),
                "model_stage": state.get("stage"),
                "calibration_day": state.get("calibrationDay"),
                "stock": item.get("code"),
                "sample_type": sample_type_for_label(label),
                "model": item.get("model_branch"),
                "signals_visible_at_time": item.get("features") or {},
                "score": item.get("score"),
                "reference_price": (item.get("features") or {}).get("price"),
                "final_result": label,
                "mfe": item.get("max_return_pct"),
                "mae": item.get("max_drawdown_pct"),
                "root_cause": root_cause,
                "lesson": lesson_for(label, root_cause),
                "next_day_change": (state.get("pendingAdjustments") or {}).get(item.get("model_branch")),
                "status": state.get("adjustmentStatus", "observe"),
            }
            memo_count += self.db.save_yao_dlm(memo)
        audit = self._audit_misses_and_rejections(local_now, history, state)
        memo_count += int(audit.get("written", 0))
        payload = {
            "run_id": run_id,
            "mode": "king_review",
            "scanSlot": "review",
            "official": True,
            "trainingEligible": False,
            "training_eligible": False,
            "status": "completed",
            "as_of": local_now.isoformat(),
            "cutoff_at": local_now.isoformat(),
            "model_version": ADAPTIVE_METHOD_VERSION,
            "candidate_count": 0,
            "candidates": [],
            "outcomeUpdate": matured,
            "dlmWritten": memo_count,
            "missAudit": audit,
            "learningState": state,
            "timing": timing,
            "data_quality": {"future_data_used": False},
            "notification_status": "not_requested",
        }
        self.db.save_yao_run(payload)
        return payload

    def _audit_misses_and_rejections(
        self,
        local_now: datetime,
        history: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        selected_codes = {
            str(item.get("code") or "").zfill(6)
            for item in history
            if item.get("scan_slot") in OFFICIAL_CANDIDATE_SLOTS
            and str(item.get("observation_date")) == local_now.date().isoformat()
        }
        try:
            snapshot = self.yao._filter_universe(self.yao._fetch_snapshot())
        except Exception as exc:
            return {"status": "unavailable", "message": str(exc), "misses": 0, "correctRejections": 0, "written": 0}
        misses = 0
        rejections = 0
        written = 0
        for row in snapshot.to_dict(orient="records"):
            code = str(row.get("code") or "").zfill(6)
            if code in selected_codes:
                continue
            change = _safe_float(row.get("change_pct")) or 0.0
            turnover = _safe_float(row.get("turnover_rate")) or 0.0
            limit_threshold = 19.0 if code.startswith(("30", "68")) else 9.4
            if change < limit_threshold:
                continue
            signature, _ = self._history_signature(code)
            locked = turnover < 0.8
            if locked:
                sample_type, root_cause = "correct_rejection", "unavailable_one_price_limit"
                rejections += 1
            else:
                recent = int(signature.get("recentLimitUps10d") or 0)
                recovery = _safe_float(signature.get("recoveryRatio")) or 0.0
                root_cause = "I2" if recent >= 2 and recovery >= 0.65 else "I1" if recent >= 2 else "I4"
                sample_type = "miss"
                misses += 1
            memo = {
                "recordKey": f"{sample_type}-{local_now:%Y%m%d}-{code}",
                "date": local_now.date().isoformat(),
                "model_stage": state.get("stage"),
                "calibration_day": state.get("calibrationDay"),
                "stock": code,
                "sample_type": sample_type,
                "model": "M6" if root_cause in {"I1", "I2", "I3", "I4"} else "risk",
                "signals_visible_at_time": {"change_pct": change, "turnover_rate": turnover, **signature},
                "score": None,
                "reference_price": _safe_float(row.get("price")),
                "final_result": "S" if sample_type == "miss" else "N",
                "mfe": None,
                "mae": None,
                "root_cause": root_cause,
                "lesson": "补充漏选前兆进入challenger，不直接放宽硬门槛。" if sample_type == "miss" else "不可交易风向标不进入正式候选。",
                "next_day_change": None,
                "status": "observe",
            }
            written += self.db.save_yao_dlm(memo)
        return {"status": "completed", "misses": misses, "correctRejections": rejections, "written": written}

    def weekly_calibration(self, *, as_of: datetime | None = None, allow_missed: bool = False) -> dict[str, Any]:
        local_now = _local_datetime(as_of)
        timing = official_timing_status("weekly", local_now)
        run_id = f"king-{local_now:%Y%m%d-%H%M%S}-weekly-{uuid.uuid4().hex[:8]}"
        if not is_market_open("cn", local_now.date()):
            return self._terminal_run(run_id, "weekly", local_now, "skipped_non_trading_day", "今日为非交易日。", training_eligible=False, timing=timing)
        if timing["status"] == "missed" and not allow_missed:
            return self._terminal_run(run_id, "weekly", local_now, "missed_official_slot", "15:45周度校准时点未到或已错过。", training_eligible=False, timing=timing)
        state = self.get_learning_state()
        history = self.db.get_yao_candidate_history(limit=1000, model_prefix="M")
        finalized = [item for item in history if item.get("maturity_status") == "mature"]
        prior_status = str(state.get("adjustmentStatus") or "observe")
        last_sample_count = int(state.get("lastWeeklySampleCount", 0))
        new_samples = max(len(finalized) - last_sample_count, 0)
        if len(finalized) < 5 or new_samples < 5:
            status = "observe"
            decision = "尚未新增满一周（5个）最终样本；权重和阈值保持不变。"
        elif prior_status == "trial":
            recent = finalized[: max(5, min(len(finalized), 20))]
            f_rate = sum(classify_result_label(item) == "F" for item in recent) / len(recent)
            baseline = float(state.get("trialBaselineFRate", 1.0))
            if f_rate <= baseline:
                status, decision = "solidified", "challenger未恶化并通过周度样本门禁，固化小步调整。"
                state["modelOffsets"] = bounded_offsets(state.get("modelOffsets") or {}, state.get("pendingAdjustments") or {})
            else:
                status, decision = "revoked", "challenger表现恶化，自动撤销试行调整。"
            state["pendingAdjustments"] = {}
        else:
            recent = finalized[: min(len(finalized), 20)]
            f_rate = sum(classify_result_label(item) == "F" for item in recent) / len(recent)
            status, decision = "trial", "达到周度样本门禁；调整进入challenger试行，尚未固化。"
            state["trialBaselineFRate"] = round(f_rate, 4)
        state["adjustmentStatus"] = status
        if new_samples >= 5:
            state["lastWeeklySampleCount"] = len(finalized)
        state["updatedAt"] = local_now.isoformat()
        self.db.save_yao_adaptive_state("king_adaptive", state)
        payload = {
            "run_id": run_id, "mode": "king_weekly", "scanSlot": "weekly", "official": True,
            "trainingEligible": False, "training_eligible": False, "status": "completed",
            "as_of": local_now.isoformat(), "cutoff_at": local_now.isoformat(),
            "model_version": ADAPTIVE_METHOD_VERSION, "candidate_count": 0, "candidates": [],
            "sampleCount": len(finalized), "decision": decision, "learningState": state,
            "timing": timing, "data_quality": {"weekly_gate": len(finalized) >= 5},
            "notification_status": "not_requested",
        }
        self.db.save_yao_run(payload)
        return payload

    def get_learning_state(self) -> dict[str, Any]:
        stored = self.db.get_yao_adaptive_state("king_adaptive") or {}
        defaults = {
            "methodVersion": ADAPTIVE_METHOD_VERSION,
            "stage": "calibration",
            "calibrationDay": 0,
            "normalTradingDays": 0,
            "adjustmentStatus": "observe",
            "modelOffsets": {branch: 0.0 for branch in MODEL_BRANCHES},
            "thresholdOffsets": {},
            "pendingAdjustments": {},
            "failureProtection": {},
            "hardRiskRulesMutable": False,
        }
        defaults.update(stored)
        return defaults

    def _historical_prior(self) -> dict[str, Any]:
        rows = self.db.get_yao_candidate_history(limit=1000, model_prefix="M")
        official = [row for row in rows if row.get("scan_slot") in OFFICIAL_CANDIDATE_SLOTS]
        trading_days = sorted({str(row.get("observation_date")) for row in official if row.get("observation_date") and row.get("maturity_status") == "mature"}, reverse=True)[:20]
        finalized = [row for row in official if row.get("maturity_status") == "mature" and str(row.get("observation_date")) in trading_days]
        labels = Counter(classify_result_label(row) for row in finalized)
        return {
            "requiredTradingDays": 20,
            "availableTradingDays": len(trading_days),
            "sampleCount": len(finalized),
            "status": "ready" if len(trading_days) >= 20 else "insufficient_history",
            "resultDistribution": dict(labels),
            "failureSamplesChecked": sum(1 for row in finalized if classify_result_label(row) == "F"),
            "missedSamplesChecked": len(self.db.list_yao_dlm(limit=100, sample_type="miss")),
            "marketDrift": "reduced_weight" if len(trading_days) >= 5 and _market_drift(finalized) else "not_detected_or_insufficient",
        }

    def _similar_history(self, candidate: dict[str, Any]) -> dict[str, Any]:
        branch = str(candidate.get("modelBranch") or "")
        rows = self.db.get_yao_candidate_history(limit=1000, model_prefix=branch)
        current = candidate.get("features") or candidate.get("featureSnapshot") or {}
        comparable = []
        seen = set()
        for row in rows:
            if row.get("maturity_status") != "mature" or row.get("scan_slot") not in OFFICIAL_CANDIDATE_SLOTS or row.get("ignition_3d") is None:
                continue
            key = (row.get("observation_date"), row.get("code"))
            if key in seen:
                continue
            seen.add(key)
            distance = feature_distance(current, row.get("features") or {})
            comparable.append((distance, row))
        comparable.sort(key=lambda pair: pair[0])
        nearest = [row for _, row in comparable[:100]]
        if len(nearest) < 30:
            return {
                "status": "insufficient_samples",
                "sampleCount": len(nearest),
                "required": 30,
                "nextDayLimitUpRate": None,
                "touchWithin3dRate": None,
                "touchWithin3dInterval95": None,
                "resultDistribution": {},
                "averageMFE": None,
                "averageMAE": None,
                "averageOpenPremium": None,
                "invalidationFirstRate": None,
                "message": "有效相似案例不足30个，不展示历史频率。",
            }
        labels = Counter(classify_result_label(row) for row in nearest)
        mfe = [_safe_float(row.get("max_return_pct")) for row in nearest]
        mae = [_safe_float(row.get("max_drawdown_pct")) for row in nearest]
        limit_hits = [bool(row.get("ignition_3d")) for row in nearest if row.get("ignition_3d") is not None]
        f_count = labels.get("F", 0)
        drift = _market_drift(nearest)
        return {
            "status": "ready",
            "sampleCount": len(nearest),
            "required": 30,
            "weight": 0.5 if drift else 1.0,
            "marketDrift": drift,
            "nextDayLimitUpRate": None,
            "touchWithin3dRate": round(sum(limit_hits) / len(limit_hits), 4) if limit_hits else None,
            "touchWithin3dInterval95": descriptive_rate_interval(sum(limit_hits), len(limit_hits)),
            "rateSampleCount": len(limit_hits),
            "frequencyMeaning": "历史案例未来3个交易日内触及涨停的比例；不是次日封板率、当前股票预测概率或可实现收益。区间未消除案例相关性。",
            "resultDistribution": dict(labels),
            "averageMFE": _mean(mfe),
            "averageMAE": _mean(mae),
            "averageOpenPremium": None,
            "invalidationFirstRate": round(f_count / len(nearest), 4),
            "commonSuccessFactor": "量价与结构共振" if labels.get("S", 0) + labels.get("A", 0) else None,
            "commonFailureFactor": "高分后结构失效" if f_count else None,
        }

    def _history_signature(self, code: str) -> tuple[dict[str, Any], str | None]:
        try:
            history = self.yao.history_fetcher(
                code,
                lookback_days=80,
                source="auto",
                retries=1,
                cache_dir=self.yao.data_dir / "daily_history",
                cache_ttl_seconds=60 * 60,
            )
            return compute_history_signature(history, code), None
        except Exception as exc:
            return {"historyPoints": 0, "recentLimitUps10d": 0, "recentLimitUps20d": 0}, str(exc)

    def _latest_display_run(self) -> dict[str, Any] | None:
        rows = []
        for mode in ("king_live", "king_0922", "king_1030", "king_1455"):
            value = self.db.get_latest_yao_run(mode=mode)
            if value:
                rows.append(value)
        if not rows:
            return None
        latest = max(rows, key=lambda row: str(row.get("as_of") or ""))
        result = latest.get("result")
        return result if isinstance(result, dict) else None

    def _dlm_summary(self) -> dict[str, Any]:
        rows = self.db.list_yao_dlm(limit=20)
        return {
            "available": bool(rows),
            "latest": rows[0] if rows else None,
            "recentCount": len(rows),
        }

    def _terminal_run(
        self,
        run_id: str,
        slot: str,
        local_now: datetime,
        status: str,
        message: str,
        *,
        training_eligible: bool,
        timing: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "mode": f"king_{slot}",
            "scanSlot": slot,
            "official": slot in OFFICIAL_CANDIDATE_SLOTS or slot in {"0925", "review", "weekly"},
            "trainingEligible": training_eligible,
            "training_eligible": training_eligible,
            "status": status,
            "message": message,
            "as_of": local_now.isoformat(),
            "cutoff_at": local_now.isoformat(),
            "model_version": ADAPTIVE_METHOD_VERSION,
            "candidate_count": 0,
            "candidates": [],
            "timing": timing,
            "data_quality": {"future_data_used": False},
            "notification_status": "not_requested",
        }
        self.db.save_yao_run(payload)
        return payload


def normalize_scan_slot(value: str) -> str:
    slot = str(value or "live").strip().lower()
    if slot not in SCAN_SLOTS:
        raise ValueError(f"unsupported adaptive scan slot: {value}")
    return slot


def official_timing_status(slot: str, now: datetime) -> dict[str, Any]:
    scheduled = _SLOT_TIME.get(slot)
    if scheduled is None:
        return {"status": "live"}
    target = datetime.combine(now.date(), scheduled, tzinfo=SHANGHAI_TZ)
    delta_minutes = (now - target).total_seconds() / 60
    tolerance = 7 if slot in {"review", "weekly"} else 4
    return {
        "status": "on_time" if abs(delta_minutes) <= tolerance else "missed",
        "scheduledAt": target.isoformat(),
        "actualAt": now.isoformat(),
        "deltaMinutes": round(delta_minutes, 2),
    }


def score_adaptive_candidate(
    row: dict[str, Any],
    *,
    context: dict[str, Any],
    signature: dict[str, Any],
    scan_slot: str,
    state: dict[str, Any],
    source_meta: dict[str, Any],
) -> dict[str, Any] | None:
    code = str(row.get("code") or "").zfill(6)
    name = str(row.get("name") or "")
    price = _safe_float(row.get("price")) or 0.0
    amount = _safe_float(row.get("amount")) or 0.0
    change = _safe_float(row.get("change_pct")) or 0.0
    turnover = _safe_float(row.get("turnover_rate")) or 0.0
    volume_ratio = _safe_float(row.get("volume_ratio")) or _safe_float(row.get("volume_ratio_20d")) or 1.0
    history_points = int(signature.get("historyPoints") or _safe_float(row.get("daily_data_points")) or 0)
    text = " ".join(str(context.get(key) or "") for key in ("news", "announcement", "fund_flow", "summary"))
    positive_hits = sum(word in text for word in POSITIVE_EVENT_WORDS)
    negative_hits = sum(word in text for word in NEGATIVE_EVENT_WORDS)
    m1_hits = sum(word in text for word in M1_EVENT_WORDS)
    m2_hits = sum(word in text for word in M2_THEME_WORDS)
    m5_hits = sum(word in text for word in M5_TREND_WORDS)
    hard_hits = [word for word in HARD_RISK_WORDS if word in text]
    if not code.strip("0") or price < 2 or amount < 20_000_000 or history_points < 5 or hard_hits:
        return None
    repeat_guard = (state.get("repeatProtection") or {}).get(code) or {}
    if repeat_guard and not positive_hits:
        try:
            if date.fromisoformat(str(repeat_guard.get("until"))) >= datetime.now(SHANGHAI_TZ).date():
                return None
        except ValueError:
            pass
    limit_pct = 20.0 if code.startswith(("30", "68")) else 10.0
    distance_to_limit = max(limit_pct - change, 0.0)
    locked = change >= limit_pct - 0.5 and turnover < 0.8
    if locked or (distance_to_limit < (4.0 if limit_pct == 20 else 2.0) and turnover < 1.0):
        return None

    breakout = _safe_float(row.get("breakout_20d_pct")) or 0.0
    drawdown = _safe_float(row.get("max_drawdown_20d_pct")) or 0.0
    recovery = _safe_float(signature.get("recoveryRatio")) or intraday_recovery(row)
    limit_10 = int(signature.get("recentLimitUps10d") or 0)
    limit_20 = int(signature.get("recentLimitUps20d") or 0)
    amount_multiple = _safe_float(signature.get("amountMultiple5d")) or min(max(volume_ratio, 0.0), 6.0)
    vwap = _safe_float(row.get("vwap"))
    if not vwap:
        volume = _safe_float(row.get("volume")) or 0.0
        vwap = amount / volume if amount > 0 and volume > 0 else price
    vwap_state = "above" if price >= vwap else "below"

    branch_scores = {
        "M1": _clamp(30 + m1_hits * 20 + max(change, 0) * 1.8 + max(breakout, 0) * 1.2 - negative_hits * 8, 0, 100),
        "M2": _clamp(30 + m2_hits * 16 + max(change, 0) * 2.0 + min(volume_ratio, 4) * 4, 0, 100),
        "M3": _clamp(30 + min(amount_multiple, 5) * 10 + min(volume_ratio, 5) * 7 + min(turnover, 20) * 1.0, 0, 100),
        "M4": _clamp(27 + limit_20 * 11 + max(breakout, 0) * 2 + min(volume_ratio, 4) * 5, 0, 100),
        "M5": _clamp(29 + m5_hits * 18 + max(breakout, 0) * 1.5 + (8 if bool(row.get("ma_bullish")) else 0), 0, 100),
        "M6-A": _clamp(25 + limit_10 * 14 + recovery * 28 + min(turnover, 25) * 0.8 + (8 if vwap_state == "above" else 0), 0, 100),
        "M6-B": _clamp(18 + limit_10 * 15 + (35 if change <= -7 and recovery >= 0.65 else recovery * 25) + min(turnover, 25) * 0.7, 0, 100),
    }
    offsets = state.get("modelOffsets") or {}
    branch_scores = {key: _clamp(value + float(offsets.get(key, 0.0)), 0, 100) for key, value in branch_scores.items()}
    # Explicit event/theme/performance evidence owns the primary branch even
    # when a stock also has M6 memory. M6 is the fallback second-stage model,
    # not a label that erases independently auditable M1/M2/M5 evidence.
    if m1_hits:
        model_branch = "M1"
    elif m5_hits:
        model_branch = "M5"
    elif m2_hits:
        model_branch = "M2"
    elif limit_10 >= 2 and change <= -7 and recovery >= 0.65:
        model_branch = "M6-B"
    elif limit_10 >= 2 and recovery >= 0.6:
        model_branch = "M6-A"
    elif limit_20 >= 1 and breakout > 0:
        model_branch = "M4"
    else:
        model_branch = "M3"
    model_score = branch_scores[model_branch]
    protection = (state.get("failureProtection") or {}).get(model_branch) or {}
    if protection.get("paused") and not positive_hits:
        return None

    nls_breakdown = compute_nls(
        change=change,
        turnover=turnover,
        volume_ratio=volume_ratio,
        recovery=recovery,
        breakout=breakout,
        distance_to_limit=distance_to_limit,
        limit_count=limit_20,
        positive_hits=positive_hits,
        negative_hits=negative_hits,
        vwap_state=vwap_state,
        scan_slot=scan_slot,
    )
    score = nls_breakdown["total"] if scan_slot == "1455" else round(model_score, 2)
    old_risk_decay = bool(negative_hits and (limit_20 > 0 or breakout > 0) and not hard_hits)
    risks = []
    if negative_hits:
        risks.append("存在历史利空词；已按后续强势消化程度递减，但新增硬风险仍一票否决。" if old_risk_decay else "存在未充分消化的负面事件。")
    if history_points < 60:
        risks.append("不足60个交易日行情；可研究但历史反馈降级。")
    if scan_slot == "1455" and nls_breakdown.get("latePulseRejected"):
        risks.append("尾盘脉冲缺少量价/催化共振，NLS已限制。")
    if drawdown < -18:
        risks.append("近20日最大回撤偏高。")
    auction = "09:25最终集合竞价确认后为准" if scan_slot == "0922" else "not_applicable"
    feature_snapshot = {
        "price": round(price, 4), "change_pct": round(change, 4), "amount": amount,
        "turnover_rate": round(turnover, 4), "volume_ratio": round(volume_ratio, 4),
        "amount_multiple_5d": round(amount_multiple, 4), "breakout_20d_pct": round(breakout, 4),
        "max_drawdown_20d_pct": round(drawdown, 4), "recent_limitups_10d": limit_10,
        "recent_limitups_20d": limit_20, "recovery_ratio": round(recovery, 4),
        "vwap": round(vwap, 4), "vwap_state": vwap_state, "distance_to_limit_pct": round(distance_to_limit, 4),
        "daily_data_points": history_points, "snapshot_source": source_meta.get("source"),
        "model_branch": model_branch, "scan_slot": scan_slot,
    }
    return {
        "rank": 0,
        "code": code,
        "name": name,
        "tier": "A" if score >= 78 else "B",
        "pool": "B" if model_branch.startswith("M6") else "A",
        "model": model_branch,
        "modelBranch": model_branch,
        "score": round(score, 2),
        "nls": nls_breakdown["total"] if scan_slot == "1455" else None,
        "referencePrice": round(price, 2),
        "currentChange": round(change, 2),
        "turnoverMultiple": round(max(turnover / 5, 0), 2),
        "amountMultiple": round(amount_multiple, 2),
        "volumeRatio": round(volume_ratio, 2),
        "R": round(recovery, 3),
        "vwapState": vwap_state,
        "auctionConfirmation": auction,
        "positioning": "高辨识度二阶段" if model_branch.startswith("M6") else "首板前/低位启动",
        "scoreBreakdown": {"selectedModel": model_branch, "models": {key: round(value, 2) for key, value in branch_scores.items()}, "nls": nls_breakdown},
        "featureSnapshot": feature_snapshot,
        "evidence": _evidence(context, source_meta),
        "tradability": {"status": "available", "reason": "硬过滤、涨停距离与成交可用性通过"},
        "upgradeConditions": [
            "量价继续同向且不跌破VWAP/关键突破位",
            "板块或可核验催化形成持续共振",
        ],
        "invalidationConditions": [
            "先跌破日内低点或20日关键支撑且无法快速收复",
            "新增重大减持、监管、业绩雷、正式否认或经营风险",
        ],
        "risks": risks or ["高波动研究候选，可能快速失效或无法按参考价成交。"],
        "data_quality": {
            "history60d": history_points >= 60,
            "sectorEvidence": bool(context.get("summary") or context.get("news")),
            "minuteTail": bool(row.get("minute_tail_available", False)),
        },
        "thresholdAdjustment": float(protection.get("thresholdAdd", 0.0)),
        "modelCandidateLimit": protection.get("maxCandidates"),
    }


def select_candidates(candidates: Iterable[dict[str, Any]], *, top_n: int, threshold: float) -> list[dict[str, Any]]:
    if top_n <= 0:
        return []
    eligible = [
        item for item in candidates
        if float(item.get("score") or 0.0) >= threshold + float(item.get("thresholdAdjustment") or 0.0)
    ]
    eligible.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("code") or "")))
    limited: list[dict[str, Any]] = []
    model_counts: Counter[str] = Counter()
    for item in eligible:
        branch = str(item.get("modelBranch") or "")
        maximum = item.get("modelCandidateLimit")
        if maximum is not None and model_counts[branch] >= int(maximum):
            continue
        limited.append(item)
        model_counts[branch] += 1
    eligible = limited
    a_pool = [item for item in eligible if not str(item.get("modelBranch") or "").startswith("M6")]
    m6_pool = [item for item in eligible if str(item.get("modelBranch") or "").startswith("M6")]

    # M6 is a valuable second-stage pool, but it must not monopolise King Top 5.
    # When both pools qualify, reserve one M6 seat and at least 60% of the list
    # for M1-M5. Prefer distinct A-pool branches before taking duplicates.
    reserve_m6 = 1 if m6_pool else 0
    minimum_a = min(len(a_pool), max(top_n - reserve_m6, 0), math.ceil(top_n * 0.6))
    maximum_m6 = min(len(m6_pool), max(1, math.floor(top_n * 0.4))) if m6_pool else 0
    selected: list[dict[str, Any]] = []
    selected_codes: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        code = str(item.get("code") or "")
        if code not in selected_codes:
            selected.append(item)
            selected_codes.add(code)

    seen_a_branches: set[str] = set()
    for item in a_pool:
        branch = str(item.get("modelBranch") or "")
        if branch in seen_a_branches:
            continue
        add(item)
        seen_a_branches.add(branch)
        if len(selected) >= minimum_a:
            break
    if len(selected) < minimum_a:
        for item in a_pool:
            add(item)
            if len(selected) >= minimum_a:
                break

    if reserve_m6:
        add(m6_pool[0])

    for item in eligible:
        if len(selected) >= top_n:
            break
        branch = str(item.get("modelBranch") or "")
        if branch.startswith("M6"):
            current_m6 = sum(str(value.get("modelBranch") or "").startswith("M6") for value in selected)
            if current_m6 >= maximum_m6:
                continue
        add(item)

    selected.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("code") or "")))
    return selected[:top_n]


def compute_nls(
    *, change: float, turnover: float, volume_ratio: float, recovery: float,
    breakout: float, distance_to_limit: float, limit_count: int,
    positive_hits: int, negative_hits: int, vwap_state: str, scan_slot: str,
) -> dict[str, Any]:
    tail = _clamp(5 + recovery * 10 + (5 if vwap_state == "above" else 0) + max(change, 0) * 0.4, 0, 20)
    volume_price = _clamp(4 + min(volume_ratio, 4) * 3 + min(turnover, 20) * 0.2 + max(breakout, 0) * 0.4, 0, 20)
    # A price move by itself is not sector evidence.  Until the shared context
    # has a verifiable theme/leader signal, this dimension remains deliberately low.
    sector = _clamp(3 + positive_hits * 4, 0, 15)
    next_day_space = _clamp(15 - max(0, 5 - distance_to_limit) * 1.5 + min(max(distance_to_limit, 0), 8) * 0.4, 0, 15)
    identity = _clamp(3 + limit_count * 4 + min(turnover, 20) * 0.25, 0, 15)
    catalyst = _clamp(positive_hits * 4 + (2 if positive_hits else 0), 0, 10)
    safety = _clamp(5 - negative_hits * 2.5 - max(turnover - 30, 0) * 0.2, 0, 5)
    late_pulse = scan_slot == "1455" and tail >= 14 and sector < 6 and volume_price < 10 and catalyst < 4
    total = tail + volume_price + sector + next_day_space + identity + catalyst + safety
    if late_pulse:
        total = min(total, 69.0)
    return {
        "tailStrength": round(tail, 2),
        "volumePriceStructure": round(volume_price, 2),
        "sectorLeaderResonance": round(sector, 2),
        "nextDaySpace": round(next_day_space, 2),
        "identityMemory": round(identity, 2),
        "catalystExpectationGap": round(catalyst, 2),
        "riskSafety": round(safety, 2),
        "positiveDimensions": round(tail + volume_price + sector + next_day_space + identity + catalyst, 2),
        "total": round(_clamp(total, 0, 100), 2),
        "latePulseRejected": late_pulse,
    }


def adaptive_threshold(scan_slot: str, state: dict[str, Any]) -> float:
    base = BASE_NLS_THRESHOLD if scan_slot == "1455" else BASE_MODEL_THRESHOLD
    offset = float((state.get("thresholdOffsets") or {}).get(scan_slot, 0.0))
    threshold = base + offset
    return max(MIN_NLS_THRESHOLD, threshold) if scan_slot == "1455" else threshold


def compute_history_signature(history: pd.DataFrame, code: str) -> dict[str, Any]:
    if history is None or history.empty:
        return {"historyPoints": 0, "recentLimitUps10d": 0, "recentLimitUps20d": 0}
    frame = history.rename(columns={"收盘": "close", "开盘": "open", "最高": "high", "最低": "low", "成交额": "amount"}).copy()
    for key in ("close", "open", "high", "low", "amount"):
        if key in frame.columns:
            frame[key] = pd.to_numeric(frame[key], errors="coerce")
    frame = frame.dropna(subset=["close"]).tail(80)
    pct = frame["close"].pct_change() * 100
    threshold = 19.0 if str(code).startswith(("30", "68")) else 9.4
    amount_multiple = None
    if "amount" in frame.columns and len(frame) >= 6:
        baseline = frame["amount"].iloc[-6:-1].mean()
        if baseline and not math.isnan(float(baseline)):
            amount_multiple = float(frame["amount"].iloc[-1] / baseline)
    recovery = 0.0
    if {"close", "high", "low"}.issubset(frame.columns) and len(frame):
        high, low, close = (float(frame[key].iloc[-1]) for key in ("high", "low", "close"))
        recovery = (close - low) / (high - low) if high > low else 0.5
    return {
        "historyPoints": len(frame),
        "recentLimitUps10d": int((pct.tail(10) >= threshold).sum()),
        "recentLimitUps20d": int((pct.tail(20) >= threshold).sum()),
        "amountMultiple5d": _round(amount_multiple),
        "recoveryRatio": round(_clamp(recovery, 0, 1), 4),
    }


def intraday_recovery(row: dict[str, Any]) -> float:
    price = _safe_float(row.get("price"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    if price is None or high is None or low is None or high <= low:
        return 0.5
    return _clamp((price - low) / (high - low), 0, 1)


def feature_distance(current: dict[str, Any], historical: dict[str, Any]) -> float:
    fields = (
        ("change_pct", 10.0), ("turnover_rate", 20.0), ("volume_ratio", 4.0),
        ("breakout_20d_pct", 10.0), ("recovery_ratio", 1.0),
    )
    total = 0.0
    used = 0
    for key, scale in fields:
        left, right = _safe_float(current.get(key)), _safe_float(historical.get(key))
        if left is None or right is None:
            continue
        total += abs(left - right) / scale
        used += 1
    return total / used if used else 999.0


def candidate_changes(previous: dict[str, Any] | None, current: list[dict[str, Any]]) -> dict[str, Any]:
    prior_items = (previous or {}).get("candidates") or []
    prior_by_code = {str(item.get("code") or ""): item for item in prior_items}
    current_by_code = {str(item.get("code") or ""): item for item in current}
    items = []
    for code, item in current_by_code.items():
        old = prior_by_code.get(code)
        items.append({
            "code": code,
            "type": "added" if old is None else "changed" if int(old.get("rank") or 0) != int(item.get("rank") or 0) or abs(float(old.get("score") or 0) - float(item.get("score") or 0)) >= 0.01 else "unchanged",
            "rankChange": None if old is None else int(old.get("rank") or 0) - int(item.get("rank") or 0),
            "scoreChange": None if old is None else round(float(item.get("score") or 0) - float(old.get("score") or 0), 2),
        })
    removed = [code for code in prior_by_code if code not in current_by_code]
    return {
        "previousRunId": (previous or {}).get("run_id"),
        "added": [item["code"] for item in items if item["type"] == "added"],
        "removed": removed,
        "items": items,
        "changed": bool(removed or any(item["type"] != "unchanged" for item in items)),
    }


def classify_result_label(item: dict[str, Any]) -> str:
    outcome = item.get("outcome") or {}
    if outcome.get("final_result") in {"S", "A", "B", "N", "F"}:
        return str(outcome["final_result"])
    mfe = _safe_float(item.get("max_return_pct")) or 0.0
    mae = _safe_float(item.get("max_drawdown_pct")) or 0.0
    invalid_first = bool(outcome.get("invalidation_first"))
    close_return = _safe_float(outcome.get("close_return_pct"))
    if invalid_first or (close_return is not None and close_return <= -5) or mae <= -7:
        return "F"
    if bool(item.get("ignition_3d")):
        return "S"
    if mfe >= 7:
        return "A"
    if mfe >= 3:
        return "B"
    return "N"


def learn_from_outcomes(state: dict[str, Any], history: list[dict[str, Any]], today: date) -> dict[str, Any]:
    result = dict(state)
    official = [item for item in history if item.get("scan_slot") in OFFICIAL_CANDIDATE_SLOTS]
    days = sorted({str(item.get("observation_date")) for item in official if item.get("observation_date")})
    calibration_day = min(len(days), 10)
    stage = "calibration" if len(days) < 10 else "stable"
    result["stage"] = stage
    result["calibrationDay"] = calibration_day
    result["normalTradingDays"] = len(days)
    result["updatedAt"] = datetime.now(SHANGHAI_TZ).isoformat()
    limit = 2.0 if stage == "calibration" else 1.0
    pending: dict[str, float] = {}
    protection: dict[str, Any] = {}
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in official:
        if item.get("maturity_status") == "mature":
            by_model[str(item.get("model_branch") or "")].append(item)
    for model, samples in by_model.items():
        latest = samples[:5]
        f_count = sum(classify_result_label(item) == "F" for item in latest)
        if f_count >= 3:
            protection[model] = {"paused": True, "until": (today + timedelta(days=2)).isoformat(), "thresholdAdd": 5, "maxCandidates": 0}
        elif f_count >= 2:
            protection[model] = {"paused": False, "until": (today + timedelta(days=3)).isoformat(), "thresholdAdd": 5, "maxCandidates": 1}
        labels = [classify_result_label(item) for item in samples[:10]]
        if len(labels) >= 2 and labels.count("F") >= 2:
            pending[model] = limit
        elif len(labels) >= 5 and labels.count("S") + labels.count("A") >= 4:
            pending[model] = -limit
    result["pendingAdjustments"] = pending
    result["failureProtection"] = protection
    repeat_protection: dict[str, Any] = {}
    for item in official:
        if item.get("maturity_status") != "mature" or classify_result_label(item) != "F":
            continue
        try:
            observed = date.fromisoformat(str(item.get("observation_date"))[:10])
        except ValueError:
            continue
        if (today - observed).days <= 3:
            repeat_protection[str(item.get("code") or "").zfill(6)] = {
                "until": (observed + timedelta(days=3)).isoformat(),
                "reason": "失效后3个交易日内无新官方催化不得重复正式推荐",
            }
    result["repeatProtection"] = repeat_protection
    result["adjustmentStatus"] = "trial" if pending else "observe"
    return result


def bounded_offsets(current: dict[str, Any], adjustments: dict[str, Any]) -> dict[str, float]:
    result = {branch: float(current.get(branch, 0.0)) for branch in MODEL_BRANCHES}
    for branch, value in adjustments.items():
        if branch in result:
            result[branch] = round(_clamp(result[branch] + float(value), -8, 8), 2)
    return result


def calibration_view(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": state.get("stage", "calibration"),
        "calibrationDay": int(state.get("calibrationDay", 0)),
        "requiredDays": 10,
        "adjustmentStatus": state.get("adjustmentStatus", "observe"),
        "probabilitiesVisible": False,
        "historicalFrequencyRequiredDays": 20,
        "hardRiskRulesMutable": False,
    }


def probability_display_status(state: dict[str, Any], similar: dict[str, Any]) -> str:
    if int(state.get("availableTradingDays", 0)) < 20:
        return "withheld_insufficient_official_history"
    if int(state.get("historyPoints", 0)) < 60:
        return "withheld_insufficient_market_history"
    if similar.get("status") != "ready" or int(similar.get("sampleCount", 0)) < 30:
        return "withheld_insufficient_similar_samples"
    return "research_rate_available"


def descriptive_rate_interval(hits: int, count: int) -> list[float] | None:
    """Nominal Wilson interval for historical frequency, not a forecast interval."""
    if count < 30 or hits < 0 or hits > count:
        return None
    z = 1.959963984540054
    rate = hits / count
    denominator = 1 + z * z / count
    center = (rate + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denominator
    return [round(max(0, center - radius), 4), round(min(1, center + radius), 4)]


def classify_root_cause(item: dict[str, Any], label: str) -> str:
    model = str(item.get("model_branch") or "")
    features = item.get("features") or {}
    if label != "F":
        return "validated_signal" if label in {"S", "A", "B"} else "neutral_follow_through"
    if model == "M6-A" and (_safe_float(features.get("recovery_ratio")) or 0) >= 0.6:
        return "I1"
    if model == "M6-B":
        return "I2"
    if (_safe_float(features.get("recent_limitups_10d")) or 0) >= 2:
        return "I3"
    return "I4" if model.startswith("M6") else "high_score_false_positive"


def sample_type_for_label(label: str) -> str:
    return "hit" if label in {"S", "A", "B"} else "false_positive" if label == "F" else "correct_rejection"


def lesson_for(label: str, root_cause: str) -> str:
    if label == "F":
        return f"保留硬风控，并在challenger中针对{root_cause}小步提高门槛。"
    if label in {"S", "A", "B"}:
        return "保留可复核信号组合；需经周度样本门禁后才固化。"
    return "中性样本继续观察，不据此单日改权重。"


def _market_drift(rows: list[dict[str, Any]]) -> bool:
    # The current providers do not expose all seven regime fields reliably.
    # Treat broad dispersion in change/turnover as a conservative drift proxy.
    changes = [_safe_float((row.get("features") or {}).get("change_pct")) for row in rows]
    values = [value for value in changes if value is not None]
    return len(values) >= 5 and max(values) - min(values) >= 12


def _evidence(context: dict[str, Any], source_meta: dict[str, Any]) -> list[dict[str, Any]]:
    fetched_at = source_meta.get("fetched_at") or datetime.now(SHANGHAI_TZ).isoformat()
    result = []
    for key, title, url in (
        ("announcement", "公告摘要", "https://www.cninfo.com.cn/new/disclosure"),
        ("news", "新闻摘要", "https://finance.eastmoney.com/"),
        ("fund_flow", "资金流摘要", "https://quote.eastmoney.com/"),
    ):
        value = str(context.get(key) or "").strip()
        if value:
            result.append({"type": key, "title": title, "summary": value, "url": url, "fetched_at": fetched_at})
    return result


def _local_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None or not math.isfinite(float(value)) else round(float(value), digits)


def _mean(values: Iterable[float | None]) -> float | None:
    available = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return round(sum(available) / len(available), 4) if available else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
