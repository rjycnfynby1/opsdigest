"""Встроенные примеры переписки (выдуманные) — чтобы проверить модель и вид отчёта без Telegram."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .entities import EntityExtractor
from .storage import Chat, Message, Store, iso

CHATS = {
    "inc": Chat(-1001000000001, "Авария: недоступен личный кабинет (INC0048213)", "group", priority="high",
                mode="incident", unread=37),
    "mon": Chat(-1001000000002, "Мониторинг PROD — алерты", "channel", priority="normal", mode="alerts", unread=58),
    "dev": Chat(500000001, "Алексей Смирнов (разработка биллинга)", "private", priority="high", unread=3),
    "team": Chat(-1001000000003, "L2 поддержка — команда", "group", priority="normal", unread=21),
    "rel": Chat(-1001000000004, "Релизы платформы", "channel", priority="low", unread=4),
    "flood": Chat(-1001000000005, "Отдел: болталка", "group", priority="low", unread=46),
}

# (чат, минуты от начала, автор, текст, моё?, упомянули меня?, ответ на сообщение этого чата с такой минутой)
MSGS = [
    ("mon", 0, "Zabbix", "PROBLEM: lk-web-03 HTTP 502 > 5% (severity: High)", 0, 0, None),
    ("mon", 2, "Zabbix", "PROBLEM: lk-web-01 HTTP 502 > 5% (severity: High)", 0, 0, None),
    ("mon", 4, "Zabbix", "PROBLEM: auth-svc latency p95 > 3s", 0, 0, None),
    ("inc", 6, "Бот аварий", "Создан аварийный чат по INC0048213: массовые жалобы на недоступность личного кабинета. Приоритет P1. Добавлены: дежурный разработчик, L2, менеджер смены.", 0, 0, None),
    ("inc", 8, "Ирина Ковалёва (менеджер смены)", "Коллеги, по ЛК 40+ обращений за 10 минут, клиенты не могут войти. Кто смотрит?", 0, 0, None),
    ("inc", 10, "Дмитрий Орлов (dev, auth)", "Смотрю. auth-svc отвечает медленно, пул соединений к БД сессий забит.", 0, 0, None),
    ("inc", 13, "Я", "L2 на связи. По обращениям: ошибка 502 при входе, затронуты и веб, и мобильное приложение. Шаблон ответа клиентам выпущу через 10 минут.", 1, 0, None),
    ("mon", 15, "Zabbix", "PROBLEM: db-sessions connections 98% of max_connections", 0, 0, None),
    ("inc", 19, "Дмитрий Орлов (dev, auth)", "Причина похожа на утечку соединений после вчерашнего релиза auth-svc 4.12 (CHG0003318). Предлагаю откат.", 0, 0, None),
    ("inc", 21, "Ирина Ковалёва (менеджер смены)", "Согласовываю откат. Алексей, нужен список крупных партнёров, у которых есть обращения, — их надо уведомить отдельно.", 0, 1, None),
    ("inc", 35, "Дмитрий Орлов (dev, auth)", "Откат auth-svc до 4.11 выполнен, соединения освобождаются.", 0, 0, None),
    ("mon", 37, "Zabbix", "OK: lk-web-01 HTTP 502 > 5% resolved", 0, 0, None),
    ("mon", 38, "Zabbix", "OK: lk-web-03 HTTP 502 > 5% resolved", 0, 0, None),
    ("mon", 40, "Zabbix", "OK: auth-svc latency p95 > 3s resolved", 0, 0, None),
    ("inc", 44, "Ирина Ковалёва (менеджер смены)", "Вход работает. Алексей, напоминаю про список партнёров. И нужен итоговый текст для клиентов о восстановлении.", 0, 1, 21),
    ("inc", 52, "Дмитрий Орлов (dev, auth)", "Завёл задачу AUTH-1290 на исправление утечки. Пока 4.12 не выкатываем повторно. Нужны от L2 примеры обращений с временем ошибки, чтобы сопоставить с логами.", 0, 0, None),
    ("mon", 180, "Zabbix", "PROBLEM: db-sessions connections 85% of max_connections", 0, 0, None),
    ("mon", 240, "Zabbix", "PROBLEM: db-sessions connections 87% of max_connections", 0, 0, None),
    ("dev", 60, "Алексей Смирнов", "Привет! По BILL-482 (двойные списания при автоплатеже) — у вас есть свежие обращения? Мне нужно 2–3 номера с данными платежей, чтобы воспроизвести.", 0, 0, None),
    ("dev", 62, "Алексей Смирнов", "Если есть INC с этим — скинь, пожалуйста, сегодня, завтра утром планирую фикс.", 0, 0, None),
    ("team", 70, "Ольга Петрова", "Передаю смену. Открыто 14 обращений на группе L2-Биллинг, 3 из них про двойные списания: INC0048190, INC0048201, INC0048207.", 0, 0, None),
    ("team", 72, "Ольга Петрова", "По INC0048190 клиент ждёт ответа с прошлой пятницы, разработка молчит. Лёша, можешь уточнить статус BILL-482?", 0, 1, None),
    ("team", 90, "Сергей Волков", "Кто выходит в субботу на дежурство? В графике пусто.", 0, 0, None),
    ("team", 95, "Ольга Петрова", "Вроде Алексей, но он не подтвердил.", 0, 1, 90),
    ("team", 300, "Сергей Волков", "После аварии по ЛК вал повторных обращений, 60+ на группе. Давайте закрывать шаблоном, текст есть?", 0, 0, None),
    ("rel", 120, "Релиз-бот", "Сегодня 22:00 — релиз payment-gateway 2.8 (CHG0003321): новые лимиты автоплатежей, изменение API уведомлений партнёрам. Возможны кратковременные ошибки 503.", 0, 0, None),
    ("rel", 125, "Релиз-бот", "Релиз auth-svc 4.12 (CHG0003318) — откатан, повторная выкатка после AUTH-1290.", 0, 0, None),
    ("flood", 100, "Марина", "Коллеги, у Андрея сегодня день рождения! 🎉", 0, 0, None),
    ("flood", 101, "Андрей", "Спасибо!", 0, 0, None),
    ("flood", 140, "Павел", "Кто на обед?", 0, 0, None),
]


def load_demo(cfg, store: Store, tz) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(hours=14)
    store.kv_set("me", json.dumps({"id": 1, "name": cfg.profile.get("name") or "Алексей", "username": "demo"},
                                  ensure_ascii=False))
    for c in CHATS.values():
        store.upsert_chat(c)
    msgs = []
    ids = {(ck, mins): 1000 + i for i, (ck, mins, *_rest) in enumerate(MSGS, 1)}
    for i, (ck, mins, who, text, out, ment, reply) in enumerate(MSGS, 1):
        chat = CHATS[ck]
        # reply — минута сообщения в том же чате, на которое отвечают
        msgs.append(Message(chat_id=chat.chat_id, msg_id=1000 + i, date=iso(start + timedelta(minutes=mins)),
                            sender=who, text=text, is_out=bool(out), mentioned=bool(ment),
                            reply_to=ids.get((ck, reply)) if reply is not None else None))
    store.add_messages(msgs)
    store.add_entities(EntityExtractor(cfg.get("entities") or {}).rows(msgs))
    return start - timedelta(minutes=1), now + timedelta(minutes=1)
