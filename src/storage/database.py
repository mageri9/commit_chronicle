"""
SQLite через SQLAlchemy Core + aiosqlite.
WAL-режим для конкурентного чтения/записи.

Заметки по архитектуре:
- db_lock убран: WAL + aiosqlite connection pool сами сериализуют запись.
  asyncio.Lock на уровне модуля ломается в multi-process окружении (arq worker
  и bot — разные процессы, разные event loop'ы).
- notified: Boolean (0/1) вместо Text("true"/"false") — ловится статическим анализом.
- set_cached_commits: upsert через INSERT OR REPLACE — безопасен при повторных вызовах.
- find_existing_requests: cutoff применяется только к "processing",
  "done" без ограничения по времени — fingerprint сам решает актуальность.
- completed_at: пишется только при финальных статусах (done / failed).
"""

from datetime import datetime, timezone, timedelta

from sqlalchemy import (
    Boolean,
    MetaData,
    Table,
    Column,
    Text,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from src.config import settings

# ---------- Engine ----------
# pool_pre_ping=True — переоткрывает соединение если оно протухло при простое
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)
metadata = MetaData()


# ---------- Таблицы ----------
requests = Table(
    "requests",
    metadata,
    Column("id", Text, primary_key=True),
    Column("username", Text, nullable=False),
    Column("chat_id", Text, nullable=False, default=""),
    Column("period_start", Text, nullable=False),
    Column("period_end", Text, nullable=False),
    Column("status", Text, nullable=False, default="pending"),
    Column("result_json", Text),
    Column("fingerprint", Text),
    Column("error_message", Text),
    # Boolean → SQLite INTEGER 0/1; default=False, не "false"
    Column("notified", Boolean, nullable=False, default=False),
    Column("created_at", Text, nullable=False),
    # completed_at — только при финальных статусах (done / failed)
    Column("completed_at", Text),
)

# Составной PRIMARY KEY создаёт индекс автоматически — отдельный не нужен
commit_cache = Table(
    "commit_cache",
    metadata,
    Column("username", Text, primary_key=True),
    Column("repo", Text, primary_key=True),
    Column("period_start", Text, primary_key=True),
    Column("period_end", Text, primary_key=True),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Text, nullable=False),
)


# ---------- Инициализация ----------
async def init_db() -> None:
    """Создать таблицы, включить WAL, создать индексы."""
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(metadata.create_all)
        # notified убран из индекса — не используется в WHERE, только в SET
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_existing_request "
                "ON requests (username, period_start, period_end, status, created_at)"
            )
        )


# ---------- API ----------
async def create_request(
    request_id: str,
    username: str,
    period_start: str,
    period_end: str,
    chat_id: str = "",
) -> str:
    """Создать запрос → вернуть ID."""
    now = datetime.now(timezone.utc).isoformat()

    async with engine.begin() as conn:
        await conn.execute(
            requests.insert().values(
                id=request_id,
                username=username,
                chat_id=chat_id,
                period_start=period_start,
                period_end=period_end,
                status="pending",
                created_at=now,
            )
        )
    return request_id


async def update_request_status(
    request_id: str,
    status: str,
    result_json: str | None = None,
    fingerprint: str | None = None,
    error_message: str | None = None,
) -> None:
    """Обновить статус и опционально результат.

    completed_at проставляется только для финальных статусов (done / failed) —
    не при переходе в "processing".
    """
    now = datetime.now(timezone.utc).isoformat()
    values: dict = {"status": status, "notified": False}

    if status in ("done", "failed"):
        values["completed_at"] = now
    if result_json is not None:
        values["result_json"] = result_json
    if fingerprint is not None:
        values["fingerprint"] = fingerprint
    if error_message is not None:
        values["error_message"] = error_message

    async with engine.begin() as conn:
        await conn.execute(
            requests.update().where(requests.c.id == request_id).values(**values)
        )


async def get_request(request_id: str) -> dict | None:
    """Получить запрос по ID."""
    async with engine.connect() as conn:
        result = await conn.execute(
            requests.select().where(requests.c.id == request_id)
        )
        row = result.first()
        return dict(row._mapping) if row else None


async def get_cached_commits(
    username: str,
    repo: str,
    period_start: str,
    period_end: str,
) -> str | None:
    """Достать закешированные коммиты (JSON-строка или None)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            commit_cache.select().where(
                commit_cache.c.username == username,
                commit_cache.c.repo == repo,
                commit_cache.c.period_start == period_start,
                commit_cache.c.period_end == period_end,
            )
        )
        row = result.first()
        return row.data_json if row else None


async def set_cached_commits(
    username: str,
    repo: str,
    period_start: str,
    period_end: str,
    data_json: str,
) -> None:
    """Положить коммиты в кеш (upsert — безопасен при повторных вызовах)."""
    now = datetime.now(timezone.utc).isoformat()

    async with engine.begin() as conn:
        await conn.execute(
            sqlite_insert(commit_cache)
            .values(
                username=username,
                repo=repo,
                period_start=period_start,
                period_end=period_end,
                data_json=data_json,
                cached_at=now,
            )
            .on_conflict_do_update(
                index_elements=["username", "repo", "period_start", "period_end"],
                set_={"data_json": data_json, "cached_at": now},
            )
        )


async def find_existing_requests(
    username: str,
    period_start: str,
    period_end: str,
    processing_cutoff_seconds: int = 3600,
) -> dict | None:
    """Найти существующий запрос за тот же период.

    Логика cutoff разделена по статусу:
    - "processing" — только в пределах processing_cutoff_seconds (защита от дублей
      в очереди; зависший processing старше часа не считается активным).
    - "done" — без ограничения по времени; fingerprint в tasks.py сам решает
      актуальность данных, отсекать по времени здесь неверно.

    Возвращает самую свежую запись или None.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=processing_cutoff_seconds)
    ).isoformat()

    async with engine.connect() as conn:
        result = await conn.execute(
            requests.select()
            .where(
                requests.c.username == username,
                requests.c.period_start == period_start,
                requests.c.period_end == period_end,
                # "done" — всегда; "processing" — только свежие
                (requests.c.status == "done")
                | (
                    (requests.c.status == "processing")
                    & (requests.c.created_at >= cutoff)
                ),
            )
            .order_by(requests.c.created_at.desc())
            .limit(1)
        )
        row = result.first()
        return dict(row._mapping) if row else None


async def recover_stuck_requests(timeout_minutes: int = 15) -> int:
    """Пометить failed запросы, зависшие в processing дольше timeout.

    Возвращает количество исправленных записей.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    ).isoformat()

    async with engine.begin() as conn:
        result = await conn.execute(
            requests.update()
            .where(
                requests.c.status == "processing",
                requests.c.created_at < cutoff,
            )
            .values(
                status="failed",
                error_message="Worker crashed or timed out",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        return result.rowcount