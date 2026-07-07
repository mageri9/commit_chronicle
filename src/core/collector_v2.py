"""
Collect Commits v2 — async pipeline поверх GitHub Engine (src/github/).

Замена src/core/collector.py::collect_commits(), но НЕ его модификация —
4.2 требует параллельного прогона старого и нового пайплайна для
сравнения результатов, поэтому оба сосуществуют до переключения
(USE_GITHUB_ENGINE_V2, Квест 4.3) и последующей зачистки старого.

Возвращает тот же AnalysisResult, что и старый collect_commits() —
это единственный контракт, который важен для normalizer/worker выше:
сериализация, кеш commit_cache, worker/tasks.py не должны знать,
чем именно был собран результат.

Известное ограничение (не в скоупе этого квеста): RateLimiter из
src/github/ratelimit.py сюда ещё не подключён как admission control
перед стартом партии запросов — сейчас за паузы при исчерпании лимита
отвечает только retry-цикл внутри GitHubClient (быстрый fail-fast,
не sleep до reset_at). Подключение RateLimiter на уровне этого пайплайна
— отдельный будущий квест, см. ratelimit.py docstring.
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
    since_date: str,
    semaphore: asyncio.Semaphore,
    index: int,
    total: int,
) -> list[Commit]:
    """
    Собрать все коммиты одного репозитория.

    Мёрж-коммиты уже отфильтрованы внутри service.get_commit_history() —
    здесь про них ничего знать не нужно, в отличие от старого collector.py,
    где `if len(commit.parents) > 1: continue` жил прямо в этой функции.
    """
    async with semaphore:
        logger.info(f"[{index}/{total}] 📁 Начало обработки {repo.full_name}")

        try:
            headers: list[CommitHeader] = [
                h async for h in service.get_commit_history(repo, since=since_date)
            ]

            if not headers:
                logger.info(f"[{index}/{total}] 📁 {repo.full_name}: ⏭️ Нет коммитов")
                return []

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
            # Пробрасываем как есть (не оборачиваем в CollectorError) —
            # asyncio.gather(return_exceptions=True) вернёт объект наверх,
            # где collect_commits_v2 отличит его от прочих ошибок и просто
            # пропустит репозиторий, не роняя весь сбор.
            raise

        except GitHubAPIError as e:
            # Ловит и AuthenticationError, и GraphQLError/NodeLimitExceeded —
            # все они наследники GitHubAPIError (см. exceptions.py).
            status = e.status_code
            if status in (403, 404):
                logger.warning(
                    f"[{index}/{total}] 📁 {repo.full_name}: ❌ Доступ запрещён ({status})"
                )
                raise RepoAccessError(repo.full_name, status)
            logger.warning(
                f"[{index}/{total}] 📁 {repo.full_name}: ❌ Ошибка API {status}"
            )
            raise CollectorError(f"GitHub API error: {status}")

        except Exception as e:
            logger.warning(
                f"[{index}/{total}] 📁 {repo.full_name}: ❌ {type(e).__name__}: {e}"
            )
            raise CollectorError(str(e))


async def collect_commits_v2(
    username: str, since_date: str, max_concurrency: int = _DEFAULT_CONCURRENCY
) -> AnalysisResult:
    """
    Асинхронная замена collect_commits() на GitHub Engine.

    Отличия от старого collector'а:
      - параллелизм через asyncio.Semaphore, а не ThreadPoolExecutor;
      - фильтрация мёрж-коммитов сделана внутри service.get_commit_history();
      - repository-level Redis-кеш (Квест 3.3) уже применяется прозрачно
        внутри service — эта функция про него ничего не знает и не должна.
    """
    service = await get_github_service()

    since = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

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

    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = [
        _process_single_repo(service, repo, since_date, semaphore, i, len(active_repos))
        for i, repo in enumerate(active_repos, 1)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_commits: list[Commit] = []
    for repo, result in zip(active_repos, results):
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