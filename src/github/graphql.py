"""
GraphQL-слой GitHub Engine — то немногое, что знает, как из ответов
GitHubClient достать репозитории и историю коммитов, используя
paginator.py для курсоров и models.py для превращения сырых узлов
в доменные объекты.

Не знает ничего про фильтрацию (форки, кеш по pushedAt, needs_rest_details)
— это ответственность более высокого уровня (service.py).
Здесь только "как получить данные", не "какие данные нам нужны".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.github.client import GitHubClient
from src.github.exceptions import GitHubAPIError
from src.github.models import CommitHeader, Repository
from src.github.paginator import collect_all, paginate
from src.github.queries import (
    REPOSITORY_COMMIT_HISTORY,
    USER_REPOSITORIES,
    USER_ID,
    repository_commit_history_variables,
    user_repositories_variables,
    user_id_variables,
)
from src.logger import get_logger

logger = get_logger(__name__)


async def list_repositories(client: GitHubClient, login: str) -> list[Repository]:
    """
    Список всех репозиториев пользователя (владелец, не участник чужих
    репо — см. ownerAffiliations: OWNER в queries.py). Включает форки —
    решение, фильтровать их или нет, принимает вызывающий код.
    """

    async def fetch(after: str | None) -> dict:
        variables = user_repositories_variables(login, after=after)
        data = await client.graphql(USER_REPOSITORIES, variables)
        user_node = data.get("user")
        if user_node is None:
            raise GitHubAPIError(
                f"Пользователь {login!r} не найден на GitHub", status_code=404
            )
        return user_node["repositories"]

    nodes = await collect_all(fetch)
    return [Repository.from_graphql_node(node) for node in nodes]


async def get_user_id(client: GitHubClient, login: str) -> str:
    """
    Node ID пользователя — нужен для фильтрации истории коммитов по автору
    (см. queries.py::REPOSITORY_COMMIT_HISTORY). Отдельный лёгкий запрос,
    не совмещённый с USER_REPOSITORIES, чтобы GitHubService мог закешировать
    результат на весь процесс (login → id меняется практически никогда)
    без привязки к пагинации репозиториев.
    """
    data = await client.graphql(USER_ID, user_id_variables(login))
    user_node = data.get("user")
    if user_node is None:
        raise GitHubAPIError(
            f"Пользователь {login!r} не найден на GitHub", status_code=404
        )
    return user_node["id"]


async def get_commit_history(
    client: GitHubClient,
    repo: Repository,
    *,
    author_id: str,
    since: str | None = None,
) -> AsyncIterator[CommitHeader]:
    """
    Стримит CommitHeader для одного репозитория по дефолтной ветке.

    Если у репозитория нет дефолтной ветки (пустой репозиторий без единого
    коммита) — просто ничего не отдаёт, не делая запрос: GraphQL-переменная
    $branch объявлена как String! (non-null), отправка null туда упадёт
    валидацией на стороне GitHub, а не вернёт пустой результат.
    """
    if not repo.default_branch:
        logger.debug(
            f"{repo.full_name}: нет default_branch (пустой репозиторий?) — пропускаю"
        )
        return

    async def fetch(after: str | None) -> dict:
        variables = repository_commit_history_variables(
            repo.owner,
            repo.name,
            repo.default_branch,
            author_id=author_id,
            since=since,
            after=after,
        )
        data = await client.graphql(REPOSITORY_COMMIT_HISTORY, variables)
        return _extract_history_connection(data)

    async for node in paginate(fetch):
        yield CommitHeader.from_graphql_node(node, repo=repo.full_name)


def _extract_history_connection(data: dict) -> dict:
    """
    Достать {"pageInfo": ..., "nodes": ...} из глубоко вложенного ответа
    repository.object.history. Если ветка существует, но почему-то не
    резолвится в Commit (не должно случаться для валидного default_branch,
    но GraphQL не гарантирует этого статически) — возвращаем пустую
    connection вместо падения, пагинатор корректно остановится сам.
    """
    repository_node = data.get("repository") or {}
    target = repository_node.get("object") or {}
    history = target.get("history")
    if history is None:
        return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    return history