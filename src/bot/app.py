"""Фабрика Telegram-приложения."""

from telegram.ext import Application
from src.config import settings


def create_app() -> Application:
    """Создать и вернуть Application."""
    return Application.builder().token(settings.telegram_bot_token).build()