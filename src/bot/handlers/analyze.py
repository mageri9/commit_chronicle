"""Обработчик /analyze @username [period]."""

import io
import json
import math
import uuid
from datetime import datetime, timedelta
from html import escape

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.worker.tasks import format_summary
from src.bot.app import get_arq_pool
from src.core import clean_github_username, validate_github_username
from src.core.report import is_report_fresh, build_analysis_result
from src.storage.cache import get_redis
from src.storage import (
    acquire_job_lock,
    release_job_lock,
    check_and_increment_daily_limit,
    check_cooldown,
    set_cooldown,
)
from src.storage.database import list_tracked_repos
from src.bot.keyboards import paginate_inline_keyboard
from src.models.models import serialize_result
from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)


MAX_ANALYSIS_DAYS = 730


def get_default_period() -> str:
    """Дата начала периода по умолчанию."""
    return (datetime.now() - timedelta(days=MAX_ANALYSIS_DAYS)).strftime("%Y-%m-%d")


def validate_period(period: str) -> bool:
    try:
        date = datetime.strptime(period, "%Y-%m-%d").date()
        today = datetime.now().date()
        max_age = (datetime.now() - timedelta(days=MAX_ANALYSIS_DAYS)).date()
        return max_age <= date <= today
    except ValueError:
        return False


async def trigger_analysis_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    period: str,
) -> None:
    """
    Точка ветвления запуска анализа.
    Если у пользователя уже есть отслеживаемые репозитории в БД — показываем выбор,
    иначе — сразу запускаем полный анализ (он сам наполнит базу при первом запуске).
    """
    repos = await list_tracked_repos(username)
    if repos:
        await show_repo_selection_menu(update, context, username, period)
    else:
        await start_analysis_job(update, context, username, period)


async def show_repo_selection_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    period: str,
    page: int = 0,
    edit_existing: bool = False,
) -> None:
    """Отрисовка инлайн-меню выбора конкретного репозитория с пагинацией."""
    from src.bot.handlers.repos import make_safe_callback

    repos = await list_tracked_repos(username)
    if not repos:
        await start_analysis_job(update, context, username, period)
        return

    # 1. Сбор кнопок репозиториев для текущей страницы
    page_size = 6  # Ограничиваем, чтобы оставить место под кнопку "Все" и пагинацию
    total_pages = math.ceil(len(repos) / page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_repos = repos[start_idx:end_idx]

    repo_buttons = []
    for r in page_repos:
        repo_name = r["repo_full_name"]
        prefix = f"select_repo:run:{username}:{period}"
        callback_data = await make_safe_callback(repo_name, prefix)
        repo_buttons.append(
            InlineKeyboardButton(f"📁 {repo_name}", callback_data=callback_data)
        )

    # Используем общую утилиту пагинации кнопок
    paginated_markup = paginate_inline_keyboard(
        repo_buttons,
        page,
        page_size,
        callback_prefix=f"select_repo:page:{username}:{period}",
    )

    # 2. Объединяем клавиатуру (Кнопка "Все репозитории" всегда закреплена наверху)
    all_callback = f"select_repo:all:{username}:{period}"
    final_keyboard = [
        [InlineKeyboardButton("📁 Все репозитории", callback_data=all_callback)]
    ]
    for row in paginated_markup.inline_keyboard:
        final_keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(final_keyboard)
    text = (
        f"🔎 Выберите репозиторий профиля <b>{username}</b> для точечного анализа "
        f"или запустите проверку по всем сразу:"
    )

    if edit_existing and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
        )


async def select_repo_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обработчик нажатий инлайн-кнопок выбора репозитория перед запуском."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "noop":
        return

    logger.info(f"select_repo_callback: raw callback data = {data!r}")

    # Заменяем фиксированный split на динамический разбор по колонам
    parts = data.split(":")
    if len(parts) < 3:
        logger.warning(f"select_repo_callback: callback data is too short: {data!r}")
        return

    action = parts[1]
    username = parts[2]

    # Устанавливаем период по умолчанию на случай отсутствия в callback
    period = get_default_period()

    if action == "page":
        # Ожидаем форматы:
        # 1. select_repo:page:{username}:{period}:page:{target_page} (длина 6)
        # 2. select_repo:page:{username}:page:{target_page} (длина 5)
        if "page" in parts:
            try:
                idx = parts.index("page", 2)
                if idx == 4:
                    period = parts[3]
                target_page = int(parts[-1])
            except Exception:
                target_page = 0
        else:
            target_page = 0

        await show_repo_selection_menu(
            update,
            context,
            username,
            period,
            page=target_page,
            edit_existing=True,
        )

    elif action == "all":
        # Ожидаем: select_repo:all:{username}:{period}
        if len(parts) >= 4:
            period = parts[3]
        await start_analysis_job(update, context, username, period, repo_full_name=None)

    elif action == "run":
        from src.bot.handlers.repos import resolve_safe_callback

        if len(parts) >= 5:
            period = parts[3]
            repo_identifier = parts[4]
        elif len(parts) == 4:
            repo_identifier = parts[3]
        else:
            logger.warning(
                f"select_repo_callback: invalid run callback format: {data!r}"
            )
            return

        repo_full_name = await resolve_safe_callback(repo_identifier)
        await start_analysis_job(
            update, context, username, period, repo_full_name=repo_full_name
        )


async def start_analysis_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    period: str,
    repo_full_name: str | None = None,
) -> None:
    """Универсальная функция запуска анализа с проверкой лимитов."""
    chat_id = str(update.effective_chat.id)

    # ====ПЕРЕХВАТ FAST-PATH ====
    if await is_report_fresh(
        username,
        repo_full_name=repo_full_name,
        max_age_seconds=settings.fast_path_max_age,
    ):
        logger.info(f"Fast-path triggered for {username} (repo: {repo_full_name})")
        try:
            result = await build_analysis_result(
                username,
                period_start=period,
                period_end=datetime.now().strftime("%Y-%m-%d"),
                repo_full_name=repo_full_name,
            )
            result_json = serialize_result(result)

            if update.callback_query:
                try:
                    await update.callback_query.delete_message()
                except Exception:
                    pass

            await send_report(
                bot=context.bot,
                chat_id=chat_id,
                username=username,
                result_json=result_json,
            )

            pool = await get_arq_pool()
            await pool.enqueue_job("silent_background_sync", username)
            return
        except Exception as e:
            logger.error(
                f"Failed to build fast-path report for {username}: {e}. "
                f"Falling back to normal worker queue."
            )

    # 1. Проверяем индивидуальный кулдаун на этот юзернейм
    cooldown_left = await check_cooldown(chat_id, username)
    if cooldown_left > 0:
        minutes_left = (cooldown_left + 59) // 60
        text = (
            f"⏳ Пожалуйста, подождите. Повторный анализ <b>{escape(username)}</b> "
            f"будет доступен через {minutes_left} мин."
        )
        if update.callback_query:
            try:
                # Схлопываем инлайн-меню, предотвращая повторные нажатия
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML
                )
            except Exception:
                await update.callback_query.message.reply_text(
                    text, parse_mode=ParseMode.HTML
                )
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    # 2. Проверяем суточный лимит запросов пользователя
    is_under_limit = await check_and_increment_daily_limit(
        chat_id, settings.max_requests_per_user
    )
    if not is_under_limit:
        text = (
            f"❌ Вы превысили суточный лимит анализов ({settings.max_requests_per_user}). "
            f"Попробуйте запустить завтра."
        )
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text)
            except Exception:
                await update.callback_query.message.reply_text(text)
        else:
            await update.message.reply_text(text)
        return

    # 3. Пытаемся захватить блокировку параллельного выполнения задач
    job_id = str(uuid.uuid4())
    lock_acquired = await acquire_job_lock(chat_id, job_id)
    if not lock_acquired:
        text = "⏳ У вас уже выполняется другой анализ. Пожалуйста, дождитесь его завершения."
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text)
            except Exception:
                await update.callback_query.message.reply_text(text)
        else:
            await update.message.reply_text(text)
        return

    # Первичное сообщение-квитанция
    target_repo_suffix = f" [{repo_full_name}]" if repo_full_name else ""
    msg_text = (
        f"🔄 Анализирую <b>{escape(username)}</b>{escape(target_repo_suffix)} "
        f"с <b>{escape(period)}</b>..."
    )

    if update.callback_query:
        msg = await update.callback_query.edit_message_text(
            msg_text, parse_mode=ParseMode.HTML
        )
    else:
        msg = await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)

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

        # Запускаем задачу
        job = await pool.enqueue_job(
            "analyze_github_user",
            username,
            period,
            _job_id=job_id,
            chat_id=chat_id,
            repo_full_name=repo_full_name,  # <-- Передаем параметр воркеру
        )

        # Устанавливаем кулдаун
        await set_cooldown(chat_id, username, settings.user_cooldown_minutes)

        target_repo_info = (
            f"\n📁 Репозиторий: {escape(repo_full_name)}" if repo_full_name else ""
        )
        await msg.edit_text(
            f"🔄 Анализ запущен\n"
            f"👤 {escape(username)}\n"
            f"📅 с {escape(period)}{target_repo_info}\n"
            f"🆔 <code>{job.job_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
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

    await trigger_analysis_flow(update, context, username, period)


async def send_report(
    bot: Bot,
    chat_id: str | int,
    username: str,
    result_json: str,
    message_id: int | None = None,
) -> None:
    """
    Универсальный рендеринг и отправка отчёта.
    Если передан message_id — редактирует существующую квитанцию (путь воркера).
    Иначе отправляет новое текстовое сообщение в чат (путь fast-path).
    """
    summary = format_summary(result_json)

    if message_id:
        try:
            await bot.edit_message_text(
                summary,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Failed to edit message {message_id}: {e}")
            await bot.send_message(
                chat_id=chat_id, text=summary, parse_mode=ParseMode.HTML
            )
    else:
        await bot.send_message(
            chat_id=chat_id, text=summary, parse_mode=ParseMode.HTML
        )

    # Отправляем компактный файл отчета
    json_bytes = io.BytesIO(result_json.encode("utf-8"))
    json_bytes.name = f"{username}_analysis.json"
    await bot.send_document(chat_id=chat_id, document=json_bytes)


