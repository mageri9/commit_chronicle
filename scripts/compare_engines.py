"""
Сравнение старого collector'а (src/core/collector.py) и нового
GitHub Engine (src/core/collector_v2.py) на одинаковых входных данных.

Квест 4.2 — параллельный прогон перед переключением production-пайплайна
(worker/tasks.py) на collect_commits_v2 (Квест 4.3). Не часть продакшн-кода,
не вызывается из worker/bot — запускается вручную при подготовке к
переключению и после значимых изменений в src/github/.

Использование (из корня репозитория):
    python scripts/compare_engines.py torvalds 2024-01-01
    python scripts/compare_engines.py torvalds 2024-01-01 --skip-old
    python scripts/compare_engines.py torvalds 2024-01-01 --json report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Скрипт лежит в scripts/, а не в корне — при прямом запуске
# `python scripts/compare_engines.py` Python кладёт в sys.path[0]
# папку самого скрипта, а не корень репозитория, и `import src`
# падает с ModuleNotFoundError. Добавляем корень репозитория явно,
# чтобы скрипт работал и как файл, и как модуль (-m scripts.compare_engines).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.collector import collect_commits
from src.core.collector_v2 import collect_commits_v2
from src.logger import get_logger
from src.models.models import AnalysisResult, Commit
from src.github.client import close_github_client

logger = get_logger(__name__)

SEP = "=" * 62


# ---------------------------------------------------------------------------
# Индексация для сравнения
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    label: str
    result: AnalysisResult | None
    elapsed: float
    error: str | None = None


def _commit_key(c: Commit) -> tuple[str, str]:
    """
    (repo, hash) как ключ сравнения — не позиция в списке. Порядок между
    ThreadPoolExecutor (старый) и asyncio.gather (новый) не гарантированно
    совпадает, даже если набор репозиториев и коммитов идентичен.
    """
    return (c.repo, c.hash)


def _files_signature(c: Commit) -> frozenset[tuple[str, int, int]]:
    """Набор (filename, additions, deletions) для коммита — сравнение состава файлов."""
    return frozenset((f.filename, f.additions, f.deletions) for f in c.files)


@dataclass
class Diff:
    only_in_old: set[tuple[str, str]] = field(default_factory=set)
    only_in_new: set[tuple[str, str]] = field(default_factory=set)
    files_mismatch: list[tuple[str, str]] = field(default_factory=list)


def compare(old: AnalysisResult, new: AnalysisResult) -> Diff:
    old_by_key = {_commit_key(c): c for c in old.commits}
    new_by_key = {_commit_key(c): c for c in new.commits}

    diff = Diff(
        only_in_old=set(old_by_key) - set(new_by_key),
        only_in_new=set(new_by_key) - set(old_by_key),
    )

    for key in set(old_by_key) & set(new_by_key):
        if _files_signature(old_by_key[key]) != _files_signature(new_by_key[key]):
            diff.files_mismatch.append(key)

    return diff


def print_files_mismatch_detail(
    old: AnalysisResult, new: AnalysisResult, diff: Diff
) -> None:
    """
    Точный diff по файлам для коммитов из diff.files_mismatch — какие именно
    файлы/суммы разошлись между old и new. Печатается отдельно от основного
    отчёта, т.к. полезно только при расследовании конкретного расхождения,
    не при каждом штатном прогоне.
    """
    if not diff.files_mismatch:
        return

    old_by_key = {_commit_key(c): c for c in old.commits}
    new_by_key = {_commit_key(c): c for c in new.commits}

    print(f"\n{SEP}")
    print("  Детальный diff по файлам (files_mismatch)")
    print(SEP)

    for repo, sha in diff.files_mismatch:
        old_c = old_by_key[(repo, sha)]
        new_c = new_by_key[(repo, sha)]

        old_files = {(f.filename, f.additions, f.deletions) for f in old_c.files}
        new_files = {(f.filename, f.additions, f.deletions) for f in new_c.files}

        print(f"\n  {repo}@{sha}")
        print(f"    message: {old_c.message[:72]!r}")

        only_old = old_files - new_files
        only_new = new_files - old_files

        if only_old:
            print("    только в old:")
            for filename, add, dele in sorted(only_old):
                print(f"      + {filename}  (+{add}/-{dele})")
        if only_new:
            print("    только в new:")
            for filename, add, dele in sorted(only_new):
                print(f"      + {filename}  (+{add}/-{dele})")
        if not only_old and not only_new:
            print("    (одинаковый набор файлов — расхождение в другом поле?)")

    print(SEP)


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------


async def _run_old(username: str, since_date: str) -> RunResult:
    loop = asyncio.get_running_loop()
    start = time.monotonic()
    try:
        result = await loop.run_in_executor(None, collect_commits, username, since_date)
        return RunResult("old (PyGithub)", result, time.monotonic() - start)
    except Exception as e:
        logger.exception("old collector упал")
        return RunResult("old (PyGithub)", None, time.monotonic() - start, error=str(e))


async def _run_new(username: str, since_date: str) -> RunResult:
    start = time.monotonic()
    try:
        result = await collect_commits_v2(username, since_date)
        return RunResult("new (GitHub Engine)", result, time.monotonic() - start)
    except Exception as e:
        logger.exception("new collector упал")
        return RunResult(
            "new (GitHub Engine)", None, time.monotonic() - start, error=str(e)
        )


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------


def print_report(old_run: RunResult, new_run: RunResult, diff: Diff | None) -> None:
    print(f"\n{SEP}")
    print("  Compare Engines — collect_commits vs collect_commits_v2")
    print(SEP)

    for run in (old_run, new_run):
        if run.error:
            print(f"  {run.label:<22} ❌ ОШИБКА: {run.error}  ({run.elapsed:.1f}s)")
        else:
            count = len(run.result.commits) if run.result else 0
            repos = len({c.repo for c in run.result.commits}) if run.result else 0
            print(
                f"  {run.label:<22} ✅ {count} коммитов, {repos} репо  "
                f"({run.elapsed:.1f}s)"
            )

    print(SEP)

    if diff is None:
        print("  Сравнение пропущено (один из прогонов недоступен)")
        print(SEP)
        return

    if not diff.only_in_old and not diff.only_in_new and not diff.files_mismatch:
        print("  🎉 Результаты идентичны")
    else:
        if diff.only_in_old:
            print(f"  ⚠️  Только в old: {len(diff.only_in_old)} коммитов")
            for repo, sha in sorted(diff.only_in_old)[:10]:
                print(f"      - {repo}@{sha}")
            if len(diff.only_in_old) > 10:
                print(f"      ... и ещё {len(diff.only_in_old) - 10}")

        if diff.only_in_new:
            print(f"  ⚠️  Только в new: {len(diff.only_in_new)} коммитов")
            for repo, sha in sorted(diff.only_in_new)[:10]:
                print(f"      - {repo}@{sha}")
            if len(diff.only_in_new) > 10:
                print(f"      ... и ещё {len(diff.only_in_new) - 10}")

        if diff.files_mismatch:
            print(f"  ⚠️  Расхождение по файлам: {len(diff.files_mismatch)} коммитов")
            for repo, sha in sorted(diff.files_mismatch)[:10]:
                print(f"      - {repo}@{sha}")
            if len(diff.files_mismatch) > 10:
                print(f"      ... и ещё {len(diff.files_mismatch) - 10}")

    print(SEP)


def _dump_json(
    path: str, old_run: RunResult, new_run: RunResult, diff: Diff | None
) -> None:
    payload = {
        "old": {
            "elapsed": old_run.elapsed,
            "error": old_run.error,
            "commits": len(old_run.result.commits) if old_run.result else None,
        },
        "new": {
            "elapsed": new_run.elapsed,
            "error": new_run.error,
            "commits": len(new_run.result.commits) if new_run.result else None,
        },
        "diff": None
        if diff is None
        else {
            "only_in_old": sorted(f"{r}@{s}" for r, s in diff.only_in_old),
            "only_in_new": sorted(f"{r}@{s}" for r, s in diff.only_in_new),
            "files_mismatch": sorted(f"{r}@{s}" for r, s in diff.files_mismatch),
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  📄 Отчёт сохранён: {path}")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> None:
    old_run: RunResult | None = None
    new_run: RunResult | None = None

    if not args.skip_old and not args.skip_new and args.parallel:
        old_run, new_run = await asyncio.gather(
            _run_old(args.username, args.since),
            _run_new(args.username, args.since),
        )
    else:
        if not args.skip_old:
            old_run = await _run_old(args.username, args.since)
        if not args.skip_new:
            new_run = await _run_new(args.username, args.since)

    diff = None
    if old_run and new_run and old_run.result and new_run.result:
        diff = compare(old_run.result, new_run.result)

    placeholder = RunResult("(пропущено)", None, 0.0, error="не запускался")
    print_report(old_run or placeholder, new_run or placeholder, diff)

    if diff and old_run and new_run and old_run.result and new_run.result:
        print_files_mismatch_detail(old_run.result, new_run.result, diff)

    if args.json and old_run and new_run:
        _dump_json(args.json, old_run, new_run, diff)

    await close_github_client()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="GitHub username")
    parser.add_argument("since", help="Период с YYYY-MM-DD")
    parser.add_argument(
        "--skip-old", action="store_true", help="Не гонять старый collector"
    )
    parser.add_argument(
        "--skip-new", action="store_true", help="Не гонять collect_commits_v2"
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Гонять old и new одновременно (быстрее, но удваивает нагрузку "
        "на GitHub-токены и рискует secondary rate limit — не отражает "
        "прод, где движки никогда не работают параллельно)",
    )
    parser.add_argument("--json", help="Сохранить отчёт в JSON-файл")
    args = parser.parse_args()

    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()