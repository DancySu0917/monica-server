from pydantic import BaseModel, field_validator
from typing import List, Optional, Dict, Any


class NoduleAssessment(BaseModel):
    nodule_id:       str
    location:        str
    size_mm:         str
    lung_rads_grade: str = "2"
    morphology:      str = ""
    density_type:    str = ""
    malignancy_risk: str = "低"
    follow_up:       str = ""


class AnalysisReport(BaseModel):
    # 系统内部填充的元数据，LLM 不返回这些字段，解析后由调用方赋值
    task_id:           str = ""
    model_used:        str = ""
    raw_response:      str = ""        # LLM 原始响应（合规存档）
    findings:          List[str] = []
    impression:        str = ""
    nodule_assessment: List[NoduleAssessment] = []
    recommendations:   List[str] = []
    confidence:        float = 0.0
    limitations:       List[str] = ["AI 分析结果存在局限性，需结合临床信息综合判断"]
    disclaimer:        str = "本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
    stage:             str = "stage7_report"

    @field_validator("disclaimer")
    @classmethod
    def disclaimer_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            return "本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
        return v
