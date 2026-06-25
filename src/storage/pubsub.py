"""Redis pub/sub — publish и subscribe."""

import redis.asyncio as aioredis
from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)


_client: aioredis.Redis | None = None


async def get_client() -> aioredis.Redis:
    """Ленивое подключение."""
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _client


async def publish(channel: str, message: str) -> None:
    """Опубликовать сообщение в канал."""
    try:
        client = await get_client()
        await client.publish(channel, message)
    except Exception:
        pass


async def subscribe(channel: str):
    """Подписаться на канал. Async generator — отдаёт сообщения."""
    try:
        client = await get_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)

        async for msg in pubsub.listen():
            if msg["type"] == "message":
                yield msg["data"]
            logger.debug(f"Получено pubsub: {msg['data']}")
    except Exception:
        pass