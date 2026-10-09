"""Frozen research queues survive display/ledger storage without becoming trades."""
from copy import deepcopy
from datetime import datetime
import json

from src.services.stock_king_display import LocalPicksDisplayStore
from src.services.yao_scout import signal_learning as ledger


NOW = datetime(2026, 10, 8, 10, 30, tzinfo=ledger.TZ)


def row(code, queue=None):
    signal = {"version": "next-day-limit-watch-v1", "selected": True,
              "signalSession": "2026-10-08", "targetSession": "2026-10-09", "score": 62}
    if queue:
        signal["queue"] = queue
    return {"code": code, "name": "离线样本", "decision_at": NOW.isoformat(),
            "signalPrice": 10, "quote": {"price": 10, "source_time": NOW.isoformat()},
            "status": "next_day_watch", "nextDaySignal": signal}


def scan():
    trade, watch, continuation = row("600001"), row("600002"), row("600003", "continuation")
    trade["status"] = "conditional"
    trade.pop("nextDaySignal")
    return {"run_id": "frozen-research", "scanSlot": "live", "status": "completed_observations",
            "generatedAt": NOW.isoformat(), "candidates": [trade],
            "nextDayWatchlist": [row("600001"), watch, deepcopy(watch)],
            "nextDayContinuationWatchlist": [continuation],
            "dataQuality": {"snapshot_count": 3000, "quote_coverage": {"requested": 3, "fresh": 3},
                            "minute_coverage": {"requested": 3, "usable": 0}}}


def test_display_counts_frozen_union_and_read_is_nonmutating(tmp_path, monkeypatch):
    monkeypatch.setenv("YAO_SCOUT_DATA_DIR", str(tmp_path))
    store = LocalPicksDisplayStore(None)
    result = scan()
    saved = store.save(result)
    assert saved["candidateCount"] == saved["tradeCandidateCount"] == 1
    assert saved["nextDayCandidateCount"] == saved["savedRecommendationCount"] == 2
    assert saved["continuationCandidateCount"] == 1
    assert saved["displayRunId"] == "frozen-research"
    assert saved["adaptive"] == result
    # Simulate an existing display saved before response summaries were added.
    old = {key: value for key, value in saved.items() if key not in {
        "tradeCandidateCount", "nextDayCandidateCount", "savedRecommendationCount",
        "continuationCandidateCount", "displayRunId"}}
    store.path.write_text(json.dumps(old), encoding="utf-8")
    before = store.path.read_bytes()
    assert store.read() == saved
    assert store.path.read_bytes() == before


def test_continuation_only_snapshot_is_saved_separately_from_main_recommendations(tmp_path, monkeypatch):
    monkeypatch.setenv("YAO_SCOUT_DATA_DIR", str(tmp_path))
    result = scan()
    result.update(candidates=[], nextDayWatchlist=[])
    saved = LocalPicksDisplayStore(None).save(result)
    assert saved["savedRecommendationCount"] == saved["candidateCount"] == 0
    assert saved["continuationCandidateCount"] == 1
    assert saved["adaptive"]["nextDayContinuationWatchlist"] == result["nextDayContinuationWatchlist"]


def test_research_queues_persist_once_without_entry_or_training_promotion(tmp_path, monkeypatch):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    # Even an accidentally entry-shaped watch-only row cannot become training.
    monkeypatch.setattr(ledger, "_signal_preflight", lambda *_: True)
    result = scan()
    original = deepcopy(result)
    saved = service.persist_scan(result, "1030", NOW, True)
    assert result == original
    summary = saved["learningLedger"]
    assert summary["selectedCount"] == 1
    assert summary["nextDaySelectedCount"] == summary["savedRecommendationCount"] == 2
    assert summary["continuationSelectedCount"] == 1
    history = service.history()["items"][0]
    events = {item["code"]: item for item in history["signals"]}
    assert len(events) == 3
    assert events["600001"]["selected"] and events["600001"]["trainingEligible"]
    assert events["600001"]["snapshot"]["status"] == "conditional"
    for code in ("600002", "600003"):
        assert not events[code]["selected"]
        assert not events[code]["trainingEligible"]
        assert not events[code]["reviewEligible"]
        assert events[code]["snapshot"]["nextDaySignal"]["selected"] is True
        assert events[code]["snapshot"]["nextDaySignal"]["version"] == "next-day-limit-watch-v1"
    assert events["600002"]["nextDaySelected"] is True
    assert events["600003"]["nextDayContinuationSelected"] is True
    assert events["600003"]["nextDaySelected"] is False
    assert events["600003"]["recommendationKinds"] == ["next_day_continuation"]
    # Replaying a changed payload cannot alter either saved selections or counts.
    replay = service.persist_scan({"run_id": "new-input", "candidates": [], "status": "completed_observations"}, "1030", NOW, True)
    assert replay["learningLedger"]["idempotentReplay"]
    assert replay["learningLedger"]["nextDaySelectedCount"] == 2
    assert replay["learningLedger"]["continuationSelectedCount"] == 1


def test_omitted_old_research_events_are_read_from_original_scan_without_rewriting(tmp_path):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    service.persist_scan(scan(), "live", NOW, False)
    with service._connect() as db:
        db.execute("DELETE FROM signal_events WHERE code IN ('600002','600003')")
    before = service.db_path.read_bytes()
    history = service.history()
    events = {item["code"]: item for item in history["items"][0]["signals"]}
    assert len(events) == 3
    for code in ("600002", "600003"):
        event = events[code]
        assert event["ledgerRecordOrigin"] == "saved_scan_projection"
        assert event["snapshot"]["nextDaySignal"]["version"] == "next-day-limit-watch-v1"
        assert not event["selected"] and not event["trainingEligible"] and not event["reviewEligible"]
        filtered = service.history({"symbol": code})
        assert [item["code"] for item in filtered["items"][0]["signals"]] == [code]
    assert "queue" not in events["600002"]["snapshot"]["nextDaySignal"]
    assert service.db_path.read_bytes() == before
    with service._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1
