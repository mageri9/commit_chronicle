"""Orchestration Pipeline - Coordinates collectors"""

from typing import Any

from github import Github
from github.Repository import Repository

from github_profiler.collectors.commits import CommitCollector
from github_profiler.collectors.rate_limit import RateLimitManager
from github_profiler.collectors.tree import TreeCollector
from github_profiler.storage.cache import LocalCache


class CollectionPipeline:
    """Orchestrates the entire data collection process"""

    def __init__(self, github_client: Github, cache_ttl_seconds: int = 3600) -> None:
        self.client = github_client
        self.rate_limiter = RateLimitManager(github_client)
        self.tree_collector = TreeCollector(github_client)
        self.commit_collector = CommitCollector(github_client)
        self.cache = LocalCache()
        self.cache_ttl = cache_ttl_seconds

    def collect_repo(self, repo: Repository, since_date: str) -> dict[str, Any]:
        """Collect all data for a single repository"""

        # Check cache
        cache_key = self.cache.repo_key(repo.full_name, "tree")
        cached_tree = self.cache.get(cache_key, ttl_seconds=self.cache_ttl)

        if cached_tree:
            tree_data = cached_tree
        else:
            tree_data = self.rate_limiter.execute_with_retry(
                lambda: self.tree_collector.collect(repo)
            )
            self.cache.set(cache_key, tree_data)

        # Commit data (shorter TTL or no cache)
        commit_data = self.rate_limiter.execute_with_retry(
            lambda: self.commit_collector.collect(repo, since_date)
        )

        return {"repo": repo.full_name, "tree": tree_data, "commits": commit_data}

    def collect_user(self, username: str, since_date: str) -> list[dict[str, Any]]:
        """Collect all data for a user's repositories"""
        user = self.client.get_user(username)
        repos = [r for r in user.get_repos() if not r.fork]

        result = []
        for repo in repos:
            print(f"  📦 {repo.full_name}...", end=" ", flush=True)
            try:
                data = self.collect_repo(repo, since_date)
                result.append(data)
                print(
                    f"✅ {len(data['tree'].get('files', []))} files, "
                    f"{data['commits'].get('total_commits', 0)} commits"
                )
            except Exception as e:
                print(f"❌ {e}")

        return result
