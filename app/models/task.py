from sqlalchemy import Column, String, Integer, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class Task(Base):
    """任务表：记录每次分析任务的状态、进度和结果引用"""
    __tablename__ = "tasks"

    task_id           = Column(String, primary_key=True)
    idempotency_key   = Column(String, unique=True, index=True)   # 任务去重键
    user_id           = Column(String, nullable=False, index=True)
    status            = Column(String, default="pending")         # pending/processing/done/error/rejected
    stage             = Column(String)                            # 当前阶段
    progress          = Column(Integer, default=0)
    model             = Column(String)
    reject_reason     = Column(Text)
    suggestions       = Column(Text)     # JSON 数组
    error_message     = Column(Text)
    created_at        = Column(DateTime, server_default=func.now())
    updated_at        = Column(DateTime, onupdate=func.now())
