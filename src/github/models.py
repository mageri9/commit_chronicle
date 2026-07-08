"""
Доменные модели транспортного слоя GitHub Engine.

Важно: это НЕ то же самое, что src/models/models.py (Commit, AnalysisResult,
CompactResult). Те — финальный формат для LLM/кеша, собранный normalizer'ом
из данных любого источника. Эти — сырьё, как оно приходит с транспорта
(GraphQL/REST), и ничего не знают о сжатии, LLM или Telegram.

Разделение по весу — сознательное архитектурное решение (см. roadmap):
    Repository    — метаданные репозитория, дёшево получить пачками
    CommitHeader  — лёгкий: дата, subject, статистика, БЕЗ списка файлов.
                    Можно скачать тысячи штук одним GraphQL-походом.
    CommitDetails — тяжёлый: список файлов с patch. Требует отдельного
                    REST-запроса на каждый коммит — тянется только тогда,
                    когда CommitHeader показал, что это действительно нужно.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


def _parse_datetime(value: str | None) -> datetime | None:
    """GitHub отдаёт ISO8601 с суффиксом 'Z' — datetime.fromisoformat его не ест напрямую."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Repository(BaseModel):
    """Репозиторий — метаданные, без коммитов."""

    owner: str
    name: str
    pushed_at: datetime | None = None
    is_fork: bool = False
    default_branch: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @classmethod
    def from_graphql_node(cls, node: dict) -> "Repository":
        """
        Собрать Repository из узла GraphQL-ответа (см. queries.py,
        USER_REPOSITORIES_WITH_HISTORY). Ожидаемая форма узла:
            {
                "name": "...",
                "owner": {"login": "..."},
                "pushedAt": "2026-01-01T00:00:00Z",
                "isFork": false,
                "defaultBranchRef": {"name": "main", "target": {...}}
            }
        """
        owner_login = (node.get("owner") or {}).get("login", "")
        default_branch_ref = node.get("defaultBranchRef") or {}
        return cls(
            owner=owner_login,
            name=node["name"],
            pushed_at=_parse_datetime(node.get("pushedAt")),
            is_fork=bool(node.get("isFork", False)),
            default_branch=default_branch_ref.get("name"),
        )


class CommitHeader(BaseModel):
    """
    Лёгкое представление коммита — без файлов. Этого достаточно для
    большинства решений: фильтрации мёржей, подсчёта статистики,
    отображения в сводке. Список файлов (CommitDetails) добирается
    отдельно и только при необходимости.
    """

    sha: str
    date: datetime
    message: str
    repo: str = ""  # full_name репозитория — проставляется normalizer'ом при сборке
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    parents_count: int = 0

    @property
    def is_merge(self) -> bool:
        """
        Мёрж-коммиты (>1 родителя) исторически исключались из статистики
        (см. старый core/collector.py: `if len(commit.parents) > 1: continue`).
        GraphQL не отдаёт список родителей бесплатно — нужно явно запросить
        `parents { totalCount }` в queries.py, иначе parents_count всегда 0
        и is_merge будет ложно-отрицательным.
        """
        return self.parents_count > 1

    @classmethod
    def from_graphql_node(cls, node: dict, *, repo: str = "") -> "CommitHeader":
        """
        Собрать CommitHeader из узла history.nodes GraphQL-ответа.
        Ожидаемая форма узла:
            {
                "oid": "abc123...",
                "committedDate": "2026-01-01T12:00:00Z",
                "messageHeadline": "fix: ...",
                "additions": 12,
                "deletions": 3,
                "changedFilesIfAvailable": 2,
                "parents": {"totalCount": 1}
            }
        """
        parents = node.get("parents") or {}
        changed_files = node.get("changedFilesIfAvailable")
        if changed_files is None:
            changed_files = node.get("changedFiles", 0)

        return cls(
            sha=node["oid"],
            date=_parse_datetime(node["committedDate"]),
            message=node.get("messageHeadline", ""),
            repo=repo,
            additions=node.get("additions") or 0,
            deletions=node.get("deletions") or 0,
            changed_files=changed_files or 0,
            parents_count=parents.get("totalCount", 0),
        )


class FileDiff(BaseModel):
    """Один изменённый файл — часть CommitDetails."""

    filename: str
    additions: int = 0
    deletions: int = 0
    patch: str | None = None


class CommitDetails(BaseModel):
    """
    Тяжёлое представление коммита — файлы и патчи. GraphQL не отдаёт patch
    вообще, поэтому это всегда результат REST-запроса
    (GET /repos/{owner}/{repo}/commits/{sha}), а не GraphQL.
    """

    sha: str
    files: list[FileDiff] = Field(default_factory=list)

    @classmethod
    def from_rest_payload(cls, payload: dict) -> "CommitDetails":
        """
        Собрать CommitDetails из ответа REST-эндпоинта
        GET /repos/{owner}/{repo}/commits/{sha}.
        """
        files = [
            FileDiff(
                filename=f.get("filename", ""),
                additions=f.get("additions", 0) or 0,
                deletions=f.get("deletions", 0) or 0,
                patch=f.get("patch"),
            )
            for f in payload.get("files" or [])
        ]
        return cls(sha=payload.get("sha", ""), files=files)