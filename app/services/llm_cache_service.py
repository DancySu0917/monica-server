"""
LLM 结果缓存服务

以 file_hash（压缩包 SHA-256）+ scan_type + model 为 key，
将 Stage6 CoT 推理结果持久化到 Redis，避免对同一影像重复消耗 Token。

Key 格式:
    llm_cache:{file_hash}:{scan_type}:{model_tag}

TTL:
    默认 30 天（LLM_CACHE_TTL_DAYS 可在 .env 中配置）

序列化:
    JSON（AnalysisReport + CoTIntermediateResult 均为 Pydantic 模型，可序列化）

开关:
    use_llm_cache=True/False 由调用方（create_analysis API）传入，
    缓存本身始终写入（只要开关开着），但读取时需要开关为 True 才命中。
"""
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# 复用 quota_service 里的 Redis 连接池，避免再建连接
_redis_pool: Optional[aioredis.Redis] = None


def _get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


class LLMCacheService:
    """LLM 推理结果的 Redis 读写封装"""

    # 默认缓存 30 天；可通过 .env LLM_CACHE_TTL_DAYS 覆盖
    TTL_SECONDS: int = getattr(settings, "LLM_CACHE_TTL_DAYS", 30) * 86400
    KEY_PREFIX = "llm_cache"

    def _make_key(self, file_hash: str, scan_type: str, model: str) -> str:
        # model 中可能含有 "." 等特殊字符，统一 lower + replace
        model_tag = model.lower().replace(".", "-").replace("/", "-") or "default"
        return f"{self.KEY_PREFIX}:{file_hash}:{scan_type.upper()}:{model_tag}"

    async def get(
        self,
        file_hash: str,
        scan_type: str,
        model: str,
    ) -> Optional[tuple]:
        """
        查询缓存。
        返回 (report_dict, cot_dict) 若命中，否则返回 None。
        """
        key = self._make_key(file_hash, scan_type, model)
        try:
            redis = _get_redis()
            raw = await redis.get(key)
            if raw is None:
                return None
            data = json.loads(raw)
            logger.info(f"[LLMCache] 命中  key={key}")
            return data["report"], data["cot"]
        except Exception as e:
            # 缓存读取失败不阻塞主流程
            logger.warning(f"[LLMCache] 读取失败（降级继续）: {e}")
            return None

    async def set(
        self,
        file_hash: str,
        scan_type: str,
        model: str,
        report_dict: dict,
        cot_dict: dict,
    ) -> None:
        """
        写入缓存。
        report_dict / cot_dict 均为 Pydantic model.model_dump() 的结果。
        """
        key = self._make_key(file_hash, scan_type, model)
        try:
            redis = _get_redis()
            payload = json.dumps(
                {"report": report_dict, "cot": cot_dict},
                ensure_ascii=False,
            )
            await redis.setex(key, self.TTL_SECONDS, payload)
            logger.info(
                f"[LLMCache] 写入  key={key}  ttl={self.TTL_SECONDS}s"
                f"  payload_len={len(payload)}"
            )
        except Exception as e:
            logger.warning(f"[LLMCache] 写入失败（非致命）: {e}")

    async def invalidate(
        self,
        file_hash: str,
        scan_type: str,
        model: str,
    ) -> bool:
        """主动失效指定缓存，返回是否删除了缓存。"""
        key = self._make_key(file_hash, scan_type, model)
        try:
            redis = _get_redis()
            deleted = await redis.delete(key)
            logger.info(f"[LLMCache] 失效  key={key}  deleted={deleted}")
            return deleted > 0
        except Exception as e:
            logger.warning(f"[LLMCache] 失效失败: {e}")
            return False

    async def exists(
        self,
        file_hash: str,
        scan_type: str,
        model: str,
    ) -> bool:
        """判断缓存是否存在（不读取内容）。"""
        key = self._make_key(file_hash, scan_type, model)
        try:
            redis = _get_redis()
            return bool(await redis.exists(key))
        except Exception as e:
            logger.warning(f"[LLMCache] exists 查询失败: {e}")
            return False
