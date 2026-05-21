"""Commit Collector - Fetches commits and patches"""

from datetime import datetime
from typing import Any

from github import Github, Repository
from github.Commit import Commit


class CommitCollector:
    """Collects commits and extracts added/removed lines"""

    def __init__(self, github_client: Github) -> None:
        self.client = github_client

    def collect(self, repo: Repository.Repository, since_date: str) -> dict[str, Any]:
        """Collect commits since date"""
        commits_data = []

        try:
            since = datetime.strptime(since_date, "%Y-%m-%d")
            commits = repo.get_commits(since=since)

            for commit in commits:
                # Skip merge commits
                if len(commit.parents) > 1:
                    continue

                commit_data = self._extract_commit_data(commit)
                commits_data.append(commit_data)

        except Exception as e:
            return {repo: repo.full_name, "error": str(e), "commits": []}

        return {
            "repo": repo.full_name,
            "commits": commits_data,
            "total_commits": len(commits_data),
            "collected_at": datetime.now().isoformat(),
        }

    def _extract_commit_data(self, commit: Commit) -> dict[str, Any]:
        """Extract data from single commit"""
        # Classify commit type by conventional commit prefix
        msg = commit.commit.message.lower()
        commit_type = "other"

        for prefix in ["feat", "fix", "refactor", "test", "docs", "chore", "style", "perf"]:
            if msg.startswith(prefix):
                commit_type = prefix
                break

        # Extract file changes
        files_changed = []
        added_lines_total = 0
        removed_lines_total = 0

        for file in commit.files or []:
            files_changed.append(
                {
                    "filename": file.filename,
                    "status": file.status,
                    "additions": file.additions,
                    "deletions": file.deletions,
                    "patch": file.patch if file.patch else "",
                }
            )
            added_lines_total += file.additions
            removed_lines_total += file.deletions

        return {
            "sha": commit.sha[:7],
            "author": commit.author.login if commit.author else None,
            "date": commit.commit.author.date.isoformat(),
            "message": commit.commit.message,
            "type": commit_type,
            "files": files_changed,
            "additions": added_lines_total,
            "deletions": removed_lines_total,
        }
