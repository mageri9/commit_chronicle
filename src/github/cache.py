"""
Repository-level cache истории коммитов.

Кеширует список CommitHeader на репозиторий, инвалидируется сравнением
Repository.pushed_at: если pushed_at репозитория не изменился с момента
последнего сохранения в кеш — GraphQL/REST запросы за этот репозиторий
не делаются вообще, отдаётся сохранённый список.

Осознанно НЕ кеширует CommitDetails (файлы/patch) — они тяжёлые, и если
pushed_at не изменился, для уже виденных коммитов новых деталей и не
появится: кешируем именно то, что имеет смысл переиспользовать.

Использует общий Redis-клиент из src/storage/redis.py — это адаптер
инфраструктурного слоя поверх уже существующего подключения, а не
отдельный источник соединения к Redis.
"""

from __future__ import annotations

import json

from src.config import settings
from src.github.models import CommitHeader, Repository
from src.logger import get_logger
from src.storage.redis import get_redis

logger = get_logger(__name__)

_KEY_PREFIX = "ghcache:commits"


def _cache_key(repo: Repository, since: str | None) -> str:
    return f"{_KEY_PREFIX}:{repo.full_name}:{since or 'all'}"


async def get_cached_history(
    repo: Repository, *, since: str | None = None
) -> list[CommitHeader] | None:
    """
    Вернуть закешированный список CommitHeader, если pushed_at репозитория
    совпадает с тем, что было на момент сохранения кеша.

    None означает "кеша нет, идти в API" — репозиторий изменился с момента
    сохранения, кеш протух по TTL, данные повреждены, или Redis недоступен.
    Любая из этих причин обрабатывается одинаково — не роняем pipeline
    из-за кеша, просто не используем его.
    """
    try:
        r = await get_redis()
        raw = await r.get(_cache_key(repo, since))
    except Exception as e:
        logger.warning(f"github cache get failed for {repo.full_name}: {e}")
        return None

    if raw is None:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"github cache corrupt for {repo.full_name}: {e}")
        return None

    cached_pushed_at = payload.get("pushed_at")
    current_pushed_at = repo.pushed_at.isoformat() if repo.pushed_at else None

    if cached_pushed_at != current_pushed_at:
        logger.debug(
            f"{repo.full_name}: pushed_at изменился "
            f"({cached_pushed_at} → {current_pushed_at}) — кеш невалиден"
        )
        return None

    try:
        return [
            CommitHeader.model_validate(node) for node in payload.get("headers", [])
        ]
    except Exception as e:
        logger.warning(f"github cache deserialize failed for {repo.full_name}: {e}")
        return None


async def set_cached_history(
    repo: Repository, headers: list[CommitHeader], *, since: str | None = None
) -> None:
    """Сохранить список CommitHeader вместе с текущим pushed_at репозитория."""
    payload = {
        "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
        "headers": [h.model_dump(mode="json") for h in headers],
    }

    try:
        r = await get_redis()
        await r.set(
            _cache_key(repo, since),
            json.dumps(payload, ensure_ascii=False),
            ex=settings.cache_ttl_github,
        )
    except Exception as e:
        logger.warning(f"github cache set failed for {repo.full_name}: {e}")