"""
基于 Redis 的每日 Token 配额保护。
防止恶意用户滥用 LLM API，每用户每天限额。
"""
import redis.asyncio as aioredis
from datetime import date
from fastapi import HTTPException

from app.config import settings

# 模块级单例连接池（避免每次实例化创建新连接）
_redis_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


class QuotaExceededError(Exception):
    pass


class QuotaService:
    """
    基于 Redis 的滑动窗口 Token 配额。
    每用户每天限额，超限返回 429。

    TOCTOU 修复：原子 incrby + 超限立即 decrby 回滚。
    """

    DAILY_TOKEN_LIMIT = settings.DAILY_TOKEN_LIMIT

    def __init__(self):
        self.redis = get_redis()

    def _key(self, user_id: str) -> str:
        return f"quota:{user_id}:{date.today().isoformat()}"

    async def check_and_consume(self, user_id: str, estimated_tokens: int) -> None:
        key = f"quota:{user_id}:{date.today()}"
        # 原子加；超限立即回滚
        current = await self.redis.incrby(key, estimated_tokens)
        await self.redis.expire(key, 86400)   # 每次刷新 TTL

        if current > self.DAILY_TOKEN_LIMIT:
            await self.redis.decrby(key, estimated_tokens)
            raise QuotaExceededError(
                f"今日分析配额已用尽（{self.DAILY_TOKEN_LIMIT:,} tokens），请明日再试"
            )

    async def get_remaining(self, user_id: str) -> int:
        key = self._key(user_id)
        used = int(await self.redis.get(key) or 0)
        return max(0, self.DAILY_TOKEN_LIMIT - used)

    async def get_status(self, user_id: str) -> dict:
        from datetime import datetime, timezone, timedelta
        remaining = await self.get_remaining(user_id)
        # 重置时间：明天 00:00 UTC+8
        now_cst = datetime.now(timezone(timedelta(hours=8)))
        reset_at = (now_cst + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        return {
            "remaining_tokens": remaining,
            "daily_limit":      self.DAILY_TOKEN_LIMIT,
            "reset_at":         reset_at,
        }
