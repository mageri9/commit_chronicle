"""Фабрика Telegram-приложения."""

import asyncio
import json
import io
from telegram.ext import Application
from arq import create_pool
from arq.connections import RedisSettings
from src.config import settings
from src.storage.database import get_request
from src.storage.pubsub import subscribe
from src.storage.cache import get_redis
from src.worker.tasks import format_summary
from src.logger import get_logger

logger = get_logger(__name__)

_arq_pool = None


async def get_arq_pool():
    """Ленивое подключение к Redis для arq."""
    global _arq_pool

    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    return _arq_pool


async def start_pubsub_listener(app: Application) -> None:
    """Слушает Redis pub/sub и обновляет сообщения бота."""
    redis = await get_redis()

    async for event_json in subscribe("job:done"):
        try:
            data = json.loads(event_json)
            job_id = data["job_id"]
            status = data["status"]

            # Найти маппинг в Redis
            key = f"job_message:{job_id}"

            mapping_raw = None

            for _ in range(10):
                mapping_raw = await redis.get(key)
                if mapping_raw:
                    break
                await asyncio.sleep(0.1)
            if not mapping_raw:
                logger.error(f"⚠️ Mapping not found after retry: {key}")
                continue

            mapping = json.loads(mapping_raw)
            chat_id = mapping["chat_id"]
            message_id = mapping["message_id"]

            if status == "done":
                # Достать полный результат из БД
                record = await get_request(job_id)
                if not record or not record.get("result_json"):
                    continue

                summary = format_summary(record["result_json"])

                # Обновить сообщение
                await app.bot.edit_message_text(
                    summary,
                    chat_id=chat_id,
                    message_id=message_id,
                )

                # Отправить JSON-файл
                json_bytes = io.BytesIO(record["result_json"].encode("utf-8"))
                json_bytes.name = f"{mapping['username']}_analysis.json"
                await app.bot.send_document(
                    chat_id=chat_id,
                    document=json_bytes,
                )

            elif status == "failed":
                error = data.get("error", "неизвестная ошибка")
                await app.bot.edit_message_text(
                    f"❌ Ошибка анализа: {error}",
                    chat_id=chat_id,
                    message_id=message_id,
                )

            # Удалить маппинг после обработки
            await redis.delete(f"job_message:{job_id}")

        except Exception as e:
            logger.error(f"⚠️ Listener error: {e}")  #  временно


async def catch_up_missed_events(app: Application) -> None:
    """При старте бота: найти завершённые, но не отправленные уведомления."""
    from src.storage.database import engine, requests
    from sqlalchemy import select, and_

    redis = await get_redis()

    async with engine.connect() as conn:
        result = await conn.execute(
            requests.select().where(
                and_(
                    requests.c.status.in_(["done", "failed"]),
                    requests.c.notified == "false",
                )
            )
        )
        rows = result.fetchall()

    for row in rows:
        row_dict = dict(row._mapping)
        job_id = row_dict["id"]

        # Найти маппинг в Redis
        mapping_raw = await redis.get(f"job_message:{job_id}")
        if not mapping_raw:
            continue

        mapping = json.loads(mapping_raw)
        chat_id = mapping["chat_id"]
        message_id = mapping["message_id"]

        if row_dict["status"] == "done" and row_dict.get("result_json"):
            summary = format_summary(row_dict["result_json"])

            await app.bot.edit_message_text(
                summary, chat_id=chat_id, message_id=message_id
            )

            json_bytes = io.BytesIO(row_dict["result_json"].encode("utf-8"))
            json_bytes.name = f"{mapping['username']}_analysis.json"
            await app.bot.send_document(chat_id=chat_id, document=json_bytes)

        elif row_dict["status"] == "failed":
            error = row_dict.get("error_message", "неизвестная ошибка")
            await app.bot.edit_message_text(
                f"❌ {error}", chat_id=chat_id, message_id=message_id
            )

        # Пометить как отправленное
        from src.storage.database import db_lock

        async with db_lock:
            async with engine.begin() as conn:
                await conn.execute(
                    requests.update()
                    .where(requests.c.id == job_id)
                    .values(notified="true")
                )

        await redis.delete(f"job_message:{job_id}")


def create_app() -> Application:
    """Создать и вернуть Application."""
    return Application.builder().token(settings.telegram_bot_token).build()