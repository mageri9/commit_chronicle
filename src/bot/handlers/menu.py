"""Обработчик интерактивного меню и текстовых сообщений."""

import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.core import clean_github_username, validate_github_username
from src.storage import set_user_binding, remove_user_binding
from src.bot.keyboards import get_main_keyboard
from src.bot.handlers.analyze import start_analysis_job, DEFAULT_PERIOD


async def menu_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Единый обработчик текстовых сообщений и меню."""
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)

    # 1. Кнопка «Справка»
    if text == "ℹ️ Справка":
        help_text = (
            "📖 <b>Справка по Commit Chronicle</b>\n\n"
            "Я помогаю собирать статистику коммитов и измененных строк на GitHub.\n\n"
            "<b>Возможности:</b>\n"
            "• Нажми <code>📊 Мой анализ</code> для моментального запуска проверки своего профиля.\n"
            "• Пришли мне любой юзернейм или ссылку на профиль GitHub, и я сразу начну анализ.\n"
            "• Для выборочного периода используй команду:\n"
            "<code>/analyze @username YYYY-MM-DD</code>\n\n"
            "<i>Максимальный период анализа — 2 года. Форки репозиториев игнорируются при сборе.</i>"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
        return

    # 2. Кнопка «Отвязать профиль»
    if text == "❌ Отвязать профиль":
        was_removed = await remove_user_binding(chat_id)
        if was_removed:
            reply_markup = get_main_keyboard(None)
            await update.message.reply_text(
                "✅ Твой профиль на GitHub успешно отвязан.",
                reply_markup=reply_markup,
            )
        else:
            await update.message.reply_text("ℹ️ У тебя не было привязанного профиля.")
        return

    # 3. Кнопка «Привязать GitHub» (Запуск стейта)
    if text == "🔗 Привязать GitHub":
        context.user_data["state"] = "awaiting_bind"
        await update.message.reply_text(
            "📝 <b>Привязка аккаунта</b>\n\n"
            "Пришли свой GitHub-юзернейм (например, <code>torvalds</code>) или ссылку на свой профиль.\n"
            "Я запомню его, и ты сможешь запускать анализ в один клик.",
            parse_mode=ParseMode.HTML,
        )
        return

    # 4. Кнопки ручного запуска для ввода сторонних аккаунтов
    if text in ("🔍 Анализировать другой", "🔍 Анализировать профиль"):
        context.user_data.pop("state", None)
        await update.message.reply_text(
            "🔎 Отправь мне имя пользователя (юзернейм) или ссылку на любой профиль GitHub для начала анализа."
        )
        return

    # 5. Обработка клика по кнопке «Мой анализ (username)»
    my_analysis_match = re.match(r"^📊 Мой анализ \((.+)\)$", text)
    if my_analysis_match:
        username = my_analysis_match.group(1).lower()
        context.user_data.pop("state", None)
        await start_analysis_job(update, username, DEFAULT_PERIOD)
        return

    # 6. Умный парсер входящего текста ( Concept 1 + Concept 2 Hybrid )
    cleaned = clean_github_username(text)
    if not validate_github_username(cleaned):
        await update.message.reply_text(
            "❌ Некорректное имя пользователя или ссылка на профиль. "
            "Пожалуйста, проверь правильность ввода."
        )
        return

    state = context.user_data.get("state")
    if state == "awaiting_bind":
        # Пользователь был в режиме привязки -> привязываем профиль
        await set_user_binding(chat_id, cleaned)
        context.user_data.pop("state", None)

        reply_markup = get_main_keyboard(cleaned)
        await update.message.reply_text(
            f"🎉 Профиль <b>{escape(cleaned)}</b> успешно привязан!\n\n"
            f"Кнопки главного меню обновлены. Теперь ты можешь запустить его анализ в один клик.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    else:
        await start_analysis_job(update, cleaned, DEFAULT_PERIOD)