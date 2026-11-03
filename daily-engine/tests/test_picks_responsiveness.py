"""Offline regression tests for large-history local-picks responsiveness.

These deliberately forbid network access and exercise the public ASGI routes,
not a running desktop or the user's databases.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_database_manager
from api.v1.endpoints import stock_king as endpoint
from src.services.stock_king_display import LocalPicksDisplayStore
from src.services.task_queue import TaskInfo, TaskStatus
from src.storage import DatabaseManager, YaoRun


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    import requests

    def forbidden(*args, **kwargs):
        pytest.fail("Responsiveness tests must not fetch quotes or launch a scan")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(endpoint, "LocalOpportunityService", forbidden)
    monkeypatch.setenv("YAO_SCOUT_DATA_DIR", str(tmp_path / "display"))
    endpoint._refresh_tasks.clear()
    yield
    endpoint._refresh_tasks.clear()


def _scan(**overrides):
    return {
        "run_id": "latest-saved-run", "mode": "king_live", "scanSlot": "live",
        "generatedAt": "2026-11-03T13:06:38+08:00",
        "status": "completed_observations",
        "candidates": [{"code": "600001", "name": "offline fixture"}],
        "profileCandidates": {},
        "dataQuality": {"snapshot_count": 3000,
                        "quote_coverage": {"requested": 20, "fresh": 20}},
        **overrides,
    }


def test_history_change_detection_cannot_read_large_result_column(tmp_path):
    """SQLite denies the body and trailing columns that traverse its overflow."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    stamp = datetime(2026, 11, 3, 10, 0)
    # 32 MiB of synthetic history is enough to catch SELECT entity regressions;
    # the authorization assertion remains deterministic on slow or fast disks.
    large_result = json.dumps({"controls": "x" * (1024 * 1024)})
    rows = [{
        "run_id": f"large-{index}", "mode": "king_live", "status": "completed",
        "as_of_at": stamp + timedelta(minutes=index), "cutoff_at": stamp,
        "model_version": "offline-test", "result_json": large_result,
    } for index in range(32)]
    denied = []
    try:
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), rows)
        with engine.connect() as connection:
            raw = connection.connection.driver_connection

            def authorizer(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ and table == "yao_runs" and column in {"result_json", "created_at"}:
                    denied.append(column)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            raw.set_authorizer(authorizer)
            # Bind the manager to exactly the guarded connection.
            db._SessionLocal = sessionmaker(bind=connection)
            headers = db.list_yao_run_headers(
                modes=("king_live",), statuses=("completed",), limit=100)
            assert len(headers) == 32 and headers[0]["run_id"] == "large-31"
            assert all("result" not in row and "result_json" not in row for row in headers)
            assert denied == []
            raw.set_authorizer(None)
    finally:
        engine.dispose()


def test_warm_display_across_store_instances_reads_no_snapshot_or_run_body(monkeypatch):
    """A page poll checks 100 metadata rows and reuses the frozen projection."""
    saved = LocalPicksDisplayStore(None).save(_scan(controls=[{"raw": "x" * 4_000_000}]))
    path = LocalPicksDisplayStore(None).path
    headers = [{"id": 100, "run_id": "latest-saved-run", "mode": "king_live",
                "status": "completed_observations", "as_of": "2026-11-03T13:06:38"}]
    headers.extend({"id": index, "run_id": f"old-{index}", "mode": "king_live",
                    "status": "completed", "as_of": "2026-11-02T10:00:00"}
                   for index in range(99, 0, -1))
    calls = []

    def metadata(**kwargs):
        calls.append(kwargs)
        return headers

    def body_forbidden(*args, **kwargs):
        pytest.fail("Unchanged display must not parse historical run bodies")

    body_reads = []

    def old_body(run_id):
        body_reads.append(run_id)
        return {"result": _scan(run_id=run_id, generatedAt="2026-11-02T10:00:00+08:00")}

    db = SimpleNamespace(list_yao_run_headers=metadata,
                         list_yao_display_runs=body_forbidden, get_yao_run=old_body)
    # A lower insertion ID is not proof of an older generatedAt. Complete the
    # bounded history audit first; then only immutable headers may be inspected.
    for _ in range(101):
        before = len(body_reads)
        assert LocalPicksDisplayStore(db).read()["displayRunId"] == saved["displayRunId"]
        assert len(body_reads)-before <= 2
        if len(body_reads) == before:
            break
    assert set(body_reads) == {f"old-{index}" for index in range(1, 100)}
    db.get_yao_run = body_forbidden
    calls.clear()
    read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == path:
            pytest.fail("Unchanged snapshot must not be read or JSON-decoded again")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    for _ in range(4):
        shown = LocalPicksDisplayStore(db).read()
        assert shown["displayRunId"] == "latest-saved-run"
        shown["adaptive"]["candidates"][0]["name"] = "caller mutation"
    fresh = LocalPicksDisplayStore(db).read()
    assert fresh["adaptive"]["candidates"][0]["name"] == "offline fixture"
    assert len(calls) == 5


def test_display_response_size_is_independent_of_raw_research_history():
    raw = "x" * (1024 * 1024)
    full = _scan(
        controls=[{"raw": raw}] * 8,
        precisionWatchlist=[{"code": "600001", "fundEvidence": {"cumulativeSeries": raw},
            "precisionDecision": {"profiles": {"regular": {"entryEligible": False,
                "reasons": ["missing evidence"], "rawMinuteEvidence": raw}}}}],
        evidenceInsufficient=[{"code": "600002", "name": "insufficient fixture",
            "baselineEvidence": {"historyDays": 12, "rawMinuteEvidence": raw},
            "rawMinuteEvidence": raw, "fundEvidence": {"cumulativeSeries": raw}}],
    )
    store = LocalPicksDisplayStore(None)
    shown = store.save(full)
    # The frozen audit evidence remains durable; only its display is projected.
    assert store.path.stat().st_size > 10 * 1024 * 1024
    assert len(json.dumps(shown).encode()) < 30_000
    assert shown["adaptive"]["evidenceInsufficient"][0]["baselineEvidence"]["historyDays"] == 12
    assert shown["adaptive"]["precisionWatchlist"][0]["precisionDecision"]["profiles"]["regular"]["entryEligible"] is False


class _DoNotTraverse(dict):
    def items(self):
        pytest.fail("Polling traversed a completed task's old research payload")

    def __iter__(self):
        pytest.fail("Polling iterated a completed task's old research payload")


def _app(monkeypatch, task):
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[get_database_manager] = lambda: None
    monkeypatch.setattr(endpoint, "get_task_queue", lambda: SimpleNamespace(
        get_task=lambda task_id: task if task_id == task.task_id else None))

    @app.get("/offline-heartbeat")
    async def heartbeat():
        return {"alive": True}

    return app


def test_completed_status_never_traverses_large_legacy_result(monkeypatch):
    task = TaskInfo(task_id="completed", stock_code="king_local_refresh",
                    report_type="stock_king_local_refresh", status=TaskStatus.COMPLETED,
                    progress=100, result=_DoNotTraverse({
                        "adaptive": _DoNotTraverse({"run_id": "frozen-run", "controls": "x" * 20_000_000})}))
    app = _app(monkeypatch, task)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.test") as client:
            for _ in range(5):
                poll, heartbeat = await asyncio.gather(
                    client.get("/picks/refresh/tasks/completed"), client.get("/offline-heartbeat"))
                assert heartbeat.json() == {"alive": True}
                assert poll.status_code == 200 and len(poll.content) < 2048
                assert poll.json()["result"] is None
                assert poll.json()["resultPersisted"] is True
                assert poll.json()["displayRunId"] == "frozen-run"

    asyncio.run(exercise())


def test_poll_and_heartbeat_continue_while_display_is_serializing(monkeypatch):
    """Hold display rendering open; even a completed poll must stay responsive."""
    task = TaskInfo(task_id="done", stock_code="king_local_refresh",
                    report_type="stock_king_local_refresh", status=TaskStatus.COMPLETED,
                    result={"resultPersisted": True, "displayRunId": "frozen-run"})
    app = _app(monkeypatch, task)
    entered, release = threading.Event(), threading.Event()
    display = {"syntheticLargeDisplay": True, "rows": ["x" * 1024] * 4096}

    class Store:
        def __init__(self, db):
            self.storage_key = "offline-large-display"

        def read(self):
            return display

        def with_current_primary_readiness(self, payload):
            return payload

    monkeypatch.setattr(endpoint, "LocalPicksDisplayStore", Store)
    response_class = endpoint.JSONResponse
    loop_thread = threading.get_ident()

    class BlockingDisplayRender(response_class):
        def render(self, content):
            if isinstance(content, dict) and content.get("syntheticLargeDisplay"):
                assert threading.get_ident() != loop_thread, "Display JSON encoding blocked the event loop"
                entered.set()
                assert release.wait(5), "Heartbeat or completed polling was blocked by display rendering"
            return super().render(content)

    # Cover the fallback too: source installations need not have orjson.
    monkeypatch.setattr(endpoint, "_picks_json_response", BlockingDisplayRender)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.test") as client:
            display_request = asyncio.create_task(client.get("/picks/display"))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                heartbeat, poll = await asyncio.wait_for(asyncio.gather(
                    client.get("/offline-heartbeat"), client.get("/picks/refresh/tasks/done")), timeout=2)
                assert heartbeat.json() == {"alive": True}
                assert poll.json()["displayRunId"] == "frozen-run"
                assert poll.json()["result"] is None and len(poll.content) < 2048
            finally:
                release.set()
                response = await display_request
            assert response.status_code == 200 and len(response.content) > 4_000_000

    asyncio.run(exercise())
