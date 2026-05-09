"""
Stage 3: 结节候选检测

策略：
- TotalSegmentator（若已安装）—— CPU fast 模式
- 降级：基于 HU 阈值 + 连通区域的简单结节候选提取

HU 参考范围：
  - 纯磨玻璃结节 (pGGN):   -800 ~ -300   (密度较低, 难检测)
  - 混合磨玻璃结节 (mGGN):  -600 ~ -200
  - 实性结节:               -100 ~ +100
  - 肺血管/支气管:          -500 ~ +50
  肺窗检测范围统一设为 -800 ~ +100, 后续通过面积/形态过滤
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
from app.services.dicom_service import extract_lung_mask

logger    = logging.getLogger(__name__)
evaluator = NoduleEvaluator()

# 结节尺寸过滤（像素等效直径）
# 真实肺结节：3mm ~ 30mm（>30mm 临床定义为肿块，不在本检测范围）
# 像素间距约 0.6-0.8mm/pixel，30mm 结节约 37-50px
# 上限设为 45px（约 32-36mm）防止肺门/心脏等大结构进入
MIN_DIAMETER_PX = 3
MAX_DIAMETER_PX = 45

# 全量扫描分批大小（每批读取DICOM, 避免OOM）
SCAN_BATCH = 64


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
    import shutil
    from totalsegmentator.python_api import totalsegmentator
    import SimpleITK as sitk

    # 使用统一存储根目录下的临时子目录，避免 /tmp 空间不足或容器兼容问题
    out_dir = settings.storage_root_abs / "processed" / task_id / "_totalseg_tmp"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
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

    finally:
        # 无论成功或失败，清理 TotalSegmentator 临时输出（避免磁盘泄漏）
        try:
            shutil.rmtree(out_dir, ignore_errors=True)
            logger.debug(f"[Stage3] TotalSegmentator 临时目录已清理: {out_dir}")
        except Exception as clean_err:
            logger.warning(f"[Stage3] 清理 TotalSegmentator 临时目录失败（非致命）: {clean_err}")


def _hu_threshold_detection(
    dcm_files: List[str],
    series_uid: str,
) -> List[NoduleCandidate]:
    """
    降级方案：纯 HU 阈值 + 连通区域检测（无需 TotalSegmentator）。

    改进要点：
    1. 全量扫描所有切片（不再受 DICOM_BATCH_SIZE 限制截断）
    2. 同时检测实性结节和磨玻璃结节（GGN），HU 范围覆盖 -800 ~ +100
    3. 仅在肺野范围内检测（排除胸壁、纵隔等干扰）
    4. 按置信度排序后限制最多返回50个候选
    """
    import pydicom
    import numpy as np
    from skimage import measure, morphology
    from app.services.dicom_service import apply_hu_transform

    # 每个切片最多贡献候选数（防止单张切片产生太多假阳性）
    MAX_PER_SLICE = 3

    # 用于按置信度筛选的全局列表
    all_candidates: List[NoduleCandidate] = []
    total = len(dcm_files)

    logger.info(f"[Stage3] 开始全量扫描 {total} 张切片（分批处理，每批 {SCAN_BATCH} 张）")

    for batch_start in range(0, total, SCAN_BATCH):
        batch = dcm_files[batch_start: batch_start + SCAN_BATCH]
        for j, dcm_path in enumerate(batch):
            i = batch_start + j
            try:
                ds  = pydicom.dcmread(dcm_path)
                hu  = apply_hu_transform(ds.pixel_array, ds)
                h, w = hu.shape

                # 提取体内掩膜和肺野空气区域（公共函数，与 Stage4 保持一致）
                body_mask, lung_air_region = extract_lung_mask(hu)

                if lung_air_region.sum() < 1000:
                    # 该切片几乎没有肺野空气，跳过（腹部、头颈切片）
                    continue

                # ── 双肺野验证：真正的胸部切片必须有两个面积相当的独立肺野区域 ──
                # 腹部切片：肠气/胃气一个区域远大于其他，且总像素少
                # 胸部切片：左右肺两个独立区域面积相近，总像素大
                lung_labeled = measure.label(lung_air_region.astype(np.uint8))
                lung_regions_props = measure.regionprops(lung_labeled)
                lung_regions_props_sorted = sorted(lung_regions_props, key=lambda r: r.area, reverse=True)
                large_lung_regions = [r for r in lung_regions_props_sorted if r.area >= 500]
                if len(large_lung_regions) < 2:
                    continue
                a1 = large_lung_regions[0].area
                a2 = large_lung_regions[1].area
                r1_cx = large_lung_regions[0].centroid[1]
                r2_cx = large_lung_regions[1].centroid[1]
                lateral_separation = abs(r1_cx - r2_cx) / w
                size_ratio = a2 / a1  # 次大/最大，真正左右肺接近1.0

                # ── 三级肺野验证策略 ──
                # 核心观察：腹部肠气总量<32000px，肺部切片总量>30000px
                # 肺中段切片：两肺质心可能都在图像同一侧（因心脏偏左），
                #   但总量极大(>50000px)，只需2个大区域即可确认
                # 肺尖/肺底：总量较小(30000-50000px)，需要更多分布证据
                total_lung_px = int(lung_air_region.sum())
                if total_lung_px < 30000:
                    # 总肺气过少，腹部/颈部切片
                    continue

                bilateral_pass = False
                if total_lung_px >= 50000:
                    # 大量肺气，几乎肯定是肺部切片（腹部肠气不可能达到此量）
                    # 只需2个面积相当的独立区域即可
                    bilateral_pass = a2 >= 1500 and size_ratio >= 0.15
                elif total_lung_px >= 35000:
                    # 中等肺气，需要双侧分布证据
                    has_left = r1_cx / w < 0.5 or r2_cx / w < 0.5
                    has_right = r1_cx / w > 0.5 or r2_cx / w > 0.5
                    on_both_sides = has_left and has_right
                    bilateral_pass = (a2 >= 1500
                                      and size_ratio >= 0.15
                                      and (lateral_separation >= 0.20
                                           or (on_both_sides and lateral_separation >= 0.08)))
                else:
                    # 30000~35000: 肺尖等少量肺气切片，宽松条件
                    bilateral_pass = a2 >= 1000 and size_ratio >= 0.10

                if not bilateral_pass:
                    continue

                # 扩展肺野轮廓：在肺气体区域周围扩张，包含肺内结节
                # 肺内结节通常与肺实质相邻，扩张后可以将结节也纳入感兴趣区域
                # 注意：膨胀范围不宜过大（>12px），否则会将纵隔血管/主动脉弓纳入ROI
                lung_roi = morphology.binary_dilation(lung_air_region, morphology.disk(10))
                lung_roi = lung_roi & body_mask

                # Step 2: 在肺野ROI内找高密度异常区域（相对于正常肺实质的密度增高区）
                # 正常肺实质：HU -900 ~ -300（低密度）
                # 结节/病灶：HU -600 ~ +200（密度高于正常肺实质）

                # 磨玻璃结节区域（GGN，HU -700 ~ -250，比正常肺密度高但低于软组织）
                ggn_mask = (hu > -700) & (hu < -250) & lung_roi
                ggn_mask = morphology.remove_small_objects(ggn_mask, min_size=25)

                # 亚实性/实性结节区域（HU -250 ~ +200）
                solid_mask = (hu > -250) & (hu < 200) & lung_roi
                solid_mask = morphology.remove_small_objects(solid_mask, min_size=20)

                # 合并两种掩膜
                combined_mask = solid_mask | ggn_mask

                # 形态学闭运算，填充小孔洞
                combined_mask = morphology.binary_closing(combined_mask, morphology.disk(2))

                labeled = measure.label(combined_mask.astype(np.uint8))
                regions = measure.regionprops(labeled)

                # 按面积降序，取最显著的候选
                regions = sorted(regions, key=lambda r: r.area, reverse=True)

                slice_candidates = []
                for r in regions:
                    diam = r.equivalent_diameter
                    if not (MIN_DIAMETER_PX <= diam <= MAX_DIAMETER_PX):
                        continue

                    # ── 形状过滤 ──
                    circularity = (4 * np.pi * r.area) / (r.perimeter ** 2 + 1e-6)

                    # bbox 长宽比：血管断面通常极度细长（aspect_ratio > 3）
                    bbox_w_px = r.bbox[3] - r.bbox[1]
                    bbox_h_px = r.bbox[2] - r.bbox[0]
                    aspect_ratio = max(bbox_w_px, bbox_h_px) / (min(bbox_w_px, bbox_h_px) + 1e-6)

                    # 过滤极端细长结构（血管/支气管断面）
                    # 真结节 aspect_ratio 通常 < 2.5（即使是椭圆形17×9mm也只有1.9）
                    if aspect_ratio > 3.5:
                        continue

                    # 圆形度硬过滤
                    # 纯磨玻璃结节(pGGN)圆形度可低至 0.15，但低于0.1几乎肯定是血管
                    if circularity < 0.10:
                        continue

                    cy, cx = r.centroid

                    # ── 肺野重叠率过滤 ──
                    # 真正的肺结节必须位于肺实质内或紧贴肺边缘
                    # 纵隔结构（主动脉、食管等）与肺空气区域无重叠
                    region_mask = (labeled == r.label)
                    lung_overlap = float(np.sum(region_mask & lung_air_region)) / (r.area + 1e-6)
                    if lung_overlap < 0.05:
                        # 候选区域与肺空气重叠不足5%，几乎完全是纵隔结构，跳过
                        continue

                    # 排除边缘区域（胸壁伪影）
                    margin = 0.05  # 5%边缘，避免排除胸膜下结节
                    if cx / w < margin or cx / w > 1 - margin or cy / h < margin or cy / h > 1 - margin:
                        continue

                    # 计算候选区域平均HU，判断类型
                    region_hu = hu[labeled == r.label]
                    mean_hu = float(np.mean(region_hu))

                    # 像素间距（提前获取用于尺寸评估）
                    try:
                        ps = float(ds.PixelSpacing[0]) if hasattr(ds, 'PixelSpacing') else 0.7
                    except Exception:
                        ps = 0.7
                    real_diam_mm = diam * ps

                    # 置信度计算：基于目标尺寸（3-30mm）的高斯评分
                    # 结节最优直径约15mm，过大（>30mm）视为非结节直接跳过
                    if real_diam_mm > 30.0:
                        # 超过30mm为肿块或正常结构（肺门血管断面等），跳过
                        continue

                    # ── 密度对比过滤：候选区域必须比周围肺实质密度显著增高 ──
                    # 这是区分真结节与正常肺组织/血管的关键特征
                    bg_hu = float(np.percentile(hu[lung_air_region], 10))
                    hu_contrast = mean_hu - bg_hu  # GGN密度高于背景
                    if hu_contrast < 20:
                        # 密度增高不足20HU，可能是正常肺实质变异，跳过
                        continue

                    # ── 置信度计算 ──
                    # 核心原则：真结节必须同时满足"圆形+适中大小+肺内+密度增高"
                    # 血管特征：细长（低圆形度/高长宽比）+纵隔位置（低肺重叠率）
                    optimal_diam_mm = 15.0
                    size_score = np.exp(-((real_diam_mm - optimal_diam_mm) ** 2) / (2 * 10 ** 2))
                    size_score = float(max(0.1, size_score))

                    # 形状评分（越圆越可能是结节）
                    # circularity 0.5以上为良好圆形度
                    shape_score = min(1.0, circularity / 0.5)

                    # 肺内位置评分（与肺空气重叠越多越可能是真结节）
                    lung_position_score = min(1.0, lung_overlap * 2.0)

                    # 细长惩罚（aspect_ratio > 2 时开始扣分）
                    elongation_penalty = max(0.0, 1.0 - (aspect_ratio - 1.0) / 4.0)
                    elongation_penalty = max(0.0, min(1.0, elongation_penalty))

                    # 磨玻璃 vs 实性
                    if mean_hu < -300:
                        nodule_type = "GGN"
                        # GGN密度评分：密度增高程度
                        density_score = min(1.0, hu_contrast / 400)
                        # 大GGN(>10mm)加分（仅当形状合理时才加）
                        ggn_size_bonus = 0.10 if (real_diam_mm >= 10 and circularity >= 0.15) else 0.0
                        # 综合置信度：形状+位置+大小+密度，各占合理权重
                        confidence = min(0.95, size_score * 0.20
                                                  + shape_score * 0.25
                                                  + lung_position_score * 0.20
                                                  + density_score * 0.20
                                                  + ggn_size_bonus
                                                  ) * elongation_penalty
                    else:
                        nodule_type = "solid"
                        density_score = min(1.0, hu_contrast / 500)
                        confidence = min(0.90, size_score * 0.25
                                                  + shape_score * 0.25
                                                  + lung_position_score * 0.20
                                                  + density_score * 0.20
                                                  ) * elongation_penalty

                    # 最低置信度门槛
                    if confidence < 0.15:
                        continue

                    real_diam_mm = round(real_diam_mm, 1)

                    slice_candidates.append(NoduleCandidate(
                        candidate_id=str(uuid.uuid4())[:8],
                        series_uid=series_uid,
                        slice_index=i,
                        bbox_x=float(r.bbox[1]) / w,
                        bbox_y=float(r.bbox[0]) / h,
                        bbox_w=float(r.bbox[3] - r.bbox[1]) / w,
                        bbox_h=float(r.bbox[2] - r.bbox[0]) / h,
                        center_voxel=[int(cx), int(cy), i],
                        center_mm=[float(cx * ps), float(cy * ps), float(i)],
                        estimated_diameter_mm=real_diam_mm,
                        confidence=confidence,
                        density_type=nodule_type,
                    ))

                    if len(slice_candidates) >= MAX_PER_SLICE:
                        break

                all_candidates.extend(slice_candidates)

            except Exception as e:
                logger.warning(f"[Stage3] 切片 {i} 像素读取/检测失败: {e}")

        logger.debug(f"[Stage3] 已扫描至切片 {min(batch_start + SCAN_BATCH, total)}/{total}，"
                     f"当前候选数={len(all_candidates)}")

    # ── 候选选择与去重 ──
    SEGMENTS = 10           # 将所有切片分成10个区段
    MAX_PER_SEGMENT = 5     # 每个区段最多取5个独立切片的候选（每个切片最多1个）
    MAX_PER_SLICE = 2       # 每个切片最多贡献2个候选（按最大直径取1个，按最高置信度取1个）
    MAX_TOTAL = 50          # 最终返回上限

    if total > 0 and all_candidates:
        from collections import defaultdict

        # ── Step 1: 连续切片血管检测 ──
        # 如果3个以上连续切片在同一位置(~20px内)有候选，这些候选很可能是血管
        # 对这些候选降低置信度（但不完全删除，因为血管附近的真结节可能被误检）
        pos_groups: dict = defaultdict(list)
        for c in all_candidates:
            # 用粗略位置作为key（圆整到20像素）
            key = (round(c.center_voxel[0], -1), round(c.center_voxel[1], -1))
            pos_groups[key].append(c)

        vessel_position_keys = set()
        for key, cands in pos_groups.items():
            if len(cands) >= 3:
                slices = sorted([c.slice_index for c in cands])
                # 检查是否连续（间隔<=3个切片）
                is_consecutive = all(slices[i+1] - slices[i] <= 3 for i in range(len(slices)-1))
                if is_consecutive:
                    vessel_position_keys.add(key)

        # 对血管位置的候选降低置信度
        for c in all_candidates:
            key = (round(c.center_voxel[0], -1), round(c.center_voxel[1], -1))
            if key in vessel_position_keys:
                c.confidence *= 0.4  # 血管位置候选置信度降至40%

        # ── Step 2: 每个切片最多保留2个候选 ──
        slice_cands: dict = defaultdict(list)
        for c in all_candidates:
            slice_cands[c.slice_index].append(c)

        deduped_candidates: List[NoduleCandidate] = []
        for s_idx, cands in slice_cands.items():
            cands_sorted = sorted(cands, key=lambda c: c.estimated_diameter_mm, reverse=True)
            # 取最大直径候选
            deduped_candidates.append(cands_sorted[0])
            # 如果还有其他候选，也取置信度最高的
            if len(cands) > 1:
                by_conf = sorted(cands, key=lambda c: c.confidence, reverse=True)
                if by_conf[0].candidate_id != cands_sorted[0].candidate_id:
                    deduped_candidates.append(by_conf[0])

        # ── Step 3: 按切片分段，每段取最佳候选 ──
        segment_candidates: List[NoduleCandidate] = []
        for seg in range(SEGMENTS):
            seg_start = (seg * total) // SEGMENTS
            seg_end   = ((seg + 1) * total) // SEGMENTS
            seg_cands = [c for c in deduped_candidates if seg_start <= c.slice_index < seg_end]
            # 每区段按置信度+直径综合排序
            seg_cands.sort(key=lambda c: (c.confidence, c.estimated_diameter_mm), reverse=True)
            segment_candidates.extend(seg_cands[:MAX_PER_SEGMENT])

        # ── Step 4: 合并去重，按置信度+直径排序 ──
        seen_cand_ids = set()
        unique_candidates = []
        for c in segment_candidates:
            if c.candidate_id not in seen_cand_ids:
                seen_cand_ids.add(c.candidate_id)
                unique_candidates.append(c)

        unique_candidates.sort(key=lambda c: (c.confidence, c.estimated_diameter_mm), reverse=True)
        top_candidates = unique_candidates[:MAX_TOTAL]
    else:
        all_candidates.sort(key=lambda c: c.confidence, reverse=True)
        top_candidates = all_candidates[:MAX_TOTAL]

    logger.info(f"[Stage3] 全量扫描完成: 总切片={total}, 原始候选={len(all_candidates)}, "
                f"最终返回={len(top_candidates)}")
    if top_candidates:
        # 显示候选的切片分布情况
        slice_idxs = [c.slice_index for c in top_candidates]
        logger.info(f"[Stage3] 候选切片分布: min={min(slice_idxs)}, max={max(slice_idxs)}, "
                    f"distinct={len(set(slice_idxs))}")
        logger.info(f"[Stage3] Top5候选: " +
                    ", ".join(f"slice={c.slice_index} diam={c.estimated_diameter_mm}mm conf={c.confidence:.2f}"
                              for c in top_candidates[:5]))

    return top_candidates
