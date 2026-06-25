"""
Фоновые задачи для arq.
"""

import asyncio
from datetime import datetime
from src.logger import get_logger


from src.config import settings
from src.core.collector import collect_commits
from src.core.token_rotator import token_rotator
from src.models import AnalysisResult
from src.storage.database import (
    create_request,
    update_request_status,
    find_existing_requests,
)
from src.storage.pubsub import publish
import json
from src.models.models import serialize_result
from src.core.fingerprint import get_github_fingerprint


logger = get_logger(__name__)


async def analyze_github_user(
    ctx, username: str, period_start: str, chat_id: str = ""
) -> dict:
    """
    Пайплайн анализа GitHub-пользователя.
    """
    request_id = ctx["job_id"]
    period_end = datetime.now().strftime("%Y-%m-%d")

    # 1. Existing request — уже анализировали или анализируем
    existing = await find_existing_requests(username, period_start, period_end)
    if existing:
        if existing["status"] == "processing":
            return {
                "status": "processing",
                "request_id": existing["id"],
                "source": "existing_request",
                "result_json": None,
            }
        # Дедуп
        if existing["status"] == "done":
            loop = asyncio.get_running_loop()
            current_fp = await loop.run_in_executor(
                None,
                get_github_fingerprint,
                username,
            )

            if current_fp and current_fp == existing.get("fingerprint"):
                # Данные свежие
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
                    fingerprint=existing.get("fingerprint"),
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
            logger.info("Данные устарели (fingerprint), пересобираем")

    # 2. Новый запрос — создать и запустить collector
    await create_request(
        request_id=request_id,
        username=username,
        period_start=period_start,
        period_end=period_end,
        chat_id=chat_id,
    )
    await update_request_status(request_id, "processing")

    # 3. Запустить collector в отдельном потоке
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            collect_commits,
            username,
            period_start,
            settings.max_workers,
        )

        # 4. Успех — сохранить
        result_json = serialize_result(result)

        fingerprint = await loop.run_in_executor(
            None,
            get_github_fingerprint,
            username,
        )

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

    # 6. Ошибка — зафиксировать
    except Exception as e:
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
    """Собрать текстовую сводку из результатов анализа."""
    data = json.loads(result_json)

    if "repos" in data:
        total_commits = sum(len(commits) for commits in data["repos"].values())
        repo_count = len(data["repos"])
    else:
        total_commits = len(data["commits"])
        repo_count = len(set(c["repo"] for c in data["commits"]))

    return (
        f"✅ Анализ готов\n"
        f"📦 Коммитов: {total_commits}\n"
        f"📁 Репозиториев: {repo_count}\n"
    )