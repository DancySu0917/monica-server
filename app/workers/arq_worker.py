"""
ARQ Worker 定义

- 注册 run_analysis_pipeline 任务
- 定义 WorkerSettings（Redis 连接、并发数、超时）
- 定时任务：每 6 小时清理过期文件
"""
import logging
from urllib.parse import urlparse

from arq import cron
from arq.connections import RedisSettings

from app.config import settings

# ARQ Worker 是独立进程，需要单独初始化日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,  # 覆盖 ARQ 框架默认的日志配置
)
logger = logging.getLogger(__name__)


async def run_analysis_pipeline(
    ctx: dict,
    task_id:        str,
    user_id:        str,
    file_path:      str,
    scan_type:      str = "CT",
    clinical_notes: str = "",
    model:          str = "",
):
    """
    ARQ 任务入口：调用 Pipeline 编排器。
    ctx 由 ARQ 框架注入（含 redis、job_id 等）。
    """
    logger.info(f"[Worker] 任务开始: job_id={ctx.get('job_id')}, task_id={task_id}")
    from app.pipeline.orchestrator import run_pipeline
    await run_pipeline(
        task_id=task_id,
        user_id=user_id,
        file_path=file_path,
        scan_type=scan_type,
        clinical_notes=clinical_notes,
        model=model,
    )


async def clean_stale_files(ctx: dict):
    """定时任务：清理 > 24h 未完成的分片临时目录 + 过期上传压缩包"""
    logger.info("[Worker-Cron] 开始清理过期文件")
    try:
        from app.services.file_service import DiskGuard
        dg = DiskGuard()
        dg.clean_stale_chunks(max_age_hours=24)
        dg.clean_old_raw_uploads(max_age_hours=24)
        stats = dg.get_storage_stats()
        logger.info(
            f"[Worker-Cron] 磁盘剩余 {stats['free_gb']}GB / {stats['total_gb']}GB"
        )
    except Exception as e:
        logger.warning(f"[Worker-Cron] 清理失败: {e}")


# ── Redis 连接 ────────────────────────────────────────────────────

def _redis_settings() -> RedisSettings:
    parsed = urlparse(settings.REDIS_URL)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").strip("/") or 0),
    )


# ── WorkerSettings（ARQ 入口点）──────────────────────────────────

class WorkerSettings:
    functions = [run_analysis_pipeline]
    cron_jobs  = [
        cron(clean_stale_files, hour={0, 6, 12, 18}, minute=30)
    ]
    redis_settings     = _redis_settings()
    max_jobs           = settings.ARQ_MAX_JOBS
    job_timeout        = settings.ARQ_JOB_TIMEOUT
    keep_result        = 3600   # 保留任务结果 1 小时
    retry_jobs         = False   # 失败不自动重试（业务层已有降级）
    log_results        = True

    on_startup  = None
    on_shutdown = None
