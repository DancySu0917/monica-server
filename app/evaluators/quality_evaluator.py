"""
Stage2 影像质量评估器

检查 DICOM 序列是否满足分析条件：
- 切片数量是否达到最小要求
- 是否为 CT 模态
- 序列是否完整（缺口检测）
"""
from typing import List
from app.evaluators.base_evaluator import BaseEvaluator, EvalResult
from app.schemas.stage2_screen import Stage2Result

MIN_SLICES = 5   # 至少 5 张切片才有分析价值


class QualityEvaluator(BaseEvaluator):

    def evaluate(self, stage2: Stage2Result) -> EvalResult:
        issues: List[str] = []
        score  = 1.0

        if stage2.slice_count < MIN_SLICES:
            issues.append(
                f"切片数不足（{stage2.slice_count} < {MIN_SLICES}），无法分析"
            )
            score -= 0.4

        if stage2.modality.upper() not in ("CT", "MR", "MRI", "PT", "PET"):
            issues.append(
                f"不支持的影像模态：{stage2.modality}，目前仅支持 CT/MRI/PET"
            )
            score -= 0.3

        if stage2.quality_issues:
            issues.extend(stage2.quality_issues)
            score -= 0.1 * len(stage2.quality_issues)

        score = max(0.0, min(1.0, score))

        if score < 0.3 or not stage2.passed:
            return self._error(
                issues=issues,
                score=score,
                slice_count=stage2.slice_count,
                modality=stage2.modality,
            )
        elif issues:
            return self._warn(
                issues=issues,
                score=score,
                slice_count=stage2.slice_count,
            )
        return self._ok(
            score=score,
            slice_count=stage2.slice_count,
            modality=stage2.modality,
        )
