"""Archived, point-in-time AKShare context shared by desktop and pip clients.

Publication dates are not report periods. Date-only publications become usable
the following day. Latest/revised vendor tables are never backfilled into a
historical decision: an archive must already have existed at that time.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
VERSION = "akshare-context-v1"
FIELDS = {
    "financials": ["股票代码", "最新公告日期", "每股收益", "净利润-净利润", "净利润-同比增长",
                   "营业总收入-同比增长", "净资产收益率", "每股经营现金流量", "所处行业"],
    "forecast": ["股票代码", "公告日期", "预测指标", "预告类型", "业绩变动幅度"],
    "unlock": ["股票代码", "解禁时间", "解禁数量", "占解禁前流通市值比例"],
}
ENDPOINTS = {"financials": "stock_yjbb_em", "forecast": "stock_yjyg_em",
             "unlock": "stock_restricted_release_detail_em"}


def timestamp(value):
    result = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
    if result.tzinfo is None:
        raise ValueError("Timezone-aware decision/observation time required")
    return result.astimezone(TZ)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def code_of(value):
    text = str(value).strip().removesuffix(".0")
    return text.zfill(6) if re.fullmatch(r"\d{1,6}", text) else ""


def quarter_ends(day, count=2):
    ends = [date(year, month, last) for year in range(day.year - 2, day.year + 1)
            for month, last in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return sorted((d for d in ends if d < day), reverse=True)[:count]


def publication_time(value):
    """No invented intraday announcement time: use next midnight."""
    try:
        day = date.fromisoformat(str(value)[:10])
        return datetime.combine(day + timedelta(days=1), time(), TZ)
    except (ValueError, TypeError):
        return None


def fetch_akshare(endpoint, params, timeout):
    multiprocessing.freeze_support()
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_fetch_worker, args=(child, endpoint, params), daemon=True)
    try:
        process.start()
        child.close()
        if not parent.poll(timeout):
            raise TimeoutError(endpoint + ' timed out')
        ok, result = parent.recv()
        if not ok:
            raise RuntimeError(result)
        return result
    finally:
        child.close()
        parent.close()
        if process.pid:
            process.join(.2)
            if process.is_alive():
                process.terminate()
                process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)
            process.close()


def _fetch_worker(connection, endpoint, params):
    try:
        import akshare as ak
        connection.send((True, getattr(ak, endpoint)(**params)))
    except Exception as exc:
        connection.send((False, type(exc).__name__ + ': ' + str(exc)[:120]))
    finally:
        connection.close()


class AkshareContextProvider:
    def __init__(self, directory, *, fetcher=None, clock=None, timeout=35):
        self.directory = Path(directory)
        self.fetcher = fetcher or fetch_akshare
        self.clock = clock or (lambda: datetime.now(TZ))
        self.timeout = timeout

    def _table(self, kind, params, cutoff, refresh):
        key = hashlib.sha256(json.dumps([kind, params], sort_keys=True).encode()).hexdigest()[:20]
        folder = self.directory / key
        for path in sorted(folder.glob("*.json"), reverse=True):
            try:
                packet = json.loads(path.read_text(encoding="utf-8"))
                age = (cutoff - timestamp(packet["observedAt"])).total_seconds()
                if 0 <= age <= 21600 and packet.get("version") == VERSION:
                    return packet
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if not refresh:
            return {"kind": kind, "status": "unavailable", "rows": [], "params": params,
                    "source": "AKShare/" + ENDPOINTS[kind], "reason": "截止时点前无有效归档；禁止用当前数据补历史"}
        try:
            frame = self.fetcher(ENDPOINTS[kind], params, self.timeout)
            required = {"股票代码", "最新公告日期" if kind == "financials" else "公告日期" if kind == "forecast" else "解禁时间"}
            required.update({'financials': {'净利润-净利润', '每股收益', '每股经营现金流量'},
                             'forecast': {'预测指标', '预告类型'},
                             'unlock': {'占解禁前流通市值比例'}}[kind])
            if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
                raise ValueError("provider_schema_changed")
            # Explicit allowlist discards unlock post-event returns and other future fields.
            rows = json.loads(frame.reindex(columns=FIELDS[kind]).to_json(orient="records", date_format="iso", force_ascii=False))
            for row in rows:
                row["股票代码"] = code_of(row["股票代码"])
            observed = timestamp(self.clock())
            packet = {"version": VERSION, "kind": kind, "params": params, "status": "available",
                      "source": "AKShare/" + ENDPOINTS[kind], "observedAt": observed.isoformat(), "rows": rows}
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (observed.strftime("%Y%m%dT%H%M%S%f") + ".json")
            temporary = path.with_suffix("." + uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(packet, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return packet
        except Exception as exc:
            return {"kind": kind, "params": params, "status": "unavailable", "rows": [],
                    "source": "AKShare/" + ENDPOINTS[kind], "reason": type(exc).__name__ + ": " + str(exc)[:120]}

    def collect(self, cutoff=None, *, refresh=True):
        now = timestamp(self.clock())
        cutoff = timestamp(cutoff or now)
        if refresh and abs((now - cutoff).total_seconds()) > 60:
            raise ValueError("Historical decisions must use refresh=False and prior archives")
        periods = quarter_ends(cutoff.date())
        current_quarter = date(cutoff.year, ((cutoff.month - 1) // 3 + 1) * 3, 1)
        next_month = (current_quarter.replace(day=28) + timedelta(days=4)).replace(day=1)
        forecast_period = next_month - timedelta(days=1)
        jobs = [("financials", {"date": d.strftime("%Y%m%d")}) for d in periods]
        jobs += [("forecast", {"date": d.strftime("%Y%m%d")}) for d in (forecast_period, periods[0])]
        jobs += [("unlock", {"start_date": cutoff.strftime("%Y%m%d"),
                             "end_date": (cutoff + timedelta(days=14)).strftime("%Y%m%d")})]
        with ThreadPoolExecutor(max_workers=3) as pool:
            tables = list(pool.map(lambda job: self._table(*job, cutoff, refresh), jobs))
        return {"version": VERSION, "tables": tables, "requestedAt": cutoff.isoformat(),
                "completedAt": timestamp(self.clock()).isoformat()}


def for_stock(bundle, code, cutoff):
    cutoff = timestamp(cutoff)
    code = code_of(code)
    output = {"version": VERSION, "financials": None, "forecasts": [], "unlocks": [],
              "coverage": {}, "gaps": [], "decisionAt": cutoff.isoformat()}
    finances = []
    covered = {kind: [] for kind in FIELDS}
    for table in bundle.get("tables", []):
        kind = table.get("kind")
        if kind not in FIELDS:
            continue
        try:
            observed = timestamp(table["observedAt"])
            usable = table.get("status") == "available" and 0 <= (cutoff - observed).total_seconds() <= 21600
        except (ValueError, TypeError, KeyError):
            usable = False
        covered[kind].append(usable)
        if not usable:
            output["gaps"].append(kind + " 数据未取得、过期或尚未可知")
            continue
        for row in table.get("rows", []):
            if code_of(row.get("股票代码")) != code:
                continue
            base = {"source": table["source"], "observedAt": observed.isoformat(),
                    "reportPeriod": table.get("params", {}).get("date") or ""}
            if kind in ("financials", "forecast"):
                published = publication_time(row.get("最新公告日期" if kind == "financials" else "公告日期"))
                if published is None or published > cutoff:
                    output["gaps"].append(kind + " 公告时间未知或尚未到可用时点")
                    covered[kind].append(False)
                    continue
                base["availableAt"] = max(published, observed).isoformat()
                base["publishedDate"] = str(row.get("最新公告日期" if kind == "financials" else "公告日期"))[:10]
            if kind == "financials":
                base.update(profit=number(row.get("净利润-净利润")), profitGrowthPct=number(row.get("净利润-同比增长")),
                            revenueGrowthPct=number(row.get("营业总收入-同比增长")), roePct=number(row.get("净资产收益率")),
                            cashPerShare=number(row.get("每股经营现金流量")), eps=number(row.get("每股收益")),
                            industry=row.get("所处行业"))
                finances.append(base)
            elif kind == "forecast":
                if (cutoff.date() - published.date()).days <= 120:
                    if not row.get('预告类型') or not row.get('预测指标'):
                        covered[kind].append(False)
                        output['gaps'].append('业绩预告类型或指标缺失，风险待核验')
                    base.update(type=str(row.get("预告类型") or "未知"), metric=str(row.get("预测指标") or "未知"),
                                changePct=number(row.get("业绩变动幅度")))
                    output["forecasts"].append(base)
            else:
                try:
                    event_date = date.fromisoformat(str(row.get("解禁时间"))[:10])
                except ValueError:
                    covered[kind].append(False)
                    output['gaps'].append('解禁日期无效，不能视为无解禁')
                    continue
                if 0 <= (event_date - cutoff.date()).days <= 14:
                    ratio = number(row.get("占解禁前流通市值比例"))
                    if ratio is None or ratio < 0:
                        covered[kind].append(False)
                        output['gaps'].append('解禁比例缺失，事件风险待核验')
                    base.update(eventDate=event_date.isoformat(), shares=number(row.get("解禁数量")),
                                floatRatioPct=ratio * 100 if ratio is not None and ratio >= 0 else None,
                                availableAt=observed.isoformat(), availabilityBasis="first_observed; no publication timestamp")
                    output["unlocks"].append(base)
    if finances:
        output["financials"] = max(finances, key=lambda row: (row["reportPeriod"], row["publishedDate"]))
    else:
        output["gaps"].append("未取得该股票已披露财报，不能视为财务健康")
    output["coverage"] = {kind: bool(values) and all(values) for kind, values in covered.items()}
    output["gaps"] = list(dict.fromkeys(output["gaps"]))
    return output
