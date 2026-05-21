"""Local Cache Layer - Persistent cache for GitHub data"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class LocalCache:
    """File-based cache for GitHub API responses"""

    def __init__(self, cache_dir: Path = Path("storage/cache")) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_key_hash(self, key: str) -> str:
        """Generate hash for cache key"""
        return hashlib.md5(key.encode()).hexdigest()[:12]

    def _get_cache_path(self, key: str) -> Path:
        """Get file path for cache entry"""
        return self.cache_dir / f"{self._get_key_hash(key)}.json"

    def get(self, key: str, ttl_seconds: int | None = None) -> Any | None:
        """Get cached value if not expired"""
        cache_path = self._get_cache_path(key)

        if not cache_path.exists():
            return None

        try:
            with open(cache_path) as f:
                data = json.load(f)

            # Check expiration
            if ttl_seconds:
                cached_at = datetime.fromisoformat(data["created_at"])
                if (datetime.now() - cached_at).total_seconds() > ttl_seconds:
                    cache_path.unlink()
                    return None

            return data["value"]
        except Exception:
            return None

    def set(self, key: str, value: Any) -> None:
        """Store value in cache"""
        cache_path = self._get_cache_path(key)

        data = {
            "key": key,
            "value": value,
            "cached_at": datetime.now().isoformat(),
        }

        with open(cache_path, "w") as f:
            json.dump(data, f, indent=2)

    def clear(self, pattern: str | None = None) -> None:
        """Clear cache entries"""
        if pattern is None:
            for path in self.cache_dir.glob("*.json"):
                path.unlink()
        else:
            # Simple pattern matching on keys (would require index)
            pass

    def repo_key(self, repo_full_name: str, data_type: str) -> str:
        """Generate cache key for repository data"""
        return f"{repo_full_name}:{data_type}"
