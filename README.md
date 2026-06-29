# Commit Chronicle

Telegram-бот для анализа GitHub-активности. Отправь `/analyze @username` — получи сводку и компактный JSON-файл.

## Возможности

- Сбор коммитов по пользователю и периоду через `/analyze @username [YYYY-MM-DD]`
- Параллельная обработка репозиториев (ThreadPoolExecutor)
- Фильтрация мёртвых репо по `pushed_at` — минимум лишних API-запросов
- Fingerprint-дедупликация: SHA256(repo:pushed_at) — повторный запрос отдаёт кэш без пересборки
- Авто-уведомление: бот сам обновляет сообщение когда анализ готов (Redis pub/sub)
- Компактный JSON: короткие ключи + индекс расширений + даты как день-офсет (~55–60% экономии токенов)
- Round-robin ротация GitHub-токенов с блокировкой при rate limit
- Recovery зависших задач при старте воркера
- IDOR-защита: результат доступен только чату, который запросил анализ

## Стек

`Python 3.11` · `python-telegram-bot v20` · `arq` · `Redis` · `SQLite + SQLAlchemy Core` · `PyGithub` · `Pydantic v2` · `loguru` · `Docker`

## Быстрый старт

```bash
cp .env.example .env
# заполнить GITHUB_TOKEN и TELEGRAM_BOT_TOKEN
docker compose up
```

Или локально без Docker:

```bash
pip install -r requirements.txt
# запустить Redis отдельно
arq src.worker.settings.WorkerSettings &
python -m src.bot.main
```

## Конфигурация

| Переменная | Обязательна | Описание |
|------------|-------------|----------|
| `GITHUB_TOKEN` | ✅ | Основной GitHub-токен (`public_repo` или `repo`) |
| `GITHUB_EXTRA_TOKENS` | — | Дополнительные токены через запятую |
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен от @BotFather |
| `REDIS_URL` | ✅ | `redis://localhost:6379/0` |
| `DATABASE_URL` | — | `sqlite+aiosqlite:///data/app.db` |
| `MAX_WORKERS` | — | Потоков для сбора репо (по умолчанию: 10) |
| `MAX_REQUESTS_PER_USER` | — | Лимит запросов на пользователя (по умолчанию: 10) |
| `USER_COOLDOWN_MINUTES` | — | Кулдаун между запросами (по умолчанию: 30) |
| `LOG_LEVEL` | — | `INFO` / `DEBUG` |

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/analyze @username` | Анализ за последние 2 года |
| `/analyze @username 2024-01-01` | Анализ с указанной даты |
| `/status <job_id>` | Статус задачи по ID |

## Архитектура

```
Telegram Bot (/analyze @username)
    ↓
arq.enqueue_job("analyze_github_user")
    ↓
Redis Queue
    ↓
Worker (arq)
    ├── find_existing_requests (SQLite dedup)
    ├── get_github_fingerprint (smart invalidation)
    ├── run_in_executor(collect_commits) — ThreadPoolExecutor
    │   ├── TokenRotator (round-robin GitHub tokens)
    │   ├── process_single_repo × N (параллельно)
    │   └── normalize() → AnalysisResult (Pydantic)
    ├── to_compact() + serialize_result() (~55-60% сжатие JSON)
    ├── update_request_status(done) + fingerprint
    └── publish("job:done") → Redis Pub/Sub
        ↓
    Bot Listener → edit_message + send_document(JSON)
```

## Формат JSON

Результат сжимается для экономии токенов при передаче в LLM.

```json
{
  "user": "torvalds",
  "from": "2024-01-01",
  "ext": [".c", ".h", ".py"],
  "repos": {
    "torvalds/linux": [
      {
        "d": 74,
        "m": "tcp: fix memory leak in error path",
        "f": [
          ["net/ipv4/tcp", 0, 12, 3],
          ["include/net/tcp", 1, 2]
        ]
      }
    ]
  }
}
```

Расшифровка:

- `from` — базовая дата периода
- `ext` — индекс расширений файлов
- `d` — день от `from` (восстановление: `from + d days`)
- `m` — subject коммита, до 72 символов
- `f` — файлы: `[путь_без_расширения, индекс_ext, добавлено?, удалено?]`; `+`/`-` опускаются если 0

## Структура проекта

```
src/
├── config.py              # pydantic-settings
├── models/models.py       # AnalysisResult, CompactResult, to_compact(), serialize_result()
├── core/
│   ├── collector.py       # GitHub API → dict (параллельно)
│   ├── normalizer.py      # dict → AnalysisResult
│   ├── fingerprint.py     # SHA256(repo:pushed_at)
│   ├── token_rotator.py   # Round-robin GitHub токенов
│   └── exceptions.py      # RateLimitError, TokenExhaustedError, RepoAccessError
├── storage/
│   ├── database.py        # SQLite WAL + индексы (SQLAlchemy Core)
│   ├── cache.py           # Redis get/set
│   └── pubsub.py          # Redis pub/sub
├── worker/
│   ├── settings.py        # WorkerSettings (arq)
│   └── tasks.py           # analyze_github_user + format_summary
├── bot/
│   ├── app.py             # Application + pub/sub listener + catch_up
│   ├── main.py            # Точка входа
│   └── handlers/
│       ├── analyze.py     # /analyze @username [period]
│       └── status.py      # /status <job_id>
└── logger.py              # loguru (цвета + ротация)
```

## Роадмап

- [ ] LLM-анализ активности (summary, паттерны, рекомендации)
- [ ] PDF-отчёты
- [ ] Метрики воркера
- [ ] Поддержка организаций
