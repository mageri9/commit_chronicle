"""Утилиты для очистки и валидации данных."""

import re

# Регулярное выражение для строгого соответствия правилам GitHub:
# - Начинается и заканчивается на букву/цифру
# - Внутри разрешены буквы, цифры и одиночные дефисы (без двух дефисов подряд)
# - Длина от 1 до 39 символов
GITHUB_USERNAME_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}$")


def clean_github_username(username: str) -> str:
    """Очищает входящую строку, извлекая чистый GitHub-юзернейм.

    Обрабатывает:
    - Ссылки вида https://github.com/username
    - Ведущие символы @
    - Пробелы, слеши в конце и параметры запроса (?tab=repositories)
    """
    if not username:
        return ""

    username = username.strip().lower()

    # 1. Если передана ссылка на github, извлекаем часть после github.com/
    url_match = re.search(r"github\.com/([a-zA-Z0-9_-]+)", username)
    if url_match:
        username = url_match.group(1)

    # 2. Убираем ведущий @ и слеши по краям
    username = username.lstrip("@").strip("/")

    # 3. Отсекаем параметры запроса (например, ?tab=repositories) или якоря (#)
    username = username.split("?")[0].split("#")[0]

    return username


def validate_github_username(username: str) -> bool:
    """Проверяет имя пользователя на соответствие правилам GitHub."""
    return bool(GITHUB_USERNAME_REGEX.match(username))