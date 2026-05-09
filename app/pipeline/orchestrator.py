"""
Pipeline 编排器

驱动 Stage1 → Stage7，每阶段：
  1. 更新 Task 进度（供 SSE 推送）
  2. 执行阶段逻辑（CPU 密集阶段通过 run_in_thread 卸载）
  3. 持久化 StageResult（供审计/A-B 测试）
  4. 若被拒则提前终止

异常处理：
  - 阶段 error 不立即终止，记录后继续（degraded 模式）
  - 关键阶段（Stage1/Stage2/Stage6）失败才标 task 失败
"""
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.models.stage_result import StageResult
from app.models.task import Task
from app.utils.thread_pool import run_in_thread

logger = logging.getLogger(__name__)

# Stage6 失败判定关键词（提取为模块常量，避免每次调用重新创建）
_STAGE6_FAIL_KEYWORDS = ("失败", "不可用", "感知失败", "fallback", "无法生成")

# 阶段进度百分比
STAGE_PROGRESS = {
    "stage1": 10,
    "stage2": 20,
    "stage3": 40,
    "stage4": 55,
    "stage5": 65,
    "stage6": 90,
    "stage7": 100,
}


async def run_pipeline(
    task_id:        str,
    user_id:        str,
    file_path:      str,
    scan_type:      str = "CT",
    clinical_notes: str = "",
    model:          str = "",
    provider:       str = "",
    use_llm_cache:  bool = True,
) -> None:
    """
    主入口：ARQ Worker 调用此函数。
    整个 Pipeline 在单线程中顺序执行（1 个 ARQ Job 占用 1 个线程）。
    """
    logger.info(f"[Pipeline] 开始: task_id={task_id}, file={file_path}")
    _update_task(task_id, status="processing", stage="stage1", progress=5)

    try:
        # Stage 1: 标准化
        from app.pipeline.stage1_normalizer import run_stage1
        stage1 = await run_in_thread(run_stage1, task_id, file_path)
        _save_stage_result(task_id, "stage1", stage1)
        _update_task(task_id, stage="stage1", progress=STAGE_PROGRESS["stage1"])

        # Stage 2: 质量筛查
        from app.pipeline.stage2_screener import run_stage2
        stage2 = await run_in_thread(run_stage2, task_id, stage1)
        _save_stage_result(task_id, "stage2", stage2)
        _update_task(task_id, stage="stage2", progress=STAGE_PROGRESS["stage2"])

        # 质量不足则拒绝
        if not stage2.passed:
            issues  = "; ".join(stage2.quality_issues) if stage2.quality_issues else "影像质量不合格"
            suggestions = ["请提供完整的 CT 序列（至少 5 张切片）", "检查上传文件是否正确"]
            _update_task(
                task_id,
                status="rejected",
                reject_reason=f"影像质量不合格: {issues}",
                suggestions=json.dumps(suggestions, ensure_ascii=False),
            )
            logger.warning(f"[Pipeline] 任务拒绝: {task_id} — {issues}")
            return

        # Stage 3: 结节检测
        from app.pipeline.stage3_detector import run_stage3
        stage3 = await run_in_thread(run_stage3, task_id, stage2)
        _save_stage_result(task_id, "stage3", stage3)
        _update_task(task_id, stage="stage3", progress=STAGE_PROGRESS["stage3"])

        # Stage 4: 切片选择 + 双窗位渲染
        from app.pipeline.stage4_selector import run_stage4
        stage4 = await run_in_thread(run_stage4, task_id, stage3)
        _save_stage_result(task_id, "stage4", stage4)
        _update_task(task_id, stage="stage4", progress=STAGE_PROGRESS["stage4"])

        # Stage 5: 知识库检索
        from app.pipeline.stage5_context import run_stage5
        stage5 = await run_in_thread(
            run_stage5, task_id, stage3, stage4, clinical_notes
        )
        _save_stage_result(task_id, "stage5", stage5)
        _update_task(task_id, stage="stage5", progress=STAGE_PROGRESS["stage5"])

        # Stage 6: CoT 三步推理（async，直接 await）
        _update_task(task_id, stage="stage6", progress=STAGE_PROGRESS["stage5"] + 5)
        from app.pipeline.stage6_llm import run_stage6

        # 提取 file_hash（upload 时已将文件命名为 SHA-256）
        file_hash = _extract_file_hash(file_path)
        model_key = model or settings.DEFAULT_MODEL

        # ① 查 LLM 缓存
        cached = None
        if use_llm_cache and file_hash:
            cached = await _load_llm_cache(file_hash, scan_type, model_key)

        if cached is not None:
            # ② 缓存命中：跳过 Stage6 LLM 调用，直接复用结果
            report, cot_snapshot = cached
            logger.info(
                f"[Pipeline] LLM 缓存命中，跳过 Stage6: "
                f"task_id={task_id}  file_hash={file_hash[:12]}…  model={model_key}"
            )
            _save_stage_result(task_id, "stage6", cot_snapshot)
            _update_task(task_id, stage="stage6", progress=STAGE_PROGRESS["stage6"])
            # 缓存命中不消耗 Token，无需配额操作
        else:
            # ③ 未命中：正常调用 LLM（预估配额检查 + 实际修正）
            await _check_quota(user_id)

            report, cot_snapshot = await run_stage6(task_id, stage5, model, provider)
            _save_stage_result(task_id, "stage6", cot_snapshot)
            _update_task(task_id, stage="stage6", progress=STAGE_PROGRESS["stage6"])

            # 用实际消耗的 Token 数修正配额（原预估小于实际消耗）
            actual_tokens = (
                (cot_snapshot.step1_tokens or 0)
                + (cot_snapshot.step2_tokens or 0)
                + (cot_snapshot.step3_tokens or 0)
            )
            await _adjust_quota(user_id, estimated_tokens=10_000, actual_tokens=actual_tokens)

            # ④ 写入缓存（开关开启且有 file_hash）
            if use_llm_cache and file_hash:
                await _save_llm_cache(file_hash, scan_type, model_key, report, cot_snapshot)

        # Stage6 完全失败（全部 fallback / confidence=0）则标记为 error，不落库
        # 注意：缓存命中时不会走到这里产生失败（缓存内容是之前成功的结果）
        is_llm_failed = (
            report.confidence == 0.0
            and report.findings
            and any(kw in f for f in report.findings for kw in _STAGE6_FAIL_KEYWORDS)
        )
        if is_llm_failed:
            _update_task(
                task_id,
                status="error",
                stage="stage6",
                error_message=(
                    "LLM 分析失败：CT图像感知均返回 fallback，无法生成可靠报告。"
                    "请确认已配置支持多模态的 LLM 后端（gemini 系列 / gpt-4o），或充值 BAI API Key。"
                ),
            )
            logger.error(f"[Pipeline] Stage6 LLM 全部失败，任务标记为 error: {task_id}")
            return

        # Stage 7: 落库
        from app.pipeline.stage7_storage import run_stage7
        result_id = await run_in_thread(
            run_stage7, task_id, user_id, report, cot_snapshot
        )
        _update_task(
            task_id,
            status="done",
            stage="stage7",
            progress=STAGE_PROGRESS["stage7"],
        )

        # 清理中间产物（异步后台，不阻塞完成通知）
        asyncio.create_task(_cleanup_intermediates(task_id))

        logger.info(f"[Pipeline] 完成: task_id={task_id}, result_id={result_id}")

    except Exception as e:
        logger.exception(f"[Pipeline] 任务失败: task_id={task_id}, error={e}")
        _update_task(task_id, status="error", error_message=str(e))


# ── 辅助函数 ──────────────────────────────────────────────────────

def _update_task(task_id: str, **kwargs):
    with SessionLocal() as db:
        task = db.query(Task).filter_by(task_id=task_id).first()
        if not task:
            return
        for k, v in kwargs.items():
            setattr(task, k, v)
        db.commit()


def _save_stage_result(task_id: str, stage: str, output_obj):
    """持久化阶段产物（JSON 序列化）"""
    try:
        if hasattr(output_obj, "model_dump_json"):
            output_json = output_obj.model_dump_json()
        else:
            output_json = json.dumps(
                output_obj if isinstance(output_obj, dict) else str(output_obj),
                ensure_ascii=False,
            )
    except Exception as e:
        output_json = json.dumps({"error": str(e)})

    sr = StageResult(
        id=f"{task_id}_{stage}",
        task_id=task_id,
        stage=stage,
        status="pass",
        output_json=output_json,
    )
    with SessionLocal() as db:
        db.merge(sr)
        db.commit()


async def _check_quota(user_id: str):
    """任务前预估配额检查（估算 10,000 tokens/次）"""
    try:
        from app.services.quota_service import QuotaService
        qs = QuotaService()
        await qs.check_and_consume(user_id, estimated_tokens=10_000)
    except Exception as e:
        # quota 检查失败不阻塞任务（Redis 不可用时降级放行）
        logger.warning(f"[Pipeline] 配额检查跳过: {e}")


async def _adjust_quota(user_id: str, estimated_tokens: int, actual_tokens: int):
    """
    Stage6 完成后，用实际 Token 消耗修正配额。
    如果实际 > 预估，补扣差额；如果实际 < 预估，退还多扣的配额。
    调用失败时静默处理（Redis 不可用时降级放行）。
    """
    try:
        from app.services.quota_service import QuotaService
        qs = QuotaService()
        await qs.adjust(user_id, estimated_tokens=estimated_tokens, actual_tokens=actual_tokens)
        diff = actual_tokens - estimated_tokens
        logger.info(
            f"[Pipeline] 配额修正: user={user_id} 预估={estimated_tokens} "
            f"实际={actual_tokens} diff={diff:+d}"
        )
    except Exception as e:
        logger.warning(f"[Pipeline] 配额修正失败（非致命）: {e}")


async def _cleanup_intermediates(task_id: str):
    """异步清理中间产物（DICOM 原始文件 + TotalSegmentator 输出）"""
    await asyncio.sleep(10)   # 等待落库完成
    try:
        from app.services.file_service import DiskGuard
        dg = DiskGuard()
        await run_in_thread(dg.clean_processed_files, task_id)
    except Exception as e:
        logger.debug(f"[Pipeline] 清理失败（非致命）: {e}")


# ── LLM 缓存辅助 ──────────────────────────────────────────────────

def _extract_file_hash(file_path: str) -> str:
    """
    从文件路径中提取 file_hash（SHA-256）。
    upload 完成时文件以其 SHA-256 为文件名存储，例如：
        .../uploads/user_id/<sha256>.zip
    返回文件名（不含后缀），失败时返回空字符串。
    """
    try:
        stem = Path(file_path).stem
        # SHA-256 = 64 位 hex
        if len(stem) == 64 and all(c in "0123456789abcdefABCDEF" for c in stem):
            return stem.lower()
    except Exception:
        pass
    return ""


async def _load_llm_cache(
    file_hash: str,
    scan_type: str,
    model: str,
) -> tuple | None:
    """
    从 Redis 读取 LLM 缓存，返回 (AnalysisReport, CoTIntermediateResult) 或 None。
    Redis 不可用时静默降级，返回 None 让主流程正常调用 LLM。
    """
    try:
        from app.services.llm_cache_service import LLMCacheService
        from app.schemas.stage7_report import AnalysisReport
        from app.schemas.stage6_cot import CoTIntermediateResult

        cache_svc = LLMCacheService()
        hit = await cache_svc.get(file_hash, scan_type, model)
        if hit is None:
            return None
        report_dict, cot_dict = hit
        report       = AnalysisReport.model_validate(report_dict)
        cot_snapshot = CoTIntermediateResult.model_validate(cot_dict)
        return report, cot_snapshot
    except Exception as e:
        logger.warning(f"[Pipeline] LLM 缓存读取失败（降级继续）: {e}")
        return None


async def _save_llm_cache(
    file_hash: str,
    scan_type: str,
    model: str,
    report,
    cot_snapshot,
) -> None:
    """
    将 LLM 推理结果写入 Redis 缓存。
    Redis 不可用或序列化失败时静默处理，不影响主流程。
    """
    try:
        from app.services.llm_cache_service import LLMCacheService
        cache_svc = LLMCacheService()
        await cache_svc.set(
            file_hash=file_hash,
            scan_type=scan_type,
            model=model,
            report_dict=report.model_dump(),
            cot_dict=cot_snapshot.model_dump(),
        )
    except Exception as e:
        logger.warning(f"[Pipeline] LLM 缓存写入失败（非致命）: {e}")
