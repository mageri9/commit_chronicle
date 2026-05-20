import os
import csv
import re
import json
import fnmatch
import sys
from datetime import datetime, date
from collections import defaultdict

from github import Github, Auth
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
USERNAME = os.getenv("GITHUB_USERNAME")
SINCE_DATE = os.getenv("SINCE_DATE", "2023-01-01")

if not TOKEN:
    raise ValueError("GITHUB_TOKEN not found in .env")

COMMIT_TYPE_PATTERNS = [
    (r"^feat[(:]", "feature"),
    (r"^fix[(:]", "bugfix"),
    (r"^refactor[(:]", "refactor"),
    (r"^test[(:]", "testing"),
    (r"^docs[(:]", "docs"),
    (r"^chore[(:]", "maintenance"),
    (r"^style[(:]", "style"),
    (r"^perf[(:]", "performance"),
]

IGNORE_GLOBS = [
    "README*", "*.md", ".gitignore", ".editorconfig",
    "LICENSE", "CHANGELOG*",
    "package-lock.json", "yarn.lock", "poetry.lock",
    "Pipfile.lock", "*.min.js", "*.min.css",
    "*.svg", "*.png", "*.jpg", "*.ico", "*.gif",
]


# ═══════════════════════════════════════════════
# KNOWLEDGE BASE
# ═══════════════════════════════════════════════

def load_kb():
    kb_path = os.path.join(os.path.dirname(__file__), "technologies.json")
    with open(kb_path, "r", encoding="utf-8") as f:
        return json.load(f)


KB = load_kb()


# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════

def match_glob(filepath, pattern):
    if "**" in pattern:
        prefix, suffix = pattern.split("**", 1)
        return filepath.startswith(prefix.rstrip("/")) and filepath.endswith(suffix.lstrip("/"))
    return fnmatch.fnmatch(filepath, pattern)


def classify_commit(message):
    for pattern, ctype in COMMIT_TYPE_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return ctype
    return "other"


def extract_skills_from_file(file_info):
    path = file_info.filename
    patch = file_info.patch or ""

    for glob in IGNORE_GLOBS:
        if match_glob(path, glob):
            return []

    results = []
    technologies = KB.get("technologies", {})

    for tech_name, tech in technologies.items():
        detection = tech.get("detection", {})
        confidence = 0.0

        for pattern in detection.get("paths", []):
            if match_glob(path, pattern):
                confidence += 0.3
                break

        for pattern in detection.get("imports", []):
            if f"import {pattern}" in patch or f"from {pattern}" in patch:
                confidence += 0.4
                break

        for pattern in detection.get("patterns", []):
            if re.search(pattern, patch, re.IGNORECASE | re.MULTILINE):
                confidence += 0.3
                break

        for dep in detection.get("dependencies", []):
            if dep in patch:
                confidence += 0.2
                break

        if confidence >= 0.3:
            results.append((
                tech_name,
                min(confidence, 1.0),
                tech.get("category", "unknown"),
            ))

    return results


def is_merge_commit(commit):
    return len(commit.parents) > 1


# ═══════════════════════════════════════════════
# COLLECT
# ═══════════════════════════════════════════════

def collect_commits(g, username, repos, since_date):
    since = datetime.strptime(since_date, "%Y-%m-%d")
    results = []

    for repo_name in repos:
        repo_full = f"{username}/{repo_name}"
        print(f"  Обрабатываю {repo_full}...")

        try:
            repo = g.get_repo(repo_full)
            commits = repo.get_commits(author=username, since=since)

            for commit in commits:
                if is_merge_commit(commit):
                    continue

                try:
                    files = commit.files
                except Exception:
                    continue

                commit_type = classify_commit(commit.commit.message)
                commit_date = commit.commit.author.date.strftime("%Y-%m-%d")
                sha = commit.sha[:7]
                message = commit.commit.message.split("\n")[0][:120]

                for file in files:
                    if not file.filename:
                        continue

                    additions = file.additions or 0
                    deletions = file.deletions or 0
                    changes = additions + deletions

                    if changes > 10000:
                        continue

                    skills = extract_skills_from_file(file)

                    for skill, confidence, category in skills:
                        results.append({
                            "date": commit_date,
                            "repo": repo_name,
                            "sha": sha,
                            "commit_type": commit_type,
                            "file": file.filename,
                            "skill": skill,
                            "category": category,
                            "confidence": round(confidence, 2),
                            "additions": additions,
                            "deletions": deletions,
                            "changes": changes,
                            "message": message,
                        })

        except Exception as e:
            print(f"    Ошибка: {e}")
            continue

    return results


# ═══════════════════════════════════════════════
# PROFILE
# ═══════════════════════════════════════════════

def save_to_csv(results, filename="skills.csv"):
    if not results:
        print("Нет данных для сохранения")
        return

    fields = [
        "date", "repo", "sha", "commit_type",
        "file", "skill", "category",
        "confidence", "additions", "deletions", "changes", "message",
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    print(f"  Сохранено {len(results)} записей в {filename}")


def build_cooccurrence(results):
    commit_skills = defaultdict(set)
    for r in results:
        commit_skills[r["sha"]].add(r["skill"])

    cooc = defaultdict(lambda: defaultdict(int))
    for skills in commit_skills.values():
        for s1 in skills:
            for s2 in skills:
                if s1 < s2:
                    cooc[s1][s2] += 1
                    cooc[s2][s1] += 1
    return cooc


def build_profile(results):
    profile = defaultdict(lambda: {
        "commits": set(),
        "repos": set(),
        "files": set(),
        "lines": 0,
        "first_used": None,
        "last_used": None,
        "category": "",
    })

    for r in results:
        skill = r["skill"]
        p = profile[skill]

        p["commits"].add(r["sha"])
        p["repos"].add(r["repo"])
        p["files"].add(r["file"])
        p["lines"] += r["changes"]

        if p["first_used"] is None or r["date"] < p["first_used"]:
            p["first_used"] = r["date"]
        if p["last_used"] is None or r["date"] > p["last_used"]:
            p["last_used"] = r["date"]

        p["category"] = r["category"]

    return profile


def print_profile(results):
    if not results:
        return

    profile = build_profile(results)
    cooc = build_cooccurrence(results)

    print()
    print("=" * 70)
    print("ИНЖЕНЕРНЫЙ ПРОФИЛЬ")
    print("=" * 70)

    sorted_skills = sorted(
        profile.items(),
        key=lambda x: len(x[1]["commits"]),
        reverse=True
    )

    for skill, data in sorted_skills[:20]:
        commits_count = len(data["commits"])
        repos_count = len(data["repos"])
        files_count = len(data["files"])

        last_date = datetime.strptime(data["last_used"], "%Y-%m-%d").date()
        days_since = (date.today() - last_date).days
        if days_since <= 30:
            status = "🟢 active"
        elif days_since <= 180:
            status = "🟡 stale"
        else:
            status = "⚪ declining"

        print(f"\n{skill} ({data['category']}) {status}")
        print(f"  Коммитов: {commits_count}  |  Репо: {repos_count}  |  Файлов: {files_count}")
        print(f"  Строк изменено: {data['lines']}")
        print(f"  Период: {data['first_used']} → {data['last_used']}")

        if skill in cooc:
            top_cooc = sorted(cooc[skill].items(), key=lambda x: x[1], reverse=True)[:5]
            cooc_str = ", ".join(f"{s}({c})" for s, c in top_cooc if c > 1)
            if cooc_str:
                print(f"  Вместе с: {cooc_str}")

    # JSON
    json_profile = {}
    for skill, data in profile.items():
        json_profile[skill] = {
            "category": data["category"],
            "commits": len(data["commits"]),
            "repos": sorted(data["repos"]),
            "files": len(data["files"]),
            "lines": data["lines"],
            "first_used": data["first_used"],
            "last_used": data["last_used"],
            "status": "active" if (date.today() - datetime.strptime(data["last_used"], "%Y-%m-%d").date()).days <= 30 else (
                "stale" if (date.today() - datetime.strptime(data["last_used"], "%Y-%m-%d").date()).days <= 180 else "declining"
            ),
        }

    json_cooc = {}
    for s1 in cooc:
        filtered = {s2: c for s2, c in cooc[s1].items() if c >= 2}
        if filtered:
            json_cooc[s1] = dict(sorted(filtered.items(), key=lambda x: x[1], reverse=True))

    with open("profile.json", "w", encoding="utf-8") as f:
        json.dump({"skills": json_profile, "cooccurrence": json_cooc}, f, indent=2, ensure_ascii=False)

    print(f"\n📄 profile.json сохранён")


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) > 1:
        USERNAME = sys.argv[1]

    auth = Auth.Token(TOKEN)
    g = Github(auth=auth)
    user = g.get_user(USERNAME)
    repos = [repo.name for repo in user.get_repos()]

    print(f"Пользователь: {USERNAME}")
    print(f"Репозиториев: {len(repos)}")
    print(f"Первые 10: {repos[:10]}")
    print()

    os.makedirs("output", exist_ok=True)
    results = collect_commits(g, USERNAME, repos, SINCE_DATE)
    save_to_csv(results, "output/skills.csv")
    print_profile(results)