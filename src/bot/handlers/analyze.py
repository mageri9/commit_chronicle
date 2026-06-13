"""Обработчик /analyze @username [period]."""

import uuid

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from src.bot.app import get_arq_pool
from src.storage.cache import get_redis
from html import escape
import re
import json


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ставит задачу на анализ GitHub-пользователя."""
    if not context.args:
        await update.message.reply_text("ℹ️ Укажи GitHub-юзернейм: /analyze @username")
        return

    username = context.args[0].lstrip("@").strip()
    if not re.fullmatch(r"^[a-zA-Z0-9_-]+$", username):
        await update.message.reply_text("❌ Некорректный GitHub username")
        return
    period = context.args[1] if len(context.args) > 1 else "2024-01-01"

    msg = await update.message.reply_text(
        f"🔄 Анализирую <b>{escape(username)}</b> с <b>{escape(period)}</b>...",
        parse_mode=ParseMode.HTML,
    )

    try:
        pool = await get_arq_pool()
        job_id = str(uuid.uuid4())

        redis = await get_redis()

        mapping = {
            "chat_id": int(update.effective_chat.id),
            "message_id": int(msg.message_id),
            "username": str(username),
            "period": str(period),
        }

        await redis.setex(f"job_message:{job_id}", 3600, json.dumps(mapping))

        job = await pool.enqueue_job(
            "analyze_github_user",
            username.lower(),
            period,
            _job_id=job_id,
        )
        print(
            "MAPPING SAVED", job.job_id, await redis.exists(f"job_message:{job.job_id}")
        )

        await msg.edit_text(
            f"🔄 Анализ запущен\n"
            f"👤 {escape(username)}\n"
            f"📅 с {escape(period)}\n"
            f"🆔 <code>{job.job_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")