"""
Фоновые задачи для arq.
"""

import json
from datetime import datetime

from src.config import settings
from src.core.collector import collect_commits
from src.github.fingerprint import get_github_fingerprint
from src.github.service import get_github_service
from src.models.models import serialize_result
from src.storage.database import (
    create_request,
    find_existing_requests,
    update_request_status,
)
from src.storage.pubsub import publish
from src.logger import get_logger

logger = get_logger(__name__)


async def analyze_github_user(
    ctx, username: str, period_start: str, chat_id: str = ""
) -> dict:
    """
    Пайплайн анализа GitHub-пользователя (GitHub Engine).

    Шаги:
        1. Дедупликация — проверить существующий запрос в БД.
        2. Fingerprint-проверка — если есть done-запрос, сравнить SHA256.
        3. Создать новый запрос и запустить collector.
        4. Сохранить результат и уведомить бота через pub/sub.
    """
    request_id = ctx["job_id"]
    period_end = datetime.now().strftime("%Y-%m-%d")

    if not chat_id:
        logger.warning(f"[{request_id}] chat_id пустой — уведомление не дойдёт")

    service = await get_github_service()

    # 1. Дедупликация — уже анализировали или анализируем?
    existing = await find_existing_requests(username, period_start, period_end)
    if existing:
        if existing["status"] == "processing":
            await publish(
                "job:done",
                json.dumps(
                    {
                        "job_id": existing["id"],
                        "status": "processing",
                        "username": username,
                    }
                ),
            )
            return {
                "status": "processing",
                "request_id": existing["id"],
                "source": "existing_request",
                "result_json": None,
            }

        if existing["status"] == "done":
            current_fp = await get_github_fingerprint(service, username)

            if current_fp and current_fp == existing.get("fingerprint"):
                await create_request(
                    request_id=request_id,
                    username=username,
                    period_start=period_start,
                    period_end=period_end,
                    chat_id=chat_id,
                )
                await update_request_status(
                    request_id,
                    "done",
                    result_json=existing["result_json"],
                    fingerprint=current_fp,
                )
                await publish(
                    "job:done",
                    json.dumps(
                        {
                            "job_id": request_id,
                            "status": "done",
                            "username": username,
                        }
                    ),
                )
                return {
                    "status": "done",
                    "request_id": request_id,
                    "source": "dedup",
                    "result_json": existing["result_json"],
                }

            logger.info(f"[{request_id}] Fingerprint изменился — пересобираем данные")

    # 2. Новый запрос — зарегистрировать в БД
    await create_request(
        request_id=request_id,
        username=username,
        period_start=period_start,
        period_end=period_end,
        chat_id=chat_id,
    )
    await update_request_status(request_id, "processing")

    # 3. Fingerprint фиксируем ДО сборки — отражает состояние репо на старте.
    fingerprint = await get_github_fingerprint(service, username)

    # 4. Собрать коммиты
    try:
        result = await collect_commits(
            username, period_start, max_concurrency=settings.max_workers
        )

        result_json = serialize_result(result)

        await update_request_status(
            request_id,
            "done",
            result_json=result_json,
            fingerprint=fingerprint,
        )
        await publish(
            "job:done",
            json.dumps(
                {
                    "job_id": request_id,
                    "status": "done",
                    "username": username,
                }
            ),
        )
        return {
            "status": "done",
            "request_id": request_id,
            "source": "collector",
            "result_json": result_json,
        }

    except Exception as e:
        logger.exception(f"[{request_id}] Ошибка сборки для {username}: {e}")

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
    """
    Собрать текстовую сводку из результатов анализа.

    Ожидает формат CompactResult: {"repos": {"repo_name": [commits...]}, ...}
    """
    data = json.loads(result_json)
    repos: dict = data.get("repos", {})
    total_commits = sum(len(commits) for commits in repos.values())
    repo_count = len(repos)

    return (
        f"✅ Анализ готов\n"
        f"📦 Коммитов: {total_commits}\n"
        f"📁 Репозиториев: {repo_count}\n"
    )