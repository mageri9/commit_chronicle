"""Динамические клавиатуры для бота."""

import math
from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def get_main_keyboard(github_username: str | None = None) -> ReplyKeyboardMarkup:
    """Возвращает клавиатуру главного меню."""
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


def paginate_inline_keyboard(
    buttons: list[InlineKeyboardButton],
    page: int,
    page_size: int = 8,
    callback_prefix: str = "page",
) -> InlineKeyboardMarkup:
    """
    Разбивает плоский список InlineKeyboardButton на страницы и добавляет
    строку навигации «← Назад / Страница X/Y / Дальше →», если кнопок больше page_size.
    """
    total_buttons = len(buttons)
    if total_buttons == 0:
        return InlineKeyboardMarkup([])

    total_pages = math.ceil(total_buttons / page_size)
    page = max(0, min(page, total_pages - 1))

    # Извлекаем кнопки для текущей страницы
    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_buttons = buttons[start_idx:end_idx]

    # Строим сетку (каждая кнопка на отдельной строке)
    keyboard = [[btn] for btn in page_buttons]

    # Добавляем строку навигации, если страниц больше одной
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "← Назад", callback_data=f"{callback_prefix}:page:{page - 1}"
                )
            )
        else:
            # Пустая кнопка-spacer для центрирования верстки
            nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

        nav_row.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )

        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    "Дальше →", callback_data=f"{callback_prefix}:page:{page + 1}"
                )
            )
        else:
            nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

        keyboard.append(nav_row)

    # ВАЖНО: этот return должен быть на уровне самого первого if (вне блока if total_pages > 1)
    return InlineKeyboardMarkup(keyboard)