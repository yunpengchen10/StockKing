from datetime import datetime, timedelta, timezone
import math

import pytest

from src.services.yao_scout.trend_evidence import (
    SHANGHAI, TREND_EVIDENCE_VERSION, compute_trend_evidence,
)


START = datetime(2026, 9, 15, 10, 0, tzinfo=SHANGHAI)


def fixture(prices, *, start=START, amounts=None):
    amounts = amounts if amounts is not None else [1000.0] * len(prices)
    bars = {
        start + timedelta(minutes=i): {"close": price, "amount_cny": amount}
        for i, (price, amount) in enumerate(zip(prices, amounts))
    }
    return bars, max(bars)


def test_monotonic_log_trend_is_efficient_with_no_close_drawdown():
    bars, end = fixture([10 * math.exp(i * 0.001) for i in range(31)])
    evidence = compute_trend_evidence(bars, end, {at: 9.0 for at in bars})
    assert evidence["version"] == TREND_EVIDENCE_VERSION
    assert evidence["validationStatus"] == "unvalidated_descriptive"
    assert evidence["missingReasons"] == {}
    for n in (15, 30):
        metrics = evidence["metrics"]
        assert metrics[f"signed_log_efficiency_{n}m"] == pytest.approx(1)
        assert metrics[f"normalized_log_slope_{n}m"] == pytest.approx(1)
        assert metrics[f"close_max_drawdown_{n}m_pct"] == 0
        assert metrics[f"close_pullback_to_rise_{n}m"] == 0
        assert metrics[f"signed_amount_proxy_{n}m"] == 1
        assert metrics[f"vwap_occupancy_{n}m"] == 1


def test_equal_endpoint_rise_can_have_a_very_different_path():
    steady, end = fixture([10 * math.exp(i * 0.001) for i in range(31)])
    choppy, _ = fixture([10 * math.exp(i * 0.001 + (0.015 if i % 2 else 0)) for i in range(31)])
    left = compute_trend_evidence(steady, end, {})["metrics"]
    right = compute_trend_evidence(choppy, end, {})["metrics"]
    assert steady[end]["close"] == choppy[end]["close"]
    assert left["signed_log_efficiency_30m"] > right["signed_log_efficiency_30m"] > 0
    assert right["signed_log_efficiency_30m"] < 0.1
    assert right["close_max_drawdown_30m_pct"] > left["close_max_drawdown_30m_pct"]


def test_rise_then_fade_reports_close_drawdown_and_giveback():
    prices = [10 + i * 0.1 for i in range(16)] + [11.5 - i * 0.15 for i in range(1, 16)]
    bars, end = fixture(prices)
    result = compute_trend_evidence(bars, end, {})
    assert result["metrics"]["close_max_drawdown_30m_pct"] == pytest.approx(100 * (1 - 9.25 / 11.5))
    assert result["metrics"]["close_pullback_to_rise_30m"] > 1
    assert result["metrics"]["signed_log_efficiency_30m"] < 0
    assert result["metrics"]["signed_log_efficiency_15m"] == pytest.approx(-1)


@pytest.mark.parametrize("direction", [0, -1])
def test_flat_and_declining_paths_are_not_positive_trends(direction):
    bars, end = fixture([10 * math.exp(direction * i * 0.001) for i in range(31)])
    evidence = compute_trend_evidence(bars, end, {})
    for n in (15, 30):
        assert evidence["metrics"][f"signed_log_efficiency_{n}m"] == pytest.approx(direction)
        assert evidence["metrics"][f"normalized_log_slope_{n}m"] == pytest.approx(direction)
        assert evidence["metrics"][f"close_pullback_to_rise_{n}m"] is None
        assert evidence["missingReasons"][f"close_pullback_to_rise_{n}m"] == "no_positive_close_rise_in_window"


def test_signed_amount_is_interval_weighted_price_direction_proxy():
    prices = [10 + (i % 2) for i in range(16)]
    # Initial-bar amount is outside the 15 returns and must not enter the sum.
    amounts = [1e12] + [200 if i % 2 else 100 for i in range(1, 16)]
    bars, end = fixture(prices, amounts=amounts)
    result = compute_trend_evidence(bars, end, {})
    assert result["metrics"]["signed_amount_proxy_15m"] == pytest.approx((8 * 200 - 7 * 100) / (8 * 200 + 7 * 100))


def test_vwap_occupancy_uses_each_minutes_own_vwap():
    bars, end = fixture([10 + i for i in range(16)])
    vwaps = {at: bar["close"] - 0.1 for at, bar in bars.items()}
    vwaps[end] = bars[end]["close"] + 0.1
    result = compute_trend_evidence(bars, end, vwaps)
    assert result["metrics"]["vwap_occupancy_15m"] == pytest.approx(14 / 15)
    # An equal VWAP is occupied; absent initial VWAP is outside the 15 endpoints.
    vwaps.pop(START)
    vwaps[end] = bars[end]["close"]
    assert compute_trend_evidence(bars, end, vwaps)["metrics"]["vwap_occupancy_15m"] == 1


def test_missing_volume_or_amount_does_not_erase_price_features():
    bars, end = fixture([10 + i * 0.01 for i in range(31)])
    bars[end]["amount_cny"] = None
    # A caller cannot construct valid VWAP when its interval volumes are missing.
    result = compute_trend_evidence(bars, end, {})
    assert result["metrics"]["signed_log_efficiency_30m"] == pytest.approx(1)
    assert result["metrics"]["signed_amount_proxy_30m"] is None
    assert result["metrics"]["vwap_occupancy_30m"] is None


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf")])
def test_invalid_or_zero_turnover_does_not_make_a_flow_value(amount):
    bars, end = fixture([10 + i * 0.01 for i in range(16)], amounts=[amount] * 16)
    assert compute_trend_evidence(bars, end, {})["metrics"]["signed_amount_proxy_15m"] is None


def test_future_and_different_day_values_cannot_change_prefix_evidence():
    bars, end = fixture([10 + i * 0.01 for i in range(31)])
    vwaps = {at: 10.0 for at in bars}
    before = compute_trend_evidence(bars, end, vwaps)
    bars[end + timedelta(minutes=1)] = {"close": 1e9, "amount_cny": 1e15}
    bars[end + timedelta(days=1)] = {"close": 0, "amount_cny": -1}
    vwaps[end + timedelta(minutes=1)] = 1e-9
    assert compute_trend_evidence(bars, end, vwaps) == before


def test_a_gap_is_not_replaced_with_an_older_observation():
    bars, end = fixture([10 + i * 0.01 for i in range(32)])
    del bars[end - timedelta(minutes=7)]
    evidence = compute_trend_evidence(bars, end, {})
    assert all(value is None for value in evidence["metrics"].values())
    assert set(evidence["missingReasons"].values()) == {"missing_contiguous_minute_bars"}


def test_lunch_and_opening_boundaries_are_not_bridged():
    for hour, minute in ((9, 31), (13, 1)):
        start = START.replace(hour=hour, minute=minute)
        bars, end = fixture([10 + i * 0.01 for i in range(15)], start=start)
        for i in range(31):
            bars[end - timedelta(minutes=i)] = {"close": 10.0, "amount_cny": 1000}
        evidence = compute_trend_evidence(bars, end, {})
        assert all(value is None for value in evidence["metrics"].values())
        assert set(evidence["missingReasons"].values()) == {"window_crosses_session_boundary"}


def test_requires_aware_minute_endpoint_and_accepts_equivalent_utc_keys():
    bars, end = fixture([10 + i * 0.01 for i in range(16)])
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_trend_evidence(bars, end.replace(tzinfo=None), {})
    with pytest.raises(ValueError, match="minute boundary"):
        compute_trend_evidence(bars, end + timedelta(seconds=1), {})
    naive = {at.replace(tzinfo=None): bar for at, bar in bars.items()}
    assert compute_trend_evidence(naive, end, {})["metrics"]["signed_log_efficiency_15m"] is None
    utc = {at.astimezone(timezone.utc): bar for at, bar in bars.items()}
    assert compute_trend_evidence(utc, end.astimezone(timezone.utc), {}) == compute_trend_evidence(bars, end, {})


@pytest.mark.parametrize("close", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_closes_fail_their_window_without_partial_calculation(close):
    bars, end = fixture([10 + i * 0.01 for i in range(31)])
    bars[START]["close"] = close
    result = compute_trend_evidence(bars, end, {})
    assert result["metrics"]["signed_log_efficiency_30m"] is None
    assert result["metrics"]["signed_log_efficiency_15m"] == pytest.approx(1)
