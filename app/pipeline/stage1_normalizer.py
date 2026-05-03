"""
Stage 1: 文件标准化

- 检测文件类型（ZIP/DICOM/图像）
- ZIP 安全解压（SafeExtractor）
- SHA256 文件去重
- DICOM 元数据提取
- 脱敏（deidentify_dicom）
"""
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pydicom

from app.config import settings
from app.database import SessionLocal
from app.models.file_record import FileRecord
from app.schemas.stage1_normalize import (
    DicomSeriesInfo,
    FileType,
    NormalizedFile,
    Stage1Result,
)
from app.services.dicom_service import (
    deidentify_dicom,
    read_dicom_meta,
    safe_float,
    safe_list,
)
from app.services.file_service import SafeExtractor

logger = logging.getLogger(__name__)
extractor = SafeExtractor()

DICOM_EXTENSIONS  = {".dcm", ""}
ARCHIVE_EXTENSION = ".zip"
IMAGE_EXTENSIONS  = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def run_stage1(task_id: str, file_path: str) -> Stage1Result:
    start = time.time()
    normalized_files: List[NormalizedFile] = []
    total_dicom_slices = 0

    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        raise FileNotFoundError(f"上传文件不存在: {file_path}")

    # 解压 ZIP 或直接处理
    if file_path_obj.suffix.lower() == ARCHIVE_EXTENSION:
        dest_dir  = str(
            Path(settings.STORAGE_ROOT).resolve()
            / "processed" / task_id / "extracted"
        )
        all_files = extractor.extract(file_path, dest_dir)
    else:
        all_files = [file_path]

    for fp in all_files:
        fobj = Path(fp)
        if not fobj.exists() or fobj.stat().st_size == 0:
            continue

        file_hash     = _sha256(fp)
        is_duplicate  = _check_duplicate(file_hash)
        file_type_det = _detect_file_type(fobj)

        dicom_series_list = None
        if file_type_det in (FileType.DICOM_SINGLE, FileType.DICOM_SERIES):
            try:
                ds = pydicom.dcmread(fp)
                deidentify_dicom(ds)
                ds.save_as(fp, write_like_original=False)
                meta = read_dicom_meta(fp)
                dicom_series_list = [
                    DicomSeriesInfo(
                        series_uid=meta["series_uid"] or str(uuid.uuid4()),
                        modality=meta["modality"],
                        slice_count=1,
                        slice_thickness_mm=meta["slice_thickness"],
                        pixel_spacing=meta["pixel_spacing"],
                        window_center=meta["window_center"],
                        window_width=meta["window_width"],
                    )
                ]
                total_dicom_slices += 1
            except Exception as e:
                logger.warning(f"[Stage1] DICOM 读取失败 {fp}: {e}")

        if not is_duplicate:
            _register_file(file_hash, file_type_det, fp)

        normalized_files.append(
            NormalizedFile(
                file_id=file_hash,
                original_filename=fobj.name,
                file_type=file_type_det,
                storage_path=fp,
                size_bytes=fobj.stat().st_size,
                is_duplicate=is_duplicate,
                dicom_series=dicom_series_list,
                created_at=datetime.now(timezone.utc),
            )
        )

    elapsed = int((time.time() - start) * 1000)
    return Stage1Result(
        task_id=task_id,
        normalized_files=normalized_files,
        total_dicom_slices=total_dicom_slices,
        file_summary=f"共处理 {len(normalized_files)} 个文件，DICOM 切片 {total_dicom_slices} 张",
        elapsed_ms=elapsed,
    )


# ── 辅助函数 ──────────────────────────────────────────────────────

def _sha256(file_path: str, chunk_size: int = 64 * 1024) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_duplicate(file_hash: str) -> bool:
    with SessionLocal() as db:
        return db.query(FileRecord).filter_by(file_hash=file_hash).first() is not None


def _register_file(file_hash: str, file_type: FileType, file_path: str):
    with SessionLocal() as db:
        if not db.query(FileRecord).filter_by(file_hash=file_hash).first():
            db.add(FileRecord(
                file_hash=file_hash,
                file_type=file_type.value,
                storage_path=file_path,
                size_bytes=Path(file_path).stat().st_size,
            ))
            db.commit()


def _detect_file_type(fobj: Path) -> FileType:
    ext = fobj.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return FileType.PLAIN_IMAGE
    if ext in DICOM_EXTENSIONS:
        try:
            pydicom.dcmread(str(fobj), stop_before_pixels=True)
            return FileType.DICOM_SINGLE
        except Exception:
            pass
    return FileType.PLAIN_IMAGE
