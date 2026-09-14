from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from api.v1.endpoints import quant


def test_quant_training_pause_and_resume_preserve_the_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    quant._write_task("original", {
        "status": "processing",
        "progress": 42,
        "message": "training",
        "request": {
            "symbols": ["600001.SH"],
            "horizons": [1, 5, 20],
            "objectives": ["conservative", "regular", "aggressive"],
            "train_master": True,
        },
    })

    paused = quant.pause_training_task("original")
    assert paused["status"] == "pause_requested"
    assert paused["progress"] == 42

    submitted = {}

    def fake_submit(task_id, request_payload, *, resumed=False):
        submitted.update({"task_id": task_id, "request": request_payload, "resumed": resumed})
        return SimpleNamespace(
            task_id=task_id,
            trace_id=task_id,
            status=SimpleNamespace(value="pending"),
        )

    monkeypatch.setattr(quant, "_submit_training", fake_submit)
    monkeypatch.setattr(quant, "_active_quant_task", lambda: None)
    result = quant.resume_training_task("original")
    assert result["status"] == "pending"
    assert result["resumed_from"] == "original"
    assert submitted["resumed"] is True
    assert submitted["request"]["symbols"] == ["600001.SH"]
    persisted = quant._read_task(result["task_id"])
    assert persisted["resumed_from"] == "original"


def test_start_training_reuses_the_active_quant_task(monkeypatch):
    active = SimpleNamespace(
        task_id="already-running",
        trace_id="already-running",
        status=quant.QueueTaskStatus.PROCESSING,
    )
    monkeypatch.setattr(quant, "_active_quant_task", lambda: active)
    monkeypatch.setattr(
        quant,
        "_submit_training",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate task submitted")),
    )

    result = quant.start_training(quant.TrainingRequest(symbols=["600001.SH"]))

    assert result == {
        "task_id": "already-running",
        "trace_id": "already-running",
        "status": "processing",
        "deduplicated": True,
    }


def test_list_training_tasks_recovers_orphaned_work_as_paused(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        quant,
        "get_task_queue",
        lambda: SimpleNamespace(
            get_task=lambda _task_id: None,
            list_pending_tasks=lambda: [],
        ),
    )
    quant._write_task("orphaned", {
        "status": "processing",
        "progress": 37,
        "message": "training",
        "request": {"symbols": ["600001.SH"]},
    })

    result = quant.list_training_tasks(20)

    assert result["count"] == 1
    assert result["tasks"][0]["status"] == "paused"
    assert result["tasks"][0]["progress"] == 37
    assert result["tasks"][0]["recovered_after_restart"] is True
    assert quant._read_task("orphaned")["status"] == "paused"


def test_list_training_tasks_puts_in_memory_work_before_disk_history(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    for index in range(3):
        quant._write_task(f"history-{index}", {
            "status": "completed",
            "progress": 100,
            "message": "done",
            "request": {"symbols": ["600001.SH"]},
        })
    quant._write_task("active", {
        "status": "pending",
        "progress": 0,
        "message": "queued",
        "request": {"symbols": ["600001.SH"]},
    })
    active = SimpleNamespace(
        task_id="active",
        report_type="stock_king_quant",
        status=quant.QueueTaskStatus.PENDING,
        to_dict=lambda: {"task_id": "active", "status": "pending"},
    )
    monkeypatch.setattr(
        quant,
        "get_task_queue",
        lambda: SimpleNamespace(
            get_task=lambda task_id: active if task_id == "active" else None,
            list_pending_tasks=lambda: [active],
        ),
    )

    result = quant.list_training_tasks(2)

    assert [task["task_id"] for task in result["tasks"]][:1] == ["active"]


def test_list_training_tasks_compacts_full_market_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        quant,
        "get_task_queue",
        lambda: SimpleNamespace(
            get_task=lambda _task_id: None,
            list_pending_tasks=lambda: [],
        ),
    )
    quant._write_task("large-request", {
        "status": "paused",
        "progress": 42,
        "message": "paused",
        "request": {
            "symbols": ["000001.SZ", "000002.SZ"],
            "universe_metadata": {
                "000001.SZ": {"industry": "bank"},
                "000002.SZ": {"industry": "property"},
            },
            "horizons": [1, 5, 20],
            "objectives": ["conservative", "regular", "aggressive"],
            "train_master": True,
        },
    })

    response = quant.list_training_tasks(20)

    listed = response["tasks"][0]
    assert listed["task_id"] == "large-request"
    assert listed["request"]["train_master"] is True
    assert listed["request"]["symbol_count"] == 2
    assert "symbols" not in listed["request"]
    assert "universe_metadata" not in listed["request"]


def test_concurrent_task_writes_remain_atomic(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))

    def persist(index: int) -> None:
        quant._write_task("concurrent", {
            "status": "processing",
            "progress": index,
            "message": f"update-{index}",
        })

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(persist, range(40)))

    persisted = quant._read_task("concurrent")
    assert persisted is not None
    assert persisted["status"] == "processing"
    assert persisted["message"].startswith("update-")
    assert list((tmp_path / "quant" / "tasks").glob("*.tmp")) == []
