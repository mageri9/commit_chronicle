"""
Round-robin распределение GitHub токенов.
Не проверяет remaining. Реагирует на 429 — блокирует токен на час.
"""

import time
from threading import Lock
from github import Github, Auth
from src.core.exceptions import TokenExhaustedError


class TokenRotator:
    """Равномерное распределение запросов по токенам с блокировкой мёртвых."""

    def __init__(self, tokens: list[str]) -> None:
        if not tokens:
            raise ValueError("Нужен хотя бы один GitHub токен")
        self._clients = [Github(auth=Auth.Token(t)) for t in tokens]
        self._index = 0
        self._lock = Lock()
        self._blocked: dict[int, float] = {}  # index → unblock_time

    @property
    def count(self) -> int:
        return len(self._clients)

    def _client_index(self, client: Github) -> int | None:
        """Найти индекс клиента в списке."""
        for i, c in enumerate(self._clients):
            if c is client:
                return i
        return None

    def block_token(self, client: Github, duration: int = 3600) -> None:
        """Заблокировать токен на duration секунд (после 429)."""
        idx = self._client_index(client)
        if idx is not None:
            self._blocked[idx] = time.time() + duration

    def _is_blocked(self, idx: int) -> bool:
        """Проверить, заблокирован ли токен."""
        if idx not in self._blocked:
            return False
        if time.time() > self._blocked[idx]:
            del self._blocked[idx]
            return False
        return True

    def get_client(self) -> Github | None:
        """Выдаёт клиента, пропуская заблокированные токены."""
        with self._lock:
            for _ in range(len(self._clients)):
                self._index = (self._index + 1) % len(self._clients)
                if not self._is_blocked(self._index):
                    return self._clients[self._index]
        raise TokenExhaustedError("Все GitHub-токены заблокированы")


# Глобальный экземпляр — создаётся один раз
from src.config import settings

token_rotator = TokenRotator(settings.all_github_tokens)