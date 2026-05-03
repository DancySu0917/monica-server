from pydantic import BaseModel
from typing import List, Optional


class SlicePerception(BaseModel):
    """Step 1 输出：每张切片的视觉感知"""
    slice_rank:          int
    window_type:         str = "lung"         # lung / mediastinum
    visual_description:  str
    abnormal_regions:    List[str] = []
    quality_note:        Optional[str] = None


class NoduleIntegration(BaseModel):
    """Step 2 输出：跨切片结节整合"""
    integrated_nodule_id:      str
    best_slice_rank:           int
    cross_slice_consistency:   str
    estimated_3d_size:         str
    location_description:      str


class CoTIntermediateResult(BaseModel):
    """CoT Step 1+2 的中间产物，落库供调试"""
    task_id:            str
    step1_perceptions:  List[SlicePerception]
    step2_integrations: List[NoduleIntegration]
    step1_tokens:       int = 0
    step2_tokens:       int = 0
