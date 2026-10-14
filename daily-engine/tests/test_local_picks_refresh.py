"""Offline regression coverage for the latest completed, durable display."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_config_dep, get_database_manager
from api.v1.endpoints import stock_king as endpoint
from src.services.stock_king_display import LocalPicksDisplayStore
from src.services.stock_king_service import StockKingService
from src.services.task_queue import AnalysisTaskQueue


def scan(code="600001", **overrides):
    candidates = [{"code": code, "name": "离线样本"}] if code else []
    return {
        "run_id": "fixture-" + (code or "empty"), "mode": "king_live", "scanSlot": "live",
        "generatedAt": "2026-09-29T10:30:00+08:00", "status": "completed_observations" if code else "no_candidates_with_coverage_limits",
        "candidates": candidates, "candidateCount": len(candidates),
        "profileCandidates": {"regular": candidates},
        "dataQuality": {"snapshot_count": 3000, "quote_coverage": {"requested": 30, "fresh": 30}},
        **overrides,
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("YAO_SCOUT_DATA_DIR", str(tmp_path / "persistent"))
    rows = []
    db = SimpleNamespace(
        list_yao_display_runs=lambda **_: [{"run_id": row["result"]["run_id"], **deepcopy(row)} for row in rows],
        get_yao_run=lambda run_id: next((deepcopy(row) for row in rows if row["result"]["run_id"] == run_id), None),
    )
    queue = object.__new__(AnalysisTaskQueue)
    AnalysisTaskQueue.__init__(queue, max_workers=2)
    monkeypatch.setattr(endpoint, "get_task_queue", lambda: queue)
    monkeypatch.setattr(endpoint, "YaoScoutService", lambda **_: SimpleNamespace(db=db))
    monkeypatch.setattr(StockKingService, "_classic_picks", lambda *a, **k: pytest.fail("classic strategies must not run"))
    endpoint._refresh_tasks.clear()
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[get_config_dep] = lambda: SimpleNamespace()
    app.dependency_overrides[get_database_manager] = lambda: db
    with TestClient(app) as client:
        yield client, db, rows, queue
    if queue._executor:
        queue._executor.shutdown(wait=True)


def install_scan(monkeypatch, result, *, gate=None, started=None):
    calls = []
    class Local:
        def __init__(self, yao, *, progress_callback):
            self.progress = progress_callback
        def run(self, slot, **kwargs):
            calls.append((slot, kwargs))
            self.progress(60, "正在核验分钟价格结构与历史覆盖")
            if started:
                started.set()
            if gate:
                assert gate.wait(5)
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)
    monkeypatch.setattr(endpoint, "LocalOpportunityService", Local)
    return calls


def completed_response(client, queue, accepted):
    task_id = accepted.json()["task_id"]
    queue._futures[task_id].result(timeout=5)
    response = client.get("/picks/refresh/tasks/" + task_id)
    assert response.status_code == 200
    return response.json()


def test_refresh_is_async_deduplicated_and_reopen_resumes(runtime, monkeypatch):
    client, db, _, queue = runtime
    store = LocalPicksDisplayStore(db)
    old = store.save(scan())
    gate, started = Event(), Event()
    calls = install_scan(monkeypatch, scan("600002"), gate=gate, started=started)
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            responses = list(callers.map(lambda _: endpoint.refresh_local_picks(
                endpoint.PicksRequest(scan_slot="1455", official=True), config=SimpleNamespace(), db_manager=db), range(2)))
        assert responses[0]["task_id"] == responses[1]["task_id"]
        first = client.post("/picks/refresh", json={})
        assert first.status_code == 202 and first.json()["task_id"] == responses[0]["task_id"]
        assert started.wait(2)
        shown = client.get("/picks/display").json()
        assert shown["adaptive"] == old["adaptive"]
        assert shown["refreshTask"]["task_id"] == first.json()["task_id"]
        assert shown["refreshTask"]["progress"] == 60
        assert shown["refreshTask"]["started_at"].endswith(("+08:00", "+00:00"))
    finally:
        gate.set()
    done = completed_response(client, queue, first)
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["result"]["adaptive"]["candidates"][0]["code"] == "600002"
    assert calls == [("live", {"top_n": 5, "official": False})]
    assert LocalPicksDisplayStore(db).read() == done["result"]


@pytest.mark.parametrize("result", [
    scan(status="unavailable", message="行情获取失败"),
    scan(None, status="skipped_non_trading_day"),
    scan(None, dataQuality={"snapshot_count": 3000, "quote_coverage": {"requested": 30, "fresh": 0}}),
    scan(None, dataQuality={"quote_coverage": {"requested": 30, "fresh": 30},
                           "minute_coverage": {"requested": 30, "usable": 0}}),
    RuntimeError("网络连接失败"),
])
def test_failed_or_skipped_refresh_preserves_previous_display(runtime, monkeypatch, result):
    client, db, _, queue = runtime
    store = LocalPicksDisplayStore(db)
    old = store.save(scan())
    original = store.path.read_bytes()
    install_scan(monkeypatch, result)
    done = completed_response(client, queue, client.post("/picks/refresh", json={}))
    assert done["status"] == "failed" and done["result"] is None and done["error"]
    assert store.path.read_bytes() == original
    assert client.get("/picks/display").json() == old


def test_valid_empty_manual_refresh_is_not_repopulated_by_equal_or_older_history(runtime, monkeypatch):
    client, db, rows, queue = runtime
    LocalPicksDisplayStore(db).save(scan())
    install_scan(monkeypatch, scan(None))
    done = completed_response(client, queue, client.post("/picks/refresh", json={}))
    assert done["status"] == "completed"
    rows.append({"mode": "king_1030", "result": scan("600009")})
    shown = LocalPicksDisplayStore(db).read()
    assert shown["adaptive"]["candidates"] == []
    assert shown["displaySnapshotVersion"] == 1 and shown["displaySource"] == "manual"


def test_display_uses_latest_valid_empty_history_then_newer_completed_scan_without_scanning(runtime, monkeypatch):
    client, db, rows, _ = runtime
    monkeypatch.setattr(endpoint, "LocalOpportunityService", lambda *a, **k: pytest.fail("GET must not scan"))
    rows.extend([
        {"mode": "king_live", "result": scan(None, status="unavailable", run_id="fixture-failed")},
        {"mode": "king_live", "result": scan(None)},
        {"mode": "king_live", "result": scan("600003", generatedAt="2026-09-18T14:55:00+08:00")},
    ])
    shown = client.get("/picks/display").json()
    assert shown["adaptive"]["candidates"] == []
    assert shown["displaySource"] == "migration"
    latest = scan("600008", generatedAt="2026-09-30T10:30:00+08:00")
    rows.insert(0, {"mode": "king_1030", "result": latest})
    shown = client.get("/picks/display").json()
    assert shown["adaptive"] == latest and shown["displaySource"] == "history"
    assert LocalPicksDisplayStore(None).read() == shown


def test_newer_scheduled_empty_replaces_manual_candidates_and_invalid_runs_are_ignored(runtime, monkeypatch):
    client, db, rows, _ = runtime
    monkeypatch.setattr(endpoint, "LocalOpportunityService", lambda *a, **k: pytest.fail("GET must not scan"))
    LocalPicksDisplayStore(db).save(scan("600001", generatedAt="2026-09-18T10:30:00+08:00"))
    latest = scan(None, generatedAt="2026-09-29T14:55:00+08:00")
    rows.extend([
        {"mode": "king_1455", "result": latest},
        {"mode": "king_1030", "result": scan("600002")},
        {"mode": "king_live", "result": scan("600003", generatedAt="2026-10-01T10:30:00+08:00", status="unavailable")},
        {"mode": "king_review", "result": scan("600004", generatedAt="2026-10-01T15:30:00+08:00")},
        {"mode": "king_weekly", "result": scan("600005", generatedAt="2026-10-02T15:30:00+08:00")},
        {"mode": "king_live", "result": scan("600006", scanSlot="review", generatedAt="2026-10-03T15:30:00+08:00")},
        {"mode": "king_live", "result": scan("600007", generatedAt="2026-10-04T10:30:00+08:00", dataQuality={"stale": True})},
        {"mode": "yao_scout", "result": scan("600008", generatedAt="2026-10-05T10:30:00+08:00")},
    ])
    shown = client.get("/picks/display").json()
    assert shown["adaptive"] == latest
    assert shown["savedRecommendationCount"] == 0 and shown["displaySource"] == "history"


@pytest.mark.parametrize("older_stamp", ["2026-09-29T02:20:00Z", "2026-09-29T10:20:00", "invalid"])
def test_timestamp_comparison_keeps_newer_manual_snapshot_without_rewriting(runtime, older_stamp):
    _, db, rows, _ = runtime
    store = LocalPicksDisplayStore(db)
    expected = store.save(scan("600001"))
    before = store.path.read_bytes()
    rows.append({"mode": "king_1030", "result": scan("600002", generatedAt=older_stamp)})
    assert store.read() == expected
    assert store.path.read_bytes() == before


def test_completion_timestamp_wins_over_history_order_and_offsets(runtime):
    _, db, rows, _ = runtime
    store = LocalPicksDisplayStore(db)
    store.save(scan("600001"))
    latest = scan("600003", generatedAt="2026-09-29T03:00:00Z")
    rows.extend([
        {"mode": "king_1030", "result": scan("600002", generatedAt="2026-09-29T10:45:00+08:00")},
        {"mode": "king_live", "result": latest},
    ])
    assert store.read()["adaptive"] == latest


def test_history_database_error_preserves_durable_display(runtime):
    _, db, _, _ = runtime
    store = LocalPicksDisplayStore(db)
    expected = store.save(scan())
    before = store.path.read_bytes()
    def unavailable(**_):
        raise RuntimeError("database unavailable")
    db.list_yao_display_runs = unavailable
    shown = store.read()
    assert shown.pop("displaySyncError")
    assert shown == expected
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("code_key", ["code", "stock_code"])
def test_display_projection_preserves_recommendations_and_full_durable_evidence(runtime, code_key):
    _, db, _, _ = runtime
    watch = {code_key: "600002", "name": "等待样本", "indicatorEvidence": {"large": "full evidence"},
             "precisionDecision": {"profiles": {"regular": {"entryEligible": False, "reasons": ["等待确认"], "score": 8}}}}
    insufficient = {"code": "600003", "indicatorEvidence": {"large": "modal evidence"}}
    result = scan(precisionWatchlist=[watch], controls=[watch], windvanes=[watch], evidenceInsufficient=[insufficient])
    store = LocalPicksDisplayStore(db)
    shown = store.save(result)
    assert shown["adaptive"]["candidates"] == result["candidates"]
    assert shown["adaptive"]["evidenceInsufficient"] == [insufficient]
    assert shown["adaptive"]["precisionWatchlist"] == [{
        code_key: "600002", "name": "等待样本",
        "precisionDecision": {"profiles": {"regular": {"entryEligible": False, "reasons": ["等待确认"]}}},
    }]
    assert "controls" not in shown["adaptive"] and "windvanes" not in shown["adaptive"]
    assert json.loads(store.path.read_text(encoding="utf-8"))["adaptive"] == result
    assert store.read() == shown


def test_manual_completion_during_history_read_cannot_be_replaced_by_older_result(runtime):
    _, db, _, _ = runtime
    store = LocalPicksDisplayStore(db)
    store.save(scan("600001"))
    started, release = Event(), Event()
    def delayed_history(**_):
        started.set()
        assert release.wait(5)
        return [{"run_id": "fixture-600002", "mode": "king_1455", "result": scan("600002", generatedAt="2026-09-29T14:55:00+08:00")}]
    db.list_yao_display_runs = delayed_history
    with ThreadPoolExecutor(max_workers=1) as readers:
        response = readers.submit(store.read)
        try:
            assert started.wait(2)
            expected = store.save(scan("600003", generatedAt="2026-09-29T15:00:00+08:00"))
        finally:
            release.set()
        assert response.result(timeout=5) == expected
    assert LocalPicksDisplayStore(None).read() == expected


def test_late_save_cannot_regress_to_an_older_completed_scan(runtime):
    _, db, _, _ = runtime
    store = LocalPicksDisplayStore(db)
    expected = store.save(scan("600003", generatedAt="2026-09-29T15:00:00+08:00"), source="history")
    assert store.save(scan("600001")) == expected
    assert store.read() == expected


def test_history_filters_before_limit_so_other_runs_do_not_hide_latest_picks(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage import DatabaseManager, YaoRun

    monkeypatch.setenv("YAO_SCOUT_DATA_DIR", str(tmp_path / "display"))
    engine = create_engine(f"sqlite:///{(tmp_path / 'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    latest = scan("600009")
    modes_and_statuses = [
        ("yao_scout", "completed"), ("king_review", "completed"),
        ("king_weekly", "completed"), ("king_live", "unavailable"),
    ]
    rows = [{"run_id": f"ignored-{i}", "mode": mode, "status": status,
             "as_of_at": datetime(2026, 9, 30), "cutoff_at": datetime(2026, 9, 30),
             "model_version": "fixture", "result_json": json.dumps(scan(str(i)))}
            for i in range(120) for mode, status in [modes_and_statuses[i % 4]]]
    rows.append({"run_id": "latest-picks", "mode": "king_1030", "status": latest["status"],
                 "as_of_at": datetime(2026, 9, 29), "cutoff_at": datetime(2026, 9, 29),
                 "model_version": "fixture", "result_json": json.dumps(latest)})
    try:
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), rows)
        assert len(db.list_yao_runs(limit=100)) == 100
        reads = []
        get_run = db.get_yao_run
        def fetch(run_id):
            reads.append(run_id)
            return get_run(run_id)
        db.get_yao_run = fetch
        assert LocalPicksDisplayStore(db).read()["adaptive"] == latest
        assert reads == ["latest-picks"]
        assert LocalPicksDisplayStore(db).read()["adaptive"] == latest
        assert reads == ["latest-picks"], "unchanged scans must not refetch their full payload"
    finally:
        engine.dispose()


def test_no_history_is_empty_and_successful_empty_history_can_migrate(runtime):
    client, _, rows, _ = runtime
    assert client.get("/picks/display").json() == {}
    rows.append({"mode": "king_live", "result": scan(None)})
    shown = client.get("/picks/display").json()
    assert shown["adaptive"]["candidates"] == [] and shown["displaySource"] == "migration"


def test_premarket_missing_quotes_explains_why_and_atomic_write_failure_preserves_old(runtime, monkeypatch):
    _, db, _, _ = runtime
    store = LocalPicksDisplayStore(db)
    old = store.save(scan())
    with pytest.raises(ValueError, match="09:30.*分钟"):
        store.save(scan(None, generatedAt="2026-09-30T09:19:45+08:00",
                        dataQuality={"quote_coverage": {"requested": 30, "fresh": 0}}))
    import src.services.stock_king_display as module
    monkeypatch.setattr(module.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(OSError, match="disk failed"):
        store.save(scan("600008"))
    assert store.read() == old
    assert list(store.path.parent.glob("*.tmp")) == []


def test_task_endpoint_rejects_unrelated_tasks_and_missing_ids(runtime):
    client, _, _, queue = runtime
    task = queue.submit_background_task(lambda: {}, stock_code="other", report_type="other")
    assert client.get("/picks/refresh/tasks/" + task.task_id).status_code == 404
    assert client.get("/picks/refresh/tasks/missing").status_code == 404
