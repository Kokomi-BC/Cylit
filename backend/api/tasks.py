"""任务中心 API（规划 §20.7）与 SSE 进度推送。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse

from backend.api.response import envelope
from backend.core.events import EVENT_TASK_PROGRESS, bus
from backend.services.paper import paper_service
from backend.services.task.task_service import task_service

router = APIRouter(tags=["tasks"])


@router.get("/tasks")
async def list_tasks(request: Request,
                     status: str | None = Query(None),
                     type_: str | None = Query(None, alias="type"),
                     queue: str | None = Query(None),
                     paper_id: str | None = Query(None),
                     limit: int = Query(100, le=500)):
    items = task_service.list(status=status, type_=type_, queue=queue,
                              paper_id=paper_id, limit=limit)
    return envelope({"items": items, "count": len(items),
                     "stats": task_service.stats(paper_id)}, request=request)


@router.get("/tasks/stats")
async def task_stats(request: Request, paper_id: str | None = Query(None)):
    return envelope(task_service.stats(paper_id), request=request)


@router.get("/tasks/stream")
async def task_stream(request: Request, since_seq: int = Query(0),
                      heartbeat: float = Query(15.0)):
    """任务进度流（进度/暂停/完成实时推送）。"""
    return StreamingResponse(
        bus.stream("tasks", since_seq=since_seq, heartbeat=heartbeat,
                   wanted={EVENT_TASK_PROGRESS}),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, request: Request):
    return envelope(task_service.get(task_id), request=request)


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, request: Request):
    return envelope(task_service.request_pause(task_id),
                    message="已请求暂停（在块边界生效）", request=request)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    return envelope(task_service.request_cancel(task_id),
                    message="已请求取消（已完成部分保留）", request=request)


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, request: Request):
    return envelope(task_service.resume(task_id),
                    message="已恢复，将从断点继续", request=request)


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request):
    """重试：等价于「从首个未完成项继续」。"""
    return envelope(task_service.resume(task_id), message="已重新排队", request=request)


@router.post("/tasks/reconcile")
async def run_reconcile(request: Request, payload: dict = Body(default={})):
    paper_id = payload.get("paper_id")
    if paper_id:
        paper_service.get_paper(paper_id)
    task = task_service.create("RECONCILE", "system", paper_id,
                               params={}, total_items=1, message="对账中")
    return envelope(task, message="已创建对账任务", request=request)
