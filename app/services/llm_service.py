"""
LLM 服务：并发限流 + 指数退避重试

降级链（按顺序尝试）：
  1. api.b.ai  —— OpenAI 兼容协议，首选（BAI_API_KEY）
  2. Evolink   —— OpenAI 兼容协议代理（EVOLINK_API_KEY），api.b.ai 不可用时备用
  3. Gemini 直连 —— Google 官方 API（GEMINI_API_KEY），最终兜底

每个后端内部按 _MODEL_CHAIN 顺序自动降级至更轻量的模型。
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

# ── 模型降级链 ────────────────────────────────────────────────────────
# api.b.ai：实测仅 gpt-5.4-nano 可用（gemini 系列需充值）
_BAI_MODEL_CHAIN: List[str] = [
    "gpt-5.4-nano",
]

# Evolink：实测可用模型
_EVOLINK_MODEL_CHAIN: List[str] = [
    "gemini-2.5-pro",        # 实测可用
    "gemini-2.5-flash",      # 新增
]

TIMEOUT = httpx.Timeout(120.0, read=120.0)


def _build_chain(model: Optional[str], chain: List[str]) -> List[str]:
    """从指定 model 开始截取降级链，指定模型不在链中则先试该模型再走整个链。
    
    例：chain=["A","B","C"], model="B" → ["B","C"]
        chain=["A","B","C"], model="X" → ["X","A","B","C"]
    """
    if not model:
        return chain
    try:
        idx = chain.index(model)
        return chain[idx:]   # 从该模型开始，后续的依次降级
    except ValueError:
        # 指定模型不在标准链中，先尝试该模型，再走整个链
        return [model] + chain


class LLMService:

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        response_format: Optional[str] = None,   # "json_object"
        max_tokens: Optional[int] = None,
        temperature: float = 0.2,
        provider: str = "",   # "" = 自动降级；"bai" / "evolink" / "gemini" = 强制指定
    ) -> tuple[str, str, int]:
        """
        调用 LLM，返回 (response_text, model_used, total_tokens)。

        provider 参数：
          ""        → 自动按降级链尝试：api.b.ai → Evolink → Gemini 直连
          "bai"     → 仅用 api.b.ai，失败直接报错
          "evolink" → 仅用 Evolink，失败直接报错
          "gemini"  → 仅用 Gemini 直连，失败直接报错
        """
        async with _llm_semaphore:
            start_model = model or settings.DEFAULT_MODEL
            errors: List[str] = []
            prov = provider.lower().strip() if provider else ""

            logger.info(f"[LLM] 请求 model={start_model} provider={prov or 'auto'}")

            # ── 1. api.b.ai（OpenAI 兼容协议）─────────────────────────
            if prov in ("", "bai") and settings.BAI_API_KEY:
                # 明确指定 provider=bai 时，不做跨模型降级；仅在自动模式下允许降级
                if prov == "bai":
                    bai_chain = [start_model]  # 只试指定的模型，失败直接报错
                else:
                    bai_chain = _build_chain(start_model, _BAI_MODEL_CHAIN)
                for m in bai_chain:
                    try:
                        text, tokens = await self._call_openai_compat(
                            model=m,
                            messages=messages,
                            response_format=response_format,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        )
                        if m != start_model:
                            logger.info(f"[LLM] api.b.ai 自动降级到 {m} 成功")
                        return text, m, tokens
                    except Exception as e:
                        err = f"api.b.ai/{m}: {e}"
                        logger.warning(f"[LLM] {err}，尝试下一个")
                        errors.append(err)
                        continue
                if prov == "bai":
                    raise RuntimeError(f"api.b.ai/{start_model} 调用失败: {'; '.join(errors)}")

            # ── 2. Evolink（OpenAI 兼容协议）─────────────────────────
            if prov in ("", "evolink") and settings.EVOLINK_API_KEY:
                # 明确指定 provider=evolink 时，不做跨模型降级
                if prov == "evolink":
                    evolink_chain = [start_model]  # 只试指定的模型，失败直接报错
                else:
                    evolink_chain = _build_chain(
                        start_model if start_model in _EVOLINK_MODEL_CHAIN else None,
                        _EVOLINK_MODEL_CHAIN,
                    )
                for m in evolink_chain:
                    try:
                        text, tokens = await self._call_openai_compat(
                            model=m,
                            messages=messages,
                            response_format=response_format,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            base_url=settings.EVOLINK_BASE_URL,
                            api_key=settings.EVOLINK_API_KEY,
                            provider_name="Evolink",
                        )
                        if m != start_model:
                            logger.info(f"[LLM] Evolink 自动降级到 {m} 成功")
                        return text, m, tokens
                    except Exception as e:
                        err = f"Evolink/{m}: {e}"
                        logger.warning(f"[LLM] {err}，尝试下一个")
                        errors.append(err)
                        continue
                if prov == "evolink":
                    raise RuntimeError(f"Evolink/{start_model} 调用失败: {'; '.join(errors)}")

            # ── 3. Gemini 直连（最终兜底）────────────────────────────
            if prov in ("", "gemini") and settings.GEMINI_API_KEY:
                fallback_model = start_model if "gemini" in start_model else "gemini-3-flash"
                try:
                    text, tokens = await self._call_gemini_direct(
                        model=fallback_model,
                        messages=messages,
                        response_format=response_format,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    logger.info(f"[LLM] Gemini 直连成功 model={fallback_model}")
                    return text, f"{fallback_model}-direct", tokens
                except Exception as e:
                    errors.append(f"Gemini直连/{fallback_model}: {e}")
                    logger.warning(f"[LLM] Gemini 直连也失败: {e}")
                    if prov == "gemini":
                        raise RuntimeError(f"Gemini 直连失败: {e}") from e

            # 全部失败（或指定后端未配置 Key）
            if prov and not errors:
                raise RuntimeError(
                    f"指定后端 '{prov}' 未配置对应 API Key"
                )
            raise RuntimeError(
                f"所有 LLM 后端均失败。错误详情: {'; '.join(errors[-6:])}"
            )

    # ────────────────────────────────────────────────────────────────
    #  OpenAI 兼容 /v1/chat/completions（api.b.ai 和 Evolink 共用）
    # ────────────────────────────────────────────────────────────────
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
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        provider_name: str = "api.b.ai",
    ) -> tuple[str, int]:
        """
        调用 OpenAI 兼容接口（/v1/chat/completions）。
        api.b.ai 和 Evolink 均使用此方法，通过 base_url / api_key 区分。
        消息格式与 OpenAI chat completions 完全一致，图片使用 base64 data URL。
        """
        _base_url = (base_url or settings.BAI_BASE_URL).rstrip("/")
        _api_key  = api_key or settings.BAI_API_KEY

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

        url = f"{_base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {_api_key}",
                    "Content-Type":  "application/json",
                },
                json=body,
            )
            # 402/403 直接抛出以触发降级
            if resp.status_code in (402, 403):
                raise RuntimeError(f"{provider_name} {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()

            choice  = data.get("choices", [{}])[0]
            text    = choice.get("message", {}).get("content", "") or ""
            usage   = data.get("usage", {})
            total_tok = usage.get("total_tokens", 0) or (
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            )
            finish  = choice.get("finish_reason", "")
            if finish and finish not in ("stop", "length", ""):
                logger.warning(f"[LLM] {provider_name} model={model} finish_reason={finish}")
            elif finish == "length":
                logger.warning(f"[LLM] {provider_name} model={model} 响应被截断（length），text长度={len(text)}")
            logger.info(f"[LLM] {provider_name} model={model} tokens={total_tok}")
            return text, total_tok

    # ────────────────────────────────────────────────────────────────
    #  Gemini 直连 — Google 官方 API（最终兜底）
    # ────────────────────────────────────────────────────────────────
    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def _call_gemini_direct(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        response_format: Optional[str],
        max_tokens: Optional[int],
        temperature: float,
    ) -> tuple[str, int]:
        """
        Google Gemini API 直连（Evolink 与 api.b.ai 均不可用时的最终备选）。
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
            else:
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

        if "2.5" in model or "3" in model:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={settings.GEMINI_API_KEY}"
        )
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            candidate = data.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            text_parts = [
                p.get("text", "")
                for p in parts
                if not p.get("thought", False) and p.get("text")
            ]
            text = "".join(text_parts)
            usage = data.get("usageMetadata", {})
            total_tok = (
                usage.get("promptTokenCount", 0) +
                usage.get("candidatesTokenCount", 0)
            )
            logger.info(f"[LLM] Gemini直连 model={model} tokens={total_tok}")
            return text, total_tok
