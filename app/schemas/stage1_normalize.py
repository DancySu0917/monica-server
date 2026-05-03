from pydantic import BaseModel
from enum import Enum
from typing import List, Optional
from datetime import datetime


class FileType(str, Enum):
    DICOM_SINGLE    = "dicom_single"
    DICOM_SERIES    = "dicom_series"
    DICOM_ARCHIVE   = "dicom_archive"
    PATHOLOGY_SLIDE = "pathology_slide"
    CT_IMAGE        = "ct_image"
    PLAIN_IMAGE     = "plain_image"


class DicomSeriesInfo(BaseModel):
    series_uid:         str
    series_description: Optional[str] = None
    modality:           str = "CT"
    slice_count:        int
    slice_thickness_mm: Optional[float] = None
    pixel_spacing:      Optional[List[float]] = None
    window_center:      Optional[float] = None
    window_width:       Optional[float] = None
    acquisition_date:   Optional[str] = None


class NormalizedFile(BaseModel):
    file_id:           str          # SHA256
    original_filename: str
    file_type:         FileType
    storage_path:      str
    size_bytes:        int
    is_duplicate:      bool
    dicom_series:      Optional[List[DicomSeriesInfo]] = None
    created_at:        datetime


class Stage1Result(BaseModel):
    task_id:             str
    normalized_files:    List[NormalizedFile]
    total_dicom_slices:  int
    file_summary:        str
    stage:               str = "stage1_normalize"
    elapsed_ms:          int
