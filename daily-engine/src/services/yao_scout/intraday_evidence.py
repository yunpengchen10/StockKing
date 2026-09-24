"""Observed minute evidence for the local scanner; no forecasts or AI calls.

Tencent's public minute/day feeds carry ``HHMM price cumulative_lots amount``
and a supplier date. Amount is cumulative CNY; one A-share lot is 100 shares.
The day endpoint currently supplies five sessions, not a 20-day baseline. We
retain fetched sessions locally and leave that metric unknown until 20 earlier
sessions with the exact same three-minute window are available.

Minute labels do not establish a second-level execution time. Only completed
minute labels at/before the cutoff are used, without interpolating missing
minutes or crossing the midday break. Local highs/lows mean sampled minute
prices, not unobserved within-minute transaction highs/lows.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
from statistics import median
from typing import Any, Iterable
from uuid import uuid4

import requests


SHANGHAI = timezone(timedelta(hours=8))
DAY_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
MINUTE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
METRIC_KEYS = (
    "speed_1m_pct", "speed_3m_pct", "speed_5m_pct", "vwap",
    "local_high_5m", "local_low_5m", "amount_3m", "relative_amount_3m_20d",
)
METRIC_DEFINITIONS = {
    "speed_1m_pct": "最近1分钟价格涨幅（%）",
    "speed_3m_pct": "最近3分钟价格涨幅（%）",
    "speed_5m_pct": "最近5分钟价格涨幅（%）",
    "vwap": "截至该分钟累计成交额÷累计成交股数（元）",
    "local_high_5m": "此前5个完整分钟的采样价最高值，不含当前分钟（元）",
    "local_low_5m": "此前5个完整分钟的采样价最低值，不含当前分钟（元）",
    "amount_3m": "该分钟与3分钟前累计成交额之差（元）",
    "relative_amount_3m_20d": "最近3分钟成交额÷此前20个交易日相同区间成交额中位数（倍）",
}


def _datetime(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def _symbol(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"(?:60|00)\d{4}", code):
        raise ValueError("minute evidence requires a six-digit A-share mainboard code")
    return ("sh" if code.startswith("6") else "sz") + code


def _session(stamp: datetime) -> str | None:
    minute = stamp.hour * 60 + stamp.minute
    if 570 <= minute <= 690:
        return "morning"
    if 780 <= minute <= 900:
        return "afternoon"
    return None


def parse_tencent_intraday(payload: dict, code: str) -> list[dict]:
    """Parse either actually observed Tencent response shape, preserving dates.

    No top-level quote timestamp, request time or today's date is substituted
    for a missing minute date. Malformed numeric amount/volume remain None so
    valid price evidence can survive a provider field omission.
    """
    symbol = _symbol(code)
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        raise ValueError("Tencent minute response reports an error or lacks code=0")
    root = (payload.get("data") or {}).get(symbol)
    if not isinstance(root, dict):
        raise ValueError("Tencent minute response lacks the requested symbol")
    sessions = root.get("data")
    sessions = [sessions] if isinstance(sessions, dict) else sessions
    if not isinstance(sessions, list):
        raise ValueError("Tencent minute response lacks dated minute rows")
    result: list[dict] = []
    for session in sessions:
        if not isinstance(session, dict) or not re.fullmatch(r"\d{8}", str(session.get("date", ""))):
            continue
        for raw in session.get("data", []):
            fields = raw.split() if isinstance(raw, str) else []
            if len(fields) < 2 or not re.fullmatch(r"\d{4}", fields[0]):
                continue
            try:
                stamp = datetime.strptime(session["date"] + fields[0], "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
            except (TypeError, ValueError):
                continue
            price = _number(fields[1], positive=True)
            if price is None or _session(stamp) is None:
                continue
            lots = _number(fields[2]) if len(fields) > 2 else None
            amount = _number(fields[3]) if len(fields) > 3 else None
            result.append({
                "source_time": stamp.isoformat(), "price": price,
                "cumulative_volume_shares": lots * 100 if lots is not None else None,
                "cumulative_amount_cny": amount,
            })
    if not result:
        raise ValueError("Tencent minute response has no valid dated trading-session rows")
    return result


def _normalize(rows: Iterable[dict], cutoff: datetime) -> tuple[dict[datetime, dict], list[str]]:
    by_time: dict[datetime, dict] = {}
    conflicting: set[datetime] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            stamp = _datetime(row["source_time"])
        except (KeyError, TypeError, ValueError):
            continue
        # A 09:40 row can still change at 09:40:59. Never use that row to
        # describe a 09:40:15 cutoff, including a replay fetched after close.
        if stamp.second or stamp.microsecond or stamp + timedelta(minutes=1) > cutoff or not _session(stamp):
            continue
        price = _number(row.get("price"), positive=True)
        if price is None:
            continue
        normalized = {
            "price": price,
            "cumulative_volume_shares": _number(row.get("cumulative_volume_shares")),
            "cumulative_amount_cny": _number(row.get("cumulative_amount_cny")),
        }
        if stamp in by_time and by_time[stamp] != normalized:
            conflicting.add(stamp)
        else:
            by_time[stamp] = normalized
    for stamp in conflicting:
        by_time.pop(stamp, None)
    return by_time, (["同一供应商分钟有冲突，冲突行已弃用"] if conflicting else [])


def _window(rows: dict[datetime, dict], end: datetime, minutes: int) -> list[dict] | None:
    stamps = [end - timedelta(minutes=offset) for offset in range(minutes, -1, -1)]
    if any(_session(stamp) != _session(end) or stamp not in rows for stamp in stamps):
        return None
    return [rows[stamp] for stamp in stamps]


def _amount_delta(window: list[dict] | None) -> float | None:
    if not window:
        return None
    amounts = [row.get("cumulative_amount_cny") for row in window]
    if any(amount is None for amount in amounts):
        return None
    if any(current < previous for previous, current in zip(amounts, amounts[1:])):
        return None
    return amounts[-1] - amounts[0]


@lru_cache(maxsize=32)
def _prior_sessions(day: date) -> tuple[date, ...]:
    # Reuse the optional calendar package already supported by daily-engine.
    # No weekday approximation: Chinese exchange holidays are not weekdays off.
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        sessions = calendar.sessions_in_range(day - timedelta(days=60), day - timedelta(days=1))
        return tuple(stamp.date() for stamp in sessions[-20:])
    except (ImportError, ValueError, TypeError, KeyError, OverflowError):
        return ()


def compute_intraday_evidence(
    rows: Iterable[dict], cutoff: datetime | str,
    history_rows: Iterable[dict] = (), *, source: str = "observed_minute_rows",
    max_age_seconds: int = 300,
    expected_history_dates: Iterable[date | str] | None = None,
) -> dict:
    """Calculate evidence from supplier-labelled rows, without thresholds.

    Returns null for an unavailable metric. At least 20 distinct *earlier*
    sessions must have all four cumulative observations defining the matching
    three-minute amount; today and future dates are excluded from the baseline.
    A five-minute-old source is rejected for immediate indicators.
    """
    cutoff_at = _datetime(cutoff)
    current, gaps = _normalize(rows, cutoff_at)
    current = {stamp: row for stamp, row in current.items() if stamp.date() == cutoff_at.date()}
    result = {
        "metrics": dict.fromkeys(METRIC_KEYS), "source": source, "asOf": None,
        "gaps": gaps, "sourceAgeSeconds": None, "historyDays": 0,
        "sourceTimePrecision": "minute_label", "currentPrice": None,
        "basis": "仅使用已结束分钟；局部高低为分钟采样价；不证明可成交或资金净流入",
    }
    if not current:
        gaps.append("当日截止时点前没有可用的完整分钟行情")
        return result
    end = max(current)
    result.update(asOf=end.isoformat(), sourceAgeSeconds=(cutoff_at - end).total_seconds())
    if result["sourceAgeSeconds"] >= max_age_seconds:
        gaps.append(f"分钟行情距截止时点已{result['sourceAgeSeconds']:.0f}秒，未用于即时时序指标")
        return result
    metrics = result["metrics"]
    latest = current[end]
    result["currentPrice"] = latest["price"]
    for minutes in (1, 3, 5):
        window = _window(current, end, minutes)
        if window:
            metrics[f"speed_{minutes}m_pct"] = round((window[-1]["price"] / window[0]["price"] - 1) * 100, 6)
        else:
            gaps.append(f"缺连续{minutes}分钟采样，涨速未计算")
    prior_five = _window(current, end, 5)
    if prior_five:
        metrics["local_high_5m"] = max(row["price"] for row in prior_five[:-1])
        metrics["local_low_5m"] = min(row["price"] for row in prior_five[:-1])
    else:
        gaps.append("缺此前5分钟连续采样，局部高低未计算")
    amount = latest["cumulative_amount_cny"]
    volume = latest["cumulative_volume_shares"]
    previous = current.get(end - timedelta(minutes=1))
    cumulative_valid = not previous or all(
        previous[key] is None or latest[key] is None or latest[key] >= previous[key]
        for key in ("cumulative_amount_cny", "cumulative_volume_shares")
    )
    if amount is not None and volume and cumulative_valid:
        metrics["vwap"] = round(amount / volume, 6)
    else:
        gaps.append("累计成交额/股数缺失、无成交或倒退，VWAP未计算")
    metrics["amount_3m"] = _amount_delta(_window(current, end, 3))
    if metrics["amount_3m"] is None:
        gaps.append("最近3分钟累计成交额缺失、断档或倒退")
    historical, historical_gaps = _normalize(history_rows, cutoff_at)
    gaps.extend(historical_gaps)
    historical = {stamp: row for stamp, row in historical.items() if stamp.date() < end.date()}
    # Require the actual preceding twenty exchange sessions. Sparse cache dates
    # must not silently stretch the baseline back several months.
    dates = _prior_sessions(end.date()) if expected_history_dates is None else tuple(
        value if isinstance(value, date) else date.fromisoformat(value)
        for value in expected_history_dates
    )
    dates = sorted({day for day in dates if day < end.date()})[-20:]
    if len(dates) != 20:
        gaps.append("最近20个交易日历未完整核验，历史成交额倍数保持缺失")
        dates = []
    baselines = []
    for day in dates:
        reference_end = end.replace(year=day.year, month=day.month, day=day.day)
        delta = _amount_delta(_window(historical, reference_end, 3))
        if delta is not None:
            baselines.append(delta)
    result["historyDays"] = len(baselines)
    if len(baselines) == 20 and median(baselines) > 0 and metrics["amount_3m"] is not None:
        metrics["relative_amount_3m_20d"] = round(metrics["amount_3m"] / median(baselines), 6)
    else:
        gaps.append(f"历史20日相同时段成交额基准不足或无效（{len(baselines)}/20），未用全天量比替代")
    return result


def _load_cache(cache_dir: Path, code: str, cutoff: datetime) -> list[dict]:
    rows: list[dict] = []
    # Only recent sessions can contribute; this also bounds long-running cache IO.
    for path in sorted(cache_dir.glob(f"{code}_????????.json"), reverse=True)[:60]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("code") != code or data.get("version") != 1:
                continue
            rows.extend(data.get("rows", []))
        except (OSError, ValueError, TypeError):
            continue
    # History cutoff is enforced again in the pure function; cache never fills
    # unknown timestamps with the last price or system/request time.
    return rows


def _save_cache(cache_dir: Path, code: str, rows: list[dict], cutoff: datetime, fetched_at: datetime, source: str) -> None:
    normalized, _ = _normalize(rows, cutoff)
    sessions: dict[str, list[dict]] = {}
    for stamp, row in normalized.items():
        sessions.setdefault(stamp.strftime("%Y%m%d"), []).append({"source_time": stamp.isoformat(), **row})
    cache_dir.mkdir(parents=True, exist_ok=True)
    for day, session_rows in sessions.items():
        path = cache_dir / f"{code}_{day}.json"
        temporary = cache_dir / f".{code}_{day}_{uuid4().hex}.tmp"
        # A historical replay or earlier cutoff must never shrink a previously
        # observed complete session. Computation still filters by its own cutoff.
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("code") == code and existing.get("version") == 1:
                kept = {row["source_time"]: row for row in existing.get("rows", []) if isinstance(row, dict) and row.get("source_time")}
                kept.update({row["source_time"]: row for row in session_rows})
                session_rows = list(kept.values())
        except (OSError, ValueError, TypeError):
            pass
        data = {"version": 1, "code": code, "source": source, "fetched_at": fetched_at.isoformat(), "rows": session_rows}
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def fetch_intraday_evidence(
    code: str, cutoff: datetime | str, cache_dir: str | Path | None = None, *,
    timeout: float = 5, fetcher=None,
) -> dict:
    """Fetch public five-day minutes, with a bounded current-day fallback.

    ``fetcher`` may be an injected requests-compatible GET for offline tests.
    This function does not fetch sector data or infer an executable signal.
    """
    symbol = _symbol(code)
    cutoff_at = _datetime(cutoff)
    directory = Path(cache_dir) if cache_dir is not None else None
    cached = _load_cache(directory, code, cutoff_at) if directory is not None and directory.exists() else []
    fetched: list[dict] = []
    source = "Tencent public minute cache"
    errors = []
    getter = fetcher or requests.get
    for endpoint in (DAY_ENDPOINT, MINUTE_ENDPOINT):
        try:
            response = getter(endpoint, params={"code": symbol}, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            response.raise_for_status()
            fetched = parse_tencent_intraday(response.json(), code)
            source = endpoint
            break
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{endpoint.rsplit('/', 2)[-2]}分时源读取失败：{type(exc).__name__}")
    fetched_at = datetime.now(SHANGHAI)
    # Freshly supplied rows take precedence over older cache versions. Identical
    # duplicate source times within a provider payload are handled by normalize.
    fresh, fresh_gaps = _normalize(fetched, cutoff_at)
    cached_map, _ = _normalize(cached, cutoff_at)
    cached_map.update(fresh)
    combined = [{"source_time": stamp.isoformat(), **row} for stamp, row in cached_map.items()]
    result = compute_intraday_evidence(combined, cutoff_at, combined, source=source)
    result.update(code=code, fetchedAt=fetched_at.isoformat(), fetchedNow=bool(fetched), metricDefinitions=METRIC_DEFINITIONS.copy())
    result["gaps"].extend(fresh_gaps)
    if not fetched:
        result["gaps"].extend(errors)
    if directory is not None and fresh:
        try:
            _save_cache(directory, code, combined, cutoff_at, fetched_at, source)
        except OSError as exc:
            result["gaps"].append(f"分钟缓存保存失败：{type(exc).__name__}")
    result["gaps"] = list(dict.fromkeys(result["gaps"]))
    return result
