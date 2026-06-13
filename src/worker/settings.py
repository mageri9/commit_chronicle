"""
Конфигурация arq-воркера.
"""

from src.worker.tasks import analyze_github_user
from src.storage.database import init_db, recover_stuck_requests
from arq.connections import RedisSettings


class WorkerSettings:
    functions = [analyze_github_user]

    redis_settings = RedisSettings(
        host="localhost",
        port=6379,
        database=0,
    )

    max_jobs = 3
    job_timeout = 300
    keep_result = 3600

    @staticmethod
    async def on_startup(ctx):
        await init_db()
        recovered = await recover_stuck_requests()
        if recovered:
            print(f"♻️  Восстановлено зависших задач: {recovered}")

    @staticmethod
    async def on_shutdown(ctx):
        pass