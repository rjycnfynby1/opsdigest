"""Навык OpsDigest: то, чему пользователь научил программу, — как SKILL.md в Claude Code, только живой.

Состоит из четырёх частей (всё в базе — шифруется вместе с ней; выгружается файлом JSON и читаемым SKILL.md):
  * правила и поправки (kb_rules) — «что я понял из отзывов и указаний»: как различать инцидент-чат и обращение
    ITSM, что считать проблемой, как писать сводку, где искать. У правила есть области (сводка, ассистент, поиск в
    источниках, описания, глоссарий, изображения) и слова-признаки: правило попадает в запрос модели, только когда
    оно к нему относится (или всегда — если признаков нет или правило закреплено);
  * глоссарий (glossary.py) — термины и их значения; навык и глоссарий пополняют друг друга: поправка про термин
    становится статьёй глоссария (подтверждённой), а глоссарий подставляется вместе с правилами;
  * опорные ссылки (kb_links) — страницы вики, с которых начинать поиск (заполняет пользователь), — уходят в каждый
    запрос к плагинам Maverick;
  * отзывы (feedback) — на любой результат (отчёт, ответ, поиск, описание чата/темы/человека): модель превращает
    отзыв в изменения навыка; если отзыв неясен — задаёт вопрос в ассистенте (беседа «отзыв»), ответ — снова в навык.

Изменения данных (правила, термины, описания чатов, тем и людей, ссылки) делаются «действиями» — общий формат для
отзывов и для указаний в ассистенте («всем чатам с „авария“ в названии поставь режим инцидент»). Действие сначала
разрешается (resolve: какие чаты/люди попадут), потом применяется (apply) с записью в журнал — любое можно отменить.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from . import logs
from .storage import Store, iso

log = logs.get("skill")
Progress = Callable[[str], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    scope       TEXT,                    -- JSON: ["digest", "assistant", "search", "describe", "glossary", "vision", "all"]
    keys        TEXT,                    -- JSON: слова-признаки; пусто — всегда
    kind        TEXT DEFAULT 'rule',     -- rule | correction | instruction | style
    pinned      INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    source      TEXT,                    -- user | feedback | assistant | import
    ref         TEXT,                    -- JSON: откуда (отзыв, беседа)
    hits        INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS kb_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    url         TEXT,
    note        TEXT,                    -- для чего: «список чатов дежурной смены», «регламент инцидентов»
    keys        TEXT,                    -- JSON: слова-признаки
    scope       TEXT,                    -- JSON: источники/области (пусто — везде)
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT,                    -- report | answer | search | chat_ai | describe_chat | describe_topic | describe_person | glossary | image
    ref         TEXT,                    -- JSON: {report, thread, msg, chat, topic, person, term}
    rating      INTEGER DEFAULT 0,       -- 1 хорошо, -1 плохо, 0 без оценки
    text        TEXT,
    excerpt     TEXT,                    -- что было в результате (кусок — для модели)
    status      TEXT DEFAULT 'new',      -- new | done | question | failed | pending
    result      TEXT,                    -- JSON: {reply, actions, journal, question, options}
    thread      INTEGER,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS kb_journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op          TEXT,
    summary     TEXT,
    data        TEXT,                    -- JSON: {before: …, after: …} — для отмены
    source      TEXT,
    ref         TEXT,
    undone      INTEGER DEFAULT 0,
    created_at  TEXT
);
"""

SCOPES = {"digest": "сводки", "assistant": "ассистент и ИИ в чате", "search": "поиск в источниках (Maverick)",
          "describe": "описания чатов, тем и людей", "glossary": "глоссарий", "vision": "описание изображений",
          "all": "везде"}
TARGETS = {"report": "отчёт-сводка", "answer": "ответ ассистента", "search": "поиск везде", "chat_ai": "ИИ в чате",
           "describe_chat": "описание чата", "describe_topic": "описание темы", "describe_person": "описание человека",
           "glossary": "статья глоссария", "image": "описание изображения", "other": "результат"}
TARGET_SCOPE = {"report": "digest", "answer": "assistant", "search": "search", "chat_ai": "assistant",
                "describe_chat": "describe", "describe_topic": "describe", "describe_person": "describe",
                "glossary": "glossary", "image": "vision", "other": "all"}
KNOWLEDGE_OPS = {"rule.add", "rule.update", "rule.off", "term.upsert", "term.hide", "link.add"}
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_\-]{1,}")


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()


def _now() -> str:
    return iso(datetime.now(timezone.utc))


def _j(x, default):
    if x is None or x == "":
        return default
    try:
        v = json.loads(x) if isinstance(x, str) else x
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _stem(w: str) -> str:
    w = w.lower().replace("ё", "е")
    return re.sub(r"(ами|ями|ого|его|ому|ему|ыми|ими|ах|ях|ов|ев|ей|ам|ям|ом|ем|ой|ий|ый|ая|яя|ое|ее|ые|ие|а|я|о|е|ы|и|у|ю)$",
                  "", w) if len(w) > 4 else w


def _list(v, n: int = 20, size: int = 80) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,;\n]", v)
    return [str(x).strip()[:size] for x in v or [] if str(x).strip()][:n]


def _scopes(v) -> list[str]:
    out = [s for s in _list(v) if s in SCOPES]
    return out or ["all"]


# ---------------------------------------------------------------- правила
def rules(store: Store, *, active: bool | None = None, q: str = "") -> list[dict]:
    ensure(store)
    rows = store.db.execute("SELECT * FROM kb_rules ORDER BY pinned DESC, active DESC, updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scope"], d["keys"], d["ref"] = _j(d["scope"], []) or ["all"], _j(d["keys"], []), _j(d["ref"], {})
        d["pinned"], d["active"] = bool(d["pinned"]), bool(d["active"])
        if active is not None and d["active"] != active:
            continue
        if q and q.lower() not in (d["text"] + " " + " ".join(d["keys"])).lower():
            continue
        out.append(d)
    return out


def rule_get(store: Store, rid: int) -> dict | None:
    return next((r for r in rules(store) if r["id"] == rid), None)


def rule_save(store: Store, r: dict, *, source: str = "user") -> dict:
    ensure(store)
    text = str(r.get("text") or "").strip()[:2000]
    if not text:
        raise ValueError("пустое правило")
    now = _now()
    vals = (text, json.dumps(_scopes(r.get("scope")), ensure_ascii=False), json.dumps(_list(r.get("keys")), ensure_ascii=False),
            str(r.get("kind") or "rule")[:20], int(bool(r.get("pinned"))), int(r.get("active", True) is not False))
    if r.get("id"):
        store.db.execute("UPDATE kb_rules SET text=?, scope=?, keys=?, kind=?, pinned=?, active=?, updated_at=? WHERE id=?",
                         (*vals, now, int(r["id"])))
        rid = int(r["id"])
    else:
        dup = next((x for x in rules(store) if _norm(x["text"]) == _norm(text)), None)
        if dup:
            store.db.execute("UPDATE kb_rules SET active=1, updated_at=? WHERE id=?", (now, dup["id"]))
            store.db.commit()
            return rule_get(store, dup["id"]) or dup
        cur = store.db.execute("""INSERT INTO kb_rules(text, scope, keys, kind, pinned, active, source, ref, created_at,
                                  updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                               (*vals, source, json.dumps(r.get("ref") or {}, ensure_ascii=False), now, now))
        rid = int(cur.lastrowid)
    store.db.commit()
    return rule_get(store, rid) or {}


def rule_delete(store: Store, rid: int) -> None:
    ensure(store)
    store.db.execute("DELETE FROM kb_rules WHERE id=?", (rid,))
    store.db.commit()


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower().replace("ё", "е")).strip()


def _matches(keys: list[str], text: str, stems: set[str]) -> bool:
    low = (text or "").lower().replace("ё", "е")
    for k in keys:
        k2 = k.lower().replace("ё", "е")
        if " " in k2 or len(k2) <= 3:
            if re.search(r"(?<![\wа-я])" + re.escape(k2), low):
                return True
        elif _stem(k2) in stems:
            return True
    return False


def relevant_rules(store: Store, text: str, scope: str | None, limit: int = 25) -> list[dict]:
    """Правила для запроса: активные, область подходит, и (закреплено, без признаков или признак есть в тексте)."""
    try:
        rs = rules(store, active=True)
    except Exception as e:  # noqa: BLE001 — нет базы/таблицы: без правил
        log.info("правила не прочитаны: %s", logs.err_info(e))
        return []
    stems = {_stem(w) for w in _WORD.findall(text or "")}
    out = []
    for r in rs:
        if scope and "all" not in r["scope"] and scope not in r["scope"]:
            continue
        if r["pinned"] or not r["keys"] or _matches(r["keys"], text, stems):
            out.append(r)
    return out[:limit]


def rules_block(store: Store | None, text: str, scope: str | None, limit_chars: int = 2200) -> str:
    """«Правила и поправки пользователя» для запроса к модели (пусто — если нечего)."""
    if store is None:
        return ""
    rs = relevant_rules(store, text, scope)
    if not rs:
        return ""
    lines, size = [], 0
    for r in rs:
        line = f"- {r['text']}"
        if size + len(line) > limit_chars:
            break
        lines.append(line)
        size += len(line)
    try:
        store.db.executemany("UPDATE kb_rules SET hits=hits+1 WHERE id=?", [(r["id"],) for r in rs[:len(lines)]])
        store.db.commit()
    except Exception:  # noqa: BLE001
        pass
    return ("Правила и поправки пользователя (он сам научил этому программу — соблюдай их, они важнее общих "
            "правил выше):\n" + "\n".join(lines))


# ---------------------------------------------------------------- опорные ссылки
def links(store: Store) -> list[dict]:
    ensure(store)
    out = []
    for r in store.db.execute("SELECT * FROM kb_links ORDER BY title COLLATE NOCASE").fetchall():
        d = dict(r)
        d["keys"], d["scope"] = _j(d["keys"], []), _j(d["scope"], [])
        out.append(d)
    return out


def link_save(store: Store, x: dict) -> dict:
    ensure(store)
    url = str(x.get("url") or "").strip()[:800]
    title = str(x.get("title") or "").strip()[:300]
    if not url and not title:
        raise ValueError("нужен адрес или название страницы")
    if url and not re.match(r"https?://", url):
        raise ValueError("адрес — с http:// или https://")
    now = _now()
    vals = (title or url, url, str(x.get("note") or "").strip()[:1000], json.dumps(_list(x.get("keys")), ensure_ascii=False),
            json.dumps(_list(x.get("scope")), ensure_ascii=False))
    if x.get("id"):
        store.db.execute("UPDATE kb_links SET title=?, url=?, note=?, keys=?, scope=?, updated_at=? WHERE id=?",
                         (*vals, now, int(x["id"])))
        lid = int(x["id"])
    else:
        dup = store.db.execute("SELECT id FROM kb_links WHERE url=? AND url!=''", (url,)).fetchone() if url else None
        if dup:
            store.db.execute("UPDATE kb_links SET title=?, url=?, note=?, keys=?, scope=?, updated_at=? WHERE id=?",
                             (*vals, now, dup[0]))
            lid = int(dup[0])
        else:
            lid = int(store.db.execute("""INSERT INTO kb_links(title, url, note, keys, scope, created_at, updated_at)
                                          VALUES (?,?,?,?,?,?,?)""", (*vals, now, now)).lastrowid)
    store.db.commit()
    return next((l for l in links(store) if l["id"] == lid), {})


def link_delete(store: Store, lid: int) -> None:
    ensure(store)
    store.db.execute("DELETE FROM kb_links WHERE id=?", (lid,))
    store.db.commit()


def links_for(store: Store, text: str = "", kind: str | None = None, limit: int = 12) -> list[dict]:
    """Ссылки для запроса: с подходящими признаками — первыми, без признаков — тоже (это «точки входа»)."""
    try:
        ls = links(store)
    except Exception:  # noqa: BLE001
        return []
    stems = {_stem(w) for w in _WORD.findall(text or "")}
    hit, rest = [], []
    for l in ls:
        if kind and l["scope"] and kind not in l["scope"] and "all" not in l["scope"]:
            continue
        if l["keys"] and text and _matches(l["keys"], text, stems):
            hit.append(l)
        elif not l["keys"] or not text:
            rest.append(l)
    return (hit + rest)[:limit]


def links_block(store: Store | None, text: str = "", kind: str | None = None, limit_chars: int = 1600) -> str:
    if store is None:
        return ""
    ls = links_for(store, text, kind)
    if not ls:
        return ""
    lines, size = [], 0
    for l in ls:
        line = f"- {l['title']}" + (f" — {l['url']}" if l["url"] and l["url"] != l["title"] else "") + \
            (f" ({l['note']})" if l.get("note") else "")
        if size + len(line) > limit_chars:
            break
        lines.append(line)
        size += len(line)
    return "Опорные страницы вики пользователя — начинай поиск с них, если подходят по теме:\n" + "\n".join(lines)


def plugin_context(store: Store, kind: str) -> str:
    """Добавка к вводной запросов к плагинам Maverick: опорные страницы (для вики и общих запросов) и правила поиска."""
    parts = []
    if kind in ("wiki", "any"):
        b = links_block(store, "", kind if kind != "any" else None)
        if b:
            parts.append(b)
    rs = [r for r in relevant_rules(store, "", "search") if r["pinned"] or not r["keys"]
          or any(k.lower() in (kind, "вики" if kind == "wiki" else kind) for k in r["keys"])]
    if rs:
        parts.append("Правила поиска пользователя:\n" + "\n".join(f"- {r['text']}" for r in rs[:12]))
    return "\n".join(parts)


def block(store: Store | None, text: str, scope: str | None, limit_chars: int = 2600) -> str:
    """Правила + глоссарий (+ опорные ссылки для поиска) — то, что подставляется в запросы к модели."""
    from .glossary import block_for
    parts = [rules_block(store, text, scope), block_for(store, text, limit_chars, scope=None)]
    if scope in ("search", "glossary", "describe"):
        parts.append(links_block(store, text))
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------- журнал и отмена
def journal(store: Store, limit: int = 100) -> list[dict]:
    ensure(store)
    out = []
    for r in store.db.execute("SELECT * FROM kb_journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        d = dict(r)
        d["undone"] = bool(d["undone"])
        d["data"] = _j(d["data"], {})
        d["ref"] = _j(d["ref"], {})
        d["targets"] = len(d["data"].get("items") or [])
        out.append(d)
    return out


def _log(store: Store, op: str, summary: str, items: list[dict], source: str, ref: dict | None) -> int:
    ensure(store)
    cur = store.db.execute("INSERT INTO kb_journal(op, summary, data, source, ref, created_at) VALUES (?,?,?,?,?,?)",
                           (op, summary[:500], json.dumps({"items": items}, ensure_ascii=False), source,
                            json.dumps(ref or {}, ensure_ascii=False), _now()))
    store.db.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------- действия
ACTIONS_HELP = """Действия (массив JSON "actions"; только нужные):
{"op": "rule.add", "text": "правило в повелительном наклонении — как поступать в будущем", "scope": ["digest", "assistant", "search", "describe", "glossary", "vision"] или ["all"], "keys": ["слова-признаки, при которых правило нужно; пусто — всегда"]}
{"op": "rule.update", "id": 3, "text": "…", "scope": […], "keys": […]}
{"op": "rule.off", "id": 3}
{"op": "term.upsert", "term": "термин", "aliases": ["синонимы, сокращения"], "kind": "service | app | module | team | person | api | process | entity | instruction | problem | abbr | other", "summary": "одна строка", "senses": [{"meaning": "значение", "how_to_tell": "как отличить", "id_format": "регулярка номера или пусто", "owner": "кто отвечает"}], "replace_senses": false, "details": "подробности, инструкция"}
{"op": "term.hide", "term": "термин"}
{"op": "link.add", "title": "название страницы", "url": "https://…", "note": "для чего", "keys": ["признаки"]}
{"op": "chat.set", "where": {"ids": [], "title": ["подстрока названия"], "not_title": [], "folder": "папка Telegram", "kind": "group | channel | private | bot | forum", "mode": "chat | incident | alerts | news"}, "set": {"description": "описание (заменит ваше)", "append": "дописать к описанию", "priority": "high | normal | low", "mode": "chat | incident | alerts | news", "exclude": false}}
{"op": "topic.set", "where": {"chat_ids": [], "chat_title": ["подстрока"], "topic_title": ["подстрока"]}, "set": {"description": "…", "append": "…", "priority": "high | normal | low", "exclude": false}}
{"op": "person.set", "where": {"ids": [], "name": ["подстрока имени"], "username": ["логин"], "chat_title": ["участники чатов с такой подстрокой"]}, "set": {"post": "должность", "dept": "подразделение", "area": "зона ответственности", "ask": "с чем к нему обращаться", "comes": "с чем приходит ко мне", "note": "заметки", "contacts": "телефоны, почта", "priority": "high | normal | low", "aliases_add": ["прозвища"]}}
{"op": "describe", "what": "chats | topics | people", "where": {как у chat.set / person.set}, "sources": ["wiki", "mail", "itsm", "rooms", "tracker", "meet"], "overwrite": true}
Правила для where: подстроки — без учёта регистра; где много объектов — условие, а не перечисление номеров."""


class Ctx:
    """Что нужно действиям: сырой config.json (raw — меняется на месте), база, разобранный cfg."""

    def __init__(self, raw: dict, store: Store, cfg=None):
        self.raw, self.store, self.cfg = raw, store, cfg
        self._chats = None

    def chats(self) -> list:
        if self._chats is None:
            self._chats = self.store.chats()
        return self._chats

    def folders(self) -> list[dict]:
        return _j(self.store.kv_get("tg_folders"), [])


def _subs(v) -> list[str]:
    return [x.lower().replace("ё", "е") for x in _list(v, 30, 120)]


def _has(title: str, subs: list[str]) -> bool:
    t = (title or "").lower().replace("ё", "е")
    return any(s in t for s in subs)


def select_chats(ctx: Ctx, where: dict) -> list:
    where = where or {}
    ids = {int(i) for i in where.get("ids") or [] if str(i).lstrip("-").isdigit()}
    title, not_title = _subs(where.get("title")), _subs(where.get("not_title"))
    kind = str(where.get("kind") or "").strip().lower()
    mode = str(where.get("mode") or "").strip().lower()
    folder = str(where.get("folder") or "").strip().lower()
    fids: set[int] | None = None
    if folder:
        f = next((f for f in ctx.folders() if (f.get("title") or "").lower() == folder), None) or \
            next((f for f in ctx.folders() if folder in (f.get("title") or "").lower()), None)
        fids = set((f or {}).get("inc") or []) | set((f or {}).get("pin") or []) if f else set()
    if not (ids or title or kind or mode or fids is not None):
        return []
    out = []
    for c in ctx.chats():
        if ids and c.chat_id not in ids:
            continue
        if title and not _has(c.title, title):
            continue
        if not_title and _has(c.title, not_title):
            continue
        if kind == "forum" and not getattr(c, "forum", False):
            continue
        if kind and kind != "forum" and (c.kind or "group") != kind:
            continue
        if fids is not None and c.chat_id not in fids:
            continue
        if mode and ctx.cfg is not None:
            r = ctx.cfg.chat_rule(c.title, c.username, c.chat_id, c.kind or "group")
            if r["kind"] != mode:
                continue
        out.append(c)
    return out


def select_topics(ctx: Ctx, where: dict) -> list[tuple]:
    where = where or {}
    cids = {int(i) for i in where.get("chat_ids") or [] if str(i).lstrip("-").isdigit()}
    ct, tt = _subs(where.get("chat_title")), _subs(where.get("topic_title"))
    if not (cids or ct):
        return []
    out = []
    for c in ctx.chats():
        if cids and c.chat_id not in cids:
            continue
        if ct and not _has(c.title, ct):
            continue
        for t in ctx.store.topics(c.chat_id):
            if tt and not _has(t.get("title") or "", tt):
                continue
            out.append((c, t))
    return out


def select_people(ctx: Ctx, where: dict) -> list[dict]:
    from . import people
    where = where or {}
    ids = {str(int(i)) for i in where.get("ids") or [] if str(i).lstrip("-").isdigit()}
    names, users, ct = _subs(where.get("name")), [u.lstrip("@") for u in _subs(where.get("username"))], _subs(where.get("chat_title"))
    if not (ids or names or users or ct):
        return []
    pp = people.all_people(ctx.raw)
    people.ensure(ctx.store)
    rows = ctx.store.db.execute(
        """SELECT sender_id AS id, MAX(sender) AS name FROM messages WHERE is_out=0 AND sender_id IS NOT NULL GROUP BY sender_id
           UNION SELECT user_id, MAX(name) FROM members GROUP BY user_id""").fetchall()
    known: dict[str, dict] = {}
    for r in rows:
        known.setdefault(str(r["id"]), {"id": str(r["id"]), "name": r["name"] or "", "username": ""})
    for uid, p in pp.items():
        known.setdefault(uid, {"id": uid, "name": p.get("name") or "", "username": ""})
        known[uid].update({k: p[k] for k in ("name", "username") if p.get(k)})
    for r in ctx.store.db.execute("SELECT user_id, username FROM members WHERE username IS NOT NULL").fetchall():
        if str(r[0]) in known and not known[str(r[0])]["username"]:
            known[str(r[0])]["username"] = r[1] or ""
    in_chats: set[str] | None = None
    if ct:
        cids = [c.chat_id for c in ctx.chats() if _has(c.title, ct)]
        in_chats = set()
        for cid in cids:
            in_chats |= {str(r[0]) for r in ctx.store.db.execute(
                "SELECT DISTINCT sender_id FROM messages WHERE chat_id=? AND sender_id IS NOT NULL UNION "
                "SELECT user_id FROM members WHERE chat_id=?", (cid, cid)).fetchall()}
    me = str(_j(ctx.store.kv_get("me"), {}).get("id") or "")
    out = []
    for uid, p in known.items():
        if uid == me:
            continue
        if ids and uid not in ids:
            continue
        al = " ".join(pp.get(uid, {}).get("aliases") or [])
        if names and not _has(p["name"] + " " + al, names):
            continue
        if users and (p.get("username") or "").lower() not in users:
            continue
        if in_chats is not None and uid not in in_chats:
            continue
        out.append(p)
    return out


def resolve(ctx: Ctx, a: dict) -> dict:
    """Что сделает действие: {op, summary, targets: [{id, title}], count, error}. Ничего не меняет."""
    from .glossary import Glossary
    op = str(a.get("op") or "")
    res: dict[str, Any] = {"op": op, "targets": [], "error": ""}
    try:
        if op == "rule.add":
            res["summary"] = f"Правило: {str(a.get('text') or '')[:300]}"
            if not str(a.get("text") or "").strip():
                res["error"] = "пустое правило"
        elif op in ("rule.update", "rule.off"):
            r = rule_get(ctx.store, int(a.get("id") or 0))
            if not r:
                res["error"] = f"правило №{a.get('id')} не найдено"
            res["summary"] = ("Выключить правило: " if op == "rule.off" else "Изменить правило: ") + \
                str((r or {}).get("text") or "")[:200] + (f" → {str(a.get('text'))[:200]}" if a.get("text") else "")
        elif op == "term.upsert":
            t = str(a.get("term") or "").strip()
            old = Glossary(ctx.store).get(t) if t else None
            res["summary"] = ("Дополнить термин «" if old else "Новый термин «") + t + "»" + \
                (f": {str(a.get('summary') or '')[:200]}" if a.get("summary") else "")
            if not t:
                res["error"] = "нет термина"
        elif op == "term.hide":
            t = str(a.get("term") or "").strip()
            res["summary"] = f"Скрыть термин «{t}»"
            if not Glossary(ctx.store).get(t):
                res["error"] = f"термина «{t}» нет в глоссарии"
        elif op == "link.add":
            res["summary"] = f"Опорная ссылка: {a.get('title') or a.get('url')}"
            if a.get("url") and not re.match(r"https?://", str(a["url"])):
                res["error"] = "адрес — с http:// или https://"
        elif op == "chat.set":
            cs = select_chats(ctx, a.get("where") or {})
            res["targets"] = [{"id": c.chat_id, "title": c.title} for c in cs]
            res["summary"] = f"Чаты ({len(cs)}): " + _set_text(a.get("set") or {})
            if not cs:
                res["error"] = "ни один чат не подошёл под условие"
        elif op == "topic.set":
            ts = select_topics(ctx, a.get("where") or {})
            res["targets"] = [{"id": [c.chat_id, t["topic_id"]], "title": f"{c.title} → {t.get('title') or t['topic_id']}"} for c, t in ts]
            res["summary"] = f"Темы ({len(ts)}): " + _set_text(a.get("set") or {})
            if not ts:
                res["error"] = "ни одна тема не подошла под условие"
        elif op == "person.set":
            ps = select_people(ctx, a.get("where") or {})
            res["targets"] = [{"id": p["id"], "title": p["name"] + (f" @{p['username']}" if p.get("username") else "")} for p in ps]
            res["summary"] = f"Люди ({len(ps)}): " + _set_text(a.get("set") or {})
            if not ps:
                res["error"] = "никто не подошёл под условие"
        elif op == "describe":
            what = str(a.get("what") or "chats")
            if what == "people":
                ps = select_people(ctx, a.get("where") or {})
                res["targets"] = [{"id": p["id"], "title": p["name"]} for p in ps]
            elif what == "topics":
                ts = select_topics(ctx, a.get("where") or {})
                res["targets"] = [{"id": [c.chat_id, t["topic_id"], t.get("title") or ""], "title": f"{c.title} → {t.get('title')}"} for c, t in ts]
            else:
                cs = select_chats(ctx, a.get("where") or {})
                res["targets"] = [{"id": c.chat_id, "title": c.title} for c in cs]
            src = ", ".join(_list(a.get("sources"))) or "только Telegram"
            res["summary"] = f"ИИ-описание ({len(res['targets'])}): {what}; источники: {src}"
            if not res["targets"]:
                res["error"] = "под условие ничего не подошло"
        else:
            res["error"] = f"неизвестное действие «{op}»"
            res["summary"] = op
    except (ValueError, TypeError, KeyError) as e:
        res["error"] = str(e)[:200]
    res["count"] = len(res["targets"])
    return res


def _set_text(s: dict) -> str:
    ru = {"description": "описание", "append": "дописать", "priority": "важность", "mode": "режим", "exclude": "исключить",
          "post": "должность", "dept": "подразделение", "area": "зона", "ask": "с чем к нему", "comes": "с чем ко мне",
          "note": "заметки", "contacts": "контакты", "aliases_add": "прозвища"}
    val = {"high": "высокая", "normal": "обычная", "low": "низкая", "chat": "рабочий", "incident": "авария/инцидент",
           "alerts": "алерты", "news": "новости", True: "да", False: "нет"}

    def v_(v):
        if isinstance(v, list):
            return ", ".join(map(str, v))
        return val.get(v, str(v)) if isinstance(v, (str, bool)) else str(v)
    return "; ".join(f"{ru.get(k, k)}: {v_(v)[:120]}" for k, v in s.items() if v not in (None, "", []))


def apply(ctx: Ctx, a: dict, *, source: str = "assistant", ref: dict | None = None,
          start_describe: Callable[[dict], Any] | None = None) -> dict:
    """Выполнить действие (raw меняется на месте — сохраняет вызывающий). Возвращает {journal, summary, count}."""
    from . import people
    from .glossary import Glossary
    from .ui.chats_api import _apply_topics, _base_prio, _id_rule, apply_fields
    pre = resolve(ctx, a)
    if pre["error"]:
        raise ValueError(pre["error"])
    op = pre["op"]
    items: list[dict] = []
    if op == "rule.add":
        r = rule_save(ctx.store, {"text": a["text"], "scope": a.get("scope"), "keys": a.get("keys"),
                                  "kind": a.get("kind") or ("correction" if source == "feedback" else "instruction"),
                                  "pinned": a.get("pinned"), "ref": ref}, source=source)
        items.append({"t": "rule", "id": r["id"], "before": None})
    elif op in ("rule.update", "rule.off"):
        before = rule_get(ctx.store, int(a["id"]))
        new = dict(before, active=False) if op == "rule.off" else dict(
            before, **{k: a[k] for k in ("text", "scope", "keys", "pinned") if a.get(k) not in (None, "")})
        rule_save(ctx.store, new)
        items.append({"t": "rule", "id": before["id"], "before": before})
    elif op == "term.upsert":
        g = Glossary(ctx.store)
        old = g.get(a["term"])
        e = {k: a[k] for k in ("term", "aliases", "kind", "summary", "senses", "details", "owner") if a.get(k)}
        if old and not a.get("replace_senses"):
            saved = g.save(e, merge=True, source=source)
            over = {k: a[k] for k in ("summary", "details", "owner", "kind") if a.get(k)}
            if over:  # поправка пользователя заменяет «что это» и подробности, значения — дополняет
                saved = g.save(dict(saved, **over, id=saved["id"]), source=source)
        else:
            saved = g.save(dict(e, id=old["id"]) if old else e, source=source)
        if saved.get("id"):  # поправка пользователя — подтверждённая статья
            ctx.store.db.execute("UPDATE glossary SET status='confirmed' WHERE id=?", (saved["id"],))
            ctx.store.db.commit()
        items.append({"t": "term", "id": saved.get("id"), "before": _term_dump(old)})
    elif op == "term.hide":
        g = Glossary(ctx.store)
        old = g.get(a["term"])
        ctx.store.db.execute("UPDATE glossary SET status='hidden' WHERE id=?", (old["id"],))
        ctx.store.db.commit()
        items.append({"t": "term", "id": old["id"], "before": _term_dump(old)})
    elif op == "link.add":
        l = link_save(ctx.store, a)
        items.append({"t": "link", "id": l["id"], "before": None})
    elif op == "chat.set":
        s = a.get("set") or {}
        for t in pre["targets"]:
            c = next(c for c in ctx.chats() if c.chat_id == t["id"])
            old = copy.deepcopy(_id_rule(ctx.raw, c.chat_id, create=False))
            r = _id_rule(ctx.raw, c.chat_id, c.title)
            f = {k: s[k] for k in ("priority", "mode", "exclude") if k in s and s[k] not in (None, "")}
            if s.get("description") not in (None, ""):
                f["description"] = s["description"]
            if s.get("append"):
                cur = str(r.get("description") or "").strip()
                f["description"] = (cur + ("\n" if cur else "") + str(s["append"]).strip()) if s["append"] not in cur else cur
            apply_fields(r, f, _base_prio(ctx.raw, c.kind or "group"))
            items.append({"t": "chat", "id": c.chat_id, "before": old})
    elif op == "topic.set":
        s = a.get("set") or {}
        for t in pre["targets"]:
            cid, tid = t["id"]
            c = next(c for c in ctx.chats() if c.chat_id == cid)
            old = copy.deepcopy((_id_rule(ctx.raw, cid, create=False) or {}).get("topics"))
            f = {k: s[k] for k in ("priority", "exclude", "description") if k in s and s[k] not in (None, "")}
            if s.get("append"):
                cur = str((((_id_rule(ctx.raw, cid, create=False) or {}).get("topics") or {}).get(str(tid)) or {})
                          .get("description") or "").strip()
                f["description"] = cur + ("\n" if cur else "") + str(s["append"]).strip()
            title = t["title"].split(" → ", 1)[-1]
            _apply_topics(ctx.raw, cid, c.title, [tid], f, {str(tid): title})
            items.append({"t": "topic", "id": [cid, tid], "before": old})
    elif op == "person.set":
        s = dict(a.get("set") or {})
        pp = ctx.raw.get("people") if isinstance(ctx.raw.get("people"), dict) else {}
        ctx.raw["people"] = pp
        for t in pre["targets"]:
            uid = str(t["id"])
            prev = copy.deepcopy(pp.get(uid))
            body = {k: v for k, v in s.items() if k in people.FIELDS + ("priority", "username") and v not in (None, "")}
            body["name"] = (prev or {}).get("name") or t["title"].split(" @", 1)[0]
            if s.get("aliases_add"):
                body["aliases"] = list(dict.fromkeys(((prev or {}).get("aliases") or []) + _list(s["aliases_add"])))
            pp[uid] = people.clean(body, prev)
            items.append({"t": "person", "id": uid, "before": prev})
    elif op == "describe":
        if start_describe is None:
            raise ValueError("ИИ-описание отсюда не запускается")
        what = str(a.get("what") or "chats")
        ids = [t["id"] for t in pre["targets"]]
        targets = {"people": ids} if what == "people" else {"topics": ids} if what == "topics" else {"chats": ids}
        job = start_describe({"targets": targets, "sources": _list(a.get("sources")), "overwrite": a.get("overwrite", True)})
        return {"journal": None, "summary": pre["summary"], "count": pre["count"], "job": job}
    jid = _log(ctx.store, op, pre["summary"], items, source, ref)
    return {"journal": jid, "summary": pre["summary"], "count": pre["count"] or len(items)}


def _term_dump(e: dict | None) -> dict | None:
    if not e:
        return None
    return {k: e.get(k) for k in ("id", "term", "kind", "aliases", "senses", "summary", "details", "owner", "related",
                                  "status", "pinned", "visual", "examples", "links")}


def undo(ctx: Ctx, jid: int) -> dict:
    """Отменить запись журнала: вернуть, как было до неё (raw меняется на месте)."""
    from .glossary import Glossary
    from .ui.chats_api import _id_rule, _rules
    ensure(ctx.store)
    r = ctx.store.db.execute("SELECT * FROM kb_journal WHERE id=?", (jid,)).fetchone()
    if not r:
        raise ValueError("запись журнала не найдена")
    if r["undone"]:
        raise ValueError("уже отменено")
    items = _j(r["data"], {}).get("items") or []
    for it in reversed(items):
        t, before = it["t"], it.get("before")
        if t == "rule":
            if before is None:
                rule_delete(ctx.store, int(it["id"]))
            else:
                rule_save(ctx.store, before)
        elif t == "link":
            link_delete(ctx.store, int(it["id"]))
        elif t == "term":
            g = Glossary(ctx.store)
            if before is None:
                if it.get("id"):
                    g.delete(int(it["id"]))
            else:
                g.save({k: v for k, v in before.items() if v is not None}, source="undo")
                ctx.store.db.execute("UPDATE glossary SET status=? WHERE id=?", (before.get("status") or "draft", before["id"]))
                ctx.store.db.commit()
        elif t == "chat":
            rules_ = _rules(ctx.raw)
            cur = _id_rule(ctx.raw, int(it["id"]), create=False)
            if cur is not None:
                rules_.remove(cur)
            if before is not None:
                rules_.insert(0, before)
        elif t == "topic":
            cid = int(it["id"][0])
            rule = _id_rule(ctx.raw, cid, create=False)
            if rule is not None:
                if before:
                    rule["topics"] = before
                else:
                    rule.pop("topics", None)
        elif t == "person":
            pp = ctx.raw.setdefault("people", {})
            if before is None:
                pp.pop(str(it["id"]), None)
            else:
                pp[str(it["id"])] = before
    ctx.store.db.execute("UPDATE kb_journal SET undone=1 WHERE id=?", (jid,))
    ctx.store.db.commit()
    return {"undone": jid, "summary": r["summary"]}


def parse_blocks(text: str) -> tuple[str, list[dict], dict | None]:
    """Ответ модели → (текст без служебных блоков, действия, вопрос {question, options}).

    Действия — блок ```actions [...]``` или JSON с полем actions; вопрос — ```ask {...}```."""
    actions: list[dict] = []
    ask = None
    t = text or ""

    def take(m: re.Match) -> str:
        nonlocal ask
        kind, body = m.group(1).lower(), m.group(2)
        try:
            v = json.loads(body)
        except ValueError:
            return m.group(0)
        if kind == "actions":
            vs = v.get("actions") if isinstance(v, dict) else v
            actions.extend(x for x in vs or [] if isinstance(x, dict) and x.get("op"))
        elif kind == "ask" and isinstance(v, dict) and v.get("question"):
            ask = {"question": str(v["question"])[:600], "options": _list(v.get("options"), 6, 200)}
        return ""
    t = re.sub(r"```(actions|ask)\s*\n?(.*?)```", take, t, flags=re.S | re.I)
    return t.strip(), actions[:30], ask


# ---------------------------------------------------------------- указания в ассистенте
COMMAND_RE = re.compile(
    r"(?i)(?:^|[\s,.;:!])(поставь|поставьте|пропиши|пропишите|пропишем|добавь|добавьте|измени|измените|исправь|исправьте|"
    r"замени|замените|запомни|запомните|учти|учтите|обнови|обновите|удали|удалите|убери|уберите|создай|создайте|"
    r"назначь|назначьте|внеси|внесите|отметь|отметьте|переименуй|допиши|допишите|опиши\s+(?:все|всех|чаты|людей|темы)|"
    r"сделай\s+(?:всем|для\s+всех)|всем\s+чатам|для\s+всех\s+чатов|в\s+глоссари[йи]|в\s+навык|новое\s+правило|"
    r"исключи|исключите|включи\s+в\s+сводку|не\s+путай|различай|впредь|в\s+дальнейшем|теперь\s+всегда|всегда\s+пиши)"
    r"(?=[\s,.;:!]|$)")

COMMAND_SYSTEM = """Ты — часть программы OpsDigest (рабочий помощник инженера 2-й линии поддержки) и выполняешь указания
пользователя по её настройке: описания чатов, тем форумов и людей, глоссарий терминов, правила (навык: как программе
поступать в сводках, ответах, поиске, описаниях), опорные ссылки на вики. Ниже — что сейчас есть в программе.

Как отвечать:
- Коротко скажи, что сделаешь (1–3 предложения), и добавь блок ```actions [ ... ]``` с действиями.
- Если указание неясно (непонятно, какие чаты, что именно записать, термин или правило) — ничего не делай, а задай
  ОДИН уточняющий вопрос блоком ```ask {"question": "…", "options": ["вариант", "…"]}``` (варианты — если можешь
  предположить).
- Пояснение пользователя о смысле терминов, номеров, чатов — это и статья глоссария (term.upsert), и, если это про то,
  как поступать, — правило (rule.add). Не дублируй уже существующие правила — меняй их (rule.update).
- Не выдумывай чаты и людей: условия where — по тому, что назвал пользователь.

{actions}"""


def looks_like_command(text: str) -> bool:
    return bool(COMMAND_RE.search(text or ""))


def catalog(ctx: Ctx, text: str, limit: int = 40) -> str:
    """Что есть в программе по словам указания: чаты, папки, люди, термины, правила, ссылки (коротко)."""
    from .glossary import Glossary
    words = [w for w in _WORD.findall(text or "") if len(w) >= 4]
    stems = {_stem(w) for w in words}
    parts = []
    cs = ctx.chats()
    hit = [c for c in cs if any(_stem(w) in stems for w in _WORD.findall(c.title or "")) or
           any(w.lower() in (c.title or "").lower() for w in words)]
    parts.append(f"Чатов в программе: {len(cs)}. Подходящие по словам указания ({len(hit)}):\n" +
                 "\n".join(f"- [{c.chat_id}] «{c.title}» ({c.kind or 'group'})" for c in hit[:limit]))
    fs = [f.get("title") for f in ctx.folders() if f.get("title")]
    if fs:
        parts.append("Папки Telegram: " + ", ".join(fs[:30]))
    try:
        ps = select_people(ctx, {"name": words[:12]}) if words else []
    except Exception:  # noqa: BLE001
        ps = []
    if ps:
        parts.append("Люди по словам указания:\n" + "\n".join(f"- [{p['id']}] {p['name']}" + (f" @{p['username']}" if p.get("username") else "")
                                                         for p in ps[:20]))
    try:
        es = Glossary(ctx.store).match(text, limit=12, pinned=False)
    except Exception:  # noqa: BLE001
        es = []
    if es:
        from .glossary import entry_line
        parts.append("Термины глоссария по словам указания:\n" + "\n".join(entry_line(e)[:400] for e in es))
    rs = rules(ctx.store, active=True)
    if rs:
        parts.append("Правила навыка:\n" + "\n".join(f"- [{r['id']}] ({', '.join(r['scope'])}) {r['text'][:300]}" for r in rs[:40]))
    ls = links(ctx.store)
    if ls:
        parts.append("Опорные ссылки: " + "; ".join(f"{l['title']}" for l in ls[:20]))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- отзывы
FEEDBACK_SYSTEM = """Ты обучаешь программу OpsDigest (помощник инженера 2-й линии поддержки: сводки по Telegram и
другим источникам, ответы, поиск, описания чатов и людей, глоссарий) по отзывам пользователя. Твоя задача — понять,
что было хорошо и что плохо, и превратить это в изменения навыка, чтобы в следующий раз программа сделала лучше:
- правила (rule.add / rule.update): как поступать в будущем — конкретно и проверяемо («номер вида INC1234567 — это
  обращение в ITSM, а чат с „INC“ в названии — инцидент-чат; не путать»), с областью (scope) и словами-признаками;
- глоссарий (term.upsert): если отзыв про смысл термина, формат номера, кто за что отвечает;
- описания чатов, тем, людей (chat.set / topic.set / person.set) — если отзыв про конкретный чат или человека;
- опорные ссылки (link.add) — если пользователь дал ссылку, с которой начинать поиск.
Хороший отзыв («так и надо») — тоже правило: закрепи, что именно сделано хорошо.
Если отзыв неясен и без уточнения правило выйдет неверным — задай ОДИН вопрос (question + options), действий не делай.
Не дублируй существующие правила — меняй их по номеру. Отвечай только JSON:
{"understood": true, "reply": "что ты понял и что меняешь — 1–3 предложения пользователю", "question": "",
 "options": [], "actions": [ … ]}

""" + ACTIONS_HELP


def feedback_add(store: Store, target: str, text: str, *, rating: int = 0, excerpt: str = "", ref: dict | None = None) -> int:
    ensure(store)
    now = _now()
    cur = store.db.execute("""INSERT INTO feedback(target, ref, rating, text, excerpt, status, created_at, updated_at)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           (target if target in TARGETS else "other", json.dumps(ref or {}, ensure_ascii=False),
                            max(-1, min(1, int(rating or 0))), (text or "").strip()[:4000], (excerpt or "")[:12000],
                            "new", now, now))
    store.db.commit()
    return int(cur.lastrowid)


def feedback_get(store: Store, fid: int) -> dict | None:
    ensure(store)
    r = store.db.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["ref"], d["result"] = _j(d["ref"], {}), _j(d["result"], {})
    return d


def feedback_list(store: Store, limit: int = 200) -> list[dict]:
    ensure(store)
    out = []
    for r in store.db.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        d = dict(r)
        d["ref"], d["result"] = _j(d["ref"], {}), _j(d["result"], {})
        d["excerpt"] = (d["excerpt"] or "")[:300]
        out.append(d)
    return out


def _feedback_set(store: Store, fid: int, status: str, result: dict, thread: int | None = None) -> None:
    store.db.execute("UPDATE feedback SET status=?, result=?, thread=COALESCE(?, thread), updated_at=? WHERE id=?",
                     (status, json.dumps(result, ensure_ascii=False), thread, _now(), fid))
    store.db.commit()


def feedback_prompt(ctx: Ctx, fb: dict, answers: list[tuple[str, str]]) -> str:
    from .glossary import Glossary, entry_line
    text = f"{fb['excerpt']}\n{fb['text']}"
    parts = [f"Что оценивают: {TARGETS.get(fb['target'], fb['target'])}"]
    ref = fb.get("ref") or {}
    if ref.get("title"):
        parts.append(f"Объект: {ref['title']}")
    parts.append("Результат программы (фрагмент):\n" + (fb["excerpt"] or "—")[:9000])
    parts.append({1: "Оценка: 👍 хорошо", -1: "Оценка: 👎 плохо"}.get(fb.get("rating") or 0, "Оценка: не указана"))
    parts.append(f"Отзыв пользователя: {fb['text'] or '—'}")
    for q, a in answers:
        parts.append(f"Ты спросил: {q}\nПользователь ответил: {a}")
    rs = relevant_rules(ctx.store, text, None, limit=40)
    if rs:
        parts.append("Существующие правила навыка:\n" + "\n".join(f"- [{r['id']}] ({', '.join(r['scope'])}) {r['text'][:300]}" for r in rs))
    try:
        es = Glossary(ctx.store).match(text, limit=10, pinned=True)
    except Exception:  # noqa: BLE001
        es = []
    if es:
        parts.append("Термины глоссария из этого текста:\n" + "\n".join(entry_line(e)[:400] for e in es))
    parts.append(f"Область по умолчанию для правил: {TARGET_SCOPE.get(fb['target'], 'all')}")
    return "\n\n".join(parts)


def feedback_process(ctx: Ctx, llm, fid: int, *, answer: str | None = None, progress: Progress = print,
                     auto: str = "knowledge", start_describe: Callable[[dict], Any] | None = None) -> dict:
    """Отзыв → модель → действия. auto: knowledge — сразу применить правила/термины/ссылки, остальное ждёт «Применить»;
    none — всё ждёт. Неясно → вопрос в беседе ассистента (kind=feedback)."""
    from . import assistant as A
    from .llm.base import extract_json
    fb = feedback_get(ctx.store, fid)
    if fb is None:
        raise ValueError("отзыв не найден")
    res = fb.get("result") or {}
    answers = [tuple(x) for x in res.get("answers") or []]
    if answer is not None and res.get("question"):
        answers.append((res["question"], answer.strip()))
    tid = fb.get("thread")
    if not tid:
        title = "Отзыв: " + (fb["text"] or TARGETS.get(fb["target"], ""))[:70]
        tid = A.create(ctx.store, title, "feedback", {"feedback": fid, "target": fb["target"]})
        A.add(ctx.store, tid, "user", ({1: "👍 ", -1: "👎 "}.get(fb["rating"] or 0, "") + (fb["text"] or "")).strip() or "(без текста)",
              {"excerpt": (fb["excerpt"] or "")[:1500], "target": fb["target"]})
    elif answer is not None:
        A.add(ctx.store, tid, "user", answer)
    progress("Разбираю отзыв…")
    from . import privacy
    privacy.attach(llm, ctx.cfg, ctx.store)
    raw = llm.complete(FEEDBACK_SYSTEM, feedback_prompt(ctx, fb, answers), max_tokens=2000)
    try:
        out = extract_json(raw)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        text, acts, ask = parse_blocks(raw)
        out = {"reply": text, "actions": acts, **({"question": ask["question"], "options": ask["options"]} if ask else {})}
    reply = str(out.get("reply") or "").strip()
    question = str(out.get("question") or "").strip()
    acts = [a for a in out.get("actions") or [] if isinstance(a, dict) and a.get("op")][:20]
    result = {"reply": reply, "answers": [list(x) for x in answers]}
    if question and not acts:
        result.update(question=question, options=_list(out.get("options"), 6, 200))
        A.add(ctx.store, tid, "assistant", reply or "Уточните, пожалуйста:", {"ask": {"question": question,
                                                                                   "options": result["options"]},
                                                                           "feedback": fid})
        _feedback_set(ctx.store, fid, "question", result, tid)
        return {"status": "question", "thread": tid, "question": question, "options": result["options"]}
    plan = run_actions(ctx, acts, source="feedback", ref={"feedback": fid}, auto=auto, start_describe=start_describe)
    result.update(plan=plan)
    A.add(ctx.store, tid, "assistant", reply or "Учту.", {"plan": plan, "feedback": fid})
    status = "pending" if any(p.get("state") == "pending" for p in plan) else "done"
    _feedback_set(ctx.store, fid, status, result, tid)
    return {"status": status, "thread": tid, "reply": reply, "plan": plan}


def run_actions(ctx: Ctx, acts: list[dict], *, source: str, ref: dict | None, auto: str = "knowledge",
                start_describe: Callable[[dict], Any] | None = None) -> list[dict]:
    """Каждое действие: разрешить; знания (правила, термины, ссылки) — применить сразу (auto=knowledge),
    изменения чатов/людей/тем и запуск описаний — всегда ждут «Применить» (raw здесь — копия только для чтения)."""
    plan = []
    for a in acts:
        p = resolve(ctx, a)
        p["action"] = a
        p["targets"] = p["targets"][:60]
        if p["error"]:
            p["state"] = "error"
        elif auto == "knowledge" and p["op"] in KNOWLEDGE_OPS:
            try:
                r = apply(ctx, a, source=source, ref=ref, start_describe=start_describe)
                p.update(state="done", journal=r.get("journal"), job=r.get("job"))
            except (ValueError, KeyError, TypeError) as e:
                p.update(state="error", error=str(e)[:200])
        else:
            p["state"] = "pending"
        plan.append(p)
    return plan


# ---------------------------------------------------------------- выгрузка и загрузка навыка
def export(store: Store) -> dict:
    from .glossary import Glossary
    return {"version": 1, "exported": _now(),
            "rules": [{k: r[k] for k in ("text", "scope", "keys", "kind", "pinned", "active")} for r in rules(store)],
            "links": [{k: l[k] for k in ("title", "url", "note", "keys", "scope")} for l in links(store)],
            "glossary": Glossary(store).export()}


def import_(store: Store, data: dict) -> dict:
    from .glossary import Glossary
    n_r = n_l = 0
    for r in data.get("rules") or []:
        if isinstance(r, dict) and r.get("text"):
            rule_save(store, r, source="import")
            n_r += 1
    for l in data.get("links") or []:
        if isinstance(l, dict) and (l.get("url") or l.get("title")):
            try:
                link_save(store, l)
                n_l += 1
            except ValueError:
                pass
    g = Glossary(store).import_(data["glossary"]) if isinstance(data.get("glossary"), dict) else {"terms": 0}
    return {"rules": n_r, "links": n_l, "terms": g.get("terms", 0)}


def skill_md(store: Store) -> str:
    """Читаемый SKILL.md: правила по областям, опорные ссылки, термины."""
    from .glossary import Glossary, entry_line
    lines = ["---", "name: opsdigest-skill",
             "description: Чему пользователь научил OpsDigest — правила и поправки, опорные страницы вики, глоссарий.",
             "---", "", f"# Навык OpsDigest (выгружен {datetime.now():%d.%m.%Y %H:%M})", ""]
    rs = rules(store, active=True)
    for sc, ru in SCOPES.items():
        part = [r for r in rs if sc in r["scope"]]
        if part:
            lines += [f"## Правила: {ru}", ""] + [f"- {r['text']}" + (f" _(признаки: {', '.join(r['keys'])})_" if r["keys"] else "")
                                                   for r in part] + [""]
    ls = links(store)
    if ls:
        lines += ["## Опорные страницы вики", ""] + [f"- [{l['title']}]({l['url']})" + (f" — {l['note']}" if l["note"] else "")
                                                     for l in ls] + [""]
    es = [e for e in Glossary(store).all() if e.get("status") != "hidden"]
    if es:
        lines += ["## Глоссарий", ""] + [entry_line(e) for e in es] + [""]
    return "\n".join(lines)


def counts(store: Store) -> dict:
    ensure(store)
    one = lambda q: store.db.execute(q).fetchone()[0]  # noqa: E731
    return {"rules": one("SELECT COUNT(*) FROM kb_rules WHERE active=1"), "links": one("SELECT COUNT(*) FROM kb_links"),
            "feedback": one("SELECT COUNT(*) FROM feedback"),
            "questions": one("SELECT COUNT(*) FROM feedback WHERE status='question'"),
            "pending": one("SELECT COUNT(*) FROM feedback WHERE status='pending'")}
