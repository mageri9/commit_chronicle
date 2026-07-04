"""Обработчик /analyze @username [period]."""

import uuid
from datetime import datetime, timedelta
import json
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.bot.app import get_arq_pool
from src.core import clean_github_username, validate_github_username
from src.storage.cache import get_redis
from src.storage import (
    acquire_job_lock,
    release_job_lock,
    check_and_increment_daily_limit,
    check_cooldown,
    set_cooldown,
)
from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)


MAX_ANALYSIS_DAYS = 730


def get_default_period() -> str:
    """Дата начала периода по умолчанию — вычисляется на момент вызова,
    а не один раз при импорте модуля."""
    return (datetime.now() - timedelta(days=MAX_ANALYSIS_DAYS)).strftime("%Y-%m-%d")


def validate_period(period: str) -> bool:
    try:
        date = datetime.strptime(period, "%Y-%m-%d").date()
        today = datetime.now().date()
        max_age = (datetime.now() - timedelta(days=MAX_ANALYSIS_DAYS)).date()
        return max_age <= date <= today
    except ValueError:
        return False


async def start_analysis_job(update: Update, username: str, period: str) -> None:
    """Универсальная функция запуска анализа с проверкой лимитов."""
    chat_id = str(update.effective_chat.id)

    # 1. Проверяем индивидуальный кулдаун на этот юзернейм
    cooldown_left = await check_cooldown(chat_id, username)
    if cooldown_left > 0:
        minutes_left = (cooldown_left + 59) // 60
        await update.message.reply_text(
            f"⏳ Пожалуйста, подождите. Повторный анализ <b>{escape(username)}</b> "
            f"будет доступен через {minutes_left} мин.",
            parse_mode=ParseMode.HTML,
        )
        return

    # 2. Проверяем суточный лимит запросов пользователя
    is_under_limit = await check_and_increment_daily_limit(
        chat_id, settings.max_requests_per_user
    )
    if not is_under_limit:
        await update.message.reply_text(
            f"❌ Вы превысили суточный лимит анализов ({settings.max_requests_per_user}). "
            f"Попробуйте запустить завтра."
        )
        return

    # 3. Пытаемся захватить блокировку параллельного выполнения задач
    job_id = str(uuid.uuid4())
    lock_acquired = await acquire_job_lock(chat_id, job_id)
    if not lock_acquired:
        await update.message.reply_text(
            "⏳ У вас уже выполняется другой анализ. Пожалуйста, дождитесь его завершения."
        )
        return

    # Первичное сообщение-квитанция
    msg = await update.message.reply_text(
        f"🔄 Анализирую <b>{escape(username)}</b> с <b>{escape(period)}</b>...",
        parse_mode=ParseMode.HTML,
    )

    try:
        pool = await get_arq_pool()
        redis = await get_redis()

        mapping = {
            "chat_id": int(chat_id),
            "message_id": int(msg.message_id),
            "username": str(username),
            "period": str(period),
        }

        await redis.setex(f"job_message:{job_id}", 3600, json.dumps(mapping))

        job = await pool.enqueue_job(
            "analyze_github_user",
            username,
            period,
            _job_id=job_id,
            chat_id=chat_id,
        )

        # Устанавливаем кулдаун
        await set_cooldown(chat_id, username, settings.user_cooldown_minutes)

        await msg.edit_text(
            f"🔄 Анализ запущен\n"
            f"👤 {escape(username)}\n"
            f"📅 с {escape(period)}\n"
            f"🆔 <code>{job.job_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        # В случае ошибки снимаем блокировку в Redis
        await release_job_lock(chat_id, job_id)
        logger.error(f"Failed to enqueue job: {e}")
        await msg.edit_text(f"❌ Ошибка запуска: {e}")


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ставит задачу на анализ GitHub-пользователя через команду."""
    if not context.args:
        await update.message.reply_text("ℹ️ Укажи GitHub-юзернейм: /analyze @username")
        return

    username = clean_github_username(context.args[0])
    if not validate_github_username(username):
        await update.message.reply_text("❌ Некорректный GitHub username")
        return

    period = context.args[1] if len(context.args) > 1 else get_default_period()
    if not validate_period(period):
        await update.message.reply_text(
            "❌ Максимальный период анализа — 2 года (YYYY-MM-DD)"
        )
        return

    await start_analysis_job(update, username, period)