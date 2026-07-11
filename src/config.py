"""
Централизованные настройки с валидацией.
Крашится при запуске если нет обязательных переменных.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from src.logger import get_logger


logger = get_logger(__name__)

PLACEHOLDER_MARKER = "xxx"


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # GitHub
    github_token: str
    github_extra_tokens: str = ""

    # Telegram
    telegram_bot_token: str

    # Storage
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    redis_url: str

    # Limits
    max_workers: int = 10
    max_requests_per_user: int = 10
    user_cooldown_minutes: int = 30

    max_backfill_days: int = 730
    fast_path_max_age: int = 120

    max_backfill_days: int = 730

    # Cache TTL in seconds
    cache_ttl_github: int = 3600
    cache_ttl_llm: int = 86400

    # ---------- Вебхуки ----------
    webhook_enabled: bool = False
    webhook_secret: str = ""
    webhook_public_url: str = ""
    webhook_port: int = 8080
    owner_github_username: str = ""

    # Logging
    log_level: str = "INFO"

    @property
    def all_github_tokens(self) -> list[str]:
        """Все токены GitHub одним списком."""
        tokens = [self.github_token]
        if self.github_extra_tokens:
            tokens.extend(
                t.strip() for t in self.github_extra_tokens.split(",") if t.strip()
            )
        return tokens

    @property
    def is_configured(self) -> bool:
        """Быстрая проверка, что ключи не из .example."""
        critical_keys = [
            self.github_token,
            self.telegram_bot_token,
        ]
        return not any(PLACEHOLDER_MARKER in key for key in critical_keys)


# Глобальные экземпляры
try:
    settings = Settings()
    llm_settings = LLMSettings()
except Exception as e:
    logger.error(e)
    raise

# Проверка при старте
if not settings.is_configured:
    raise RuntimeError(
        "❌ Настройки содержат placeholder'ы. "
        "Скопируйте .env.example → .env и заполните реальными ключами."
    )

if settings.webhook_enabled and not settings.webhook_secret:
    raise RuntimeError(
        "❌ WEBHOOK_ENABLED=true, но WEBHOOK_SECRET пустой — "
        "без секрета приём вебхуков небезопасен (нельзя проверить подпись)."
    )