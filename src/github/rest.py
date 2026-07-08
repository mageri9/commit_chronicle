"""
REST-добор деталей коммита — файлы и patch, которых GraphQL не отдаёт
в принципе (в схеме GitHub GraphQL просто нет поля patch на файле).

Используется только для коммитов, прошедших фильтр needs_rest_details() —
то есть не для каждого коммита подряд, а только там, где
CommitHeader уже показал, что в коммите реально есть изменённые файлы.
Это и есть основная экономия запросов по сравнению со старым
PyGithub-collector'ом, который ходил в REST за файлами для КАЖДОГО
коммита без разбора.
"""

from __future__ import annotations

from src.github.client import GitHubClient
from src.github.models import CommitDetails


async def get_commit_details(
    client: GitHubClient, owner: str, name: str, sha: str
) -> CommitDetails:
    """GET /repos/{owner}/{name}/commits/{sha} -> CommitDetails (файлы + patch)."""
    payload = await client.get(f"/repos/{owner}/{name}/commits/{sha}")
    return CommitDetails.from_rest_payload(payload or {})