"""
Конфигурация arq-воркера.
"""

from src.worker.tasks import analyze_github_user
from src.storage.database import init_db, recover_stuck_requests
from arq.connections import RedisSettings
from src.logger import get_logger
from src.config import settings


logger = get_logger(__name__)


class WorkerSettings:
    functions = [analyze_github_user]

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
        pass