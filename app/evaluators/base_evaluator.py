"""评估器基类 + 结果模型"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Any


class EvalStatus(str, Enum):
    PASS  = "pass"
    WARN  = "warn"
    ERROR = "error"


@dataclass
class EvalResult:
    status:     EvalStatus
    score:      float           # 0.0 ~ 1.0
    issues:     List[str] = field(default_factory=list)
    metadata:   dict     = field(default_factory=dict)


class BaseEvaluator:
    """
    所有评估器的基类。
    子类实现 evaluate() 方法，返回 EvalResult。
    """

    def evaluate(self, data: Any) -> EvalResult:
        raise NotImplementedError

    def _ok(self, score: float = 1.0, **meta) -> EvalResult:
        return EvalResult(EvalStatus.PASS, score, metadata=meta)

    def _warn(self, issues: List[str], score: float = 0.6, **meta) -> EvalResult:
        return EvalResult(EvalStatus.WARN, score, issues=issues, metadata=meta)

    def _error(self, issues: List[str], score: float = 0.0, **meta) -> EvalResult:
        return EvalResult(EvalStatus.ERROR, score, issues=issues, metadata=meta)
