"""Rules Loader — load and validate detection rules from JSON"""

import json
from pathlib import Path
from typing import Any


class RulesLoader:
    """Loads technology detection rules from JSON"""

    def __init__(self, rules_path: Path | None = None):
        if rules_path is None:
            current_dir = Path(__file__).parent.parent
            rules_path = current_dir / "rules" / "technologies.json"
        self.rules_path = rules_path
        self._rules: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        """Load rules from JSON file"""
        if self._rules is None:
            if not self.rules_path.exists():
                raise FileNotFoundError(
                    f"Rules file not found: {self.rules_path}\n"
                    f"Expected at: {self.rules_path.absolute()}"
                )
            with open(self.rules_path, encoding="utf-8") as f:
                self._rules = json.load(f)
        return self._rules

    def get_technology(self, tech_id: str) -> dict[str, Any] | None:
        """Get rules for specific technology"""
        rules = self.load()
        return rules.get(tech_id)

    def get_all_technologies(self) -> list[str]:
        """Get all technology IDs"""
        rules = self.load()
        return list(rules.keys())
