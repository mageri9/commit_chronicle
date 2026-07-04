"""Обработчик /status <job_id> и /status_<job_id>."""

from telegram import Update
from telegram.ext import ContextTypes
from src.storage.database import get_request


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статус задачи по ID."""

    # Извлечь job_id: /status_abc123 или /status abc123
    text = update.message.text.strip()
    if text.startswith("/status_"):
        job_id = text.split("_", 1)[1]
    elif context.args:
        job_id = context.args[0]
    else:
        await update.message.reply_text("ℹ️ Укажи ID: /status abc123")
        return

    # Запросить БД
    record = await get_request(job_id)

    if record is None:
        await update.message.reply_text("❌ Задача не найдена. Проверь ID.")
        return

    # Проверка владельца
    if record.get("chat_id") != str(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа к этому анализу.")
        return

    status = record["status"]

    if status == "pending":
        await update.message.reply_text("⏳ Ожидает в очереди...")
    elif status == "processing":
        await update.message.reply_text("🔄 Анализируем...")
    elif status == "done":
        result_json = record.get("result_json", "{}")
        await update.message.reply_text(
            f"✅ Готово!\n"
            f"👤 {record['username']}\n"
            f"📅 {record['period_start']} → {record['period_end']}\n"
            f"📦 Данных: {len(result_json)} символов"
        )
    elif status == "failed":
        error = record.get("error_message", "неизвестная ошибка")
        await update.message.reply_text(f"❌ Ошибка: {error}")