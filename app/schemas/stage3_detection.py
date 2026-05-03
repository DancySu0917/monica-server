from pydantic import BaseModel
from typing import List, Optional


class NoduleCandidate(BaseModel):
    candidate_id:           str
    series_uid:             str
    slice_index:            int
    bbox_x:                 float   # 归一化坐标 [0,1]
    bbox_y:                 float
    bbox_w:                 float
    bbox_h:                 float
    center_voxel:           List[int]
    center_mm:              List[float]
    estimated_diameter_mm:  float
    confidence:             float
    window_center:          float = -600.0
    window_width:           float = 1500.0


class Stage3Result(BaseModel):
    task_id:               str
    has_nodule_candidates: bool
    candidates:            List[NoduleCandidate]
    dicom_paths:           List[str] = []    # 所有 DICOM 文件路径（供 Stage4 使用）
    total_slices_scanned:  int
    stage:                 str = "stage3_detection"
    elapsed_ms:            int
