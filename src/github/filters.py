"""
Правила отсева — когда коммиту РЕАЛЬНО нужен REST-запрос за файлами,
а когда можно сэкономить и обойтись только CommitHeader из GraphQL.
Мёрж-коммиты сюда не относятся — отфильтровываются раньше, на уровне
пайплайна (service.py), до needs_rest_details вообще не доходят.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.github.models import CommitHeader


@dataclass(frozen=True)
class RestDetailsPolicy:
    """max_age_days — коммиты старше не добираются файлами через REST,
    даже если были реальные изменения. None — без ограничения по возрасту."""

    max_age_days: int | None = None


_DEFAULT_POLICY = RestDetailsPolicy()


def needs_rest_details(
    header: CommitHeader,
    *,
    policy: RestDetailsPolicy | None = None,
    now: datetime | None = None,
) -> bool:
    """
    Правила (первое совпавшее решает):
        1. changed_files == 0          -> False
        2. additions == deletions == 0 -> False
        3. коммит старше max_age_days  -> False
        4. иначе                       -> True
    """
    policy = policy or _DEFAULT_POLICY

    if header.changed_files == 0:
        return False

    if header.additions == 0 and header.deletions == 0:
        return False

    if policy.max_age_days is not None:
        reference = now or datetime.now(timezone.utc)
        commit_date = header.date
        if commit_date.tzinfo is None:
            commit_date = commit_date.replace(tzinfo=timezone.utc)
        age_days = (reference - commit_date).days
        if age_days > policy.max_age_days:
            return False

    return True