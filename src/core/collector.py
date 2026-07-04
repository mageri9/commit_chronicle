"""
Commit Chronicle — минимальный сборщик фактов (FAST edition)
Параллельная обработка + фильтр мёртвых репозиториев
"""

import json
import os
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from github import GithubException, RateLimitExceededException
from dotenv import load_dotenv
from src.logger import get_logger

from src.core.normalizer import normalize
from src.core.token_rotator import token_rotator
from src.models import AnalysisResult
from src.core.exceptions import (
    RateLimitError,
    TokenExhaustedError,
    RepoAccessError,
    CollectorError,
)


load_dotenv()

logger = get_logger(__name__)


def process_single_repo(
    repo, username: str, since: datetime, since_date: str, index: int, total: int
) -> dict | None:
    """Обрабатывает один репозиторий (для параллельного выполнения)"""

    logger.info(f"[{index}/{total}] 📁 {repo.full_name}...", end=" ", flush=True)

    try:
        commits_manifest = []
        commits = repo.get_commits(author=username, since=since)

        for commit in commits:
            if len(commit.parents) > 1:
                continue

            manifest = {
                "hash": commit.sha[:7],
                "date": commit.commit.author.date.isoformat(),
                "message": commit.commit.message.split("\n")[0],
                "files": {},
            }

            for file in commit.files or []:
                manifest["files"][file.filename] = {
                    "+": file.additions or 0,
                    "-": file.deletions or 0,
                }

            commits_manifest.append(manifest)

        if commits_manifest:
            logger.info(f"✅ {len(commits_manifest)} коммитов")
            return {
                "repo": repo.full_name,
                "period": f"{since_date}..{datetime.now().strftime('%Y-%m-%d')}",
                "commits": commits_manifest,
            }
        else:
            logger.info("⏭️ нет коммитов")
            return None

    except GithubException as e:
        if e.status in (403, 404):
            logger.warning(f"❌ Доступ запрещён ({e.status})")
            raise RepoAccessError(repo.full_name, e.status)

        logger.warning(f"❌ API: {e.status}")
        raise CollectorError(f"GitHub API error: {e.status}")

    except Exception as e:
        logger.warning(f"❌ {type(e).__name__}: {e}")
        raise CollectorError(str(e))


def collect_commits(
    username: str, since_date: str, max_workers: int = 10
) -> AnalysisResult:
    g = token_rotator.get_client()

    # Проверяем лимиты
    try:
        rate_limit = g.get_rate_limit()

        remaining = rate_limit.resources.core.remaining

        logger.info(f"📡 API запросов осталось: {remaining}")

        if remaining < 10:
            raise TokenExhaustedError(
                f"Осталось всего {remaining} запросов. Нужен новый токен."
            )
    except TokenExhaustedError:
        raise
    except Exception as e:
        logger.error(f"⚠️ Не удалось проверить лимиты: {e}")

    user = g.get_user(username)

    # ГЛАВНАЯ ОПТИМИЗАЦИЯ: фильтруем по pushed_at
    since = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    all_repos = [r for r in user.get_repos() if not r.fork]
    active_repos = [r for r in all_repos if r.pushed_at and r.pushed_at >= since]
    skipped = len(all_repos) - len(active_repos)

    logger.info(f"📂 Всего репозиториев: {len(all_repos)}")
    logger.info(
        f"📂 Активных с {since_date}: {len(active_repos)} (пропущено: {skipped})"
    )
    logger.info(f"⚡ Параллельных потоков: {max_workers}\n")

    all_manifests = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_single_repo,
                repo,
                username,
                since,
                since_date,
                i,
                len(active_repos),
            ): repo
            for i, repo in enumerate(active_repos, 1)
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    all_manifests.append(result)
            except RateLimitExceededException:
                token_rotator.block_token(g, duration=3600)
                logger.warning("❌ Лимит API — токен заблокирован на час")
                raise RateLimitError("GitHub API rate limit exceeded", retry_after=60)
            except TokenExhaustedError as e:
                logger.warning(f"\n⚠️ {e}")
                executor.shutdown(wait=False, cancel_futures=True)
                break
            except RepoAccessError as e:
                logger.warning(f"⚠️ Пропущен: {e}")
                continue
            except CollectorError as e:
                logger.warning(f"⚠️ Ошибка: {e}")
                continue

    return normalize(all_manifests, username, since_date)


def main():
    username = os.getenv("GITHUB_USERNAME", "mageri9")
    since = os.getenv("GITHUB_SINCE", "2024-01-01")
    workers = int(os.getenv("MAX_WORKERS", "10"))

    if len(sys.argv) > 1:
        username = sys.argv[1]
    if len(sys.argv) > 2:
        since = sys.argv[2]
    if len(sys.argv) > 3:
        workers = int(sys.argv[3])

    logger.info(f"\n🚀 Commit Chronicle — сбор коммитов (FAST)")
    logger.info(f"👤 Пользователь: {username}")
    logger.info(f"📅 Период: с {since}\n")

    start_time = datetime.now()
    output_file = "commit_chronicle.json"

    result = collect_commits(username, since, max_workers=workers)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(
            result.model_dump(mode="json"),
            f,
            indent=2,
            ensure_ascii=False,
        )

    total_commits = len(result.commits)
    repo_count = len({c.repo for c in result.commits})

    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info(f"\n{'=' * 50}")
    logger.info(f"✅ Сохранено в {output_file}")
    logger.info(f"📊 Репозиториев с коммитами: {repo_count}")
    logger.info(f"📊 Всего коммитов: {total_commits}")
    logger.info(f"⏱️ Время выполнения: {elapsed:.1f}s")


if __name__ == "__main__":
    main()