"""Offline regression coverage for the manually refreshed, durable display."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
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
    db = SimpleNamespace(list_yao_runs=lambda **_: deepcopy(rows))
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


def test_valid_empty_manual_refresh_is_durable_and_stops_history_repopulation(runtime, monkeypatch):
    client, db, rows, queue = runtime
    LocalPicksDisplayStore(db).save(scan())
    install_scan(monkeypatch, scan(None))
    done = completed_response(client, queue, client.post("/picks/refresh", json={}))
    assert done["status"] == "completed"
    rows.append({"mode": "king_1030", "result": scan("600009")})
    shown = LocalPicksDisplayStore(db).read()
    assert shown["adaptive"]["candidates"] == []
    assert shown["displaySnapshotVersion"] == 1 and shown["displaySource"] == "manual"


def test_display_migrates_last_good_candidates_without_scanning_and_then_freezes(runtime, monkeypatch):
    client, db, rows, _ = runtime
    monkeypatch.setattr(endpoint, "LocalOpportunityService", lambda *a, **k: pytest.fail("GET must not scan"))
    rows.extend([
        {"mode": "king_live", "result": scan(None, status="unavailable")},
        {"mode": "king_live", "result": scan(None)},
        {"mode": "king_live", "result": scan("600003", generatedAt="2026-09-18T14:55:00+08:00")},
    ])
    shown = client.get("/picks/display").json()
    assert shown["adaptive"]["candidates"][0]["code"] == "600003"
    assert shown["displaySource"] == "migration"
    rows.insert(0, {"mode": "king_1030", "result": scan("600008")})
    assert LocalPicksDisplayStore(db).read() == shown


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
