"""
Stage 5: 知识库检索 + LLM Payload 组装

- 从 sqlite-vec 检索语义相似案例和指南条目
- 组装 LLM Payload（SelectedSlice + NoduleDescription + MedicalContext）
"""
import logging
import time
from typing import Any, Dict, List

from app.config import settings
from app.schemas.stage3_detection import Stage3Result
from app.schemas.stage4_selection import Stage4Result
from app.schemas.stage5_context import LLMPayload, MedicalContext, NoduleDescription
from app.services.knowledge_service import get_knowledge_service

logger = logging.getLogger(__name__)


def run_stage5(
    task_id: str,
    stage3: Stage3Result,
    stage4: Stage4Result,
    clinical_notes: str = "",
) -> LLMPayload:
    start = time.time()

    # 构建结节描述文本（用于知识库检索）
    nodule_desc_text = _build_nodule_description(stage3)
    query_text = (
        f"肺结节 {nodule_desc_text} {clinical_notes}".strip()
        or "肺部 CT 影像分析"
    )

    # 知识库检索
    similar_cases: List[Dict[str, Any]] = []
    guidelines:    List[Dict[str, Any]] = []

    try:
        ks = get_knowledge_service()
        similar_cases = ks.search(query_text, top_k=3, category="case")
        guidelines    = ks.search(query_text, top_k=3, category="guideline")
    except Exception as e:
        logger.warning(f"[Stage5] 知识库检索失败: {e}（使用空上下文继续）")

    # 构建用户提示词
    user_prompt = _build_user_prompt(
        nodule_desc_text, clinical_notes, similar_cases, guidelines
    )

    elapsed = int((time.time() - start) * 1000)
    # 选择传递给LLM的结节候选：三层策略确保关键病灶不遗漏
    # 1. 优先大直径结节（临床意义更大）
    # 2. 补充高置信度候选
    # 3. 补充 Stage 4 选中切片的候选（确保 LLM 看到的图像和描述一致）
    all_cands = list(stage3.candidates)

    # Stage 4 选中切片的索引集合
    selected_slice_indices = {s.slice_index for s in stage4.selected_slices}

    # 按直径降序排列，确保大GGN排在前面
    by_diam = sorted(all_cands, key=lambda c: -c.estimated_diameter_mm)
    # 按置信度降序排列
    by_conf = sorted(all_cands, key=lambda c: -c.confidence)
    # Stage 4 选中切片的候选（LLM会看到这些切片的图像，描述必须匹配）
    from_selected_slices = [c for c in all_cands if c.slice_index in selected_slice_indices]

    # 合并去重：先取最大直径的10个，再取最高置信度的5个，最后补充选中切片的候选
    seen_ids = set()
    selected_cands = []

    # 第一优先：最大直径的10个
    for c in by_diam[:10]:
        if c.candidate_id not in seen_ids:
            seen_ids.add(c.candidate_id)
            selected_cands.append(c)

    # 第二优先：最高置信度的5个
    for c in by_conf[:5]:
        if c.candidate_id not in seen_ids and len(selected_cands) < 20:
            seen_ids.add(c.candidate_id)
            selected_cands.append(c)

    # 第三优先：Stage 4 选中切片的候选（确保图文一致性）
    # 按直径降序，大结节优先
    from_selected_slices.sort(key=lambda c: -c.estimated_diameter_mm)
    for c in from_selected_slices:
        if c.candidate_id not in seen_ids and len(selected_cands) < 25:
            seen_ids.add(c.candidate_id)
            selected_cands.append(c)

    logger.info(f"[Stage5] 传递给LLM的结节候选: {len(selected_cands)}个, "
                f"直径范围: {selected_cands[-1].estimated_diameter_mm if selected_cands else 0}"
                f"-{selected_cands[0].estimated_diameter_mm if selected_cands else 0}mm")
    # 检查选中切片的候选是否全部包含
    missing_slice_cands = [c for c in from_selected_slices if c.candidate_id not in seen_ids]
    if missing_slice_cands:
        logger.warning(f"[Stage5] 选中切片有 {len(missing_slice_cands)} 个候选未传递给LLM（超过上限）")

    return LLMPayload(
        task_id=task_id,
        user_prompt=user_prompt,
        selected_slices=[s.model_dump() for s in stage4.selected_slices],
        nodule_description=NoduleDescription(
            nodules=[c.model_dump() for c in selected_cands]
        ),
        medical_context=MedicalContext(
            similar_cases=similar_cases,
            guidelines=guidelines,
        ),
        elapsed_ms=elapsed,
    )


def _build_nodule_description(stage3: Stage3Result) -> str:
    """仅描述结节数量和大小，不包含算法密度类型（避免干扰LLM视觉判断）"""
    if not stage3.candidates:
        return "未发现明显结节候选"
    # 按直径降序排列
    top = sorted(stage3.candidates, key=lambda c: -c.estimated_diameter_mm)[:5]
    parts = []
    for c in top:
        parts.append(
            f"结节候选（切片{c.slice_index}，直径约{c.estimated_diameter_mm:.1f}mm，"
            f"置信度{c.confidence:.2f}）"
        )
    return "；".join(parts)


def _build_user_prompt(
    nodule_desc: str,
    clinical_notes: str,
    similar_cases: List[Dict],
    guidelines: List[Dict],
) -> str:
    """
    构建用户提示词。
    注意：nodule_desc 不包含密度类型信息，密度判断完全交给LLM基于图像决定。
    """
    clinical_part = f"\n【临床备注】{clinical_notes}" if clinical_notes else ""

    return f"""请对以下肺部 CT 影像进行专业分析，基于图像视觉内容判断结节的密度类型（实性/混合磨玻璃/纯磨玻璃）和大小。

算法初步检测到 {nodule_desc}{clinical_part}

请按 JSON 格式输出完整分析报告，包含 findings、impression、nodule_assessment、recommendations、confidence 和 limitations 字段。"""
