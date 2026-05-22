"""Signal Model — raw findings from collectors

Signal ≠ Evidence
Signal = raw detection before normalization
"""

from dataclasses import dataclass
from enum import Enum


class SignalType(Enum):
    """Types of raw signals from collectors"""

    IMPORT = "import"
    DEPENDENCY = "dependency"
    PATH = "path"
    KEYWORD = "keyword"


@dataclass(frozen=True)
class Signal:
    """Raw finding from file analysis"""

    type: SignalType
    technology_id: str
    value: str  ## actual match: "fastapi", "from fastapi import...", etc.
    repo: str
    file_path: str
    line: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "technology_id": self.technology_id,
            "value": self.value,
            "repo": self.repo,
            "file_path": self.file_path,
            "line": self.line,
        }
