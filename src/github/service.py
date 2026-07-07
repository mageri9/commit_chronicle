"""
Публичный фасад GitHub Engine — единственная точка входа для остального
проекта (collector/worker). Здесь и только здесь сходятся GraphQL, REST
и фильтрация. Снаружи src/github/ никто не должен импортировать
client.py, graphql.py, rest.py, filters.py напрямую — только этот файл.

Это то, что в плане рефакторинга называлось "Collector вообще ничего
не знает про GitHub API. Он знает только github.list_repositories(),
github.get_commit_history(), github.enrich_with_details()".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.github.client import GitHubClient, get_github_client
from src.github.filters import RestDetailsPolicy, needs_rest_details
from src.github.graphql import get_commit_history as _get_commit_history
from src.github.graphql import list_repositories as _list_repositories
from src.github.models import CommitDetails, CommitHeader, Repository
from src.github.rest import get_commit_details as _get_commit_details


class GitHubService:
    """
    Использование:
        service = await get_github_service()
        repos = await service.list_repositories("torvalds")
        async for header in service.get_commit_history(repos[0]):
            details = await service.enrich_with_details(repos[0], header)
    """

    def __init__(
        self, client: GitHubClient, *, rest_policy: RestDetailsPolicy | None = None
    ) -> None:
        self._client = client
        self._rest_policy = rest_policy or RestDetailsPolicy()

    async def list_repositories(self, login: str) -> list[Repository]:
        """Все репозитории пользователя (владелец). Форки не фильтрует."""
        return await _list_repositories(self._client, login)

    async def get_commit_history(
        self, repo: Repository, *, since: str | None = None
    ) -> AsyncIterator[CommitHeader]:
        """
        Стримит CommitHeader по одному репозиторию, УЖЕ без мёрж-коммитов —
        это единственное место, где применяется фильтр is_merge, так что
        вызывающему коду (collector) не нужно про него помнить.
        """
        async for header in _get_commit_history(self._client, repo, since=since):
            if header.is_merge:
                continue
            yield header

    async def enrich_with_details(
        self, repo: Repository, header: CommitHeader
    ) -> CommitDetails | None:
        """
        Вернуть файлы коммита, если needs_rest_details() решил, что это
        оправдано. None означает "REST не нужен" — вызывающий код просто
        работает с тем, что уже есть в CommitHeader, без списка файлов.
        """
        if not needs_rest_details(header, policy=self._rest_policy):
            return None
        return await _get_commit_details(
            self._client, repo.owner, repo.name, header.sha
        )


_service: GitHubService | None = None


async def get_github_service() -> GitHubService:
    """Ленивый синглтон на процесс, по аналогии с get_github_client()."""
    global _service
    if _service is None:
        client = await get_github_client()
        _service = GitHubService(client)
    return _service