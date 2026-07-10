"""Тесты needs_rest_details (src/github/filters.py)."""

from datetime import datetime, timedelta, timezone

from src.github.filters import RestDetailsPolicy, needs_rest_details
from src.github.models import CommitHeader


def make_header(**overrides):
    defaults = dict(
        sha="abc1234",
        date=datetime.now(timezone.utc),
        message="fix: something",
        repo="owner/repo",
        additions=10,
        deletions=2,
        changed_files=1,
        parents_count=1,
    )
    defaults.update(overrides)
    return CommitHeader(**defaults)


def test_no_changed_files_does_not_need_rest():
    header = make_header(changed_files=0, additions=0, deletions=0)
    assert needs_rest_details(header) is False


def test_zero_diff_rename_or_chmod_does_not_need_rest():
    # changed_files > 0 но нет реальных строковых изменений (переименование,
    # chmod, пустой файл) — известное принятое ограничение (docs/adr/001).
    header = make_header(changed_files=1, additions=0, deletions=0)
    assert needs_rest_details(header) is False


def test_real_changes_need_rest():
    header = make_header(changed_files=1, additions=5, deletions=1)
    assert needs_rest_details(header) is True


def test_only_additions_still_need_rest():
    header = make_header(changed_files=1, additions=5, deletions=0)
    assert needs_rest_details(header) is True


def test_only_deletions_still_need_rest():
    header = make_header(changed_files=1, additions=0, deletions=5)
    assert needs_rest_details(header) is True


def test_no_policy_ignores_commit_age():
    ancient = make_header(date=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert needs_rest_details(ancient) is True


def test_max_age_policy_excludes_old_commits():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_header = make_header(date=now - timedelta(days=400))
    policy = RestDetailsPolicy(max_age_days=365)

    assert needs_rest_details(old_header, policy=policy, now=now) is False


def test_max_age_policy_keeps_recent_commits():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent_header = make_header(date=now - timedelta(days=10))
    policy = RestDetailsPolicy(max_age_days=365)

    assert needs_rest_details(recent_header, policy=policy, now=now) is True


def test_max_age_policy_handles_naive_commit_datetime():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    naive_header = make_header(date=datetime(2025, 1, 1))  # без tzinfo
    policy = RestDetailsPolicy(max_age_days=30)

    assert needs_rest_details(naive_header, policy=policy, now=now) is False


def test_zero_diff_rule_takes_priority_over_age_policy():
    # Правило 2 (нет реальных изменений) применяется раньше правила
    # возраста — даже свежий "пустой" коммит не уйдёт в REST.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    header = make_header(date=now, changed_files=1, additions=0, deletions=0)
    policy = RestDetailsPolicy(max_age_days=3650)

    assert needs_rest_details(header, policy=policy, now=now) is False