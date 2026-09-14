"""Daily-owned Stock King picks and structured research advice."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np

from src.quant.service import get_quant_service, normalize_a_share_symbol
from src.quant.tier_models import TIER_MODEL_VERSION, get_tier_model_service
from src.services.screening_service import (
    ScreeningService,
    get_dsa_candidate_context,
    get_dsa_realtime_quote,
)
from src.services.stock_king_board import board_group as _board_group, metadata_board as _metadata_board
from src.services.history_loader import load_history_df
from src.quant.features import normalize_daily_frame

logger = logging.getLogger(__name__)


STOCK_KING_SCHEMA_VERSION = 4
CLASSIC_STOCK_KING_SCHEMA_VERSION = 3
STOCK_KING_METHODOLOGY_VERSION = "king-local-5d-v1"
CLASSIC_STOCK_KING_METHODOLOGY_VERSION = TIER_MODEL_VERSION
TIER_STRATEGIES = {
    "conservative": ("low_volatility_quality",),
    "regular": ("balanced_alpha", "momentum_quality"),
    "aggressive": ("volume_breakout", "capital_heat"),
}
TIER_LABELS = {
    "conservative": "保守推荐",
    "regular": "常规推荐",
    "aggressive": "激进推荐",
}
BOARD_LABELS = {"kechuang": "科创板", "nonKeChuang": "非科创板"}
MIN_PRICE_TICK = Decimal("0.01")


class StockKingService:
    def __init__(self, config: Any, db_manager: Any = None):
        self.config = config
        self.db_manager = db_manager
        root = Path(os.getenv("SCREENING_DATA_DIR") or "data/screening")
        root.mkdir(parents=True, exist_ok=True)
        self.cache_path = root / "stock-king-picks-local-v1.json"
        self._lock = threading.RLock()

    def picks(
        self,
        *,
        max_per_board: int = 5,
        force: bool = False,
        scan_slot: str = "live",
        top_n: int = 5,
        official: bool | None = None,
        allow_missed: bool = False,
        ai_config: dict | None = None,
    ) -> Dict[str, Any]:
        """Return Schema 4 with a fresh adaptive scan and v2.2 classic fields.

        The classic payload retains its daily cache.  Adaptive ``live`` scans are
        always recomputed and are excluded from training by the shared store.
        """
        classic = self._classic_picks(max_per_board=max_per_board, force=force)
        try:
            if self.db_manager is None:
                raise RuntimeError("adaptive picks require database manager")
            from src.services.yao_scout import AdaptiveKingService, YaoScoutService
            from src.services.yao_scout.local_opportunities import LocalOpportunityService

            yao = YaoScoutService(config=self.config, db_manager=self.db_manager)
            adaptive = LocalOpportunityService(yao, ai_config).run(
                scan_slot,
                top_n=top_n,
                official=official,
                allow_missed=allow_missed,
            )
        except Exception as exc:
            logger.exception("Adaptive King Picks scan unavailable: %s", exc)
            adaptive = {
                "schemaVersion": 1,
                "status": "unavailable",
                "scanSlot": scan_slot,
                "official": bool(official),
                "candidateCount": 0,
                "candidates": [],
                "changes": {"added": [], "removed": [], "items": [], "changed": False},
                "modelVersion": STOCK_KING_METHODOLOGY_VERSION,
                "dataQuality": {"critical_complete": False, "error": str(exc)},
                "riskNotice": "自适应行情或证据暂不可用；经典三档仍可查看。",
            }
        payload = dict(classic)
        payload.update({
            "schemaVersion": STOCK_KING_SCHEMA_VERSION,
            "methodologyVersion": STOCK_KING_METHODOLOGY_VERSION,
            "classicMethodologyVersion": CLASSIC_STOCK_KING_METHODOLOGY_VERSION,
            "adaptive": adaptive,
            "scanSlot": adaptive.get("scanSlot", scan_slot),
            "generatedAt": adaptive.get("generatedAt") or classic.get("generatedAt"),
            "cacheHit": bool(classic.get("cacheHit", False)),
        })
        for tier in (payload.get("tiers") or {}).values():
            if isinstance(tier, dict):
                tier.setdefault("methodologyVersion", CLASSIC_STOCK_KING_METHODOLOGY_VERSION)
        return payload

    def _classic_picks(self, *, max_per_board: int = 5, force: bool = False) -> Dict[str, Any]:
        max_per_board = min(max(int(max_per_board), 5), 50)
        if not force:
            cached = self._load_cache()
            if self._cache_is_current(cached):
                cached["cacheHit"] = True
                return cached

        service = ScreeningService(self.config, self.db_manager)
        pools: Dict[str, Dict[str, Dict[str, Any]]] = {
            tier: {} for tier in TIER_STRATEGIES
        }
        runs: list[Dict[str, Any]] = []
        errors: list[str] = []
        max_results = min(100, max(40, max_per_board * 6))
        for tier, strategies in TIER_STRATEGIES.items():
            for strategy in strategies:
                try:
                    run = service.screen(
                        strategy=strategy,
                        local_only=True,
                        market="cn",
                        max_results=max_results,
                        selection_seed=f"stock-king-{date.today().isoformat()}-{tier}-{strategy}",
                    )
                    diagnostics = _run_diagnostics(run)
                    diagnostics["tier"] = tier
                    runs.append(diagnostics)
                    for candidate in run.get("candidates") or []:
                        normalized = _normalized_candidate(candidate, strategy)
                        if normalized is None:
                            continue
                        previous = pools[tier].get(normalized["code"])
                        pools[tier][normalized["code"]] = _prefer_candidate(previous, normalized)
                except Exception as exc:  # A fallback style may fail without discarding the tier.
                    errors.append(f"{tier}/{strategy}: {exc}")
                    logger.warning("Stock King strategy %s/%s failed: %s", tier, strategy, exc)

        quant = get_quant_service()
        tier_models = getattr(quant, "tier_models", None) or get_tier_model_service()
        conservative_pool = [
            item for item in pools["conservative"].values() if _conservative_eligible(item)
        ]
        conservative_pool = tier_models.score_candidates("conservative", conservative_pool)
        regular_pool = tier_models.score_candidates("regular", list(pools["regular"].values()))
        aggressive_pool = self._score_aggressive_pool(
            list(pools["aggressive"].values()), tier_models, max_per_board
        )

        selections = _select_tiers(
            conservative_pool=conservative_pool,
            regular_pool=regular_pool,
            aggressive_pool=aggressive_pool,
            max_per_board=max_per_board,
        )
        tiers: Dict[str, Dict[str, Any]] = {}
        for tier in ("conservative", "regular", "aggressive"):
            tier_runs = [item for item in runs if item.get("tier") == tier]
            tier_payload: Dict[str, Any] = {
                "label": TIER_LABELS[tier],
                "description": _tier_description(tier),
                "kechuang": [],
                "nonKeChuang": [],
                "requestedPerBoard": max_per_board,
                "shortfall": {},
                "diagnostics": {
                    "screeningRuns": tier_runs,
                    "sourceErrors": [item for item in errors if item.startswith(f"{tier}/")],
                    "model": (tier_models.registry().get("objectives") or {}).get(tier) or {},
                },
            }
            for group in ("kechuang", "nonKeChuang"):
                selected = selections[tier][group]
                tier_payload[group] = [
                    self._decorate_pick(item, index + 1, group, tier, quant)
                    for index, item in enumerate(selected)
                ]
                tier_payload["shortfall"][group] = max(0, max_per_board - len(selected))
            tiers[tier] = tier_payload

        regular_tier = tiers["regular"]
        all_codes = {
            item["symbol"]["code"]
            for tier in tiers.values()
            for group in ("kechuang", "nonKeChuang")
            for item in tier[group]
        }
        payload = {
            "schemaVersion": CLASSIC_STOCK_KING_SCHEMA_VERSION,
            "methodologyVersion": CLASSIC_STOCK_KING_METHODOLOGY_VERSION,
            "asOfDate": date.today().isoformat(),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "tiers": tiers,
            # Compatibility contract: legacy clients continue to see the
            # balanced/regular two-board result.
            "kechuang": regular_tier["kechuang"],
            "nonKeChuang": regular_tier["nonKeChuang"],
            "requestedPerBoard": max_per_board,
            "candidateCount": len(all_codes),
            "shortfall": {
                "kechuang": regular_tier["shortfall"]["kechuang"],
                "nonKeChuang": regular_tier["shortfall"]["nonKeChuang"],
            },
            "diagnostics": {"screeningRuns": runs, "sourceErrors": errors},
            "cacheHit": False,
        }
        with self._lock:
            history = self._load_cache().get("history") or []
            prior = self._load_cache()
            if prior.get("asOfDate") and prior.get("asOfDate") != payload["asOfDate"]:
                history = ([{
                    "asOfDate": prior.get("asOfDate"),
                    "kechuang": prior.get("kechuang") or [],
                    "nonKeChuang": prior.get("nonKeChuang") or [],
                }] + history)[:60]
            payload["history"] = history
            self._write_cache(payload)
        return payload

    def advice(self, symbol_code: str, research_note: Optional[Dict[str, str]] = None, technical_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        symbol = normalize_a_share_symbol(symbol_code)
        if not symbol:
            return {
                "summary": "首版量化建议只支持 A 股；港股和美股仍可在 K 线研究中查看。",
                "dataSufficient": False,
                "bullArguments": [], "bearArguments": [],
                "keyRisks": ["prediction_only_supports_a_shares"],
                "observationTriggers": [], "invalidationConditions": [],
            }
        code = symbol.split(".")[0]
        note = dict(research_note or {})
        context = get_dsa_candidate_context(code, include_news=True, include_fundamentals=True, mode="post_rank_full") or {}
        quant_service = get_quant_service()
        forecasts = quant_service.forecast_all_horizons(symbol)
        try:
            tier_service = getattr(quant_service, "tier_models", None) or get_tier_model_service()
            tier_analysis = tier_service.analyze_symbol(
                symbol,
                stock_name=str((context.get("quote") or {}).get("name") or ""),
                candidate={"dsa_context": context},
            )
        except Exception as exc:  # Analysis failure must not erase the existing advice page.
            logger.warning("Three-tier advice analysis unavailable for %s: %s", symbol, exc)
            tier_analysis = {"available": False, "reason": f"tier_analysis_unavailable:{type(exc).__name__}", "tiers": {}}
        available = [item for item in forecasts if item.get("available")]
        technical = dict(technical_snapshot or self._technical_snapshot(code))
        technical_available = bool(technical.get("available")) and _number(technical.get("close")) > 0
        bull: list[str] = []
        bear: list[str] = []
        risks: list[str] = []
        observation: list[str] = []
        invalidation: list[str] = []

        if technical_available:
            trend = str(technical.get("trend") or "均线交织")
            ma5, ma10 = _number(technical.get("ma5")), _number(technical.get("ma10"))
            ma20, ma60 = _number(technical.get("ma20")), _number(technical.get("ma60"))
            close = _number(technical.get("close"))
            if trend == "多头排列":
                bull.append(f"均线呈多头排列：收盘 {close:.2f} > MA5 {ma5:.2f} > MA10 {ma10:.2f} > MA20 {ma20:.2f} > MA60 {ma60:.2f}。")
            elif trend == "空头排列":
                bear.append(f"均线呈空头排列：收盘 {close:.2f} 低于短中期均线，趋势仍偏弱。")
            elif technical.get("aboveMa20"):
                bull.append(f"收盘 {close:.2f} 位于 MA20 {ma20:.2f} 上方，但均线仍交织，尚非完整多头排列。")
            else:
                bear.append(f"收盘 {close:.2f} 未站上 MA20 {ma20:.2f}，趋势确认不足。")

            return20 = _number(technical.get("return20"))
            (bull if return20 > 0 else bear).append(f"近 20 个交易日收益为 {return20 * 100:.2f}%。")

            macd_dif = _number(technical.get("macdDif"))
            macd_dea = _number(technical.get("macdDea"))
            macd_histogram = _number(technical.get("macdHistogram"))
            if technical.get("macdBullish") and macd_histogram > 0:
                bull.append(f"MACD DIF {macd_dif:.3f} 高于 DEA {macd_dea:.3f}，柱值 {macd_histogram:.3f}，动量偏多。")
            else:
                bear.append(f"MACD DIF {macd_dif:.3f} / DEA {macd_dea:.3f} / 柱值 {macd_histogram:.3f}，尚未形成多头动量确认。")

            rsi14 = _number(technical.get("rsi14"), 50.0)
            if rsi14 >= 70:
                risks.append(f"RSI14 为 {rsi14:.1f}，处于偏热区，追高需防短线回撤。")
            elif rsi14 <= 30:
                risks.append(f"RSI14 为 {rsi14:.1f}，处于偏冷区；超卖不等于立即反转。")
            elif rsi14 >= 50:
                bull.append(f"RSI14 为 {rsi14:.1f}，强弱分界上方但未进入极端过热区。")
            else:
                bear.append(f"RSI14 为 {rsi14:.1f}，仍在强弱分界下方。")

            kdj_k = _number(technical.get("kdjK"), 50.0)
            kdj_d = _number(technical.get("kdjD"), 50.0)
            kdj_j = _number(technical.get("kdjJ"), 50.0)
            if kdj_k > kdj_d and kdj_j < 100:
                bull.append(f"KDJ K/D/J 为 {kdj_k:.1f}/{kdj_d:.1f}/{kdj_j:.1f}，短线动能偏强。")
            elif kdj_k < kdj_d:
                bear.append(f"KDJ K/D/J 为 {kdj_k:.1f}/{kdj_d:.1f}/{kdj_j:.1f}，短线动能偏弱。")
            if kdj_j > 100 or kdj_j < 0:
                risks.append(f"KDJ J 值为 {kdj_j:.1f}，处于极端区，信号容易钝化或反复。")

            boll_position = _number(technical.get("bollPosition"), 0.5)
            if boll_position >= 0.9:
                risks.append("价格接近或越过布林上轨，趋势强但短线扩张风险上升。")
            elif boll_position <= 0.1:
                risks.append("价格接近或跌破布林下轨，弱势与超跌风险并存。")

            volume_ratio = _number(technical.get("volumeRatio5To20"))
            if volume_ratio >= 1.2 and return20 > 0:
                bull.append(f"近 5 日均量为 20 日均量的 {volume_ratio:.2f} 倍，量价方向相互确认。")
            elif volume_ratio < 0.8 and return20 > 0:
                risks.append(f"价格上涨但近 5 日/20 日量比仅 {volume_ratio:.2f}，突破确认度有限。")

            atr_pct = _number(technical.get("atrPercent"))
            volatility = _number(technical.get("volatility20"))
            if atr_pct > 0.035 or volatility > 0.03:
                risks.append(f"ATR14/价格为 {atr_pct * 100:.2f}%，20 日收益波动为 {volatility * 100:.2f}%，波动风险较高。")

            support = _number(technical.get("support20"))
            resistance = _number(technical.get("resistance20"))
            if support > 0:
                observation.append(f"观察 {support:.2f} 附近的 20 日支撑是否有效；有效跌破后不再沿用当前技术结构。")
                invalidation.append(f"收盘有效跌破 20 日支撑 {support:.2f} 且两个交易日内无法收复。")
            if resistance > 0:
                observation.append(f"观察能否放量突破 20 日压力 {resistance:.2f}，避免把无量上冲当作有效突破。")
        else:
            risks.append("K 线历史不足或行情源不可用，技术指标层未参与本次建议。")

        for forecast in available:
            horizon = forecast.get("horizon")
            up = _number(forecast.get("probabilityUp"))
            down = _number(forecast.get("probabilityDown"))
            target = bull if up > down else bear
            target.append(f"{horizon} 日合格模型：上涨 {up * 100:.1f}% / 下跌 {down * 100:.1f}%。")

        if not available:
            risks.append("当前没有通过滚动样本外门槛的预测模型，不能使用方向概率。")
        risks.extend(str(item) for item in context.get("warnings") or [])
        if note.get("invalidation_risk"):
            risks.append("你的证伪条件：" + note["invalidation_risk"])
        observation.extend([
            "观察收盘价能否连续站稳 20 日均线。",
            "观察量能是否与价格突破同向，而非缩量脉冲。",
        ])
        if note.get("observation_plan"):
            observation.insert(0, "你的观察计划：" + note["observation_plan"])
        invalidation.extend([
            "跌破近期波段低点且两个交易日内无法收复。",
            "模型数据过期、覆盖率不足或冠军模型被停用。",
        ])
        if note.get("invalidation_risk"):
            invalidation.insert(0, note["invalidation_risk"])
        return {
            "symbol": symbol,
            "summary": "核心逻辑：先用可复核的行情与研究证据，再分别解释 SafeBound、BalancedRank、LimitPulse 三个独立预测目标。模型未发布时只显示规则观察分；单股不伪造横截面百分位。",
            "dataSufficient": technical_available and bool(context),
            "bullArguments": bull,
            "bearArguments": bear,
            "keyRisks": _dedupe(risks),
            "observationTriggers": _dedupe(observation),
            "invalidationConditions": _dedupe(invalidation),
            "logicLayers": [
                "K线技术层：MA5/10/20/60、MACD、RSI14、KDJ、布林带、ATR、量能、20日支撑/压力",
                "Daily 研究层：基本面、新闻、事件与风险覆盖",
                "三档量化层：稳健 20–60 日收益下界与风险、常规 5–20 日中性超额排名、激进 1–3 日首次触板风险率",
                "个人判断层：核心逻辑、观察计划和证伪条件只读引用",
                "解释层：配置的 AI 只组织证据，不修改行情、指标、概率或排名",
            ],
            "technicalIndicatorsUsed": [
                "MA5", "MA10", "MA20", "MA60", "MACD", "RSI14", "KDJ",
                "BOLL", "ATR14", "VOL_MA5/20", "RETURN_5/20/60", "SUPPORT/RESISTANCE_20",
            ],
            "evidence": {
                "goTechnical": technical,
                "dailyContext": context,
                "quantForecasts": forecasts,
                "tierModelAnalysis": tier_analysis,
                "userResearchNote": note,
            },
            "tierModelAnalysis": tier_analysis,
            "calculationNotes": [
                "档位分数是当日同板块候选百分位，不是收益率或成功概率。",
                "AI 建议中的单股页面展示原始模型信号、门槛、已学习权重和公式；股票不在当日 King 池时 tierScore 留空。",
                "只有通过滚动样本外发布门槛的激进模型才展示触板概率；否则只显示规则观察分与降级原因。",
            ],
            "explanationMode": "deterministic_structured",
        }

    def _score_aggressive_pool(
        self,
        candidates: list[Dict[str, Any]],
        tier_models: Any,
        max_per_board: int,
    ) -> list[Dict[str, Any]]:
        ordered = sorted(candidates, key=_candidate_score, reverse=True)
        attempts = {"kechuang": 0, "non-kechuang": 0}
        tradable = {"kechuang": 0, "non-kechuang": 0}
        scored: list[Dict[str, Any]] = []
        max_attempts = max_per_board * 4
        score_target = max_per_board * 2
        for candidate in ordered:
            group = _board_group(candidate)
            if attempts[group] >= max_attempts or tradable[group] >= score_target:
                continue
            attempts[group] += 1
            buyability = _buyability(candidate)
            if buyability["status"] != "tradable_at_generation":
                continue
            enriched = dict(candidate)
            enriched["buyability"] = buyability
            scored.append(enriched)
            tradable[group] += 1
        modeled = tier_models.score_candidates("aggressive", scored)
        for item in modeled:
            item["potentialScore"] = item.get("tierScore")
            item["potentialReasons"] = _aggressive_model_reasons(item)
            item["potentialDegradedReasons"] = item.get("degradedReasons") or []
        return sorted(modeled, key=lambda item: (_number(item.get("tierScore")), _candidate_score(item)), reverse=True)

    def _decorate_pick(
        self,
        candidate: Dict[str, Any],
        rank: int,
        group: str,
        tier: str,
        quant: Any,
    ) -> Dict[str, Any]:
        code = candidate["code"]
        name = candidate.get("name") or code
        forecasts = candidate.get("forecasts") or []
        available = [item for item in forecasts if item.get("available")]
        if available:
            summary = "；".join(
                f"{item['horizon']}日 涨{_number(item.get('probabilityUp')) * 100:.1f}%/跌{_number(item.get('probabilityDown')) * 100:.1f}%"
                for item in available
            )
        else:
            summary = "预测暂不可用"
        context = candidate.get("dsa_context") or {}
        degraded = list(context.get("warnings") or [])
        tier_score = _number(candidate.get("tierScore"))
        tier_reasons = _tier_reasons(candidate, tier)
        payload = {
            "symbol": {
                "code": code, "name": name, "market": "cn",
                "exchange": code.split(".")[-1], "board": _metadata_board(candidate),
            },
            "boardGroup": group,
            "rank": rank,
            "score": candidate.get("score"),
            "strategy": candidate.get("strategy"),
            "strategySources": candidate.get("strategySources") or [candidate.get("strategy")],
            "recommendationTier": tier,
            "tierScore": round(tier_score, 4),
            "scoreMeaning": candidate.get("scoreMeaning") or "规则观察分的当日候选百分位",
            "tierReasons": tier_reasons,
            "screeningSource": candidate.get("reason") or candidate.get("strategy"),
            "riskDecision": "通过硬过滤与风险覆盖" if not candidate.get("risk_flags") else "；".join(candidate.get("risk_flags") or []),
            "degradedReasons": _dedupe(degraded),
            "forecastSummary": summary,
            "forecasts": forecasts,
            "industry": candidate.get("industry") or "",
            "dsaAnalysisSummary": candidate.get("dsa_analysis_summary") or "",
            "price": _number(candidate.get("price")),
            "changePct": _number(candidate.get("change_pct")),
        }
        if tier == "aggressive":
            payload.update({
                "potentialScore": round(tier_score, 4),
                "potentialHorizon": "1-3_trading_days",
                "potentialReasons": candidate.get("potentialReasons") or [],
                "potentialDegradedReasons": candidate.get("potentialDegradedReasons") or [],
                "buyability": candidate.get("buyability") or {},
            })
        for field in (
            "modelStatus", "modelVersion", "trainedThrough", "calibratedThrough",
            "confidenceGrade", "learnedModelWeights", "topContributors",
            "returnLowerBound20d", "returnLowerBound60d", "predictedVolatility20d",
            "predictedVolatilityUpper20d", "drawdownQuantile20d", "riskGateResults",
            "excessRank5d", "excessRank20d", "rawRankSignal5d", "rawRankSignal20d",
            "industryNeutral", "riskAdjustedSignal", "expectedHoldingRange",
            "touchProbability1d", "touchProbability3d", "touchHazards",
            "probabilityInterval", "baseRate3d", "probabilityLift",
        ):
            if field in candidate:
                payload[field] = candidate[field]
        payload["degradedReasons"] = _dedupe([
            *payload.get("degradedReasons", []), *(candidate.get("degradedReasons") or [])
        ])
        return payload

    @staticmethod
    def _technical_snapshot(code: str) -> Dict[str, Any]:
        raw, source = load_history_df(code, days=160)
        frame = normalize_daily_frame(raw) if raw is not None else normalize_daily_frame([])
        if len(frame) < 30:
            return {"available": False, "source": source}
        close = frame["close"].astype(float)
        returns = close.pct_change(fill_method=None)
        ma20 = float(close.tail(20).mean())
        return {
            "available": True, "source": source, "close": float(close.iloc[-1]),
            "ma20": ma20, "ma60": float(close.tail(60).mean()) if len(close) >= 60 else None,
            "aboveMa20": bool(close.iloc[-1] > ma20),
            "return20": float(close.iloc[-1] / close.iloc[-21] - 1.0),
            "volatility20": float(returns.tail(20).std(ddof=0)),
            "dataDate": frame["date"].iloc[-1].date().isoformat(),
        }

    def _load_cache(self) -> Dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _cache_is_current(cached: Dict[str, Any]) -> bool:
        return (
            cached.get("schemaVersion") == CLASSIC_STOCK_KING_SCHEMA_VERSION
            and cached.get("methodologyVersion") == CLASSIC_STOCK_KING_METHODOLOGY_VERSION
            and cached.get("asOfDate") == date.today().isoformat()
            and isinstance(cached.get("tiers"), dict)
        )

    def _write_cache(self, payload: Dict[str, Any]) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(self.cache_path)


def _normalized_candidate(candidate: Any, strategy: str) -> Optional[Dict[str, Any]]:
    if not isinstance(candidate, dict):
        return None
    normalized = dict(candidate)
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    code = normalize_a_share_symbol(
        candidate.get("code") or raw.get("code") or candidate.get("symbol") or raw.get("symbol") or ""
    )
    if not code:
        return None
    normalized["code"] = code
    normalized["name"] = candidate.get("name") or raw.get("name") or code
    normalized["strategy"] = strategy
    normalized["strategySources"] = _dedupe([*(candidate.get("strategySources") or []), strategy])
    normalized["raw"] = raw
    for key in ("price", "change_pct", "amount", "industry", "factor_scores", "dsa_context"):
        if normalized.get(key) in (None, "", {}):
            normalized[key] = raw.get(key)
    if _risk_excluded(normalized):
        return None
    return normalized


def _prefer_candidate(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    if previous is None:
        return current
    winner, other = (current, previous) if _candidate_score(current) > _candidate_score(previous) else (previous, current)
    merged = dict(winner)
    merged["strategySources"] = _dedupe([
        *(previous.get("strategySources") or [previous.get("strategy")]),
        *(current.get("strategySources") or [current.get("strategy")]),
    ])
    if not merged.get("dsa_context") and other.get("dsa_context"):
        merged["dsa_context"] = other["dsa_context"]
    return merged


def _candidate_value(candidate: Dict[str, Any], *keys: str) -> Any:
    sources = [candidate]
    raw = candidate.get("raw")
    if isinstance(raw, dict):
        sources.append(raw)
    context = candidate.get("dsa_context")
    if isinstance(context, dict):
        quote = context.get("quote")
        if isinstance(quote, dict):
            sources.append(quote)
        fundamentals = context.get("fundamentals")
        if isinstance(fundamentals, dict):
            sources.append(fundamentals)
    for source in sources:
        for key in keys:
            if source.get(key) is not None:
                return source.get(key)
    return None


def _optional_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _candidate_score(candidate: Dict[str, Any]) -> float:
    value = _candidate_score_optional(candidate)
    return 0.0 if value is None else value


def _tier_score(candidate: Dict[str, Any]) -> float:
    for key in ("tierScore", "potentialScore"):
        value = _optional_number(candidate.get(key))
        if value is not None:
            return min(100.0, max(0.0, value))
    return _candidate_score(candidate)


def _candidate_score_optional(candidate: Dict[str, Any]) -> Optional[float]:
    for key in ("score", "final_score", "screen_score"):
        value = _optional_number(_candidate_value(candidate, key))
        if value is not None:
            return min(100.0, max(0.0, value))
    return None


def _factor_score(candidate: Dict[str, Any], name: str) -> Optional[float]:
    factors = _candidate_value(candidate, "factor_scores")
    if not isinstance(factors, dict):
        return None
    value = _optional_number(factors.get(name))
    return None if value is None else min(100.0, max(0.0, value))


def _conservative_eligible(candidate: Dict[str, Any]) -> bool:
    """Apply non-negotiable low-volatility quality gates."""
    change_60d = _optional_number(_candidate_value(candidate, "change_60d"))
    volatility = _optional_number(_candidate_value(candidate, "volatility_20d_pct"))
    drawdown = _optional_number(_candidate_value(candidate, "max_drawdown_20d_pct"))
    atr = _optional_number(_candidate_value(candidate, "atr_20_pct"))
    quality = _optional_number(_candidate_value(candidate, "daily_quality_score"))
    above_ma20 = _candidate_value(candidate, "price_above_ma20") is True
    required = (change_60d, volatility, drawdown, atr, quality)
    if any(value is None for value in required):
        return False
    return bool(
        0 < change_60d <= 35
        and above_ma20
        and volatility <= 30
        and drawdown >= -8
        and atr <= 4.5
        and quality >= 85
    )


def _aggressive_potential(candidate: Dict[str, Any], forecasts: Any) -> Dict[str, Any]:
    components: list[tuple[str, float, Optional[float]]] = [
        ("原策略最终分", 0.35, _candidate_score_optional(candidate)),
        ("动量", 0.20, _factor_score(candidate, "momentum")),
        ("活跃度", 0.20, _factor_score(candidate, "activity")),
        ("技术信号", 0.10, _optional_number(_candidate_value(candidate, "signal_score"))),
        (
            "题材/板块热度",
            0.10,
            _first_number(
                _factor_score(candidate, "theme_heat"),
                _candidate_value(candidate, "board_heat_score", "concept_heat_score", "industry_heat_score"),
            ),
        ),
        ("流动性", 0.05, _factor_score(candidate, "liquidity")),
    ]
    usable = [(label, weight, min(100.0, max(0.0, float(value)))) for label, weight, value in components if value is not None]
    total_weight = sum(weight for _, weight, _ in usable)
    deterministic = sum(weight * value for _, weight, value in usable) / total_weight if total_weight else 0.0

    quant_parts: list[tuple[float, float, int]] = []
    for item in forecasts if isinstance(forecasts, list) else []:
        if not isinstance(item, dict) or item.get("available") is not True:
            continue
        horizon = int(_number(item.get("horizon")))
        probability = _optional_number(item.get("probabilityUp"))
        if horizon not in {1, 5} or probability is None:
            continue
        # Some providers expose an explicit gate result. An explicit false is
        # authoritative; an omitted flag keeps backwards-compatible qualified results.
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        if item.get("qualified") is False or metrics.get("qualified") is False:
            continue
        quant_parts.append((0.70 if horizon == 1 else 0.30, min(1.0, max(0.0, probability)), horizon))
    quant_weight = sum(weight for weight, _, _ in quant_parts)
    quant_support = (
        sum(weight * probability * 100 for weight, probability, _ in quant_parts) / quant_weight
        if quant_weight else None
    )
    score = deterministic if quant_support is None else deterministic * 0.90 + quant_support * 0.10
    reasons = [
        f"{label} {value:.1f} 分（有效权重 {weight / total_weight * 100:.0f}%）"
        for label, weight, value in sorted(usable, key=lambda item: item[1] * item[2], reverse=True)[:3]
    ] if total_weight else []
    degraded: list[str] = []
    if quant_support is None:
        degraded.append("无通过样本外门槛的 1 日/5 日量化结果，潜力分仅使用确定性因子且不扣分。")
    else:
        horizons = "、".join(f"{horizon}日" for _, _, horizon in sorted(quant_parts, key=lambda item: item[2]))
        reasons.append(f"{horizons}合格量化支持 {quant_support:.1f} 分（总权重 10%）")
    return {
        "score": round(min(100.0, max(0.0, score)), 4),
        "deterministicScore": round(deterministic, 4),
        "quantSupportScore": None if quant_support is None else round(quant_support, 4),
        "reasons": reasons,
        "degradedReasons": degraded,
    }


def _first_number(*values: Any) -> Optional[float]:
    numbers = [number for number in (_optional_number(value) for value in values) if number is not None]
    return max(numbers) if numbers else None


def _quote_value(quote: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if quote.get(key) is not None:
            return quote.get(key)
    return None


def _limit_ratio(candidate: Dict[str, Any]) -> float:
    board = _metadata_board(candidate)
    if board.startswith(("star", "chinext")):
        return 0.20
    if board.startswith("bse"):
        return 0.30
    return 0.10


def _buyability(candidate: Dict[str, Any]) -> Dict[str, Any]:
    code = candidate.get("code") or ""
    checked_at = datetime.now(timezone.utc).isoformat()
    context = candidate.get("dsa_context") if isinstance(candidate.get("dsa_context"), dict) else {}
    cached_quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
    quote: Dict[str, Any] = {}
    source = "unavailable"
    if cached_quote and _quote_value(cached_quote, "is_stale", "isStale") is not True:
        quote = dict(cached_quote)
        source = "generation_context"
    else:
        try:
            refreshed = get_dsa_realtime_quote(code)
            if hasattr(refreshed, "to_dict"):
                refreshed = refreshed.to_dict()
            if isinstance(refreshed, dict):
                quote = refreshed
                source = "refreshed_realtime"
        except Exception as exc:  # noqa: BLE001 - a quote outage makes the pick ineligible.
            logger.info("Stock King realtime quote unavailable for %s: %s", code, exc)

    def result(status: str, **extra: Any) -> Dict[str, Any]:
        return {
            "status": status,
            "checkedAt": checked_at,
            "quoteSource": source,
            "tradableAtGeneration": status == "tradable_at_generation",
            **extra,
        }

    if not quote:
        return result("quote_unavailable", reason="无法取得可靠实时行情")
    if _quote_value(quote, "is_stale", "isStale") is True:
        return result("stale_quote", reason="行情已标记过期")
    trade_status = str(_quote_value(quote, "trade_status", "tradeStatus", "status") or "").strip().lower()
    if _quote_value(quote, "suspended", "is_suspended", "isSuspended") is True or trade_status in {"suspended", "停牌"}:
        return result("suspended", reason="生成时停牌")

    price = _optional_number(_quote_value(quote, "price", "latest_price", "latestPrice"))
    amount = _optional_number(_quote_value(quote, "amount", "turnover_amount", "turnoverAmount"))
    if price is None or price <= 0:
        return result("invalid_price", reason="生成时价格无效")
    if amount is None:
        amount = _optional_number(_candidate_value(candidate, "amount"))
    if amount is None or amount <= 0:
        return result("zero_turnover", reason="生成时成交额为零")

    previous_close = _optional_number(_quote_value(
        quote, "pre_close", "preClose", "previous_close", "previousClose", "settlement"
    ))
    if previous_close is None or previous_close <= 0:
        return result("missing_previous_close", reason="缺少可靠前收盘价", currentPrice=round(price, 4))
    ratio = _limit_ratio(candidate)
    limit_price = (Decimal(str(previous_close)) * (Decimal("1") + Decimal(str(ratio)))).quantize(
        MIN_PRICE_TICK, rounding=ROUND_HALF_UP
    )
    current_price = Decimal(str(price)).quantize(MIN_PRICE_TICK, rounding=ROUND_HALF_UP)
    permission_board = _metadata_board(candidate)
    permission_required = permission_board.startswith(("star", "chinext", "bse"))
    common = {
        "currentPrice": float(current_price),
        "previousClose": round(previous_close, 4),
        "limitUpPrice": float(limit_price),
        "limitPercent": int(ratio * 100),
        "minimumPriceTick": float(MIN_PRICE_TICK),
        "permissionRequired": permission_required,
        "permissionBoard": permission_board if permission_required else "main",
    }
    if current_price > limit_price - MIN_PRICE_TICK:
        return result("at_limit_up", reason="当前价未低于涨停价一个最小价位", **common)
    distance = max(0.0, (float(limit_price) / float(current_price) - 1.0) * 100)
    return result(
        "tradable_at_generation",
        reason="生成时行情显示未停牌、未封板且有成交",
        distanceToLimitUpPct=round(distance, 4),
        **common,
    )


def _select_tiers(
    *,
    conservative_pool: list[Dict[str, Any]],
    regular_pool: list[Dict[str, Any]],
    aggressive_pool: list[Dict[str, Any]],
    max_per_board: int,
) -> Dict[str, Dict[str, list[Dict[str, Any]]]]:
    conservative = {item["code"]: item for item in conservative_pool}
    aggressive = {item["code"]: item for item in aggressive_pool}
    for code in set(conservative) & set(aggressive):
        conservative_score = _tier_score(conservative[code])
        aggressive_score = _tier_score(aggressive[code])
        if aggressive_score > conservative_score:
            conservative.pop(code, None)
        else:  # Ties favor the lower-risk tier.
            aggressive.pop(code, None)

    selected: Dict[str, Dict[str, list[Dict[str, Any]]]] = {
        tier: {"kechuang": [], "nonKeChuang": []}
        for tier in ("conservative", "regular", "aggressive")
    }

    def group_key(candidate: Dict[str, Any]) -> str:
        return "kechuang" if _board_group(candidate) == "kechuang" else "nonKeChuang"

    for tier, values, score_fn in (
        ("conservative", list(conservative.values()), _tier_score),
        ("aggressive", list(aggressive.values()), _tier_score),
    ):
        for group in ("kechuang", "nonKeChuang"):
            ordered = sorted((item for item in values if group_key(item) == group), key=score_fn, reverse=True)
            selected[tier][group] = _industry_diverse(ordered, max_per_board)

    used_codes = {
        item["code"]
        for tier in ("conservative", "aggressive")
        for group in ("kechuang", "nonKeChuang")
        for item in selected[tier][group]
    }
    regular_by_code: Dict[str, Dict[str, Any]] = {}
    for item in regular_pool:
        if item["code"] not in used_codes:
            regular_by_code[item["code"]] = _prefer_candidate(regular_by_code.get(item["code"]), item)
    for group in ("kechuang", "nonKeChuang"):
        ordered = sorted(
            (item for item in regular_by_code.values() if group_key(item) == group),
            key=_tier_score,
            reverse=True,
        )
        selected["regular"][group] = _industry_diverse(ordered, max_per_board)
    return selected


def _tier_description(tier: str) -> str:
    return {
        "conservative": "低波质量硬门槛：近 60 日温和正增长、站上 MA20、严格控制波动和回撤。",
        "regular": "均衡多因子为主、趋势质量补位，兼顾估值、趋势、流动性、稳定性与行业分散。",
        "aggressive": "放量突破与资金热度候选，排序未来 1–3 个交易日的涨停潜力；生成时未封板仅为可交易性门槛。",
    }[tier]


def _tier_reasons(candidate: Dict[str, Any], tier: str) -> list[str]:
    if tier == "aggressive":
        return list(candidate.get("potentialReasons") or [])
    if tier == "conservative":
        return [
            f"近 60 日涨幅 {_number(_candidate_value(candidate, 'change_60d')):.1f}% 且站上 MA20",
            f"年化波动 {_number(_candidate_value(candidate, 'volatility_20d_pct')):.1f}% / 20 日最大回撤 {_number(_candidate_value(candidate, 'max_drawdown_20d_pct')):.1f}%",
            f"ATR {_number(_candidate_value(candidate, 'atr_20_pct')):.1f}% / 日线质量 {_number(_candidate_value(candidate, 'daily_quality_score')):.1f} 分",
        ]
    factors = _candidate_value(candidate, "factor_scores")
    labels = {"value": "估值", "momentum": "趋势", "liquidity": "流动性", "stability": "稳定性", "activity": "活跃度"}
    reasons: list[str] = []
    if isinstance(factors, dict):
        for name, value in sorted(
            ((name, _optional_number(value)) for name, value in factors.items()),
            key=lambda item: item[1] if item[1] is not None else -1,
            reverse=True,
        ):
            if value is not None and name in labels:
                reasons.append(f"{labels[name]} {value:.1f} 分")
            if len(reasons) == 3:
                break
    if not reasons and candidate.get("reason"):
        reasons.append(str(candidate["reason"]))
    return reasons


def _aggressive_model_reasons(candidate: Dict[str, Any]) -> list[str]:
    if candidate.get("modelStatus") != "qualified":
        return ["模型尚未通过发布门槛，当前仅按规则观察分排序。"]
    p1 = _optional_number(candidate.get("touchProbability1d"))
    p3 = _optional_number(candidate.get("touchProbability3d"))
    base = _optional_number(candidate.get("baseRate3d"))
    lift = _optional_number(candidate.get("probabilityLift"))
    reasons = []
    if p1 is not None and p3 is not None:
        reasons.append(f"校准后首次触板概率：1 日 {p1 * 100:.2f}% / 3 日 {p3 * 100:.2f}%")
    if base is not None and lift is not None:
        reasons.append(f"同板块 3 日基准率 {base * 100:.2f}%，相对提升 {lift:.2f} 倍")
    hazards = candidate.get("touchHazards")
    if isinstance(hazards, dict) and hazards:
        reasons.append("条件风险率 h1/h2/h3：" + "/".join(
            f"{_number(hazards.get(str(day))) * 100:.2f}%" for day in (1, 2, 3)
        ))
    return reasons or ["通过首次触板概率模型的滚动样本外发布门槛。"]


def _risk_excluded(candidate: Dict[str, Any]) -> bool:
    name = str(candidate.get("name") or "").upper()
    if "ST" in name or "退市" in name or "整理" in name or name.startswith(("N", "C")):
        return True
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    if _candidate_value(candidate, "suspended", "is_suspended") is True or str(
        _candidate_value(candidate, "trade_status", "tradeStatus") or ""
    ).lower() in {"suspended", "停牌"}:
        return True
    list_date = str(_candidate_value(candidate, "list_date", "listing_date", "listDate") or "").strip()
    if list_date:
        try:
            parsed = datetime.strptime(list_date[:8], "%Y%m%d").date() if "-" not in list_date else date.fromisoformat(list_date[:10])
            if (date.today() - parsed).days < 180:
                return True
        except ValueError:
            pass
    return False


def _industry_diverse(candidates: list[Dict[str, Any]], limit: int) -> list[Dict[str, Any]]:
    selected = []
    counts: Dict[str, int] = {}
    for item in candidates:
        industry = str(item.get("industry") or item.get("llm_sector") or f"未知:{item.get('code')}")
        if counts.get(industry, 0) >= 2:
            continue
        selected.append(item)
        counts[industry] = counts.get(industry, 0) + 1
        if len(selected) == limit:
            return selected
    return selected


def _run_diagnostics(run: Dict[str, Any]) -> Dict[str, Any]:
    return {key: run.get(key) for key in (
        "run_id", "strategy", "snapshot_count", "snapshot_source", "after_filter_count",
        "candidate_count", "ranking_mode", "degradation", "warnings", "source_errors",
    )}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
