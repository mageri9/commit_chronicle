"""
Collector — сбор коммитов через GitHub Engine (src/github/).

Единственный collector в проекте с завершения миграции (Акт 4).
Старый PyGithub-based collector и src/core/token_rotator.py удалены
в финальной зачистке — см. docs/adr/001-github-engine.md.

Известное ограничение: RateLimiter из src/github/ratelimit.py сюда
ещё не подключён как admission control перед стартом партии запросов —
за паузы при исчерпании лимита отвечает только retry-цикл внутри
GitHubClient (быстрый fail-fast, не sleep до reset_at). Подключение
RateLimiter на уровне этого пайплайна — отдельная будущая задача.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.core.exceptions import CollectorError, RepoAccessError
from src.github.exceptions import GitHubAPIError, RateLimitExceeded
from src.github.models import CommitHeader, Repository
from src.github.service import GitHubService, get_github_service
from src.logger import get_logger
from src.models.models import AnalysisResult, Commit, FileChange

logger = get_logger(__name__)

_DEFAULT_CONCURRENCY = 10


async def _process_single_repo(
    service: GitHubService,
    repo: Repository,
    headers: list[CommitHeader],
    semaphore: asyncio.Semaphore,
    index: int,
    total: int,
) -> list[Commit]:
    """
    Обогащает уже полученные CommitHeader деталями файлов и строит
    итоговые Commit. История коммитов теперь достаётся ДО этой функции,
    батчем через GitHubService.get_commit_history_batch (см.
    collect_commits) — здесь только REST-добор файлов, который
    по-прежнему делается параллельно по репозиториям через семафор.
    Мёрж-коммиты и коммиты чужих авторов уже отфильтрованы заранее.
    """
    async with semaphore:
        if not headers:
            logger.info(f"[{index}/{total}] 📁 {repo.full_name}: ⏭️ Нет коммитов")
            return []

        logger.info(f"[{index}/{total}] 📁 Начало обработки {repo.full_name}")

        try:
            commits: list[Commit] = []
            for header in headers:
                files: list[FileChange] = []
                details = await service.enrich_with_details(repo, header)
                if details is not None:
                    files = [
                        FileChange(
                            filename=f.filename,
                            additions=f.additions,
                            deletions=f.deletions,
                        )
                        for f in details.files
                    ]

                commits.append(
                    Commit(
                        hash=header.sha[:7],
                        date=header.date,
                        message=header.message,
                        repo=repo.full_name,
                        files=files,
                    )
                )

            logger.info(
                f"[{index}/{total}] 📁 {repo.full_name}: ✅ Найдено коммитов: {len(commits)}"
            )
            return commits

        except RateLimitExceeded:
            raise

        except GitHubAPIError as e:
            status = e.status_code
            if status in (403, 404):
                logger.warning(
                    f"[{index}/{total}] 📁 {repo.full_name}: ❌ Доступ запрещён ({status})"
                )
                raise RepoAccessError(repo.full_name, status)
            logger.warning(
                f"[{index}/{total}] 📁 {repo.full_name}: ❌ Ошибка API {status}: {e}"
            )
            raise CollectorError(f"GitHub API error {status}: {e}")

        except Exception as e:
            logger.warning(
                f"[{index}/{total}] 📁 {repo.full_name}: ❌ {type(e).__name__}: {e}"
            )
            raise CollectorError(str(e))


async def collect_commits(
    username: str, since_date: str, max_concurrency: int = _DEFAULT_CONCURRENCY
) -> AnalysisResult:
    """
    Собрать коммиты пользователя через GitHub Engine.

    История коммитов забирается батчем через GitHubService.get_commit_history_batch
    (GraphQL aliases batching, см. src/github/batch.py) — вместо одного
    GraphQL-запроса на каждый репозиторий. REST-добор файлов по-прежнему
    идёт параллельно по репозиториям (semaphore), это batching не трогает.
    """
    service = await get_github_service()

    since = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_repos = await service.list_repositories(username)
    active_repos = [
        r for r in all_repos if not r.is_fork and r.pushed_at and r.pushed_at >= since
    ]
    skipped = len(all_repos) - len(active_repos)

    logger.info(f"📂 Всего репозиториев: {len(all_repos)}")
    logger.info(
        f"📂 Активных с {since_date}: {len(active_repos)} (пропущено: {skipped})"
    )
    logger.info(f"⚡ Максимальная конкурентность: {max_concurrency}\n")

    history_by_repo = await service.get_commit_history_batch(
        active_repos, since=since_iso
    )

    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = []
    task_repos = []
    for i, repo in enumerate(active_repos, 1):
        headers = history_by_repo.get(repo.full_name)
        if headers is None:
            logger.warning(
                f"⚠️ Пропущен (не удалось получить историю): {repo.full_name}"
            )
            continue
        task_repos.append(repo)
        tasks.append(
            _process_single_repo(
                service, repo, headers, semaphore, i, len(active_repos)
            )
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_commits: list[Commit] = []
    for repo, result in zip(task_repos, results):
        if isinstance(result, RepoAccessError):
            logger.warning(f"⚠️ Пропущен: {result}")
            continue
        if isinstance(result, RateLimitExceeded):
            logger.warning(f"⚠️ Rate limit при сборе {repo.full_name}: {result}")
            continue
        if isinstance(result, CollectorError):
            logger.warning(f"⚠️ Ошибка: {result}")
            continue
        if isinstance(result, BaseException):
            logger.warning(f"⚠️ Неожиданная ошибка {repo.full_name}: {result}")
            continue
        all_commits.extend(result)

    return AnalysisResult(
        username=username,
        period_start=since_date,
        commits=all_commits,
        generated_at=datetime.now(),
    )