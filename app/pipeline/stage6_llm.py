"""
Stage 6: CoT 三步推理（全肺综合分析版）

Step 1: 并行感知每张切片 — 全肺扫描，包括结节、感染、积液、气肿等所有异常
Step 2: 跨切片整合 — 合并结节清单 + 其他异常清单
Step 3: 生成最终全面报告 — 结节 Lung-RADS + 全肺其他发现

设计原则：
- 算法只提供"候选位置坐标"，不传递密度类型判断
- 密度、大小、类型由 LLM 基于 CT 图像视觉判断
- CT 影像学惯例：图像左侧 = 患者右肺，图像右侧 = 患者左肺
- Step1 负责全肺扫描，不只看算法标记区域
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
from app.schemas.stage6_cot import (
    CoTIntermediateResult,
    NoduleIntegration,
    OtherFindingIntegration,
    SlicePerception,
)
from app.schemas.stage7_report import AnalysisReport, NoduleAssessment, PulmonaryFinding
from app.services.llm_service import LLMService
from app.utils.llm_parser import parse_llm_response

logger  = logging.getLogger(__name__)
llm_svc = LLMService()


async def run_stage6(
    task_id: str,
    payload: LLMPayload,
    model: str = "",
    provider: str = "",
) -> tuple[AnalysisReport, CoTIntermediateResult]:
    start = time.time()
    model = model or settings.DEFAULT_MODEL

    logger.info(f"[Stage6] ===== 开始 CoT 推理（全肺分析）task_id={task_id} model={model} provider={provider or 'auto'} =====")
    logger.info(f"[Stage6] 输入切片数={len(payload.selected_slices)}  结节候选数={len(payload.nodule_description.nodules)}")

    # ── Step 1: 并行全肺感知每张切片 ────────────────────────────
    step1_results, step1_tokens = await _step1_perceive(
        payload.selected_slices, model, provider
    )
    logger.info(f"[Stage6-Step1] 完成  感知切片数={len(step1_results)}  tokens={step1_tokens}")
    for i, p in enumerate(step1_results):
        logger.info(f"[Stage6-Step1] slice[{i}] rank={p.slice_rank}  "
                    f"other_findings={len(p.other_findings)}  "
                    f"desc={p.visual_description[:100] if p.visual_description else 'EMPTY'}")

    # Step1 全部 fallback 时提前检查：无图像感知数据则无法可靠分析
    step1_valid_count = sum(
        1 for p in step1_results
        if p.visual_description and "fallback" not in p.visual_description and "失败" not in p.visual_description
    )
    if step1_valid_count == 0:
        logger.error(
            f"[Stage6] Step1 全部失败（{len(step1_results)}/{len(step1_results)} 张切片 fallback），"
            f"无法进行可靠的图像分析，终止流程。"
            f"请检查 LLM 后端配置（需要多模态模型）或充值 BAI key。"
        )
        raise RuntimeError(
            "CT图像感知全部失败：所有切片的多模态感知均返回 fallback，"
            "无图像视觉信息支撑，无法生成可靠诊断报告。"
            "请确认 LLM 后端有多模态能力（需 gemini 系列或 gpt-4o 等视觉模型）。"
        )

    # ── Step 2: 跨切片整合（结节 + 其他异常）──────────────────
    step2_nodules, step2_other, step2_tokens = await _step2_integrate(
        step1_results, payload.nodule_description.model_dump(), model, provider
    )
    logger.info(f"[Stage6-Step2] 完成  整合结节数={len(step2_nodules)}  其他异常={len(step2_other)}  tokens={step2_tokens}")
    for i, n in enumerate(step2_nodules):
        logger.info(f"[Stage6-Step2] nodule[{i}]: {json.dumps(n.model_dump(), ensure_ascii=False)}")
    for i, f in enumerate(step2_other):
        logger.info(f"[Stage6-Step2] finding[{i}]: {json.dumps(f.model_dump(), ensure_ascii=False)}")

    # ── Step 3: 生成最终全面报告 ────────────────────────────────
    report, step3_tokens = await _step3_generate_report(
        task_id=task_id,
        step1=step1_results,
        step2_nodules=step2_nodules,
        step2_other=step2_other,
        payload=payload,
        model=model,
        provider=provider,
    )

    cot_snapshot = CoTIntermediateResult(
        task_id=task_id,
        step1_perceptions=step1_results,
        step2_integrations=step2_nodules,
        step2_other_findings=step2_other,
        step1_tokens=step1_tokens,
        step2_tokens=step2_tokens,
        step3_tokens=step3_tokens,
    )

    return report, cot_snapshot


# ── Step 1：全肺感知 ────────────────────────────────────────────

async def _step1_perceive(
selected_slices: list,
model: str,
provider: str = "",
) -> tuple[List[SlicePerception], int]:
    """
    并发感知每张切片：全肺扫描模式。

    核心原则：
    - 全肺扫描，不只关注算法标记的结节候选位置
    - 检查9大类肺部异常（结节/感染/GGO/气肿/间质病变/积液/淋巴结/气道/其他）
    - 密度类型完全由模型基于图像视觉判断，不接受算法标签
    - CT 放射学方向：图像左侧 = 患者右肺，图像右侧 = 患者左肺
    """
    STEP1_SYSTEM = (
        "你是一名资深胸部影像科医生，专长肺部CT全面分析。\n"
        "请对每张CT切片进行系统性全肺扫描，识别所有类型的肺部异常，用中文详细描述。\n"
        "\n"
        "【CT影像学阅片方向——必须记住】\n"
        "- CT轴位图像按放射学惯例显示：图像左侧 = 患者右肺，图像右侧 = 患者左肺\n"
        "- 所有位置描述使用患者解剖方向（患者左肺/右肺），非图像左右\n"
        "\n"
        "【需要检查的9大类异常】\n"
        "1. 结节/肿块：大小、密度（实性/混合/纯磨玻璃）、边界、形态\n"
        "   - 实性结节：高密度白色团块，遮蔽血管\n"
        "   - 混合磨玻璃(mGGN)：磨玻璃背景中有实性成分\n"
        "   - 纯磨玻璃(pGGN)：淡薄云雾状，不遮蔽血管\n"
        "2. 肺炎/感染：片状/斑片状实变、支气管充气征、分布范围（单/双侧）\n"
        "3. 磨玻璃影(GGO)：弥漫性/局灶性磨玻璃，面积和分布\n"
        "4. 肺气肿/慢阻肺：低密度区、肺大疱（位置、大小）、气道壁增厚\n"
        "5. 肺间质病变：网格影、蜂窝影、牵拉性支气管扩张、纤维化（分布/程度）\n"
        "6. 胸膜/积液：胸腔积液量和分布、胸膜增厚、气胸\n"
        "7. 淋巴结/纵隔：纵隔增宽、淋巴结肿大（位置、短轴径）、纵隔肿块\n"
        "8. 气道异常：支气管扩张、气道狭窄、气道壁增厚\n"
        "9. 其他：钙化、空洞（壁厚/大小）、肺不张、血管异常\n"
        "\n"
        "【结节大小估算——必须利用尺寸参考信息】\n"
        "- 提示词中会提供像素间距（mm/像素）和视野大小，请利用这些信息估算结节实际直径\n"
        "- 如：结节水平方向占图像约5%宽度，视野325mm → 直径约16mm\n"
        "- 也可参考血管直径（肺动脉约15-25mm，段级血管约3-8mm）作为比例参照\n"
        "- 大小要尽量精确，误差控制在±2mm以内\n"
        "\n"
        "【密度判断铁律（针对结节）】\n"
        "- 必须基于图像视觉：遮蔽血管 → 至少mGGN或solid；血管可见穿行 → pGGN\n"
        "- 第二张窄窗位图像提高了低密度区的对比度，实性结节在此图中仍为高密度白色\n"
        "- 判断时必须说明视觉依据\n"
        "\n"
        "【忠实性原则】\n"
        "- 只描述实际可见的异常，不臆造；若正常则明确说明正常\n"
        "- 非结节异常（如积液、气肿）不要遗漏，这些临床意义同样重要\n"
        "\n"
        "仅输出 JSON，无 markdown 包裹。"
    )

    STEP1_SCHEMA = """{
  "slice_rank": <int>,
  "window_type": "lung",
  "visual_description": "<整体描述：肺野情况、有无异常、双肺对称性等>",
  "abnormal_regions": [
    {
      "location": "<患者解剖方向：左/右肺 上/中/下叶>",
      "size_mm": "<直径估计，如16x12mm>",
      "density_type": "<pGGN|mGGN|solid|微小结节>",
      "density_basis": "<判断依据：血管是否被遮蔽？密度特征？>",
      "description": "<边界、形态详细描述>"
    }
  ],
  "ggn_detected": <true|false>,
  "other_findings": [
    {
      "category": "<分类：肺炎/感染|磨玻璃影(GGO)|肺气肿|肺间质病变|胸腔积液|胸膜异常|淋巴结肿大|气道异常|纵隔异常|钙化|空洞|肺不张|其他>",
      "location": "<患者解剖方向的位置>",
      "description": "<详细描述：范围、密度、特征等>",
      "severity": "<轻度|中度|重度 或 局灶|多发|广泛 或 少量|中量|大量>"
    }
  ],
  "quality_note": "<图像质量备注或null>"
}"""

    fallback_count = 0

    async def perceive_one(slice_data: dict) -> tuple[SlicePerception, int]:
        rank      = slice_data.get("rank", 0)
        dual      = slice_data.get("dual_window", {})
        lung_path = dual.get("lung_window_path", "")
        narrow_path = dual.get("ggn_window_path", "")  # 窄窗位（非GGN专用）

        # ── 提取像素间距信息（用于LLM准确估算结节大小）──
        dcm_meta    = slice_data.get("dicom_metadata", {})
        pixel_spacing = dcm_meta.get("pixel_spacing")  # [row_spacing, col_spacing] mm/px
        fov_mm      = dcm_meta.get("fov_mm")           # 图像实际视野大小（mm）
        slice_thickness = slice_data.get("slice_thickness_mm")

        # 构建尺寸参考说明
        if pixel_spacing and len(pixel_spacing) >= 1:
            ps = float(pixel_spacing[0])
            thickness_line = f"  切片厚度: {slice_thickness:.1f}mm\n" if slice_thickness else ""
            size_hint = (
                f"【尺寸参考——请用于估算结节大小】\n"
                f"  像素间距: {ps:.3f}mm/像素（512×512图像对应实际视野约{ps*512:.0f}mm）\n"
                + thickness_line +
                f"  估算公式: 结节占图像宽度的比例 × {ps*512:.0f}mm = 实际直径\n"
                f"  示例: 结节占图像宽度约3% → 直径约{ps*512*0.03:.0f}mm\n"
                f"        结节占图像宽度约6% → 直径约{ps*512*0.06:.0f}mm\n"
            )
        else:
            size_hint = "【尺寸参考】像素间距未知，请根据与血管/气管的相对大小估算结节直径。\n"

        content_blocks = []
        if lung_path and Path(lung_path).exists():
            b64 = _encode_image_b64(lung_path)
            if b64:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
                })

        # 第二张：窄窗位（提高低密度异常对比度，不是专门针对磨玻璃的窗口）
        if narrow_path and Path(narrow_path).exists():
            b64_narrow = _encode_image_b64(narrow_path)
            if b64_narrow:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64_narrow}", "detail": "high"},
                })

        # 只提供坐标位置，不提供密度标签
        location_hints = _format_location_only_hints(slice_data)
        has_narrow = bool(narrow_path and Path(narrow_path).exists())

        text_block = {
            "type": "text",
            "text": (
                f"切片 #{rank}：{'第一张为标准肺窗（WW=1200），第二张为窄窗位（WW=600，提高低密度异常对比度）。' if has_narrow else '标准肺窗。'}\n"
                f"⚠️ 方向提示：图像左侧 = 患者右肺，图像右侧 = 患者左肺（放射学惯例）。\n"
                f"\n"
                f"{size_hint}\n"
                f"【图像标注说明——关键！请充分利用】\n"
                f"① 肺窗图像底部有黄色物理标尺（每格=10mm），可直接对比结节与刻度来测量大小，无需估算像素比例\n"
                f"② 若肺窗图像中有彩色矩形框（红/橙/绿/蓝），框内即为算法检测到的疑似结节区域\n"
                f"   - 框右上角标签'候选N ~Xmm'中的Xmm是算法估计直径（供参考，你需要基于标尺独立测量）\n"
                f"   - 若候选框内的区域视觉上不像结节，请如实描述所见并说明理由\n"
                f"③ 测量方法：将结节的最大横径与底部标尺对比，读出毫米数\n"
                f"\n"
                f"请对这张切片进行全面系统扫描，检查所有类型肺部异常（不只看结节）：\n"
                f"① 扫描整个肺野是否有肺炎、磨玻璃影、气肿、间质改变等\n"
                f"② 检查胸膜腔是否有积液、气胸\n"
                f"③ 观察纵隔和淋巴结是否异常\n"
                f"④ 重点检查以下算法标记的疑似结节位置（仅位置参考，密度类型请自行视觉判断）：\n"
                + location_hints
                + f"\n\n结节大小请优先通过对比底部黄色标尺来测量，精确到1mm。\n"
                f"请按以下 JSON 格式输出（other_findings 数组放非结节异常）：\n{STEP1_SCHEMA}"
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
                provider=provider,
            )
            logger.info(f"[Stage6-Step1] 切片 #{rank} LLM原始返回({used_model}): {raw[:300]}")
            perception = parse_llm_response(raw, SlicePerception, fallback_perception)
            perception.slice_rank = rank
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


# ── Step 2：跨切片整合（结节 + 其他异常）────────────────────────

async def _step2_integrate(
    perceptions: List[SlicePerception],
    nodule_desc: dict,
    model: str,
    provider: str = "",
) -> tuple[List[NoduleIntegration], List[OtherFindingIntegration], int]:
    """
    跨切片整合：同时整合结节和其他肺部异常。

    核心原则：
    - 完全基于 Step1 视觉感知，不引入算法密度标签
    - 结节整合：合并跨切片可见的同一结节
    - 其他异常整合：合并多切片中重复出现的相同异常
    """
    nodules_list = nodule_desc.get("nodules", [])
    valid_perceptions = [
        p for p in perceptions
        if p.visual_description and "fallback" not in p.visual_description
    ]
    if not valid_perceptions and not nodules_list:
        logger.warning("[Stage6-Step2] 无有效感知结果且无结节候选，跳过整合步骤")
        return [], [], 0

    SYSTEM = (
        "你是一名资深影像科医生，擅长多切片CT图像综合分析。\n"
        "请基于多张切片的视觉感知结果，整合出两个清单：\n"
        "1. 结节清单（nodules）：跨切片可见的肺结节/肿块\n"
        "2. 其他异常清单（other_findings）：肺炎/积液/气肿/间质病变等非结节异常\n"
        "\n"
        "【结节整合规则】\n"
        "1. 只整合有视觉描述支持的结节，禁止凭算法候选臆造\n"
        "2. 同一结节在多切片均可见时，只保留一个（合并位置相近+相邻切片出现的）\n"
        "3. density_type 必须来自视觉感知，取多切片多数票，solid/mGGN 优先于 pGGN\n"
        "4. 结节大小取各切片描述的最大测量值\n"
        "5. 位置使用患者解剖方向（患者左肺/右肺）\n"
        "\n"
        "【其他异常整合规则】\n"
        "1. 合并多切片中描述的相同位置/类型异常（如：多切片均见右侧积液 → 合并为一条）\n"
        "2. 正常表现不要列入 other_findings\n"
        "3. 描述要综合多切片的信息，给出更全面的评估\n"
        "4. 若所有切片均无非结节异常，other_findings 返回空列表 []\n"
        "\n"
        "仅输出 JSON 对象，包含 nodules 和 other_findings 两个数组，无 markdown 包裹。"
    )

    perception_text = json.dumps(
        [p.model_dump() for p in perceptions],
        ensure_ascii=False,
        indent=2,
    )

    algo_count = len(nodule_desc.get("nodules", []))
    algo_max_diam = max(
        (nd.get("estimated_diameter_mm", 0) for nd in nodule_desc.get("nodules", [])),
        default=0
    )

    user_msg = (
        f"各切片视觉感知结果（共{len(perceptions)}张切片）：\n{perception_text}\n\n"
        f"备注：算法共检测到 {algo_count} 个候选区域，最大直径约 {algo_max_diam:.1f}mm（仅供数量参考，密度类型以视觉感知为准）。\n\n"
        "请输出包含以下两个数组的 JSON 对象：\n"
        '{\n'
        '  "nodules": [\n'
        '    {\n'
        '      "integrated_nodule_id": "N1",\n'
        '      "best_slice_rank": 2,\n'
        '      "cross_slice_consistency": "高",\n'
        '      "estimated_3d_size": "约16x12mm",\n'
        '      "location_description": "右肺下叶（患者解剖方向）",\n'
        '      "density_type": "solid（必须来自视觉感知，禁止参考算法候选）",\n'
        '      "algo_density_type": null\n'
        '    }\n'
        '  ],\n'
        '  "other_findings": [\n'
        '    {\n'
        '      "finding_id": "F1",\n'
        '      "category": "胸腔积液",\n'
        '      "location": "右侧胸腔",\n'
        '      "description": "右侧胸腔少量积液，多切片可见，液体深度约15mm",\n'
        '      "severity": "少量",\n'
        '      "supporting_slices": [2, 3, 4],\n'
        '      "measurements": {"积液深度": "约15mm"}\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "【强制要求】nodules 中 density_type 必须来自视觉感知，从[pGGN, mGGN, solid, 微小结节, unknown]选一个。"
    )

    logger.info(f"[Stage6-Step2] 发送请求  感知数={len(perceptions)}  prompt长度={len(user_msg)}")
    try:
        raw, _, tokens = await llm_svc.complete(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            model=model,
            response_format="json_object",
            temperature=0.1,
            provider=provider,
        )
        logger.info(f"[Stage6-Step2] LLM原始返回: {raw[:600]}")

        from app.utils.llm_parser import extract_json_from_llm
        obj = extract_json_from_llm(raw)

        # 解析结节
        nodule_data = []
        if isinstance(obj, dict):
            nodule_data = obj.get("nodules", []) or next(
                (v for v in obj.values() if isinstance(v, list) and
                 v and isinstance(v[0], dict) and "density_type" in v[0]),
                []
            )
        elif isinstance(obj, list):
            nodule_data = obj

        nodule_integrations = []
        for item in (nodule_data if isinstance(nodule_data, list) else []):
            try:
                nodule_integrations.append(NoduleIntegration(**item))
            except Exception:
                pass

        # 解析其他异常
        other_data = []
        if isinstance(obj, dict):
            other_data = obj.get("other_findings", [])

        other_integrations = []
        for item in (other_data if isinstance(other_data, list) else []):
            try:
                other_integrations.append(OtherFindingIntegration(**item))
            except Exception as e:
                logger.debug(f"[Stage6-Step2] other_finding 解析失败: {e}, item={item}")

        if not nodule_integrations:
            logger.warning(f"[Stage6-Step2] 结节整合列表为空，原始内容: {raw[:300]}")

        return nodule_integrations, other_integrations, tokens

    except Exception as e:
        logger.warning(f"[Stage6-Step2] 整合失败: {e}")
        return [], [], 0


# ── Step 3：生成全面肺部报告 ─────────────────────────────────────

async def _step3_generate_report(
    task_id: str,
    step1: List[SlicePerception],
    step2_nodules: List[NoduleIntegration],
    step2_other: List[OtherFindingIntegration],
    payload: LLMPayload,
    model: str,
    provider: str = "",
) -> tuple[AnalysisReport, int]:
    """生成全面肺部 CT 报告，包含结节 Lung-RADS 和所有其他肺部发现"""

    SYSTEM = (
        "你是一名资深胸部影像科医生，专长肺部CT全面分析与Lung-RADS分级。\n"
        "请基于整合结果，生成完整的肺部CT分析报告，涵盖结节评估和所有其他肺部异常。\n\n"

        "【Lung-RADS 2022 分级规则——必须严格遵守】\n\n"
        "▌实性结节（solid）：\n"
        "  直径 < 6mm                  → Lung-RADS 2\n"
        "  直径 6mm ~ < 8mm            → Lung-RADS 3（6个月CT随访）\n"
        "  直径 8mm ~ < 15mm           → Lung-RADS 4A（3个月CT随访或PET-CT）\n"
        "  直径 ≥ 15mm 或有毛刺/分叶   → Lung-RADS 4B（活检或手术）\n\n"
        "▌纯磨玻璃结节（pGGN）：\n"
        "  直径 < 6mm                  → Lung-RADS 2\n"
        "  直径 6mm ~ 20mm             → Lung-RADS 3（6个月CT随访）\n"
        "  直径 > 20mm                 → Lung-RADS 4A\n\n"
        "▌混合磨玻璃结节（mGGN）：\n"
        "  直径 < 6mm                  → Lung-RADS 2\n"
        "  直径 ≥ 6mm，实性成分 < 6mm  → Lung-RADS 4A\n"
        "  实性成分 ≥ 6mm              → Lung-RADS 4B\n\n"
        "▌微小结节（< 6mm）           → Lung-RADS 2（无论密度类型）\n\n"

        "【报告生成规则】\n"
        "1. 结节：density_type 和大小严格使用整合结节清单中的值，不得修改\n"
        "2. 其他异常：使用 other_findings 清单中的描述，忠实呈现\n"
        "3. findings 数组：每条影像发现一个元素，结节和非结节发现都要列出\n"
        "4. impression：按临床重要性（Lung-RADS 高→低）总结所有阳性发现\n"
        "5. overall_lung_rads：取所有结节中最高的 Lung-RADS 等级\n"
        "6. 若结节清单为空，nodule_assessment 为空数组，overall_lung_rads 为 '1'\n"
        "7. pulmonary_findings 数组：将 other_findings 转化为标准化条目\n\n"

        "必须以 JSON 格式输出全部字段，不要遗漏 disclaimer 字段，不要 markdown 包裹。"
    )

    REPORT_SCHEMA = """{
  "findings": [
    "<影像发现1：如结节描述>",
    "<影像发现2：如积液描述>",
    "<影像发现3：如气肿描述>"
  ],
  "impression": "<总体印象：按Lung-RADS从高到低列出结节，再列出其他重要发现，2-5句话>",
  "nodule_assessment": [
    {
      "nodule_id": "N1",
      "location": "右肺下叶（患者解剖方向）",
      "size_mm": "16x12mm",
      "lung_rads_grade": "4B",
      "morphology": "类圆形/不规则/有毛刺",
      "density_type": "实性结节(solid)",
      "malignancy_risk": "高",
      "follow_up": "建议活检或手术切除"
    }
  ],
  "pulmonary_findings": [
    {
      "finding_id": "F1",
      "category": "胸腔积液",
      "location": "右侧胸腔",
      "description": "右侧胸腔少量积液，液体深度约15mm",
      "severity": "少量",
      "measurements": {"积液深度": "约15mm"},
      "clinical_significance": "建议结合临床症状评估",
      "follow_up": "观察随访，必要时穿刺引流"
    }
  ],
  "overall_lung_rads": "4B",
  "recommendations": ["<建议1>", "<建议2>"],
  "confidence": 0.85,
  "limitations": ["AI分析结果存在局限性，需结合临床信息综合判断"],
  "disclaimer": "本报告由AI辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
}"""

    valid_step1 = [
        p for p in step1
        if p.visual_description and "fallback" not in p.visual_description and "失败" not in p.visual_description
    ]
    all_fallback = len(valid_step1) == 0

    if all_fallback and not step2_nodules and not step2_other:
        # Step1 和 Step2 均无有效数据，算法坐标不可作为诊断依据，直接终止
        # （正常情况下 run_stage6 已在 Step1 后 raise，此处作为双重保险）
        logger.error(f"[Stage6-Step3] Step1 全部 fallback 且 Step2 无有效数据，拒绝用算法坐标生成报告")
        raise RuntimeError(
            "CT图像感知全部失败且整合无有效结果，"
            "拒绝用算法估计坐标生成报告（会产生误导性的高危结论）。"
        )
    elif all_fallback and (step2_nodules or step2_other):
        context_text = (
            f"【注意】CT图像感知未能正常完成，基于整合数据生成报告。\n"
            f"整合结节：{json.dumps([n.model_dump() for n in step2_nodules], ensure_ascii=False)}\n"
            f"其他异常：{json.dumps([f.model_dump() for f in step2_other], ensure_ascii=False)}\n"
        )
        logger.warning(f"[Stage6-Step3] Step1 全部 fallback，基于整合数据生成报告")
    else:
        # 正常流程：完全基于视觉感知 + 整合结果
        context_text = (
            f"【Step2 整合结节清单（密度类型来自视觉感知，请严格使用）】：\n"
            f"{json.dumps([n.model_dump() for n in step2_nodules], ensure_ascii=False, indent=2)}\n\n"
            f"【Step2 整合其他异常清单】：\n"
            f"{json.dumps([f.model_dump() for f in step2_other], ensure_ascii=False, indent=2)}\n\n"
            f"【Step1 视觉感知摘要（前5张切片）】：\n"
            f"{json.dumps([p.visual_description for p in step1[:5]], ensure_ascii=False)}\n\n"
            f"【密度类型映射】：pGGN=纯磨玻璃结节，mGGN=混合磨玻璃结节，solid=实性结节\n"
            f"【位置方向】：所有位置均使用患者解剖方向（患者左肺/右肺）"
        )

    user_msg = (
        f"{context_text}\n\n"
        f"请按以下 JSON 格式输出完整肺部CT报告（必须包含 disclaimer 和 pulmonary_findings 字段）：\n"
        f"{REPORT_SCHEMA}"
    )

    logger.info(f"[Stage6-Step3] 发送请求  结节数={len(step2_nodules)}  其他异常={len(step2_other)}  prompt长度={len(user_msg)}")
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
            provider=provider,
        )
        logger.info(f"[Stage6-Step3] LLM原始返回({used_model}) tokens={tokens}: {raw[:800]}")
    except Exception as e:
        logger.error(f"[Stage6-Step3] 报告生成失败: {e}")
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

    if not report.disclaimer or not report.disclaimer.strip():
        report.disclaimer = "本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"

    return report, tokens


# ── 候选位置提示格式化（仅位置，无密度）─────────────────────────

def _format_location_only_hints(slice_data: dict) -> str:
    """
    将切片的结节候选信息格式化为位置坐标提示。
    仅提供位置引导，不传递任何密度类型判断。

    CT 影像学方向：图像左侧(bbox_x<0.5) = 患者右肺，图像右侧(bbox_x>0.5) = 患者左肺。
    """
    candidates = slice_data.get("nodule_candidates", [])
    if not candidates:
        return "（该切片无算法检测候选，请全面观察整个肺野）"

    hints = []
    for i, c in enumerate(candidates, 1):
        diam       = c.get("estimated_diameter_mm", 0)
        confidence = c.get("confidence", 0)
        bbox_x     = c.get("bbox_x", 0.5)
        bbox_y     = c.get("bbox_y", 0.5)

        # CT 影像学惯例：图像坐标左侧 = 患者右肺
        side_label = "患者右肺（图像左侧）" if bbox_x < 0.5 else "患者左肺（图像右侧）"

        if bbox_y < 0.35:
            lobe_label = "上叶区域"
        elif bbox_y > 0.65:
            lobe_label = "下叶区域"
        else:
            lobe_label = "中叶/舌段区域"

        hints.append(
            f"  候选{i}: {side_label} {lobe_label}，"
            f"算法估计直径约{diam:.1f}mm，置信度{confidence:.2f}"
            f"（图像坐标 x≈{bbox_x:.2f}, y≈{bbox_y:.2f}，密度类型请自行基于图像判断）"
        )

    return "\n".join(hints)


# ── 图像编码 ─────────────────────────────────────────────────────

def _encode_image_b64(image_path: str) -> str:
    """将 PNG 转 base64，文件不存在或读取失败返回空字符串"""
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.debug(f"[Stage6] 图像编码失败 {image_path}: {e}")
        return ""
