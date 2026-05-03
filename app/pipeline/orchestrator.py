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
        # 配额检查
        await _check_quota(user_id)

        report, cot_snapshot = await run_stage6(task_id, stage5, model)
        _save_stage_result(task_id, "stage6", cot_snapshot)
        _update_task(task_id, stage="stage6", progress=STAGE_PROGRESS["stage6"])

        # Stage6 完全失败（全部 fallback）则标记为 error，不继续落库
        is_llm_failed = (
            report.confidence == 0.0
            and report.findings
            and any("失败" in f or "不可用" in f for f in report.findings)
        )
        if is_llm_failed:
            _update_task(
                task_id,
                status="error",
                stage="stage6",
                error_message="LLM 分析失败：所有模型均返回 fallback，请重新提交任务",
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
    """任务前检查 Token 配额（估算 10,000 tokens/次）"""
    try:
        from app.services.quota_service import QuotaService, QuotaExceededError
        qs = QuotaService()
        await qs.check_and_consume(user_id, estimated_tokens=10_000)
    except Exception as e:
        # quota 检查失败不阻塞任务（Redis 不可用时降级放行）
        logger.warning(f"[Pipeline] 配额检查跳过: {e}")


async def _cleanup_intermediates(task_id: str):
    """异步清理中间产物（DICOM 原始文件 + TotalSegmentator 输出）"""
    await asyncio.sleep(10)   # 等待落库完成
    try:
        from app.services.file_service import DiskGuard
        dg = DiskGuard()
        await run_in_thread(dg.clean_processed_files, task_id)
    except Exception as e:
        logger.debug(f"[Pipeline] 清理失败（非致命）: {e}")
