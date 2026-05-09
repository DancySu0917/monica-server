from pydantic import BaseModel, field_validator
from typing import List, Optional, Dict, Any


class NoduleAssessment(BaseModel):
    """结节/肿块评估"""
    nodule_id:       str
    location:        str
    size_mm:         str
    lung_rads_grade: str = "2"
    morphology:      str = ""
    density_type:    str = ""
    malignancy_risk: str = "低"
    follow_up:       str = ""


class PulmonaryFinding(BaseModel):
    """
    非结节类肺部异常发现（通用结构）
    涵盖：肺炎/感染、磨玻璃影、肺气肿/慢阻肺、间质病变、
         胸腔积液、淋巴结肿大、气道异常、纵隔异常、钙化/瘢痕等
    """
    finding_id:       str                      # 编号，如 F1, F2
    category:         str                      # 类别，见下方枚举说明
    location:         str                      # 位置描述（患者解剖方向）
    description:      str                      # 详细描述
    severity:         str = ""                 # 严重程度：轻度/中度/重度 或 局灶/广泛
    measurements:     Optional[Dict[str, str]] = None  # 测量值，如 {"积液深度": "15mm", "范围": "右侧胸腔下1/3"}
    clinical_significance: str = ""           # 临床意义
    follow_up:        str = ""                 # 随访建议


# category 参考枚举（不强制，允许自由描述）：
# "肺炎/感染"、"磨玻璃影(GGO)"、"肺气肿/慢阻肺"、"肺间质病变"、
# "胸腔积液"、"胸膜增厚"、"淋巴结肿大"、"气道异常/支气管扩张"、
# "纵隔异常"、"钙化/瘢痕"、"空洞"、"肺不张"、"血管异常"、"其他"


class AnalysisReport(BaseModel):
    # 系统内部填充的元数据，LLM 不返回这些字段，解析后由调用方赋值
    task_id:           str = ""
    model_used:        str = ""
    raw_response:      str = ""        # LLM 原始响应（合规存档）

    # ── 核心报告字段 ────────────────────────────────────────────
    findings:          List[str] = []          # 影像发现列表（文字描述）
    impression:        str = ""                # 总体印象（1-5句话概括）

    # ── 结节评估（保留原有字段）────────────────────────────────
    nodule_assessment: List[NoduleAssessment] = []

    # ── 全肺其他异常（新增）────────────────────────────────────
    pulmonary_findings: List[PulmonaryFinding] = []   # 非结节类异常

    # ── 综合评估 ────────────────────────────────────────────────
    overall_lung_rads:  str = ""               # 整体 Lung-RADS（取最高级别）
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
