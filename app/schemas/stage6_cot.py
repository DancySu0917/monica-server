from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class SlicePerception(BaseModel):
    """Step 1 输出：每张切片的视觉感知（全肺扫描）"""
    slice_rank:          int
    window_type:         str = "lung"         # lung / mediastinum
    visual_description:  str

    # ── 结节/肿块 ────────────────────────────────────────────────
    abnormal_regions:    List[Any] = []       # 兼容 str 和 dict 格式（结节候选描述）
    ggn_detected:        bool = False          # 是否检测到磨玻璃结节

    # ── 全肺其他异常（新增）──────────────────────────────────────
    # 每个元素结构参考：
    # {
    #   "category": "胸腔积液",
    #   "location": "右侧胸腔",
    #   "description": "右侧胸腔少量积液，液体深度约15mm",
    #   "severity": "少量"
    # }
    other_findings:      List[Dict[str, Any]] = []  # 非结节异常

    quality_note:        Optional[str] = None


class NoduleIntegration(BaseModel):
    """Step 2 输出：跨切片结节整合"""
    integrated_nodule_id:      str
    best_slice_rank:           int
    cross_slice_consistency:   str
    estimated_3d_size:         str
    location_description:      str
    density_type:              Optional[str] = None   # 密度类型: pGGN/mGGN/solid/微小结节/unknown
    algo_density_type:         Optional[str] = None   # Stage3算法检测密度类型（参考，不轻信）


class OtherFindingIntegration(BaseModel):
    """Step 2 输出：跨切片非结节异常整合"""
    finding_id:          str                          # 编号，如 F1
    category:            str                          # 类别
    location:            str                          # 位置
    description:         str                          # 综合描述
    severity:            str = ""                     # 严重程度
    supporting_slices:   List[int] = []               # 支持切片 rank 列表
    measurements:        Optional[Dict[str, str]] = None


class CoTIntermediateResult(BaseModel):
    """CoT Step 1+2+3 的中间产物，落库供调试"""
    task_id:                  str
    step1_perceptions:        List[SlicePerception]
    step2_integrations:       List[NoduleIntegration]
    step2_other_findings:     List[OtherFindingIntegration] = []
    step1_tokens:             int = 0
    step2_tokens:             int = 0
    step3_tokens:             int = 0
