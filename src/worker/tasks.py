"""
Фоновые задачи для arq.
"""

import json
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.core.report import build_analysis_result
from src.core.sync import sync_user_repos
from src.github.client import get_github_client
from src.github.service import get_github_service
from src.models.models import serialize_result
from src.storage.database import (
    create_request,
    find_existing_requests,
    get_tracked_repos_for_repo,
    update_request_status,
    upsert_commits,
    upsert_tracked_repo,
    set_repo_needs_full_resync,
)
from src.storage.pubsub import publish
from src.logger import get_logger

logger = get_logger(__name__)


def _backfill_start_iso() -> str:
    """Глубина полного бэкфилла — константа проекта, не зависит от запроса."""
    dt = datetime.now(timezone.utc) - timedelta(days=settings.max_backfill_days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def analyze_github_user(
    ctx,
    username: str,
    period_start: str,
    chat_id: str = "",
    repo_full_name: str | None = None,
) -> dict:
    """
    Пайплайн анализа GitHub-пользователя поверх постоянного хранилища.

    repo_full_name — опциональный фильтр "весь профиль / один репозиторий",
    применяется ТОЛЬКО при сборке отчёта. Синхронизация всегда идёт по
    всему профилю (в пределах max_backfill_days), чтобы БД можно было
    сразу переиспользовать под любой будущий запрос без повторного sync.
    """
    request_id = ctx["job_id"]
    period_end = datetime.now().strftime("%Y-%m-%d")

    if not chat_id:
        logger.warning(f"[{request_id}] chat_id пустой — уведомление не дойдёт")

    existing = await find_existing_requests(username, period_start, period_end)
    if existing and existing["status"] == "processing":
        await publish(
            "job:done",
            json.dumps(
                {"job_id": existing["id"], "status": "processing", "username": username}
            ),
        )
        return {
            "status": "processing",
            "request_id": existing["id"],
            "source": "existing_request",
            "result_json": None,
        }

    await create_request(
        request_id=request_id,
        username=username,
        period_start=period_start,
        period_end=period_end,
        chat_id=chat_id,
    )
    await update_request_status(request_id, "processing")

    try:
        service = await get_github_service()

        backfill_since_iso = _backfill_start_iso()
        backfill_since_dt = datetime.strptime(
            backfill_since_iso, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)

        all_repos = await service.list_repositories(username)
        active_repos = [
            r
            for r in all_repos
            if not r.is_fork and r.pushed_at and r.pushed_at >= backfill_since_dt
        ]

        # Синхронизация всего профиля (в пределах окна), не только
        # запрошенного сейчас периода/репозитория.
        await sync_user_repos(
            service,
            active_repos,
            username,
            period_start_iso=backfill_since_iso,
            max_concurrency=settings.max_workers,
        )

        # Отчёт же — строго по тому, что реально попросили в этом запросе.
        result = await build_analysis_result(
            username, period_start, period_end, repo_full_name=repo_full_name
        )
        result_json = serialize_result(result)

        await update_request_status(request_id, "done", result_json=result_json)
        await publish(
            "job:done",
            json.dumps({"job_id": request_id, "status": "done", "username": username}),
        )
        return {
            "status": "done",
            "request_id": request_id,
            "source": "storage",
            "result_json": result_json,
        }

    except Exception as e:
        logger.exception(f"[{request_id}] Ошибка анализа для {username}: {e}")
        await update_request_status(request_id, "failed", error_message=str(e))
        await publish(
            "job:done",
            json.dumps(
                {
                    "job_id": request_id,
                    "status": "failed",
                    "username": username,
                    "error": str(e),
                }
            ),
        )
        raise


def format_summary(result_json: str) -> str:
    """Собрать текстовую сводку из результатов анализа (без изменений)."""
    data = json.loads(result_json)
    repos: dict = data.get("repos", {})
    total_commits = sum(len(commits) for commits in repos.values())
    repo_count = len(repos)

    return (
        f"✅ Анализ готов\n"
        f"📦 Коммитов: {total_commits}\n"
        f"📁 Репозиториев: {repo_count}\n"
    )


# ---------------------------------------------------------------------------
# Вебхуки — обработка push-события через тот же worker pool
# ---------------------------------------------------------------------------


async def handle_push_event(ctx, payload: dict) -> dict:
    """
    Обработать GitHub push-вебхук. Вызывается arq-воркером — payload кладёт
    в очередь src/webhook/server.py сразу после проверки подписи, без какой
    -либо работы с GitHub API в самом HTTP-хендлере (GitHub требует ответ
    <10с, а REST-добор деталей коммитов может быть дольше).

    Фильтрация по автору: push-событие содержит коммиты ВСЕХ, кто пушил в
    этот репозиторий, а не только отслеживаемого профиля — поэтому commit
    добавляется в хранилище конкретного analyzed_username, только если
    author.login коммита совпадает с этим username. Иначе легко перепутать
    чужую активность с активностью анализируемого человека.
    """
    repo_info = payload.get("repository") or {}
    repo_full_name = repo_info.get("full_name")
    if not repo_full_name:
        return {"status": "skipped", "reason": "no repository.full_name"}

    default_branch = repo_info.get("default_branch")
    ref = payload.get("ref", "")
    if default_branch and ref != f"refs/heads/{default_branch}":
        return {"status": "skipped", "reason": f"not default branch ({ref})"}

    owner_info = repo_info.get("owner") or {}
    owner_login = owner_info.get("login") or owner_info.get("name") or ""

    tracked_rows = await get_tracked_repos_for_repo(repo_full_name)
    analyzed_usernames = [r["analyzed_username"] for r in tracked_rows] or (
        [owner_login] if owner_login else []
    )
    if not analyzed_usernames:
        return {"status": "skipped", "reason": "no tracked_repos entry and no owner"}

    if payload.get("forced"):
        for username in analyzed_usernames:
            await set_repo_needs_full_resync(repo_full_name, username, True)
        logger.warning(
            f"{repo_full_name}: force-push — помечен needs_full_resync "
            f"для {analyzed_usernames}, дальше досинхронизирует обычный /analyze"
        )
        return {"status": "flagged_resync", "repo": repo_full_name}

    commit_entries = payload.get("commits") or []
    if not commit_entries and payload.get("head_commit"):
        commit_entries = [payload["head_commit"]]
    if not commit_entries:
        return {"status": "skipped", "reason": "no commits in payload"}

    client = await get_github_client()
    owner, name = repo_full_name.split("/", 1)
    now = datetime.now(timezone.utc).isoformat()

    rows_by_user: dict[str, list[dict]] = {u: [] for u in analyzed_usernames}

    for entry in commit_entries:
        sha = entry.get("id") or entry.get("sha")
        if not sha:
            continue
        try:
            raw = await client.get(f"/repos/{owner}/{name}/commits/{sha}")
        except Exception as e:
            logger.warning(f"{repo_full_name}: не удалось получить {sha}: {e}")
            continue
        if not raw:
            continue

        # Мёрж-коммиты фильтруем так же, как в остальном пайплайне
        # (CommitHeader.is_merge -> parents_count > 1).
        if len(raw.get("parents") or []) > 1:
            continue

        commit_author_login = (raw.get("author") or {}).get("login", "")
        commit_meta = raw.get("commit") or {}
        commit_date = (commit_meta.get("author") or {}).get("date")
        message = (commit_meta.get("message") or "").split("\n")[0][:72]
        stats = raw.get("stats") or {}
        files = [
            [f.get("filename", ""), f.get("additions", 0), f.get("deletions", 0)]
            for f in (raw.get("files") or [])
        ]

        row_base = {
            "sha": sha,
            "repo_full_name": repo_full_name,
            "committed_at": commit_date,
            "message": message,
            "additions": stats.get("additions", 0),
            "deletions": stats.get("deletions", 0),
            "changed_files": len(files),
            "files_json": json.dumps(files, ensure_ascii=False) if files else None,
            "created_at": now,
        }

        for username in analyzed_usernames:
            if commit_author_login.lower() == username.lower():
                rows_by_user[username].append(
                    {**row_base, "analyzed_username": username}
                )

    total_written = 0
    for username, rows in rows_by_user.items():
        if not rows:
            continue
        await upsert_commits(rows)
        total_written += len(rows)
        await upsert_tracked_repo(
            repo_full_name=repo_full_name,
            analyzed_username=username,
            owner_login=owner_login or owner,
            default_branch=default_branch,
            last_synced_sha=rows[-1]["sha"],
            last_synced_at=now,
            last_pushed_at=now,
            needs_full_resync=False,
            sync_mode="webhook",
        )

    logger.info(f"{repo_full_name}: webhook добавил {total_written} коммит(ов)")
    return {"status": "done", "repo": repo_full_name, "commits": total_written}