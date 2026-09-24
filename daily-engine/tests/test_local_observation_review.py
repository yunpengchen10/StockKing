from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from src.services.yao_scout import local_observation_review as mod

NOW = datetime(2026, 9, 14, 15, 30, tzinfo=mod.TZ)


class MemoryDB:
    def __init__(self, rows):
        self.rows, self.state = rows, None

    def list_yao_runs(self, **kwargs):
        return deepcopy(self.rows)

    def get_yao_adaptive_state(self, key):
        return deepcopy(self.state)

    def save_yao_adaptive_state(self, key, state):
        assert key == mod.STATE_KEY
        self.state = deepcopy(state)


def quote(stamp=NOW, price=11, code="600001"):
    return {"code": code, "source": "fixture", "source_time": stamp.isoformat(), "price": price}


def row(run_id="scan-1", decision=None, code="600001", status="conditional"):
    decision = decision or NOW.replace(hour=10, minute=30)
    item = {"code": code, "decision_at": decision.isoformat(), "referencePrice": 10,
            "quote": quote(decision, 10, code), "status": status}
    return {"run_id": run_id, "mode": "king_1030", "result": {
        "as_of": decision.isoformat(), "candidates": [item]}}


def test_price_comparison_keeps_two_source_times_and_never_creates_trade_returns():
    db = MemoryDB([row()])
    result = mod.review_local_observations(db, lambda _: quote(), NOW)
    item = result["observations"][0]
    assert item["observation_change_pct"] == 10
    assert item["baseline_quote"]["source_time"] == "2026-09-14T10:30:00+08:00"
    assert item["review_quote"]["source_time"] == NOW.isoformat()
    assert result["verified_count"] == 1
    assert result["training_eligible"] is False and result["weights_changed"] is False
    assert "return" not in item and "win_rate" not in result
    assert "非成交收益" in result["meaning"]


def test_all_profiles_and_rejected_controls_are_reviewed_once():
    saved = row()
    pick = saved['result']['candidates'].pop()
    rejected = row(code='600002')['result']['candidates'][0]
    saved['result'].update(profileCandidates={'conservative': [], 'regular': [], 'aggressive': [pick]},
        precisionResearch=[pick, rejected], precisionWatchlist=[rejected])
    calls = []
    result = mod.review_local_observations(MemoryDB([saved]),
        lambda code: calls.append(code) or quote(code=code), NOW)
    assert sorted(calls) == ['600001', '600002']
    observations = {r['code']: r for r in result['observations']}
    assert observations['600001']['selected_profiles'] == ['aggressive']
    assert observations['600002']['selected_profiles'] == []
    assert not result['training_eligible']


@pytest.mark.parametrize("change", [
    {"source_time": None, "fetched_at": NOW.isoformat()},
    {"source_time": (NOW + timedelta(seconds=1)).isoformat()},
    {"source_time": (NOW - timedelta(days=1)).isoformat()},
    {"source_time": NOW.replace(hour=14, minute=55).isoformat()},
    {"code": "600002"}, {"price": 0}, {"source": ""}, {"is_stale": True},
])
def test_missing_stale_or_wrong_quote_stays_unknown_with_no_time_fabrication(change):
    db = MemoryDB([row()])
    result = mod.review_local_observations(db, lambda _: {**quote(), **change}, NOW)
    assert result["observations"][0]["status"] == "unknown"
    assert result["observations"][0]["observation_change_pct"] is None
    assert result["unknown_count"] == 1


def test_baseline_source_must_exist_and_match_its_saved_price_and_decision():
    rows = [row("missing"), row("mismatch"), row("late-source")]
    rows[0]["result"]["candidates"][0]["quote"].pop("source_time")
    rows[1]["result"]["candidates"][0]["referencePrice"] = 9
    rows[2]["result"]["candidates"][0]["quote"]["source_time"] = NOW.isoformat()
    result = mod.review_local_observations(MemoryDB(rows), lambda _: quote(), NOW)
    assert result["verified_count"] == 0
    assert all(item["observation_change_pct"] is None for item in result["observations"])
    assert any("不一致" in gap for item in result["observations"] for gap in item["gaps"])


def test_individual_failures_dedup_and_cutoff_preserve_coverage():
    rows = [row(), row(), row("other", code="600002"),
            row("yesterday", NOW - timedelta(days=1)), row("future", NOW + timedelta(days=1))]
    calls = []
    def fetch(code):
        calls.append(code)
        if code == "600002":
            raise RuntimeError("fixture upstream unavailable")
        return quote()
    result = mod.review_local_observations(MemoryDB(rows), fetch, NOW)
    assert calls == ["600001", "600002"]
    assert result["observation_count"] == 2
    assert result["verified_count"] == result["unknown_count"] == 1


def test_reminders_persist_in_actual_sqlite_and_review_rerun_does_not_double_count(tmp_path, monkeypatch):
    from src.storage import DatabaseManager
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    db_url = "sqlite:///" + str(tmp_path / "observations.db").replace("\\", "/")
    db = DatabaseManager(db_url)
    rows = [row("gap-1"), row("gap-2", status="windvane")]
    for original in rows:
        saved = {**original["result"], "run_id": original["run_id"], "mode": original["mode"],
                 "training_eligible": False, "status": "completed_observations"}
        db.save_yao_run(saved)
    for _ in range(2):
        mod.review_local_observations(db, lambda _: {}, NOW)
    db._engine.dispose()
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    restarted = DatabaseManager(db_url)
    try:
        reminders = mod.read_observation_reminders(restarted, "600001", NOW + timedelta(days=1))
        assert len(reminders) == 2
        assert "此前 2 条" in reminders[0]
        assert "风向标" in reminders[1]
        assert mod.read_observation_reminders(restarted, "600002", NOW + timedelta(days=1)) == []
        assert mod.read_observation_reminders(restarted, "600001", NOW - timedelta(days=1)) == []
        assert restarted.get_yao_adaptive_state(mod.STATE_KEY)["latest_review"]["weights_changed"] is False
    finally:
        restarted._engine.dispose()


@pytest.mark.parametrize("extra_issue, expected", [
    (None, "observed"), ("cross_source_price_conflict", "unknown"),
    ("invalid_order_book", "unknown"), ("future_source_time", "unknown"),
])
def test_actual_public_adapter_postclose_reference_is_reviewable_but_conflicts_are_not(extra_issue, expected):
    from src.services.public_market_quotes import adapt_public_quote
    source = {**quote(NOW.replace(hour=15, minute=0)), "previous_close": 10,
              "open": 10, "volume_shares": 100, "amount_cny": 1100}
    issues = ["stale_quote", "request_phase_post_close", "source_phase_post_close",
              "post_close_cannot_reconstruct_1455"]
    if extra_issue:
        issues.append(extra_issue)
    assessment = {"code": "600001", "quote": source, "status": "reference_only",
        "phase": "post_close", "same_market_date": True, "issues": issues,
        "quote_usable_for_current_price_check": False, "execution_verified": False}
    adapted = adapt_public_quote({"checked_at": NOW.isoformat(), "quotes": [assessment]}, assessment)
    assert adapted["is_stale"] is True
    result = mod.review_local_observations(MemoryDB([row()]), lambda _: adapted, NOW)
    assert result["observations"][0]["status"] == expected
    assert (result["observations"][0]["observation_change_pct"] == 10) == (expected == "observed")


@pytest.mark.parametrize("check_delta, source_delta, expected", [
    (timedelta(seconds=5), timedelta(seconds=4), "observed"),
    (timedelta(seconds=5), timedelta(seconds=6), "unknown"),
    (timedelta(days=1), timedelta(days=1), "unknown"),
    (timedelta(seconds=-1), timedelta(seconds=-1), "unknown"),
])
def test_actual_fetch_completion_clock_accepts_later_source_but_not_future_or_cross_day(check_delta, source_delta, expected):
    elapsed = [NOW]
    def fetch(_code):
        elapsed[0] = NOW + check_delta
        return quote(NOW + source_delta)
    result = mod.review_local_observations(MemoryDB([row()]), fetch, NOW, clock=lambda: elapsed[0])
    reviewed = result["observations"][0]
    assert reviewed["status"] == expected
    assert reviewed["review_quote"]["source_time"] == (NOW + source_delta).isoformat()
    assert reviewed["review_quote"]["checked_at"] == (NOW + check_delta).isoformat()
    assert result["reviewed_at"] == NOW.isoformat()
