"""
Ручной smoke-тест: ставит реальную задачу в очередь arq и ждёт результат.

Не является pytest-тестом (требует живого Redis + запущенного воркера).
Используется для быстрой проверки пайплайна руками при разработке:

    python -m arq src.worker.settings.WorkerSettings &
    python tests/manual_smoke_enqueue.py
"""

import asyncio
from arq import create_pool
from arq.connections import RedisSettings


async def main():
    pool = await create_pool(RedisSettings(host="localhost", port=6379, database=0))
    job = await pool.enqueue_job("analyze_github_user", "mageri9", "2024-01-01")
    print(f"Job ID: {job.job_id}")

    result = await job.result(timeout=120)
    print(f"Status: {result['status']}")
    if result.get("result_json"):
        print(f"Data: {len(result['result_json'])} chars")


if __name__ == "__main__":
    asyncio.run(main())