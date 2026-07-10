"""Тесты RateLimiter и эвристики estimate_graphql_cost (src/github/ratelimit.py)."""

import time

import pytest

from src.github.auth import TokenPool
from src.github.ratelimit import RateLimiter, estimate_graphql_cost


# ---------------------------------------------------------------------------
# estimate_graphql_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_defaults_to_one_without_connection_args():
    query = "query { viewer { login } }"
    assert estimate_graphql_cost(query) == 1


def test_estimate_cost_first_100_is_one_unit():
    query = "query { repos(first: 100) { nodes { name } } }"
    assert estimate_graphql_cost(query) == 1


def test_estimate_cost_first_250_rounds_down_to_two_units():
    query = "query { repos(first: 250) { nodes { name } } }"
    assert estimate_graphql_cost(query) == 2


def test_estimate_cost_sums_multiple_connections():
    query = (
        "query { a: repos(first: 100) { nodes { name } } "
        "b: repos(last: 300) { nodes { name } } }"
    )
    assert estimate_graphql_cost(query) == 1 + 3


def test_estimate_cost_never_below_one_per_connection():
    query = "query { repos(first: 1) { nodes { name } } }"
    assert estimate_graphql_cost(query) == 1


# ---------------------------------------------------------------------------
# RateLimiter._compute_wait — чистая функция, без sleep/side effects
# ---------------------------------------------------------------------------


def _limiter(buffer=50):
    pool = TokenPool(["t1"])
    return RateLimiter(pool, min_remaining_buffer=buffer)


def test_compute_wait_unknown_remaining_goes_immediately():
    limiter = _limiter()
    assert limiter._compute_wait(None, 0.0, estimated_cost=5) == 0.0


def test_compute_wait_enough_remaining_goes_immediately():
    limiter = _limiter(buffer=50)
    now = time.time()
    assert limiter._compute_wait(200, now + 3600, estimated_cost=5) == 0.0


def test_compute_wait_insufficient_but_reset_already_passed():
    limiter = _limiter(buffer=50)
    now = time.time()
    assert limiter._compute_wait(10, now - 1, estimated_cost=5) == 0.0


def test_compute_wait_insufficient_and_not_yet_reset():
    limiter = _limiter(buffer=50)
    now = time.time()
    reset_at = now + 30
    wait = limiter._compute_wait(10, reset_at, estimated_cost=5)
    assert 29 < wait <= 30


# ---------------------------------------------------------------------------
# RateLimiter.acquire — event loop, реальный sleep подменяется fake_sleep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_returns_immediately_when_enough_remaining():
    pool = TokenPool(["t1"])
    await pool.report_usage("t1", "graphql", remaining=100, reset_at=time.time() + 3600)
    limiter = RateLimiter(pool, min_remaining_buffer=10)

    handle = await limiter.acquire("graphql", estimated_cost=5)
    assert handle.value == "t1"


@pytest.mark.asyncio
async def test_acquire_sleeps_then_succeeds_once_limit_resets(monkeypatch):
    pool = TokenPool(["t1"])
    now = time.time()
    reset_at = now + 5
    await pool.report_usage("t1", "graphql", remaining=0, reset_at=reset_at)

    limiter = RateLimiter(pool, min_remaining_buffer=10, max_wait_seconds=60)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        # симулируем наступление reset_at и восстановление лимита
        await pool.report_usage(
            "t1", "graphql", remaining=500, reset_at=reset_at + 3600
        )

    monkeypatch.setattr("src.github.ratelimit.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("src.github.ratelimit.time.time", lambda: now)

    handle = await limiter.acquire("graphql", estimated_cost=5)

    assert handle.value == "t1"
    assert len(slept) == 1
    assert slept[0] == pytest.approx(5, abs=0.01)


@pytest.mark.asyncio
async def test_acquire_wait_capped_by_max_wait_seconds(monkeypatch):
    pool = TokenPool(["t1"])
    now = time.time()
    reset_at = now + 10_000  # намного больше max_wait_seconds лимитера
    await pool.report_usage("t1", "rest", remaining=0, reset_at=reset_at)

    limiter = RateLimiter(pool, min_remaining_buffer=10, max_wait_seconds=1.0)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        # разблокируем токен, чтобы тест не завис в бесконечном цикле
        await pool.report_usage("t1", "rest", remaining=500, reset_at=reset_at)

    monkeypatch.setattr("src.github.ratelimit.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("src.github.ratelimit.time.time", lambda: now)

    await limiter.acquire("rest", estimated_cost=5)

    assert slept == [1.0]  # min(wait_for=10000, max_wait_seconds=1.0)