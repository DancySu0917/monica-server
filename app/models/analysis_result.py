from sqlalchemy import Column, String, Integer, Float, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class AnalysisResult(Base):
    """最终分析报告表：append-only，支持版本化"""
    __tablename__ = "analysis_results"

    id                = Column(String, primary_key=True)
    task_id           = Column(String, index=True)
    user_id           = Column(String, index=True)
    version           = Column(Integer, default=1)
    findings          = Column(Text)           # JSON
    impression        = Column(Text)
    nodule_assessment = Column(Text)           # JSON
    recommendations   = Column(Text)           # JSON
    confidence        = Column(Float)
    limitations       = Column(Text)           # JSON
    disclaimer        = Column(Text, nullable=False)
    cot_snapshot      = Column(Text)           # JSON，CoT 三步中间结果
    raw_response      = Column(Text)           # LLM 原始响应（合规存档）
    llm_model         = Column(String)
    tokens_step1      = Column(Integer, default=0)
    tokens_step2      = Column(Integer, default=0)
    tokens_step3      = Column(Integer, default=0)
    eval_scores       = Column(Text)           # JSON
    created_at        = Column(DateTime, server_default=func.now())
