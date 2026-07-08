"""
Исключения транспортного слоя GitHub Engine.

Это исключения уровня "клиент говорит с GitHub API" — они не знают
ничего про репозитории, коммиты или бизнес-смысл ошибки. Бизнес-уровень
(collector) ловит их и решает, что делать: пропустить репозиторий,
повторить запрос, остановить сбор целиком.

Иерархия сознательно плоская: не пытаемся заранее угадать все возможные
причины ошибок GitHub API — вместо этого несём в исключении сырые данные
(status_code, response body), которые вызывающий код может исследовать
сам при необходимости.
"""

from __future__ import annotations


class GitHubAPIError(Exception):
    """
    Базовая ошибка транспортного слоя. Всё, что можно поймать одним
    except, чтобы обработать "что-то пошло не так на уровне HTTP/API",
    не заботясь о конкретной причине.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)

    def __repr__(self) -> str:  # pragma: no cover — удобство отладки в логах
        return (
            f"{self.__class__.__name__}("
            f"status_code={self.status_code!r}, "
            f"message={str(self)!r})"
        )


class AuthenticationError(GitHubAPIError):
    """
    Токен невалиден, отозван или не имеет нужных прав (401/403 не по
    причине rate limit). Отличается от RateLimitExceeded тем, что
    повторная попытка с тем же токеном бессмысленна — токен нужно
    исключить из ротации, а не подождать и повторить.
    """


class RateLimitExceeded(GitHubAPIError):
    """
    Исчерпан лимит запросов — либо REST (X-RateLimit-*), либо GraphQL
    (rateLimit.remaining в теле ответа). reset_at — unix-время, когда
    лимит обновится; используется RateLimiter'ом, чтобы не ждать вслепую.
    """

    def __init__(
        self,
        message: str,
        *,
        reset_at: float | None = None,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        self.reset_at = reset_at
        super().__init__(message, status_code=status_code, response_body=response_body)


class GraphQLError(GitHubAPIError):
    """
    GraphQL-запрос вернул HTTP 200, но в теле ответа есть поле "errors".
    Это отдельный случай от GitHubAPIError, потому что GraphQL почти
    всегда отвечает 200 даже при ошибке — статус-код не годится как
    сигнал, приходится проверять тело ответа отдельно.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict] | None = None,
        query: str | None = None,
    ) -> None:
        self.errors = errors or []
        self.query = query
        super().__init__(message)


class NodeLimitExceeded(GraphQLError):
    """
    GraphQL-запрос отклонён из-за превышения лимита узлов
    (MAX_NODE_LIMIT_EXCEEDED). Отдельный класс — потому что реакция
    на эту ошибку отличается от прочих GraphQL-ошибок: нужно уменьшить
    объём запроса (paginate меньшими порциями), а не ретраить как есть.
    """