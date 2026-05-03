"""
SSE 实时进度推送 API

GET /stream/{task_id}

兼容微信小程序：
  - 不使用 Last-Event-ID 自动重连（小程序 SSE 库不稳定支持）
  - 定期发送 heartbeat 防止代理/Nginx 30s 超时断流
  - 若任务已完成直接返回最终状态
"""
import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.config import settings
from app.database import SessionLocal, get_task_status
from app.models.task import Task

router = APIRouter(prefix="/stream", tags=["Stream"])
logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 15   # 秒
MAX_WAIT_SECONDS   = 600  # 10 分钟超时


@router.get("/{task_id}", summary="SSE 实时进度推送")
async def stream_task_progress(
    task_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # 验证任务归属
    with SessionLocal() as db:
        task = db.query(Task).filter_by(
            task_id=task_id,
            user_id=user["user_id"]
        ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return StreamingResponse(
        _event_generator(task_id, user["user_id"], request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",   # 关闭 Nginx 响应缓冲
        },
    )


async def _event_generator(
    task_id: str,
    user_id: str,
    request: Request,
) -> AsyncGenerator[str, None]:
    waited = 0
    last_stage = ""
    last_status = ""

    while waited < MAX_WAIT_SECONDS:
        # 检测客户端是否断开
        if await request.is_disconnected():
            logger.info(f"[SSE] 客户端断开连接: task_id={task_id}")
            return

        with SessionLocal() as db:
            task = db.query(Task).filter_by(
                task_id=task_id,
                user_id=user_id
            ).first()

        if not task:
            yield _sse_event("error", {"message": "任务不存在"})
            return

        # 状态变化时推送更新
        changed = (task.stage != last_stage or task.status != last_status)
        if changed:
            last_stage  = task.stage  or ""
            last_status = task.status or ""

            event_data = {
                "task_id":  task_id,
                "status":   task.status,
                "stage":    task.stage,
                "progress": task.progress,
            }
            yield _sse_event("progress", event_data)

        # 终态
        if task.status in ("done", "error", "rejected"):
            if task.status == "done":
                result = _load_result(task_id)
                yield _sse_event("done", {"task_id": task_id, "result": result})
            elif task.status == "rejected":
                yield _sse_event("rejected", {
                    "task_id":       task_id,
                    "reject_reason": task.reject_reason,
                    "suggestions":   task.suggestions,
                })
            else:
                yield _sse_event("error", {
                    "task_id":       task_id,
                    "error_message": task.error_message,
                })
            return

        # heartbeat（防代理超时）
        yield _sse_heartbeat()
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        waited += HEARTBEAT_INTERVAL

    # 超时
    yield _sse_event("timeout", {"task_id": task_id, "message": "等待超时，请稍后查询结果"})


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_heartbeat() -> str:
    return ": heartbeat\n\n"


def _load_result(task_id: str) -> dict:
    """从数据库读取分析结果摘要（SSE done 事件负载）"""
    from app.models.analysis_result import AnalysisResult
    with SessionLocal() as db:
        result = db.query(AnalysisResult).filter_by(task_id=task_id).first()
    if not result:
        return {}
    return {
        "findings":          result.findings,
        "impression":        result.impression,
        "confidence":        result.confidence,
        "disclaimer":        result.disclaimer,
        "recommendations":   result.recommendations,
    }
