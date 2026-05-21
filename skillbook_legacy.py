"""
LEGACY - DO NOT MODIFY
Reference baseline for Phase 0
Frozen: 2026-05-21
"""

import base64
import json
import os
import re
from collections import defaultdict
from contextlib import suppress
from datetime import date, datetime

from dotenv import load_dotenv
from github import Auth, Github

# =========================================================
# CONFIG
# =========================================================

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
USERNAME = os.getenv("GITHUB_USERNAME")
SINCE_DATE = os.getenv("SINCE_DATE", "2023-01-01")

MAX_FILE_SIZE = 200_000
MAX_FILES_PER_REPO = 2000

OUTPUT_FILE = "tech_profile.json"

if not TOKEN:
    raise ValueError("GITHUB_TOKEN missing in .env")

# =========================================================
# TECHNOLOGY DETECTION RULES
# =========================================================

TECHNOLOGIES = {
    # --- Languages ---
    "python": {
        "name": "Python",
        "category": "language",
        "ecosystem": "python",
        "detection": {
            "paths": ["*.py"],
            "imports": [],
            "keywords": [],
        },
    },
    "javascript": {
        "name": "JavaScript",
        "category": "language",
        "ecosystem": "javascript",
        "detection": {
            "paths": ["*.js", "*.jsx"],
            "imports": [],
            "keywords": [],
        },
    },
    "typescript": {
        "name": "TypeScript",
        "category": "language",
        "ecosystem": "javascript",
        "detection": {
            "paths": ["*.ts", "*.tsx"],
            "imports": [],
            "keywords": [],
        },
    },
    "go": {
        "name": "Go",
        "category": "language",
        "ecosystem": "go",
        "detection": {
            "paths": ["*.go"],
            "imports": [],
            "keywords": [],
        },
    },
    "rust": {
        "name": "Rust",
        "category": "language",
        "ecosystem": "rust",
        "detection": {
            "paths": ["*.rs"],
            "imports": [],
            "keywords": [],
        },
    },
    # --- Python Backend ---
    "fastapi": {
        "name": "FastAPI",
        "category": "backend",
        "ecosystem": "python-backend",
        "detection": {
            "paths": [],
            "imports": ["fastapi"],
            "keywords": ["FastAPI(", "APIRouter"],
        },
    },
    "django": {
        "name": "Django",
        "category": "backend",
        "ecosystem": "python-backend",
        "detection": {
            "paths": ["manage.py"],
            "imports": ["django"],
            "keywords": ["DJANGO_SETTINGS_MODULE", "django.setup()"],
        },
    },
    "flask": {
        "name": "Flask",
        "category": "backend",
        "ecosystem": "python-backend",
        "detection": {
            "paths": [],
            "imports": ["flask"],
            "keywords": ["Flask(__name__)"],
        },
    },
    # --- Python Data ---
    "sqlalchemy": {
        "name": "SQLAlchemy",
        "category": "database",
        "ecosystem": "python-data",
        "detection": {
            "paths": [],
            "imports": ["sqlalchemy"],
            "keywords": ["declarative_base", "session.query"],
        },
    },
    "pydantic": {
        "name": "Pydantic",
        "category": "data",
        "ecosystem": "python-data",
        "detection": {
            "paths": [],
            "imports": ["pydantic"],
            "keywords": ["BaseModel"],
        },
    },
    "pandas": {
        "name": "Pandas",
        "category": "data",
        "ecosystem": "python-data",
        "detection": {
            "paths": [],
            "imports": ["pandas"],
            "keywords": ["pd.DataFrame", "pd.read_csv"],
        },
    },
    # --- Python Async ---
    "asyncio": {
        "name": "asyncio",
        "category": "language",
        "ecosystem": "python-async",
        "detection": {
            "paths": [],
            "imports": ["asyncio"],
            "keywords": ["async def", "await ", "asyncio.create_task"],
        },
    },
    "aiohttp": {
        "name": "aiohttp",
        "category": "backend",
        "ecosystem": "python-async",
        "detection": {
            "paths": [],
            "imports": ["aiohttp"],
            "keywords": ["aiohttp.ClientSession"],
        },
    },
    # --- Testing ---
    "pytest": {
        "name": "pytest",
        "category": "testing",
        "ecosystem": "python-testing",
        "detection": {
            "paths": ["conftest.py", "pytest.ini"],
            "imports": ["pytest"],
            "keywords": ["def test_", "@pytest.fixture"],
        },
    },
    # --- Frontend ---
    "react": {
        "name": "React",
        "category": "frontend",
        "ecosystem": "javascript-frontend",
        "detection": {
            "paths": [],
            "imports": ["react"],
            "keywords": ["useState(", "useEffect(", "ReactDOM"],
        },
    },
    "nextjs": {
        "name": "Next.js",
        "category": "frontend",
        "ecosystem": "javascript-frontend",
        "detection": {
            "paths": ["next.config.js", "next.config.mjs"],
            "imports": ["next"],
            "keywords": ["getServerSideProps", "use client"],
        },
    },
    # --- Databases ---
    "postgresql": {
        "name": "PostgreSQL",
        "category": "database",
        "ecosystem": "databases",
        "detection": {
            "paths": [],
            "imports": ["psycopg2", "asyncpg", "psycopg"],
            "keywords": ["postgresql://", "postgres://"],
        },
    },
    "redis": {
        "name": "Redis",
        "category": "database",
        "ecosystem": "databases",
        "detection": {
            "paths": [],
            "imports": ["redis", "aioredis"],
            "keywords": ["redis://", "Redis("],
        },
    },
    "mongodb": {
        "name": "MongoDB",
        "category": "database",
        "ecosystem": "databases",
        "detection": {
            "paths": [],
            "imports": ["pymongo", "motor"],
            "keywords": ["mongodb://", "MongoClient"],
        },
    },
    # --- DevOps ---
    "docker": {
        "name": "Docker",
        "category": "devops",
        "ecosystem": "infrastructure",
        "detection": {
            "paths": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml", ".dockerignore"],
            "imports": [],
            "keywords": ["FROM ", "docker build", "docker run"],
        },
    },
    "kubernetes": {
        "name": "Kubernetes",
        "category": "devops",
        "ecosystem": "infrastructure",
        "detection": {
            "paths": [],
            "imports": [],
            "keywords": ["apiVersion:", "kind: Deployment", "kind: Service"],
        },
    },
    "github-actions": {
        "name": "GitHub Actions",
        "category": "devops",
        "ecosystem": "infrastructure",
        "detection": {
            "paths": [".github/workflows/*.yml", ".github/workflows/*.yaml"],
            "imports": [],
            "keywords": ["actions/checkout", "runs-on:"],
        },
    },
    "nginx": {
        "name": "Nginx",
        "category": "devops",
        "ecosystem": "infrastructure",
        "detection": {
            "paths": ["nginx.conf"],
            "imports": [],
            "keywords": ["proxy_pass", "upstream "],
        },
    },
    # --- Cloud ---
    "aws": {
        "name": "AWS",
        "category": "cloud",
        "ecosystem": "cloud",
        "detection": {
            "paths": [],
            "imports": ["boto3", "botocore"],
            "keywords": ["aws_access_key", "s3://", "arn:aws:"],
        },
    },
    # --- Message Queues ---
    "celery": {
        "name": "Celery",
        "category": "backend",
        "ecosystem": "python-backend",
        "detection": {
            "paths": [],
            "imports": ["celery"],
            "keywords": ["@shared_task", "Celery("],
        },
    },
    "kafka": {
        "name": "Kafka",
        "category": "backend",
        "ecosystem": "infrastructure",
        "detection": {
            "paths": [],
            "imports": ["kafka", "confluent_kafka", "aiokafka"],
            "keywords": ["KafkaConsumer", "KafkaProducer"],
        },
    },
    # --- APIs ---
    "graphql": {
        "name": "GraphQL",
        "category": "backend",
        "ecosystem": "web",
        "detection": {
            "paths": ["*.graphql", "*.gql"],
            "imports": ["graphql", "graphene", "strawberry"],
            "keywords": ["type Query", "type Mutation", "ObjectType"],
        },
    },
    "grpc": {
        "name": "gRPC",
        "category": "backend",
        "ecosystem": "infrastructure",
        "detection": {
            "paths": ["*.proto"],
            "imports": ["grpc"],
            "keywords": ["grpc.server", "_pb2"],
        },
    },
    "websockets": {
        "name": "WebSockets",
        "category": "backend",
        "ecosystem": "web",
        "detection": {
            "paths": [],
            "imports": ["websockets", "websocket"],
            "keywords": ["ws://", "wss://", "@app.websocket"],
        },
    },
    # --- Telegram Bots ---
    "aiogram": {
        "name": "aiogram",
        "category": "backend",
        "ecosystem": "python-async",
        "detection": {
            "paths": [],
            "imports": ["aiogram"],
            "keywords": ["Dispatcher(", "@dp.message", "FSMContext"],
        },
    },
}

# =========================================================
# HELPERS
# =========================================================


def glob_to_regex(pattern):
    """Простой glob → regex"""
    pattern = re.escape(pattern)
    pattern = pattern.replace(r"\*\*", ".*")
    pattern = pattern.replace(r"\*", "[^/]*")
    return "^" + pattern + "$"


def match_path(filepath, pattern):
    return bool(re.match(glob_to_regex(pattern), filepath))


def safe_decode(content):
    try:
        return base64.b64decode(content).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def detect_in_file(filepath, content):
    """Ищет технологии в одном файле"""
    found = []

    for tech_id, tech in TECHNOLOGIES.items():
        detection = tech.get("detection", {})
        detected = False
        evidence = []

        # Path signals
        for pattern in detection.get("paths", []):
            if match_path(filepath, pattern):
                detected = True
                evidence.append({"type": "path", "value": pattern})
                break

        # Import signals
        if not detected:
            for imp in detection.get("imports", []):
                patterns = [
                    rf"import\s+{re.escape(imp)}",
                    rf"from\s+{re.escape(imp)}",
                    rf'require\(["\']{re.escape(imp)}',
                ]
                for p in patterns:
                    if re.search(p, content):
                        detected = True
                        evidence.append({"type": "import", "value": imp})
                        break
                if detected:
                    break

        # Keyword signals
        if not detected:
            for kw in detection.get("keywords", []):
                if kw.lower() in content.lower():
                    detected = True
                    evidence.append({"type": "keyword", "value": kw})
                    break

        if detected:
            found.append(
                {
                    "technology": tech_id,
                    "name": tech["name"],
                    "category": tech["category"],
                    "ecosystem": tech["ecosystem"],
                    "evidence": evidence,
                }
            )

    return found


def activity_status(last_seen):
    if not last_seen:
        return "unknown"

    last_date = datetime.strptime(last_seen, "%Y-%m-%d").date()
    days = (date.today() - last_date).days

    if days <= 30:
        return "active"
    elif days <= 180:
        return "stale"
    else:
        return "declining"


# =========================================================
# REPOSITORY SCANNER
# =========================================================


def walk_repo(repo, path=""):
    """Рекурсивно собирает все файлы репозитория"""
    files = []

    try:
        contents = repo.get_contents(path)

        while contents:
            item = contents.pop(0)

            if item.type == "dir":
                with suppress(Exception):
                    contents.extend(repo.get_contents(item.path))
            else:
                files.append(item)

    except Exception:
        pass

    return files


def scan_repo(repo):
    """Сканирует один репозиторий"""
    print(f"  📁 {repo.full_name}...", end=" ", flush=True)

    tech_data = defaultdict(lambda: {"files": set()})

    try:
        files = walk_repo(repo)
        processed = 0

        for file in files:
            if processed >= MAX_FILES_PER_REPO:
                break

            if file.size > MAX_FILE_SIZE:
                continue

            processed += 1

            try:
                raw = repo.get_contents(file.path)

                if raw.encoding != "base64":
                    continue

                content = safe_decode(raw.content)
                detected = detect_in_file(file.path, content)

                for tech in detected:
                    tech_data[tech["technology"]]["files"].add(file.path)
                    tech_data[tech["technology"]]["name"] = tech["name"]
                    tech_data[tech["technology"]]["category"] = tech["category"]
                    tech_data[tech["technology"]]["ecosystem"] = tech["ecosystem"]

            except Exception:
                continue

        print(f"✅ {len(tech_data)} technologies")
        return tech_data

    except Exception as e:
        print(f"❌ {e}")
        return {}


# =========================================================
# ACTIVITY SCANNER (commits)
# =========================================================


def scan_activity(repo, since_date):
    """Собирает активность по коммитам (факт коммита, не строки)"""
    since = datetime.strptime(since_date, "%Y-%m-%d")
    activity = defaultdict(lambda: {"commits": set(), "commit_types": defaultdict(int)})

    try:
        commits = repo.get_commits(since=since)

        for commit in commits:
            if len(commit.parents) > 1:  # merge commit
                continue

            msg = commit.commit.message.lower()

            # Классификация коммита
            for prefix in ["feat", "fix", "refactor", "test", "docs", "chore", "style", "perf"]:
                if msg.startswith(prefix):
                    ctype = prefix
                    break
            else:
                ctype = "other"

            try:
                files = commit.files
            except Exception:
                continue

            for file in files:
                if not file.filename:
                    continue

                detected = detect_in_file(file.filename, file.patch or "")

                for tech in detected:
                    tech_id = tech["technology"]
                    activity[tech_id]["commits"].add(commit.sha[:7])
                    activity[tech_id]["commit_types"][ctype] += 1

    except Exception:
        pass

    return activity


# =========================================================
# PROFILE BUILDER
# =========================================================


def build_profile(g, username, since_date):
    """Собирает полный профиль по всем репозиториям"""

    user = g.get_user(username)
    repos = [r for r in user.get_repos() if not r.fork]

    print(f"\n👤 {username}")
    print(f"📦 {len(repos)} repositories\n")

    # Presence + files
    presence = defaultdict(
        lambda: {
            "name": "",
            "category": "",
            "ecosystem": "",
            "repos": set(),
            "files": set(),
            "first_seen": None,
            "last_seen": None,
        }
    )

    # Activity
    activity = defaultdict(lambda: {"commits": set(), "commit_types": defaultdict(int)})

    for repo in repos:
        # Presence scan
        repo_tech = scan_repo(repo)

        for tech_id, data in repo_tech.items():
            p = presence[tech_id]
            p["name"] = data.get("name", tech_id)
            p["category"] = data.get("category", "")
            p["ecosystem"] = data.get("ecosystem", "")
            p["repos"].add(repo.name)
            p["files"].update(data["files"])

            # Dates
            created = repo.created_at.strftime("%Y-%m-%d")
            updated = repo.updated_at.strftime("%Y-%m-%d")

            if not p["first_seen"] or created < p["first_seen"]:
                p["first_seen"] = created
            if not p["last_seen"] or updated > p["last_seen"]:
                p["last_seen"] = updated

        # Activity scan
        repo_activity = scan_activity(repo, since_date)

        for tech_id, data in repo_activity.items():
            a = activity[tech_id]
            a["commits"].update(data["commits"])
            for ctype, count in data["commit_types"].items():
                a["commit_types"][ctype] += count

    # Сборка финального профиля
    profile = {}

    for tech_id in presence:
        profile[tech_id] = {
            "name": presence[tech_id]["name"],
            "category": presence[tech_id]["category"],
            "ecosystem": presence[tech_id]["ecosystem"],
            "repos": sorted(presence[tech_id]["repos"]),
            "repo_count": len(presence[tech_id]["repos"]),
            "file_count": len(presence[tech_id]["files"]),
            "commits": len(activity.get(tech_id, {}).get("commits", set())),
            "commit_types": dict(activity.get(tech_id, {}).get("commit_types", {})),
            "first_seen": presence[tech_id]["first_seen"],
            "last_seen": presence[tech_id]["last_seen"],
            "status": activity_status(presence[tech_id]["last_seen"]),
        }

    return profile


# =========================================================
# OUTPUT
# =========================================================


def print_profile(profile):
    """Выводит профиль, сгруппированный по экосистемам"""

    # Группировка по экосистемам
    ecosystems = defaultdict(list)
    for tech_id, data in profile.items():
        eco = data.get("ecosystem", "other")
        ecosystems[eco].append((tech_id, data))

    print("\n" + "=" * 60)
    print("TECHNOLOGY PROFILE")
    print("=" * 60)

    status_icons = {"active": "🟢", "stale": "🟡", "declining": "⚪", "unknown": "❓"}

    for eco, techs in sorted(ecosystems.items()):
        print(f"\n── {eco} ──")

        for _tech_id, data in sorted(techs, key=lambda x: x[1]["repo_count"], reverse=True):
            icon = status_icons.get(data["status"], "❓")

            print(
                f"  {icon} {data['name']:<20} "
                f"repos: {data['repo_count']:<3} "
                f"files: {data['file_count']:<4} "
                f"commits: {data['commits']:<4} "
                f"{data['first_seen']} → {data['last_seen']}"
            )

    # Статистика
    total_techs = len(profile)
    active = sum(1 for d in profile.values() if d["status"] == "active")
    stale = sum(1 for d in profile.values() if d["status"] == "stale")
    declining = sum(1 for d in profile.values() if d["status"] == "declining")

    print(f"\n{'=' * 60}")
    print(
        f"Total: {total_techs} technologies ({active} active, {stale} stale, {declining} declining)"
    )
    print(f"{'=' * 60}\n")


def save_profile(profile, filename=OUTPUT_FILE):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    print(f"💾 Saved to {filename}")


# =========================================================
# MAIN
# =========================================================


def main():
    auth = Auth.Token(TOKEN)
    g = Github(auth=auth)

    profile = build_profile(g, USERNAME, SINCE_DATE)

    print_profile(profile)
    save_profile(profile)


if __name__ == "__main__":
    main()
