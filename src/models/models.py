"""
Единые Pydantic-модели на весь проект.
Используются collector'ом, кешем, LLM — без лишних слоёв.

Формат CompactResult оптимизирован для минимального числа токенов:

  {
    "user": "torvalds",
    "from": "2024-01-01",        # period_start — база для дат
    "ext":  [".c", ".h", ".py"], # индекс расширений (дедупликация путей)
    "repos": {
      "linux": [
        {
          "d": 74,               # день от period_start (не строка даты)
          "m": "fix: tcp leak",  # subject коммита, max 72 символа
          "f": [                 # файлы — только если есть изменения
            ["net/tcp", 0, 12, 3],   # [путь_без_ext, ext_idx, +, -]
            ["include/net", 1]        # +/- опускаются если 0
          ]
        }
      ]
    }
  }

Восстановление даты на стороне LLM:
    date = datetime.strptime(from, "%Y-%m-%d") + timedelta(days=d)
"""

import json
from datetime import datetime
from pathlib import PurePosixPath

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Исходные модели (collector → normalizer → AnalysisResult)
# ---------------------------------------------------------------------------


class FileChange(BaseModel):
    """Один изменённый файл в коммите."""

    filename: str
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class Commit(BaseModel):
    """Один коммит со всеми данными."""

    hash: str
    date: datetime
    message: str
    repo: str
    files: list[FileChange] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """Полный результат анализа — то что collector отдаёт наружу."""

    username: str
    period_start: str  # "YYYY-MM-DD" — база для дат в CompactResult
    commits: list[Commit]
    generated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Компактные модели (LLM / кэш)
# ---------------------------------------------------------------------------
# Файл сериализуется вручную (не через model_dump) — это позволяет
# использовать массивы вместо объектов и получить максимальную компрессию.
# Pydantic здесь нужен только для валидации при десериализации.
# ---------------------------------------------------------------------------

# Файл в компактном виде: [stem, ext_idx] или [stem, ext_idx, additions, deletions]
# ext_idx — индекс в списке CompactResult.ext
CompactFileRow = list  # [str, int] | [str, int, int, int]


class CompactCommit(BaseModel):
    """Один коммит в компактном представлении."""

    d: int  # день от period_start
    m: str  # subject коммита, max 72 символа
    f: list[CompactFileRow] = Field(default_factory=list)


class CompactResult(BaseModel):
    """
    Финальный JSON для LLM и кэша.

    Поля:
        user   — GitHub username
        from_  — period_start (в JSON: "from", ge=0 валидация дат)
        ext    — индекс расширений файлов, например [".py", ".md"]
        repos  — коммиты, сгруппированные по repo_name
    """

    user: str
    from_: str = Field(alias="from")  # "from" — зарезервированное слово в Python
    ext: list[str] = Field(default_factory=list)
    repos: dict[str, list[CompactCommit]]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Конвертация
# ---------------------------------------------------------------------------


def _build_ext_index(commits: list[Commit]) -> list[str]:
    """
    Собрать упорядоченный список уникальных расширений из всех файлов.
    Порядок: по убыванию частоты (самые частые — меньший индекс → меньше токенов).
    """
    freq: dict[str, int] = {}
    for commit in commits:
        for f in commit.files:
            if f.additions or f.deletions:  # пропускаем файлы без изменений
                ext = PurePosixPath(f.filename).suffix or ""
                freq[ext] = freq.get(ext, 0) + 1
    return sorted(freq, key=lambda e: -freq[e])


def _file_row(
    filename: str, additions: int, deletions: int, ext_idx: dict[str, int]
) -> CompactFileRow:
    """
    Преобразовать один файл в компактную строку-массив.

    Формат:
        [stem, ext_idx]              — если additions=0 и deletions=0 (не должно попасть сюда)
        [stem, ext_idx, +, -]        — полная строка
        [stem, ext_idx, +]           — если deletions=0
    """
    p = PurePosixPath(filename)
    stem = str(p.with_suffix(""))  # путь без расширения
    idx = ext_idx.get(p.suffix or "", -1)

    if deletions:
        return [stem, idx, additions, deletions]
    if additions:
        return [stem, idx, additions]
    return [stem, idx]  # fallback (фильтруется в to_compact)


def to_compact(result: AnalysisResult) -> CompactResult:
    """Конвертировать AnalysisResult → CompactResult с максимальной компрессией."""
    period_start = datetime.strptime(result.period_start, "%Y-%m-%d")

    # 1. Индекс расширений — строится один раз по всем коммитам
    ext_list = _build_ext_index(result.commits)
    ext_idx = {ext: i for i, ext in enumerate(ext_list)}

    repos: dict[str, list[CompactCommit]] = {}

    for commit in result.commits:
        # Полный путь репо как ключ — избегаем коллизий одинаковых имён
        repo_key = commit.repo
        if repo_key not in repos:
            repos[repo_key] = []

        # Только файлы с реальными изменениями
        files: list[CompactFileRow] = [
            _file_row(f.filename, f.additions, f.deletions, ext_idx)
            for f in commit.files
            if f.additions or f.deletions
        ]

        repos[repo_key].append(
            CompactCommit(
                d=(commit.date.replace(tzinfo=None) - period_start).days,
                m=commit.message.split("\n")[0][:72],
                f=files,
            )
        )

    return CompactResult(
        user=result.username,
        from_=result.period_start,
        ext=ext_list,
        repos=repos,
    )


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------


def _serialize_compact(result: CompactResult) -> dict:
    """
    Сериализовать CompactResult в dict вручную.

    Pydantic model_dump не используется — CompactFileRow это list,
    и нам нужен полный контроль над exclude_defaults для массивов.
    """
    return {
        "user": result.user,
        "from": result.from_,
        "ext": result.ext,
        "repos": {
            repo: [
                {
                    "d": c.d,
                    "m": c.m,
                    **({"f": c.f} if c.f else {}),  # пустой f — не пишем
                }
                for c in commits
            ]
            for repo, commits in result.repos.items()
        },
    }


def serialize_result(result: AnalysisResult) -> str:
    """Конвертировать AnalysisResult в компактный JSON без пробелов."""
    compact = to_compact(result)
    return json.dumps(
        _serialize_compact(compact), ensure_ascii=False, separators=(",", ":")
    )


# ---------------------------------------------------------------------------
# Восстановление (для LLM-промпта / дебага)
# ---------------------------------------------------------------------------


def deserialize_result(raw: str) -> CompactResult:
    """Восстановить CompactResult из JSON-строки."""
    data = json.loads(raw)
    return CompactResult.model_validate(data)


def explain_format() -> str:
    """
    Вернуть короткую подсказку для LLM о формате CompactResult.
    Вставляется в системный промпт один раз.
    """
    return (
        'Dates: `from` + `d` days offset. '
        'Files: `[path_no_ext, ext[idx], additions?, deletions?]`. '
        'Omitted additions/deletions means 0.'
    )