"""
LLM 服务：并发限流 + 指数退避重试

通过 Evolink 代理（Gemini 协议 + Bearer 鉴权）调用 LLM，
支持按 _EVOLINK_MODEL_CHAIN 顺序自动降级。
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.config import settings

logger = logging.getLogger(__name__)

# 并发限流：2C2G 环境，最多 2 个并发 LLM 请求
_llm_semaphore = asyncio.Semaphore(2)

# Evolink 支持的模型列表（降级顺序）
_EVOLINK_MODEL_CHAIN: List[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
]

TIMEOUT = httpx.Timeout(120.0, read=120.0)


class LLMService:

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        response_format: Optional[str] = None,   # "json_object"
        max_tokens: Optional[int] = None,   # None = 不限制，由模型自行决定
        temperature: float = 0.2,
    ) -> tuple[str, str, int]:
        """
        调用 LLM。
        返回 (response_text, model_used, total_tokens)

        通过 Evolink 代理按 _EVOLINK_MODEL_CHAIN 顺序自动降级。
        """
        async with _llm_semaphore:
            start_model = model or settings.DEFAULT_MODEL
            try:
                start_idx = _EVOLINK_MODEL_CHAIN.index(start_model)
            except ValueError:
                start_idx = 0
            chain = _EVOLINK_MODEL_CHAIN[start_idx:]

            for m in chain:
                try:
                    text, tokens = await self._call_evolink(
                        model=m,
                        messages=messages,
                        response_format=response_format,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    if m != start_model:
                        logger.info(f"[LLM] 降级到 {m} 成功")
                    return text, m, tokens
                except Exception as e:
                    logger.warning(f"[LLM] {m} 失败: {e}，尝试下一个模型")
                    continue

            # 所有模型均失败，在 semaphore 内部 raise（保证 semaphore 正常释放）
            raise RuntimeError(f"所有模型均失败，降级链: {chain}")

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
    )
    async def _call_evolink(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        response_format: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, int]:
        """
        Evolink 代理：Gemini 消息格式 + Bearer Token 鉴权。
        URL: {EVOLINK_BASE_URL}/{model}:generateContent
        Auth: Authorization: Bearer {EVOLINK_API_KEY}
        """
        contents = []
        system_text = ""
        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                system_text = msg.get("content", "")
            elif role == "assistant":
                contents.append({
                    "role": "model",
                    "parts": [{"text": msg.get("content", "")}],
                })
            else:  # user
                content_val = msg.get("content", "")
                if isinstance(content_val, list):
                    parts = []
                    for block in content_val:
                        if block.get("type") == "text":
                            parts.append({"text": block["text"]})
                        elif block.get("type") == "image_url":
                            url_val = block["image_url"]["url"]
                            if url_val.startswith("data:"):
                                mime, b64 = url_val.split(";", 1)
                                mime = mime.split(":")[1]
                                b64  = b64.split(",", 1)[1]
                                parts.append({
                                    "inline_data": {"mime_type": mime, "data": b64}
                                })
                    contents.append({"role": "user", "parts": parts})
                else:
                    contents.append({
                        "role": "user",
                        "parts": [{"text": content_val}],
                    })

        gen_config: Dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            gen_config["maxOutputTokens"] = max_tokens

        body: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if response_format == "json_object":
            body["generationConfig"]["responseMimeType"] = "application/json"

        # gemini-2.5-flash/pro 的 thinking 模式默认开启，内部推理会消耗大量 token
        # 对于结构化 JSON 输出任务，关闭 thinking 可节省 token 并避免输出被截断
        if "2.5" in model or "3" in model:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

        url = f"{settings.EVOLINK_BASE_URL.rstrip('/')}/{model}:generateContent"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.EVOLINK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            text = (
                data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
            )
            usage = data.get("usageMetadata", {})
            total_tok = (
                usage.get("promptTokenCount", 0) +
                usage.get("candidatesTokenCount", 0)
            )
            logger.info(f"[LLM] Evolink model={model} tokens={total_tok}")
            return text, total_tok
