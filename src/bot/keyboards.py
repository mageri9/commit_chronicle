"""Динамические клавиатуры для бота."""

from telegram import ReplyKeyboardMarkup, KeyboardButton


def get_main_keyboard(github_username: str | None = None) -> ReplyKeyboardMarkup:
    """Возвращает клавиатуру главного меню.

    Если передан github_username, выводится кнопка быстрого анализа этого профиля
    и кнопка отвязки. Если профиля нет — предлагается кнопка привязки.
    """
    if github_username:
        keyboard = [
            [KeyboardButton(f"📊 Мой анализ ({github_username})")],
            [
                KeyboardButton("🔍 Анализировать другой"),
                KeyboardButton("❌ Отвязать профиль"),
            ],
        ]
    else:
        keyboard = [
            [KeyboardButton("🔗 Привязать GitHub")],
            [
                KeyboardButton("🔍 Анализировать профиль"),
                KeyboardButton("ℹ️ Справка"),
            ],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)