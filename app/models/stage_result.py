from sqlalchemy import Column, String, Integer, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class StageResult(Base):
    """各阶段中间产物表：append-only，供审计/回放/A-B 测试"""
    __tablename__ = "stage_results"

    id            = Column(String, primary_key=True)   # f"{task_id}_{stage}"
    task_id       = Column(String, index=True, nullable=False)
    stage         = Column(String, nullable=False)     # stage1 ~ stage7
    status        = Column(String)                     # pass / warn / error
    input_json    = Column(Text)
    output_json   = Column(Text)
    eval_json     = Column(Text)
    error_message = Column(Text)
    elapsed_ms    = Column(Integer)
    created_at    = Column(DateTime, server_default=func.now())
