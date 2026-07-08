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
from loguru import logger

from collections.abc import AsyncIterator

from src.github.client import GitHubClient, get_github_client
from src.github.filters import RestDetailsPolicy, needs_rest_details
from src.github.graphql import get_commit_history as _get_commit_history
from src.github.graphql import get_user_id as _get_user_id
from src.github.graphql import list_repositories as _list_repositories
from src.github.models import CommitDetails, CommitHeader, Repository
from src.github.rest import get_commit_details as _get_commit_details
from src.github.cache import get_cached_history, set_cached_history
from src.github.batch import DEFAULT_BATCH_SIZE
from src.github.batch import fetch_first_pages as _fetch_first_pages


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
        self._user_id_cache: dict[str, str] = {}

    async def _resolve_user_id(self, login: str) -> str:
        cached = self._user_id_cache.get(login)
        if cached is not None:
            return cached
        user_id = await _get_user_id(self._client, login)
        self._user_id_cache[login] = user_id
        return user_id

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

        Repository-level cache (Квест 3.3): если pushed_at репозитория не
        изменился с прошлого раза — отдаём сохранённый список, GraphQL не
        трогаем вообще. Кешируется только полностью пройденная история
        (запись в кеш — после генератора, не внутри) — партиальный список
        из-за раннего break или исключения у вызывающего кода не попадёт
        в кеш как будто это полная история.
        """
        author_id = await self._resolve_user_id(repo.owner)
        logger.info(f"🔑 author_id для {repo.owner} = {author_id!r}")
        # временно, для диагностики — убрать после подтверждения фикса

        cached = await get_cached_history(repo, author_id=author_id, since=since)
        if cached is not None:
            for header in cached:
                yield header
            return

        headers: list[CommitHeader] = []
        async for header in _get_commit_history(
            self._client, repo, author_id=author_id, since=since
        ):
            if header.is_merge:
                continue
            headers.append(header)
            yield header

        await set_cached_history(repo, headers, author_id=author_id, since=since)

    async def get_commit_history_batch(
        self, repos: list[Repository], *, since: str | None = None
    ) -> dict[str, list[CommitHeader]]:
        """
        Забрать историю коммитов для нескольких репозиториев ОДНОГО
        владельца, используя GraphQL aliases batching там, где это
        возможно. Возвращает {repo.full_name: [CommitHeader, ...]}.

        Те же гарантии, что и у get_commit_history(): без мёрж-коммитов,
        с repository-level кешем (Квест 3.3), тем же author_id.

        Репозитории с готовым валидным кешем не участвуют в batch-запросе
        вообще. Репозитории, чья история не поместилась в одну страницу
        (>PAGE_SIZE коммитов за период), докачиваются обычным потоковым
        способом через get_commit_history().

        Если батч-запрос для группы репозиториев целиком упал (rate limit,
        транспортная ошибка) — эти репозитории ПРОПУСКАЮТСЯ (отсутствуют
        в возвращённом dict, а не присутствуют с пустым списком) — так
        вызывающий код (collector.py) может отличить "не удалось получить
        историю" от "коммитов действительно нет".
        """
        if not repos:
            return {}

        owners = {repo.owner for repo in repos}
        if len(owners) != 1:
            raise ValueError(
                "get_commit_history_batch ожидает репозитории одного "
                f"владельца (author_id общий для всех) — получено: {owners}"
            )
        owner = next(iter(owners))
        author_id = await self._resolve_user_id(owner)

        result: dict[str, list[CommitHeader]] = {}
        to_fetch: list[Repository] = []

        for repo in repos:
            if not repo.default_branch:
                result[repo.full_name] = []
                continue
            cached = await get_cached_history(repo, author_id=author_id, since=since)
            if cached is not None:
                result[repo.full_name] = cached
            else:
                to_fetch.append(repo)

        for i in range(0, len(to_fetch), DEFAULT_BATCH_SIZE):
            chunk = to_fetch[i : i + DEFAULT_BATCH_SIZE]

            try:
                batch_results = await _fetch_first_pages(
                    self._client, chunk, author_id=author_id, since=since
                )
            except Exception as e:
                logger.warning(
                    f"batch fetch failed for {[r.full_name for r in chunk]}: {e}"
                )
                continue  # репозитории чанка остаются отсутствующими в result

            for br in batch_results:
                if br.has_more:
                    logger.debug(
                        f"{br.repo.full_name}: >1 страницы истории — "
                        f"докачиваю через get_commit_history (без batch)"
                    )
                    headers = [
                        h async for h in self.get_commit_history(br.repo, since=since)
                    ]
                    result[br.repo.full_name] = headers
                    continue

                headers = [h for h in br.headers if not h.is_merge]
                result[br.repo.full_name] = headers
                await set_cached_history(
                    br.repo, headers, author_id=author_id, since=since
                )

        return result

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