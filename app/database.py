"""
SQLAlchemy 数据库初始化：
- SQLite + WAL 模式（提高并发读写性能）
- sqlite-vec 扩展加载（向量检索）
- SessionLocal 上下文管理器
"""
from contextlib import contextmanager
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# ── 引擎 ──────────────────────────────────────────────────────────
engine = create_engine(
    settings.database_url_abs,   # 使用绝对路径，避免工作目录变化
    connect_args={"check_same_thread": False},
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

# WAL 模式 + 性能调优（每次新连接时设置）
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA cache_size=-32000")   # 32MB page cache
    cursor.close()


# ── ORM ──────────────────────────────────────────────────────────
Base = declarative_base()

SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def SessionLocal():
    """使用方式：with SessionLocal() as db: ..."""
    session = SessionFactory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── 工具函数 ──────────────────────────────────────────────────────
def get_task_status(task_id: str, user_id: str):
    """根据 task_id + user_id 查询任务（SSE 端点使用）"""
    from app.models.task import Task
    with SessionLocal() as db:
        return (
            db.query(Task)
            .filter(Task.task_id == task_id, Task.user_id == user_id)
            .first()
        )


def create_tables():
    """创建所有表（应用启动时调用）"""
    # 导入所有 model 以注册到 Base.metadata
    import app.models.file_record      # noqa: F401
    import app.models.upload_session   # noqa: F401
    import app.models.task             # noqa: F401
    import app.models.analysis_result  # noqa: F401
    import app.models.stage_result     # noqa: F401
    Base.metadata.create_all(bind=engine)


def get_db_connection():
    """返回原生 sqlite3 连接（供 sqlite-vec 使用）"""
    import sqlite3
    # 从绝对路径 URL 中提取文件路径
    abs_url = settings.database_url_abs
    db_path = abs_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
