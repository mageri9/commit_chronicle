"""Кеш на Redis — get/set с TTL."""

from src.logger import get_logger
from src.storage.redis import get_redis

logger = get_logger(__name__)


async def cache_get(key: str) -> str | None:
    """Получить значение. None если ключа нет или Redis недоступен."""
    try:
        r = await get_redis()
        return await r.get(key)
    except Exception as e:
        logger.warning(f"cache_get({key!r}) failed: {e}")
        return None


async def cache_set(key: str, value: str, ttl: int = 3600) -> None:
    """Записать значение с TTL в секундах."""
    try:
        r = await get_redis()
        await r.set(key, value, ex=ttl)
    except Exception as e:
        logger.warning(f"cache_set({key!r}) failed: {e}")


async def acquire_job_lock(chat_id: str, job_id: str, ttl: int = 600) -> bool:
    """Пытается захватить блокировку на параллельное выполнение задачи.

    Использует атомарную команду SET NX. Возвращает True в случае успеха,
    False — если у пользователя уже выполняется другая задача.
    """
    try:
        r = await get_redis()
        # nx=True обеспечивает атомарность: ключ установится только если его нет
        success = await r.set(f"active_job:{chat_id}", job_id, nx=True, ex=ttl)
        return bool(success)
    except Exception as e:
        logger.warning("Redis lock acquire failed: %s", e)
        # В случае сбоя Redis разрешаем запуск, чтобы не блокировать интерфейс
        return True


async def release_job_lock(chat_id: str, job_id: str) -> None:
    """Освобождает блокировку, если переданный job_id совпадает с текущим в ключе.

    Это защищает от ситуации, когда одна задача снимает блокировку другой
    задачи, которая запустилась позже (например, по истечении таймаута).
    """
    try:
        r = await get_redis()
        key = f"active_job:{chat_id}"
        current_val = await r.get(key)
        if current_val == job_id:
            await r.delete(key)
    except Exception as e:
        logger.warning("Redis lock release failed: %s", e)


async def check_and_increment_daily_limit(chat_id: str, max_requests: int) -> bool:
    """Проверяет суточный лимит запросов и атомарно увеличивает счетчик.

    Возвращает True, если лимит не превышен, иначе False.
    При первом запросе за день устанавливает TTL счетчика на 24 часа.
    """
    try:
        r = await get_redis()
        key = f"request_today:{chat_id}"

        current = await r.incr(key)
        if current == 1:
            await r.expire(key, 86400)

        return current <= max_requests
    except Exception as e:
        logger.warning("Redis daily limit check failed: %s", e)
        return True


async def check_cooldown(chat_id: str, username: str) -> int:
    """Проверяет, действует ли кулдаун на анализ данного юзернейма этим чатом.

    Возвращает оставшееся время кулдауна в секундах, либо 0, если кулдаун истек.
    """
    try:
        r = await get_redis()
        key = f"cooldown:{chat_id}:{username}"
        ttl = await r.ttl(key)
        return max(0, ttl)
    except Exception as e:
        logger.warning("Redis cooldown check failed: %s", e)
        return 0


async def set_cooldown(chat_id: str, username: str, cooldown_minutes: int) -> None:
    """Устанавливает временной кулдаун на анализ конкретного юзернейма."""
    try:
        r = await get_redis()
        key = f"cooldown:{chat_id}:{username}"
        await r.set(key, "active", ex=cooldown_minutes * 60)
    except Exception as e:
        logger.warning("Redis cooldown set failed: %s", e)