"""
Fingerprint текущего состояния GitHub-репозиториев пользователя —
GitHub Engine версия (заменяет src/core/fingerprint.py, удалённый в
финальной зачистке).

Тот же принцип: SHA256 по отсортированному списку "owner/name:pushedAt"
для всех не-форк репозиториев. Используется в worker/tasks.py для
дедупликации — если fingerprint не изменился с последнего "done"-запроса,
свежий сбор не нужен, отдаётся кэш из БД.

В отличие от старой версии — не создаёт отдельное GitHub-соединение,
работает через тот же GitHubService (list_repositories), что и весь
остальной пайплайн, не тратя лишний токен/HTTP-клиент только ради
фингерпринта.
"""

from __future__ import annotations

import hashlib

from src.github.service import GitHubService


async def get_github_fingerprint(service: GitHubService, username: str) -> str:
    """
    Отпечаток состояния GitHub: repo:pushed_at для всех не-форк репозиториев.

    Пустая строка при любой ошибке (не удалось получить список репо) —
    вызывающий код трактует это как "fingerprint неизвестен" и просто
    идёт собирать данные заново, без исключения — как и в PyGithub-версии.
    """
    try:
        repos = await service.list_repositories(username)
    except Exception:
        return ""

    data = [
        f"{repo.full_name}:{repo.pushed_at.isoformat() if repo.pushed_at else 'none'}"
        for repo in repos
        if not repo.is_fork
    ]
    data.sort()
    raw = "|".join(data)

    return hashlib.sha256(raw.encode()).hexdigest()