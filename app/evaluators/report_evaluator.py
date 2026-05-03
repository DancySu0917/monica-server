"""
Stage6/Stage7 报告评估器

评估 LLM 生成报告的完整性和合规性：
- impression 非空
- findings 至少 1 条
- disclaimer 存在
- confidence 在合理范围
"""
from typing import List
from app.evaluators.base_evaluator import BaseEvaluator, EvalResult
from app.schemas.stage7_report import AnalysisReport


class ReportEvaluator(BaseEvaluator):

    def evaluate(self, report: AnalysisReport) -> EvalResult:
        issues: List[str] = []
        score = 1.0

        if not report.impression or not report.impression.strip():
            issues.append("缺少 impression（主要印象）")
            score -= 0.3

        if not report.findings:
            issues.append("findings 为空，报告无实质内容")
            score -= 0.3

        if not report.disclaimer or not report.disclaimer.strip():
            issues.append("缺少免责声明 disclaimer（合规必填）")
            score -= 0.4   # 合规必须项，扣分更重

        if not (0.0 <= report.confidence <= 1.0):
            issues.append(f"confidence 值异常: {report.confidence}")
            score -= 0.1

        score = max(0.0, min(1.0, score))

        meta = {
            "model_used":    report.model_used,
            "findings_count": len(report.findings),
            "confidence":    report.confidence,
        }

        if "disclaimer" in "".join(issues) and score < 0.6:
            return self._error(issues, score, **meta)
        elif issues:
            return self._warn(issues, score, **meta)
        return self._ok(score, **meta)
