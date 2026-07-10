"""Тесты TokenPool — выбор токена, раздельные лимиты REST/GraphQL, блокировки."""

import time

import pytest

from src.github.auth import TokenHandle, TokenPool


def test_pool_requires_at_least_one_token():
    with pytest.raises(ValueError):
        TokenPool([])


def test_size_reports_token_count():
    pool = TokenPool(["t1", "t2", "t3"])
    assert pool.size == 3


@pytest.mark.asyncio
async def test_acquire_returns_handle_for_single_fresh_token():
    pool = TokenPool(["t1"])
    handle = await pool.acquire("rest")
    assert isinstance(handle, TokenHandle)
    assert handle.value == "t1"


@pytest.mark.asyncio
async def test_acquire_prefers_higher_known_remaining():
    pool = TokenPool(["low", "high"])
    now = time.time()
    await pool.report_usage("low", "rest", remaining=10, reset_at=now + 3600)
    await pool.report_usage("high", "rest", remaining=500, reset_at=now + 3600)

    handle = await pool.acquire("rest")
    assert handle.value == "high"


@pytest.mark.asyncio
async def test_acquire_prefers_never_used_token_over_known_low_remaining():
    pool = TokenPool(["known", "fresh"])
    now = time.time()
    await pool.report_usage("known", "rest", remaining=10, reset_at=now + 3600)
    # "fresh" ни разу не использовался -> remaining=None -> трактуется как
    # неограниченно доступный и предпочитается токену с известным низким остатком.
    handle = await pool.acquire("rest")
    assert handle.value == "fresh"


@pytest.mark.asyncio
async def test_exhausted_token_skipped_in_favor_of_available_one():
    pool = TokenPool(["exhausted", "ok"])
    now = time.time()
    await pool.report_usage("exhausted", "rest", remaining=0, reset_at=now + 3600)
    await pool.report_usage("ok", "rest", remaining=50, reset_at=now + 3600)

    handle = await pool.acquire("rest")
    assert handle.value == "ok"


@pytest.mark.asyncio
async def test_rest_and_graphql_limits_tracked_independently():
    pool = TokenPool(["t1"])
    now = time.time()
    await pool.report_usage("t1", "rest", remaining=0, reset_at=now + 3600)
    await pool.report_usage("t1", "graphql", remaining=500, reset_at=now + 3600)

    rest_remaining, _ = await pool.peek("t1", "rest")
    graphql_remaining, _ = await pool.peek("t1", "graphql")
    assert rest_remaining == 0
    assert graphql_remaining == 500


@pytest.mark.asyncio
async def test_all_exhausted_falls_back_to_token_with_soonest_reset():
    pool = TokenPool(["late", "soon"])
    now = time.time()
    await pool.report_usage("late", "rest", remaining=0, reset_at=now + 3600)
    await pool.report_usage("soon", "rest", remaining=0, reset_at=now + 60)

    handle = await pool.acquire("rest")
    assert handle.value == "soon"


@pytest.mark.asyncio
async def test_handle_report_updates_pool_state():
    pool = TokenPool(["t1"])
    handle = await pool.acquire("graphql")
    await handle.report(remaining=123, reset_at=999.0)

    remaining, reset_at = await pool.peek("t1", "graphql")
    assert remaining == 123
    assert reset_at == 999.0


@pytest.mark.asyncio
async def test_block_removes_token_from_rotation():
    pool = TokenPool(["bad", "good"])
    await pool.block("bad", reason="401 unauthorized", duration=3600)

    for _ in range(5):
        handle = await pool.acquire("rest")
        assert handle.value == "good"


@pytest.mark.asyncio
async def test_block_lazily_expires_after_duration(monkeypatch):
    pool = TokenPool(["t1"])
    now = time.time()
    await pool.block("t1", reason="401", duration=10)

    monkeypatch.setattr("src.github.auth.time.time", lambda: now + 20)

    # Блокировка истекла -> токен снова доступен как основной кандидат.
    handle = await pool.acquire("rest")
    assert handle.value == "t1"


@pytest.mark.asyncio
async def test_peek_unknown_token_returns_none_and_zero():
    pool = TokenPool(["t1"])
    remaining, reset_at = await pool.peek("does-not-exist", "rest")
    assert remaining is None
    assert reset_at == 0.0


def test_snapshot_masks_token_values():
    pool = TokenPool(["supersecrettoken"])
    snapshot = pool.snapshot()
    (masked_key,) = snapshot.keys()

    assert "supersecrettoken" not in masked_key
    assert masked_key.startswith("supersec")  # token[:8]
    assert snapshot[masked_key]["rest_remaining"] is None
    assert snapshot[masked_key]["graphql_remaining"] is None
    assert snapshot[masked_key]["blocked"] is None