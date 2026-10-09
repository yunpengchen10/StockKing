"""Review coverage and observations remain independent from execution learning."""
from datetime import date, timedelta
import pytest

from src.services.yao_scout.next_day_watch import VERSION, read_next_day_reviews
from src.services.yao_scout.signal_learning import SignalLearningService
from tests.test_signal_learning import _bars, _candidate, _clock


DAY = date(2026, 9, 7)
DAYS = [DAY + timedelta(days=index) for index in range(6)]


def calendar(start, end):
    return [day for day in DAYS if start <= day <= end]


def watch(code="000003", *, selected=True):
    return {"code": code, "name": "研究样本", "decision_at": _clock(DAY).isoformat(),
            "nextDaySignal": {"version": VERSION, "signalSession": DAY.isoformat(),
                              "queue": "first_board", "selected": selected}}


def test_archive_queue_captures_unmatured_signals_and_skips_settled_reviews(tmp_path):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: _bars(DAYS))
    invalid_entry = _candidate("000002", DAY)
    invalid_entry["quote"].pop("limit_up")
    service.persist_scan({"status": "completed_observations",
                          "candidates": [_candidate("000001", DAY), invalid_entry],
                          "nextDayWatchlist": [watch()]}, "0940", _clock(DAY), True)
    before = service.db_path.read_bytes()
    assert service.review_codes(_clock(DAY, 9, 39)) == []
    assert set(service.review_codes(_clock(DAY, 15, 30))) == {"000001", "000002", "000003"}
    assert service.review_requirements(_clock(DAYS[1], 15, 30)) == {
        '000001': [day.isoformat() for day in DAYS[:2]],
        '000002': [day.isoformat() for day in DAYS[:2]],
        '000003': [day.isoformat() for day in DAYS[:2]]}
    assert service.db_path.read_bytes() == before
    service.review_due(_clock(DAYS[-1], 15, 30))
    assert service.review_codes(_clock(DAYS[-1], 15, 30)) == []
    with service._connect() as db:
        db.execute("DELETE FROM signal_outcomes WHERE horizon=5 AND signal_id IN "
                   "(SELECT signal_id FROM signal_events WHERE code='000001')")
    assert service.review_codes(_clock(DAYS[-1], 15, 30)) == ["000001"]


@pytest.mark.parametrize("failure", ["missing_limit", "stale_source", "conflicting_limit"])
def test_execution_rejection_keeps_complete_price_observation_without_training(tmp_path, failure):
    candidate = _candidate("000001", DAY)
    if failure == "missing_limit":
        candidate["quote"].pop("limit_up")
    elif failure == "stale_source":
        candidate["quote"]["source_time"] = _clock(DAY, 9, 39).isoformat()
    else:
        candidate["quote"]["limit_up"] = 15
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: _bars(DAYS))
    service.persist_scan({"status": "completed_observations", "candidates": [candidate]},
                         "0940", _clock(DAY), True)
    changed = service.review_due(_clock(DAYS[-1], 15, 30))
    assert changed["updated"] == 3
    for row in service.reviews()["items"]:
        assert row["status"] == "unverified"
        assert row["simulatedNetReturn"] is None
        assert row["observedReturn"] > 0
        assert row["observedMfe"] >= 0 and row["observedMae"] >= 0
        assert row["observationStatus"] == "complete"
    assert service._dataset(_clock(DAYS[-1], 15, 30)) == []
    assert service.review_due(_clock(DAYS[-1], 15, 31))["updated"] == 0


@pytest.mark.parametrize("failure,reason", [
    ("missing_source", "signal_reference_source_time_missing"),
    ("future_source", "signal_reference_source_time_in_future"),
    ("missing_price", "signal_reference_price_unverified"),
    ("mismatched_price", "signal_reference_price_unverified"),
])
def test_unverifiable_frozen_reference_cannot_become_observation(tmp_path, failure, reason):
    candidate = _candidate("000001", DAY)
    if failure == "missing_source":
        candidate["quote"].pop("source_time")
    elif failure == "future_source":
        candidate["quote"]["source_time"] = _clock(DAY, 10, 0).isoformat()
    elif failure == "missing_price":
        candidate["quote"].pop("price")
    else:
        candidate["quote"]["price"] = 11
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: _bars(DAYS))
    service.persist_scan({"status": "completed_observations", "candidates": [candidate]},
                         "0940", _clock(DAY), True)
    service.review_due(_clock(DAYS[-1], 15, 30))
    for row in service.reviews()["items"]:
        assert row["status"] == "unverified" and row["observedReturn"] is None
        assert row["observationStatus"] == "unavailable" and row["observationReason"] == reason
    assert service.review_codes(_clock(DAYS[-1], 15, 30)) == []
    assert service._dataset(_clock(DAYS[-1], 15, 30)) == []


def test_legacy_unverified_observations_retry_missing_minutes_without_changing_execution(tmp_path):
    complete = _bars(DAYS)
    # A target-day close alone cannot settle a full-window price observation.
    rows = [bar for bar in complete if bar["end"].endswith("15:00:00+08:00")]
    candidate = _candidate("000001", DAY)
    candidate["quote"].pop("limit_up")
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: rows)
    service.persist_scan({"status": "completed_observations", "candidates": [candidate]},
                         "0940", _clock(DAY), True)
    service.review_due(_clock(DAYS[-1], 15, 30))
    for row in service.reviews()["items"]:
        assert row["status"] == "unverified" and row["observedReturn"] is None
        assert row["reason"] == "point_in_time_price_limit_missing"
        assert row["observationReason"] == "incomplete_minute_path"
    assert service.review_codes(_clock(DAYS[-1], 15, 30)) == ["000001"]
    # These are the shape of old terminal rows, without observation retry flags.
    with service._connect() as db:
        db.execute("UPDATE signal_outcomes SET details_json='{}'")
    rows[:] = complete
    result = service.review_due(_clock(DAYS[-1], 15, 31))
    assert result["updated"] == 3
    assert all(row["status"] == "unverified" and row["observedReturn"] is not None for row in result["items"])
    assert service.review_codes(_clock(DAYS[-1], 15, 31)) == []
    assert service._dataset(_clock(DAYS[-1], 15, 31)) == []


def test_known_corporate_action_never_becomes_comparable_observation(tmp_path):
    rows = _bars(DAYS)
    for bar in rows:
        if bar["end"].startswith(DAYS[1].isoformat()):
            bar["corporate_action"] = True
    candidate = _candidate("000001", DAY)
    candidate["quote"].pop("limit_up")
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: rows)
    service.persist_scan({"status": "completed_observations", "candidates": [candidate]},
                         "0940", _clock(DAY), True)
    service.review_due(_clock(DAYS[-1], 15, 30))
    assert all(row["observedReturn"] is None for row in service.reviews()["items"])
    assert all(row["observationStatus"] == "unavailable" for row in service.reviews()["items"])
    assert service.review_codes(_clock(DAYS[-1], 15, 30)) == []


def test_next_day_read_distinguishes_not_due_never_run_and_failed_data_without_writes(tmp_path):
    clock = [_clock(DAY)]
    bars = []
    service = SignalLearningService(tmp_path, clock=lambda: clock[0],
        calendar_provider=calendar, bar_fetcher=lambda *_: bars)
    service.persist_scan({"status": "completed_observations", "nextDayWatchlist": [watch()],
                          "controls": [watch("000004", selected=False)]}, "0940", clock[0], True)
    before = service.db_path.read_bytes()
    pending = read_next_day_reviews(service)
    assert pending["statusCounts"] == {"pending": 2}
    assert all(item["reviewedAt"] is None and item["signalDate"] == DAY.isoformat()
               and item["targetSession"] == DAYS[1].isoformat() for item in pending["items"])
    assert service.db_path.read_bytes() == before
    clock[0] = _clock(DAYS[1], 15, 30)
    due = read_next_day_reviews(service)
    assert due["statusCounts"] == {"review_not_run": 2}
    assert {item["code"]: item["selected"] for item in due["items"]} == {"000003": True, "000004": False}
    assert service.db_path.read_bytes() == before
    service.calendar_provider = lambda *_: []
    assert read_next_day_reviews(service)["statusCounts"] == {"calendar_unavailable": 2}
    service.calendar_provider = calendar
    service.review_due(clock[0])
    failed = read_next_day_reviews(service)
    assert failed["statusCounts"] == {"incomplete": 2}
    assert all(item["reviewedAt"] == clock[0].isoformat() for item in failed["items"])
    bars[:] = _bars(DAYS)
    service.review_due(clock[0] + timedelta(minutes=1))
    mature = read_next_day_reviews(service, {"symbol": "000003"})
    assert mature["count"] == 1 and mature["statusCounts"] == {"mature_price_pattern": 1}
    assert mature["items"][0]["executionVerified"] is False
    assert service.review_codes(clock[0]) == ["000004"]  # execution control still awaits T+3/T+5


def test_next_day_ranking_does_not_consume_trading_model_scores():
    from tests.test_next_day_watch import candidate, calendar as research_calendar, NOW
    from src.services.yao_scout.next_day_watch import build_next_day_watchlist
    rows = [candidate("600001"), candidate("600002")]
    original = build_next_day_watchlist(rows, NOW, calendar_provider=research_calendar)
    rows[0].update(learningRankScore=-1000, learningRankVersion="champion")
    rows[1].update(learningRankScore=1000, learningRankVersion="champion")
    changed = build_next_day_watchlist(rows, NOW, calendar_provider=research_calendar)
    assert [(item["code"], item["nextDaySignal"]["score"]) for item in original["nextDayWatchlist"]] == [
        (item["code"], item["nextDaySignal"]["score"]) for item in changed["nextDayWatchlist"]]


def test_small_review_page_keeps_selected_recommendations_ahead_of_controls(tmp_path):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY), calendar_provider=calendar)
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000009", DAY)],
                          "controls": [_candidate("000001", DAY), watch("000002", selected=False)],
                          "nextDayWatchlist": [watch("000008")]}, "0940", _clock(DAY), True)
    rows = service.reviews({"limit": 1})
    assert rows["items"][0]["code"] == "000009" and rows["items"][0]["selected"]
    assert rows["nextDayReview"]["items"][0]["code"] == "000008"
    assert rows["nextDayReview"]["items"][0]["selected"]


def test_newer_controls_cannot_hide_older_selected_reviews(tmp_path):
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAYS[1]), calendar_provider=calendar)
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000009", DAY)],
                          "nextDayWatchlist": [watch("000008")]}, "0940", _clock(DAY), True)
    newer = watch("000001", selected=False)
    newer.update(decision_at=_clock(DAYS[1]).isoformat())
    newer['nextDaySignal']['signalSession'] = DAYS[1].isoformat()
    service.persist_scan({"status": "completed_observations", "candidates": [],
                          "controls": [_candidate("000002", DAYS[1]), newer]}, "0940", _clock(DAYS[1]), True)
    rows = service.reviews({'limit': 1})
    assert rows['items'][0]['code'] == '000009' and rows['items'][0]['signalDate'] == DAY.isoformat()
    assert rows['nextDayReview']['items'][0]['code'] == '000008'


@pytest.mark.parametrize("suspended,volume", [(True, 1000), (False, 0)])
def test_inactive_next_session_is_not_a_mature_negative_label(suspended, volume):
    from tests.test_next_day_watch import bars, signal, calendar as research_calendar, NEXT
    from src.services.yao_scout.next_day_watch import next_day_outcome
    rows = bars()
    for row in rows[1:]:
        row.update(suspended=suspended, volume_shares=volume)
    outcome = next_day_outcome(signal(), rows, _clock(NEXT, 15, 30), calendar_provider=research_calendar)
    assert outcome["status"] == "incomplete"
    assert outcome["touchLimit"] is None and outcome["closeAtLimit"] is None


@pytest.mark.parametrize("suspended", [True, False])
def test_inactive_target_does_not_hide_an_open_simulated_position(tmp_path, suspended):
    rows = _bars(DAYS[:2])
    for row in rows:
        if row["end"].startswith(DAYS[1].isoformat()):
            row.update(suspended=suspended, volume_shares=100000 if suspended else 0)
            if not suspended:
                row["amount_cny"] = 0
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: rows)
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000001", DAY)]},
                         "0940", _clock(DAY), True)
    service.review_due(_clock(DAYS[1], 15, 30))
    row = next(item for item in service.reviews()["items"] if item["horizon"] == 1)
    assert row["status"] == "exit_blocked" and row["positionOpen"]
    assert row["observationStatus"] == "unavailable" and row["observedReturn"] is None
    assert row["simulatedNetReturn"] is None


@pytest.mark.parametrize("flag", ["corporate_action", "no_price_limit"])
def test_deferred_exit_special_session_does_not_become_mature_training_label(tmp_path, flag):
    rows = _bars(DAYS[:3])
    for row in rows:
        if row["end"] == _clock(DAYS[1], 15, 0, 0).isoformat():
            row.update(volume_shares=0, amount_cny=0)
        elif row["end"].startswith(DAYS[2].isoformat()):
            row[flag] = True
    service = SignalLearningService(tmp_path, clock=lambda: _clock(DAY),
        calendar_provider=calendar, bar_fetcher=lambda *_: rows)
    service.persist_scan({"status": "completed_observations", "candidates": [_candidate("000001", DAY)]},
                         "0940", _clock(DAY), True)
    service.review_due(_clock(DAYS[2], 15, 30))
    row = next(item for item in service.reviews()["items"] if item["horizon"] == 1)
    assert row["status"] == "pending_data" and row["positionOpen"]
    assert row["reason"] == "price_limit_or_corporate_action_unverifiable"
    assert row["simulatedNetReturn"] is None
