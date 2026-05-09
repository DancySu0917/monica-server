"""
管理员工具接口（仅限内部调试，不要暴露给公网）

GET /admin/logs          → 读取服务日志

⚠️  安全策略：所有 /admin 路由必须携带有效 Bearer JWT 且 scope=="admin"。
    不满足条件返回 403，防止未授权访问日志中的敏感信息。
"""
import os
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from app.api.deps import get_current_user
from app.config import settings

router = APIRouter(prefix="/admin", tags=["Admin"])
logger = logging.getLogger(__name__)


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    """要求 JWT scope == 'admin'，否则返回 403。DEV_MODE 下跳过校验。"""
    if not settings.DEV_MODE and user.get("scope") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限（scope=admin）",
        )
    return user

# 日志目录：相对于项目根（本文件的上上级）
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / ".dev-logs"

# 允许查看的日志文件白名单（防止路径穿越）
_ALLOWED_LOGS = {
    "api":         "api.log",
    "worker":      "arq_worker.log",
    "worker_err":  "arq_worker.err",
    "fastapi":     "fastapi.log",
    "fastapi_err": "fastapi.err",
    "uvicorn":     "uvicorn.log",
}


def _tail_lines(path: Path, n: int) -> list[str]:
    """高效读取文件末尾 n 行（大文件不用全量读入）"""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        # 先尝试从末尾倒读
        try:
            f.seek(0, 2)
            file_size = f.tell()
            block = min(file_size, n * 200)  # 预估每行 200 字节
            f.seek(max(0, file_size - block))
            data = f.read()
        except OSError:
            data = path.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:] if len(lines) > n else lines


@router.get("/logs", summary="查看服务日志")
async def get_logs(
    source:  str = Query("worker", description="日志来源: api / worker / worker_err / fastapi / fastapi_err / uvicorn"),
    lines:   int = Query(200,      ge=1, le=5000, description="返回末尾行数"),
    keyword: str = Query("",       description="关键词过滤（区分大小写，留空返回全部）"),
    task_id: str = Query("",       description="按 task_id 过滤（含该字符串的行）"),
    _admin:  dict = Depends(_require_admin),
):
    """
    返回日志文件末尾指定行，支持关键词/task_id 过滤。
    响应为纯文本，前端可直接渲染到 <pre>。
    """
    if source not in _ALLOWED_LOGS:
        raise HTTPException(status_code=400, detail=f"未知日志来源: {source}，可选: {list(_ALLOWED_LOGS.keys())}")

    log_path = _LOG_DIR / _ALLOWED_LOGS[source]
    raw_lines = _tail_lines(log_path, lines)

    # 过滤
    result = raw_lines
    if task_id:
        result = [l for l in result if task_id in l]
    if keyword:
        result = [l for l in result if keyword in l]

    if not result:
        return PlainTextResponse(f"[暂无匹配的日志行]  source={source}  lines={lines}\n")

    return PlainTextResponse("\n".join(result) + "\n")


@router.get("/logs/list", summary="列出可用日志文件")
async def list_logs(_admin: dict = Depends(_require_admin)):
    """返回各日志文件的名称、大小、最后修改时间"""
    result = []
    for key, filename in _ALLOWED_LOGS.items():
        p = _LOG_DIR / filename
        if p.exists():
            stat = p.stat()
            result.append({
                "key":       key,
                "filename":  filename,
                "size_kb":   round(stat.st_size / 1024, 1),
                "modified":  int(stat.st_mtime),
            })
        else:
            result.append({"key": key, "filename": filename, "size_kb": 0, "modified": 0})
    return result
