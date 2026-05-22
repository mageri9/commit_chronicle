"""Evidence Model — normalized signals with strength

Evidence = Signal + Strength
No float confidence scores, only literals
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from github_profiler.domain import SignalType


class Strength(Enum):
    """Evidence strength — deterministic categories only"""

    STRONG = "strong"  # dependency declaration
    MEDIUM = "medium"  # import statement
    WEAK = "weak"  # keyword match


@dataclass
class Evidence:
    """Normalized evidence for aggregation"""

    technology_id: str
    signal_type: SignalType
    strength: Strength
    repo: str
    file_path: str
    value: str
    collected_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "technology_id": self.technology_id,
            "signal_type": self.signal_type,
            "strength": self.strength.value,
            "repo": self.repo,
            "file_path": self.file_path,
            "value": self.value,
            "collected_at": self.collected_at.isoformat(),
        }
