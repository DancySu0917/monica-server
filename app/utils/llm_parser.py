"""
LLM 响应健壮解析工具。

即使设置了 response_format: json_object，LLM 仍可能返回：
- JSON 前后包裹 Markdown 代码块：```json\\n{...}\\n```
- 字段值含截断省略号
- 数字字段返回字符串："confidence": "0.85"
- 整合失败时返回空对象：{}
"""
import json
import re
import logging
from typing import Type, TypeVar, Callable, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def extract_json_from_llm(raw: str) -> dict:
    """
    从 LLM 响应中提取 JSON。
    策略：直接解析 → 去 markdown 块 → 正则提取 {} → 返回空 dict
    """
    if not raw or not raw.strip():
        return {}

    # 策略 1：直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 策略 2：去掉 ```json ... ``` 包裹
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 策略 3：正则提取第一个完整 JSON 对象（re.DOTALL 支持多行）
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 策略 3b：尝试从第一个 { 到最后一个 } 之间提取（处理前后有多余文本的情况）
    start = raw.find("{")
    end   = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    # 策略 4：返回空 dict，让调用方用默认值处理
    logger.warning(f"[LLMParser] 无法从响应中提取 JSON，原始内容前 200 字符: {raw[:200]}")
    return {}


def parse_llm_response(
    raw: str,
    model_class: Type[T],
    fallback_factory: Optional[Callable[[], T]] = None,
) -> T:
    """
    解析 LLM 响应为指定 Pydantic 模型。
    - 字段类型容错：字符串数字自动转 float
    - 解析失败时使用 fallback_factory 兜底
    """
    data = extract_json_from_llm(raw)

    # 字段类型容错：将字符串数字转为 float
    for field_name, field_info in model_class.model_fields.items():
        if field_name in data:
            annotation = field_info.annotation
            if annotation in (float,) and isinstance(data[field_name], str):
                try:
                    data[field_name] = float(data[field_name])
                except (ValueError, TypeError):
                    data.pop(field_name)

    try:
        return model_class(**data)
    except Exception as e:
        logger.warning(
            f"[LLMParser] {model_class.__name__} 构建失败: {e}，"
            f"原始内容前 300 字符: {raw[:300]}"
        )
        if fallback_factory:
            return fallback_factory()
        raise ValueError(
            f"LLM 响应解析失败: {e}\n原始响应前 500 字符: {raw[:500]}"
        )
