"""Deterministic, daily-bar A-share account simulation from recorded signals.

Never obtains today's recommendations or invents a historical signal. Imported
provenance is a user assertion, not proof of an out-of-sample performance record.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENGINE_VERSION = "a-share-account-v1"
SHANGHAI = timezone(timedelta(hours=8))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AccountConfig(StrictModel):
    initial_cash: float = Field(100000, ge=100, le=1_000_000_000)
    delay_sessions: int = Field(0, ge=0, le=20)
    holding_sessions: int = Field(5, ge=1, le=500)
    position_weight: float = Field(0.2, gt=0, le=1)
    max_positions: int = Field(5, ge=1, le=100)
    commission_rate: float = Field(0.0003, ge=0, le=0.02)
    minimum_commission: float = Field(5, ge=0, le=1000)
    stamp_tax_rate: float | None = Field(None, ge=0, le=0.02)
    transfer_fee_rate: float = Field(0.00001, ge=0, le=0.01)
    slippage_bps: float = Field(10, ge=0, le=500)
    stop_loss_pct: float | None = Field(None, gt=0, lt=1)
    take_profit_pct: float | None = Field(None, gt=0, le=10)


class RecordedSignal(StrictModel):
    signal_id: str = Field(min_length=1, max_length=120)
    symbol: str
    signal_date: date
    available_at: datetime
    source: str = Field(min_length=1, max_length=300)
    side: Literal["buy", "sell"] = "buy"
    holding_sessions: int | None = Field(None, ge=1, le=500)
    position_weight: float | None = Field(None, gt=0, le=1)

    @model_validator(mode="after")
    def validate_availability(self):
        if self.available_at.tzinfo is None:
            raise ValueError("available_at 必须带时区，例如 2026-08-03T15:30:00+08:00")
        self.available_at = self.available_at.astimezone(SHANGHAI)
        if self.signal_date > self.available_at.date():
            raise ValueError("signal_date 不能晚于 available_at")
        return self


class DailyBar(StrictModel):
    symbol: str
    date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0, description="成交股数，不是手")
    limit_up: float | None = Field(None, gt=0)
    limit_down: float | None = Field(None, gt=0)
    no_price_limit: bool = False
    suspended: bool = False
    corporate_action: bool = False

    @model_validator(mode="after")
    def validate_prices(self):
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC 区间无效")
        if not self.no_price_limit:
            if self.limit_up is None or self.limit_down is None or self.limit_down >= self.limit_up:
                raise ValueError("必须提供时点一致涨跌停价，或显式 no_price_limit=true")
            if self.low < self.limit_down - 0.011 or self.high > self.limit_up + 0.011:
                raise ValueError("OHLC 超出提供的涨跌停价")
        if self.corporate_action:
            raise ValueError("当前账户引擎不支持除权分红/拆并股区间，请移除该区间或使用支持公司行动的引擎")
        return self


class BenchmarkBar(StrictModel):
    date: date
    close: float = Field(gt=0)


class BacktestRequest(StrictModel):
    dataset_name: str = Field(min_length=1, max_length=200)
    provenance: Literal["user_supplied_point_in_time", "synthetic_demo"]
    point_in_time_confirmed: bool = False
    calendar_source: str = Field(min_length=1, max_length=300)
    price_basis: Literal["unadjusted"] = "unadjusted"
    benchmark_name: str = Field("用户导入基准", min_length=1, max_length=200)
    config: AccountConfig = Field(default_factory=AccountConfig)
    calendar: list[date] = Field(default_factory=list, max_length=5000)
    signals: list[RecordedSignal] = Field(default_factory=list, max_length=10000)
    bars: list[DailyBar] = Field(default_factory=list, max_length=150000)
    benchmark: list[BenchmarkBar] = Field(default_factory=list, max_length=5000)
    calendar_csv: str = Field("", max_length=500_000)
    signals_csv: str = Field("", max_length=4_000_000)
    bars_csv: str = Field("", max_length=24_000_000)
    benchmark_csv: str = Field("", max_length=500_000)

    @model_validator(mode="before")
    @classmethod
    def decode_csv(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for name in ("calendar", "signals", "bars", "benchmark"):
            content = value.get(f"{name}_csv")
            if not content:
                continue
            if value.get(name):
                raise ValueError(f"{name} 与 {name}_csv 不能同时提供")
            rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
            if name == "calendar":
                if any("date" not in row for row in rows):
                    raise ValueError("calendar_csv 需要 date 列")
                value[name] = [row["date"] for row in rows]
            else:
                value[name] = [{key: item for key, item in row.items() if item != ""} for row in rows]
        return value


def _money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _symbol(raw: str) -> str:
    raw = raw.strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")):
        raw = raw[2:] + "." + raw[:2]
    code = raw.split(".")[0]
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"无效 A 股代码: {raw}")
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        exchange = "SH"
    elif code.startswith(("000", "001", "002", "003", "300", "301")):
        exchange = "SZ"
    elif code.startswith(("43", "83", "87", "88", "92")):
        exchange = "BJ"
    else:
        raise ValueError(f"暂不支持该证券类型: {raw}")
    if "." in raw and raw.split(".")[1] != exchange:
        raise ValueError(f"证券代码与市场不一致: {raw}")
    return f"{code}.{exchange}"


def _lot(symbol: str) -> tuple[int, int]:
    if symbol.startswith(("688", "689")):
        return 200, 1
    if symbol.endswith(".BJ"):
        return 100, 1
    return 100, 100


def _fees(notional: float, side: str, day: date, config: AccountConfig) -> dict:
    tax = config.stamp_tax_rate
    if tax is None:
        tax = 0.0005 if day >= date(2023, 8, 28) else 0.001
    commission = _money(max(config.minimum_commission, notional * config.commission_rate))
    transfer = _money(notional * config.transfer_fee_rate)
    stamp = _money(notional * tax) if side == "sell" else 0.0
    return {"commission": commission, "transfer_fee": transfer, "stamp_tax": stamp,
            "total": _money(commission + transfer + stamp)}


def _execution(bar: DailyBar, side: str, config: AccountConfig) -> tuple[float | None, str]:
    if bar.suspended or bar.volume <= 0:
        return None, "suspended_or_zero_volume"
    # Opening at a limit is rejected even when the daily bar later unlocks:
    # daily bars cannot establish a fill at the original opening auction.
    if side == "buy" and bar.limit_up is not None and bar.open >= bar.limit_up - 1e-8:
        return None, "open_at_limit_up"
    if side == "sell" and bar.limit_down is not None and bar.open <= bar.limit_down + 1e-8:
        return None, "open_at_limit_down"
    price = _money(bar.open * (1 + (1 if side == "buy" else -1) * config.slippage_bps / 10000))
    if price > bar.high + 1e-8 or price < bar.low - 1e-8:
        return None, "slippage_outside_observed_range"
    if (bar.limit_up is not None and price > bar.limit_up) or (bar.limit_down is not None and price < bar.limit_down):
        return None, "slippage_outside_price_limits"
    return price, ""


def run_backtest(request: BacktestRequest | dict[str, Any]) -> dict[str, Any]:
    req = request if isinstance(request, BacktestRequest) else BacktestRequest.model_validate(request)
    if not req.point_in_time_confirmed:
        raise ValueError("请确认导入信号为事前记录，未将当前候选名单回填到历史日期")
    if not req.signals or not req.bars or len(req.calendar) < 2:
        raise ValueError("缺少事前信号、OHLCV 或交易日历，不能生成绩效")
    calendar = sorted(req.calendar)
    if len(set(calendar)) != len(calendar):
        raise ValueError("交易日历包含重复日期")
    if calendar[0] < date(2008, 9, 19):
        raise ValueError("当前税费默认口径仅支持 2008-09-19 之后的区间")
    if any(day.weekday() >= 5 for day in calendar):
        raise ValueError("A 股交易日历不能包含周末；节假日由导入的官方日历负责")
    indices = {day: i for i, day in enumerate(calendar)}
    bars = {}
    for bar in req.bars:
        symbol = _symbol(bar.symbol)
        key = (symbol, bar.date)
        if key in bars:
            raise ValueError(f"重复行情: {symbol} {bar.date}")
        bars[key] = bar
    if len({signal.signal_id for signal in req.signals}) != len(req.signals):
        raise ValueError("signal_id 必须唯一")
    signals_by_index: dict[int, list[RecordedSignal]] = {}
    outside = []
    symbols = set()
    for signal in sorted(req.signals, key=lambda item: (item.available_at, item.signal_id)):
        signal.symbol = _symbol(signal.symbol)
        symbols.add(signal.symbol)
        if signal.signal_date not in indices:
            raise ValueError(f"信号日期不在交易日历中: {signal.signal_id}")
        execution_index = bisect_right(calendar, signal.available_at.date()) + req.config.delay_sessions
        if execution_index >= len(calendar):
            outside.append({"signal_id": signal.signal_id, "symbol": signal.symbol, "reason": "outside_execution_window"})
        else:
            signals_by_index.setdefault(execution_index, []).append(signal)
    if not signals_by_index:
        raise ValueError("没有在可用时点之后可执行的信号；不能生成绩效")
    # A missing bar is an unknown valuation, not a suspension. Explicitly
    # supplied zero-volume/suspended rows retain their provided closing mark.
    missing = [f"{symbol}@{day}" for symbol in sorted(symbols) for day in calendar if (symbol, day) not in bars]
    if missing:
        raise ValueError(f"行情不完整（缺少 {len(missing)} 行），不能计算可信净值: {', '.join(missing[:6])}")
    benchmark = {}
    for bar in req.benchmark:
        if bar.date in benchmark:
            raise ValueError("基准包含重复日期")
        benchmark[bar.date] = bar.close
    if any(day not in benchmark for day in calendar):
        raise ValueError("基准必须覆盖完整交易日历；不以股票池收益代替基准")

    config = req.config
    config.initial_cash = _money(config.initial_cash)
    cash = _money(config.initial_cash)
    previous_equity = cash
    positions: dict[str, dict] = {}
    trades, rejected, closed, curve = [], list(outside), [], []
    total_fees = 0.0
    peak = cash
    cash_peak = cash
    worst_recovery, underwater_since = 0, None
    scheduled_buys = 0

    def reject(day, symbol, reason, signal_id="", side="buy"):
        rejected.append({"date": day.isoformat(), "symbol": symbol, "reason": reason,
                         "signal_id": signal_id, "side": side})

    for index, day in enumerate(calendar):
        today = signals_by_index.get(index, [])
        explicit_sells = {s.symbol: s for s in today if s.side == "sell"}
        for symbol, signal in explicit_sells.items():
            if symbol not in positions:
                reject(day, symbol, "no_position", signal.signal_id, "sell")
            else:
                positions[symbol]["pending_exit"] = "signal_exit"
        for symbol, position in list(positions.items()):
            previous_close = bars[(symbol, calendar[index - 1])].close if index else position["entry_price"]
            movement = previous_close / position["entry_price"] - 1
            reason = position.get("pending_exit")
            if not reason and index >= position["exit_index"]:
                reason = "holding_period"
            if not reason and config.stop_loss_pct is not None and movement <= -config.stop_loss_pct:
                reason = "previous_close_stop_loss"
            if not reason and config.take_profit_pct is not None and movement >= config.take_profit_pct:
                reason = "previous_close_take_profit"
            if not reason:
                continue
            position["pending_exit"] = reason
            if index <= position["entry_index"]:
                reject(day, symbol, "t_plus_one", position["signal_id"], "sell")
                continue
            price, block = _execution(bars[(symbol, day)], "sell", config)
            if price is None:
                reject(day, symbol, block, position["signal_id"], "sell")
                continue
            notional = _money(position["quantity"] * price)
            fees = _fees(notional, "sell", day, config)
            cash = _money(cash + notional - fees["total"])
            total_fees = _money(total_fees + fees["total"])
            pnl = _money(notional - fees["total"] - position["entry_cost"])
            trade = {"date": day.isoformat(), "symbol": symbol, "side": "sell", "quantity": position["quantity"],
                     "price": price, "notional": notional, "fees": fees, "cash_after": cash,
                     "signal_id": position["signal_id"], "reason": reason, "realized_pnl": pnl}
            trades.append(trade)
            closed.append({"symbol": symbol, "entry_date": position["entry_date"], "exit_date": day.isoformat(),
                           "net_pnl": pnl, "net_return": pnl / position["entry_cost"],
                           "holding_sessions": index - position["entry_index"]})
            del positions[symbol]
        for signal in (s for s in today if s.side == "buy"):
            scheduled_buys += 1
            symbol = signal.symbol
            if symbol in positions or symbol in explicit_sells:
                reject(day, symbol, "existing_position_or_exit_signal", signal.signal_id)
                continue
            if len(positions) >= config.max_positions:
                reject(day, symbol, "max_positions", signal.signal_id)
                continue
            price, block = _execution(bars[(symbol, day)], "buy", config)
            if price is None:
                reject(day, symbol, block, signal.signal_id)
                continue
            budget = min(cash, previous_equity * (signal.position_weight or config.position_weight))
            minimum, step = _lot(symbol)
            # Algebraic upper bound followed by a cash/fee check. The previous
            # close NAV determines risk budget; no future closing price is used.
            quantity = int(max(0, budget - config.minimum_commission) / (price * (1 + config.commission_rate + config.transfer_fee_rate)))
            quantity = quantity // step * step
            while quantity >= minimum:
                notional = _money(quantity * price)
                fees = _fees(notional, "buy", day, config)
                if notional + fees["total"] <= budget + 1e-8:
                    break
                quantity -= step
            if quantity < minimum:
                reject(day, symbol, "insufficient_cash_or_minimum_lot", signal.signal_id)
                continue
            cost = _money(notional + fees["total"])
            cash = _money(cash - cost)
            total_fees = _money(total_fees + fees["total"])
            positions[symbol] = {"quantity": quantity, "entry_price": price, "entry_date": day.isoformat(),
                                 "entry_index": index, "entry_cost": cost, "signal_id": signal.signal_id,
                                 "exit_index": index + (signal.holding_sessions or config.holding_sessions)}
            trades.append({"date": day.isoformat(), "symbol": symbol, "side": "buy", "quantity": quantity,
                           "price": price, "notional": notional, "fees": fees, "cash_after": cash,
                           "signal_id": signal.signal_id, "available_at": signal.available_at.isoformat(),
                           "reason": "recorded_signal"})
        market_value = _money(sum(pos["quantity"] * bars[(symbol, day)].close for symbol, pos in positions.items()))
        equity = _money(cash + market_value)
        peak = max(peak, equity)
        if equity >= cash_peak:
            if underwater_since is not None:
                worst_recovery = max(worst_recovery, index - underwater_since)
            underwater_since, cash_peak = None, equity
        elif underwater_since is None:
            underwater_since = max(0, index - 1)
        curve.append({"date": day.isoformat(), "cash": cash, "market_value": market_value,
                      "equity": equity, "nav": equity / config.initial_cash,
                      "benchmark_nav": benchmark[day] / benchmark[calendar[0]],
                      "drawdown": equity / peak - 1, "positions": len(positions)})
        previous_equity = equity
    if underwater_since is not None:
        worst_recovery = max(worst_recovery, len(calendar) - 1 - underwater_since)
    final = curve[-1]
    canonical = req.model_dump(mode="json", exclude={"calendar_csv", "signals_csv", "bars_csv", "benchmark_csv"})
    fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    actual_buys = sum(trade["side"] == "buy" for trade in trades)
    return {
        "engine_version": ENGINE_VERSION, "input_sha256": fingerprint,
        "dataset_name": req.dataset_name, "provenance": req.provenance,
        "performance_kind": "synthetic_demo" if req.provenance == "synthetic_demo" else "imported_signal_account_simulation",
        "verified_live_performance": False, "config": config.model_dump(),
        "benchmark_name": req.benchmark_name, "calendar_source": req.calendar_source,
        "summary": {"from": calendar[0].isoformat(), "through": calendar[-1].isoformat(),
                    "sessions": len(calendar), "initial_cash": config.initial_cash, "final_equity": final["equity"],
                    "net_return": final["nav"] - 1, "benchmark_return": final["benchmark_nav"] - 1,
                    "excess_return": final["nav"] - final["benchmark_nav"],
                    "max_drawdown": min(row["drawdown"] for row in curve), "total_fees": total_fees,
                    "realized_pnl": _money(sum(row["net_pnl"] for row in closed)),
                    "closed_trades": len(closed), "win_rate": sum(row["net_pnl"] > 0 for row in closed) / len(closed) if closed else None,
                    "entry_fill_rate": actual_buys / scheduled_buys if scheduled_buys else None,
                    "open_positions": len(positions), "unrecovered_drawdown": underwater_since is not None,
                    "longest_drawdown_sessions": worst_recovery},
        "equity_curve": curve, "trades": trades, "closed_positions": closed, "rejected_orders": rejected,
        "open_positions": [{**{k: v for k, v in pos.items() if k not in {"entry_index", "exit_index"}}, "symbol": symbol,
                            "mark_price": bars[(symbol, calendar[-1])].close} for symbol, pos in positions.items()],
        "limitations": [
            "导入信号的事前性由用户声明；时间戳和哈希保证可追溯，不证明未经过历史挑选。此结果不会使模型通过发布门槛。",
            "仅模拟下一个交易日开盘或延后若干交易日的开盘；没有分钟行情，不能宣称分钟级跟随延迟测试。",
            "开盘处于买入涨停/卖出跌停时拒绝成交；每日 OHLCV 不能证明竞价队列深度，实际成交可能更差。",
            "买入未成交即作废，退出受阻逐日重试；持有期满、止盈止损与退出信号均服从 T+1。止盈止损以前一收盘触发。",
            "期末持仓按收盘估值，不强制假设卖出；尚未发生的卖出费税未扣。无利息、分红、拆并股、融资或软件订阅费。",
            "使用未复权价格与逐日涨跌停价；含公司行动的区间不支持。佣金和过户费按所设固定费率，印花税为空时按日期适配。",
            "基准为导入指数的收盘归一化毛收益，不包含 ETF 执行费用；盈亏颜色遵循 A 股红涨绿跌。",
        ],
    }


def example_dataset() -> dict[str, Any]:
    """Small explicitly synthetic fixture: never label it as strategy performance."""
    calendar = [date(2026, 8, day) for day in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)]
    opens = (10, 10.05, 10.12, 10.0, 10.18, 10.28, 10.25, 10.4, 10.3, 10.45)
    bars = [{"symbol": "600000.SH", "date": day.isoformat(), "open": price,
             "high": round(price + 0.25, 2), "low": round(price - 0.25, 2), "close": round(price + 0.06, 2),
             "volume": 1_000_000, "limit_up": 11.5, "limit_down": 8.5} for day, price in zip(calendar, opens)]
    return {"dataset_name": "合成数据 · 操作演示（不代表策略业绩）", "provenance": "synthetic_demo",
            "point_in_time_confirmed": True, "calendar_source": "2026-08 工作日合成演示日历", "price_basis": "unadjusted",
            "benchmark_name": "合成基准", "calendar": [d.isoformat() for d in calendar], "bars": bars,
            "signals": [{"signal_id": "demo-001", "symbol": "600000.SH", "signal_date": "2026-08-03",
                         "available_at": "2026-08-03T15:30:00+08:00", "source": "synthetic_demo", "side": "buy"}],
            "benchmark": [{"date": day.isoformat(), "close": 1000 + index * 2} for index, day in enumerate(calendar)],
            "config": AccountConfig().model_dump()}
