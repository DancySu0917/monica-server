"""
Stage 4: 切片选择 + 双窗位渲染 + pHash 去重

- 评分策略：结节覆盖率 + 切片位置多样性
- 渲染：肺窗 (WC=-600, WW=1500) + 纵隔窗 (WC=40, WW=400) → 512×512 PNG
- pHash 去重：汉明距离 < 8 的切片视为重复，丢弃
"""
import logging
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pydicom
from PIL import Image

from app.config import settings
from app.schemas.stage3_detection import Stage3Result, NoduleCandidate
from app.schemas.stage4_selection import DualWindowPng, SelectedSlice, Stage4Result
from app.services.dicom_service import apply_hu_transform, read_dicom_meta

logger = logging.getLogger(__name__)

TARGET_SIZE = (512, 512)
PHASH_HAMMING_THRESHOLD = 8   # 汉明距离 < 8 视为重复

# 窗位预设
LUNG_WC, LUNG_WW           = -600.0, 1500.0
MEDIASTINUM_WC, MEDIASTINUM_WW = 40.0, 400.0


def run_stage4(task_id: str, stage3: Stage3Result) -> Stage4Result:
    start = time.time()

    dcm_files   = stage3.dicom_paths or []
    candidates  = stage3.candidates
    top_k       = settings.TOP_K_SLICES

    if not dcm_files:
        return Stage4Result(
            task_id=task_id,
            selected_slices=[],
            selection_strategy="empty",
            total_series_slices=0,
            nodule_coverage_rate=0.0,
            elapsed_ms=int((time.time() - start) * 1000),
        )

    # 构建候选切片索引
    candidate_slice_set: Set[int] = {c.slice_index for c in candidates}

    # 对每张 DICOM 打分
    scores = _score_slices(dcm_files, candidate_slice_set, top_k)

    # 按分数排序，取 top_k
    sorted_indices = [idx for idx, _ in sorted(scores.items(), key=lambda x: -x[1])]

    # 渲染 + pHash 去重
    selected_slices: List[SelectedSlice] = []
    seen_phashes: List[str] = []
    out_dir = (
        Path(settings.STORAGE_ROOT).resolve()
        / "processed" / task_id / "slices"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    for rank, idx in enumerate(sorted_indices):
        if len(selected_slices) >= top_k:
            break
        if idx >= len(dcm_files):
            continue

        dcm_path = dcm_files[idx]
        try:
            dual = _render_dual_window(dcm_path, task_id, idx, out_dir)
        except Exception as e:
            logger.warning(f"[Stage4] 渲染切片 {idx} 失败: {e}")
            continue

        # pHash 去重
        if _is_duplicate(dual.phash_lung, seen_phashes):
            logger.debug(f"[Stage4] 切片 {idx} pHash 重复，跳过")
            continue

        seen_phashes.append(dual.phash_lung)

        # 取此切片的结节候选
        slice_candidates = [c for c in candidates if c.slice_index == idx]

        meta = read_dicom_meta(dcm_path)
        selected_slices.append(SelectedSlice(
            series_uid=meta["series_uid"],
            slice_index=idx,
            rank=rank,
            score=scores.get(idx, 0.0),
            dual_window=dual,
            slice_location_mm=meta["slice_location"],
            slice_thickness_mm=meta["slice_thickness"],
            nodule_candidates=[c.model_dump() for c in slice_candidates],
            dicom_metadata={
                "window_center":  meta["window_center"],
                "window_width":   meta["window_width"],
                "pixel_spacing":  meta["pixel_spacing"],
            },
            selection_reason=_selection_reason(idx, candidate_slice_set),
        ))

    # 结节覆盖率
    selected_idx_set = {s.slice_index for s in selected_slices}
    covered = len(candidate_slice_set & selected_idx_set)
    coverage = covered / len(candidate_slice_set) if candidate_slice_set else 1.0

    return Stage4Result(
        task_id=task_id,
        selected_slices=selected_slices,
        selection_strategy="score+phash_dedup",
        total_series_slices=len(dcm_files),
        nodule_coverage_rate=round(coverage, 3),
        elapsed_ms=int((time.time() - start) * 1000),
    )


# ── 辅助函数 ──────────────────────────────────────────────────────

def _score_slices(
    dcm_files: List[str],
    candidate_set: Set[int],
    top_k: int,
) -> Dict[int, float]:
    """为每张切片打分（结节命中 × 2 + 位置多样性 bonus）"""
    n     = len(dcm_files)
    scores: Dict[int, float] = {}

    # 均匀采样 top_k * 3 张作为候选池（控制内存）
    if n > top_k * 3:
        step = n // (top_k * 3)
        pool = list(range(0, n, step))[:top_k * 3]
    else:
        pool = list(range(n))

    # 加入所有结节候选切片（防遗漏）
    pool = sorted(set(pool) | candidate_set)

    for i in pool:
        score = 0.0
        if i in candidate_set:
            score += 2.0   # 有结节候选的切片优先
        # 位置均匀性奖励（避免集中在同一区域）
        score += 1.0 - abs(i - n // 2) / (n + 1) * 0.5
        scores[i] = round(score, 3)

    return scores


def _render_dual_window(
    dcm_path: str,
    task_id: str,
    idx: int,
    out_dir: Path,
) -> DualWindowPng:
    """渲染肺窗 + 纵隔窗两张 512×512 PNG"""
    import imagehash

    ds  = pydicom.dcmread(dcm_path)
    hu  = apply_hu_transform(ds.pixel_array, ds)   # ★ 先做 HU 转换

    lung_arr        = _window_normalize(hu, LUNG_WC,        LUNG_WW)
    mediastinum_arr = _window_normalize(hu, MEDIASTINUM_WC, MEDIASTINUM_WW)

    lung_path = str(out_dir / f"slice_{idx:04d}_lung.png")
    med_path  = str(out_dir / f"slice_{idx:04d}_med.png")

    _save_png(lung_arr,        lung_path)
    _save_png(mediastinum_arr, med_path)

    # pHash
    phash_lung = str(imagehash.phash(Image.open(lung_path)))
    phash_med  = str(imagehash.phash(Image.open(med_path)))

    return DualWindowPng(
        lung_window_path=lung_path,
        mediastinum_window_path=med_path,
        phash_lung=phash_lung,
        phash_mediastinum=phash_med,
    )


def _window_normalize(hu: np.ndarray, wc: float, ww: float) -> np.ndarray:
    """线性窗位映射到 [0, 255]"""
    low  = wc - ww / 2
    high = wc + ww / 2
    clipped = np.clip(hu, low, high)
    norm    = (clipped - low) / (high - low) * 255.0
    return norm.astype(np.uint8)


def _save_png(arr: np.ndarray, path: str):
    img = Image.fromarray(arr, mode="L").resize(TARGET_SIZE, Image.LANCZOS)
    img.save(path, "PNG", optimize=True)


def _is_duplicate(phash_str: str, seen: List[str]) -> bool:
    """检查 pHash 汉明距离，< 阈值则视为重复"""
    import imagehash
    try:
        h = imagehash.hex_to_hash(phash_str)
        return any(
            (h - imagehash.hex_to_hash(s)) < PHASH_HAMMING_THRESHOLD
            for s in seen
        )
    except Exception:
        return False


def _selection_reason(idx: int, candidate_set: Set[int]) -> str:
    if idx in candidate_set:
        return "结节候选切片（优先选取）"
    return "均匀采样补充（提高区域覆盖率）"
