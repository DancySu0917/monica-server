"""
FastAPI 应用入口

- lifespan：启动时建表、知识库冷启动；关闭时释放线程池
- 路由挂载：auth / upload / analysis / stream / result
- 全局异常处理
- SlowAPI 限流（基于 IP）
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.utils.rate_limit import limiter   # 集中管理，避免各 router 循环导入

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── 生命周期 ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭钩子"""
    logger.info("🚀 Monica Server 启动中...")

    # 1. 创建数据库表
    from app.database import create_tables
    create_tables()
    logger.info("✅ 数据库表初始化完成")

    # 2. 确保存储目录存在（使用绝对路径，避免工作目录变化导致问题）
    from pathlib import Path
    storage_root = settings.storage_root_abs
    for sub in ("uploads", "processed", "exports", "chunks"):
        (storage_root / sub).mkdir(parents=True, exist_ok=True)
    logger.info(f"✅ 存储目录已就绪: {storage_root}")

    # 3. 知识库冷启动（若为空则导入）—— 放入后台任务
    import asyncio
    from app.utils.thread_pool import run_in_thread

    async def _init_knowledge():
        try:
            from app.services.knowledge_service import get_knowledge_service
            ks = get_knowledge_service()
            await run_in_thread(ks.ensure_loaded)
        except Exception as e:
            logger.warning(f"知识库初始化失败（非致命）: {e}")

    asyncio.create_task(_init_knowledge())

    yield   # 应用运行中

    # 关闭：释放线程池（shutdown_pool 是同步函数，直接调用即可）
    from app.utils.thread_pool import shutdown_pool
    shutdown_pool()
    logger.info("👋 Monica Server 已关闭")


# ── FastAPI 实例 ──────────────────────────────────────────────────

app = FastAPI(
    title="Monica Medical AI Server",
    description="医疗影像 AI 分析平台 API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# 限流中间件
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS（通过 ALLOWED_ORIGINS 环境变量配置）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 路由 ──────────────────────────────────────────────────────────

from app.api.auth     import router as auth_router
from app.api.upload   import router as upload_router
from app.api.analysis import router as analysis_router
from app.api.stream   import router as stream_router
from app.api.result   import router as result_router
from app.api.admin    import router as admin_router

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(analysis_router)
app.include_router(stream_router)
app.include_router(result_router)
app.include_router(admin_router)


# ── 全局异常处理 ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"未处理异常: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "服务器内部错误，请联系管理员"},
    )


# ── 健康检查 ──────────────────────────────────────────────────────

@app.get("/health", tags=["Infra"])
@limiter.limit("60/minute")
async def health_check(request: Request):
    from app.services.file_service import DiskGuard
    from app.utils.thread_pool import run_in_thread
    disk_stats = await run_in_thread(DiskGuard().get_storage_stats)
    return {
        "status": "ok",
        "version": "1.0.0",
        "disk":  disk_stats,
    }


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Monica Medical AI Server", "status": "running"}


# ── 测试台（内部调试用，生产部署时可通过 Nginx 限制访问 IP）────────

_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/test", include_in_schema=False, summary="功能测试台")
async def test_ui():
    """内置 Web 测试台，用于验证各接口功能"""
    html_path = os.path.join(os.path.dirname(__file__), "..", "static", "test.html")
    if not os.path.exists(html_path):
        return JSONResponse(status_code=404, content={"detail": "测试页面不存在"})
    return FileResponse(html_path, media_type="text/html")
