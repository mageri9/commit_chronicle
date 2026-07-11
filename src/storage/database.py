"""
SQLite через SQLAlchemy Core + aiosqlite.
WAL-режим для конкурентного чтения/записи.
"""

from datetime import datetime, timezone, timedelta

from sqlalchemy import (
    Boolean,
    Integer,
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


# ---------- Таблицы (существующие) ----------
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
    Column("notified", Boolean, nullable=False, default=False),
    Column("created_at", Text, nullable=False),
    Column("completed_at", Text),
)


# ---------- Таблицы (новые) ----------

tracked_repos = Table(
    "tracked_repos",
    metadata,
    # Составной PK: один и тот же репозиторий может отслеживаться в контексте
    # разных анализируемых профилей (например, участник + владелец).
    Column("repo_full_name", Text, primary_key=True),
    Column("analyzed_username", Text, primary_key=True),
    Column("owner_login", Text, nullable=False),
    Column("default_branch", Text),
    Column("last_synced_sha", Text),
    Column("last_synced_at", Text),
    Column("last_pushed_at", Text),
    # "poll" — обновляется только по запросу пользователя (Этап 1),
    # "webhook" — есть активный GitHub-вебхук (Этап 2, см. ТЗ).
    Column("sync_mode", Text, nullable=False, default="poll"),
    Column("webhook_id", Text),
    Column("is_active", Boolean, nullable=False, default=True),
    # Взводится при force-push / рассинхроне — следующая sync_repo_commits()
    # сделает полный пересбор вместо дельты, см. src.core.sync.
    Column("needs_full_resync", Boolean, nullable=False, default=False),
    Column("created_at", Text, nullable=False),
)

commits_table = Table(
    "commits",
    metadata,
    Column("sha", Text, primary_key=True),
    Column("repo_full_name", Text, primary_key=True),
    Column("analyzed_username", Text, primary_key=True),
    Column("committed_at", Text, nullable=False),
    Column("message", Text, nullable=False, default=""),
    Column("additions", Integer, nullable=False, default=0),
    Column("deletions", Integer, nullable=False, default=0),
    Column("changed_files", Integer, nullable=False, default=0),
    # Компактный список файлов как JSON-массив [[filename, +, -], ...],
    # формат согласован с models.py::_file_row, но без разбивки по ext-индексу —
    # индекс расширений строится один раз при сериализации отчёта, не при записи.
    Column("files_json", Text),
    Column("created_at", Text, nullable=False),
)

user_bindings = Table(
    "user_bindings",
    metadata,
    # telegram_id — строкой (как и chat_id в requests), т.к. в остальном коде
    # id из Telegram везде передаётся как str.
    Column("telegram_id", Text, primary_key=True),
    Column("github_username", Text, nullable=False),
    Column("linked_at", Text, nullable=False),
)


# ---------- Инициализация ----------
async def init_db() -> None:
    """Создать таблицы, включить WAL, создать индексы."""
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_existing_request "
                "ON requests (username, period_start, period_end, status, created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_commits_repo_period "
                "ON commits (repo_full_name, analyzed_username, committed_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_commits_user_period "
                "ON commits (analyzed_username, committed_at)"
            )
        )


# ---------- API: requests (без изменений) ----------
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
    """Обновить статус и опционально результат."""
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


async def find_existing_requests(
    username: str,
    period_start: str,
    period_end: str,
    processing_cutoff_seconds: int = 3600,
) -> dict | None:
    """Найти существующий запрос за тот же период (защита от дублей в очереди)."""
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
    """Пометить failed запросы, зависшие в processing дольше timeout."""
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


async def get_unnotified_requests() -> list[dict]:
    """Завершённые (done / failed) запросы, уведомление по которым ещё не отправлено."""
    async with engine.connect() as conn:
        result = await conn.execute(
            requests.select().where(
                requests.c.status.in_(["done", "failed"]),
                requests.c.notified == False,  # noqa: E712
            )
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def mark_as_notified(request_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            requests.update().where(requests.c.id == request_id).values(notified=True)
        )


async def get_user_binding(telegram_id: str) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            user_bindings.select().where(user_bindings.c.telegram_id == telegram_id)
        )
        row = result.first()
        # Используем безопасное обращение через _mapping
        return row._mapping["github_username"] if row else None


async def set_user_binding(telegram_id: str, github_username: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with engine.begin() as conn:
        await conn.execute(
            sqlite_insert(user_bindings)
            .values(
                telegram_id=telegram_id,
                github_username=github_username,
                linked_at=now,
            )
            .on_conflict_do_update(
                index_elements=["telegram_id"],
                set_={"github_username": github_username, "linked_at": now},
            )
        )


async def remove_user_binding(telegram_id: str) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(
            user_bindings.delete().where(user_bindings.c.telegram_id == telegram_id)
        )
        return result.rowcount > 0


# ---------- API: tracked_repos / commits ----------


async def get_tracked_repo(repo_full_name: str, analyzed_username: str) -> dict | None:
    """Состояние синхронизации одного репозитория. None — ещё не синхронизировали."""
    async with engine.connect() as conn:
        result = await conn.execute(
            tracked_repos.select().where(
                tracked_repos.c.repo_full_name == repo_full_name,
                tracked_repos.c.analyzed_username == analyzed_username,
            )
        )
        row = result.first()
        return dict(row._mapping) if row else None


async def upsert_tracked_repo(
    *,
    repo_full_name: str,
    analyzed_username: str,
    owner_login: str,
    default_branch: str | None,
    last_synced_sha: str | None,
    last_synced_at: str,
    last_pushed_at: str | None,
    needs_full_resync: bool = False,
    sync_mode: str = "poll",
    webhook_id: str | None = None,
) -> None:
    """Upsert состояния синхронизации. Вызывается из src.core.sync после
    каждой успешной (пусть и пустой) попытки синхронизации репозитория,
    а также из src.worker.tasks.handle_push_event и src.webhook.register."""
    now = datetime.now(timezone.utc).isoformat()
    set_ = {
        "default_branch": default_branch,
        "last_synced_sha": last_synced_sha,
        "last_synced_at": last_synced_at,
        "last_pushed_at": last_pushed_at,
        "needs_full_resync": needs_full_resync,
        "sync_mode": sync_mode,
    }
    # webhook_id намеренно НЕ затирается None-ом в конфликтующей ветке —
    # обычная poll-синхронизация (sync.py) вызывает эту функцию без него,
    # не хотим стирать уже сохранённый id при обычном /analyze.
    if webhook_id is not None:
        set_["webhook_id"] = webhook_id

    async with engine.begin() as conn:
        await conn.execute(
            sqlite_insert(tracked_repos)
            .values(
                repo_full_name=repo_full_name,
                analyzed_username=analyzed_username,
                owner_login=owner_login,
                default_branch=default_branch,
                last_synced_sha=last_synced_sha,
                last_synced_at=last_synced_at,
                last_pushed_at=last_pushed_at,
                sync_mode=sync_mode,
                webhook_id=webhook_id,
                is_active=True,
                needs_full_resync=needs_full_resync,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["repo_full_name", "analyzed_username"],
                set_=set_,
            )
        )


async def set_repo_needs_full_resync(
    repo_full_name: str, analyzed_username: str, value: bool = True
) -> None:
    """Взвести/снять флаг полного пересбора (force-push, ручной сброс через /repos)."""
    async with engine.begin() as conn:
        await conn.execute(
            tracked_repos.update()
            .where(
                tracked_repos.c.repo_full_name == repo_full_name,
                tracked_repos.c.analyzed_username == analyzed_username,
            )
            .values(needs_full_resync=value)
        )


async def set_repo_active(
    repo_full_name: str, analyzed_username: str, is_active: bool
) -> None:
    """Включить/выключить отслеживание репозитория (кнопки в /repos)."""
    async with engine.begin() as conn:
        await conn.execute(
            tracked_repos.update()
            .where(
                tracked_repos.c.repo_full_name == repo_full_name,
                tracked_repos.c.analyzed_username == analyzed_username,
            )
            .values(is_active=is_active)
        )


async def list_tracked_repos(analyzed_username: str) -> list[dict]:
    """Список отслеживаемых репозиториев юзера для /repos, свежие сверху."""
    async with engine.connect() as conn:
        result = await conn.execute(
            tracked_repos.select()
            .where(
                tracked_repos.c.analyzed_username == analyzed_username,
                tracked_repos.c.is_active == True,  # noqa: E712
            )
            .order_by(tracked_repos.c.last_pushed_at.desc())
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def list_all_tracked_repos(analyzed_username: str) -> list[dict]:
    """Список абсолютно всех отслеживаемых репозиториев юзера (активных и неактивных)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            tracked_repos.select()
            .where(tracked_repos.c.analyzed_username == analyzed_username)
            .order_by(tracked_repos.c.last_pushed_at.desc())
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def list_repos_by_sync_mode(sync_mode: str) -> list[dict]:
    """Список активных отслеживаемых репозиториев по режиму синхронизации."""
    async with engine.connect() as conn:
        result = await conn.execute(
            tracked_repos.select().where(
                tracked_repos.c.sync_mode == sync_mode,
                tracked_repos.c.is_active == True,  # noqa: E712
            )
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def get_tracked_repos_for_repo(repo_full_name: str) -> list[dict]:
    """
    Все записи tracked_repos для данного репозитория, по всем analyzed_username.
    Нужно вебхуку: push-событие приходит на репозиторий, а не на конкретный
    "анализируемый профиль" — один репозиторий может отслеживаться в контексте
    нескольких профилей (см. схему tracked_repos в ТЗ).
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            tracked_repos.select().where(
                tracked_repos.c.repo_full_name == repo_full_name,
                tracked_repos.c.is_active == True,  # noqa: E712
            )
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def upsert_commits(rows: list[dict]) -> None:
    """
    Bulk upsert коммитов. Идемпотентно — повторная запись того же
    (sha, repo_full_name, analyzed_username) перезаписывает данные.

    Реализовано построчно (не executemany) ради простоты ON CONFLICT —
    для типичного размера дельты (единицы-десятки коммитов за push/период
    между запросами) это не узкое место. При росте объёма — заменить на
    один INSERT ... ON CONFLICT с несколькими VALUES.
    """
    if not rows:
        return
    async with engine.begin() as conn:
        for row in rows:
            update_fields = {
                k: v
                for k, v in row.items()
                if k not in ("sha", "repo_full_name", "analyzed_username")
            }
            await conn.execute(
                sqlite_insert(commits_table)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=["sha", "repo_full_name", "analyzed_username"],
                    set_=update_fields,
                )
            )


async def get_commits(
    analyzed_username: str,
    *,
    period_start: str,
    period_end: str,
    repo_full_name: str | None = None,
) -> list[dict]:
    """
    Выборка коммитов из постоянного хранилища для сборки отчёта
    (src.core.report.build_analysis_result).

    period_start/period_end — "YYYY-MM-DD". Сравнение строковое (ISO
    лексикографически сортируется), поэтому period_end нормализуется до
    конца дня — иначе коммиты со временем > 00:00:00 в last day терялись бы.
    """
    period_end_inclusive = f"{period_end}T23:59:59+00:00"

    query = commits_table.select().where(
        commits_table.c.analyzed_username == analyzed_username,
        commits_table.c.committed_at >= period_start,
        commits_table.c.committed_at <= period_end_inclusive,
    )
    if repo_full_name:
        query = query.where(commits_table.c.repo_full_name == repo_full_name)
    query = query.order_by(commits_table.c.committed_at)

    async with engine.connect() as conn:
        result = await conn.execute(query)
        return [dict(row._mapping) for row in result.fetchall()]


async def get_last_sync_time(
    analyzed_username: str, repo_full_name: str | None = None
) -> datetime | None:
    """Возвращает самую старую (минимальную) дату синхронизации среди активных репозиториев пользователя."""
    query = tracked_repos.select().where(
        tracked_repos.c.analyzed_username == analyzed_username,
        tracked_repos.c.is_active == True,  # noqa: E712
    )
    if repo_full_name:
        query = query.where(tracked_repos.c.repo_full_name == repo_full_name)

    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.fetchall()

    if not rows:
        return None

    min_dt = None
    for row in rows:
        val = row._mapping.get("last_synced_at")
        if not val:
            # Если хотя бы один активный репозиторий еще ни разу не синхронизировался — отчет не свежий
            return None
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if min_dt is None or dt < min_dt:
                min_dt = dt
        except Exception:
            return None
    return min_dt