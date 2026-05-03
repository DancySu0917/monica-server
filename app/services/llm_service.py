"""
LLM 服务：并发限流 + 指数退避重试

环境路由策略：
  development → Ollama 本地模型，零费用、无网络依赖
  production  → Evolink 代理（Gemini 协议 + Bearer 鉴权）
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

# 生产环境 Evolink 支持的模型列表（降级顺序）
_EVOLINK_MODEL_CHAIN: List[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

TIMEOUT = httpx.Timeout(120.0, read=120.0)


class LLMService:

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        response_format: Optional[str] = None,   # "json_object"
        max_tokens: int = 2000,
        temperature: float = 0.2,
    ) -> tuple[str, str, int]:
        """
        调用 LLM。
        返回 (response_text, model_used, total_tokens)

        开发环境（APP_ENV=development）：直接走本地 Ollama，零费用、无网络依赖。
        生产环境（APP_ENV=production） ：走 Evolink 代理，按 _EVOLINK_MODEL_CHAIN 降级。
        """
        async with _llm_semaphore:
            # ── 开发环境：本地 Ollama ────────────────────────────────────
            if settings.APP_ENV == "development":
                ollama_model = settings.OLLAMA_DEFAULT_MODEL
                try:
                    text, tokens = await self._call_ollama(
                        model=ollama_model,
                        messages=messages,
                        response_format=response_format,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    logger.info(f"[LLM-Dev] Ollama model={ollama_model} tokens={tokens}")
                    return text, f"ollama:{ollama_model}", tokens
                except Exception as e:
                    logger.error(f"[LLM-Dev] Ollama 调用失败: {e}")
                    raise RuntimeError(f"开发环境 Ollama 调用失败，请确认 ollama serve 已启动: {e}")

            # ── 生产环境：Evolink 代理 + 降级链 ─────────────────────────
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

        raise RuntimeError(f"所有模型均失败，降级链: {chain}")

    async def _call_ollama(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        response_format: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, int]:
        """
        调用本地 Ollama（OpenAI 兼容接口）。
        无需 API Key，直连 http://localhost:11434/v1/chat/completions。
        """
        body: Dict[str, Any] = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      False,
        }
        if response_format == "json_object":
            body["response_format"] = {"type": "json_object"}

        # 本地模型推理较慢，超时放宽
        ollama_timeout = httpx.Timeout(180.0, read=180.0)
        async with httpx.AsyncClient(timeout=ollama_timeout) as client:
            resp = await client.post(
                settings.OLLAMA_BASE_URL,
                headers={"Authorization": "Bearer ollama"},
                json=body,
            )
            resp.raise_for_status()
            data      = resp.json()
            text      = data["choices"][0]["message"]["content"]
            total_tok = data.get("usage", {}).get("total_tokens", 0)
            return text, total_tok

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

        body: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature":     temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if response_format == "json_object":
            body["generationConfig"]["responseMimeType"] = "application/json"

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
            logger.info(f"[LLM-Prod] Evolink model={model} tokens={total_tok}")
            return text, total_tok
