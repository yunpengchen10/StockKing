"""Private Stock King desktop integration endpoints."""
from __future__ import annotations

import uuid
from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.deps import get_config_dep, get_database_manager
from api.v1.errors import api_error
from src.config import Config
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.services.task_queue import get_task_queue
from src.services.stock_king_service import StockKingService
from src.services.yao_scout import AdaptiveKingService, YaoScoutService
from src.storage import DatabaseManager
from src.services.yao_scout.local_opportunities import LocalOpportunityService

router = APIRouter()


class PicksRequest(BaseModel):
    max_per_board: int = Field(5, ge=5, le=50)
    force: bool = False
    scan_slot: Literal["auto", "live", "0920", "0922", "0940", "0955", "1030", "1455"] = "live"
    top_n: int = Field(5, ge=0, le=5)
    official: bool | None = None
    allow_missed: bool = False


class AdaptiveRunRequest(BaseModel):
    scan_slot: Literal["0920", "0922", "0925", "0940", "0955", "1030", "1455", "review", "weekly"]
    top_n: int = Field(5, ge=0, le=5)
    allow_missed: bool = False


class AdviceRequest(BaseModel):
    symbol_code: str = Field(..., min_length=1, max_length=32)
    research_note: Dict[str, str] = Field(default_factory=dict)
    technical_snapshot: Dict[str, Any] = Field(default_factory=dict)


@router.post("/picks")
def get_picks(
    request: PicksRequest,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    return StockKingService(config, db_manager).picks(
        max_per_board=request.max_per_board,
        force=request.force,
        scan_slot=request.scan_slot,
        top_n=request.top_n,
        official=request.official,
        allow_missed=request.allow_missed,
    )


@router.post("/picks/runs", status_code=202)
def start_adaptive_run(
    request: AdaptiveRunRequest,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    task_id = uuid.uuid4().hex
    queue = get_task_queue()

    def execute() -> Dict[str, Any]:
        queue.update_task_progress(task_id, 10, "正在读取历史先验与校准状态")
        result = LocalOpportunityService(YaoScoutService(config=config, db_manager=db_manager)).run(
            request.scan_slot,
            top_n=request.top_n,
            official=True,
            allow_missed=request.allow_missed,
        )
        queue.update_task_progress(task_id, 98, "本地信号、对照样本及复盘状态已保存")
        return result

    task = queue.submit_background_task(
        execute,
        stock_code="king_adaptive",
        stock_name=f"King精选 / {request.scan_slot}",
        report_type="stock_king_adaptive",
        message="自适应King精选任务已提交",
        task_id=task_id,
        trace_id=task_id,
    )
    return {
        "task_id": task.task_id,
        "trace_id": task.trace_id or task.task_id,
        "status": task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        "message": task.message or "自适应King精选任务已提交",
    }


@router.get("/picks/tasks/{task_id}")
def get_adaptive_task(task_id: str) -> Dict[str, Any]:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type != "stock_king_adaptive":
        raise api_error(404, "stock_king_task_not_found", f"King精选任务 {task_id} 不存在或已过期")
    return {
        "task_id": task.task_id,
        "trace_id": task.trace_id or task.task_id,
        "status": task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        "progress": task.progress,
        "message": task.message,
        "error": task.error,
        "result": task.result if task.status == QueueTaskStatus.COMPLETED and isinstance(task.result, dict) else None,
    }


@router.get("/picks/latest")
def get_latest_adaptive_run(
    scan_slot: Literal["live", "0920", "0922", "0925", "0940", "0955", "1030", "1455", "review", "weekly"] = "live",
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    row = db_manager.get_latest_yao_run(mode=f"king_{scan_slot}")
    if not row:
        raise api_error(404, "stock_king_run_not_found", f"尚无 {scan_slot} King精选运行")
    return row


@router.get("/picks/history")
def get_adaptive_history(
    limit: int = Query(20, ge=1, le=100),
    scan_slot: str | None = Query(None, pattern="^(live|0920|0922|0925|0940|0955|1030|1455|review|weekly)$"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    mode = f"king_{scan_slot}" if scan_slot else None
    rows = db_manager.list_yao_runs(
        limit=100 if mode is None else limit,
        mode=mode,
        include_result=True,
    )
    if mode is None:
        rows = [row for row in rows if str(row.get("mode") or "").startswith("king_")][:limit]
    return {"items": rows, "count": len(rows)}


@router.get("/picks/learning")
def get_adaptive_learning(
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    return _signal_learning(config, db_manager).learning_state()


def _signal_learning(config, db_manager):
    from src.services.yao_scout.signal_learning import SignalLearningService
    return SignalLearningService(YaoScoutService(config=config, db_manager=db_manager).data_dir)


@router.get('/picks/records')
def get_signal_records(date: str = '', symbol: str = '', version: str = '',
    config: Config = Depends(get_config_dep), db_manager: DatabaseManager = Depends(get_database_manager)):
    return _signal_learning(config, db_manager).history({'date': date, 'symbol': symbol, 'version': version})


@router.get('/picks/reviews')
def get_signal_reviews(date: str = '', symbol: str = '', version: str = '',
    config: Config = Depends(get_config_dep), db_manager: DatabaseManager = Depends(get_database_manager)):
    return _signal_learning(config, db_manager).reviews({'date': date, 'symbol': symbol, 'version': version})


@router.post("/advice")
def get_advice(request: AdviceRequest, config: Config = Depends(get_config_dep)) -> Dict[str, Any]:
    return StockKingService(config).advice(
        request.symbol_code,
        research_note=request.research_note,
        technical_snapshot=request.technical_snapshot,
    )


class EconomyReviewRequest(BaseModel):
    request_id: str
    ai_config: Dict[str, Any] = Field(repr=False)
    codes: list[str] = Field(min_length=1,max_length=5)
    evidence: Dict[str, Any]
    prompt: str = Field(min_length=1,max_length=32768)


def economy_service(db_manager):
    from src.services.economy_review import EconomyReviewService
    from sqlalchemy.engine import make_url
    return EconomyReviewService(make_url(db_manager._db_url).database)


@router.post('/ai/reviews')
def economy_review(request: EconomyReviewRequest, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return economy_service(db_manager).run(request.request_id, request.ai_config, request.codes, request.evidence, request.prompt)
    except ValueError as exc:
        raise api_error(400, 'economy_review_failed', str(exc)) from None


@router.get('/ai/reviews/{request_id}')
def economy_review_status(request_id: str, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return economy_service(db_manager).status(request_id)
    except ValueError as exc:
        raise api_error(404,'review_not_found',str(exc)) from None


@router.post('/ai/reviews/{request_id}/cancel')
def cancel_economy_review(request_id: str, db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return economy_service(db_manager).cancel(request_id)
    except ValueError as exc:
        raise api_error(404,'review_not_found',str(exc)) from None


@router.get('/ai/budget')
def economy_budget(db_manager: DatabaseManager = Depends(get_database_manager)):
    return economy_service(db_manager).usage()


@router.post('/ai/budget')
def save_economy_budget(value: Dict[str, Any], db_manager: DatabaseManager = Depends(get_database_manager)):
    try:
        return economy_service(db_manager).settings(value)
    except ValueError as exc:
        raise api_error(400,'invalid_budget',str(exc)) from None
