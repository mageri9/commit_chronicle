"""Normalizer — pure transformation: Signal → Evidence

No aggregation, no filtering, no scoring.
Deterministic mapping only.
"""

from datetime import datetime
from typing import Any

from github_profiler.domain import Evidence, SignalType, Strength


class Normalizer:
    """Transforms raw signals into normalized Evidence"""

    # Signal type → detector name mapping
    DETECTOR_MAP: dict[SignalType, str] = {
        SignalType.DEPENDENCY: "dependency_detector",
        SignalType.IMPORT: "import_detector",
        SignalType.PATH: "path_detector",
        SignalType.KEYWORD: "keyword_detector",
    }

    # Signal type → strength mapping
    STRENGTH_MAP: dict[SignalType, Strength] = {
        SignalType.DEPENDENCY: Strength.STRONG,
        SignalType.IMPORT: Strength.MEDIUM,
        SignalType.PATH: Strength.MEDIUM,
        SignalType.KEYWORD: Strength.WEAK,
    }

    def normalize(self, signals: list[dict[str, Any]]) -> list[Evidence]:
        """Convert raw signals to Evidence objects"""
        evidences = []
        detected_at = datetime.now()

        for signal in signals:
            signal_type = signal.get("signal_type")
            if not signal_type:
                continue

            strength = self.STRENGTH_MAP[signal_type]
            detector = self.DETECTOR_MAP[signal_type]

            evidence = Evidence(
                technology_id=signal.get("technology_id", ""),
                signal_type=signal_type,
                strength=strength,
                detector=detector,
                repo=signal.get("repo", "unknown"),
                file_path=signal.get("file_path", ""),
                value=signal.get("value", ""),
                detected_at=detected_at,
            )
            evidences.append(evidence)

        return evidences

    def normalize_signal(self, signal: dict[str, Any], repo: str, file_path: str) -> Evidence:
        """Normalize a single signal with context"""
        signal_type = signal.get("signal_type", "")

        return Evidence(
            technology_id=signal.get("technology_id", ""),
            signal_type=signal_type,
            strength=self.STRENGTH_MAP.get(signal_type, Strength.WEAK),
            detector=self.DETECTOR_MAP.get(signal_type, "unknown"),
            repo=repo,
            file_path=file_path,
            value=signal.get("value", ""),
            detected_at=datetime.now(),
        )
