"""Stock King quant forecast, training, and model-metric endpoints."""
from __future__ import annotations

import uuid
import json
import os
import threading
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.quant.service import SUPPORTED_HORIZONS, get_quant_service
from src.quant.executable_backtest import BacktestRequest, example_dataset, run_backtest
from src.services.task_queue import TaskStatus as QueueTaskStatus, get_task_queue

router = APIRouter()

_QUANT_SUBMIT_LOCK = threading.Lock()
_TASK_PERSIST_LOCK = threading.RLock()
_ACTIVE_TASK_STATUSES = {"pending", "processing", "pause_requested"}


class QuantTrainingPaused(RuntimeError):
    """Cooperative stop raised at persisted progress checkpoints."""


class TrainingRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list, max_length=7000)
    horizons: List[int] = Field(default_factory=lambda: list(SUPPORTED_HORIZONS))
    objectives: List[str] = Field(default_factory=lambda: ["conservative", "regular", "aggressive"])
    universe_metadata: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    train_master: bool = False
    legacy_forecasts: bool = False


def _task_root() -> Path:
    path = Path(os.getenv("SCREENING_DATA_DIR") or "data/screening") / "quant" / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_path(task_id: str) -> Path:
    return _task_root() / f"{task_id}.json"


def _write_task(task_id: str, payload: Dict[str, Any]) -> None:
    """Atomically persist a task without sharing a temporary path.

    The training worker and the desktop polling endpoints can update the same
    task close together. On Windows, reusing ``<task>.tmp`` lets one thread
    replace the file while another still has it open, which raises
    ``PermissionError: [WinError 5]`` and incorrectly fails a running model.
    Serialize in-process writes and give every replacement its own filename.
    """
    path = _task_path(task_id)
    payload = {**payload, "task_id": task_id, "updated_at": datetime.now(timezone.utc).isoformat()}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with _TASK_PERSIST_LOCK:
        try:
            temporary.write_text(serialized, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_task(task_id: str) -> Dict[str, Any] | None:
    try:
        return json.loads(_task_path(task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _active_quant_task():
    """Return the one in-memory quant task that owns the training slot."""
    for task in get_task_queue().list_pending_tasks():
        if task.report_type == "stock_king_quant":
            return task
    return None


def _task_response(task, *, deduplicated: bool = False) -> Dict[str, Any]:
    status = str(getattr(task.status, "value", task.status))
    return {
        "task_id": task.task_id,
        "trace_id": task.trace_id or task.task_id,
        "status": status,
        "deduplicated": deduplicated,
    }


def _recover_orphaned_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Turn stale pre-restart work into an explicitly resumable task."""
    if payload.get("status") not in _ACTIVE_TASK_STATUSES:
        return payload
    task_id = str(payload.get("task_id") or "")
    queued = get_task_queue().get_task(task_id) if task_id else None
    if (
        queued is not None
        and queued.report_type == "stock_king_quant"
        and queued.status in {QueueTaskStatus.PENDING, QueueTaskStatus.PROCESSING}
    ):
        return payload
    recovered = {
        **payload,
        "status": "paused",
        "message": "Daily 引擎曾中断；任务已安全转为暂停，可继续运行并复用现有缓存",
        "recovered_after_restart": True,
    }
    if task_id:
        _write_task(task_id, recovered)
        return _read_task(task_id) or recovered
    return recovered


def _task_list_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the compact task summary consumed by the desktop task bar.

    A full-market request contains thousands of symbols plus per-symbol
    metadata. Repeating that payload for every history row can exceed the
    desktop bridge's response limit and leave the JSON body truncated.
    """
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    symbols = request.get("symbols") if isinstance(request.get("symbols"), list) else []
    request_summary = {
        "train_master": bool(request.get("train_master")),
        "horizons": request.get("horizons") or [],
        "objectives": request.get("objectives") or [],
        "symbol_count": len(symbols),
    }
    keys = {
        "task_id", "trace_id", "status", "progress", "message", "error",
        "updated_at", "resumed", "resumed_from", "recovered_after_restart",
    }
    return {
        **{key: value for key, value in payload.items() if key in keys},
        "request": request_summary,
    }


def _submit_training(task_id: str, request_payload: Dict[str, Any], *, resumed: bool = False):
    queue = get_task_queue()

    def run() -> Dict[str, Any]:
        _write_task(task_id, {
            "status": "processing", "progress": 10,
            "message": "正在加载历史行情并执行滚动样本外训练",
            "request": request_payload, "resumed": resumed,
        })

        def update(progress: int, message: str) -> None:
            current = _read_task(task_id) or {}
            if current.get("status") in {"pause_requested", "paused"}:
                raise QuantTrainingPaused("quant_training_paused")
            queue.update_task_progress(task_id, progress, message)
            _write_task(task_id, {
                "status": "processing", "progress": progress, "message": message,
                "request": request_payload, "resumed": resumed,
            })

        try:
            result = get_quant_service().train_universe(
                request_payload["symbols"], request_payload.get("horizons") or SUPPORTED_HORIZONS,
                train_master=bool(request_payload.get("train_master")),
                metadata=request_payload.get("universe_metadata") or {},
                objectives=request_payload.get("objectives") or ["conservative", "regular", "aggressive"],
                legacy_forecasts=bool(request_payload.get("legacy_forecasts")),
                progress=update,
            )
            _write_task(task_id, {
                "status": "completed", "progress": 100, "message": "训练与模型门槛检查已完成",
                "request": request_payload, "result": result, "resumed": resumed,
            })
            return result
        except QuantTrainingPaused:
            _write_task(task_id, {
                "status": "paused", "progress": int((_read_task(task_id) or {}).get("progress") or 0),
                "message": "训练已暂停；已完成的面板与模型缓存均已保留，可继续运行",
                "request": request_payload, "resumed": resumed,
            })
            return {"paused": True, "task_id": task_id}
        except Exception as exc:
            _write_task(task_id, {
                "status": "failed", "progress": 0, "message": "量化训练失败",
                "request": request_payload, "error": str(exc), "resumed": resumed,
            })
            raise

    return queue.submit_background_task(
        run, stock_code="stock_king_quant", stock_name="Stock King Quant",
        report_type="stock_king_quant", message="量化训练任务已提交",
        task_id=task_id, trace_id=task_id,
    )


@router.get("/forecasts/{symbol}")
def get_forecast(symbol: str, horizon: int = Query(...)) -> Dict[str, Any]:
    return get_quant_service().forecast(symbol, horizon)


@router.get("/metrics")
def get_metrics() -> Dict[str, Any]:
    return get_quant_service().metrics()


@router.get("/backtests/template")
def get_executable_template() -> Dict[str, Any]:
    return {"template": example_dataset(), "schema": BacktestRequest.model_json_schema()}


@router.post("/backtests/executable")
def run_executable_backtest(request: BacktestRequest) -> Dict[str, Any]:
    try:
        result = run_backtest(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "backtest_data_invalid", "message": str(exc)}) from exc
    report_id = result["input_sha256"]
    report_root = _task_root().parent / "account-backtests"
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"{report_id}.json"
    result.update({"report_id": report_id, "report_path": str(report_path.resolve()),
                   "saved_at": datetime.now(timezone.utc).isoformat()})
    report = {"input": request.model_dump(mode="json", exclude={"calendar_csv", "signals_csv", "bars_csv", "benchmark_csv"}),
              "result": result}
    temporary = report_root / f".{report_id}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary, report_path)
        summary = {key: result.get(key) for key in ("report_id", "dataset_name", "saved_at", "performance_kind", "summary", "config")}
        temporary.write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary, report_root / f"{report_id}.summary.json")
    finally:
        temporary.unlink(missing_ok=True)
    return result


@router.get("/backtests/reports")
def list_executable_reports(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    root = _task_root().parent / "account-backtests"
    rows = []
    for path in sorted(root.glob("*.summary.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            continue
    return {"reports": rows}


@router.get("/backtests/reports/{report_id}")
def get_executable_report(report_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", report_id):
        raise HTTPException(status_code=422, detail={"error": "invalid_report_id"})
    path = _task_root().parent / "account-backtests" / f"{report_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail={"error": "backtest_report_not_found"})


@router.post("/training/tasks", status_code=202)
def start_training(request: TrainingRequest) -> Dict[str, Any]:
    if not request.symbols:
        raise HTTPException(status_code=422, detail={"error": "symbols_required", "message": "训练任务至少需要一只 A 股"})
    invalid = [value for value in request.horizons if value not in SUPPORTED_HORIZONS]
    if invalid:
        raise HTTPException(status_code=422, detail={"error": "invalid_horizon", "message": f"不支持的周期: {invalid}"})
    invalid_objectives = [
        value for value in request.objectives
        if value not in {"conservative", "regular", "aggressive"}
    ]
    if invalid_objectives:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_objective", "message": f"不支持的档位目标: {invalid_objectives}"},
        )
    request_payload = request.model_dump()
    with _QUANT_SUBMIT_LOCK:
        active = _active_quant_task()
        if active is not None:
            return _task_response(active, deduplicated=True)
        task_id = uuid.uuid4().hex
        _write_task(task_id, {
            "status": "pending", "progress": 0, "message": "量化训练任务已提交",
            "request": request_payload,
        })
        try:
            task = _submit_training(task_id, request_payload)
        except Exception as exc:
            _write_task(task_id, {
                "status": "failed", "progress": 0, "message": "量化训练任务提交失败",
                "request": request_payload, "error": str(exc),
            })
            raise
        return _task_response(task)


@router.get("/tasks/{task_id}")
def get_training_task(task_id: str) -> Dict[str, Any]:
    task = get_task_queue().get_task(task_id)
    persisted = _read_task(task_id)
    if persisted and persisted.get("status") in {"pause_requested", "paused"}:
        return persisted
    if task is None and persisted and persisted.get("status") in _ACTIVE_TASK_STATUSES:
        with _QUANT_SUBMIT_LOCK:
            return _recover_orphaned_task(persisted)
    if task is None:
        if persisted is None:
            raise HTTPException(status_code=404, detail={"error": "quant_task_not_found"})
        return persisted
    if task.report_type != "stock_king_quant":
        raise HTTPException(status_code=404, detail={"error": "quant_task_not_found"})
    return {
        "task_id": task.task_id, "trace_id": task.trace_id or task.task_id,
        "status": task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        "progress": task.progress, "message": task.message, "error": task.error,
        "result": task.result if task.status == QueueTaskStatus.COMPLETED else None,
    }


@router.post("/tasks/{task_id}/pause", status_code=202)
def pause_training_task(task_id: str) -> Dict[str, Any]:
    persisted = _read_task(task_id)
    if persisted is None:
        raise HTTPException(status_code=404, detail={"error": "quant_task_not_found"})
    if persisted.get("status") not in {"pending", "processing", "pause_requested"}:
        raise HTTPException(status_code=409, detail={"error": "quant_task_not_running"})
    _write_task(task_id, {
        **persisted,
        "status": "pause_requested",
        "message": "已请求暂停；将在当前安全检查点保留缓存并停止",
    })
    return _read_task(task_id) or {"task_id": task_id, "status": "pause_requested"}


@router.post("/tasks/{task_id}/resume", status_code=202)
def resume_training_task(task_id: str) -> Dict[str, Any]:
    persisted = _read_task(task_id)
    if persisted is None:
        raise HTTPException(status_code=404, detail={"error": "quant_task_not_found"})
    if persisted.get("status") not in {"paused", "pause_requested"}:
        raise HTTPException(status_code=409, detail={"error": "quant_task_not_paused"})
    request_payload = persisted.get("request") or {}
    if not request_payload.get("symbols"):
        raise HTTPException(status_code=409, detail={"error": "quant_task_request_missing"})
    with _QUANT_SUBMIT_LOCK:
        active = _active_quant_task()
        if active is not None:
            return {**_task_response(active, deduplicated=True), "resumed_from": task_id}
        next_task_id = uuid.uuid4().hex
        _write_task(next_task_id, {
            "status": "pending", "progress": 0, "message": "续跑任务已提交，将复用已完成的面板缓存",
            "request": request_payload, "resumed": True, "resumed_from": task_id,
        })
        try:
            task = _submit_training(next_task_id, request_payload, resumed=True)
        except Exception as exc:
            _write_task(next_task_id, {
                "status": "failed", "progress": 0, "message": "量化训练续跑提交失败",
                "request": request_payload, "resumed": True, "resumed_from": task_id,
                "error": str(exc),
            })
            raise
        return {
            **_task_response(task),
            "resumed_from": task_id,
        }


@router.get("/tasks")
def list_training_tasks(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    rows = []
    with _QUANT_SUBMIT_LOCK:
        included: set[str] = set()
        # The in-memory queue is the authority for work that is about to run or
        # is currently running. Merge it first so a large disk history cannot
        # push the active task outside the UI's result limit.
        for queued in get_task_queue().list_pending_tasks():
            if queued.report_type != "stock_king_quant":
                continue
            payload = _read_task(queued.task_id) or {
                **queued.to_dict(),
                "request": {},
            }
            rows.append(_task_list_item(_recover_orphaned_task(payload)))
            included.add(queued.task_id)
        for path in sorted(_task_root().glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.stem in included:
                continue
            payload = _read_task(path.stem)
            if payload:
                rows.append(_task_list_item(_recover_orphaned_task(payload)))
                included.add(path.stem)
            if len(rows) >= limit:
                break
    rows = rows[:limit]
    return {"tasks": rows, "count": len(rows)}
