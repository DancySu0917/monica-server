"""
基于 Redis 的每日 Token 配额保护。
防止恶意用户滥用 LLM API，每用户每天限额。
"""
import asyncio
import redis.asyncio as aioredis
from datetime import date
from fastapi import HTTPException

from app.config import settings

# 模块级单例连接池（避免每次实例化创建新连接）
_redis_pool: aioredis.Redis | None = None
_redis_pool_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """惰性创建 Lock（必须在事件循环中创建）"""
    global _redis_pool_lock
    if _redis_pool_lock is None:
        _redis_pool_lock = asyncio.Lock()
    return _redis_pool_lock


async def get_redis_async() -> aioredis.Redis:
    """异步获取 Redis 连接池（带锁的惰性初始化，防止并发重复创建）"""
    global _redis_pool
    if _redis_pool is not None:
        return _redis_pool
    async with _get_lock():
        if _redis_pool is None:
            _redis_pool = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                max_connections=20,
            )
    return _redis_pool


def get_redis() -> aioredis.Redis:
    """同步获取连接池（模块加载阶段使用，不在协程中调用则无竞态风险）"""
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
    _KEY_TTL = 86400  # 24 小时

    def __init__(self):
        self.redis = get_redis()

    def _key(self, user_id: str) -> str:
        return f"quota:{user_id}:{date.today().isoformat()}"

    async def check_and_consume(self, user_id: str, estimated_tokens: int) -> None:
        key = self._key(user_id)   # 统一使用 _key() 方法，避免格式不一致
        # 原子加；超限立即回滚
        current = await self.redis.incrby(key, estimated_tokens)
        await self.redis.expire(key, self._KEY_TTL)   # 每次刷新 TTL

        if current > self.DAILY_TOKEN_LIMIT:
            await self.redis.decrby(key, estimated_tokens)
            raise QuotaExceededError(
                f"今日分析配额已用尽（{self.DAILY_TOKEN_LIMIT:,} tokens），请明日再试"
            )

    async def adjust(self, user_id: str, estimated_tokens: int, actual_tokens: int) -> None:
        """
        Stage6 完成后，用实际 Token 消耗修正配额。
        如果实际 > 预估，补扣差额；如果实际 < 预估，退还多扣的配额（不低于 0）。
        """
        if actual_tokens <= 0:
            return
        diff = actual_tokens - estimated_tokens
        if diff == 0:
            return
        key = self._key(user_id)
        if diff > 0:
            # 实际消耗超出预估，补扣差额
            await self.redis.incrby(key, diff)
            await self.redis.expire(key, self._KEY_TTL)
        else:
            # 实际消耗不足预估，退还多扣的（确保不低于 0）
            await self.redis.decrby(key, -diff)

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
