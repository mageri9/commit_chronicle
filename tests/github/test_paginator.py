"""Тесты курсорного пагинатора (src/github/paginator.py)."""

import pytest

from src.github.paginator import PaginationError, collect_all, paginate


def make_pages(pages):
    """pages: [(nodes, has_next, end_cursor), ...] -> (fetch_page, calls_log)."""
    calls = []

    async def fetch_page(after):
        calls.append(after)
        nodes, has_next, end_cursor = pages[len(calls) - 1]
        return {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
        }

    return fetch_page, calls


@pytest.mark.asyncio
async def test_paginate_single_page():
    fetch, calls = make_pages([([{"id": 1}, {"id": 2}], False, None)])
    result = [n async for n in paginate(fetch)]

    assert result == [{"id": 1}, {"id": 2}]
    assert calls == [None]


@pytest.mark.asyncio
async def test_paginate_multiple_pages_passes_cursor_forward():
    fetch, calls = make_pages(
        [
            ([{"id": 1}], True, "cursor1"),
            ([{"id": 2}], True, "cursor2"),
            ([{"id": 3}], False, None),
        ]
    )
    result = [n async for n in paginate(fetch)]

    assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert calls == [None, "cursor1", "cursor2"]


@pytest.mark.asyncio
async def test_paginate_skips_none_nodes():
    fetch, _ = make_pages([([{"id": 1}, None, {"id": 2}], False, None)])
    result = [n async for n in paginate(fetch)]
    assert result == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_paginate_stops_when_has_next_but_cursor_missing():
    fetch, calls = make_pages([([{"id": 1}], True, None), ([{"id": 2}], False, None)])
    result = [n async for n in paginate(fetch)]

    # Противоречивый ответ (hasNextPage=True, endCursor=None) -> паджинатор
    # останавливается, не уходя во второй запрос.
    assert result == [{"id": 1}]
    assert calls == [None]


@pytest.mark.asyncio
async def test_paginate_handles_missing_nodes_key():
    async def fetch(after):
        return {"pageInfo": {"hasNextPage": False, "endCursor": None}}

    result = [n async for n in paginate(fetch)]
    assert result == []


@pytest.mark.asyncio
async def test_paginate_raises_on_page_limit_exceeded():
    async def infinite_fetch(after):
        return {
            "nodes": [{"id": 1}],
            "pageInfo": {"hasNextPage": True, "endCursor": "next"},
        }

    with pytest.raises(PaginationError):
        async for _ in paginate(infinite_fetch, max_pages=3):
            pass


@pytest.mark.asyncio
async def test_collect_all_materializes_full_list():
    fetch, _ = make_pages([([{"id": 1}], True, "c1"), ([{"id": 2}], False, None)])
    result = await collect_all(fetch)
    assert result == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_collect_all_on_empty_connection():
    fetch, _ = make_pages([([], False, None)])
    result = await collect_all(fetch)
    assert result == []