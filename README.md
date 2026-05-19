# Skillbook

Developer intelligence from Git history.

Skillbook анализирует GitHub-коммиты и строит инженерный профиль разработчика.

Без клонирования репозиториев.
Без LLM.
Без базы данных.
Только GitHub API, сигналы из коммитов и немного здравого смысла.

---

# Возможности

- Анализ GitHub-коммитов через API
- Извлечение навыков:
  - по путям файлов
  - по содержимому patch/diff
  - по типам коммитов
- Weighted scoring
- Recency-aware профиль
- Co-occurrence связи между технологиями
- CSV + JSON экспорт
- Построение инженерного профиля разработчика

---

# Как это работает

Skillbook:

1. Получает коммиты пользователя через GitHub API
2. Анализирует изменённые файлы
3. Определяет навыки через:
   - glob rules
   - regex rules
   - commit metadata
4. Строит агрегированный профиль:
   - активность
   - специализация
   - интенсивность изменений
   - temporal relevance
   - связи между технологиями

---

# Установка

```bash
pip install -r requirements.txt
```

Минимальные зависимости:

```txt
PyGithub
python-dotenv
```

---

# Настройка

Создай `.env` файл:

```env
GITHUB_TOKEN=ghp_your_token
```

GitHub token:
https://github.com/settings/tokens

Для публичных репозиториев достаточно:

```text
public_repo
```

---

# Запуск

```bash
python main.py username
```

Пример:

```bash
python main.py mageri9
```

---

# Output

## CSV

```text
output/skills.csv
```

Сырые события:

- commit
- file
- detected skill
- weight
- confidence
- lines changed

---

## JSON

```text
output/profile.json
```

Агрегированный инженерный профиль:

- skills
- repositories
- timelines
- co-occurrence graph
- activity metrics

---

# Пример профиля

```text
ИНЖЕНЕРНЫЙ ПРОФИЛЬ
======================================================================

fastapi (backend) 🟢 active

  Коммитов: 15
  Репозиториев: 2

  Строк изменено: 627

  Raw weight: 78.9
  Recency-adjusted: 72.1

  Типы коммитов:
    feature:15
    maintenance:4
    refactor:2

  Период:
    2025-03-12 → 2026-05-15

  Вместе с:
    asyncio(12)
    pydantic(8)
    redis(5)
```

---

# Skill Extraction

Навыки определяются через `SKILL_RULES`.

## Path rules

Определение по имени файла:

```python
("*.py", "python", 0.9, "language")
```

## Patch rules

Определение по содержимому diff:

```python
(r"FastAPI\(", "fastapi", 3, "backend")
```

---

# Roadmap

- Timeline visualization
- Skill graphs
- Contributor fingerprints
- Team analytics
- HTML reports
- CLI package
- Ownership analysis

---

# Почему это интересно

Git history показывает:
- что разработчик реально делал
- насколько активно
- как менялся стек
- какие технологии используются вместе

Коммиты обычно честнее резюме.

---

# License

MIT
