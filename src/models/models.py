"""
Единые Pydantic-модели на весь проект.
Используются collector'ом, кешем, LLM — без лишних слоёв.
"""

from datetime import datetime
from pydantic import BaseModel, Field


class FileChange(BaseModel):
    """Один изменённый файл в коммите"""

    filename: str
    additions: int = 0
    deletions: int = 0


class Commit(BaseModel):
    """Один коммит со всеми данными"""

    hash: str
    date: datetime
    message: str
    repo: str
    files: list[FileChange] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """Полный результат анализа — то что collector отдаёт наружу"""

    username: str
    period: str
    commits: list[Commit]
    generated_at: datetime = datetime.now()


class CompactFile(BaseModel):
    model_config = {"populate_by_name": True}

    p: str = Field(alias="path")
    plus: int | None = Field(default=None, alias="+")
    minus: int | None = Field(default=None, alias="-")

    @property
    def add(self) -> int:
        return self.plus or 0

    @property
    def delete(self) -> int:
        return self.minus or 0


class CompactCommit(BaseModel):
    dt: str
    msg: str
    f: list[CompactFile] = []


class CompactResult(BaseModel):
    user: str
    period: str
    repos: dict[str, list[CompactCommit]]


def to_compact(result: AnalysisResult) -> CompactResult:
    repos: dict[str, list[CompactCommit]] = {}

    for commit in result.commits:
        repo_name = commit.repo.split("/")[-1]

        if repo_name not in repos:
            repos[repo_name] = []

        files = []
        for f in commit.files:
            file_data = CompactFile(path=f.filename)
            if f.additions:
                file_data.plus = f.additions
            if f.deletions:
                file_data.minus = f.deletions
            files.append(file_data)

        repos[repo_name].append(
            CompactCommit(
                dt=commit.date.strftime("%Y-%m-%d"),
                msg=commit.message.split("\n")[0][:200],
                f=files,
            )
        )

    return CompactResult(
        user=result.username,
        period=result.period,
        repos=repos,
    )


def serialize_result(result: AnalysisResult) -> str:
    """Конвертировать AnalysisResult в компактный JSON."""
    compact = to_compact(result)
    return compact.model_dump_json(by_alias=True, exclude_none=True)