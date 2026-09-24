"""Persist source-backed observation reviews without creating trade outcomes.

These are changes between two observed prices, never realised returns or model
training labels. Repeated data gaps become verification reminders only.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
import math
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
STATE_KEY = "king-local-observation-review-v1"
SCAN_MODES = {"king_live", "king_0920", "king_0922", "king_1030", "king_1455"}


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(TZ) if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _price(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _quote(quote, code, latest, earliest, *, post_close_reference=False):
    if not isinstance(quote, dict):
        quote = {}
    stamp = _stamp(quote.get("provider_timestamp") or quote.get("source_time"))
    price = _price(quote.get("price"))
    gaps = []
    if str(quote.get("code") or "").split(".")[0] != code:
        gaps.append("报价代码未核验")
    if not quote.get("source"):
        gaps.append("报价来源未知")
    if stamp is None:
        gaps.append("原始报价时间未知")
    elif stamp.date() != latest.date() or not earliest <= stamp <= latest:
        gaps.append("报价时间不在核验窗口")
    if price is None:
        gaps.append("报价价格缺失")
    metadata = quote.get("public_quote_metadata") or {}
    # The public adapter calls every non-continuous quote "stale" for trading.
    # Only its explicitly assessed same-day post-close reference can override
    # that flag here. Price conflicts/invalid books/unknown issues still fail.
    allowed_reference_issues = {"stale_quote", "request_phase_post_close", "source_phase_post_close",
        "post_close_cannot_reconstruct_1455", "unencrypted_public_source_fallback",
        "cross_source_asynchronous_reference"}
    issues = metadata.get("issues") or []
    assessed_reference = (post_close_reference and metadata.get("status") == "reference_only"
        and metadata.get("phase") == "post_close" and metadata.get("same_market_date") is True
        and metadata.get("code") == code
        and {"request_phase_post_close", "source_phase_post_close"}.issubset(issues)
        and not set(issues) - allowed_reference_issues
        and (metadata.get("cross_source_check") or {}).get("prices_agree") is not False)
    if post_close_reference and metadata and (set(issues) - allowed_reference_issues
            or (metadata.get("cross_source_check") or {}).get("prices_agree") is False):
        gaps.append("公开源核验存在冲突或无效字段")
    if quote.get("is_stale") and not assessed_reference:
        gaps.append("来源标记陈旧")
    return {"price": price, "source_time": stamp.isoformat() if stamp else None,
            "source": quote.get("source")}, gaps


def _load_state(db):
    getter = getattr(db, "get_yao_adaptive_state", None)
    if not callable(getter):
        return {}
    state = getter(STATE_KEY)
    return state if isinstance(state, dict) else {}


def review_local_observations(db, quote_fetcher, now, history=None, *, clock=None):
    """Review today's saved scans, preserving unknown prices and provenance.

    ``quote_fetcher`` is injected and called at most once for each code. Its
    timestamp must come from the provider. A post-close reference may use a
    timestamp from 15:00 through its actual check time; it is not labelled the
    closing price. Inject ``clock`` alongside the fetcher to audit elapsed time.
    """
    if now.tzinfo is None:
        raise ValueError("复盘时钟必须包含时区")
    now = now.astimezone(TZ)
    clock = clock or (lambda: now)
    if now.time() < time(15, 0):
        return {"status": "not_due", "message": "收盘后才生成当日观察复盘"}
    rows = history if history is not None else db.list_yao_runs(limit=100, include_result=True)
    observations, seen, quotes = [], set(), {}
    code_counts, gap_counts, windvane_counts = Counter(), Counter(), Counter()
    for row in rows:
        result = row.get("result") or {}
        if row.get("mode", result.get("mode")) not in SCAN_MODES:
            continue
        run_id = row.get("run_id") or result.get("run_id")
        if not run_id:
            continue
        run_stamp = _stamp(result.get("as_of") or row.get("as_of"))
        profile_rows = result.get('profileCandidates') or {}
        candidates = list(result.get('candidates') or []) + list(result.get('windvanes') or [])
        candidates += list(result.get('precisionResearch') or []) + list(result.get('precisionWatchlist') or [])
        candidates += list(result.get('evidenceInsufficient') or [])
        for members in profile_rows.values():
            candidates += members
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            code = str(candidate.get("code") or "")
            if not re.fullmatch(r"\d{6}", code) or (run_id, code) in seen:
                continue
            decision = _stamp(candidate.get("decision_at"))
            anchor = decision or run_stamp
            if anchor is None or anchor.date() != now.date() or anchor > now:
                continue
            seen.add((run_id, code))
            code_counts[code] += 1
            if candidate.get("status") == "windvane":
                windvane_counts[code] += 1
            reference = _price(candidate.get("referencePrice"))
            baseline, gaps = _quote(candidate.get("quote"), code, decision or anchor,
                                    (decision or anchor) - timedelta(seconds=30))
            if decision is None:
                gaps.append("原观察决策时间未知")
            if reference is None:
                gaps.append("原观察价格缺失")
            elif baseline["price"] is not None and not math.isclose(reference, baseline["price"], abs_tol=1e-6):
                gaps.append("原观察价格与源报价不一致")
            if code not in quotes:
                try:
                    fetched_quote = quote_fetcher(code)
                except Exception:
                    fetched_quote = {}
                checked_at = clock()
                if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
                    raise ValueError("复盘检查时钟必须包含时区")
                quotes[code] = (fetched_quote, checked_at.astimezone(TZ))
            fetched_quote, checked_at = quotes[code]
            review_quote, review_gaps = _quote(fetched_quote, code, checked_at,
                max(anchor, now.replace(hour=15, minute=0, second=0, microsecond=0)), post_close_reference=True)
            review_quote["checked_at"] = checked_at.isoformat()
            if checked_at < now or checked_at.date() != now.date():
                review_gaps.append("复盘检查时钟倒退或跨日")
            gaps = list(dict.fromkeys(gaps + review_gaps))
            gap_counts[code] += int(bool(gaps))
            observations.append({"run_id": run_id, "code": code, "name": candidate.get("name") or code,
                'selected_profiles': [key for key, members in profile_rows.items()
                                      if any(p.get('code') == code for p in members)],
                'entry_decisions': (candidate.get('precisionDecision') or {}).get('profiles', {}),
                "decision_at": decision.isoformat() if decision else None,
                "reference_price": reference, "baseline_quote": baseline, "review_quote": review_quote,
                "observation_change_pct": round((review_quote["price"] / reference - 1) * 100, 4) if not gaps else None,
                "status": "unknown" if gaps else "observed", "gaps": gaps})
    reviewed = sum(item["status"] == "observed" for item in observations)
    review = {"version": STATE_KEY, "status": "reviewed" if observations else "no_observations",
        "reviewed_at": now.isoformat(), "observation_count": len(observations),
        "verified_count": reviewed, "unknown_count": len(observations) - reviewed,
        "coverage": "最近100轮内当日已保存扫描；无法衡量未保存或未扫描股票的漏选",
        "meaning": "原观察报价至复盘报价的价格变化；非成交收益、非策略胜率",
        "weights_changed": False, "training_eligible": False,
        "summary": f"当日 {len(observations)} 条观察，{reviewed} 条报价可比，{len(observations)-reviewed} 条待补证据。",
        "observations": observations}
    state = _load_state(db)
    days = {day: value for day, value in (state.get("days") or {}).items()
            if (now.date() - timedelta(days=40)).isoformat() <= day <= now.date().isoformat()}
    days[now.date().isoformat()] = {"observations": dict(code_counts), "missing": dict(gap_counts),
                                  "windvanes": dict(windvane_counts)}
    days = dict(sorted(days.items())[-20:])
    db.save_yao_adaptive_state(STATE_KEY, {"version": STATE_KEY, "reviewed_at": now.isoformat(),
                                         "days": days, "latest_review": review})
    return review


def read_observation_reminders(db, code, now):
    """Read prior review evidence to add verification reminders to the next scan."""
    if now.tzinfo is None:
        return []
    now = now.astimezone(TZ)
    state = _load_state(db)
    reviewed_at = _stamp(state.get("reviewed_at"))
    if reviewed_at is None or reviewed_at > now:
        return []
    missing = windvanes = 0
    for day, counts in (state.get("days") or {}).items():
        if not (now.date() - timedelta(days=40)).isoformat() <= day <= now.date().isoformat():
            continue
        missing += (counts.get("missing") or {}).get(code, 0)
        windvanes += (counts.get("windvanes") or {}).get(code, 0)
    reminders = []
    if missing >= 2:
        reminders.append(f"此前 {missing} 条观察报价证据不完整，本轮须重新核对源时间与价格；不因此排除股票")
    if windvanes:
        reminders.append("历史扫描出现不可买风向标状态，本轮重新核对封板、停牌及成交条件")
    return reminders
