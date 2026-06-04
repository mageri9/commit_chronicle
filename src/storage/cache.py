"""Минимальный кеш на Redis. Две функции."""

import redis.asyncio as aioredis
from src.config import settings

import logging

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Ленивое подключение — создаётся при первом использовании."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _redis


async def cache_get(key: str) -> str | None:
    """Получить значение. None если ключа нет или Redis недоступен."""
    try:
        r = await get_redis()
        return await r.get(key)
    except Exception as e:
        logger.warning("Redis GET failed: %s", e)
        return None


async def cache_set(key: str, value: str, ttl: int = 3600) -> None:
    """Записать значение с TTL в секундах."""
    try:
        r = await get_redis()
        await r.set(key, value, ex=ttl)
    except Exception as e:
        logger.warning("Redis GET failed: %s", e)
        pass