"""Настройка логирования через loguru."""

import sys
from pathlib import Path
from loguru import logger


def setup_logging():
    """Настроить loguru: консоль с цветами + файлы с ротацией."""
    logger.remove()

    logs_dir = Path(__file__).parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    logger.add(
        logs_dir / "app_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        compression="gz",
        level="DEBUG",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
    )

    logger.add(
        logs_dir / "errors_{time_YYYY-MM-DD}.log",
        rotation="00:00",
        retention="14 days",
        level="ERROR",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}: {message}",
    )

    logger.add(
        sys.stderr,
        level="INFO",
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>: <level>{message}</level>",
    )

    return logger


def get_logger(name: str):
    """Вернуть логгер для модуля (для совместимости с logging-стилем)."""
    return logger.bind(name=name)