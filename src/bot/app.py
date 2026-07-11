"""Фабрика Telegram-приложения."""

import asyncio
import io
import json

from arq import create_pool
from arq.connections import RedisSettings
from telegram import Bot
from telegram.ext import Application

from src.config import settings
from src.logger import get_logger
from src.storage.database import get_request, get_unnotified_requests, mark_as_notified
from src.storage.pubsub import subscribe
from src.storage.redis import get_redis
from src.storage.cache import release_job_lock
from src.worker.tasks import format_summary

logger = get_logger(__name__)

_arq_pool = None

# Сколько раз и с каким интервалом ждать маппинг job_message:{job_id} в Redis.
# Маппинг пишется ботом до enqueue — в норме он уже есть. Retry нужен
# на случай гонки между publish(worker) и set(bot) при очень быстром завершении.
_MAPPING_RETRY_COUNT = 10
_MAPPING_RETRY_INTERVAL = 0.1  # секунд


async def get_arq_pool():
    """Ленивое подключение к Redis для arq."""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


# ---------------------------------------------------------------------------
# Общая логика уведомления
# ---------------------------------------------------------------------------


async def _notify_user(
    bot: Bot,
    job_id: str,
    status: str,
    chat_id: str,
    message_id: int,
    username: str,
    result_json: str | None = None,
    error: str | None = None,
) -> None:
    """Отправить итоговое уведомление пользователю."""
    if status == "done" and result_json:
        # Импортируем общую функцию отправки результатов
        from src.bot.handlers.analyze import send_report

        await send_report(
            bot=bot,
            chat_id=chat_id,
            username=username,
            result_json=result_json,
            message_id=message_id,  # Указываем квитанцию для редактирования
        )

    elif status == "failed":
        await bot.edit_message_text(
            f"❌ Ошибка анализа: {error or 'неизвестная ошибка'}",
            chat_id=chat_id,
            message_id=message_id,
        )


async def _get_mapping(redis, job_id: str) -> dict | None:
    """
    Достать маппинг job_message:{job_id} из Redis с retry.

    Маппинг пишется ботом до enqueue — в норме уже есть к моменту
    получения pub/sub события. Retry защищает от гонки при очень
    быстром завершении задачи.
    """
    key = f"job_message:{job_id}"
    for _ in range(_MAPPING_RETRY_COUNT):
        raw = await redis.get(key)
        if raw:
            return json.loads(raw)
        await asyncio.sleep(_MAPPING_RETRY_INTERVAL)

    logger.error(f"Mapping not found after {_MAPPING_RETRY_COUNT} retries: {key}")
    return None


# ---------------------------------------------------------------------------
# Pub/Sub listener
# ---------------------------------------------------------------------------


async def start_pubsub_listener(app: Application) -> None:
    """Слушает Redis pub/sub и обновляет сообщения бота."""
    redis = await get_redis()

    async for event_json in subscribe("job:done"):
        try:
            data = json.loads(event_json)
            job_id = data["job_id"]
            status = data["status"]

            mapping = await _get_mapping(redis, job_id)
            if not mapping:
                continue

            chat_id = mapping["chat_id"]
            message_id = mapping["message_id"]
            username = mapping["username"]

            result_json: str | None = None
            error: str | None = None

            if status == "done":
                record = await get_request(job_id)
                if not record or not record.get("result_json"):
                    logger.error(f"[{job_id}] result_json отсутствует в БД")
                    continue
                result_json = record["result_json"]
            elif status == "failed":
                error = data.get("error", "неизвестная ошибка")

            await _notify_user(
                bot=app.bot,
                job_id=job_id,
                status=status,
                chat_id=chat_id,
                message_id=message_id,
                username=username,
                result_json=result_json,
                error=error,
            )

            await mark_as_notified(job_id)
            await release_job_lock(str(chat_id), job_id)  # Сняли лок
            await redis.delete(f"job_message:{job_id}")

        except Exception as e:
            logger.exception(f"Listener error on event {event_json!r}: {e}")


# ---------------------------------------------------------------------------
# Catch-up при старте
# ---------------------------------------------------------------------------


async def catch_up_missed_events(app: Application) -> None:
    """При старте бота: найти завершённые, но не отправленные уведомления."""
    redis = await get_redis()
    rows = await get_unnotified_requests()

    if not rows:
        return

    logger.info(f"catch_up: найдено {len(rows)} неотправленных уведомлений")

    for row in rows:
        job_id = row["id"]

        mapping = await _get_mapping(redis, job_id)
        if not mapping:
            # Маппинг протух в Redis (TTL) — уведомить уже некуда,
            # но помечаем notified чтобы не проверять повторно при следующем старте
            await mark_as_notified(job_id)
            continue

        chat_id = mapping["chat_id"]
        message_id = mapping["message_id"]
        username = mapping["username"]

        try:
            await _notify_user(
                bot=app.bot,
                job_id=job_id,
                status=row["status"],
                chat_id=chat_id,
                message_id=message_id,
                username=username,
                result_json=row.get("result_json"),
                error=row.get("error_message"),
            )
        except Exception as e:
            logger.exception(f"[{job_id}] catch_up notify failed: {e}")
            continue

        await mark_as_notified(job_id)
        await release_job_lock(str(chat_id), job_id)  # Сняли лок
        await redis.delete(f"job_message:{job_id}")


# ---------------------------------------------------------------------------
# Фабрика
# ---------------------------------------------------------------------------


def create_app() -> Application:
    """Создать и вернуть Application."""
    return Application.builder().token(settings.telegram_bot_token).build()