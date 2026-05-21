"""GitHub Tree Collector - Recursive tree fetching"""

from datetime import datetime
from typing import Any

from github import Github, Repository


class TreeCollector:
    """Collects repository file tree using recursive Git tree API"""

    def __init__(self, github_client: Github):
        self.client = github_client
        self.max_file_size = 200_000  # 200KB
        self.max_files_per_repo = 2000

    def collect(self, repo: Repository.Repository) -> dict[str, Any]:
        """Collect file tree for a repository"""
        try:
            # Get HEAD commit
            head_commit = repo.get_commits().reversed[0]

            # Get recursive tree (GitHub API limitation: no native recursive)
            # We'll fetch tree and recursively expand
            tree = repo.get_git_tree(sha=head_commit.sha, recursive=True)

            files = []
            for item in tree.tree:
                if item.type == "blob":  # file, not directory
                    if item.size <= self.max_file_size:
                        files.append(
                            {"path": item.path, "size": item.size, "sha": item.sha, "url": item.url}
                        )

                    if len(files) >= self.max_files_per_repo:
                        break

            return {
                "repo": repo.full_name,
                "branch": repo.default_branch,
                "commit_sha": head_commit.sha,
                "files": files,
                "total_files": len(files),
                "collected_at": datetime.now().isoformat(),
            }

        except Exception as e:
            return {"repo": repo.full_name, "error": str(e), "files": []}
