"""
Единые Pydantic-модели на весь проект.
Используются collector'ом, кешем, LLM — без лишних слоёв.
"""

from datetime import datetime
from pydantic import BaseModel


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
    files: list[FileChange] = []


class AnalysisResult(BaseModel):
    """Полный результат анализа — то что collector отдаёт наружу"""

    username: str
    period: str
    commits: list[Commit]
    generated_at: datetime = datetime.now()