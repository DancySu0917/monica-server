from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.sql import func
from app.database import Base


class FileRecord(Base):
    """文件去重表：以 SHA256 为主键，避免重复存储相同文件"""
    __tablename__ = "file_records"

    file_hash    = Column(String, primary_key=True)   # SHA256
    file_type    = Column(String, nullable=False)      # dicom_series / plain_image / ...
    storage_path = Column(String, nullable=False)
    size_bytes   = Column(Integer)
    created_at   = Column(DateTime, server_default=func.now())
