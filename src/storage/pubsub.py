"""Redis pub/sub — publish и subscribe."""

import redis.asyncio as aioredis
from redis.asyncio.client import PubSub

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

_client: aioredis.Redis | None = None


def _make_client() -> aioredis.Redis:
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        retry_on_timeout=True,  # переподключается при обрыве
        socket_keepalive=True,  # держит соединение живым при простое
    )


async def get_client() -> aioredis.Redis:
    """Ленивое подключение. Один клиент на процесс."""
    global _client
    _client = _client or _make_client()
    return _client


async def close() -> None:
    """Закрыть клиент при graceful shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def publish(channel: str, message: str) -> None:
    """Опубликовать сообщение в канал.

    Пробрасывает исключение наружу — вызывающий код (tasks.py)
    решает как реагировать на недоступность Redis.
    """
    client = await get_client()
    try:
        await client.publish(channel, message)
        logger.debug(f"publish → {channel!r}: {message!r}")
    except Exception as e:
        logger.error(f"publish({channel!r}) failed: {e}")
        raise


async def subscribe(channel: str):
    """Подписаться на канал. Async generator — отдаёт данные сообщений.

    Гарантирует unsubscribe + aclose при любом выходе (break / исключение).
    При ошибке во время listen — пробрасывает исключение наружу,
    чтобы bot listener мог применить retry-логику.
    """
    client = await get_client()
    pubsub: PubSub = client.pubsub()
    await pubsub.subscribe(channel)
    logger.debug(f"subscribe → {channel!r}")

    try:
        async for msg in pubsub.listen():
            logger.debug(f"pubsub {msg['type']} ← {channel!r}: {msg.get('data')!r}")
            if msg["type"] == "message":
                yield msg["data"]
    except Exception as e:
        logger.error(f"subscribe({channel!r}) error: {e}")
        raise
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        logger.debug(f"unsubscribe ← {channel!r}")