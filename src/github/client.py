"""
GitHubClient — единственная точка HTTP-доступа к GitHub API.

Не знает ничего про репозитории, коммиты или бизнес-логику — только про
то, как сходить в REST/GraphQL, подставить подходящий токен из TokenPool
и корректно среагировать на rate-limit/auth-ошибки. Всё, что выше
(graphql.py, rest.py, service.py), работает через client.get/post/graphql
и не думает о токенах, ретраях или заголовках.

Один экземпляр на процесс — переиспользует HTTP/2-соединения между
всеми вызовами (см. get_github_client() в конце файла).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from src.github.auth import TokenHandle, TokenPool
from src.github.exceptions import (
    AuthenticationError,
    GitHubAPIError,
    GraphQLError,
    NodeLimitExceeded,
    RateLimitExceeded,
)
from src.logger import get_logger

logger = get_logger(__name__)

REST_BASE_URL = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"

# Сколько раз пробовать другой токен при auth-ошибке/rate-limit,
# прежде чем сдаться и пробросить ошибку наверх.
_MAX_AUTH_RETRIES = 3


class GitHubClient:
    """
    Транспортный клиент GitHub API (REST + GraphQL) поверх httpx/HTTP2.

    Использование:
        client = GitHubClient(token_pool)
        data = await client.graphql(query, variables)
        payload = await client.get("/rate_limit")
    """

    def __init__(
        self,
        token_pool: TokenPool,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._pool = token_pool
        self._http = httpx.AsyncClient(
            # http2 несовместим с явно переданным mock-транспортом (тесты) —
            # в проде transport всегда None, и HTTP/2 включается как обычно.
            http2=transport is None,
            timeout=timeout,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------

    async def get(self, endpoint: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET к REST API. endpoint — путь без базового URL, напр. '/rate_limit'."""
        return await self._request_rest("GET", endpoint, params=params)

    async def post(
        self, endpoint: str, *, json_body: dict[str, Any] | None = None
    ) -> Any:
        """POST к REST API."""
        return await self._request_rest("POST", endpoint, json_body=json_body)

    async def _request_rest(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = endpoint if endpoint.startswith("http") else f"{REST_BASE_URL}{endpoint}"

        last_error: GitHubAPIError | None = None
        for _ in range(_MAX_AUTH_RETRIES):
            handle = await self._pool.acquire("rest")
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {handle.value}"},
            )

            await self._report_rest_limit(handle, response)

            if response.status_code == 401:
                await handle.block(reason="401 unauthorized")
                last_error = AuthenticationError(
                    "Токен отклонён (401)",
                    status_code=401,
                    response_body=response.text,
                )
                continue

            if response.status_code == 403 and _is_rate_limit_response(response):
                last_error = RateLimitExceeded(
                    "REST rate limit исчерпан",
                    reset_at=_parse_reset_header(response),
                    status_code=403,
                    response_body=response.text,
                )
                continue

            if response.status_code == 429:
                last_error = RateLimitExceeded(
                    "Secondary rate limit (429)",
                    reset_at=_parse_retry_after(response),
                    status_code=429,
                    response_body=response.text,
                )
                continue

            if response.status_code >= 400:
                # Сюда попадают permission-ошибки (403 без rate-limit),
                # 404 и прочее — это НЕ retry-friendly ошибки, бизнес-слой
                # (collector) решает по status_code, что с ними делать.
                raise GitHubAPIError(
                    f"GitHub REST error {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text,
                )

            return response.json() if response.content else None

        assert last_error is not None
        raise last_error

    async def _report_rest_limit(
        self, handle: TokenHandle, response: httpx.Response
    ) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is not None and reset is not None:
            await handle.report(remaining=int(remaining), reset_at=float(reset))

    # ------------------------------------------------------------------
    # GraphQL
    # ------------------------------------------------------------------

    async def graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Выполнить GraphQL-запрос. Возвращает содержимое "data".

        Если query включает блок `rateLimit { remaining resetAt }` —
        клиент прочитает его из ответа и обновит TokenPool. Осознанное
        решение: GraphQL не отдаёт лимиты в заголовках (в отличие от REST),
        поэтому запрашивающий query сам решает, включать ли этот блок
        (см. src/github/queries.py).
        """
        last_error: GitHubAPIError | None = None

        for _ in range(_MAX_AUTH_RETRIES):
            handle = await self._pool.acquire("graphql")
            response = await self._http.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {handle.value}"},
            )

            if response.status_code == 401:
                await handle.block(reason="401 unauthorized")
                last_error = AuthenticationError(
                    "Токен отклонён (401)",
                    status_code=401,
                    response_body=response.text,
                )
                continue

            if response.status_code == 429:
                last_error = RateLimitExceeded(
                    "Secondary rate limit (429)",
                    reset_at=_parse_retry_after(response),
                    status_code=429,
                    response_body=response.text,
                )
                continue

            if response.status_code >= 400:
                raise GitHubAPIError(
                    f"GitHub GraphQL transport error {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text,
                )

            body = response.json()

            if body.get("errors"):
                errors = body["errors"]
                error_types = {e.get("type") for e in errors}
                message = "; ".join(e.get("message", "") for e in errors)

                if "MAX_NODE_LIMIT_EXCEEDED" in error_types:
                    raise NodeLimitExceeded(
                        message or "GraphQL node limit exceeded",
                        errors=errors,
                        query=query,
                    )

                if "RATE_LIMITED" in error_types:
                    last_error = RateLimitExceeded(message or "GraphQL rate limit")
                    continue

                raise GraphQLError(
                    message or "GraphQL error", errors=errors, query=query
                )

            data = body.get("data") or {}
            await self._maybe_report_graphql_limit(handle, data)
            return data

        assert last_error is not None
        raise last_error

    async def _maybe_report_graphql_limit(
        self, handle: TokenHandle, data: dict[str, Any]
    ) -> None:
        rate_limit = data.get("rateLimit")
        if not rate_limit:
            return
        remaining = rate_limit.get("remaining")
        reset_at_str = rate_limit.get("resetAt")
        if remaining is None or reset_at_str is None:
            return
        await handle.report(
            remaining=int(remaining), reset_at=_parse_iso_timestamp(reset_at_str)
        )


# ---------------------------------------------------------------------------
# Вспомогательные парсеры ответов
# ---------------------------------------------------------------------------


def _is_rate_limit_response(response: httpx.Response) -> bool:
    return response.headers.get("X-RateLimit-Remaining") == "0"


def _parse_reset_header(response: httpx.Response) -> float | None:
    reset = response.headers.get("X-RateLimit-Reset")
    return float(reset) if reset is not None else None


def _parse_retry_after(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return None
    return time.time() + float(retry_after)


def _parse_iso_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# Синглтон на процесс (по аналогии с src/storage/redis.py)
# ---------------------------------------------------------------------------

_client: GitHubClient | None = None
_pool: TokenPool | None = None


async def get_github_client() -> GitHubClient:
    """Ленивая инициализация — один клиент и один пул токенов на процесс."""
    global _client, _pool
    if _client is None:
        from src.config import settings

        _pool = TokenPool(settings.all_github_tokens)
        _client = GitHubClient(_pool)
    return _client


async def close_github_client() -> None:
    """Закрыть клиент при graceful shutdown."""
    global _client, _pool
    if _client is not None:
        await _client.aclose()
        _client = None
        _pool = None
        logger.debug("GitHub client closed")