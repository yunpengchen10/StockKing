"""Point-in-time ledger and conservative execution checks for King V1.1."""
from datetime import date, datetime, timedelta

import numpy as np

from src.services.yao_scout.signal_learning import SignalLearningService, TZ


def _clock(day: date, hour: int = 9, minute: int = 40, second: int = 35) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


def _candidate(code: str, day: date, *, price: float = 10.0) -> dict:
    return {
        "code": code, "name": "样本", "decision_at": _clock(day, second=30).isoformat(),
        "signalPrice": price, "referencePrice": price, "baselineHistoryDays": 5,
        "quote": {"price": price, "pre_close": price, "limit_up": 11.0,
                  "source_time": _clock(day, second=25).isoformat()},
        "tradability": {"status": "unverified"}, "finalScore": 70.0,
        "v11Inputs": {"r3_pct": 0.5}, "factorScores": {"momentum": 75.0},
    }


def _bars(days: list[date], *, locked_entry: bool = False) -> list[dict]:
    result = []
    for index, day in enumerate(days):
        close = 10.05 + index * 0.08
        minutes = ([(9, minute) for minute in range(31, 60)]
                   + [(10, minute) for minute in range(60)]
                   + [(11, minute) for minute in range(31)]
                   + [(13, minute) for minute in range(1, 60)]
                   + [(14, minute) for minute in range(60)] + [(15, 0)])
        for hour, minute in minutes:
            op, high, low, finish = close, close + 0.03, close - 0.03, close
            if index == 0 and (hour, minute) == (9, 41):
                op, high, low, finish = 10.0, 10.8, 9.95, 10.0
            if index == 0 and (hour, minute) == (9, 42):
                if locked_entry:
                    op, high, low, finish = 11.0, 11.0, 11.0, 11.0
                else:
                    op, high, low, finish = 10.0, 10.15, 9.9, 10.05
            stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
            result.append({"end": stamp.isoformat(), "open": op, "high": high,
                           "low": low, "close": finish, "volume_shares": 100000,
                           "amount_cny": 100000 * finish, "source": "test_one_minute"})
    return result


def test_persist_first_official_run_and_pending_reviews(tmp_path):
    days = [date(2026, 9, 7) + timedelta(days=i) for i in range(8)]
    service = SignalLearningService(tmp_path, calendar_provider=lambda a, b: [d for d in days if a <= d <= b],
                                    clock=lambda: _clock(days[0]))
    first = {"status": "completed_observations", "candidates": [_candidate("000001", days[0])],
             "controls": [_candidate("000002", days[0])], "modelVersion": "king-v1.1-rules"}
    saved = service.persist_scan(first, "0940", _clock(days[0]), True)
    replay = service.persist_scan({"status": "completed_observations", "candidates": []},
                                  "0940", _clock(days[0]), True)
    assert not saved["learningLedger"]["idempotentReplay"]
    assert replay["learningLedger"]["idempotentReplay"]
    assert len(replay["candidates"]) == 1
    history = service.history({"date": days[0].isoformat(), "symbol": "000001"})
    assert len(history["items"]) == 1
    assert history["items"][0]["modelVersion"] == "king-v1.1-rules"
    assert len(service.history({"version": "king-v1.1-rules"})["items"]) == 1
    assert len(history["items"][0]["signals"]) == 1
    assert len(service.reviews({"date": days[0].isoformat()})["items"]) == 6
    assert len(service.reviews({"version": "king-v1.1-rules"})["items"]) == 6
    assert {item["status"] for item in service.reviews()["items"]} == {"pending"}
    service.persist_scan({"status": "missed_slot", "candidates": [], "controls": [_candidate("000003", days[0])]},
                         "0955", _clock(days[0], 9, 55), True)
    assert not service.history({"symbol": "000003"})["items"][0]["signals"][0]["trainingEligible"]
    service.persist_scan(first, "live", _clock(days[0]), False)
    manual = next(item for item in service.history({"symbol": "000001"})["items"] if not item["official"])
    assert not manual["signals"][0]["trainingEligible"]
    assert manual["signals"][0]["reviewEligible"]


def test_reviews_mature_only_after_close_and_use_first_complete_minute(tmp_path):
    days = [date(2026, 9, 7) + timedelta(days=i) for i in range(6)]
    bars = _bars(days)
    service = SignalLearningService(tmp_path, bar_fetcher=lambda code, cutoff: bars,
                                    calendar_provider=lambda a, b: [d for d in days if a <= d <= b],
                                    clock=lambda: _clock(days[0]))
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000001", days[0])]},
                         "0940", _clock(days[0]), True)
    assert service.review_due(_clock(days[1], 14, 59))["updated"] == 0
    changed = service.review_due(_clock(days[5], 15, 30))
    assert changed["updated"] == 3
    reviews = service.reviews({"symbol": "000001"})["items"]
    assert {item["horizon"] for item in reviews} == {1, 3, 5}
    assert all(item["status"] == "mature" for item in reviews)
    assert all(item["entryAt"].endswith("09:42:00+08:00") for item in reviews)
    assert all(item["exitAt"].endswith("15:00:00+08:00") for item in reviews)
    assert all(item["observedMfe"] < 0.8 for item in reviews)
    assert all(item["mae"] >= 0 and item["observedMae"] >= 0 for item in reviews)
    assert all(item["evidenceGrade"] == "simulated_minute_proxy" for item in reviews)
    assert service.review_due(_clock(days[5], 15, 31))["updated"] == 0


def test_first_minute_limit_up_stays_cash_and_missing_source_limit_unverified(tmp_path):
    days = [date(2026, 9, 7) + timedelta(days=i) for i in range(6)]
    bars = _bars(days, locked_entry=True)
    service = SignalLearningService(tmp_path, bar_fetcher=lambda code, cutoff: bars,
                                    calendar_provider=lambda a, b: [d for d in days if a <= d <= b],
                                    clock=lambda: _clock(days[0]))
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000001", days[0])]},
                         "0940", _clock(days[0]), True)
    result = service.review_due(_clock(days[5], 15, 30))
    assert {item["status"] for item in result["items"]} == {"no_fill"}
    assert all(item["observedReturn"] is not None for item in result["items"])
    assert service._dataset(_clock(days[5], 15, 30))[0]["net"] == 0
    second = _candidate("000002", days[0])
    second["quote"].pop("limit_up")
    service.persist_scan({"status": "completed_observations", "candidates": [second]},
                         "0955", _clock(days[0], 9, 55), True)
    result = service.review_due(_clock(days[5], 15, 31))
    assert {item["status"] for item in result["items"]} == {"unverified"}


def test_account_gate_refuses_incomplete_same_queue_and_missing_nav_marks(tmp_path):
    service = SignalLearningService(tmp_path)
    item = {"day": "2026-09-07", "runId": "r", "code": "000001", "selected": True,
            "signalId": "s", "decisionAt": "2026-09-07T09:40:00+08:00",
            "features": {"finalScore": 80}, "filled": True,
            "entryAt": "2026-09-07T09:41:00+08:00", "entryPrice": 10.0,
            "exitAt": "2026-09-14T15:00:00+08:00", "exitPrice": 11.0,
            "entryMinuteVolume": 1000000, "marks": {"2026-09-07": 10.1},
            "net": 0.09, "mae": 0.02}
    failed = service._gates([item], np.array([90.0]), np.array([0.9]), seed=1,
                            config=service.execution_config, complete_universe=False)
    assert not failed["passed"] and failed["reason"] == "incomplete_same_queue_or_forward_predictions"
    failed = service._gates([item], np.array([90.0]), np.array([0.9]), seed=1,
                            config=service.execution_config, complete_universe=True)
    assert not failed["passed"] and failed["reason"] == "account_nav_unverifiable"


def test_incomplete_intermediate_minute_path_stays_pending(tmp_path):
    days = [date(2026, 9, 7) + timedelta(days=i) for i in range(6)]
    complete = _bars(days)
    missing_end = datetime(2026, 9, 9, 10, 0, tzinfo=TZ).isoformat()
    bars = [bar for bar in complete if bar["end"] != missing_end]
    service = SignalLearningService(tmp_path, bar_fetcher=lambda code, cutoff: bars,
                                    calendar_provider=lambda a, b: [d for d in days if a <= d <= b],
                                    clock=lambda: _clock(days[0]))
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000001", days[0])],
                          "scoreVersion": "stockking-v1.1-rules"}, "0940", _clock(days[0]), True)
    service.review_due(_clock(days[5], 15, 30))
    outcomes = {item["horizon"]: item for item in service.reviews()["items"]}
    assert outcomes[1]["status"] == "mature"
    assert outcomes[3]["status"] == outcomes[5]["status"] == "pending_data"
    assert outcomes[3]["reason"] == "incomplete_minute_path"
    bars.append(next(bar for bar in complete if bar["end"] == missing_end))
    assert service.review_due(_clock(days[5], 15, 31))["updated"] == 2
    outcomes = {item["horizon"]: item for item in service.reviews()["items"]}
    assert all(item["status"] == "mature" for item in outcomes.values())


def test_horizons_follow_injected_sessions_across_weekend_and_holiday(tmp_path):
    # Friday, Monday, Wednesday, Thursday, Friday, Monday; Tuesday is closed.
    sessions = [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 9),
                date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)]
    bars = _bars(sessions)
    service = SignalLearningService(tmp_path, bar_fetcher=lambda code, cutoff: bars,
                                    calendar_provider=lambda start, end: [day for day in sessions
                                                                          if start <= day <= end],
                                    clock=lambda: _clock(sessions[0]))
    service.persist_scan({"status": "completed_observations",
                          "candidates": [_candidate("000001", sessions[0])],
                          "scoreVersion": "stockking-v1.1-rules"},
                         "0940", _clock(sessions[0]), True)
    assert service.review_due(_clock(sessions[1], 14, 59))["updated"] == 0
    assert [item["horizon"] for item in service.review_due(_clock(sessions[1], 15, 30))["items"]] == [1]
    assert service.review_due(_clock(date(2026, 9, 8), 15, 30))["updated"] == 0
    assert service.review_due(_clock(sessions[2], 15, 30))["updated"] == 0
    assert service.review_due(_clock(sessions[3], 14, 59))["updated"] == 0
    assert [item["horizon"] for item in service.review_due(_clock(sessions[3], 15, 30))["items"]] == [3]
    assert service.review_due(_clock(sessions[5], 14, 59))["updated"] == 0
    assert [item["horizon"] for item in service.review_due(_clock(sessions[5], 15, 30))["items"]] == [5]
    by_horizon = {item["horizon"]: item for item in service.reviews()["items"]}
    assert {horizon: item["exitAt"][:10] for horizon, item in by_horizon.items()} == {
        1: "2026-09-07", 3: "2026-09-10", 5: "2026-09-14"}


def test_corrupt_or_nonfinite_champion_falls_back_to_rule_rank(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path)
    with service._connect() as db:
        db.execute("INSERT INTO learning_registry VALUES('v11',?,?)",
                   ('{"championVersion":"corrupt"}', _clock(date(2026, 9, 7)).isoformat()))
        db.execute("INSERT INTO model_versions VALUES(?,?,?,?,?)",
                   ("corrupt", "active", "not-json", "{}", _clock(date(2026, 9, 7)).isoformat()))
    candidates = [{"code": "000001", "finalScore": 20}, {"code": "000002", "finalScore": 80}]
    ranked = service.rank_candidates(candidates)
    assert [row["code"] for row in ranked] == ["000002", "000001"]
    assert all(row["learningRankVersion"] == "rules_v1.1" for row in ranked)
    monkeypatch.setattr(service, "_model_artifact", lambda version: {"version": "corrupt"})
    monkeypatch.setattr(service, "_predict_artifact", lambda artifact, rows: (
        np.array([np.nan, 80.0]), np.array([0.5, 0.5])))
    assert [row["code"] for row in service.rank_candidates(candidates)] == ["000002", "000001"]


def test_training_uses_separate_calibration_and_test_without_publishing(tmp_path, monkeypatch):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(date(2026, 9, 29), 15, 45))
    start = date(2026, 1, 5)
    samples = []
    for index in range(120):
        day = (start + timedelta(days=index)).isoformat()
        for number in range(10):
            x = ((index * 7 + number * 13) % 31) / 30
            net = 0.06 * x - 0.026 + 0.002 * ((index + number) % 3)
            samples.append({"day": day, "code": f"{number:06d}",
                            "features": {"x": x, "finalScore": 60 + 20 * x},
                            "net": net, "mae": 0.02 + 0.03 * (1 - x),
                            "marks": {day: 10.0}})
    monkeypatch.setattr(service, "_dataset", lambda now, dedupe=True: samples)
    observed = {}

    def capture(test, challenger, logistic, **kwargs):
        observed["test_days"] = len({item["day"] for item in test})
        observed["calibrated_range"] = (float(min(challenger)), float(max(challenger)))
        observed["logistic_finite"] = bool(np.all(np.isfinite(logistic)))
        return {"passed": False, "reason": "test_gate_rejected"}

    monkeypatch.setattr(service, "_gates", capture)
    state = service.train_and_evaluate(_clock(date(2026, 9, 29), 15, 45))
    assert observed["test_days"] == 24
    assert 0 <= observed["calibrated_range"][0] <= observed["calibrated_range"][1] <= 100
    assert observed["logistic_finite"]
    assert state["shadowVersion"] is None and state["championVersion"] is None
    assert state["reason"] == "challenger_holdout_gate_failed"
