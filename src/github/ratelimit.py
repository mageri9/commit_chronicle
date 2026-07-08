"""
RateLimiter — стратегия "ждать или идти" поверх TokenPool.

Разделение ответственности:
    TokenPool  — хранит состояние токенов и умеет выбрать лучший.
    RateLimiter — решает, достаточно ли лимита у выбранного токена
                  ПРЯМО СЕЙЧАС, и если нет — ждёт до reset_at (с потолком),
                  прежде чем отдать токен вызывающему коду.

Смысл: воркер (и даже GitHubClient) не должен думать о лимитах вообще —
он просто вызывает limiter.acquire() и получает готовый к использованию
токен, либо (в крайнем случае) ждёт разумное время.

Важно: это НЕ встроено в retry-цикл GitHubClient (см. client.py) —
там нужен быстрый fail-fast с явной ошибкой наверх, а не сон на
reset_at внутри уже начатого запроса. RateLimiter подключается на
уровне пайплайна (Акт 3 / service.py) как admission control перед
стартом партии запросов — это другая ответственность.
"""

from __future__ import annotations

import asyncio
import re
import time

from src.github.auth import ApiKind, TokenHandle, TokenPool
from src.logger import get_logger


logger = get_logger(__name__)

# Официальная формула GitHub для GraphQL rate limit cost:
# https://docs.github.com/en/graphql/overview/rate-limits-and-node-limits-for-the-graphql-api
# "each first or last argument... adds (value / 100), minimum 1, per connection".
# Это не точный парсер AST — регулярка ищет все first:/last: в тексте запроса.
# Используется ТОЛЬКО как pre-flight эвристика, чтобы не пытаться идти
# уже истощённым токеном. Настоящая цена приходит от GitHub в ответе
# (rateLimit.cost) и именно она пишется в TokenPool через client.py.
_CONNECTION_ARG_RE = re.compile(r"(?:first|last)\s*:\s*(\d+)")


def estimate_graphql_cost(query: str) -> int:
    """
    Грубая оценка стоимости GraphQL-запроса до отправки.

    Ограничения (сознательно, не скрываем):
      - не учитывает фактическую вложенность полей и переиспользование
        объектов между connection'ами;
      - не учитывает alias-батчинг из Акта 3.5 — там потребуется более
        точный расчёт, это отдельный будущий квест;
      - если в запросе нет first/last (например, простой viewer{login}),
        возвращает 1 — минимально возможную стоимость.

    Никогда не используется как источник истины для TokenPool — только
    как основание для "стоит ли вообще пробовать этим токеном сейчас".
    """
    matches = _CONNECTION_ARG_RE.findall(query)
    if not matches:
        return 1
    return max(1, sum(max(1, int(value) // 100) for value in matches))


class RateLimiter:
    """
    Использование:
        limiter = RateLimiter(token_pool)
        handle = await limiter.acquire("graphql", estimated_cost=5)
        # ... запрос ...
        await handle.report(remaining=..., reset_at=...)

    min_remaining_buffer — не тратим лимит "под ноль": если после запроса
    останется меньше буфера, предпочитаем подождать более свежий токен
    (или reset), чтобы не спровоцировать secondary rate limit / abuse
    detection от GitHub, которая срабатывает и на легитимный, но слишком
    плотный трафик.

    max_wait_seconds — потолок одного ожидания. Если и после этого лимит
    не восстановился (например, множественные токены исчерпаны надолго),
    вызывающий код получит токен всё равно и естественным образом поймает
    RateLimitExceeded от GitHubClient — лучше явная ошибка, чем зависший
    воркер на час.
    """

    def __init__(
        self,
        pool: TokenPool,
        *,
        min_remaining_buffer: int = 50,
        max_wait_seconds: float = 300.0,
    ) -> None:
        self._pool = pool
        self._buffer = min_remaining_buffer
        self._max_wait = max_wait_seconds

    async def acquire(self, api: ApiKind, *, estimated_cost: int = 1) -> TokenHandle:
        """
        Вернуть токен, у которого (по последним известным данным)
        точно хватит лимита на estimated_cost. При необходимости ждёт,
        но не дольше max_wait_seconds за одну попытку.
        """
        while True:
            handle = await self._pool.acquire(api)
            remaining, reset_at = await self._pool.peek(handle.value, api)
            wait_for = self._compute_wait(remaining, reset_at, estimated_cost)

            if wait_for <= 0:
                return handle

            sleep_for = min(wait_for, self._max_wait)
            logger.info(
                f"rate limiter: жду {sleep_for:.1f}s перед {api} "
                f"(remaining={remaining}, estimated_cost={estimated_cost})"
            )
            await asyncio.sleep(sleep_for)

    def _compute_wait(
        self, remaining: int | None, reset_at: float, estimated_cost: int
    ) -> float:
        """
        0.0 — можно идти прямо сейчас.
        >0.0 — сколько секунд подождать (потолок max_wait применяется
        в acquire(), не здесь — чтобы _compute_wait оставался чистой
        функцией без побочных эффектов, это упрощает тестирование).
        """
        if remaining is None:
            return 0.0

        if remaining - self._buffer >= estimated_cost:
            return 0.0

        now = time.time()
        if reset_at <= now:
            return 0.0

        return reset_at - now