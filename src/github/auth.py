"""
Token Pool — хранилище состояния GitHub-токенов и выбор лучшего для запроса.

Отличия от старого src/core/token_rotator.py:
    - round-robin заменён на "выбрать токен с наибольшим известным remaining";
    - REST и GraphQL учитываются раздельно — у них разные бюджеты
      (5000 req/h для REST, отдельный points-бюджет для GraphQL)
      и разное время сброса;
    - блокировка токена при rate-limit хранит реальный reset_at
      (из ответа GitHub), а не фиксированный duration вслепую;
    - блокировка при auth-ошибке (невалидный токен) — отдельный
      механизм, т.к. она не снимается автоматическим reset'ом лимита.

Пул не знает про HTTP — это чистое хранилище состояния с политикой
выбора. Решение "надо ли сейчас ждать перед запросом и сколько" —
зона ответственности ratelimit.py, который работает поверх этого пула.
"""

from __future__ import annotations

import time
from asyncio import Lock
from dataclasses import dataclass, field
from typing import Literal

ApiKind = Literal["rest", "graphql"]


@dataclass
class _RateWindow:
    """Известное состояние лимита для одного API одного токена."""

    remaining: int | None = None  # None = ещё не знаем, считаем токен доступным
    reset_at: float = 0.0  # unix-время, когда лимит обновится


@dataclass
class TokenState:
    """Полное состояние одного токена в пуле."""

    value: str
    rest: _RateWindow = field(default_factory=_RateWindow)
    graphql: _RateWindow = field(default_factory=_RateWindow)
    blocked_until: float | None = None  # см. AuthenticationError, не rate-limit
    blocked_reason: str | None = None

    def window(self, api: ApiKind) -> _RateWindow:
        return self.rest if api == "rest" else self.graphql

    def is_blocked(self, now: float) -> bool:
        """Заблокирован ли токен целиком (auth-ошибка)."""
        if self.blocked_until is None:
            return False
        if now >= self.blocked_until:
            # Блокировка истекла — снимаем лениво, при первой же проверке
            self.blocked_until = None
            self.blocked_reason = None
            return False
        return True

    def is_exhausted(self, api: ApiKind, now: float) -> bool:
        """Известно, что лимит исчерпан и ещё не наступило время сброса."""
        w = self.window(api)
        if w.remaining is None:
            return False
        if w.remaining > 0:
            return False
        return now < w.reset_at


class TokenHandle:
    """
    Обёртка вокруг выбранного токена для вызывающего кода (client.py).

    Несёт .value для подстановки в заголовок Authorization и ссылку
    на пул, чтобы после запроса одним вызовом отчитаться об использовании
    или сообщить о блокировке — без явной передачи токена туда-обратно.
    """

    __slots__ = ("value", "_pool", "_api")

    def __init__(self, value: str, pool: "TokenPool", api: ApiKind) -> None:
        self.value = value
        self._pool = pool
        self._api = api

    async def report(self, *, remaining: int, reset_at: float) -> None:
        """Обновить известное состояние лимита после успешного запроса."""
        await self._pool.report_usage(
            self.value, self._api, remaining=remaining, reset_at=reset_at
        )

    async def block(self, *, reason: str, duration: float = 3600.0) -> None:
        """Заблокировать токен целиком (например, после AuthenticationError)."""
        await self._pool.block(self.value, reason=reason, duration=duration)


class TokenPool:
    """
    Пул GitHub-токенов с раздельным учётом лимитов REST и GraphQL.

    Использование:
        pool = TokenPool(settings.all_github_tokens)
        handle = await pool.acquire("graphql")
        # ... делаем запрос с Authorization: Bearer {handle.value} ...
        await handle.report(remaining=..., reset_at=...)
    """

    def __init__(self, tokens: list[str]) -> None:
        if not tokens:
            raise ValueError("Нужен хотя бы один GitHub токен")
        self._states: dict[str, TokenState] = {t: TokenState(value=t) for t in tokens}
        self._lock = Lock()

    @property
    def size(self) -> int:
        return len(self._states)

    async def acquire(self, api: ApiKind = "rest") -> TokenHandle:
        """
        Вернуть токен с наибольшим известным remaining для данного API.

        Токены, которые ещё ни разу не использовались (remaining неизвестен),
        считаются доступными и предпочитаются токенам с точно исчерпанным
        лимитом — так пул естественным образом сначала пробует "свежие"
        токены, прежде чем полагаться на неполные данные о старых.

        Если все токены сейчас недоступны (заблокированы или лимит
        исчерпан) — возвращается тот, что освободится раньше всех.
        Вызывающий код (ratelimit.py) решает, ждать ли и сколько.
        """
        async with self._lock:
            now = time.time()
            candidates = [
                s
                for s in self._states.values()
                if not s.is_blocked(now) and not s.is_exhausted(api, now)
            ]

            if candidates:
                best = max(
                    candidates,
                    key=lambda s: (
                        s.window(api).remaining
                        if s.window(api).remaining is not None
                        else float("inf")
                    ),
                )
                return TokenHandle(best.value, self, api)

            soonest = min(
                self._states.values(),
                key=lambda s: max(s.blocked_until or 0.0, s.window(api).reset_at),
            )
            return TokenHandle(soonest.value, self, api)

    async def report_usage(
        self, token: str, api: ApiKind, *, remaining: int, reset_at: float
    ) -> None:
        """Обновить известное состояние токена после реального запроса."""
        async with self._lock:
            state = self._states.get(token)
            if state is None:
                return
            window = state.window(api)
            window.remaining = remaining
            window.reset_at = reset_at

    async def block(self, token: str, *, reason: str, duration: float = 3600.0) -> None:
        """
        Заблокировать токен целиком — используется для ошибок, которые
        rate-limit'ом не являются (невалидный/отозванный токен). В отличие
        от исчерпания лимита, такая блокировка не снимается автоматическим
        reset_at от GitHub, поэтому используется ручной duration.
        """
        async with self._lock:
            state = self._states.get(token)
            if state is None:
                return
            state.blocked_until = time.time() + duration
            state.blocked_reason = reason

    def snapshot(self) -> dict[str, dict]:
        """
        Текущее состояние всех токенов для логов/дебага.
        Токены маскируются — в логи не должен попадать полный секрет.
        """
        return {
            f"{token[:8]}…": {
                "rest_remaining": s.rest.remaining,
                "graphql_remaining": s.graphql.remaining,
                "blocked": s.blocked_reason,
            }
            for token, s in self._states.items()
        }