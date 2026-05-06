"""
Stage 2: 影像质量筛查

- 收集所有 DICOM 文件
- 按 Z 轴排序，提取序列 metadata
- 检查切片数量、模态、缺口等
"""
import logging
import time
from pathlib import Path
from typing import List

import pydicom

from app.config import settings
from app.schemas.stage1_normalize import Stage1Result, FileType
from app.schemas.stage2_screen import Stage2Result
from app.services.dicom_service import read_dicom_meta, sort_dicom_files_by_z
from app.evaluators.quality_evaluator import QualityEvaluator

logger    = logging.getLogger(__name__)
evaluator = QualityEvaluator()

MIN_SLICE_THICKNESS_GAP_RATIO = 2.5   # 允许的最大切片间距倍数（检测缺口）


def run_stage2(task_id: str, stage1: Stage1Result) -> Stage2Result:
    start = time.time()

    # 收集所有 DICOM 文件
    dcm_files = [
        f.storage_path
        for f in stage1.normalized_files
        if f.file_type in (FileType.DICOM_SINGLE, FileType.DICOM_SERIES)
        and Path(f.storage_path).exists()
    ]

    # 也递归扫描 extracted 目录（ZIP 解压后整批文件可能不在 normalized_files 里）
    extracted_dir = Path(settings.STORAGE_ROOT).resolve() / "processed" / task_id / "extracted"
    if extracted_dir.exists():
        for p in extracted_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".dcm", ""):
                try:
                    pydicom.dcmread(str(p), stop_before_pixels=True)
                    if str(p) not in dcm_files:
                        dcm_files.append(str(p))
                except Exception:
                    pass

    if not dcm_files:
        return Stage2Result(
            task_id=task_id,
            dicom_series_dir="",
            series_uid="",
            slice_count=0,
            modality="UNKNOWN",
            quality_score=0.0,
            quality_issues=["未找到有效 DICOM 文件"],
            passed=False,
            elapsed_ms=int((time.time() - start) * 1000),
        )

    # 排序
    dcm_files = sort_dicom_files_by_z(dcm_files)

    # 读取第一张元数据作为序列代表
    meta       = read_dicom_meta(dcm_files[0])
    series_uid = meta["series_uid"] or task_id
    modality   = meta["modality"]

    # 缺口检测
    quality_issues = _check_gaps(dcm_files)

    # 序列目录（供 Stage3 使用）
    series_dir = str(Path(dcm_files[0]).parent)

    result = Stage2Result(
        task_id=task_id,
        dicom_series_dir=series_dir,
        series_uid=series_uid,
        slice_count=len(dcm_files),
        modality=modality,
        quality_score=0.0,    # 由 evaluator 填充
        quality_issues=quality_issues,
        passed=len(dcm_files) >= 5,
        elapsed_ms=int((time.time() - start) * 1000),
    )

    eval_result = evaluator.evaluate(result)
    result.quality_score = eval_result.score
    return result


def _check_gaps(dcm_files: List[str]) -> List[str]:
    """检测切片间距是否均匀（发现大缺口提示可能缺片）"""
    issues: List[str] = []
    if len(dcm_files) < 3:
        return issues

    positions = []
    for p in dcm_files:
        try:
            meta = read_dicom_meta(p)
            positions.append(meta["image_position_z"])
        except Exception:
            pass

    if len(positions) < 3:
        return issues

    gaps = [abs(positions[i+1] - positions[i]) for i in range(len(positions)-1)]
    median_gap = sorted(gaps)[len(gaps)//2]
    if median_gap < 0.01:
        return issues

    large_gaps = [g for g in gaps if g > median_gap * MIN_SLICE_THICKNESS_GAP_RATIO]
    if large_gaps:
        issues.append(
            f"检测到 {len(large_gaps)} 处切片缺口（最大间距 {max(large_gaps):.1f}mm），可能缺少切片"
        )
    return issues
