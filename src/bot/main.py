import asyncio
from html import escape

from telegram.ext import (
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
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
            "👋 Привет!\n\n"
            "Я <b>Commit Chronicle</b> — бот для анализа активности на GitHub.\n"
            "Помогу собрать статистику коммитов, измененных строк и сгенерировать инсайты.\n\n"
            "Нажми кнопку ниже, чтобы привязать свой профиль, или просто пришли мне ссылку/юзернейм."
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

    # --- ИНТЕГРАЦИЯ NEXUS SDK ---
    from os import environ

    nexus_sdk = None
    nexus_secret = environ.get("NEXUS_APP_SECRET", "")
    nexus_url = environ.get(
        "NEXUS_ENDPOINT_URL", "http://nexus-webhook:8000/events/app"
    )

    if nexus_secret:
        try:
            from nexus_sdk import NexusSDK

            nexus_sdk = NexusSDK(
                endpoint_url=nexus_url,
                app_secret=nexus_secret,
                project_name="chronicle",  # Должно строго совпадать с именем в manifests
            )
            # 1. Регистрируем глобальный перехватчик исключений python-telegram-bot
            nexus_sdk.register_ptb_error_handler(app)
            # 2. Запускаем периодическую отправку пульса (Heartbeat) каждые 15 секунд
            nexus_sdk.start_heartbeat(interval_seconds=15)
            print(
                "📡 Nexus SDK Observability initialized successfully for python-telegram-bot (Heartbeat & Error Handler)"
            )
        except Exception as e:
            print(f"Failed to initialize Nexus SDK: {e}")
    else:
        print("⚠️ NEXUS_APP_SECRET is not set in environment. Nexus SDK is disabled.")
    # ----------------------------

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze_command))

    from src.bot.handlers.repos import repos_command, repos_callback

    app.add_handler(CommandHandler("repos", repos_command))
    app.add_handler(CallbackQueryHandler(repos_callback, pattern=r"^repos:"))

    from src.bot.handlers.analyze import select_repo_callback

    app.add_handler(
        CallbackQueryHandler(select_repo_callback, pattern=r"^select_repo:")
    )

    app.add_handler(
        MessageHandler(filters.Regex(r"^/status(_\S+)?(\s+\S+)?$"), status_handler)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_message_handler)
    )

    app.post_init = on_startup

    try:
        app.run_polling()
    finally:
        # Грациозное закрытие сессии Nexus SDK по завершении работы полинга
        if nexus_sdk:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(nexus_sdk.close())
                else:
                    loop.run_until_complete(nexus_sdk.close())
            except Exception as e:
                print(f"Failed to close Nexus SDK: {e}")


if __name__ == "__main__":
    main()