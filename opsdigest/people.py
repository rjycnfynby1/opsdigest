"""Люди: участники чатов и заметки пользователя о коллегах.

Заметка о человеке одна на все чаты (ключ — номер пользователя Telegram) и хранится в config.json → people:
  {"123456": {"name": "Иван Петров", "username": "ipetrov", "priority": "high | normal | low",
              "post": "должность", "dept": "подразделение", "contacts": "телефоны, почта",
              "card": "из вики/адресной книги (компетенции, руководитель)", "card_at": "…", "card_url": "…",
              "area": "зона ответственности", "ask": "по каким вопросам я могу к нему обратиться",
              "comes": "с чем он может прийти ко мне", "note": "прочее", "aliases": ["Ваня", "Петрович"],
              "ai": "общее описание от ИИ — по его сообщениям во всех чатах", "ai_at": "…",
              "roles": {"-100123": {"text": "его роль в этом чате (вручную)", "ai": "роль по мнению ИИ", "ai_at": "…"}},
              "updated": "…"}}
Ручное описание — сверху, часть ИИ — отдельно ниже: повторный анализ обновляет только её.
Модель видит заметки участников чата в заголовке чата (сводка, ИИ в чате) — так проще понять, кто взял задачу и
у кого смежные компетенции. Список участников чата — из переписки в базе и из Telegram (таблица members).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .storage import Store, iso

FIELDS = ("post", "dept", "area", "ask", "comes", "contacts", "note")
PRIORITIES = ("high", "normal", "low")
FIELD_RU = {"post": "должность", "dept": "подразделение", "area": "зона ответственности",
            "ask": "с чем к нему обращаться", "comes": "с чем приходит ко мне", "contacts": "контакты", "note": "заметки"}

MEMBERS_SQL = """CREATE TABLE IF NOT EXISTS members (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT,
    username   TEXT,
    role       TEXT,           -- creator | admin | member | bot
    updated_at TEXT,
    PRIMARY KEY (chat_id, user_id)
)"""


def ensure(store: Store) -> None:
    store.db.execute(MEMBERS_SQL)


def all_people(cfg_or_raw) -> dict[str, dict]:
    data = cfg_or_raw.get("people") if hasattr(cfg_or_raw, "get") else None
    return {str(k): v for k, v in (data or {}).items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def has_text(p: dict, chat_id: int | None = None) -> bool:
    if any(str(p.get(f) or "").strip() for f in FIELDS) or str(p.get("ai") or "").strip():
        return True
    if p.get("priority") in ("high", "low"):
        return True
    r = role(p, chat_id) if chat_id is not None else {}
    return bool(r.get("text") or r.get("ai"))


def role(p: dict, chat_id: int | None) -> dict:
    return ((p.get("roles") or {}).get(str(chat_id)) or {}) if chat_id is not None else {}


def describe(p: dict, limit: int = 420, chat_id: int | None = None) -> str:
    """Человек одной строкой для модели: приоритет, ручные поля, роль в этом чате, часть ИИ."""
    parts = []
    if p.get("priority") == "high":
        parts.append("важный для пользователя человек")
    elif p.get("priority") == "low":
        parts.append("его сообщения пользователю менее важны")
    parts += [f"{FIELD_RU[f]}: {str(p[f]).strip()}" for f in FIELDS if str(p.get(f) or "").strip()]
    r = role(p, chat_id)
    if r.get("text"):
        parts.append(f"роль в этом чате: {r['text'].strip()}")
    elif r.get("ai"):
        parts.append(f"роль в этом чате (по мнению ИИ): {r['ai'].strip()}")
    if str(p.get("ai") or "").strip():
        parts.append(f"по переписке (ИИ): {str(p['ai']).strip()}")
    if str(p.get("card") or "").strip():
        parts.append(f"из вики/адресной книги: {str(p['card']).strip()}")
    s = "; ".join(parts)
    return s[:limit] + ("…" if len(s) > limit else "")


def save_members(store: Store, chat_id: int, rows: list[dict]) -> int:
    ensure(store)
    now = iso(datetime.now(timezone.utc))
    for r in rows:
        store.db.execute(
            """INSERT INTO members(chat_id, user_id, name, username, role, updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, username=excluded.username,
               role=COALESCE(excluded.role, members.role), updated_at=excluded.updated_at""",
            (chat_id, int(r["id"]), r.get("name") or "", r.get("username"), r.get("role"), now))
    store.db.commit()
    return len(rows)


def chat_members(store: Store, chat_id: int, people: dict[str, dict] | None = None,
                 me_id: int | None = None, source: str = "telegram") -> list[dict]:
    """Участники чата: кто писал (из базы, со счётчиком и датой) + список участников из Telegram (если загружали)."""
    ensure(store)
    people = people or {}
    by: dict[int | str, dict] = {}
    for r in store.db.execute(
            """SELECT sender_id, sender, COUNT(*) AS n, MAX(date) AS last FROM messages
               WHERE source=? AND chat_id=? AND is_out=0 AND sender IS NOT NULL AND sender!=''
               GROUP BY sender_id ORDER BY n DESC""", (source, chat_id)).fetchall():
        key = r["sender_id"] or ("n:" + r["sender"])
        by[key] = {"id": r["sender_id"], "name": r["sender"], "msgs": r["n"], "last": r["last"] or "",
                   "username": None, "tg_role": None, "member": False}
    for r in store.db.execute("SELECT * FROM members WHERE chat_id=?", (chat_id,)).fetchall():
        x = by.setdefault(r["user_id"], {"id": r["user_id"], "name": r["name"], "msgs": 0, "last": "",
                                         "username": None, "tg_role": None, "member": True})
        x.update(username=r["username"] or x.get("username"), tg_role=r["role"] or x.get("tg_role"), member=True)
        if r["name"]:
            x["name"] = r["name"]
    out = []
    for x in by.values():
        if me_id and x["id"] == me_id:
            continue
        p = people.get(str(x["id"])) if x["id"] else None
        x["person"] = p or None
        x["described"] = bool(p and any(str(p.get(f) or "").strip() for f in FIELDS))
        x["ai"] = bool(p and p.get("ai"))
        r = role(p or {}, chat_id)
        x["role_text"] = r.get("text") or ""
        x["role_ai"] = r.get("ai") or ""
        x["priority"] = (p or {}).get("priority") or "normal"
        out.append(x)
    out.sort(key=lambda x: (x["priority"] != "high", -(x["msgs"] or 0), (x["name"] or "").lower()))
    return out


def chat_notes(cfg, store: Store, chat_id: int, limit: int = 12) -> list[str]:
    """«Имя: зона…; с чем к нему…» — для заголовка чата в запросе к модели (только описанные участники)."""
    people = all_people(cfg)
    if not any(has_text(p) for p in people.values()):
        return []
    ids: list[int] = []
    try:
        ensure(store)
        ids = [r[0] for r in store.db.execute(
            """SELECT sender_id FROM messages WHERE chat_id=? AND sender_id IS NOT NULL AND is_out=0
               GROUP BY sender_id ORDER BY COUNT(*) DESC""", (chat_id,)).fetchall()]
        ids += [r[0] for r in store.db.execute("SELECT user_id FROM members WHERE chat_id=?", (chat_id,)).fetchall()]
    except Exception:  # noqa: BLE001
        pass
    out, seen = [], set()
    for uid in ids:
        if uid in seen:
            continue
        seen.add(uid)
        p = people.get(str(uid))
        if p and has_text(p, chat_id):
            out.append(f"{p.get('name') or uid}: {describe(p, chat_id=chat_id)}")
        if len(out) >= limit:
            break
    return out


def common_chats(store: Store, user_id: int, limit: int = 30) -> list[dict]:
    """В каких чатах человек писал или числится участником."""
    ensure(store)
    rows = store.db.execute(
        """SELECT c.chat_id, c.title, c.kind, COUNT(m.msg_id) AS n, MAX(m.date) AS last FROM chats c
           LEFT JOIN messages m ON m.source=c.source AND m.chat_id=c.chat_id AND m.sender_id=?
           WHERE c.chat_id IN (SELECT chat_id FROM messages WHERE sender_id=?
                               UNION SELECT chat_id FROM members WHERE user_id=?)
           GROUP BY c.chat_id ORDER BY n DESC LIMIT ?""", (user_id, user_id, user_id, limit)).fetchall()
    return [{"id": r["chat_id"], "title": r["title"] or "", "kind": r["kind"] or "", "msgs": r["n"],
             "last": r["last"] or ""} for r in rows]


def clean(body: dict, prev: dict | None = None) -> dict:
    """Поля заметки из окна: обрезка, aliases — список. Часть ИИ и роли в чатах из prev сохраняются."""
    prev = dict(prev or {})
    out = {k: prev[k] for k in ("ai", "ai_at", "roles", "card", "card_at", "card_url") if prev.get(k)}
    out["name"] = str(body.get("name") or prev.get("name") or "").strip()[:120]
    pr = body.get("priority") if "priority" in body else prev.get("priority")
    if pr in PRIORITIES and pr != "normal":
        out["priority"] = pr
    if "ai" in body:  # окно может поправить часть ИИ руками или очистить её
        v = str(body.get("ai") or "").strip()[:4000]
        if v:
            out["ai"] = v
        else:
            out.pop("ai", None)
            out.pop("ai_at", None)
    if "card" in body:  # карточка из вики/адресной книги (ИИ-описание с источниками) или правка из окна
        v = str(body.get("card") or "").strip()[:3000]
        if v:
            out["card"] = v
        else:
            for k in ("card", "card_at", "card_url"):
                out.pop(k, None)
    if body.get("chat") is not None and ("role" in body or "role_ai" in body):
        roles = dict(out.get("roles") or {})
        r = dict(roles.get(str(body["chat"])) or {})
        if "role" in body:
            r["text"] = str(body.get("role") or "").strip()[:2000]
        if "role_ai" in body:
            r["ai"] = str(body.get("role_ai") or "").strip()[:2000]
        r = {k: v for k, v in r.items() if v}
        if r:
            roles[str(body["chat"])] = r
        else:
            roles.pop(str(body["chat"]), None)
        out["roles"] = roles
    if not out.get("roles"):
        out.pop("roles", None)
    u = str(body.get("username") or prev.get("username") or "").strip().lstrip("@")
    if u:
        out["username"] = u[:64]
    for f in FIELDS:
        v = str(body.get(f) if f in body else prev.get(f) or "").strip()[:3000]
        if v:
            out[f] = v
    al = body.get("aliases") if "aliases" in body else prev.get("aliases")
    if isinstance(al, str):
        al = re.split(r"[,;\n]", al)
    al = [str(a).strip() for a in al or [] if str(a).strip()]
    if al:
        out["aliases"] = al[:12]
    out["updated"] = iso(datetime.now(timezone.utc))
    return out
