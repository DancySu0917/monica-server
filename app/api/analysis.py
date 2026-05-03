"""
分析任务 API

POST /analysis/create → 入队 ARQ 任务，返回 task_id
GET  /analysis/status/{task_id} → 任务状态轮询
"""
import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.api.deps import get_current_user
from app.config import settings
from app.database import SessionLocal
from app.models.task import Task
from app.services.file_service import DiskGuard

router     = APIRouter(prefix="/analysis", tags=["Analysis"])
logger     = logging.getLogger(__name__)
disk_guard = DiskGuard()


class CreateAnalysisRequest(BaseModel):
    file_id:         str          # complete_upload 返回的 SHA256
    scan_type:       str = "CT"   # CT / MRI / PET
    clinical_notes:  str = ""     # 医生补充说明（可选）
    model:           str = ""     # 指定模型（空则用默认降级链）
    idempotency_key: str = ""     # 幂等键（空则自动生成）

    @field_validator("file_id")
    @classmethod
    def validate_file_id(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[0-9a-fA-F]{8,64}", v):
            raise ValueError("file_id 格式非法")
        return v.lower()

    @field_validator("scan_type")
    @classmethod
    def validate_scan_type(cls, v: str) -> str:
        allowed = {"CT", "MRI", "PET", "X-RAY", "US"}
        if v.upper() not in allowed:
            raise ValueError(f"scan_type 必须为 {allowed} 之一")
        return v.upper()


class CreateAnalysisResponse(BaseModel):
    task_id:         str
    status:          str
    already_exists:  bool


@router.post("/create", response_model=CreateAnalysisResponse, summary="创建分析任务")
async def create_analysis(
    body: CreateAnalysisRequest,
    user: dict = Depends(get_current_user),
):
    disk_guard.assert_enough_space(required_gb=0.2)

    user_id = user["user_id"]

    # 幂等检查：相同 file_id + user_id 已有进行中/完成任务则直接返回
    idempotency_key = (
        body.idempotency_key
        or hashlib.sha256(f"{user_id}:{body.file_id}".encode()).hexdigest()[:32]
    )

    with SessionLocal() as db:
        existing = db.query(Task).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            # 只复用"进行中"的任务，避免重复入队
            # done/error/failed/rejected 都允许重新提交（用户可以重跑）
            if existing.status in ("pending", "processing"):
                return CreateAnalysisResponse(
                    task_id=existing.task_id,
                    status=existing.status,
                    already_exists=True,
                )
            else:
                # 释放幂等键，让新任务可以正常插入
                existing.idempotency_key = f"__old__{existing.task_id}"
                db.commit()
                logger.info(f"[Analysis] 任务 {existing.task_id} 状态={existing.status}，允许重新提交")

    # 检查文件记录是否存在
    from app.models.file_record import FileRecord
    with SessionLocal() as db:
        file_rec = db.query(FileRecord).filter_by(file_hash=body.file_id).first()
        if not file_rec:
            raise HTTPException(
                status_code=404,
                detail=f"文件 {body.file_id} 不存在，请先完成上传",
            )
        file_path = file_rec.storage_path

    # 创建任务记录
    task_id = str(uuid.uuid4())
    model   = body.model or settings.DEFAULT_MODEL

    task = Task(
        task_id=task_id,
        idempotency_key=idempotency_key,
        user_id=user_id,
        status="pending",
        model=model,
    )
    with SessionLocal() as db:
        db.add(task)
        db.commit()

    # 入队 ARQ 任务
    try:
        import redis.asyncio as aioredis
        from arq.connections import create_pool, RedisSettings
        from urllib.parse import urlparse
        parsed = urlparse(settings.REDIS_URL)
        arq_redis = await create_pool(
            RedisSettings(
                host=parsed.hostname or "localhost",
                port=parsed.port or 6379,
                database=int(parsed.path.strip("/") or 0),
            )
        )
        await arq_redis.enqueue_job(
            "run_analysis_pipeline",
            task_id=task_id,
            user_id=user_id,
            file_path=file_path,
            scan_type=body.scan_type,
            clinical_notes=body.clinical_notes,
            model=model,
        )
        await arq_redis.close()
    except Exception as e:
        logger.error(f"[Analysis] 任务入队失败: {e}")
        with SessionLocal() as db:
            t = db.query(Task).filter_by(task_id=task_id).first()
            if t:
                t.status = "error"
                t.error_message = f"任务入队失败: {str(e)}"
                db.commit()
        raise HTTPException(status_code=503, detail="任务队列暂不可用，请稍后重试")

    return CreateAnalysisResponse(
        task_id=task_id,
        status="pending",
        already_exists=False,
    )


@router.get("/status/{task_id}", summary="查询任务状态")
async def get_task_status(
    task_id: str,
    user: dict = Depends(get_current_user),
):
    with SessionLocal() as db:
        task = db.query(Task).filter_by(
            task_id=task_id,
            user_id=user["user_id"],
        ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id":       task.task_id,
        "status":        task.status,
        "stage":         task.stage,
        "progress":      task.progress,
        "error_message": task.error_message,
        "reject_reason": task.reject_reason,
    }
