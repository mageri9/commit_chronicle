"""
Сборка отчёта (AnalysisResult) из постоянного хранилища commits — без
обращения к GitHub API. Используется вместо collector.collect_commits()
всякий раз, когда данные уже синхронизированы (см. src.core.sync).

Формат вывода не меняется — дальше как обычно идёт
src.models.models.to_compact()/serialize_result(), отчёт для LLM/Telegram
получается идентичным тому, что раньше собирал collector.
"""

from __future__ import annotations

import json
from datetime import datetime

from src.models.models import AnalysisResult, Commit, FileChange
from src.storage.database import get_commits


async def build_analysis_result(
    analyzed_username: str,
    period_start: str,
    period_end: str,
    repo_full_name: str | None = None,
) -> AnalysisResult:
    """
    repo_full_name=None -> отчёт по всем отслеживаемым репозиториям юзера.
    repo_full_name="owner/name" -> отчёт только по одному репозиторию —
    это и есть разделение "весь профиль / конкретный репо" из ТЗ.
    """
    rows = await get_commits(
        analyzed_username,
        period_start=period_start,
        period_end=period_end,
        repo_full_name=repo_full_name,
    )

    commits: list[Commit] = []
    for row in rows:
        raw_files = json.loads(row["files_json"]) if row.get("files_json") else []
        files = [
            FileChange(filename=f[0], additions=f[1], deletions=f[2]) for f in raw_files
        ]
        commits.append(
            Commit(
                hash=row["sha"][:7],
                date=datetime.fromisoformat(row["committed_at"]),
                message=row["message"],
                repo=row["repo_full_name"],
                files=files,
            )
        )

    return AnalysisResult(
        username=analyzed_username,
        period_start=period_start,
        commits=commits,
        generated_at=datetime.now(),
    )


async def is_report_fresh(
    analyzed_username: str,
    *,
    repo_full_name: str | None = None,  # <-- Добавлен параметр для точечной проверки
    max_age_seconds: int = 120,
) -> bool:
    """
    Грубая проверка "можно ли отдать отчёт мгновенно, без похода в GitHub".

    Смотрим last_synced_at по отслеживаемым репо — если синхронизировались
    недавно, считаем данные достаточно свежими для мгновенной выдачи из БД.

    ВАЖНО: это допущение верно только для sync_mode="webhook" — там между
    синками БД обновляется пушами в реальном времени, так что "давно
    синкались, но ничего нового и не могло появиться" — обоснованное
    предположение. Для sync_mode="poll" никто не сообщает боту о новых
    коммитах между вызовами /analyze: last_synced_at "свежий" только
    потому, что мы недавно САМИ его туда записали, а не потому, что с тех
    пор в GitHub точно ничего не изменилось. Поэтому для чисто поллинговых
    репозиториев fast-path отключаем — /analyze должен реально дойти до
    GitHub и досинхронизироваться, а не отдать снэпшот из БД.
    """
    from src.storage.database import (
        list_tracked_repos,
    )  # локальный импорт — избегаем цикла

    repos = await list_tracked_repos(analyzed_username)
    if not repos:
        return False

    # Если запрошен конкретный репозиторий — фильтруем список в Python,
    # проверяя только его актуальность для мгновенного fast-path
    if repo_full_name:
        repos = [r for r in repos if r["repo_full_name"] == repo_full_name]
        if not repos:
            return False

    now = datetime.now().astimezone()
    for repo in repos:
        if repo.get("sync_mode") != "webhook":
            return False
        last_synced_at = repo.get("last_synced_at")
        if not last_synced_at:
            return False
        synced = datetime.fromisoformat(last_synced_at)
        if (now - synced).total_seconds() > max_age_seconds:
            return False
    return True