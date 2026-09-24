from datetime import datetime, timedelta

import pytest
import requests

from src.services.yao_scout.intraday_evidence import (
    SHANGHAI, compute_intraday_evidence, fetch_intraday_evidence,
    parse_tencent_intraday,
)


DAY = datetime(2026, 9, 15, 9, 30, tzinfo=SHANGHAI)


def rows(day=DAY, count=12, amount_step=1000):
    return [{
        "source_time": (day + timedelta(minutes=index)).isoformat(),
        "price": 10 + index / 10,
        "cumulative_amount_cny": 10000 + index * amount_step,
        "cumulative_volume_shares": 1000 + index * 100,
    } for index in range(count)]


def test_metrics_use_completed_minutes_and_no_future_rows():
    evidence = compute_intraday_evidence(rows(), DAY + timedelta(minutes=10, seconds=15))
    metrics = evidence["metrics"]
    assert evidence["asOf"] == (DAY + timedelta(minutes=9)).isoformat()
    assert metrics["speed_1m_pct"] == pytest.approx((10.9 / 10.8 - 1) * 100, abs=1e-6)
    assert metrics["speed_3m_pct"] == pytest.approx((10.9 / 10.6 - 1) * 100, abs=1e-6)
    assert metrics["speed_5m_pct"] == pytest.approx((10.9 / 10.4 - 1) * 100, abs=1e-6)
    assert metrics["vwap"] == 10
    assert metrics["local_high_5m"] == 10.8  # excludes newest price, allowing breakthrough comparison
    assert metrics["local_low_5m"] == 10.4
    assert metrics["amount_3m"] == 3000
    assert metrics["relative_amount_3m_20d"] is None


def test_same_time_median_requires_20_earlier_days_and_excludes_future():
    past = [row for day in range(1, 21) for row in rows(DAY - timedelta(days=day), amount_step=500)]
    dates = [(DAY - timedelta(days=day)).date() for day in range(1, 21)]
    future = rows(DAY + timedelta(days=1), amount_step=100000)
    current = rows(amount_step=2000)
    evidence = compute_intraday_evidence(current, DAY + timedelta(minutes=10), past + current + future, expected_history_dates=dates)
    assert evidence["historyDays"] == 20
    assert evidence["metrics"]["relative_amount_3m_20d"] == 4
    partial = compute_intraday_evidence(current, DAY + timedelta(minutes=10), past[:19 * 12], expected_history_dates=dates)
    assert partial["historyDays"] == 19
    assert partial["metrics"]["relative_amount_3m_20d"] is None


def test_older_complete_days_do_not_replace_missing_recent_sessions():
    dates = [(DAY - timedelta(days=day)).date() for day in range(1, 21)]
    past = [row for day in range(2, 22) for row in rows(DAY - timedelta(days=day), amount_step=500)]
    evidence = compute_intraday_evidence(rows(), DAY + timedelta(minutes=10), past, expected_history_dates=dates)
    assert evidence["historyDays"] == 19
    assert evidence["metrics"]["relative_amount_3m_20d"] is None


def test_missing_minute_is_not_interpolated_or_skipped():
    observed = rows()
    del observed[7]
    evidence = compute_intraday_evidence(observed, DAY + timedelta(minutes=10))
    assert evidence["metrics"]["speed_1m_pct"] is not None
    assert evidence["metrics"]["speed_3m_pct"] is None
    assert evidence["metrics"]["amount_3m"] is None


def test_lunch_break_does_not_become_last_three_observations():
    observed = rows(DAY.replace(hour=11, minute=25), count=6) + rows(DAY.replace(hour=13, minute=0), count=2)
    evidence = compute_intraday_evidence(observed, DAY.replace(hour=13, minute=2))
    assert evidence["metrics"]["speed_1m_pct"] is not None
    assert evidence["metrics"]["speed_3m_pct"] is None


def test_five_minute_stale_data_is_not_an_immediate_signal():
    evidence = compute_intraday_evidence(rows(count=10), DAY + timedelta(minutes=14))
    assert evidence["sourceAgeSeconds"] == 300
    assert not any(value is not None for value in evidence["metrics"].values())


def test_amount_missing_or_cumulative_reset_does_not_create_volume_evidence():
    observed = rows()
    observed[9]["cumulative_amount_cny"] = 1
    evidence = compute_intraday_evidence(observed, DAY + timedelta(minutes=10))
    assert evidence["metrics"]["vwap"] is None
    assert evidence["metrics"]["amount_3m"] is None
    assert evidence["metrics"]["speed_3m_pct"] is not None


def payload(code="600519", dated=True):
    day = {"data": ["0930 10.00 10 10000", "0931 10.10 20 20100", "1530 9.00 30 29000"]}
    if dated:
        day["date"] = "20260915"
    return {"code": 0, "data": {"sh" + code: {"data": [day]}}}


def test_parser_preserves_supplier_date_units_and_discards_postmarket_rows():
    observed = parse_tencent_intraday(payload(), "600519")
    assert len(observed) == 2
    assert observed[0]["source_time"] == DAY.isoformat()
    assert observed[0]["cumulative_volume_shares"] == 1000
    assert observed[0]["cumulative_amount_cny"] == 10000
    with pytest.raises(ValueError):
        parse_tencent_intraday(payload(dated=False), "600519")


def test_current_minute_shape_and_unknown_amount_remain_honest():
    source = payload()
    source["data"]["sh600519"]["data"] = source["data"]["sh600519"]["data"][0]
    source["data"]["sh600519"]["data"]["data"][0] = "0930 10 10"
    observed = parse_tencent_intraday(source, "600519")
    assert observed[0]["cumulative_amount_cny"] is None


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return payload()


def test_fetch_cache_uses_real_supplier_minutes_and_staleness_on_fallback(tmp_path):
    result = fetch_intraday_evidence("600519", DAY + timedelta(minutes=2), tmp_path, fetcher=lambda *_a, **_k: Response())
    assert result["fetchedNow"] is True
    assert result["asOf"] == (DAY + timedelta(minutes=1)).isoformat()
    assert len(list(tmp_path.glob("600519_*.json"))) == 1

    def failed(*args, **kwargs):
        raise requests.Timeout("offline")

    cached = fetch_intraday_evidence("600519", DAY + timedelta(minutes=7), tmp_path, fetcher=failed)
    assert cached["fetchedNow"] is False
    assert cached["sourceAgeSeconds"] == 360
    assert cached["metrics"]["speed_1m_pct"] is None
    assert any("Timeout" in gap for gap in cached["gaps"])


def test_conflicting_duplicate_minute_invalidates_affected_window():
    observed = rows()
    observed.append({**observed[8], "price": 20})
    evidence = compute_intraday_evidence(observed, DAY + timedelta(minutes=10))
    assert evidence["metrics"]["speed_1m_pct"] is None
    assert any("冲突" in gap for gap in evidence["gaps"])


@pytest.mark.parametrize("code", ["600519/../secret", "sh600519", "123456", "600519?q=x", 600519])
def test_only_bounded_mainboard_symbol_parameters(code):
    with pytest.raises(ValueError):
        fetch_intraday_evidence(code, DAY)
