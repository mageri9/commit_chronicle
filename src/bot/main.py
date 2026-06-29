"""Точка входа Telegram-бота."""

import asyncio
from html import escape

from telegram.ext import CommandHandler, MessageHandler, filters
from src.bot.app import create_app, catch_up_missed_events, start_pubsub_listener
from src.bot.handlers.analyze import analyze_command
from src.bot.handlers.status import status_handler
from src.bot.handlers.menu import menu_message_handler
from src.storage import get_user_binding
from src.bot.keyboards import get_main_keyboard


async def start(update, context) -> None:
    """Приветствие на /start с учетом привязки GitHub."""
    chat_id = str(update.effective_chat.id)
    username = await get_user_binding(chat_id)

    if username:
        text = (
            f"👋 Рад видеть тебя снова!\n\n"
            f"Твой GitHub-профиль: <b>{escape(username)}</b>\n\n"
            f"Нажми кнопку ниже, чтобы запустить моментальный анализ за последние 2 года."
        )
    else:
        text = (
            f"👋 Привет!\n\n"
            f"Я <b>Commit Chronicle</b> — бот для анализа активности на GitHub.\n"
            f"Помогу собрать статистику коммитов, измененных строк и сгенерировать инсайты.\n\n"
            f"Нажми кнопку ниже, чтобы привязать свой профиль, или просто пришли мне ссылку/юзернейм."
        )

    reply_markup = get_main_keyboard(username)
    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def on_startup(app):
    """Действия при старте бота."""
    await catch_up_missed_events(app)
    asyncio.create_task(start_pubsub_listener(app))


def main():
    app = create_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(
        MessageHandler(filters.Regex(r"^/status(_\S+)?(\s+\S+)?$"), status_handler)
    )
    # Регистрируем обработчик текстовых сообщений меню и парсера
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_message_handler)
    )

    app.post_init = on_startup

    app.run_polling()


if __name__ == "__main__":
    main()