"""Detection Engine — Orchestrates all detectors

Collects signals from:
- Path detector (medium)
- Import detector (medium)
- Dependency detector (strong)
- Keyword detector (weak)

Returns raw signals for normalization.
"""

from pathlib import Path
from typing import Any

from .dependency_detector import DependencyDetector
from .import_detector import ImportDetector
from .keyword_detector import KeywordDetector
from .path_detector import PathDetector
from .rules_loader import RulesLoader


class DetectionEngine:
    """Orchestrates all detectors for a file"""

    def __init__(self, rules_path: Path | None = None):
        if rules_path is None:
            rules_path = Path(__file__).parent.parent / "rules" / "technologies.json"

        self.rules_loader = RulesLoader(rules_path)
        self.rules = self.rules_loader.load()

        self.path_detector = PathDetector(self.rules)
        self.import_detector = ImportDetector(self.rules)
        self.dependency_detector = DependencyDetector(self.rules)
        self.keyword_detector = KeywordDetector(self.rules)

    def detect_file(self, file_path: str, content: str) -> list[dict[str, Any]]:
        """Detect all technologies in a single file"""
        signals = []

        # 1. Path detector (check file name only, no content needed)
        signals.extend(self.path_detector.detect(file_path))

        # 2. Import detector (needs content)
        signals.extend(self.import_detector.detect(file_path, content))

        # 3. Dependency detector (needs content, specific files)
        signals.extend(self.dependency_detector.detect(file_path, content))

        # 4. Keyword detector (weak, always last)
        # Only if no stronger signals found? Or always? Let's always run but mark as weak.
        signals.extend(self.keyword_detector.detect(content))

        return signals

    def detect_repo(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Detect technologies across all files in a repo"""
        all_signals = []

        for file_info in files:
            file_path = file_info.get("path", "")
            content = file_info.get("content", "")

            if not content:
                continue

            signals = self.detect_file(file_path, content)

            for signal in signals:
                all_signals.append(
                    {
                        **signal,
                        "repo": file_info.get("repo", "unknown"),
                        "file_path": file_path,
                    }
                )

        return all_signals
