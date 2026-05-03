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
    return LLMPayload(
        task_id=task_id,
        user_prompt=user_prompt,
        selected_slices=[s.model_dump() for s in stage4.selected_slices],
        nodule_description=NoduleDescription(
            nodules=[c.model_dump() for c in stage3.candidates[:20]]
        ),
        medical_context=MedicalContext(
            similar_cases=similar_cases,
            guidelines=guidelines,
        ),
        elapsed_ms=elapsed,
    )


def _build_nodule_description(stage3: Stage3Result) -> str:
    if not stage3.candidates:
        return "未发现明显结节候选"
    top = sorted(stage3.candidates, key=lambda c: -c.confidence)[:5]
    parts = []
    for c in top:
        parts.append(
            f"候选#{c.candidate_id}: "
            f"直径约 {c.estimated_diameter_mm:.1f}mm，"
            f"切片 {c.slice_index}，"
            f"置信度 {c.confidence:.2f}"
        )
    return "；".join(parts)


def _build_user_prompt(
    nodule_desc: str,
    clinical_notes: str,
    similar_cases: List[Dict],
    guidelines: List[Dict],
) -> str:
    case_text = "\n".join(
        f"- {c.get('title', '')}: {c.get('content', '')[:200]}"
        for c in similar_cases
    ) or "无相关案例"

    guide_text = "\n".join(
        f"- {g.get('title', '')}: {g.get('content', '')[:200]}"
        for g in guidelines
    ) or "无相关指南"

    clinical_part = f"\n【临床备注】{clinical_notes}" if clinical_notes else ""

    return f"""请对以下 CT 影像进行专业分析：

【结节候选摘要】
{nodule_desc}
{clinical_part}

【相似案例参考】
{case_text}

【相关诊疗指南】
{guide_text}

请按 JSON 格式输出完整分析报告，包含 findings、impression、nodule_assessment、recommendations、confidence 和 limitations 字段。"""
