"""
Stage 3: 结节候选检测

策略：
- TotalSegmentator（若已安装）—— CPU fast 模式
- 降级：基于 HU 阈值 + 连通区域的简单结节候选提取
"""
import logging
import time
import uuid
from pathlib import Path
from typing import List

from app.config import settings
from app.schemas.stage2_screen import Stage2Result
from app.schemas.stage3_detection import NoduleCandidate, Stage3Result
from app.evaluators.nodule_evaluator import NoduleEvaluator

logger    = logging.getLogger(__name__)
evaluator = NoduleEvaluator()


def run_stage3(task_id: str, stage2: Stage2Result) -> Stage3Result:
    start = time.time()

    if not stage2.passed:
        return Stage3Result(
            task_id=task_id,
            has_nodule_candidates=False,
            candidates=[],
            total_slices_scanned=0,
            elapsed_ms=int((time.time() - start) * 1000),
        )

    # 收集 DICOM 文件
    from app.services.dicom_service import sort_dicom_files_by_z
    dcm_files = _collect_dicom_files(stage2.dicom_series_dir)
    if not dcm_files:
        return Stage3Result(
            task_id=task_id,
            has_nodule_candidates=False,
            candidates=[],
            total_slices_scanned=0,
            elapsed_ms=int((time.time() - start) * 1000),
        )

    # 尝试 TotalSegmentator，失败则降级
    candidates: List[NoduleCandidate] = []
    try:
        candidates = _run_total_segmentator(task_id, stage2.dicom_series_dir, dcm_files)
        logger.info(f"[Stage3] TotalSegmentator 找到 {len(candidates)} 个候选")
    except Exception as e:
        logger.warning(f"[Stage3] TotalSegmentator 不可用或失败: {e}，降级为 HU 阈值")
        candidates = _hu_threshold_detection(dcm_files, stage2.series_uid)

    evaluator.evaluate(Stage3Result(
        task_id=task_id,
        has_nodule_candidates=bool(candidates),
        candidates=candidates,
        total_slices_scanned=len(dcm_files),
        elapsed_ms=0,
    ))

    return Stage3Result(
        task_id=task_id,
        has_nodule_candidates=bool(candidates),
        candidates=candidates,
        dicom_paths=dcm_files,
        total_slices_scanned=len(dcm_files),
        elapsed_ms=int((time.time() - start) * 1000),
    )


def _collect_dicom_files(series_dir: str) -> List[str]:
    from app.services.dicom_service import sort_dicom_files_by_z
    import pydicom

    p = Path(series_dir)
    if not p.exists():
        return []
    files = []
    for f in p.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".dcm", ""):
            try:
                pydicom.dcmread(str(f), stop_before_pixels=True)
                files.append(str(f))
            except Exception:
                pass
    return sort_dicom_files_by_z(files)


def _run_total_segmentator(
    task_id: str,
    series_dir: str,
    dcm_files: List[str],
) -> List[NoduleCandidate]:
    """
    调用 TotalSegmentator（需独立安装 TotalSegmentator + SimpleITK）。
    快速模式（--fast）可在 CPU 上运行，约 5~10 分钟。
    """
    from totalsegmentator.python_api import totalsegmentator
    import SimpleITK as sitk

    out_dir = Path(f"/tmp/seg_{task_id}")
    out_dir.mkdir(exist_ok=True)

    totalsegmentator(
        input=series_dir,
        output=str(out_dir),
        fast=settings.TOTALSEG_FAST,
        task="total",
        quiet=True,
    )

    # 解析肺部分割 mask，提取结节候选中心
    lung_mask_paths = list(out_dir.glob("lung*.nii.gz")) + list(out_dir.glob("*lung*.nii.gz"))
    if not lung_mask_paths:
        return []

    candidates: List[NoduleCandidate] = []
    for mask_path in lung_mask_paths[:1]:   # 取第一个肺部 mask
        mask_img = sitk.ReadImage(str(mask_path))
        mask_arr = sitk.GetArrayFromImage(mask_img)

        # 在肺部 mask 内用 HU 阈值找结节
        for i, dcm_path in enumerate(dcm_files[:settings.DICOM_BATCH_SIZE]):
            import pydicom, numpy as np
            from app.services.dicom_service import apply_hu_transform

            ds  = pydicom.dcmread(dcm_path)
            hu  = apply_hu_transform(ds.pixel_array, ds)
            # 肺结节 HU 范围：-650 ~ 250
            nodule_mask = (hu > -650) & (hu < 250)
            if mask_arr.shape[0] > i:
                nodule_mask &= mask_arr[i] > 0

            from skimage import measure
            labeled = measure.label(nodule_mask.astype(np.uint8))
            regions = measure.regionprops(labeled)
            for r in regions:
                if r.area < 10 or r.area > 10000:
                    continue
                cy, cx = r.centroid
                h, w   = hu.shape
                size   = (r.equivalent_diameter * 1.0)  # 近似像素径
                candidates.append(NoduleCandidate(
                    candidate_id=str(uuid.uuid4())[:8],
                    series_uid=str(ds.get("SeriesInstanceUID", "")),
                    slice_index=i,
                    bbox_x=float(r.bbox[1]) / w,
                    bbox_y=float(r.bbox[0]) / h,
                    bbox_w=float(r.bbox[3] - r.bbox[1]) / w,
                    bbox_h=float(r.bbox[2] - r.bbox[0]) / h,
                    center_voxel=[int(cx), int(cy), i],
                    center_mm=[float(cx), float(cy), float(i)],
                    estimated_diameter_mm=round(size, 1),
                    confidence=min(1.0, float(r.area) / 200),
                ))
                if len(candidates) >= 100:
                    break

    return candidates[:50]   # 最多返回 50 个候选


def _hu_threshold_detection(
    dcm_files: List[str],
    series_uid: str,
) -> List[NoduleCandidate]:
    """
    降级方案：纯 HU 阈值 + 连通区域检测（无需 TotalSegmentator）。
    精度较低但可靠，保证基本功能。
    """
    import pydicom
    import numpy as np
    from skimage import measure, morphology
    from app.services.dicom_service import apply_hu_transform

    candidates: List[NoduleCandidate] = []
    batch = dcm_files[:settings.DICOM_BATCH_SIZE]

    for i, dcm_path in enumerate(batch):
        try:
            ds  = pydicom.dcmread(dcm_path)
            hu  = apply_hu_transform(ds.pixel_array, ds)
            h, w = hu.shape

            # 肺窗 HU 阈值
            mask = (hu > -700) & (hu < -300)
            # 形态学操作去噪
            mask = morphology.remove_small_objects(mask, min_size=30)

            labeled = measure.label(mask.astype(np.uint8))
            regions = measure.regionprops(labeled)

            for r in regions:
                # 尺寸过滤：等效直径 3mm~30mm（以 pixel 估算）
                if not (5 <= r.equivalent_diameter <= 60):
                    continue
                cy, cx = r.centroid
                candidates.append(NoduleCandidate(
                    candidate_id=str(uuid.uuid4())[:8],
                    series_uid=series_uid,
                    slice_index=i,
                    bbox_x=float(r.bbox[1]) / w,
                    bbox_y=float(r.bbox[0]) / h,
                    bbox_w=float(r.bbox[3] - r.bbox[1]) / w,
                    bbox_h=float(r.bbox[2] - r.bbox[0]) / h,
                    center_voxel=[int(cx), int(cy), i],
                    center_mm=[float(cx), float(cy), float(i)],
                    estimated_diameter_mm=round(r.equivalent_diameter, 1),
                    confidence=min(1.0, float(r.area) / 500),
                ))
        except Exception as e:
            logger.debug(f"[Stage3] 切片 {i} 检测失败: {e}")

        if len(candidates) >= 50:
            break

    return candidates[:50]
