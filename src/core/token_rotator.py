"""
Round-robin распределение GitHub токенов.
Не контролирует лимиты. Не проверяет remaining.
Равномерно распределяет нагрузку между токенами.
"""

from threading import Lock
from github import Github, Auth


class TokenRotator:
    """Равномерное распределение запросов по токенам."""

    def __init__(self, tokens: list[str]) -> None:
        if not tokens:
            raise ValueError("Нужен хотя бы один GitHub токен")
        self._clients = [Github(auth=Auth.Token(t)) for t in tokens]
        self._index = 0
        self._lock = Lock()

    @property
    def count(self) -> int:
        return len(self._clients)

    def get_client(self) -> Github:
        """Выдаёт следующего клиента по кругу. Потокобезопасно."""
        with self._lock:
            client = self._clients[self._index]
            self._index = (self._index + 1) % len(self._clients)
        return client


# Глобальный экземпляр — создаётся один раз
from src.config import settings

token_rotator = TokenRotator(settings.all_github_tokens)