"""
LLM 服务：并发限流 + 指数退避重试

使用单一 OpenAI 兼容后端（LLM_BASE_URL + LLM_API_KEY + LLM_MODEL），
不含任何降级或多供应商逻辑。
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

TIMEOUT = httpx.Timeout(120.0, read=120.0)


class LLMService:

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        response_format: Optional[str] = None,   # "json_object"
        max_tokens: Optional[int] = None,
        temperature: float = 0.2,
    ) -> tuple[str, str, int]:
        """
        调用 LLM，返回 (response_text, model_used, total_tokens)。
        使用 settings.LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，无降级逻辑。
        """
        async with _llm_semaphore:
            used_model = model or settings.LLM_MODEL
            logger.info(f"[LLM] 请求 model={used_model}")
            text, tokens = await self._call_openai_compat(
                model=used_model,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return text, used_model, tokens

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
    )
    async def _call_openai_compat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        response_format: Optional[str],
        max_tokens: Optional[int],
        temperature: float,
    ) -> tuple[str, int]:
        """
        调用 OpenAI 兼容接口（/v1/chat/completions）。
        消息格式与 OpenAI chat completions 完全一致，图片使用 base64 data URL。
        """
        base_url = settings.LLM_BASE_URL.rstrip("/")
        api_key  = settings.LLM_API_KEY

        body: Dict[str, Any] = {
            "model":       model,
            "messages":    messages,
            "temperature": temperature,
            "stream":      False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format == "json_object":
            body["response_format"] = {"type": "json_object"}

        url = f"{base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            choice    = data.get("choices", [{}])[0]
            text      = choice.get("message", {}).get("content", "") or ""
            usage     = data.get("usage", {})
            total_tok = usage.get("total_tokens", 0) or (
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            )
            finish = choice.get("finish_reason", "")
            if finish and finish not in ("stop", "length", ""):
                logger.warning(f"[LLM] model={model} finish_reason={finish}")
            elif finish == "length":
                logger.warning(f"[LLM] model={model} 响应被截断（length），text长度={len(text)}")
            logger.info(f"[LLM] model={model} tokens={total_tok}")
            return text, total_tok
