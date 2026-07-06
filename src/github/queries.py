"""
GraphQL-запросы GitHub Engine.

Два запроса, сознательно не один:
    USER_REPOSITORIES         — список репозиториев пользователя с метаданными
                                (нужны, чтобы решить, какие вообще трогать —
                                см. repo-level кеш по pushedAt, Квест 3.3).
    REPOSITORY_COMMIT_HISTORY — история коммитов ОДНОГО репозитория.

Почему не один запрос сразу на всё: GraphQL позволяет пагинировать только
"плоские" connection'ы. Если попытаться получить репозитории и вложенную
историю коммитов каждого репозитория одним запросом, для репозиториев
с историей длиннее одной страницы (first: 100) продолжить пагинацию
именно этой вложенной connection'и без повторного похода за каждым репо
отдельно — нельзя. Поэтому pipeline двухступенчатый:
    1. пагинируем repositories (paginator.py, свой курсор)
    2. для repositories, которым это нужно, пагинируем history отдельно
       (свой курсор на каждый репозиторий)

Оба запроса включают `rateLimit { remaining resetAt cost }` — это
единственный способ узнать реальную стоимость GraphQL-запроса (GraphQL,
в отличие от REST, не отдаёт лимиты в заголовках ответа).
GitHubClient._maybe_report_graphql_limit() автоматически читает этот
блок из ответа и обновляет TokenPool — вызывающему коду ничего
дополнительно делать не нужно.

Постраничность (pageInfo/after) исполняется paginator.py — сами запросы
здесь только объявляют форму данных и ничего не знают о циклах.
"""

from __future__ import annotations

# Размер одной страницы для обеих connection'ов. Один и тот же размер
# для простоты — при необходимости точечно потюнить для history
# (например, из-за более дорогого cost) это можно разнести на два
# отдельных значения, но пока нет данных, что это нужно.
PAGE_SIZE = 100


USER_REPOSITORIES = f"""
query($login: String!, $after: String) {{
  user(login: $login) {{
    repositories(
      first: {PAGE_SIZE}
      after: $after
      ownerAffiliations: OWNER
      orderBy: {{ field: PUSHED_AT, direction: DESC }}
    ) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        name
        owner {{ login }}
        pushedAt
        isFork
        defaultBranchRef {{ name }}
      }}
    }}
  }}
  rateLimit {{ remaining resetAt cost }}
}}
"""


REPOSITORY_COMMIT_HISTORY = f"""
query(
  $owner: String!
  $name: String!
  $branch: String!
  $since: GitTimestamp
  $after: String
) {{
  repository(owner: $owner, name: $name) {{
    object(expression: $branch) {{
      ... on Commit {{
        history(first: {PAGE_SIZE}, after: $after, since: $since) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            oid
            committedDate
            messageHeadline
            additions
            deletions
            changedFilesIfAvailable
            parents {{ totalCount }}
          }}
        }}
      }}
    }}
  }}
  rateLimit {{ remaining resetAt cost }}
}}
"""
# ВАЖНО: `parents { totalCount }` — обязательное поле. Без него
# CommitHeader.parents_count всегда останется 0, и is_merge будет
# ложно-отрицательным для всех коммитов (см. models.py). Не убирать
# при рефакторинге запроса, даже если кажется, что поле "не используется".


def user_repositories_variables(login: str, *, after: str | None = None) -> dict:
    """Переменные для USER_REPOSITORIES."""
    return {"login": login, "after": after}


def repository_commit_history_variables(
    owner: str,
    name: str,
    branch: str,
    *,
    since: str | None = None,
    after: str | None = None,
) -> dict:
    """
    Переменные для REPOSITORY_COMMIT_HISTORY.

    branch — expression для GraphQL `object(expression: ...)`, обычно
    имя дефолтной ветки без префикса (например "main"), т.е. значение
    Repository.default_branch из models.py.
    since — ISO8601-строка (GitTimestamp), например "2024-01-01T00:00:00Z".
    Передача since вместо REST-фильтрации по клиенту — намеренная замена
    старой логики `since=` параметра в PyGithub-collector'е.
    """
    return {
        "owner": owner,
        "name": name,
        "branch": branch,
        "since": since,
        "after": after,
    }