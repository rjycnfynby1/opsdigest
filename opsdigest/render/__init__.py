"""Отображение сводки: HTML-отчёт (браузер/телефон, можно переслать коллегам) и короткий текст для Telegram."""
from .html import render_html
from .telegram_text import render_telegram

TYPE_RU = {
    "reply": "Ответить",
    "action": "Сделать",
    "follow_up": "Зависло",
    "decision": "Решение",
    "incident": "Инцидент",
    "question": "Уточнить",
    "fyi": "К сведению",
}
PRIO_RU = {"high": "высокий", "medium": "средний", "low": "низкий", "normal": "обычный", "ignore": "игнор"}
PERIOD_RU = {"day": "день", "week": "неделю", "month": "месяц", "custom": "период", "demo": "демо", "hours": "последние часы", "source": "очередь"}

__all__ = ["render_html", "render_telegram", "TYPE_RU", "PRIO_RU", "PERIOD_RU"]
