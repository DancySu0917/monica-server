import os
import logging
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    APP_ENV: str = "production"
    SECRET_KEY: str = "change-me-in-production"

    # 微信小程序
    WX_APPID: str = ""
    WX_SECRET: str = ""

    # 数据库
    DATABASE_URL: str = "sqlite:///./monica.db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # LLM 默认模型（生产环境 Evolink 降级链起点）
    DEFAULT_MODEL: str = "gemini-2.5-flash"

    # Evolink 代理（Gemini 协议 + Bearer 鉴权，生产环境使用）
    EVOLINK_API_KEY: str = ""
    EVOLINK_BASE_URL: str = "https://direct.evolink.ai/v1beta/models"

    # Ollama 本地部署（开发环境使用，兼容 OpenAI 接口格式）
    OLLAMA_BASE_URL: str = "http://localhost:11434/v1/chat/completions"
    OLLAMA_DEFAULT_MODEL: str = "amsaravi/medgemma-4b-it:q6"

    # 存储（支持相对路径和绝对路径，最终统一转为绝对路径）
    STORAGE_ROOT: str = "./storage"
    MAX_UPLOAD_SIZE_MB: int = 500
    CHUNK_SIZE_MB: int = 5

    # 配额保护
    DAILY_TOKEN_LIMIT: int = 200_000

    # 性能（适配 2C2G）
    ARQ_MAX_JOBS: int = 1
    ARQ_JOB_TIMEOUT: int = 600
    DICOM_BATCH_SIZE: int = 50
    TOP_K_SLICES: int = 10
    TOTALSEG_FAST: bool = True

    # CORS（逗号分隔，留空则开发环境允许所有，生产环境拒绝所有）
    ALLOWED_ORIGINS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def storage_root_abs(self) -> Path:
        """返回存储根目录的绝对路径，避免因工作目录变化导致路径错误"""
        p = Path(self.STORAGE_ROOT)
        if not p.is_absolute():
            # 相对路径以项目根目录（config.py 所在目录的上级）为基准
            base = Path(__file__).resolve().parent.parent
            p = base / p
        return p.resolve()

    @property
    def database_url_abs(self) -> str:
        """将 SQLite 相对路径转为绝对路径（PostgreSQL 等不受影响）"""
        url = self.DATABASE_URL
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
            # sqlite:///./monica.db 或 sqlite:///monica.db → 绝对路径
            rel = url.removeprefix("sqlite:///")
            p = Path(rel)
            if not p.is_absolute():
                base = Path(__file__).resolve().parent.parent
                p = (base / p).resolve()
            return f"sqlite:///{p}"
        return url

    @property
    def cors_origins(self) -> list[str]:
        """解析 ALLOWED_ORIGINS 为列表"""
        if self.ALLOWED_ORIGINS:
            return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]
        if self.APP_ENV == "development":
            return ["*"]
        return []

    def validate_production(self) -> None:
        """生产环境启动时检查必要配置，缺失则告警"""
        if self.APP_ENV != "production":
            return
        warnings = []
        if self.SECRET_KEY in ("change-me-in-production", "your-secret-key-here-min-32-chars-please-change-this"):
            warnings.append("SECRET_KEY 仍为默认值，请立即修改！")
        if not self.EVOLINK_API_KEY:
            warnings.append("EVOLINK_API_KEY 未设置，LLM 调用将失败")
        if not self.cors_origins:
            warnings.append("ALLOWED_ORIGINS 未设置，所有跨域请求将被拒绝")
        for w in warnings:
            logger.warning(f"[Config] ⚠️  {w}")


settings = Settings()

# 生产环境自动校验
settings.validate_production()
