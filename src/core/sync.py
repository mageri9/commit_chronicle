"""
Инкрементальная синхронизация коммитов репозитория с постоянным хранилищем
(таблицы tracked_repos/commits в src.storage.database).

Разделение ответственности:
    sync_repo_commits() / sync_user_repos()  — ТОЛЬКО пишут в БД, ничего
                                                не возвращают.
    src.core.report.build_analysis_result()  — ТОЛЬКО читает из БД,
                                                не ходит в GitHub API.

Известное упрощение MVP: если период_start запрашивается РАНЬШЕ, чем была
сделана первая синхронизация репозитория, эта функция не доберёт более
старую историю автоматически (tracked_repos не хранит "глубину" истории,
только last_synced_at последней сверки). Обходной путь на этом этапе —
set_repo_needs_full_resync() из /repos, чтобы форсировать полный пересбор
на нужный период при следующем запросе.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from src.github.models import CommitHeader, Repository
from src.github.service import GitHubService
from src.logger import get_logger
from src.storage.database import (
    get_tracked_repo,
    upsert_commits,
    upsert_tracked_repo,
)

logger = get_logger(__name__)

_DEFAULT_CONCURRENCY = 10


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def _headers_to_rows(
    service: GitHubService,
    repo: Repository,
    headers: list[CommitHeader],
    analyzed_username: str,
) -> list[dict]:
    """
    CommitHeader -> строка для upsert_commits(). Файлы добираются через REST
    только там, где service.enrich_with_details() решит, что это оправдано
    (needs_rest_details) — та же экономия запросов, что и в collector.py.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []

    for header in headers:
        files_json = None
        details = await service.enrich_with_details(repo, header)
        if details is not None:
            files = [[f.filename, f.additions, f.deletions] for f in details.files]
            files_json = json.dumps(files, ensure_ascii=False)

        rows.append(
            {
                "sha": header.sha,
                "repo_full_name": repo.full_name,
                "analyzed_username": analyzed_username,
                "committed_at": _iso(header.date),
                "message": header.message,
                "additions": header.additions,
                "deletions": header.deletions,
                "changed_files": header.changed_files,
                "files_json": files_json,
                "created_at": now,
            }
        )
    return rows


async def sync_repo_commits(
    service: GitHubService,
    repo: Repository,
    analyzed_username: str,
    *,
    period_start_iso: str,
) -> int:
    """
    Довести таблицу commits для (repo, analyzed_username) до актуального
    состояния. Возвращает количество новых/обновлённых коммитов (для логов).
    """
    tracked = await get_tracked_repo(repo.full_name, analyzed_username)
    now_iso = datetime.now(timezone.utc).isoformat()
    current_pushed_at = _iso(repo.pushed_at)

    full_backfill = tracked is None or bool(tracked.get("needs_full_resync"))

    if not full_backfill:
        if tracked.get("last_pushed_at") == current_pushed_at:
            logger.debug(f"{repo.full_name}: pushed_at не изменился — пропуск")
            return 0
        since = tracked.get("last_synced_at")
    else:
        since = period_start_iso

    headers: list[CommitHeader] = [
        h async for h in service.get_commit_history(repo, since=since)
    ]

    if headers:
        rows = await _headers_to_rows(service, repo, headers, analyzed_username)
        await upsert_commits(rows)

    await upsert_tracked_repo(
        repo_full_name=repo.full_name,
        analyzed_username=analyzed_username,
        owner_login=repo.owner,
        default_branch=repo.default_branch,
        last_synced_sha=(
            headers[-1].sha if headers else (tracked or {}).get("last_synced_sha")
        ),
        last_synced_at=now_iso,
        last_pushed_at=current_pushed_at,
        needs_full_resync=False,
    )

    logger.info(
        f"{repo.full_name}: синхронизировано "
        f"({'полный пересбор' if full_backfill else 'дельта'}), "
        f"новых коммитов: {len(headers)}"
    )
    return len(headers)


async def sync_user_repos(
    service: GitHubService,
    repos: list[Repository],
    analyzed_username: str,
    *,
    period_start_iso: str,
    max_concurrency: int = _DEFAULT_CONCURRENCY,
) -> None:
    """
    Синхронизировать несколько репозиториев параллельно (semaphore — как в
    collector.py). Ошибка по одному репозиторию не должна ронять остальные:
    логируется и пропускается, следующий запрос попробует снова.
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _one(repo: Repository) -> None:
        async with semaphore:
            try:
                await sync_repo_commits(
                    service,
                    repo,
                    analyzed_username,
                    period_start_iso=period_start_iso,
                )
            except Exception as e:
                logger.warning(f"⚠️ sync не удался для {repo.full_name}: {e}")

    await asyncio.gather(*(_one(r) for r in repos))