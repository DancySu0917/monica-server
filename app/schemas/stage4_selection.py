from pydantic import BaseModel
from typing import List, Optional, Dict


class DualWindowPng(BaseModel):
    """同一切片的双窗位渲染结果"""
    lung_window_path:       str    # 肺窗 (WC=-600, WW=1500)
    mediastinum_window_path: str   # 纵隔窗 (WC=40, WW=400)
    phash_lung:             str    # 感知哈希，用于去重
    phash_mediastinum:      str    # 纵隔窗 pHash


class SelectedSlice(BaseModel):
    series_uid:          str
    slice_index:         int
    rank:                int
    score:               float
    dual_window:         DualWindowPng
    slice_location_mm:   Optional[float] = None
    slice_thickness_mm:  Optional[float] = None
    nodule_candidates:   List = []
    dicom_metadata:      Dict = {}
    selection_reason:    str


class Stage4Result(BaseModel):
    task_id:               str
    selected_slices:       List[SelectedSlice]
    selection_strategy:    str
    total_series_slices:   int
    nodule_coverage_rate:  float
    stage:                 str = "stage4_selection"
    elapsed_ms:            int
