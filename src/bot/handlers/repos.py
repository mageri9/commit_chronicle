import math
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.storage.database import (
    get_user_binding,
    list_all_tracked_repos,
    set_repo_active,
)
from src.storage.redis import get_redis
from src.bot.keyboards import paginate_inline_keyboard
from src.logger import get_logger

logger = get_logger(__name__)


async def make_safe_callback(repo_full_name: str, prefix: str) -> str:
    """Генерирует callback_data длиной <= 64 байт с использованием Redis-маппинга при необходимости."""
    if len(prefix) + 1 + len(repo_full_name) <= 64:
        return f"{prefix}:{repo_full_name}"

    import hashlib

    r = await get_redis()
    short_id = hashlib.md5(repo_full_name.encode()).hexdigest()[:12]
    await r.setex(f"repo_cb:{short_id}", 86400, repo_full_name)
    return f"{prefix}:r_{short_id}"


async def resolve_safe_callback(callback_val: str) -> str:
    """Восстанавливает полное имя репозитория, если использовался Redis-маппинг."""
    if callback_val.startswith("r_"):
        short_id = callback_val[2:]
        r = await get_redis()
        full_name = await r.get(f"repo_cb:{short_id}")
        if full_name:
            return full_name
    return callback_val


def render_repos_text(repos: list[dict], page: int, page_size: int = 8) -> str:
    """Генерирует текстовое представление страницы со списком репозиториев."""
    total = len(repos)
    total_pages = math.ceil(total / page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_repos = repos[start_idx:end_idx]

    text = (
        f"📁 <b>Отслеживаемые репозитории (Страница {page + 1}/{total_pages})</b>\n\n"
    )

    for r in page_repos:
        status_icon = "✅" if r["is_active"] else "❌"
        status_text = "Активен" if r["is_active"] else "Отключен"
        sync_mode = "🛰️ Webhook" if r["sync_mode"] == "webhook" else "🔄 Poll"

        last_pushed = r["last_pushed_at"]
        if last_pushed:
            last_pushed = last_pushed.split("T")[0]
        else:
            last_pushed = "нет данных"

        text += (
            f"• <b>{r['repo_full_name']}</b>\n"
            f"  Статус: {status_icon} {status_text}\n"
            f"  Обновлен: {last_pushed}\n"
            f"  Режим: {sync_mode}\n\n"
        )

    text += "<i>Нажмите на кнопку под сообщением, чтобы включить или отключить синхронизацию репозитория.</i>"
    return text


async def build_repos_keyboard(
    repos: list[dict], page: int, page_size: int = 8
) -> InlineKeyboardMarkup:
    """Строит инлайн-клавиатуру со списком репозиториев и кнопками переключения."""
    total = len(repos)
    total_pages = math.ceil(total / page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_repos = repos[start_idx:end_idx]

    buttons = []
    for r in page_repos:
        repo_name = r["repo_full_name"]
        is_active = r["is_active"]

        action_icon = "🔕 Откл:" if is_active else "🔔 Вкл:"
        label = f"{action_icon} {repo_name}"

        target_status = "0" if is_active else "1"
        callback_data = await make_safe_callback(
            repo_name, f"repos:toggle:{target_status}"
        )

        buttons.append(InlineKeyboardButton(label, callback_data=callback_data))

    return paginate_inline_keyboard(
        buttons,
        page,
        page_size,
        callback_prefix="repos",
    )


async def repos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Точка входа: команда /repos."""
    chat_id = str(update.effective_chat.id)
    username = await get_user_binding(chat_id)

    if not username:
        await update.message.reply_text(
            "❌ У вас нет привязанного GitHub-профиля. Сначала привяжите его с помощью кнопки в меню."
        )
        return

    repos = await list_all_tracked_repos(username)
    if not repos:
        await update.message.reply_text(
            f"ℹ️ Для профиля <b>{username}</b> еще нет отслеживаемых репозиториев.\n"
            f"Запустите анализ, чтобы наполнить базу данных.",
            parse_mode=ParseMode.HTML,
        )
        return

    text = render_repos_text(repos, 0)
    reply_markup = await build_repos_keyboard(repos, 0)

    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def repos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий инлайн-кнопок пагинации и переключения активности."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "noop":
        return

    parts = data.split(":", 3)
    if len(parts) < 3:
        return

    action = parts[1]
    chat_id = str(update.effective_chat.id)
    username = await get_user_binding(chat_id)

    if not username:
        await query.edit_message_text("❌ Привязанный профиль не найден.")
        return

    repos = await list_all_tracked_repos(username)
    if not repos:
        await query.edit_message_text("ℹ️ Список репозиториев пуст.")
        return

    page = 0

    if action == "page":
        page = int(parts[2])
    elif action == "list":
        target_status = parts[2] == "1"
        repo_identifier = parts[3]
        repo_full_name = await resolve_safe_callback(repo_identifier)

        await set_repo_active(repo_full_name, username, target_status)

        repos = await list_all_tracked_repos(username)

        repo_idx = next(
            (i for i, r in enumerate(repos) if r["repo_full_name"] == repo_full_name),
            0,
        )
        page = repo_idx // 8

    text = render_repos_text(repos, page)
    reply_markup = await build_repos_keyboard(repos, page)

    await query.edit_message_text(
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )