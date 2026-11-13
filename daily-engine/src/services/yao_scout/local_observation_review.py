"""Persist source-backed observation reviews without creating trade outcomes.

These are changes between two observed prices, never realised returns or model
training labels. Repeated data gaps become verification reminders only.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
import json
import math
import re
import sqlite3
from time import monotonic
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
STATE_KEY = "king-local-observation-review-v1"
SCAN_MODES = {"king_live", "king_0920", "king_0922", "king_0940", "king_0955", "king_1030", "king_1455"}
MAX_REVIEW_SOURCE_BYTES = 256 * 1024 * 1024
MAX_REVIEW_ROWS = 20
MAX_REVIEW_CANDIDATES = 2000


def read_review_history(db, now, *, limit=100, budget_seconds=10.0):
    """Project only review evidence, never decode a complete saved scan.

    Metadata still covers the latest 100 runs. Large scan JSON is parsed by
    SQLite one run at a time, with a source-size limit, row limit and deadline.
    Missing/truncated runs remain explicit coverage gaps, not empty successes.
    """
    if now.tzinfo is None:
        raise ValueError("复盘时钟必须包含时区")
    now = now.astimezone(TZ)
    injected = getattr(db, 'list_yao_observation_review_history', None)
    if callable(injected):
        return injected(now=now, limit=limit, budget_seconds=budget_seconds)
    coverage = {'mode': 'sqlite_review_projection', 'metadataLimit': min(100, max(1, int(limit))),
        'metadataCount': 0, 'eligibleRunCount': 0, 'projectedRunCount': 0,
        'omittedRunCount': 0, 'maximumProjectedRuns': MAX_REVIEW_ROWS,
        'maximumCandidatesPerRun': MAX_REVIEW_CANDIDATES, 'complete': False, 'gaps': []}
    packet = {'rows': [], 'coverage': coverage}
    if not callable(getattr(db, 'get_session', None)):
        coverage['gaps'].append('compact_review_history_unavailable')
        return packet
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    deadline = monotonic() + max(.01, min(10.0, float(budget_seconds)))
    # All paths are fixed. json_each values stay inside SQLite; only this small
    # JSON object reaches Python. In particular quote raw payloads, minutes,
    # snapshot rows, factor archives and the other candidate fields are omitted.
    def fields(prefix, names):
        # -> keeps JSON booleans as booleans: true must never turn into a
        # numeric reference price of 1 after crossing the SQL boundary.
        return ','.join("'" + name + "',json(value -> '$." + prefix + name + "')"
                        for name in names)
    quote = fields('quote.', ('code', 'source', 'source_time', 'provider_timestamp', 'price', 'is_stale'))
    candidate = fields('', ('code', 'name', 'status', 'decision_at', 'referencePrice'))
    candidate += ", 'quote',json_object(" + quote + ')'
    candidate += (", 'precisionDecision',json_object('profiles',CASE WHEN "
        "json_type(value,'$.precisionDecision.profiles') IS NULL THEN json('{}') "
        "ELSE json_extract(value,'$.precisionDecision.profiles') END)")
    candidate += ", '_reviewProfile',profile"
    branches = ["SELECT value,NULL AS profile FROM json_each(r.doc,'$." + key + "') WHERE type='object'"
                for key in ('candidates', 'windvanes', 'precisionResearch', 'precisionWatchlist', 'evidenceInsufficient')]
    branches.append("SELECT m.value,p.key AS profile FROM json_each(r.doc,'$.profileCandidates') p "
                    "JOIN json_each(p.value) m WHERE p.type='array' AND m.type='object'")
    # Older supported Python builds may ship pre-3.45 SQLite without JSONB.
    # They still project in SQL; no fallback ever deserializes the full report.
    document = 'jsonb(result_json)' if sqlite3.sqlite_version_info >= (3, 45, 0) else 'result_json'
    projection = text("WITH source AS MATERIALIZED (SELECT " + document + " AS doc FROM yao_runs WHERE id=:row_id) "
        "SELECT json_extract(r.doc,'$.as_of'), "
        "(SELECT json_group_array(json_object(" + candidate + ")) FROM (" + ' UNION ALL '.join(branches) +
        " LIMIT :candidate_limit)) FROM source r")
    projected, gaps = packet['rows'], coverage['gaps']
    try:
        with db.get_session() as session:
            driver = session.connection().connection.driver_connection
            if not isinstance(driver, sqlite3.Connection):
                gaps.append('compact_review_requires_sqlite')
                return packet
            # created_at/result_json are deliberately absent: their large text
            # overflow pages are not needed to select the small run headers.
            headers = session.execute(text('SELECT id,run_id,mode,as_of_at FROM yao_runs '
                'ORDER BY as_of_at DESC,id DESC LIMIT :limit'), {'limit': coverage['metadataLimit']}).all()
            coverage['metadataCount'] = len(headers)
            eligible = []
            for row_id, run_id, mode, raw_at in headers:
                try:
                    at = raw_at if isinstance(raw_at, datetime) else datetime.fromisoformat(raw_at)
                    at = (at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at).astimezone(TZ)
                except (TypeError, ValueError):
                    continue
                if mode in SCAN_MODES and at.date() == now.date() and at <= now:
                    eligible.append((row_id, run_id, mode, at.isoformat()))
            coverage['eligibleRunCount'] = len(eligible)
            old_limit = driver.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_REVIEW_SOURCE_BYTES)
            driver.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            try:
                for row_id, run_id, mode, at in eligible[:MAX_REVIEW_ROWS]:
                    if monotonic() >= deadline:
                        gaps.append('compact_review_history_budget_exhausted')
                        break
                    try:
                        saved_at, encoded = session.execute(projection, {'row_id': row_id,
                            'candidate_limit': MAX_REVIEW_CANDIDATES + 1}).one()
                        members = json.loads(encoded or '[]')
                    except (SQLAlchemyError, ValueError) as error:
                        gaps.append('compact_review_history_budget_exhausted' if monotonic() >= deadline
                                    else 'compact_review_projection_' + type(error).__name__)
                        continue
                    if len(members) > MAX_REVIEW_CANDIDATES:
                        gaps.append('compact_review_candidate_limit')
                        # A truncated group can lose selected-profile membership;
                        # omit this run rather than report a misleading review.
                        continue
                    result = {'as_of': saved_at, 'candidates': [], 'profileCandidates': {}}
                    for member in members:
                        profile = member.pop('_reviewProfile', None)
                        if profile is None:
                            result['candidates'].append(member)
                        else:
                            result['profileCandidates'].setdefault(profile, []).append(member)
                    projected.append({'run_id': run_id, 'mode': mode, 'as_of': at, 'result': result})
            finally:
                driver.set_progress_handler(None, 0)
                driver.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_limit)
    except (SQLAlchemyError, sqlite3.Error, RuntimeError):
        gaps.append('compact_review_history_unavailable')
    coverage['projectedRunCount'] = len(projected)
    coverage['omittedRunCount'] = coverage['eligibleRunCount'] - len(projected)
    if coverage['eligibleRunCount'] > MAX_REVIEW_ROWS:
        gaps.append('compact_review_run_limit')
    coverage['gaps'] = list(dict.fromkeys(gaps))
    coverage['complete'] = not coverage['gaps'] and not coverage['omittedRunCount']
    return packet


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
    history_packet = ({'rows': history, 'coverage': {'mode': 'provided_history', 'complete': True}}
                      if history is not None else read_review_history(db, now))
    rows = history_packet['rows']
    history_coverage = history_packet['coverage']
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
    review = {"version": STATE_KEY, "status": ("partial" if not history_coverage['complete'] else
                                              "reviewed" if observations else "no_observations"),
        "reviewed_at": now.isoformat(), "observation_count": len(observations),
        "verified_count": reviewed, "unknown_count": len(observations) - reviewed,
        "coverage": ("最近100轮内当日已保存扫描；无法衡量未保存或未扫描股票的漏选" if history_coverage['complete'] else
                     "最近100轮元数据中的部分当日扫描；有界读取存在遗漏，详见历史覆盖"),
        "historyCoverage": history_coverage,
        "meaning": "原观察报价至复盘报价的价格变化；非成交收益、非策略胜率",
        "weights_changed": False, "training_eligible": False,
        "summary": f"当日 {len(observations)} 条观察，{reviewed} 条报价可比，{len(observations)-reviewed} 条待补证据。",
        "observations": observations}
    state = _load_state(db)
    days = {day: value for day, value in (state.get("days") or {}).items()
            if (now.date() - timedelta(days=40)).isoformat() <= day <= now.date().isoformat()}
    if history_coverage['complete']:
        days[now.date().isoformat()] = {"observations": dict(code_counts), "missing": dict(gap_counts),
                                      "windvanes": dict(windvane_counts)}
    else:
        # Partial history must not overwrite an existing complete daily count.
        review['remindersUpdateStatus'] = 'deferred_until_complete_history'
    days = dict(sorted(days.items())[-20:])
    db.save_yao_adaptive_state(STATE_KEY, {"version": STATE_KEY, "reviewed_at": now.isoformat(),
                                         "days": days, "latest_review": review})
    return review


def _reminders_from_state(state, code, now):
    """Read prior review evidence to add verification reminders to the next scan."""
    if now.tzinfo is None:
        return []
    now = now.astimezone(TZ)
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


def read_observation_reminders_batch(db, codes, now):
    """Decode the saved review once, before fetching the scan's final quotes."""
    codes = list(dict.fromkeys(codes))
    if not codes or now.tzinfo is None:
        return {code: [] for code in codes}
    state = _load_state(db)
    return {code: _reminders_from_state(state, code, now) for code in codes}


def read_observation_reminders(db, code, now):
    return read_observation_reminders_batch(db, [code], now).get(code, [])
