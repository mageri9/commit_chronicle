"""
Приёмник GitHub push-вебхуков.

Намеренно тонкий: единственная работа здесь — проверить подпись и как можно
быстрее ответить 200, положив payload в arq-очередь. Вся тяжёлая работа
(REST-добор деталей коммитов) — в src.worker.tasks.handle_push_event,
выполняется тем же пулом воркеров, что и обычный /analyze.

GitHub требует ответ на вебхук в пределах ~10 секунд, иначе помечает
доставку failed (хотя и ретраит) — синхронная обработка здесь была бы риском.

Запуск:
    python -m src.webhook.server
Требует WEBHOOK_ENABLED=true и WEBHOOK_SECRET в .env (см. config.py — иначе
приложение не стартует, см. проверку в конце config.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json

from aiohttp import web
from arq import create_pool
from arq.connections import RedisSettings

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

_arq_pool = None


async def _get_pool():
    """
    Собственный лёгкий pool, отдельный от src.bot.app.get_arq_pool() —
    webhook-сервис работает как самостоятельный процесс/контейнер, не должен
    тянуть за собой зависимости бота (telegram и т.п.). Если захочется убрать
    дублирование — вынести оба в общий src/queue.py.
    """
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


def _verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


async def handle_github_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _verify_signature(settings.webhook_secret, body, signature):
        logger.warning("webhook: неверная подпись, запрос отклонён")
        return web.Response(status=401, text="invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        # GitHub шлёт ping сразу после создания хука — просто подтверждаем.
        return web.Response(status=200, text="pong")

    if event_type != "push":
        # Хук подписан только на push (см. register.py), но на всякий
        # случай не роняем обработку прочих событий, если их пришлют вручную.
        return web.Response(status=200, text=f"ignored event: {event_type}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="invalid json")

    pool = await _get_pool()
    await pool.enqueue_job("handle_push_event", payload)

    return web.Response(status=200, text="queued")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook/github", handle_github_webhook)
    return app


if __name__ == "__main__":
    if not settings.webhook_enabled:
        raise SystemExit(
            "WEBHOOK_ENABLED=false — включите в .env перед запуском этого сервиса"
        )
    web.run_app(create_app(), port=settings.webhook_port)