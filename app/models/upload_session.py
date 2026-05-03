from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.sql import func
from app.database import Base


class UploadSession(Base):
    """分片上传会话表：记录每次上传的状态和分片临时目录"""
    __tablename__ = "upload_sessions"

    upload_id    = Column(String, primary_key=True)
    user_id      = Column(String, nullable=False, index=True)
    filename     = Column(String, nullable=False)
    total_size   = Column(Integer)
    total_chunks = Column(Integer)
    file_sha256  = Column(String, nullable=False)   # 客户端预计算
    chunk_dir    = Column(String)                   # 分块临时目录（绝对路径）
    status       = Column(String, default="uploading")   # uploading/complete/failed
    created_at   = Column(DateTime, server_default=func.now())
