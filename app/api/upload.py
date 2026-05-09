"""
分片上传 API（支持断点续传）

流程：
  1. POST /upload/init      → 获取 upload_id（或 already_exists 直接复用）
  2. PUT  /upload/chunk     → 上传各分片（可并发、可断点续传）
  3. POST /upload/complete  → 合并校验，返回 file_id
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

from app.api.deps import get_current_user
from app.services.file_service import FileService, DiskGuard
from app.config import settings
from app.utils.rate_limit import limiter

router     = APIRouter(prefix="/upload", tags=["Upload"])
logger     = logging.getLogger(__name__)
file_svc   = FileService()
disk_guard = DiskGuard()

MAX_CHUNK_BYTES = settings.CHUNK_SIZE_MB * 1024 * 1024


# ── Schema ────────────────────────────────────────────────────────

class InitUploadRequest(BaseModel):
    filename:     str
    total_size:   int
    total_chunks: int
    file_sha256:  str

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("filename 不能为空")
        # 防止路径穿越
        from pathlib import Path
        if ".." in v or "/" in v or "\\" in v:
            raise ValueError("filename 包含非法字符")
        return v.strip()

    @field_validator("total_size")
    @classmethod
    def validate_total_size(cls, v: int) -> int:
        max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        if v > max_bytes:
            raise ValueError(
                f"文件大小 {v / 1024**2:.1f}MB 超过限制 {settings.MAX_UPLOAD_SIZE_MB}MB"
            )
        return v

    @field_validator("file_sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("file_sha256 格式非法")
        return v.lower()


class UploadChunkResponse(BaseModel):
    upload_id:       str
    chunk_index:     int
    received_chunks: list
    total_chunks:    int


class CompleteUploadResponse(BaseModel):
    file_id:    str   # SHA256（服务器端实际计算值）
    # 注意：不返回 file_path，避免暴露服务器内部目录结构


# ── 路由 ──────────────────────────────────────────────────────────

@router.post("/init", summary="初始化分片上传")
@limiter.limit("30/minute")   # 每 IP 每分钟最多 30 次初始化上传
async def init_upload(
    request: Request,
    body: InitUploadRequest,
    user: dict = Depends(get_current_user),
):
    disk_guard.assert_enough_space(required_gb=body.total_size / 1024**3)
    result = file_svc.init_upload(
        user_id=user["user_id"],
        filename=body.filename,
        total_size=body.total_size,
        total_chunks=body.total_chunks,
        file_sha256=body.file_sha256,
    )
    return result


# upload_id 合法格式：up_ 开头 + 16 位十六进制
_UPLOAD_ID_RE = __import__("re").compile(r"^up_[0-9a-f]{16}$")


def _validate_upload_id(upload_id: str) -> str:
    """校验 upload_id 格式，非法时抛 422。"""
    if not upload_id or not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=422, detail="upload_id 格式非法")
    return upload_id


@router.put("/chunk", summary="上传单个分片")
async def upload_chunk(
    request: Request,
    upload_id:   str = None,
    chunk_index: int = None,
    user: dict = Depends(get_current_user),
):
    """
    请求头中指定 upload_id 和 chunk_index（query params）。
    Body 为原始 bytes。
    """
    # 从 query params 读取
    params = dict(request.query_params)
    upload_id   = params.get("upload_id", upload_id)
    chunk_index_str = params.get("chunk_index", str(chunk_index) if chunk_index is not None else None)

    if not upload_id or chunk_index_str is None:
        raise HTTPException(status_code=422, detail="缺少 upload_id 或 chunk_index 参数")
    upload_id = _validate_upload_id(upload_id)

    try:
        chunk_index = int(chunk_index_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="chunk_index 必须为整数")

    data = await request.body()
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"分片大小 {len(data)} 字节超过限制 {MAX_CHUNK_BYTES} 字节"
        )
    if len(data) == 0:
        raise HTTPException(status_code=422, detail="分片内容为空")

    try:
        received = file_svc.save_chunk(upload_id, chunk_index, data, user_id=user["user_id"])
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.models.upload_session import UploadSession
    from app.database import SessionLocal as Sess
    with Sess() as db:
        session = db.query(UploadSession).filter_by(upload_id=upload_id).first()
        total   = session.total_chunks if session else 0

    return UploadChunkResponse(
        upload_id=upload_id,
        chunk_index=chunk_index,
        received_chunks=received,
        total_chunks=total,
    )


@router.get("/chunks/{upload_id}", summary="查询已上传分片（断点续传）")
async def get_received_chunks(
    upload_id: str,
    user: dict = Depends(get_current_user),
):
    upload_id = _validate_upload_id(upload_id)
    try:
        received = file_svc.get_received_chunks(upload_id, user_id=user["user_id"])
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"upload_id": upload_id, "received_chunks": received}


@router.delete("/file/{file_id}", summary="删除文件记录（清除秒传缓存）")
async def delete_file_record(
    file_id: str,
    user: dict = Depends(get_current_user),
):
    """
    删除指定 file_id 的 FileRecord 及磁盘文件，使该文件下次上传时重新走完整流程。
    同时将关联的已完成任务幂等键置为过期，允许重新提交分析。
    """
    import re, os
    from pathlib import Path
    from app.models.file_record import FileRecord
    from app.models.task import Task
    from app.database import SessionLocal as Sess

    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", file_id):
        raise HTTPException(status_code=422, detail="file_id 格式非法")
    file_id = file_id.lower()

    with Sess() as db:
        rec = db.query(FileRecord).filter_by(file_hash=file_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail=f"file_id={file_id} 不存在")
        # 归属校验：只允许删除自己上传的文件（通过 storage_path 中的 user_id 目录判断）
        # DEV_MODE 下跳过校验，方便测试时清理不同账号上传的文件
        if not settings.DEV_MODE and rec.storage_path and f"/{user['user_id']}/" not in rec.storage_path:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权删除该文件",
            )

        storage_path = rec.storage_path

        # 1. 删除磁盘文件（若存在）
        try:
            if storage_path and Path(storage_path).exists():
                Path(storage_path).unlink()
                logger.info(f"[Upload] 已删除文件 {storage_path}")
        except Exception as e:
            logger.warning(f"[Upload] 删除文件失败 {storage_path}: {e}")

        # 2. 删除 FileRecord
        db.delete(rec)

        # 3. 将该 file_id 关联的所有已完成/失败任务幂等键设为过期
        #    （pending/processing 任务不干预，避免影响进行中的分析）
        tasks = db.query(Task).filter(
            Task.idempotency_key.like(f"%{file_id[:16]}%")
        ).all()
        invalidated = 0
        for t in tasks:
            if t.status not in ("pending", "processing"):
                t.idempotency_key = f"__cleared__{t.task_id}"
                invalidated += 1

        db.commit()

    logger.info(f"[Upload] file_id={file_id[:12]}… 已清除，关联任务幂等键重置 {invalidated} 条")
    return {
        "deleted":         True,
        "file_id":         file_id,
        "tasks_reset":     invalidated,
        "message":         "文件记录已删除，下次上传将重新执行完整流程",
    }


@router.post("/complete", summary="合并所有分片，校验 SHA256")
@limiter.limit("30/minute")   # 每 IP 每分钟最多 30 次合并
async def complete_upload(
    request: Request,
    upload_id: str,
    user: dict = Depends(get_current_user),
):
    try:
        final_path, file_sha256 = file_svc.complete_upload(upload_id, user_id=user["user_id"])
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # file_id 使用完整 SHA256（64位），与 FileRecord.file_hash 保持一致
    # 不返回 file_path，避免暴露服务器内部目录结构
    return CompleteUploadResponse(file_id=file_sha256)
