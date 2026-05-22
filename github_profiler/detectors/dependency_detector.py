"""Dependency Detector — STRONGEST signal layer

Parses dependency files:
- requirements.txt (Python)
- package.json (Node.js)
- pyproject.toml (Python)
- go.mod (Go)
- Cargo.toml (Rust)
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class DependencyDetector:
    """Detects technologies from dependency files"""

    def __init__(self, rules: dict[str, Any]) -> None:
        self.rules = rules

    def detect(self, file_path: str, content: str) -> list[dict[str, Any]]:
        """Detect technologies in dependency file"""
        detected = []

        if file_path.endswith("requirements.txt"):
            detected = self._parse_requirements_txt(content)
        elif file_path.endswith("package.json"):
            detected = self._parse_package_json(content)
        elif file_path.endswith("pyproject.toml"):
            detected = self._parse_pyproject_toml(content)
        elif file_path.endswith("go.mod"):
            detected = self._parse_go_mod(content)
        elif file_path.endswith("Cargo.toml"):
            detected = self._parse_cargo_toml(content)

        return detected

    def _parse_requirements_txt(self, content: str) -> list[dict[str, Any]]:
        """Parse requirements.txt format"""
        detected = []

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Extract package name (before ==, >=, <=, etc.)
            pkg_name = re.split(r"[=<>!]", line)[0].strip().lower()

            # Check against rules
            for tech_id, tech in self.rules.items():
                deps = tech.get("dependencies", [])
                if pkg_name in deps:
                    detected.append(
                        {
                            "technology_id": tech_id,
                            "signal_type": "dependency",
                            "value": line,
                            "dependency_name": pkg_name,
                        }
                    )

        return detected

    def _parse_package_json(self, content: str) -> list[dict[str, Any]]:
        """Parse package.json dependencies"""
        detected = []

        try:
            import json

            data = json.loads(content)

            # Check dependencies and devDependencies
            deps = {}
            deps.update(data.get("dependencies", {}))
            deps.update(data.get("devDependencies", {}))

            for pkg_name in deps:
                pgk_lower = pkg_name.lower()

                for tech_id, tech in self.rules.items():
                    rule_deps = [d.lower() for d in tech.get("dependencies", [])]
                    if pgk_lower in rule_deps:
                        detected.append(
                            {
                                "technology_id": tech_id,
                                "signal_type": "dependency",
                                "value": f"{pkg_name}: {deps[pkg_name]}",
                                "dependency_name": pkg_name,
                            }
                        )

        except Exception:
            logger.exception("Failed to parse package.json")

        return detected

    def _parse_pyproject_toml(self, content: str) -> list[dict[str, Any]]:
        """Parse pyproject.toml dependencies"""
        detected = []

        # Simple regex-based parsing (no TOML parser)
        in_deps = False
        for line in content.split("\n"):
            if "dependencies = [" in line or "dependencies = [" in line:
                in_deps = True
            elif in_deps and "]" in line:
                break
            elif in_deps and '"' in line:
                # Extract quoted dependency
                match = re.search(r'"([^"]+)"', line)
                if match:
                    pkg_name = match.group(1).split(">=")[0].split("==")[0].strip()

                    for tech_id, tech in self.rules.items():
                        if pkg_name in tech.get("dependencies", []):
                            detected.append(
                                {
                                    "technology_id": tech_id,
                                    "signal_type": "dependency",
                                    "value": pkg_name,
                                    "dependency_name": pkg_name,
                                }
                            )
        return detected

    def _parse_go_mod(self, content: str) -> list[dict[str, Any]]:
        """Parse go.mod dependencies"""
        detected = []

        for line in content.split("\n"):
            if line.startswith("require"):
                # Extract module name
                parts = line.split()
                if len(parts) >= 2:
                    module = parts[1].split("/")[0]  # First part of module path

                    for tech_id, tech in self.rules.items():
                        if module in tech.get("dependencies", []):
                            detected.append(
                                {
                                    "technology_id": tech_id,
                                    "signal_type": "dependency",
                                    "value": line.strip(),
                                    "dependency_name": module,
                                }
                            )

        return detected

    def _parse_cargo_toml(self, content: str) -> list[dict[str, Any]]:
        """Parse Cargo.toml dependencies"""
        detected = []

        in_deps = False
        for line in content.split("\n"):
            if line.strip() == "[dependencies]":
                in_deps = True
            elif in_deps and line.strip().startswith("["):
                break
            elif in_deps and "=" in line:
                pkg_name = line.split("=")[0].strip()

                for tech_id, tech in self.rules.items():
                    if pkg_name in tech.get("dependencies", []):
                        detected.append(
                            {
                                "technology_id": tech_id,
                                "signal_type": "dependency",
                                "value": line.strip(),
                                "dependency_name": pkg_name,
                            }
                        )

        return detected
