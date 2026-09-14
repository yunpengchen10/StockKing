# -*- coding: utf-8 -*-
"""Yao-scout run, research backfill and metrics endpoints."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import get_config_dep, get_database_manager
from api.v1.errors import api_error
from src.config import Config
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.services.task_queue import get_task_queue
from src.services.yao_scout import YaoScoutService
from src.storage import DatabaseManager

router = APIRouter()


class YaoRunRequest(BaseModel):
    mode: Literal["preopen", "postclose", "intraday"] = "preopen"
    top_n: int = Field(5, ge=1, le=20)
    notify: bool = False
    allow_late: bool = False


class YaoBackfillRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=5000)
    years: int = Field(5, ge=1, le=10)
    max_symbols: int = Field(200, ge=1, le=5000)


class AcceptedTask(BaseModel):
    task_id: str
    trace_id: str
    status: str = "pending"
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    trace_id: str | None = None
    status: str
    progress: int = 0
    message: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


def _service(config: Config, db_manager: DatabaseManager) -> YaoScoutService:
    return YaoScoutService(config=config, db_manager=db_manager)


@router.post("/runs", status_code=202, response_model=AcceptedTask)
def start_yao_run(
    request: YaoRunRequest,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AcceptedTask:
    task_id = uuid.uuid4().hex
    queue = get_task_queue()

    def execute() -> dict[str, Any]:
        queue.update_task_progress(task_id, 15, "正在校验时点数据与交易日")
        result = _service(config, db_manager).run(
            request.mode,
            top_n=request.top_n,
            notify=request.notify,
            allow_late=request.allow_late,
        )
        queue.update_task_progress(task_id, 98, "候选、证据和报告已持久化")
        return result

    task = queue.submit_background_task(
        execute,
        stock_code="yao_scout",
        stock_name=f"妖股雷达 / {request.mode}",
        report_type="yao_scout_run",
        message="妖股雷达任务已提交",
        task_id=task_id,
        trace_id=task_id,
    )
    return AcceptedTask(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "妖股雷达任务已提交",
    )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_yao_task(task_id: str) -> TaskStatusResponse:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type not in {"yao_scout_run", "yao_scout_backfill"}:
        raise api_error(404, "yao_scout_task_not_found", f"妖股雷达任务 {task_id} 不存在或已过期")
    result = task.result if task.status == QueueTaskStatus.COMPLETED and isinstance(task.result, dict) else None
    return TaskStatusResponse(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        progress=task.progress,
        message=task.message,
        error=task.error,
        result=result,
    )


@router.get("/runs/latest")
def latest_yao_run(
    mode: str | None = Query(None, pattern="^(preopen|postclose|intraday)$"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> dict[str, Any]:
    result = db_manager.get_latest_yao_run(mode=mode)
    if result is None:
        raise api_error(404, "yao_scout_run_not_found", "尚无妖股雷达运行报告")
    return result


@router.get("/runs")
def list_yao_runs(
    limit: int = Query(20, ge=1, le=100),
    mode: str | None = Query(None, pattern="^(preopen|postclose|intraday)$"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> dict[str, Any]:
    items = db_manager.list_yao_runs(limit=limit, mode=mode)
    return {"items": items, "count": len(items)}


@router.get("/runs/{run_id}")
def get_yao_run(
    run_id: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> dict[str, Any]:
    result = db_manager.get_yao_run(run_id)
    if result is None:
        raise api_error(404, "yao_scout_run_not_found", f"妖股雷达运行 {run_id} 不存在")
    return result


@router.get("/metrics")
def get_yao_metrics(db_manager: DatabaseManager = Depends(get_database_manager)) -> dict[str, Any]:
    metrics = db_manager.get_yao_metrics()
    metrics.setdefault("precision_at_5", {
        key: value.get("hit_rate") for key, value in (metrics.get("labels") or {}).items()
    })
    metrics.setdefault("calibration", {"status": "unavailable_until_probability_model_qualified"})
    metrics.setdefault("false_positive_rate", None)
    return metrics


@router.post("/backfill/tasks", status_code=202, response_model=AcceptedTask)
def start_yao_backfill(
    request: YaoBackfillRequest,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AcceptedTask:
    task_id = uuid.uuid4().hex
    queue = get_task_queue()

    def execute() -> dict[str, Any]:
        queue.update_task_progress(task_id, 10, "正在构建时点样本与匹配反例基础集")
        result = _service(config, db_manager).backfill(
            symbols=request.symbols,
            years=request.years,
            max_symbols=request.max_symbols,
        )
        queue.update_task_progress(task_id, 98, "历史样本、模型门禁记录和研究报告已生成")
        return result

    task = queue.submit_background_task(
        execute,
        stock_code="yao_backfill",
        stock_name="妖股雷达 / 历史回填",
        report_type="yao_scout_backfill",
        message="历史回填任务已提交",
        task_id=task_id,
        trace_id=task_id,
    )
    return AcceptedTask(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "历史回填任务已提交",
    )
