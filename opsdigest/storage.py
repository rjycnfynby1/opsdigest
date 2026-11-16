"""Локальная база SQLite: чаты, сообщения (с полнотекстовым поиском), упоминания, сводки, вложения и описания изображений.

Всё хранится только на этом компьютере (data/opsdigest.db). Источник в ключах — на будущее:
'telegram' сейчас, позже 'mail', 'itsm', 'tracker', 'confluence'.

Вложения (таблица media): по одной строке на сообщение с файлом. Изображения и лёгкие документы скачиваются
в папку media/ рабочей папки (status=ok), тяжёлые файлы остаются ссылкой (status=ref) и докачиваются по запросу.
Описания изображений (image_desc) хранятся по SHA-256 содержимого: одинаковая картинка описывается один раз.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    source      TEXT NOT NULL DEFAULT 'telegram',
    chat_id     INTEGER NOT NULL,
    title       TEXT,
    kind        TEXT,          -- private | group | channel | bot
    username    TEXT,
    priority    TEXT,          -- high | normal | low | ignore
    mode        TEXT,          -- chat | incident | alerts
    note        TEXT,
    unread      INTEGER DEFAULT 0,
    muted       INTEGER DEFAULT 0,
    max_msg_id  INTEGER DEFAULT 0,
    min_msg_id  INTEGER DEFAULT 0,
    min_date    TEXT,
    updated_at  TEXT,
    PRIMARY KEY (source, chat_id)
);
CREATE TABLE IF NOT EXISTS messages (
    source      TEXT NOT NULL DEFAULT 'telegram',
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    date        TEXT NOT NULL,   -- ISO UTC
    sender_id   INTEGER,
    sender      TEXT,
    is_out      INTEGER DEFAULT 0,
    mentioned   INTEGER DEFAULT 0,
    reply_to    INTEGER,
    text        TEXT,
    UNIQUE (source, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_date ON messages(date);
CREATE INDEX IF NOT EXISTS ix_messages_chat_date ON messages(source, chat_id, date);
-- запросы окна и задач по чату или автору без source (список чатов, люди, описания, глоссарий): без этих индексов
-- каждый читал всю таблицу, держа замок базы десятки секунд, — окно «теряло связь» (сессия 13)
CREATE INDEX IF NOT EXISTS ix_messages_chat_msg ON messages(chat_id, msg_id);
CREATE INDEX IF NOT EXISTS ix_messages_sender ON messages(sender_id, chat_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, sender, content='messages', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, sender) VALUES (new.rowid, new.text, new.sender);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, sender) VALUES ('delete', old.rowid, old.text, old.sender);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, sender) VALUES ('delete', old.rowid, old.text, old.sender);
    INSERT INTO messages_fts(rowid, text, sender) VALUES (new.rowid, new.text, new.sender);
END;
CREATE TABLE IF NOT EXISTS entities (
    etype   TEXT NOT NULL,       -- incident | task | change | ...
    value   TEXT NOT NULL,       -- INC0012345, PRJ-42
    source  TEXT NOT NULL,
    chat_id INTEGER,
    msg_id  INTEGER,
    date    TEXT,
    UNIQUE (etype, value, source, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_entities_value ON entities(value);
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT,
    since       TEXT,
    until       TEXT,
    created_at  TEXT,
    provider    TEXT,
    data        TEXT,           -- JSON сводки
    html_path   TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL DEFAULT 'telegram',
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    date        TEXT,
    kind        TEXT,            -- photo | image | document | video | gif | voice | audio | video_note | sticker | other
    mime        TEXT,
    name        TEXT,
    size        INTEGER,
    width       INTEGER,
    height      INTEGER,
    duration    INTEGER,
    status      TEXT DEFAULT 'pending',  -- pending (скачать) | ok (скачано) | ref (тяжёлый, только ссылка) | failed | gone
    path        TEXT,            -- относительно рабочей папки (media/…)
    sha256      TEXT,
    desc_state  TEXT,            -- '' | failed | unsupported (описание само — в image_desc по sha256)
    error       TEXT,
    updated_at  TEXT,
    UNIQUE (source, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_media_chat_date ON media(source, chat_id, date);
CREATE INDEX IF NOT EXISTS ix_media_sha ON media(sha256);
CREATE INDEX IF NOT EXISTS ix_media_status ON media(status, kind);
CREATE TABLE IF NOT EXISTS image_desc (
    sha256      TEXT PRIMARY KEY,
    kind        TEXT,            -- скриншот, фото, схема, график, документ…
    summary     TEXT,            -- одна фраза
    details     TEXT,            -- подробнее
    text        TEXT,            -- значимый текст с изображения
    importance  INTEGER,
    model       TEXT,
    created_at  TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(summary, details, text, name, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS topics (
    chat_id     INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL,      -- номер темы форума (1 — «Общая»)
    title       TEXT,
    icon        TEXT,                  -- эмодзи (если известно)
    color       INTEGER,
    closed      INTEGER DEFAULT 0,
    pinned      INTEGER DEFAULT 0,
    hidden      INTEGER DEFAULT 0,
    top_msg     INTEGER DEFAULT 0,
    top_date    TEXT,
    unread      INTEGER DEFAULT 0,
    mentions    INTEGER DEFAULT 0,
    read_in     INTEGER DEFAULT 0,
    muted       INTEGER DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (chat_id, topic_id)
);
CREATE TABLE IF NOT EXISTS members (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT,
    username   TEXT,
    role       TEXT,
    updated_at TEXT,
    PRIMARY KEY (chat_id, user_id)
);
"""

IMAGE_KINDS = ("photo", "image")
CHAT_COLUMNS = {"history_done": "INTEGER DEFAULT 0", "history_at": "TEXT",
                # для мессенджера: закреплён, в архиве, докуда прочитано (входящие/исходящие), упоминания, контакт
                "pinned": "INTEGER DEFAULT 0", "archived": "INTEGER DEFAULT 0", "read_in": "INTEGER DEFAULT 0",
                "read_out": "INTEGER DEFAULT 0", "mentions": "INTEGER DEFAULT 0", "contact": "INTEGER DEFAULT 0",
                "top_date": "TEXT", "forum": "INTEGER DEFAULT 0",
                "photo": "TEXT"}   # номер фото профиля чата ('' — фото нет, NULL — не знаем); аватарки — avatars.py
# meta — JSON для показа в мессенджере: форматирование, пересылка, правка, альбом, реакции, кнопки-ссылки
# topic — тема форума (NULL — обычный чат или «Общая» тема)
MESSAGE_COLUMNS = {"meta": "TEXT", "topic": "INTEGER"}


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass
class Chat:
    chat_id: int
    title: str
    kind: str
    username: str | None = None
    priority: str = "normal"
    mode: str = "chat"
    note: str = ""
    unread: int = 0
    muted: bool = False
    source: str = "telegram"
    max_msg_id: int = 0
    min_msg_id: int = 0
    min_date: str | None = None


@dataclass
class Message:
    chat_id: int
    msg_id: int
    date: str
    sender: str
    text: str
    sender_id: int | None = None
    is_out: bool = False
    mentioned: bool = False
    reply_to: int | None = None
    source: str = "telegram"
    extra: dict = field(default_factory=dict)
    meta: dict | None = None
    topic: int | None = None


def db_exists(path: str | Path) -> bool:
    """Есть ли база (открытая или зашифрованная data/opsdigest.db.odx)."""
    from .secure.memdb import exists
    return exists(path)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.shared = False
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            from .secure import memdb, vault
            if vault.protected(self.path):  # защита включена: база в памяти, на диске — зашифрованный снимок
                self.db = memdb.connect(self.path)
                self.shared = True
                self.db.executescript(SCHEMA)
                self._migrate()
                return
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        self.db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            try:  # выгрузка истории и сводка могут писать одновременно — журнал WAL не блокирует чтение
                self.db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, cols in (("chats", CHAT_COLUMNS), ("messages", MESSAGE_COLUMNS)):  # миграция старых баз
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.db.commit()

    def close(self) -> None:
        if not self.shared:  # общая зашифрованная база живёт до блокировки/выхода
            self.db.close()

    # ---------------------------------------------------------------- чаты
    def upsert_chat(self, c: Chat) -> None:
        now = iso(datetime.now(timezone.utc))
        self.db.execute(
            """INSERT INTO chats(source, chat_id, title, kind, username, priority, mode, note, unread, muted, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source, chat_id) DO UPDATE SET
                 title=excluded.title, kind=excluded.kind, username=excluded.username,
                 priority=excluded.priority, mode=excluded.mode, note=excluded.note,
                 unread=excluded.unread, muted=excluded.muted, updated_at=excluded.updated_at""",
            (c.source, c.chat_id, c.title, c.kind, c.username, c.priority, c.mode, c.note,
             c.unread, int(c.muted), now),
        )
        self.db.commit()

    def get_chat(self, chat_id: int, source: str = "telegram") -> Chat | None:
        r = self.db.execute("SELECT * FROM chats WHERE source=? AND chat_id=?", (source, chat_id)).fetchone()
        return self._chat(r) if r else None

    def chats(self, source: str = "telegram") -> list[Chat]:
        return [self._chat(r) for r in self.db.execute("SELECT * FROM chats WHERE source=? ORDER BY title", (source,))]

    @staticmethod
    def _chat(r: sqlite3.Row) -> Chat:
        return Chat(chat_id=r["chat_id"], title=r["title"] or "", kind=r["kind"] or "", username=r["username"],
                    priority=r["priority"] or "normal", mode=r["mode"] or "chat", note=r["note"] or "",
                    unread=r["unread"] or 0, muted=bool(r["muted"]), source=r["source"],
                    max_msg_id=r["max_msg_id"] or 0, min_msg_id=r["min_msg_id"] or 0, min_date=r["min_date"])

    def set_chat_range(self, chat_id: int, max_id: int, min_id: int, min_date: str | None,
                       source: str = "telegram") -> None:
        self.db.execute("UPDATE chats SET max_msg_id=?, min_msg_id=?, min_date=? WHERE source=? AND chat_id=?",
                        (max_id, min_id, min_date, source, chat_id))
        self.db.commit()

    def set_chat_rule(self, chat_id: int, priority: str, mode: str, note: str, source: str = "telegram") -> None:
        self.db.execute("UPDATE chats SET priority=?, mode=?, note=? WHERE source=? AND chat_id=?",
                        (priority, mode, note, source, chat_id))

    def set_history_done(self, chat_id: int, done: bool = True, source: str = "telegram") -> None:
        self.db.execute("UPDATE chats SET history_done=?, history_at=? WHERE source=? AND chat_id=?",
                        (int(done), iso(datetime.now(timezone.utc)), source, chat_id))
        self.db.commit()

    def history_done(self, chat_id: int, source: str = "telegram") -> bool:
        r = self.db.execute("SELECT history_done FROM chats WHERE source=? AND chat_id=?", (source, chat_id)).fetchone()
        return bool(r and r["history_done"])

    def chat_overview(self, source: str = "telegram") -> list[dict]:
        """Все чаты из базы со счётчиками: сообщения, изображения (скачано / описано), файлы-ссылки."""
        rows = self.db.execute(
            """SELECT c.*,
                 (SELECT COUNT(*) FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id) AS n_msgs,
                 (SELECT MAX(date) FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id) AS last_date,
                 (SELECT COUNT(*) FROM media x WHERE x.source=c.source AND x.chat_id=c.chat_id
                     AND x.kind IN ('photo','image')) AS n_img,
                 (SELECT COUNT(*) FROM media x WHERE x.source=c.source AND x.chat_id=c.chat_id
                     AND x.kind IN ('photo','image') AND x.status='ok') AS n_img_ok,
                 (SELECT COUNT(*) FROM media x JOIN image_desc d ON d.sha256=x.sha256
                     WHERE x.source=c.source AND x.chat_id=c.chat_id AND x.kind IN ('photo','image')) AS n_img_desc,
                 (SELECT COUNT(*) FROM media x WHERE x.source=c.source AND x.chat_id=c.chat_id
                     AND x.status='ref') AS n_ref
               FROM chats c WHERE c.source=? ORDER BY last_date DESC""", (source,)).fetchall()
        out = []
        for r in rows:
            c = self._chat(r)
            out.append({"id": c.chat_id, "title": c.title, "kind": c.kind, "username": c.username,
                        "priority": c.priority, "mode": c.mode, "unread": c.unread, "muted": c.muted,
                        "messages": r["n_msgs"], "last": r["last_date"] or "", "images": r["n_img"],
                        "images_ok": r["n_img_ok"], "images_described": r["n_img_desc"], "refs": r["n_ref"],
                        "history_done": bool(r["history_done"]), "from": c.min_date or ""})
        return out

    # ---------------------------------------------------------------- сообщения
    def add_messages(self, msgs: Iterable[Message]) -> int:
        n = 0
        for m in msgs:
            meta = json.dumps(m.meta, ensure_ascii=False, separators=(",", ":")) if m.meta else None
            cur = self.db.execute(
                """INSERT INTO messages(source, chat_id, msg_id, date, sender_id, sender, is_out, mentioned, reply_to,
                                        text, meta, topic)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, chat_id, msg_id) DO UPDATE SET
                     text=excluded.text, sender=excluded.sender, mentioned=excluded.mentioned,
                     meta=COALESCE(excluded.meta, messages.meta), topic=COALESCE(excluded.topic, messages.topic)
                   WHERE messages.text IS NOT excluded.text
                      OR (excluded.meta IS NOT NULL AND messages.meta IS NOT excluded.meta)
                      OR (excluded.topic IS NOT NULL AND messages.topic IS NOT excluded.topic)""",
                (m.source, m.chat_id, m.msg_id, m.date, m.sender_id, m.sender, int(m.is_out), int(m.mentioned),
                 m.reply_to, m.text, meta, m.topic),
            )
            n += cur.rowcount if cur.rowcount > 0 else 0
        self.db.commit()
        return n

    # ---------------------------------------------------------------- мессенджер
    def set_chat_state(self, chat_id: int, source: str = "telegram", **fields) -> None:
        """Состояние диалога из Telegram (непрочитано, закреплён, архив, докуда прочитано…)."""
        allowed = {"unread", "muted", "pinned", "archived", "read_in", "read_out", "mentions", "contact", "top_date",
                   "title", "forum", "photo"}
        sets = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        self.db.execute(f"UPDATE chats SET {cols} WHERE source=? AND chat_id=?", (*sets.values(), source, chat_id))
        self.db.commit()

    def chat_list(self, source: str = "telegram", chat_id: int | None = None) -> list[sqlite3.Row]:
        """Все чаты (или один) с последним сообщением — для списка чатов мессенджера."""
        one = " AND c.chat_id=?" if chat_id is not None else ""
        return self.db.execute(
            """SELECT c.*, m.msg_id AS l_id, m.date AS l_date, m.text AS l_text, m.sender AS l_sender,
                      m.is_out AS l_out, m.sender_id AS l_sender_id
               FROM chats c LEFT JOIN messages m ON m.rowid = (
                   SELECT rowid FROM messages x WHERE x.source=c.source AND x.chat_id=c.chat_id
                   ORDER BY x.date DESC, x.msg_id DESC LIMIT 1)
               WHERE c.source=?""" + one, (source,) + ((chat_id,) if chat_id is not None else ())).fetchall()

    @staticmethod
    def topic_sql(topic: int | None) -> tuple[str, tuple]:
        """Условие «сообщения темы»: тема 1 («Общая») — это и сообщения без темы."""
        if topic is None:
            return "", ()
        if int(topic) == 1:
            return " AND (topic IS NULL OR topic=1)", ()
        return " AND topic=?", (int(topic),)

    def page(self, chat_id: int, before: int | None = None, limit: int = 50, after: int | None = None,
             source: str = "telegram", topic: int | None = None) -> list[sqlite3.Row]:
        """Порция сообщений чата (или темы форума) по номерам: старше before (по убыванию) или новее after."""
        t, ta = self.topic_sql(topic)
        if after is not None:
            return self.db.execute(f"""SELECT * FROM messages WHERE source=? AND chat_id=? AND msg_id>?{t}
                                      ORDER BY msg_id LIMIT ?""", (source, chat_id, after, *ta, limit)).fetchall()
        return self.db.execute(f"""SELECT * FROM messages WHERE source=? AND chat_id=? AND msg_id<?{t}
                                  ORDER BY msg_id DESC LIMIT ?""",
                               (source, chat_id, before or 2**62, *ta, limit)).fetchall()

    # ---------------------------------------------------------------- темы форумов
    def upsert_topics(self, chat_id: int, rows: list[dict]) -> None:
        now = iso(datetime.now(timezone.utc))
        for r in rows:
            self.db.execute(
                """INSERT INTO topics(chat_id, topic_id, title, icon, color, closed, pinned, hidden, top_msg, top_date,
                                      unread, mentions, read_in, muted, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(chat_id, topic_id) DO UPDATE SET title=excluded.title, icon=COALESCE(excluded.icon, icon),
                     color=excluded.color, closed=excluded.closed, pinned=excluded.pinned, hidden=excluded.hidden,
                     top_msg=excluded.top_msg, top_date=excluded.top_date, unread=excluded.unread,
                     mentions=excluded.mentions, read_in=excluded.read_in, muted=excluded.muted,
                     updated_at=excluded.updated_at""",
                (chat_id, int(r["id"]), r.get("title") or "", r.get("icon"), r.get("color"), int(bool(r.get("closed"))),
                 int(bool(r.get("pinned"))), int(bool(r.get("hidden"))), r.get("top_msg") or 0, r.get("top_date"),
                 r.get("unread") or 0, r.get("mentions") or 0, r.get("read_in") or 0, int(bool(r.get("muted"))), now))
        self.db.commit()

    def set_topic_state(self, chat_id: int, topic_id: int, **fields) -> None:
        allowed = {"unread", "mentions", "read_in", "muted", "top_msg", "top_date", "title", "closed", "pinned"}
        sets = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        self.db.execute(f"UPDATE topics SET {cols} WHERE chat_id=? AND topic_id=?", (*sets.values(), chat_id, topic_id))
        self.db.commit()

    def topics(self, chat_id: int) -> list[dict]:
        """Темы форума (из Telegram; если список не загружали — по сообщениям в базе) со счётчиками."""
        rows = {r["topic_id"]: dict(r) for r in self.db.execute("SELECT * FROM topics WHERE chat_id=?", (chat_id,))}
        for r in self.db.execute("""SELECT COALESCE(topic, 1) AS t, COUNT(*) AS n, MAX(date) AS last, MAX(msg_id) AS mx
                                    FROM messages WHERE chat_id=? GROUP BY COALESCE(topic, 1)""", (chat_id,)):
            x = rows.setdefault(r["t"], {"chat_id": chat_id, "topic_id": r["t"], "title": "" if r["t"] != 1 else "Общая",
                                         "unread": 0, "pinned": 0, "closed": 0, "muted": 0, "top_date": None})
            x["messages"], x["last"] = r["n"], r["last"]
        out = []
        for x in rows.values():
            x.setdefault("messages", 0)
            x.setdefault("last", None)
            if not x.get("title"):
                x["title"] = "Общая" if x["topic_id"] == 1 else f"Тема {x['topic_id']}"
            out.append(x)
        out.sort(key=lambda x: max(str(x.get("top_date") or ""), str(x.get("last") or "")), reverse=True)
        out.sort(key=lambda x: not x.get("pinned"))  # закреплённые — сверху (сортировка устойчивая)
        return out

    def rows_by_ids(self, chat_id: int, ids: Iterable[int], source: str = "telegram") -> list[sqlite3.Row]:
        ids = list(dict.fromkeys(int(i) for i in ids))
        out = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            out += self.db.execute(f"""SELECT * FROM messages WHERE source=? AND chat_id=? AND msg_id IN
                                       ({",".join("?" * len(part))})""", (source, chat_id, *part)).fetchall()
        return out

    def delete_messages(self, chat_id: int | None, ids: Iterable[int], source: str = "telegram") -> list[tuple[int, int]]:
        """Удалить сообщения (удалены в Telegram). chat_id=None — в личных чатах и обычных группах, где номера
        сообщений общие на весь аккаунт. Возвращает [(chat_id, msg_id)] удалённых."""
        ids = [int(i) for i in ids]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        if chat_id is None:
            rows = self.db.execute(f"""SELECT chat_id, msg_id FROM messages WHERE source=? AND msg_id IN ({q})
                                       AND chat_id > -1000000000000""", (source, *ids)).fetchall()
        else:
            rows = self.db.execute(f"""SELECT chat_id, msg_id FROM messages WHERE source=? AND chat_id=?
                                       AND msg_id IN ({q})""", (source, chat_id, *ids)).fetchall()
        gone = [(r["chat_id"], r["msg_id"]) for r in rows]
        for cid, mid in gone:
            self.db.execute("DELETE FROM messages WHERE source=? AND chat_id=? AND msg_id=?", (source, cid, mid))
            self.db.execute("DELETE FROM media WHERE source=? AND chat_id=? AND msg_id=?", (source, cid, mid))
            self.db.execute("DELETE FROM entities WHERE source=? AND chat_id=? AND msg_id=?", (source, cid, mid))
        self.db.commit()
        return gone

    def bump_unread(self, chat_id: int, mentioned: bool = False, date: str | None = None,
                    source: str = "telegram") -> None:
        self.db.execute("""UPDATE chats SET unread=COALESCE(unread,0)+1, mentions=COALESCE(mentions,0)+?,
                           top_date=COALESCE(?, top_date) WHERE source=? AND chat_id=?""",
                        (int(mentioned), date, source, chat_id))
        self.db.commit()

    def bounds(self, chat_id: int, source: str = "telegram") -> tuple[int, int, str | None]:
        """(самый старый номер, самый новый номер, дата самого старого) сообщений чата в базе."""
        r = self.db.execute("""SELECT MIN(msg_id), MAX(msg_id), MIN(date) FROM messages WHERE source=? AND chat_id=?""",
                            (source, chat_id)).fetchone()
        return int(r[0] or 0), int(r[1] or 0), r[2]

    def max_ids(self, source: str = "telegram") -> dict[int, int]:
        return {r[0]: r[1] or 0 for r in self.db.execute(
            "SELECT chat_id, MAX(msg_id) FROM messages WHERE source=? GROUP BY chat_id", (source,)).fetchall()}

    def unread_after(self, chat_id: int, max_id: int, source: str = "telegram") -> int:
        r = self.db.execute("""SELECT COUNT(*) FROM messages WHERE source=? AND chat_id=? AND msg_id>? AND is_out=0""",
                            (source, chat_id, max_id)).fetchone()
        return int(r[0] or 0)

    def messages(self, chat_id: int, since: str, until: str, source: str = "telegram",
                 topic: int | None = None) -> list[Message]:
        t, ta = self.topic_sql(topic)
        rows = self.db.execute(
            f"""SELECT * FROM messages WHERE source=? AND chat_id=? AND date>=? AND date<?{t} ORDER BY date, msg_id""",
            (source, chat_id, since, until, *ta))
        return [self._msg(r) for r in rows]

    def active_chats(self, since: str, until: str, source: str = "telegram",
                     include_ignored: bool = False) -> list[tuple[Chat, int]]:
        """Чаты с сообщениями за период (кроме ignore, если не include_ignored) и число сообщений."""
        excl = "" if include_ignored else " AND COALESCE(c.priority,'normal')!='ignore'"
        rows = self.db.execute(
            f"""SELECT c.*, COUNT(m.msg_id) AS n FROM chats c
               JOIN messages m ON m.source=c.source AND m.chat_id=c.chat_id
               WHERE c.source=? AND m.date>=? AND m.date<?{excl}
               GROUP BY c.source, c.chat_id ORDER BY n DESC""", (source, since, until))
        return [(self._chat(r), r["n"]) for r in rows]

    def period_counts(self, since: str, until: str, source: str = "telegram") -> dict[int, int]:
        """Сколько сообщений за период в каждом чате (по индексу даты, без чтения текстов)."""
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT chat_id, COUNT(*) FROM messages WHERE source=? AND date>=? AND date<? GROUP BY chat_id",
            (source, since, until))}

    def search(self, query: str, since: str | None = None, limit: int = 60,
               include_excluded: bool = False, chat_id: int | None = None) -> list[tuple[Message, str]]:
        """Полнотекстовый поиск по тексту сообщений и по описаниям изображений и именам файлов.

        Возвращает (сообщение, название чата), лучшие совпадения первыми. Исключённые чаты (priority=ignore) — нет."""
        excl = "" if include_excluded else " AND COALESCE(c.priority,'normal')!='ignore'"
        date = " AND m.date>=?" if since else ""
        if chat_id is not None:
            date += f" AND m.chat_id={int(chat_id)}"
        sql = f"""SELECT m.*, c.title AS chat_title, bm25(messages_fts) AS score FROM messages_fts f
                 JOIN messages m ON m.rowid=f.rowid
                 LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id
                 WHERE messages_fts MATCH ?{excl}{date} ORDER BY score LIMIT ?"""
        sql2 = f"""SELECT m.*, c.title AS chat_title, bm25(media_fts) AS score FROM media_fts f
                 JOIN media x ON x.id=f.rowid
                 JOIN messages m ON m.source=x.source AND m.chat_id=x.chat_id AND m.msg_id=x.msg_id
                 LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id
                 WHERE media_fts MATCH ?{excl}{date} ORDER BY score LIMIT ?"""
        args: list = [query] + ([since] if since else []) + [limit]
        rows: list = []
        for q in (sql, sql2):
            try:
                rows += self.db.execute(q, args).fetchall()
            except sqlite3.OperationalError:
                continue
        rows.sort(key=lambda r: r["score"])
        out, seen = [], set()
        for r in rows:
            k = (r["source"], r["chat_id"], r["msg_id"])
            if k in seen:
                continue
            seen.add(k)
            out.append((self._msg(r), r["chat_title"] or ""))
        return out[:limit]

    def context(self, chat_id: int, msg_id: int, before: int = 3, after: int = 3,
                source: str = "telegram") -> list[Message]:
        a = self.db.execute("""SELECT * FROM messages WHERE source=? AND chat_id=? AND msg_id<? ORDER BY msg_id DESC LIMIT ?""",
                            (source, chat_id, msg_id, before)).fetchall()
        b = self.db.execute("""SELECT * FROM messages WHERE source=? AND chat_id=? AND msg_id>=? ORDER BY msg_id LIMIT ?""",
                            (source, chat_id, msg_id, after + 1)).fetchall()
        return [self._msg(r) for r in list(reversed(a)) + list(b)]

    @staticmethod
    def _msg(r: sqlite3.Row) -> Message:
        keys = r.keys()
        return Message(chat_id=r["chat_id"], msg_id=r["msg_id"], date=r["date"], sender=r["sender"] or "",
                       text=r["text"] or "", sender_id=r["sender_id"], is_out=bool(r["is_out"]),
                       mentioned=bool(r["mentioned"]), reply_to=r["reply_to"], source=r["source"],
                       topic=r["topic"] if "topic" in keys else None)

    @staticmethod
    def meta_of(r: sqlite3.Row) -> dict:
        try:
            return json.loads(r["meta"]) if r["meta"] else {}
        except (ValueError, IndexError, KeyError):
            return {}

    # ---------------------------------------------------------------- вложения
    def add_media(self, rows: Iterable[dict]) -> int:
        """Зарегистрировать вложения сообщений (без скачивания). Уже известные — не трогаем, кроме метаданных."""
        now = iso(datetime.now(timezone.utc))
        n = 0
        for r in rows:
            cur = self.db.execute(
                """INSERT INTO media(source, chat_id, msg_id, date, kind, mime, name, size, width, height, duration,
                                     status, path, sha256, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, chat_id, msg_id) DO UPDATE SET
                     kind=excluded.kind, mime=COALESCE(excluded.mime, media.mime), name=COALESCE(excluded.name, media.name),
                     size=COALESCE(excluded.size, media.size), width=COALESCE(excluded.width, media.width),
                     height=COALESCE(excluded.height, media.height),
                     status=CASE WHEN media.status IN ('ok') THEN media.status
                                 WHEN excluded.status='ok' THEN 'ok'
                                 WHEN media.status IN ('failed','gone') THEN media.status
                                 ELSE excluded.status END,
                     path=CASE WHEN media.status='ok' THEN media.path ELSE COALESCE(excluded.path, media.path) END,
                     sha256=CASE WHEN media.status='ok' THEN media.sha256 ELSE COALESCE(excluded.sha256, media.sha256) END""",
                (r.get("source", "telegram"), r["chat_id"], r["msg_id"], r.get("date"), r.get("kind"), r.get("mime"),
                 r.get("name"), r.get("size"), r.get("width"), r.get("height"), r.get("duration"),
                 r.get("status") or "pending", r.get("path"), r.get("sha256"), now))
            n += cur.rowcount if cur.rowcount > 0 else 0
            mid = self.db.execute("SELECT id FROM media WHERE source=? AND chat_id=? AND msg_id=?",
                                  (r.get("source", "telegram"), r["chat_id"], r["msg_id"])).fetchone()["id"]
            if r.get("name") and not self.db.execute("SELECT 1 FROM media_fts WHERE rowid=?", (mid,)).fetchone():
                self.db.execute("INSERT INTO media_fts(rowid, summary, details, text, name) VALUES (?,?,?,?,?)",
                                (mid, "", "", "", r["name"]))
        self.db.commit()
        return n

    @staticmethod
    def _media(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("d_kind", "summary", "details", "text", "importance", "model", "d_created"):
            d.setdefault(k, None)
        d["described"] = bool(d.get("summary") or d.get("details") or d.get("text"))
        return d

    _MEDIA_SQL = """SELECT x.*, d.kind AS d_kind, d.summary, d.details, d.text, d.importance, d.model,
                           d.created_at AS d_created, c.title AS chat_title
                    FROM media x LEFT JOIN image_desc d ON d.sha256=x.sha256
                    LEFT JOIN chats c ON c.source=x.source AND c.chat_id=x.chat_id"""

    def get_media(self, media_id: int) -> dict | None:
        r = self.db.execute(self._MEDIA_SQL + " WHERE x.id=?", (media_id,)).fetchone()
        return self._media(r) if r else None

    def media_of(self, chat_id: int, msg_ids: Iterable[int], source: str = "telegram") -> dict[int, dict]:
        ids = list(msg_ids)
        out: dict[int, dict] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            q = self._MEDIA_SQL + f" WHERE x.source=? AND x.chat_id=? AND x.msg_id IN ({','.join('?' * len(part))})"
            for r in self.db.execute(q, (source, chat_id, *part)):
                out[r["msg_id"]] = self._media(r)
        return out

    def media_list(self, *, chat_ids: Iterable[int] | None = None, since: str | None = None,
                   until: str | None = None, kinds: Iterable[str] | None = IMAGE_KINDS,
                   status: Iterable[str] | None = None, undescribed: bool = False, limit: int | None = None,
                   include_excluded: bool = False, source: str = "telegram") -> list[dict]:
        """Вложения по условиям, от старых к новым. undescribed — изображения без описания (и не помеченные как
        неподдерживаемые)."""
        q = self._MEDIA_SQL + " WHERE x.source=?"
        args: list = [source]
        if chat_ids is not None:
            ids = list(chat_ids)
            if not ids:
                return []
            q += f" AND x.chat_id IN ({','.join('?' * len(ids))})"
            args += ids
        elif not include_excluded:
            q += " AND COALESCE(c.priority,'normal')!='ignore'"
        if since:
            q += " AND x.date>=?"
            args.append(since)
        if until:
            q += " AND x.date<?"
            args.append(until)
        if kinds:
            ks = list(kinds)
            q += f" AND x.kind IN ({','.join('?' * len(ks))})"
            args += ks
        if status:
            st = list(status)
            q += f" AND x.status IN ({','.join('?' * len(st))})"
            args += st
        if undescribed:
            q += " AND d.sha256 IS NULL AND COALESCE(x.desc_state,'')!='unsupported'"
        q += " ORDER BY x.date, x.id"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [self._media(r) for r in self.db.execute(q, args)]

    def set_media_file(self, media_id: int, path: str, sha256: str, size: int | None = None,
                       mime: str | None = None) -> None:
        self.db.execute("""UPDATE media SET status='ok', path=?, sha256=?, size=COALESCE(?, size), mime=COALESCE(?, mime),
                           error=NULL, updated_at=? WHERE id=?""",
                        (path, sha256, size, mime, iso(datetime.now(timezone.utc)), media_id))
        self.db.commit()
        if self.description(sha256):
            self._index_media(sha256)

    def set_media_status(self, media_id: int, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE media SET status=?, error=?, updated_at=? WHERE id=?",
                        (status, (error or "")[:300] or None, iso(datetime.now(timezone.utc)), media_id))
        self.db.commit()

    def set_desc_state(self, media_id: int, state: str, error: str | None = None) -> None:
        self.db.execute("UPDATE media SET desc_state=?, error=COALESCE(?, error) WHERE id=?",
                        (state, (error or "")[:300] or None, media_id))
        self.db.commit()

    def description(self, sha256: str | None) -> dict | None:
        if not sha256:
            return None
        r = self.db.execute("SELECT * FROM image_desc WHERE sha256=?", (sha256,)).fetchone()
        return dict(r) if r else None

    def save_description(self, sha256: str, d: dict) -> None:
        self.db.execute(
            """INSERT INTO image_desc(sha256, kind, summary, details, text, importance, model, created_at)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET kind=excluded.kind, summary=excluded.summary,
               details=excluded.details, text=excluded.text, importance=excluded.importance, model=excluded.model,
               created_at=excluded.created_at""",
            (sha256, d.get("kind"), d.get("summary"), d.get("details"), d.get("text"), d.get("importance"),
             d.get("model"), d.get("created_at") or iso(datetime.now(timezone.utc))))
        self.db.execute("UPDATE media SET desc_state='' WHERE sha256=?", (sha256,))
        self._index_media(sha256)
        self.db.commit()

    def _index_media(self, sha256: str) -> None:
        """Описание изображения — в поиск для всех сообщений с этой картинкой."""
        d = self.description(sha256) or {}
        for r in self.db.execute("SELECT id, name FROM media WHERE sha256=?", (sha256,)).fetchall():
            self.db.execute("DELETE FROM media_fts WHERE rowid=?", (r["id"],))
            self.db.execute("INSERT INTO media_fts(rowid, summary, details, text, name) VALUES (?,?,?,?,?)",
                            (r["id"], d.get("summary") or "", d.get("details") or "", d.get("text") or "",
                             r["name"] or ""))
        self.db.commit()

    def media_stats(self, chat_id: int | None = None, source: str = "telegram") -> dict:
        w, args = "WHERE x.source=?", [source]
        if chat_id is not None:
            w += " AND x.chat_id=?"
            args.append(chat_id)
        r = self.db.execute(
            f"""SELECT SUM(x.kind IN ('photo','image')) AS images,
                       SUM(x.kind IN ('photo','image') AND x.status='ok') AS images_ok,
                       SUM(x.kind IN ('photo','image') AND x.status='pending') AS images_pending,
                       SUM(x.kind IN ('photo','image') AND d.sha256 IS NOT NULL) AS described,
                       SUM(x.status='ref') AS refs,
                       SUM(x.kind NOT IN ('photo','image') AND x.status='ok') AS files_ok,
                       COUNT(DISTINCT x.sha256) AS unique_files
                FROM media x LEFT JOIN image_desc d ON d.sha256=x.sha256 {w}""", args).fetchone()
        return {k: int(r[k] or 0) for k in r.keys()}

    # ---------------------------------------------------------------- упоминания
    def add_entities(self, rows: Iterable[tuple[str, str, str, int, int, str]]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO entities(etype, value, source, chat_id, msg_id, date) VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()

    def entities_between(self, since: str, until: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT e.*, c.title AS chat_title, c.username AS chat_username, c.kind AS chat_kind
               FROM entities e LEFT JOIN chats c ON c.source=e.source AND c.chat_id=e.chat_id
               WHERE e.date>=? AND e.date<? ORDER BY e.value, e.date""", (since, until)).fetchall()

    def entity_history(self, value: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT e.*, c.title AS chat_title FROM entities e
               LEFT JOIN chats c ON c.source=e.source AND c.chat_id=e.chat_id
               WHERE e.value=? ORDER BY e.date""", (value,)).fetchall()

    # ---------------------------------------------------------------- сводки
    def save_digest(self, period: str, since: str, until: str, provider: str, data: dict, html_path: str) -> int:
        cur = self.db.execute(
            "INSERT INTO digests(period, since, until, created_at, provider, data, html_path) VALUES (?,?,?,?,?,?,?)",
            (period, since, until, iso(datetime.now(timezone.utc)), provider, json.dumps(data, ensure_ascii=False),
             html_path))
        self.db.commit()
        return int(cur.lastrowid)

    def last_digest(self, period: str | None = None) -> dict | None:
        sql = "SELECT * FROM digests"
        args: tuple = ()
        if period:
            sql += " WHERE period=?"
            args = (period,)
        r = self.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if not r:
            return None
        d = json.loads(r["data"])
        d.setdefault("meta", {})["html_path"] = r["html_path"]
        return d

    # ---------------------------------------------------------------- kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))
        self.db.commit()
