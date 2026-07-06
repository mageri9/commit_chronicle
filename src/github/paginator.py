"""
Курсорный пагинатор — общий для любых GraphQL connection'ов
(repositories, commit history и т.д.).

Не знает ничего про конкретный запрос или форму вложенности ответа —
принимает callable, который по курсору возвращает уже извлечённый
connection-объект {"pageInfo": {...}, "nodes": [...]}. Извлечение из
специфичной вложенности (user.repositories, repository.object.history)
— забота вызывающего кода (graphql.py), а не паджинатора. Это то, что
позволяет одному и тому же paginate() обслуживать оба запроса из
queries.py без единой строчки, знающей об их структуре.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from src.logger import get_logger

logger = get_logger(__name__)

# fetch_page(after) -> {"pageInfo": {"hasNextPage": bool, "endCursor": str | None}, "nodes": [...]}
FetchPage = Callable[[str | None], Awaitable[dict]]

# Защитный потолок: если hasNextPage почему-то всегда True (баг API,
# сломанный курсор, кольцевая пагинация) — не крутимся вечно и не жжём
# rate limit впустую, а падаем с понятной ошибкой вместо зависания.
_DEFAULT_MAX_PAGES = 1000


class PaginationError(Exception):
    """Пагинация превысила максимально разумное число страниц."""


async def paginate(
    fetch_page: FetchPage, *, max_pages: int = _DEFAULT_MAX_PAGES
) -> AsyncIterator[dict]:
    """
    Асинхронно отдаёт узлы (nodes) постранично, скрывая курсорную логику.

    Использование:
        async def fetch(after):
            data = await client.graphql(query, {"login": login, "after": after})
            return data["user"]["repositories"]

        async for node in paginate(fetch):
            ...

    Стримит узлы по мере получения страниц — не держит весь результат
    в памяти сразу, что важно для истории коммитов (потенциально тысячи
    узлов), в отличие от репозиториев (обычно десятки-сотни, см. collect_all).
    """
    after: str | None = None
    page_count = 0

    while True:
        page_count += 1
        if page_count > max_pages:
            raise PaginationError(
                f"Превышен лимит страниц ({max_pages}) — похоже на "
                f"бесконечную пагинацию (сломанный курсор или баг API)."
            )

        connection = await fetch_page(after)
        nodes = connection.get("nodes") or []

        for node in nodes:
            if node is not None:
                # GraphQL иногда отдаёт null-элементы в списках при
                # частичных ошибках (partial response) — пропускаем,
                # не роняя всю пагинацию из-за одного плохого узла.
                yield node

        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return

        after = page_info.get("endCursor")
        if after is None:
            # hasNextPage=True, но курсора нет — противоречивый ответ API.
            # Продолжать пагинацию некуда; лучше остановиться явно с
            # предупреждением в логах, чем уйти в бесконечный цикл с
            # after=None (что просто повторило бы первую страницу).
            logger.warning(
                "paginate: hasNextPage=True, но endCursor отсутствует — "
                "останавливаюсь, чтобы не зациклиться"
            )
            return


async def collect_all(
    fetch_page: FetchPage, *, max_pages: int = _DEFAULT_MAX_PAGES
) -> list[dict]:
    """
    Материализовать все страницы в список — удобно для небольших
    connection'ов (например, список репозиториев пользователя).
    Для потенциально больших connection'ов (история коммитов)
    предпочтительнее использовать paginate() напрямую как стрим.
    """
    return [node async for node in paginate(fetch_page, max_pages=max_pages)]