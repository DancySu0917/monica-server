from pydantic import BaseModel
from typing import List, Optional


class Stage2Result(BaseModel):
    task_id:            str
    dicom_series_dir:   str           # 提取后的 DICOM 目录路径
    series_uid:         str
    slice_count:        int
    modality:           str
    quality_score:      float         # 0~1，越高越好
    quality_issues:     List[str] = []
    passed:             bool
    stage:              str = "stage2_screen"
    elapsed_ms:         int
