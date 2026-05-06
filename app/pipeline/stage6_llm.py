"""
Stage 6: CoT 三步推理

Step 1: 并行感知每张切片（视觉感知）
Step 2: 跨切片结节整合（纯文本）
Step 3: 生成最终报告（结构化 JSON）

所有 LLM 调用通过 LLMService（降级链 + Semaphore 限流）。
LLM 输出通过 parse_llm_response 容错解析。
"""
import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import List

from app.config import settings
from app.schemas.stage5_context import LLMPayload
from app.schemas.stage6_cot import CoTIntermediateResult, SlicePerception, NoduleIntegration
from app.schemas.stage7_report import AnalysisReport, NoduleAssessment
from app.services.llm_service import LLMService
from app.utils.llm_parser import parse_llm_response

logger  = logging.getLogger(__name__)
llm_svc = LLMService()


async def run_stage6(
    task_id: str,
    payload: LLMPayload,
    model: str = "",
) -> tuple[AnalysisReport, CoTIntermediateResult]:
    start = time.time()
    model = model or settings.DEFAULT_MODEL

    logger.info(f"[Stage6] ===== 开始 CoT 推理 task_id={task_id} model={model} =====")
    logger.info(f"[Stage6] 输入切片数={len(payload.selected_slices)}  结节候选数={len(payload.nodule_description.nodules)}")

    # ── Step 1: 并行感知每张切片 ────────────────────────────────
    step1_results, step1_tokens = await _step1_perceive(
        payload.selected_slices, model
    )
    logger.info(f"[Stage6-Step1] 完成  感知切片数={len(step1_results)}  tokens={step1_tokens}")
    for i, p in enumerate(step1_results):
        logger.info(f"[Stage6-Step1] slice[{i}] rank={p.slice_rank}  "
                    f"desc={p.visual_description[:120] if p.visual_description else 'EMPTY'}")

    # ── Step 2: 跨切片结节整合 ──────────────────────────────────
    step2_results, step2_tokens = await _step2_integrate(
        step1_results, payload.nodule_description.model_dump(), model
    )
    logger.info(f"[Stage6-Step2] 完成  整合结节数={len(step2_results)}  tokens={step2_tokens}")
    for i, n in enumerate(step2_results):
        logger.info(f"[Stage6-Step2] nodule[{i}]: {json.dumps(n.model_dump(), ensure_ascii=False)}")

    # ── Step 3: 生成最终报告 ────────────────────────────────────
    report, step3_tokens = await _step3_generate_report(
        task_id=task_id,
        step1=step1_results,
        step2=step2_results,
        payload=payload,
        model=model,
    )

    cot_snapshot = CoTIntermediateResult(
        task_id=task_id,
        step1_perceptions=step1_results,
        step2_integrations=step2_results,
        step1_tokens=step1_tokens,
        step2_tokens=step2_tokens,
    )

    return report, cot_snapshot


# ── Step 1 ────────────────────────────────────────────────────────

async def _step1_perceive(
    selected_slices: list,
    model: str,
) -> tuple[List[SlicePerception], int]:
    """并发感知每张切片（多模态 + 视觉描述）"""
    STEP1_SYSTEM = (
        "你是一名资深影像科医生。请仔细观察 CT 切片图像，用中文描述你看到的内容，"
        "重点关注：结节/肿块形态、密度、边界、周围结构。"
        "仅输出 JSON，无 markdown 包裹。"
    )
    STEP1_SCHEMA = """{
  "slice_rank": <int>,
  "window_type": "<lung|mediastinum>",
  "visual_description": "<详细描述>",
  "abnormal_regions": ["<描述1>", ...],
  "quality_note": "<图像质量备注或null>"
}"""

    fallback_count = 0

    async def perceive_one(slice_data: dict) -> tuple[SlicePerception, int]:
        rank       = slice_data.get("rank", 0)
        dual       = slice_data.get("dual_window", {})
        lung_path  = dual.get("lung_window_path", "")

        content_blocks = []
        if lung_path and Path(lung_path).exists():
            b64 = _encode_image_b64(lung_path)
            if b64:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
                })

        text_block = {
            "type": "text",
            "text": (
                f"这是切片 #{rank}（肺窗）。"
                f"请分析此切片，按以下 JSON 格式输出，不要添加任何 markdown 包裹：\n{STEP1_SCHEMA}"
            ),
        }
        content_blocks.append(text_block)

        messages = [
            {"role": "system", "content": STEP1_SYSTEM},
            {"role": "user",   "content": content_blocks},
        ]

        def fallback_perception():
            nonlocal fallback_count
            fallback_count += 1
            return SlicePerception(
                slice_rank=rank,
                visual_description="图像感知失败（fallback），无法获取描述",
            )

        try:
            logger.debug(f"[Stage6-Step1] 切片 #{rank} 发送请求，有图片={bool(content_blocks[:-1])}")
            raw, used_model, tokens = await llm_svc.complete(
                messages=messages,
                model=model,
                response_format="json_object",
                temperature=0.1,
            )
            logger.info(f"[Stage6-Step1] 切片 #{rank} LLM原始返回({used_model}): {raw[:300]}")
            perception = parse_llm_response(raw, SlicePerception, fallback_perception)
            perception.slice_rank = rank   # 确保 rank 与切片对应
            return perception, tokens
        except Exception as e:
            logger.warning(f"[Stage6-Step1] 切片 {rank} 感知失败: {e}")
            return fallback_perception(), 0

    tasks = [perceive_one(s) for s in selected_slices[:settings.TOP_K_SLICES]]
    results = await asyncio.gather(*tasks)
    perceptions = [r[0] for r in results]
    total_tokens = sum(r[1] for r in results)

    if fallback_count > 0:
        logger.warning(f"[Stage6-Step1] {fallback_count}/{len(tasks)} 张切片使用 fallback")

    return perceptions, total_tokens


# ── Step 2 ────────────────────────────────────────────────────────

async def _step2_integrate(
    perceptions: List[SlicePerception],
    nodule_desc: dict,
    model: str,
) -> tuple[List[NoduleIntegration], int]:
    """跨切片结节整合（纯文本，成本更低）"""
    SYSTEM = (
        "你是一名资深影像科医生，擅长多切片 CT 图像综合分析。"
        "请基于多张切片的感知结果，整合为跨切片的结节整合描述。"
        "仅输出 JSON 对象，包含 nodules 数组字段，无 markdown 包裹。"
    )
    perception_text = json.dumps(
        [p.model_dump() for p in perceptions],
        ensure_ascii=False,
        indent=2,
    )
    user_msg = (
        f"各切片感知结果：\n{perception_text}\n\n"
        f"结节候选信息：{json.dumps(nodule_desc, ensure_ascii=False)}\n\n"
        "请输出 JSON 对象，其中 nodules 字段为整合结节数组，每个元素格式如下：\n"
        '{"nodules": [{"integrated_nodule_id":"N1","best_slice_rank":1,'
        '"cross_slice_consistency":"高","estimated_3d_size":"约8mm","location_description":"右上肺"}]}'
    )

    logger.info(f"[Stage6-Step2] 发送请求  感知数={len(perceptions)}  prompt长度={len(user_msg)}")
    logger.debug(f"[Stage6-Step2] user_msg前500字: {user_msg[:500]}")
    try:
        raw, _, tokens = await llm_svc.complete(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            model=model,
            response_format="json_object",
            temperature=0.1,
        )
        logger.info(f"[Stage6-Step2] LLM原始返回: {raw[:500]}")
        # 容错解析：从 {"nodules": [...]} 或直接数组中提取结节列表
        from app.utils.llm_parser import extract_json_from_llm
        obj = extract_json_from_llm(raw)
        # 优先取 nodules 字段，否则取第一个 list 值
        if isinstance(obj, dict):
            data = obj.get("nodules") or next(
                (v for v in obj.values() if isinstance(v, list)),
                []
            )
        elif isinstance(obj, list):
            data = obj
        else:
            data = []
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        integrations = []
        for item in data:
            try:
                integrations.append(NoduleIntegration(**item))
            except Exception:
                pass
        if not integrations:
            logger.warning(f"[Stage6-Step2] JSON 解析后列表为空，原始内容: {raw[:300]}")
        return integrations, tokens
    except Exception as e:
        logger.warning(f"[Stage6-Step2] 整合失败: {e}")
        return [], 0


# ── Step 3 ────────────────────────────────────────────────────────

async def _step3_generate_report(
    task_id: str,
    step1: List[SlicePerception],
    step2: List[NoduleIntegration],
    payload: LLMPayload,
    model: str,
) -> tuple[AnalysisReport, int]:
    """生成最终结构化报告"""
    SYSTEM = (
        "你是一名资深胸部影像科医生。请基于感知和整合结果，生成规范的 CT 分析报告。"
        "必须以 JSON 格式输出，包含全部字段，不要遗漏 disclaimer 字段。"
        "不要添加任何 markdown 包裹或额外文字。"
    )
    REPORT_SCHEMA = """{
  "findings": ["<影像发现1>", "<影像发现2>"],
  "impression": "<总体印象，1-2句话>",
  "nodule_assessment": [
    {
      "nodule_id": "N1",
      "location": "右上肺",
      "size_mm": "8mm",
      "lung_rads_grade": "3",
      "morphology": "实性",
      "density_type": "实性结节",
      "malignancy_risk": "低",
      "follow_up": "建议3个月后复查"
    }
  ],
  "recommendations": ["<建议1>", "<建议2>"],
  "confidence": 0.85,
  "limitations": ["AI分析结果存在局限性，需结合临床信息综合判断"],
  "disclaimer": "本报告由AI辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
}"""

    context_text = (
        f"Step1 感知摘要：{json.dumps([p.visual_description for p in step1[:5]], ensure_ascii=False)}\n"
        f"Step2 整合结节：{json.dumps([n.model_dump() for n in step2], ensure_ascii=False)}\n"
        f"用户提示：{payload.user_prompt[:500]}"
    )

    user_msg = (
        f"{context_text}\n\n"
        f"请按以下 JSON 格式输出最终报告（必须包含 disclaimer 字段）：\n{REPORT_SCHEMA}"
    )

    logger.info(f"[Stage6-Step3] 发送请求  step1数={len(step1)}  step2数={len(step2)}  prompt长度={len(user_msg)}")
    logger.info(f"[Stage6-Step3] context_text: {context_text[:600]}")
    try:
        raw, used_model, tokens = await llm_svc.complete(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            model=model,
            response_format="json_object",
            temperature=0.2,
        )
        logger.info(f"[Stage6-Step3] LLM原始返回({used_model}) tokens={tokens}: {raw[:800]}")
    except Exception as e:
        logger.error(f"[Stage6-Step3] 报告生成失败（所有模型均失败）: {e}")
        raw = "{}"
        used_model = model
        tokens = 0

    def fallback_report():
        return AnalysisReport(
            task_id=task_id,
            model_used=model,
            raw_response=raw,
            findings=["AI 分析暂时不可用，请人工复核"],
            impression="AI 分析失败，无法提供自动报告",
            confidence=0.0,
            disclaimer="本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。",
        )

    report = parse_llm_response(raw, AnalysisReport, fallback_report)
    report.task_id    = task_id
    report.model_used = used_model
    report.raw_response = raw

    # 确保 disclaimer 非空
    if not report.disclaimer or not report.disclaimer.strip():
        report.disclaimer = "本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"

    return report, tokens


# ── 图像编码 ─────────────────────────────────────────────────────

def _encode_image_b64(image_path: str) -> str:
    """将 PNG 转 base64，文件不存在或读取失败返回空字符串"""
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.debug(f"[Stage6] 图像编码失败 {image_path}: {e}")
        return ""
