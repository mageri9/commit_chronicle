"""
Замер токенов CompactResult при разных параметрах сжатия.

Цель: помочь подобрать параметры (msg_limit, включать ли files)
чтобы укладываться в контекстное окно LLM.

Использование (из корня репозитория):
    python scripts/benchmark_json.py                  # последняя запись из БД
    python scripts/benchmark_json.py --id <job_id>    # конкретная запись
    python scripts/benchmark_json.py --limit 8000     # показать что влезает в 8k токенов
"""

import argparse
import asyncio
import json
from dataclasses import dataclass

import tiktoken

from src.models.models import (
    CompactResult,
    deserialize_result,
)
from src.storage.database import engine, requests

# Ключ "from" зарезервирован в Python, в модели называется from_.
# Хардкодим здесь — бенчмарк локальная утилита, не продакшн-код.
_FROM_KEY = "from"


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------

# cl100k_base — энкодинг GPT-4 / GPT-4o / claude-* (приближение)
_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


# ---------------------------------------------------------------------------
# Варианты сжатия
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    label: str
    msg_limit: int  # макс. символов в subject коммита
    include_files: bool  # включать ли список файлов
    files_limit: int  # макс. файлов на коммит (0 = все)


VARIANTS: list[Variant] = [
    Variant(
        "full (72 chars, all files)", msg_limit=72, include_files=True, files_limit=0
    ),
    Variant("msg only (72 chars)", msg_limit=72, include_files=False, files_limit=0),
    Variant("msg only (50 chars)", msg_limit=50, include_files=False, files_limit=0),
    Variant("top-5 files", msg_limit=72, include_files=True, files_limit=5),
    Variant("top-3 files", msg_limit=72, include_files=True, files_limit=3),
]


def apply_variant(compact: CompactResult, v: Variant) -> str:
    """Сериализовать CompactResult с параметрами варианта."""
    repos_out = {}
    for repo, commits in compact.repos.items():
        out_commits = []
        for c in commits:
            files = c.f if v.include_files else []
            if v.files_limit and files:
                files = files[: v.files_limit]
            row: dict = {
                "d": c.d,
                "m": c.m[: v.msg_limit],
            }
            if files:
                row["f"] = files
            out_commits.append(row)
        repos_out[repo] = out_commits

    payload = {
        "user": compact.user,
        _FROM_KEY: compact.from_,
        "ext": compact.ext,
        "repos": repos_out,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Статистика по данным
# ---------------------------------------------------------------------------


def data_stats(compact: CompactResult) -> dict:
    """Базовая статистика по содержимому CompactResult."""
    total_commits = sum(len(v) for v in compact.repos.values())
    total_files = sum(len(c.f) for commits in compact.repos.values() for c in commits)
    return {
        "repos": len(compact.repos),
        "commits": total_commits,
        "files_total": total_files,
        "files_per_commit": round(total_files / total_commits, 1)
        if total_commits
        else 0,
        "ext_count": len(compact.ext),
    }


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

SEP = "=" * 62


def print_report(compact: CompactResult, token_limit: int | None) -> None:
    stats = data_stats(compact)

    print(f"\n{SEP}")
    print(f"  CompactResult — замер токенов")
    print(f"  user={compact.user!r}  from={compact.from_!r}")
    print(SEP)
    print(
        f"  Репозиториев: {stats['repos']}  "
        f"Коммитов: {stats['commits']}  "
        f"Файлов: {stats['files_total']} "
        f"({stats['files_per_commit']} / коммит)"
    )
    print(f"  Расширений в индексе: {stats['ext_count']}")
    print(SEP)
    print(f"  {'Вариант':<35} {'Байт':>7} {'Токенов':>8}", end="")
    if token_limit:
        print(f"  {'Влезает?':>8}", end="")
    print()
    print(f"  {'-' * 58}")

    results = []
    for v in VARIANTS:
        serialized = apply_variant(compact, v)
        tokens = count_tokens(serialized)
        size = len(serialized)
        fits = tokens <= token_limit if token_limit else None
        results.append((v.label, size, tokens, fits))

    # базовый вариант (первый) для расчёта экономии
    base_tokens = results[0][2]

    for label, size, tokens, fits in results:
        saving = (
            f"-{100 - tokens / base_tokens * 100:.0f}%"
            if tokens != base_tokens
            else "base"
        )
        fits_mark = ""
        if fits is not None:
            fits_mark = f"  {'✅' if fits else '❌'} {tokens}/{token_limit}"
        print(f"  {label:<35} {size:>7}  {tokens:>6}  {saving:>6}{fits_mark}")

    print(SEP)

    if token_limit:
        # Найти лучший вариант который влезает
        fitting = [(l, t) for l, _, t, f in results if f]
        if fitting:
            best = min(fitting, key=lambda x: x[1])
            print(f"  💡 Лучший вариант в {token_limit} токенов: {best[0]!r}")
        else:
            print(f"  ⚠️  Ни один вариант не влезает в {token_limit} токенов")
    print()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def _load_from_db(job_id: str | None) -> str | None:
    async with engine.connect() as conn:
        query = requests.select().where(requests.c.status == "done")
        if job_id:
            query = query.where(requests.c.id == job_id)
        else:
            query = query.order_by(requests.c.created_at.desc()).limit(1)

        result = await conn.execute(query)
        row = result.first()

    if not row:
        return None
    return dict(row._mapping)["result_json"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", dest="job_id", help="job_id записи в БД")
    parser.add_argument(
        "--limit",
        dest="token_limit",
        type=int,
        default=None,
        help="Целевой лимит токенов (например 8000 для gpt-4o-mini)",
    )
    args = parser.parse_args()

    raw_json = asyncio.run(_load_from_db(args.job_id))
    if not raw_json:
        print("❌ Нет завершённых запросов в БД")
        return

    try:
        compact = deserialize_result(raw_json)
    except Exception as e:
        print(f"❌ Не удалось десериализовать result_json: {e}")
        return

    print_report(compact, token_limit=args.token_limit)


if __name__ == "__main__":
    main()