"""
Тесты GitHubClient (src/github/client.py) поверх httpx.MockTransport —
никаких реальных сетевых обращений. Проверяем маршрутизацию ответов
GitHub в нужные исключения и репортинг лимитов в TokenPool.
"""

import time

import httpx
import pytest

from src.github.auth import TokenPool
from src.github.client import GitHubClient
from src.github.exceptions import (
    AuthenticationError,
    GitHubAPIError,
    GraphQLError,
    NodeLimitExceeded,
    RateLimitExceeded,
)


def make_client(handler, tokens=("token-a",)):
    """transport=... отключает HTTP/2 внутри GitHubClient (см. client.py),
    что и требуется для httpx.MockTransport."""
    pool = TokenPool(list(tokens))
    transport = httpx.MockTransport(handler)
    client = GitHubClient(pool, transport=transport)
    return client, pool


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_get_success_returns_json_and_reports_limit():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer token-a"
        return httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "9999999999"},
        )

    client, pool = make_client(handler)
    try:
        data = await client.get("/user")
        assert data == {"login": "octocat"}

        remaining, reset_at = await pool.peek("token-a", "rest")
        assert remaining == 42
        assert reset_at == 9999999999.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rest_401_raises_authentication_error_after_max_retries():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(401, text="Bad credentials")

    client, pool = make_client(handler)
    try:
        with pytest.raises(AuthenticationError):
            await client.get("/user")
        assert call_count["n"] == 3  # _MAX_AUTH_RETRIES
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rest_401_falls_back_to_next_working_token():
    seen_tokens = []

    def handler(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        seen_tokens.append(token)
        if token == "bad":
            return httpx.Response(401, text="Bad credentials")
        return httpx.Response(200, json={"ok": True})

    client, pool = make_client(handler, tokens=("bad", "good"))
    try:
        data = await client.get("/user")
        assert data == {"ok": True}
        assert seen_tokens[0] == "bad"
        assert "good" in seen_tokens
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rest_403_primary_rate_limit_raises_with_reset_at():
    def handler(request):
        return httpx.Response(
            403,
            text="API rate limit exceeded",
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123456"},
        )

    client, pool = make_client(handler)
    try:
        with pytest.raises(RateLimitExceeded) as exc_info:
            await client.get("/user")
        assert exc_info.value.reset_at == 123456.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rest_429_secondary_rate_limit_uses_retry_after():
    def handler(request):
        return httpx.Response(
            429, text="secondary rate limit", headers={"Retry-After": "5"}
        )

    client, pool = make_client(handler)
    try:
        before = time.time()
        with pytest.raises(RateLimitExceeded) as exc_info:
            await client.get("/user")
        assert exc_info.value.reset_at >= before + 5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rest_404_raises_immediately_without_retry():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(404, text="Not Found")

    client, pool = make_client(handler)
    try:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.get("/repos/x/y")
        assert exc_info.value.status_code == 404
        assert call_count["n"] == 1  # не rate-limit/auth -> без retry
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graphql_success_reports_rate_limit_from_body():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {"login": "octocat"},
                    "rateLimit": {
                        "remaining": 4999,
                        "resetAt": "2026-01-01T00:00:00Z",
                        "cost": 1,
                    },
                }
            },
        )

    client, pool = make_client(handler)
    try:
        data = await client.graphql(
            "query { viewer { login } rateLimit { remaining resetAt cost } }"
        )
        assert data["viewer"] == {"login": "octocat"}

        remaining, _ = await pool.peek("token-a", "graphql")
        assert remaining == 4999
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_graphql_node_limit_exceeded_raises_immediately():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "errors": [
                    {"type": "MAX_NODE_LIMIT_EXCEEDED", "message": "too many nodes"}
                ]
            },
        )

    client, pool = make_client(handler)
    try:
        with pytest.raises(NodeLimitExceeded):
            await client.graphql("query { x }")
        assert call_count["n"] == 1  # не ретраится — нужно уменьшить запрос
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_graphql_rate_limited_error_retried_then_raises():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(
            200, json={"errors": [{"type": "RATE_LIMITED", "message": "rate limited"}]}
        )

    client, pool = make_client(handler)
    try:
        with pytest.raises(RateLimitExceeded):
            await client.graphql("query { x }")
        assert call_count["n"] == 3  # _MAX_AUTH_RETRIES
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_graphql_generic_error_raises_immediately():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(
            200, json={"errors": [{"type": "NOT_FOUND", "message": "no such repo"}]}
        )

    client, pool = make_client(handler)
    try:
        with pytest.raises(GraphQLError) as exc_info:
            await client.graphql("query { x }")
        assert "no such repo" in str(exc_info.value)
        assert call_count["n"] == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_graphql_401_blocks_token_and_retries_other():
    seen_tokens = []

    def handler(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        seen_tokens.append(token)
        if token == "bad":
            return httpx.Response(401, text="Bad credentials")
        return httpx.Response(200, json={"data": {"ok": True}})

    client, pool = make_client(handler, tokens=("bad", "good"))
    try:
        data = await client.graphql("query { ok }")
        assert data == {"ok": True}
        assert "good" in seen_tokens
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_graphql_transport_error_raises_github_api_error():
    def handler(request):
        return httpx.Response(500, text="server error")

    client, pool = make_client(handler)
    try:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.graphql("query { x }")
        assert exc_info.value.status_code == 500
    finally:
        await client.aclose()