"""Настройки чатов (приоритет, режим, описание, исключение) — из config.json в базу.

Правила хранятся в config.json (telegram.chats: match = номер чата, priority, kind, exclude, description) — окно пишет
их в разделе «Чаты». В базе у каждого чата лежит действующий результат: priority (ignore = исключён), mode и note
(описание от пользователя — его читает модель). Обновляется при синхронизации, загрузке экспорта, сохранении настроек
и перед каждой сводкой — поэтому изменения действуют и без входа в Telegram.
"""
from __future__ import annotations

from .storage import Store


def apply_chat_rules(cfg, store: Store) -> int:
    """Пересчитать приоритет/режим/описание всех чатов базы по правилам. Возвращает число изменённых."""
    n = 0
    skip_muted = bool(cfg.telegram.get("skip_muted"))
    for c in store.chats():
        rule = cfg.chat_rule(c.title, c.username, c.chat_id, c.kind or "group")
        prio = rule["priority"]
        if skip_muted and c.muted and not rule["explicit"]:
            prio = "ignore"
        if (prio, rule["kind"], rule["note"]) != (c.priority, c.mode, c.note):
            store.set_chat_rule(c.chat_id, prio, rule["kind"], rule["note"], c.source)
            n += 1
    store.db.commit()
    return n
