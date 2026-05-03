"""
Stage3 结节评估器

评估 TotalSegmentator 结节候选的置信度和可信度：
- 置信度加权分
- 候选数量合理性检查
"""
from typing import List
from app.evaluators.base_evaluator import BaseEvaluator, EvalResult
from app.schemas.stage3_detection import Stage3Result


class NoduleEvaluator(BaseEvaluator):

    def evaluate(self, stage3: Stage3Result) -> EvalResult:
        if not stage3.has_nodule_candidates:
            return self._ok(
                score=1.0,
                message="未检出结节候选，无需进一步评估",
                total_candidates=0,
            )

        candidates  = stage3.candidates
        n           = len(candidates)
        issues: List[str] = []
        score = 1.0

        # 候选数量异常（>50 往往是假阳性爆炸）
        if n > 50:
            issues.append(f"结节候选过多（{n}），疑似假阳性，建议人工复核")
            score -= 0.3

        # 低置信度候选比例
        low_conf = [c for c in candidates if c.confidence < 0.3]
        if len(low_conf) > n * 0.5:
            issues.append(
                f"超过 50% 的候选置信度 < 0.3（共 {len(low_conf)}/{n}）"
            )
            score -= 0.2

        # 平均置信度
        avg_conf = sum(c.confidence for c in candidates) / n
        meta = {
            "total_candidates": n,
            "avg_confidence":   round(avg_conf, 3),
            "low_conf_count":   len(low_conf),
        }

        score = max(0.0, min(1.0, score))
        if score < 0.4:
            return self._warn(issues, score, **meta)
        return self._ok(score, **meta)
