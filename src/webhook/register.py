"""
Разовый CLI-скрипт: регистрирует GitHub push-вебхук на репозитории
owner_github_username (см. .env), используя settings.github_token — токен
должен иметь права admin:repo_hook на эти репозитории (то есть это ДОЛЖНЫ
быть твои собственные репозитории, не чужие — для чужих у бота таких прав
нет и не будет без отдельного OAuth-флоу, см. README).

Использование:
    python -m src.webhook.register                  # все не-форк репо owner_github_username
    python -m src.webhook.register owner/repo1 owner/repo2   # только перечисленные

Идемпотентен: повторный запуск на уже подключённом репозитории обновит
webhook (создаст новый, если старый не находится по сохранённому id) —
GitHub не даёт создать два одинаковых хука на один URL, вернёт 422,
это логируется и пропускается, а не падает всем скриптом.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from src.config import settings
from src.github.client import get_github_client, close_github_client
from src.github.exceptions import GitHubAPIError
from src.github.service import get_github_service
from src.logger import get_logger
from src.storage.database import init_db, upsert_tracked_repo

logger = get_logger(__name__)


async def _register_one(client, repo_full_name: str) -> str | None:
    owner, name = repo_full_name.split("/", 1)
    try:
        payload = await client.post(
            f"/repos/{owner}/{name}/hooks",
            json_body={
                "name": "web",
                "active": True,
                "events": ["push"],
                "config": {
                    "url": settings.webhook_public_url,
                    "content_type": "json",
                    "secret": settings.webhook_secret,
                    "insecure_ssl": "0",
                },
            },
        )
        return str(payload.get("id")) if payload else None
    except GitHubAPIError as e:
        if e.status_code == 422:
            logger.info(f"{repo_full_name}: хук уже существует (422) — пропуск")
        else:
            logger.warning(f"{repo_full_name}: не удалось создать хук ({e})")
        return None


async def main(repo_args: list[str]) -> None:
    if not settings.webhook_enabled:
        raise SystemExit(
            "WEBHOOK_ENABLED=false — включи в .env перед регистрацией хуков"
        )
    if not settings.webhook_public_url:
        raise SystemExit("WEBHOOK_PUBLIC_URL пустой — GitHub должен куда-то стучаться")

    await init_db()

    if repo_args:
        repo_full_names = repo_args
        owner_login = settings.owner_github_username or repo_args[0].split("/")[0]
    else:
        if not settings.owner_github_username:
            raise SystemExit(
                "Укажи OWNER_GITHUB_USERNAME в .env или передай репозитории явно"
            )
        service = await get_github_service()
        repos = await service.list_repositories(settings.owner_github_username)
        repo_full_names = [r.full_name for r in repos if not r.is_fork]
        owner_login = settings.owner_github_username

    client = await get_github_client()
    now = datetime.now(timezone.utc).isoformat()

    for repo_full_name in repo_full_names:
        webhook_id = await _register_one(client, repo_full_name)
        await upsert_tracked_repo(
            repo_full_name=repo_full_name,
            analyzed_username=owner_login,
            owner_login=repo_full_name.split("/")[0],
            default_branch=None,  # уточнится при следующей обычной синхронизации
            last_synced_sha=None,
            last_synced_at=now,
            last_pushed_at=None,
            sync_mode="webhook" if webhook_id else "poll",
        )
        status = (
            f"webhook_id={webhook_id}"
            if webhook_id
            else "не подключён, fallback на poll"
        )
        print(f"{repo_full_name}: {status}")

    await close_github_client()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))