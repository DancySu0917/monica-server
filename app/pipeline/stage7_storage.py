"""
Stage 7: 结果落库

- 将 AnalysisReport + CoTSnapshot 持久化到数据库
- 更新 Task 状态为 done
- 触发 DiskGuard 清理中间产物
"""
import json
import logging
import time
import uuid
from pathlib import Path

from app.database import SessionLocal
from app.models.analysis_result import AnalysisResult
from app.models.task import Task
from app.schemas.stage6_cot import CoTIntermediateResult
from app.schemas.stage7_report import AnalysisReport
from app.evaluators.report_evaluator import ReportEvaluator

logger    = logging.getLogger(__name__)
evaluator = ReportEvaluator()


def run_stage7(
    task_id: str,
    user_id: str,
    report: AnalysisReport,
    cot_snapshot: CoTIntermediateResult,
) -> str:
    """将分析结果落库，返回 result_id"""
    start = time.time()

    # 评估报告质量
    eval_result = evaluator.evaluate(report)

    result_id = str(uuid.uuid4())
    findings_json           = json.dumps(report.findings,          ensure_ascii=False)
    nodule_assessment_json  = json.dumps(
        [n.model_dump() for n in report.nodule_assessment],
        ensure_ascii=False
    )
    pulmonary_findings_json = json.dumps(
        [f.model_dump() for f in report.pulmonary_findings],
        ensure_ascii=False
    )
    recommendations_json  = json.dumps(report.recommendations,   ensure_ascii=False)
    limitations_json      = json.dumps(report.limitations,       ensure_ascii=False)
    cot_snapshot_json     = cot_snapshot.model_dump_json()
    eval_scores_json      = json.dumps({
        "status": eval_result.status.value,
        "score":  eval_result.score,
        "issues": eval_result.issues,
    }, ensure_ascii=False)

    result = AnalysisResult(
        id=result_id,
        task_id=task_id,
        user_id=user_id,
        findings=findings_json,
        impression=report.impression,
        nodule_assessment=nodule_assessment_json,
        pulmonary_findings=pulmonary_findings_json,
        overall_lung_rads=report.overall_lung_rads or "",
        recommendations=recommendations_json,
        confidence=report.confidence,
        limitations=limitations_json,
        disclaimer=report.disclaimer,
        cot_snapshot=cot_snapshot_json,
        raw_response=report.raw_response,
        llm_model=report.model_used,
        tokens_step1=cot_snapshot.step1_tokens,
        tokens_step2=cot_snapshot.step2_tokens,
        tokens_step3=cot_snapshot.step3_tokens,
        eval_scores=eval_scores_json,
    )

    with SessionLocal() as db:
        db.add(result)
        db.commit()

    logger.info(
        f"[Stage7] 结果落库完成: task_id={task_id}, "
        f"eval={eval_result.status.value}, score={eval_result.score:.2f}, "
        f"elapsed={int((time.time() - start) * 1000)}ms"
    )
    return result_id
