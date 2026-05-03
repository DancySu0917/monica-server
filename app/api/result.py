"""
结果查询 API

GET /result/{task_id}         → 完整分析报告
GET /result/{task_id}/slices  → 已选切片 PNG 路径列表
"""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user
from app.database import SessionLocal
from app.models.analysis_result import AnalysisResult
from app.models.stage_result import StageResult
from app.models.task import Task

router = APIRouter(prefix="/result", tags=["Result"])
logger = logging.getLogger(__name__)


@router.get("/{task_id}", summary="获取完整分析报告")
async def get_result(
    task_id: str,
    user: dict = Depends(get_current_user),
):
    # 验证任务归属
    with SessionLocal() as db:
        task = db.query(Task).filter_by(
            task_id=task_id,
            user_id=user["user_id"]
        ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in ("done",):
        raise HTTPException(
            status_code=409,
            detail=f"任务尚未完成（当前状态：{task.status}）",
        )

    with SessionLocal() as db:
        result = db.query(AnalysisResult).filter_by(task_id=task_id).first()
    if not result:
        raise HTTPException(status_code=404, detail="分析结果不存在")

    def _parse(field) -> object:
        if not field:
            return None
        if isinstance(field, str):
            try:
                return json.loads(field)
            except Exception:
                return field
        return field

    return {
        "task_id":           task_id,
        "version":           result.version,
        "findings":          _parse(result.findings) or [],
        "impression":        result.impression or "",
        "nodule_assessment": _parse(result.nodule_assessment) or [],
        "recommendations":   _parse(result.recommendations) or [],
        "confidence":        result.confidence or 0.0,
        "limitations":       _parse(result.limitations) or [],
        "disclaimer":        result.disclaimer,
        "llm_model":         result.llm_model,
        "created_at":        result.created_at.isoformat() if result.created_at else None,
        "eval_scores":       _parse(result.eval_scores),
        "tokens": {
            "step1": result.tokens_step1,
            "step2": result.tokens_step2,
            "step3": result.tokens_step3,
            "total": (result.tokens_step1 or 0) +
                     (result.tokens_step2 or 0) +
                     (result.tokens_step3 or 0),
        },
    }


@router.get("/{task_id}/slices", summary="获取已选切片 PNG 路径列表")
async def get_selected_slices(
    task_id: str,
    user: dict = Depends(get_current_user),
):
    with SessionLocal() as db:
        task = db.query(Task).filter_by(
            task_id=task_id,
            user_id=user["user_id"]
        ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 从 Stage4 StageResult 中读取已选切片
    with SessionLocal() as db:
        sr = db.query(StageResult).filter_by(
            task_id=task_id,
            stage="stage4"
        ).first()

    if not sr or not sr.output_json:
        raise HTTPException(
            status_code=404,
            detail="切片信息不存在（任务可能未完成 Stage4）"
        )

    try:
        stage4_data = json.loads(sr.output_json)
        selected    = stage4_data.get("selected_slices", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切片数据解析失败: {e}")

    slices = []
    for s in selected:
        dw = s.get("dual_window", {})
        slices.append({
            "rank":                    s.get("rank"),
            "slice_index":             s.get("slice_index"),
            "score":                   s.get("score"),
            "selection_reason":        s.get("selection_reason"),
            "lung_window_path":        dw.get("lung_window_path"),
            "mediastinum_window_path": dw.get("mediastinum_window_path"),
        })

    return {"task_id": task_id, "slices": slices}


@router.get("/{task_id}/stages", summary="获取各阶段中间产物（调试/审计用）")
async def get_stage_results(
    task_id: str,
    user: dict = Depends(get_current_user),
):
    with SessionLocal() as db:
        task = db.query(Task).filter_by(
            task_id=task_id,
            user_id=user["user_id"]
        ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    with SessionLocal() as db:
        stages = db.query(StageResult).filter_by(task_id=task_id).all()

    return {
        "task_id": task_id,
        "stages": [
            {
                "stage":       s.stage,
                "status":      s.status,
                "elapsed_ms":  s.elapsed_ms,
                "error":       s.error_message,
                "created_at":  s.created_at.isoformat() if s.created_at else None,
            }
            for s in sorted(stages, key=lambda x: x.stage)
        ],
    }
