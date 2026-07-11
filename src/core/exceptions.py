"""Кастомные исключения сборщика."""


class CollectorError(Exception):
    """Базовая ошибка сборщика."""

    pass


class RateLimitError(CollectorError):
    """Лимит API исчерпан (429)."""

    def __init__(self, message: str, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(message)


class TokenExhaustedError(CollectorError):
    """Все GitHub-токены исчерпаны."""

    def __init__(self, message: str = "Все GitHub-токены исчерпаны"):
        super().__init__(message)