"""
GraphQL aliases batching — объединение первой страницы истории коммитов
нескольких репозиториев в один HTTP-запрос (отложенная оптимизация из
роадмапа: "GraphQL aliases batching").

Работает только для ПЕРВОЙ страницы истории каждого репозитория —
у GraphQL нет способа продолжить пагинацию нескольких независимых
connection'ов одним запросом с разными курсорами, поэтому репозитории,
у которых история не поместилась в одну страницу (hasNextPage=True),
докачиваются обычным способом через graphql.get_commit_history()
(paginator.py), не через batch. Для типичных личных репозиториев
(<100 коммитов за период) это редкость — основная масса репо получает
полную историю одним алиасом в общем запросе.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.github.client import GitHubClient
from src.github.models import CommitHeader, Repository
from src.github.queries import (
    build_batch_commit_history_query,
    batch_commit_history_variables,
)
from src.logger import get_logger

logger = get_logger(__name__)

# Разумный потолок алиасов в одном запросе. GitHub считает "cost" всего
# запроса по сумме first/100 всех connection'ов (см. ratelimit.py) — при
# первой странице (first: 100) каждый алиас стоит ~1 unit, так что 20
# репозиториев в одном запросе — это ~20 units, далеко от лимита за один
# запрос. Держим умеренно, чтобы не рисковать MAX_NODE_LIMIT_EXCEEDED на
# аккаунтах с очень большим числом репозиториев.
DEFAULT_BATCH_SIZE = 20


@dataclass
class BatchRepoResult:
    repo: Repository
    headers: list[CommitHeader]
    has_more: bool  # True — история не поместилась в одну страницу


async def fetch_first_pages(
    client: GitHubClient,
    repos: list[Repository],
    *,
    author_id: str,
    since: str | None,
) -> list[BatchRepoResult]:
    """
    Забрать первую страницу истории коммитов для каждого репозитория
    из repos ОДНИМ GraphQL-запросом (через aliases). repos должен
    состоять только из репозиториев с непустым default_branch —
    вызывающий код (service.py) отвечает за этот фильтр заранее.
    """
    if not repos:
        return []

    query = build_batch_commit_history_query(len(repos))
    variables = batch_commit_history_variables(
        [
            (repo.owner, repo.name, repo.default_branch, author_id, since)
            for repo in repos
        ]
    )

    data = await client.graphql(query, variables)

    results: list[BatchRepoResult] = []
    for i, repo in enumerate(repos):
        connection = _extract_history_connection(data.get(f"repo{i}") or {})
        nodes = connection.get("nodes") or []
        page_info = connection.get("pageInfo") or {}

        headers = [
            CommitHeader.from_graphql_node(node, repo=repo.full_name)
            for node in nodes
            if node is not None
        ]

        results.append(
            BatchRepoResult(
                repo=repo,
                headers=headers,
                has_more=bool(page_info.get("hasNextPage")),
            )
        )

    return results


def _extract_history_connection(repository_node: dict) -> dict:
    """
    Тот же принцип, что graphql.py::_extract_history_connection, но для
    одного алиас-узла из батч-ответа (уже без обёртки "repository"), а
    не top-level ответа одиночного запроса — поэтому не переиспользуем
    ту функцию напрямую, формы входных данных разные.
    """
    target = repository_node.get("object") or {}
    history = target.get("history")
    if history is None:
        return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    return history