"""
Конфигурация arq-воркера.
"""

from arq import cron
from src.worker.tasks import (
    analyze_github_user,
    handle_push_event,
    reconcile_webhook_repos,
    silent_background_sync,
)
from src.storage.database import init_db, recover_stuck_requests
from arq.connections import RedisSettings
from src.logger import get_logger
from src.config import settings


logger = get_logger(__name__)


class WorkerSettings:
    functions = [
        analyze_github_user,
        handle_push_event,
        reconcile_webhook_repos,
        silent_background_sync,
    ]

    cron_jobs = [cron(reconcile_webhook_repos, hour={0, 6, 12, 18}, minute=0)]

    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    max_jobs = 3
    job_timeout = 300
    keep_result = 3600

    @staticmethod
    async def on_startup(ctx):
        await init_db()
        recovered = await recover_stuck_requests()
        if recovered:
            logger.info(f"Восстановлено зависших задач: {recovered}")
        logger.info("Worker started")

    @staticmethod
    async def on_shutdown(ctx):
        from src.storage.redis import close_redis
        from src.github.client import close_github_client

        await close_redis()
        await close_github_client()