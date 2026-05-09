"""
Stage 4: 切片选择 + 双窗位渲染 + pHash 去重

- 评分策略：病灶显著性评分（基于图像特征）+ 结节候选优先 + 切片位置多样性
- 渲染：肺窗 (WC=-600, WW=1200) + 纵隔窗 (WC=40, WW=400) → 512×512 PNG
- pHash 去重：汉明距离 < 8 的切片视为重复，丢弃
- 病灶热点检测：独立于Stage3候选，直接分析每张切片的图像特征，确保有病灶的切片被优先选中
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
from app.services.dicom_service import apply_hu_transform, extract_lung_mask, read_dicom_meta

logger = logging.getLogger(__name__)

TARGET_SIZE = (512, 512)
PHASH_HAMMING_THRESHOLD = 8   # 汉明距离 < 8 视为重复

# 窗位预设
LUNG_WC, LUNG_WW           = -600.0, 1200.0   # 标准肺窗（WW从1500收窄至1200，提高GGN对比度）
MEDIASTINUM_WC, MEDIASTINUM_WW = 40.0, 400.0
GGN_WC, GGN_WW              = -500.0, 600.0    # GGN增强窗（窄窗，HU[-800,-200]，GGN显影更清晰）


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

    # 构建候选切片索引 + 每个切片的最大结节直径（用于评分权重）
    candidate_slice_set: Set[int] = {c.slice_index for c in candidates}
    # 每个候选切片的最大直径映射（大结节临床意义更大，应获得更高分数）
    max_diam_per_slice: Dict[int, float] = {}
    max_conf_per_slice: Dict[int, float] = {}
    for c in candidates:
        if c.slice_index not in max_diam_per_slice or c.estimated_diameter_mm > max_diam_per_slice[c.slice_index]:
            max_diam_per_slice[c.slice_index] = c.estimated_diameter_mm
        if c.slice_index not in max_conf_per_slice or c.confidence > max_conf_per_slice[c.slice_index]:
            max_conf_per_slice[c.slice_index] = c.confidence

    # 对每张 DICOM 打分
    scores = _score_slices(dcm_files, candidate_slice_set, top_k, max_diam_per_slice, max_conf_per_slice)

    # 按分数排序，取 top_k
    sorted_indices = [idx for idx, _ in sorted(scores.items(), key=lambda x: -x[1])]

    # ── 双轨选片策略 ──────────────────────────────────────────────
    # 轨道A：高分候选（结节优先）- 占 top_k 的 60%
    # 轨道B：强制均匀覆盖（全局分段采样）- 占 top_k * 2 个均匀点，补充不重复的
    # 两轨去重后合并，确保既覆盖可疑结节又不遗漏任何区域

    track_a_count = max(1, int(top_k * 0.6))
    # 轨道B均匀采样数量设为 top_k（比例更高，确保全局覆盖）
    track_b_sample = top_k

    n = len(dcm_files)
    # 强制均匀采样：将切片分成 track_b_sample 个区段，每段取中间切片
    uniform_indices: List[int] = []
    for seg in range(track_b_sample):
        seg_start = (seg * n) // track_b_sample
        seg_end   = ((seg + 1) * n) // track_b_sample
        seg_mid   = (seg_start + seg_end) // 2
        uniform_indices.append(min(seg_mid, n - 1))

    # 轨道A：取高分排序前 track_a_count 个（已通过_score_slices过滤假阳性）
    track_a: List[int] = []
    seen_in_combined: Set[int] = set()
    for idx in sorted_indices:
        if len(track_a) >= track_a_count:
            break
        if idx not in seen_in_combined:
            track_a.append(idx)
            seen_in_combined.add(idx)

    # 轨道B：均匀采样补充（避免与轨道A重复）
    track_b: List[int] = []
    for idx in uniform_indices:
        if idx not in seen_in_combined:
            track_b.append(idx)
            seen_in_combined.add(idx)

    # 合并两轨，按分数重新排序，留冗余供 pHash 去重消耗
    combined_order = track_a + track_b
    combined_order.sort(key=lambda x: -scores.get(x, 0.0))
    sorted_indices = combined_order[:top_k * 2]  # 留冗余供pHash去重消耗
    logger.info(f"[Stage4] 双轨选片: 轨道A={len(track_a)}个高分候选, 轨道B={len(track_b)}个均匀补充, 合计={len(combined_order)}个候选")

    # 渲染 + pHash 去重
    selected_slices: List[SelectedSlice] = []
    seen_phashes: List[str] = []
    out_dir = settings.storage_root_abs / "processed" / task_id / "slices"
    out_dir.mkdir(parents=True, exist_ok=True)

    for rank, idx in enumerate(sorted_indices):
        if len(selected_slices) >= top_k:
            break
        if idx >= len(dcm_files):
            continue

        dcm_path = dcm_files[idx]
        # 取此切片的结节候选（用于渲染时叠加标注框）
        slice_candidates_for_render = [c for c in candidates if c.slice_index == idx]
        try:
            dual = _render_dual_window(
                dcm_path, task_id, idx, out_dir,
                nodule_candidates=slice_candidates_for_render,
            )
        except Exception as e:
            logger.warning(f"[Stage4] 渲染切片 {idx} 失败: {e}")
            continue

        # pHash 去重
        if _is_duplicate(dual.phash_lung, seen_phashes):
            logger.debug(f"[Stage4] 切片 {idx} pHash 重复，跳过")
            continue

        seen_phashes.append(dual.phash_lung)

        # 取此切片的结节候选（已在渲染时使用，这里继续传给 SelectedSlice）
        slice_candidates = slice_candidates_for_render

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
                # 保存像素间距，供 LLM 层准确估算结节实际大小
                "fov_mm": round(float(meta["pixel_spacing"][0]) * 512, 1) if meta.get("pixel_spacing") else None,
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

def _compute_abnormality_score(dcm_path: str) -> float:
    """
    独立于 Stage3 候选，直接分析切片的图像特征，计算病灶显著性评分（0.0 ~ 1.0）。

    评分维度：
    1. 结节候选密度：肺野内非血管异常高密度区的数量和大小
    2. 磨玻璃影面积：HU -700 ~ -300 在肺野内的面积占比
    3. 实变/肺炎：HU -100 ~ 100 在肺野内大面积分布
    4. 胸腔积液：图像底部高密度区
    5. 气肿/大疱：肺野内极低密度且面积大的区域

    返回 0.0（正常肺片）~ 1.0（强烈异常信号）
    """
    try:
        import pydicom as _pydicom
        import numpy as _np
        from skimage import measure as _measure, morphology as _morphology
        from app.services.dicom_service import apply_hu_transform as _apply_hu

        ds  = _pydicom.dcmread(dcm_path)
        hu  = _apply_hu(ds.pixel_array, ds)
        h, w = hu.shape

        # 使用公共函数提取肺野掩膜（与 Stage3 保持一致）
        body_mask, lung_air = extract_lung_mask(hu)

        total_lung_px = int(lung_air.sum())
        if total_lung_px < 20000:
            # 肺野太少，非胸部切片，评分为0
            return 0.0

        # 扩展 ROI（包含紧邻肺边缘的结节）
        lung_roi = _morphology.binary_dilation(lung_air, _morphology.disk(12))
        lung_roi = lung_roi & body_mask

        # ── 维度1: 结节/病灶候选评分 ─────────────────────────────────
        # 在肺野ROI内检测高密度局灶性异常（结节、实变）
        # 磨玻璃结节：HU -700 ~ -250
        ggn_mask = (hu > -700) & (hu < -250) & lung_roi
        ggn_mask = _morphology.remove_small_objects(ggn_mask, min_size=20)
        ggn_mask = _morphology.binary_closing(ggn_mask, _morphology.disk(2))

        # 亚实性/实性结节：HU -250 ~ +200
        solid_mask = (hu > -250) & (hu < 200) & lung_roi
        solid_mask = _morphology.remove_small_objects(solid_mask, min_size=15)

        # 合并并提取连通区域
        nodule_mask = ggn_mask | solid_mask
        nodule_mask = _morphology.binary_closing(nodule_mask, _morphology.disk(2))
        labeled = _measure.label(nodule_mask.astype(_np.uint8))
        regions = _measure.regionprops(labeled)

        # 筛选符合结节尺寸的区域
        nodule_score = 0.0
        pixel_spacing = 0.7
        try:
            if hasattr(ds, 'PixelSpacing') and ds.PixelSpacing:
                pixel_spacing = float(ds.PixelSpacing[0])
        except Exception:
            pass

        for r in regions:
            diam_px = r.equivalent_diameter
            diam_mm = diam_px * pixel_spacing
            if diam_mm < 3.0 or diam_mm > 35.0:
                continue
            circularity = (4 * _np.pi * r.area) / (r.perimeter ** 2 + 1e-6)
            if circularity < 0.08:
                continue
            bbox_w_px = r.bbox[3] - r.bbox[1]
            bbox_h_px = r.bbox[2] - r.bbox[0]
            aspect_ratio = max(bbox_w_px, bbox_h_px) / (min(bbox_w_px, bbox_h_px) + 1e-6)
            if aspect_ratio > 4.0:
                continue
            # 对比度验证：候选区域必须比肺背景密度高
            region_hu = hu[labeled == r.label]
            mean_hu = float(_np.mean(region_hu))
            bg_hu = float(_np.percentile(hu[lung_air], 10))
            if mean_hu - bg_hu < 15:
                continue
            # 单个结节贡献评分（大结节贡献更多）
            node_contrib = min(0.4, 0.1 + (diam_mm / 30.0) * 0.3)
            nodule_score = min(1.0, nodule_score + node_contrib)

        # ── 维度2: 磨玻璃影（GGO）面积 ──────────────────────────────
        # 弥漫性磨玻璃：HU -750 ~ -350，在肺野内面积占比大
        ggo_mask = (hu > -750) & (hu < -350) & lung_roi
        ggo_mask = _morphology.remove_small_objects(ggo_mask, min_size=100)
        ggo_ratio = float(ggo_mask.sum()) / (total_lung_px + 1e-6)
        ggo_score = min(0.8, ggo_ratio * 3.0)  # GGO超过27%肺野时满分

        # ── 维度3: 实变/肺炎（大面积高密度）────────────────────────
        consolidation_mask = (hu > -100) & (hu < 100) & lung_roi
        consolidation_mask = _morphology.remove_small_objects(consolidation_mask, min_size=500)
        consolidation_ratio = float(consolidation_mask.sum()) / (total_lung_px + 1e-6)
        consolidation_score = min(0.9, consolidation_ratio * 5.0)  # 实变超过18%肺野时满分

        # ── 维度4: 胸腔积液（图像下部高密度月牙形区域）────────────
        # 胸腔积液通常出现在图像下部 30% 区域，密度 0~80HU
        lower_region = hu[int(h * 0.65):, :]
        pleural_mask = (lower_region > 0) & (lower_region < 80) & body_mask[int(h * 0.65):, :]
        pleural_px = int(pleural_mask.sum())
        # 胸腔积液 > 500px 时认为有意义
        pleural_score = min(0.6, pleural_px / 3000.0)

        # ── 维度5: 肺气肿/肺大疱（极低密度大面积区域）─────────────
        # 正常肺 HU -900 ~ -600，肺气肿/大疱 HU < -900，面积大
        emphysema_mask = (hu < -900) & lung_roi
        emphysema_mask = _morphology.remove_small_objects(emphysema_mask, min_size=300)
        emphysema_ratio = float(emphysema_mask.sum()) / (total_lung_px + 1e-6)
        emphysema_score = min(0.5, emphysema_ratio * 2.0)  # 占肺野25%以上时满分

        # ── 综合评分：各维度加权融合 ─────────────────────────────────
        # 结节/局灶病灶权重最高（是我们最关心的）
        combined = (
            nodule_score        * 0.45 +
            ggo_score           * 0.20 +
            consolidation_score * 0.20 +
            pleural_score       * 0.10 +
            emphysema_score     * 0.05
        )

        return round(min(1.0, combined), 4)

    except Exception as e:
        logger.debug(f"[Stage4] 病灶评分失败 {dcm_path}: {e}")
        return 0.0


def _has_bilateral_lung(dcm_path: str) -> bool:
    """
    判断该切片是否具有双侧肺野（胸部切片）。
    三级验证策略（与 Stage 3 保持一致）：
    - 大量肺气(>=50000px): 只需2个面积相当的独立区域
    - 中等肺气(35000-50000px): 需要双侧分布证据
    - 少量肺气(30000-35000px): 宽松条件（肺尖区域）
    - 肺气不足(<30000px): 非肺部切片
    
    预处理流程与 Stage 3 完全一致：body_mask + binary_closing + fill_holes + lung_air
    """
    try:
        import pydicom as _pydicom
        import numpy as _np
        from skimage import measure as _measure
        from app.services.dicom_service import apply_hu_transform as _apply_hu

        ds  = _pydicom.dcmread(dcm_path)
        hu  = _apply_hu(ds.pixel_array, ds)
        h, w = hu.shape

        # 使用公共函数提取肺野掩膜（与 Stage3 保持一致）
        _, lung_air = extract_lung_mask(hu)

        total_px = int(lung_air.sum())
        if total_px < 30000:
            return False

        labeled = _measure.label(lung_air.astype(_np.uint8))
        props   = _measure.regionprops(labeled)
        large   = sorted([r for r in props if r.area >= 500], key=lambda r: r.area, reverse=True)

        if len(large) < 2:
            return False

        a1, a2 = large[0].area, large[1].area
        cx1, cx2 = large[0].centroid[1], large[1].centroid[1]
        sep = abs(cx1 - cx2) / w
        ratio = a2 / a1

        if total_px >= 50000:
            # 大量肺气，几乎肯定是肺部切片
            return a2 >= 1500 and ratio >= 0.15
        elif total_px >= 35000:
            # 中等肺气，需要双侧分布证据
            has_left = (cx1 / w < 0.5) or (cx2 / w < 0.5)
            has_right = (cx1 / w > 0.5) or (cx2 / w > 0.5)
            on_both_sides = has_left and has_right
            return (a2 >= 1500 and ratio >= 0.15
                    and (sep >= 0.20 or (on_both_sides and sep >= 0.08)))
        else:
            # 30000~35000: 肺尖等少量肺气切片
            return a2 >= 1000 and ratio >= 0.10
    except Exception:
        return True  # 读取失败时不过滤，让后续处理


def _score_slices(
    dcm_files: List[str],
    candidate_set: Set[int],
    top_k: int,
    max_diam_per_slice: Dict[int, float] = None,
    max_conf_per_slice: Dict[int, float] = None,
) -> Dict[int, float]:
    """
    为每张切片打分（病灶显著性评分 + 结节候选优先 + 全局均匀覆盖）。

    评分策略（三层叠加）：
    1. 病灶显著性评分（独立于Stage3候选，直接分析图像特征）
       - 对全部均匀采样池 + 候选切片计算 abnormality_score（0.0~1.0）
       - abnormality_score × 3.5 → 最多贡献 3.5 分（最重要的维度）
    2. Stage3结节候选加分（已验证有双肺野的候选）
       - 大结节(>=15mm): 额外 +2.0 分
       - 中等结节(10-15mm): 额外 +1.5 分
       - 小结节(<10mm): 额外 +1.0 分
    3. 全局均匀性奖励
       - 防止所有选中切片集中在同一区域

    确保：有真实病灶的切片即使被Stage3漏检，也能通过图像特征被发现。
    """
    n     = len(dcm_files)
    scores: Dict[int, float] = {}
    max_diam_per_slice = max_diam_per_slice or {}
    max_conf_per_slice = max_conf_per_slice or {}

    # 预先验证所有候选切片是否真的有双肺野（过滤腹部假阳性）
    valid_candidate_set: Set[int] = set()
    invalid_candidate_set: Set[int] = set()
    for idx in candidate_set:
        if idx < len(dcm_files):
            if _has_bilateral_lung(dcm_files[idx]):
                valid_candidate_set.add(idx)
            else:
                invalid_candidate_set.add(idx)
                logger.debug(f"[Stage4] 候选切片 {idx} 无双侧肺野（腹部/头颈假阳性），排除候选加分")

    if invalid_candidate_set:
        logger.info(f"[Stage4] 排除 {len(invalid_candidate_set)} 个无肺野的假阳性候选切片: {sorted(invalid_candidate_set)[:10]}")

    # 均匀采样：从全部切片中均匀采 top_k * 5 个位置（覆盖率更高）
    # 扩大采样池以确保捕获全部潜在病灶区域
    sample_count = min(n, top_k * 6)
    if n > sample_count:
        # 均匀间隔采样，确保覆盖头尾和中部
        step = n / sample_count
        pool = [int(round(step * k)) for k in range(sample_count)]
        pool = [min(idx, n - 1) for idx in pool]  # 防止越界
    else:
        pool = list(range(n))

    # 强制加入所有有效结节候选切片（防遗漏最重要的区域）
    pool = sorted(set(pool) | valid_candidate_set)

    # ── 对采样池中每张切片计算独立病灶显著性评分（多线程并行）───────
    # 关键改进：不依赖Stage3候选，直接基于图像特征判断切片是否有病灶
    # 使用线程池并行计算，避免串行扫描60张切片的时间开销
    from concurrent.futures import ThreadPoolExecutor, as_completed

    valid_pool = [idx for idx in pool if idx < len(dcm_files)]
    logger.info(f"[Stage4] 对 {len(valid_pool)} 张候选切片计算病灶显著性评分（多线程）...")

    abn_start = time.time()
    abnormality_scores: Dict[int, float] = {}

    def _score_one(pool_idx: int):
        return pool_idx, _compute_abnormality_score(dcm_files[pool_idx])

    # 最多使用4线程（兼顾2C服务器，避免OOM）
    max_workers = min(4, len(valid_pool))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_score_one, idx): idx for idx in valid_pool}
        for fut in as_completed(futures):
            try:
                pool_idx, abn_score = fut.result()
                abnormality_scores[pool_idx] = abn_score
            except Exception as e:
                pool_idx = futures[fut]
                abnormality_scores[pool_idx] = 0.0
                logger.debug(f"[Stage4] 切片 {pool_idx} 病灶评分异常: {e}")

    abn_elapsed = int((time.time() - abn_start) * 1000)

    # 统计高分切片
    high_abn = [(idx, s) for idx, s in abnormality_scores.items() if s > 0.3]
    high_abn.sort(key=lambda x: -x[1])
    logger.info(f"[Stage4] 病灶显著性评分完成，耗时={abn_elapsed}ms，"
                f"> 0.3 的切片: {len(high_abn)} 个，"
                f"Top5: {[(idx, f'{s:.3f}') for idx, s in high_abn[:5]]}")

    # 对候选池按相邻关系聚类，惩罚过于密集的切片（鼓励多样性）
    selected_positions: List[int] = []

    for i in pool:
        score = 0.0

        # ── 第一层：病灶显著性评分（最重要，独立于Stage3候选）──────
        abn = abnormality_scores.get(i, 0.0)
        score += abn * 3.5  # abnormality_score 满分时贡献 3.5 分

        # ── 第二层：Stage3结节候选额外加分 ──────────────────────────
        if i in valid_candidate_set:
            diam = max_diam_per_slice.get(i, 5.0)
            conf = max_conf_per_slice.get(i, 0.5)
            # Stage3候选作为补充确认，额外加分（但不再是主要分数来源）
            if diam >= 15:
                base = 2.0 + min(0.5, (diam - 15) / 20)
            elif diam >= 10:
                base = 1.5 + (diam - 10) / 20
            else:
                base = 1.0
            # 置信度微调（0.0 ~ 0.3 bonus）
            conf_bonus = min(0.3, conf * 0.3)
            score += base + conf_bonus
        elif i in invalid_candidate_set:
            # 腹部假阳性候选，扣分（使其不会出现在 top_k）
            score -= 2.0

        # ── 第三层：全局分布均匀性奖励 ──────────────────────────────
        segment = (i * top_k) // (n + 1)
        covered_segments = set((p * top_k) // (n + 1) for p in selected_positions)
        if segment not in covered_segments:
            score += 0.5  # 新区段奖励
        else:
            score += 0.1  # 已有区段小分

        # 防止切片过于集中：与已选切片距离越远越好
        if selected_positions:
            min_dist = min(abs(i - p) for p in selected_positions)
            diversity_bonus = min(0.3, min_dist / (n + 1) * 3)
            score += diversity_bonus

        scores[i] = round(score, 3)
        selected_positions.append(i)

    return scores


def _render_dual_window(
    dcm_path: str,
    task_id: str,
    idx: int,
    out_dir: Path,
    nodule_candidates: list = None,
) -> DualWindowPng:
    """渲染肺窗 + 纵隔窗 + GGN增强窗 三张 512×512 PNG

    肺窗图像额外叠加：
    - 底部物理标尺（每格10mm，帮助LLM直接对比结节大小）
    - 候选结节标注框（红色矩形 + 标签，帮助LLM定位并测量）
    """
    import imagehash

    ds  = pydicom.dcmread(dcm_path)
    hu  = apply_hu_transform(ds.pixel_array, ds)   # ★ 先做 HU 转换

    # 提取像素间距和原始尺寸（用于物理标尺精确绘制）
    # 关键：pixel_spacing 是原始 DICOM 的间距，渲染到 512×512 后需要用原始尺寸修正
    try:
        pixel_spacing = float(ds.PixelSpacing[0]) if hasattr(ds, 'PixelSpacing') and ds.PixelSpacing else 0.7
    except Exception:
        pixel_spacing = 0.7
    try:
        orig_rows = int(ds.Rows) if hasattr(ds, 'Rows') else 512
    except Exception:
        orig_rows = 512

    lung_arr        = _window_normalize(hu, LUNG_WC,        LUNG_WW)
    mediastinum_arr = _window_normalize(hu, MEDIASTINUM_WC, MEDIASTINUM_WW)
    ggn_arr         = _window_normalize(hu, GGN_WC,          GGN_WW)

    lung_path = str(out_dir / f"slice_{idx:04d}_lung.png")
    med_path  = str(out_dir / f"slice_{idx:04d}_med.png")
    ggn_path  = str(out_dir / f"slice_{idx:04d}_ggn.png")

    # 肺窗：叠加标尺 + 候选框（供LLM精确测量）
    _save_png_annotated(lung_arr, lung_path, pixel_spacing, orig_rows, nodule_candidates or [])
    # 纵隔窗和窄窗：只叠加标尺，不加候选框（保持干净对比）
    _save_png_annotated(mediastinum_arr, med_path, pixel_spacing, orig_rows, [])
    _save_png_annotated(ggn_arr, ggn_path, pixel_spacing, orig_rows, [])

    # pHash
    phash_lung = str(imagehash.phash(Image.open(lung_path)))
    phash_med  = str(imagehash.phash(Image.open(med_path)))

    return DualWindowPng(
        lung_window_path=lung_path,
        mediastinum_window_path=med_path,
        ggn_window_path=ggn_path,
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
    # 转为RGB三通道图（将灰度图复制为3通道）
    # 原因：多数视觉LLM对RGB图像识别精度明显优于灰度图，
    # 且部分LLM内部将灰度图强制转换会导致颜色信息失真
    gray_img = Image.fromarray(arr, mode="L").resize(TARGET_SIZE, Image.LANCZOS)
    rgb_img  = gray_img.convert("RGB")
    rgb_img.save(path, "PNG", optimize=True)


def _save_png_annotated(
    arr: np.ndarray,
    path: str,
    pixel_spacing: float,
    orig_size: int,
    nodule_candidates: list,
):
    """
    渲染 PNG 并叠加两类标注（帮助 LLM 精确测量结节大小）：

    1. 底部物理标尺（黄色）
       - 每格代表 10mm，标注 0、10、20、30mm 刻度
       - LLM 可直接将结节与刻度对比，无需估算像素占比

    2. 候选结节标注框（红色，仅肺窗图像）
       - 红色矩形框标出候选区域
       - 框右上角标签："候选N ~Xmm"（算法估计直径）
       - 帮助 LLM 快速定位并聚焦测量

    标尺精度说明：
       - DICOM 的 pixel_spacing 是原始图像的 mm/px
       - 原始图像 orig_size × orig_size 像素，渲染到 512×512
       - 缩放比 scale = 512 / orig_size
       - 渲染后每像素物理大小 = pixel_spacing / scale = pixel_spacing × orig_size / 512
       - 渲染后每 mm 对应像素数 = 512 / (pixel_spacing × orig_size)
       - 标尺 1 格（10mm）的像素长度 = 10 × 512 / (pixel_spacing × orig_size)
    """
    from PIL import ImageDraw, ImageFont

    gray_img = Image.fromarray(arr, mode="L").resize(TARGET_SIZE, Image.LANCZOS)
    rgb_img  = gray_img.convert("RGB")
    draw     = ImageDraw.Draw(rgb_img)
    W, H     = TARGET_SIZE  # 512, 512

    # ── 1. 底部物理标尺 ────────────────────────────────────────────
    # 标尺参数
    ruler_y       = H - 20          # 标尺顶部 y 坐标（距底部20px）
    ruler_height  = 8               # 标尺高度（px）
    tick_interval_mm = 10           # 每格10mm

    # ★ 精确计算：考虑原始图像尺寸和渲染缩放
    # 原始图像视野 FOV = pixel_spacing × orig_size  (mm)
    # 渲染到 512 后，每 mm 对应的渲染像素数 = 512 / FOV
    fov_mm             = pixel_spacing * orig_size   # 真实物理视野（mm）
    px_per_mm_rendered = 512.0 / fov_mm              # 渲染后每mm像素数
    tick_px            = int(round(tick_interval_mm * px_per_mm_rendered))

    ruler_color   = (255, 220, 0)   # 黄色（在肺窗灰色背景上清晰可见）
    text_color    = (255, 220, 0)

    # 标尺起始 x（左侧留 10px 边距）
    ruler_x_start = 10
    # 标尺长度：最多画到 50mm，不超出图像宽度
    max_ticks     = min(5, int((W - ruler_x_start - 10) / (tick_px + 1)))
    ruler_x_end   = ruler_x_start + max_ticks * tick_px

    # 横线
    draw.line([(ruler_x_start, ruler_y + ruler_height),
               (ruler_x_end,   ruler_y + ruler_height)], fill=ruler_color, width=2)

    # 刻度和标注
    for t in range(max_ticks + 1):
        x    = ruler_x_start + t * tick_px
        mm   = t * tick_interval_mm
        tick_h = ruler_height if t % 1 == 0 else ruler_height // 2
        draw.line([(x, ruler_y), (x, ruler_y + ruler_height)], fill=ruler_color, width=2)
        # 标注数字（0、10、20、30、40、50mm）
        label = f"{mm}mm" if mm == 0 else f"{mm}"
        # 用小字体写标注（PIL 默认字体）
        try:
            draw.text((x - 5, ruler_y - 13), label, fill=text_color)
        except Exception:
            pass

    # 标尺右侧说明
    try:
        draw.text((ruler_x_end + 4, ruler_y - 4), "|", fill=ruler_color)
    except Exception:
        pass

    # ── 2. 候选结节标注框（仅当有候选时绘制）──────────────────────
    BOX_COLORS = [
        (255, 60,  60),   # 红色（候选1）
        (255, 140,  0),   # 橙色（候选2）
        (50,  200, 50),   # 绿色（候选3）
        (80,  180, 255),  # 蓝色（候选4）
    ]

    for i, cand in enumerate(nodule_candidates[:4]):  # 最多标注4个候选
        try:
            # 归一化坐标 → 512×512 像素坐标
            bx  = float(cand.bbox_x if hasattr(cand, 'bbox_x') else cand.get('bbox_x', 0.5))
            by  = float(cand.bbox_y if hasattr(cand, 'bbox_y') else cand.get('bbox_y', 0.5))
            bw  = float(cand.bbox_w if hasattr(cand, 'bbox_w') else cand.get('bbox_w', 0.05))
            bh  = float(cand.bbox_h if hasattr(cand, 'bbox_h') else cand.get('bbox_h', 0.05))
            diam = float(cand.estimated_diameter_mm if hasattr(cand, 'estimated_diameter_mm')
                         else cand.get('estimated_diameter_mm', 0))

            x1 = int(bx * W)
            y1 = int(by * H)
            x2 = int((bx + bw) * W)
            y2 = int((by + bh) * H)

            # 确保框在图像内
            x1, x2 = max(0, x1), min(W - 1, x2)
            y1, y2 = max(0, y1), min(H - 1, y2)

            # 跳过无意义的极小框
            if x2 - x1 < 3 or y2 - y1 < 3:
                continue

            color = BOX_COLORS[i % len(BOX_COLORS)]

            # 画矩形框（2px 线宽）
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

            # 标签："候选N ~Xmm"
            label = f"候选{i+1} ~{diam:.0f}mm"
            # 标签位置：框右上角或框顶部
            label_x = min(x2 + 3, W - 60)
            label_y = max(y1 - 14, 2)
            # 半透明背景（用深色矩形模拟）
            try:
                draw.rectangle([label_x - 1, label_y - 1, label_x + 62, label_y + 12],
                               fill=(0, 0, 0))
                draw.text((label_x, label_y), label, fill=color)
            except Exception:
                draw.text((label_x, label_y), label, fill=color)

        except Exception as e:
            logger.debug(f"[Stage4] 候选框标注失败 #{i}: {e}")
            continue

    rgb_img.save(path, "PNG", optimize=True)


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
