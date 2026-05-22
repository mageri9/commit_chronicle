"""Technology Schema — minimal, no aliases/popularity/embeddings"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Technology:
    """Technology entity — source of truth for detection"""

    id: str  # fastapi, django, react, etc.
    name: str  # FastAPI, Django, React
    category: str  # backend, frontend, database, devops, cloud
    ecosystem: str  # python-backend, javascript-frontend, etc.
    tags: list[str]  # optional: ["async", "rest", "orm"]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "ecosystem": self.ecosystem,
            "tags": self.tags,
        }
