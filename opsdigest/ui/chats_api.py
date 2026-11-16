"""Раздел «Чаты» окна: список чатов с папками Telegram и фильтрами, массовые изменения, правила по названию,
темы форумов, участники и заметки о людях, шаблоны описаний, ИИ-описания (фоновая задача «describe»).

Настройки живут в config.json рабочей папки:
  telegram.chats — правила чатов (match = номер чата или часть названия / re:… / @имя), в правиле по номеру —
                   description (вручную), ai_description + ai_at (от ИИ), topics — настройки тем форума;
  people         — заметки о людях (opsdigest/people.py);
  templates      — шаблоны описаний: {"chat": [{"name", "text"}], "topic": […], "person.area": […], …}.
Изменения сразу применяются к базе (apply_chat_rules) — сводка и мессенджер видят их без Telegram.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .. import logs, people
from ..secure import atomic, memdb

log = logs.get("ui")
PRIO = ("high", "normal", "low")
MODES = ("chat", "incident", "alerts", "news")
TEMPLATE_KINDS = ("chat", "topic", "person.area", "person.ask", "person.comes", "person.note", "person.role")


class ApiError(Exception):
    """Понятная пользователю ошибка (превращается в UserError окна)."""


# ---------------------------------------------------------------- правила в config.json
def _rules(raw: dict) -> list[dict]:
    tg = raw.setdefault("telegram", {})
    rules = tg.get("chats")
    if not isinstance(rules, list):
        rules = tg["chats"] = []
    return rules


def _id_rule(raw: dict, chat_id: int, title: str = "", create: bool = True) -> dict | None:
    """Правило чата по номеру (создаётся в начале списка — по номеру точнее, чем по названию)."""
    rules = _rules(raw)
    for r in rules:
        if isinstance(r, dict) and str(r.get("match", "")).strip() == str(int(chat_id)):
            if title and not r.get("title"):
                r["title"] = title[:80]
            return r
    if not create:
        return None
    r = {"match": str(int(chat_id))}
    if title:
        r["title"] = title[:80]
    rules.insert(0, r)
    return r


def _base_prio(raw: dict, kind: str) -> str:
    d = (raw.get("telegram") or {}).get("defaults") or {"private": "high", "group": "normal", "channel": "low",
                                                         "bot": "ignore"}
    return d.get(kind or "group", "normal")


def apply_fields(rule: dict, f: dict, base: str) -> None:
    """Поля из окна → правило. Пустое значение убирает ключ; остальные ключи правила (темы, ИИ) не трогаются."""
    if "priority" in f:
        p = f["priority"]
        if p == "ignore":  # старое окно: «не читать»
            f = dict(f, exclude=True)
            p = base if base != "ignore" else "low"
        if p in PRIO:
            rule["priority"] = p
    if "mode" in f and f["mode"] in MODES:
        rule["kind"] = f["mode"]
    if "exclude" in f:
        if f["exclude"]:
            rule["exclude"] = True
        else:
            rule.pop("exclude", None)
            if rule.get("priority") == "ignore":
                rule["priority"] = base if base != "ignore" else "low"
            if base == "ignore" and rule.get("priority") in (None, "ignore"):  # боты исключены по умолчанию
                rule["priority"] = "low"
    for key, src in (("description", "description"), ("ai_description", "ai_description")):
        if src in f:
            v = str(f[src] or "").strip()[:6000]
            if v:
                rule[key] = v
            else:
                rule.pop(key, None)
                if key == "ai_description":
                    rule.pop("ai_at", None)
    if f.get("ai_at"):
        rule["ai_at"] = f["ai_at"]
    for k in ("ai_mode", "ai_priority"):
        if k in f:
            if f[k]:
                rule[k] = f[k]
            else:
                rule.pop(k, None)


def _after_save(app) -> None:
    from ..chatrules import apply_chat_rules
    try:
        st = app.store()
        try:
            apply_chat_rules(app.cfg(), st)
        finally:
            st.close()
    except Exception as e:  # noqa: BLE001
        log.warning("правила чатов не применены к базе: %s", logs.err_info(e))


SAVE_EVERY, SAVE_SECONDS = 10, 20   # описания ИИ: запись в config.json пачками


def _file_error(e: OSError) -> str:
    if atomic.busy(e):
        return ("config.json занят другой программой (антивирус, OneDrive или поиск Windows) — "
                f"{type(e).__name__}")
    return f"{type(e).__name__}: {e.strerror or e}"


def update(app, fn) -> Any:
    """Прочитать config.json, изменить (fn(raw)), записать — под замком: окно и фоновая задача не затирают друг друга."""
    with app.cfg_lock:
        raw = app.raw()
        res = fn(raw)
        app.save(raw)
        return res


# ---------------------------------------------------------------- список чатов
def chats_view(app) -> dict:
    cfg = app.cfg()
    if not memdb.exists(cfg.path(cfg["storage"]["path"])):
        return {"chats": [], "folders": [], "rules": [], "stats": {}}
    st = app.store()
    try:
        rows = st.chat_overview()
        extra = {r["chat_id"]: r for r in st.db.execute(
            """SELECT c.chat_id, c.pinned, c.archived, c.contact, c.top_date, c.forum, c.photo,
                      (SELECT COUNT(*) FROM topics t WHERE t.chat_id=c.chat_id) AS n_topics,
                      (SELECT COUNT(DISTINCT m.sender_id) FROM messages m WHERE m.chat_id=c.chat_id AND m.is_out=0)
                        AS n_people
               FROM chats c""").fetchall()}
        folders = json.loads(st.kv_get("tg_folders") or "[]")
        stats = st.media_stats()
    finally:
        st.close()
    for r in rows:
        rule = cfg.chat_rule(r["title"], r["username"], r["id"], r["kind"] or "group")
        e = extra.get(r["id"])
        last = max(str(r.get("last") or ""), str((e["top_date"] if e else "") or ""))
        r.update(priority=rule["rank"], excluded=rule["excluded"], description=rule["description"], mode=rule["kind"],
                 explicit=rule["explicit"], ai=rule["ai"], ai_at=rule["ai_at"], by=rule["by"], last=last,
                 topics_set=len(rule["topics"]))
        if e:
            r.update(pinned=bool(e["pinned"]), archived=bool(e["archived"]), contact=bool(e["contact"]),
                     forum=bool(e["forum"]), topics=e["n_topics"], people=e["n_people"], photo=e["photo"] or "")
    rules = [dict(r, i=i) for i, r in enumerate((cfg.telegram.get("chats") or []))
             if isinstance(r, dict) and not re.fullmatch(r"-?\d+", str(r.get("match", "")).strip())]
    return {"chats": rows, "folders": folders, "rules": rules, "stats": stats,
            "people": sum(1 for p in people.all_people(cfg).values() if people.has_text(p))}


def bulk(app, body: dict) -> dict:
    """Одно изменение для многих чатов: важность, режим, исключение (и описание — для одного чата)."""
    ids = [int(i) for i in body.get("ids") or [] if str(i).lstrip("-").isdigit()]
    f = {k: v for k, v in (body.get("set") or {}).items()
         if k in ("priority", "mode", "exclude", "description", "ai_description")}
    if not ids or not f:
        raise ApiError("не выбраны чаты или что изменить")
    st = app.store()
    try:
        info = {c.chat_id: c for c in st.chats()}
    finally:
        st.close()

    def go(raw):
        for cid in ids:
            c = info.get(cid)
            r = _id_rule(raw, cid, c.title if c else "")
            apply_fields(r, f, _base_prio(raw, c.kind if c else "group"))
        raw.setdefault("ui", {})["chats_reviewed"] = True
    update(app, go)
    _after_save(app)
    return {"changed": len(ids)}


def save_chat(app, body: dict) -> dict:
    """Окно «Настроить» одного чата (совместимо со старым видом запроса {chat: {...}})."""
    c = body.get("chat") or body
    cid = int(c["id"])
    f = {"priority": c.get("priority"), "mode": c.get("kind") or c.get("mode"), "exclude": bool(c.get("exclude")),
         "description": c.get("description") or ""}
    if "ai_description" in c:
        f["ai_description"] = c.get("ai_description") or ""
    f = {k: v for k, v in f.items() if v is not None}

    def go(raw):
        r = _id_rule(raw, cid, str(c.get("title") or ""))
        apply_fields(r, f, _base_prio(raw, c.get("type") or "group"))
    update(app, go)
    _after_save(app)
    return {"saved": True}


def name_rule(app, body: dict) -> dict:
    """Правило по названию (для будущих чатов тоже): «авария» → режим «авария», исключить и т. п."""
    def go(raw):
        rules = _rules(raw)
        if body.get("delete") is not None:
            i = int(body["delete"])
            if 0 <= i < len(rules) and not re.fullmatch(r"-?\d+", str(rules[i].get("match", ""))):
                rules.pop(i)
            return {"deleted": True}
        m = str(body.get("match") or "").strip()
        if len(m) < 2:
            raise ApiError("слово или фраза для правила — от 2 символов")
        if m.startswith("re:"):
            try:
                re.compile(m[3:])
            except re.error as e:
                raise ApiError(f"ошибка в регулярном выражении: {e}") from None
        r = next((x for x in rules if isinstance(x, dict) and str(x.get("match", "")) == m), None)
        if r is None:
            r = {"match": m}
            rules.append(r)  # после правил по номеру: явная настройка чата важнее правила по названию
        apply_fields(r, {k: v for k, v in body.items() if k in ("priority", "mode", "exclude", "description")},
                     "normal")
        return {"rule": r}
    res = update(app, go)
    _after_save(app)
    return res


# ---------------------------------------------------------------- темы форумов
def topics_view(app, chat_id: int, live: bool = False) -> dict:
    note = ""
    if live and app.live.state == "online":
        try:
            app.live.call(app.live.load_topics(chat_id), 90)
        except Exception as e:  # noqa: BLE001
            note = f"список тем из Telegram не получен: {e}"
    cfg = app.cfg()
    st = app.store()
    try:
        c = st.get_chat(chat_id)
        rows = st.topics(chat_id)
    finally:
        st.close()
    rule = cfg.chat_rule(c.title if c else "", c.username if c else None, chat_id, (c.kind if c else "") or "group")
    sets = rule["topics"]
    for t in rows:
        s = sets.get(str(t["topic_id"])) or {}
        t.update(priority=s.get("priority") or "normal", excluded=bool(s.get("exclude")),
                 description=s.get("description") or "", ai=s.get("ai_description") or "", ai_at=s.get("ai_at") or "")
    return {"topics": rows, "note": note, "chat": {"id": chat_id, "title": c.title if c else ""}}


def save_topics(app, body: dict) -> dict:
    """Настройки тем: {chat, topics: [номера], set: {priority, exclude, description, ai_description}, titles: {…}}."""
    cid = int(body["chat"])
    tids = [int(t) for t in body.get("topics") or [] if str(t).isdigit()]
    f = body.get("set") or {}
    titles = body.get("titles") or {}
    if not tids:
        raise ApiError("не выбраны темы")
    st = app.store()
    try:
        c = st.get_chat(cid)
    finally:
        st.close()
    update(app, lambda raw: _apply_topics(raw, cid, c.title if c else "", tids, f, titles))
    return {"changed": len(tids)}


def _apply_topics(raw: dict, cid: int, title: str, tids: list[int], f: dict, titles: dict) -> None:
    """Настройки тем (как в save_topics) — изменение raw без записи."""
    r = _id_rule(raw, cid, title)
    topics = r.setdefault("topics", {})
    for t in tids:
        x = topics.setdefault(str(t), {})
        if titles.get(str(t)):
            x["title"] = str(titles[str(t)])[:80]
        if f.get("priority") in PRIO:
            if f["priority"] == "normal":
                x.pop("priority", None)
            else:
                x["priority"] = f["priority"]
        if "exclude" in f:
            if f["exclude"]:
                x["exclude"] = True
            else:
                x.pop("exclude", None)
        for k in ("description", "ai_description"):
            if k in f:
                v = str(f[k] or "").strip()[:4000]
                if v:
                    x[k] = v
                else:
                    x.pop(k, None)
                    if k == "ai_description":
                        x.pop("ai_at", None)
        if f.get("ai_at"):
            x["ai_at"] = f["ai_at"]
        if set(x) <= {"title"}:
            topics.pop(str(t), None)
    if not topics:
        r.pop("topics", None)


# ---------------------------------------------------------------- люди
def members_view(app, chat_id: int, live: bool = False) -> dict:
    note = ""
    if live and app.live.state == "online":
        try:
            app.live.call(app.live.members(chat_id), 120)
        except Exception as e:  # noqa: BLE001 — каналы без прав админа: только те, кто писал
            note = f"список участников из Telegram не получен: {e}"
    cfg = app.cfg()
    st = app.store()
    try:
        me = json.loads(st.kv_get("me") or "{}")
        rows = people.chat_members(st, chat_id, people.all_people(cfg), me.get("id"))
    finally:
        st.close()
    return {"members": rows, "note": note}


def people_view(app, q: str = "") -> dict:
    """Все люди: кто писал в чатах (из базы) и о ком есть заметки — для вкладки «Люди»."""
    cfg = app.cfg()
    pp = people.all_people(cfg)
    st = app.store()
    try:
        me = json.loads(st.kv_get("me") or "{}")
        rows = st.db.execute(
            """SELECT sender_id AS id, sender AS name, COUNT(*) AS msgs, COUNT(DISTINCT chat_id) AS chats,
                      MAX(date) AS last FROM messages WHERE is_out=0 AND sender_id IS NOT NULL AND sender!=''
               GROUP BY sender_id ORDER BY msgs DESC""").fetchall()
        bots = {r[0] for r in st.db.execute("SELECT chat_id FROM chats WHERE kind='bot'").fetchall()}
    finally:
        st.close()
    out, seen = [], set()
    for r in rows:
        if r["id"] == me.get("id") or r["id"] in bots:
            continue
        p = pp.get(str(r["id"])) or {}
        seen.add(str(r["id"]))
        out.append({"id": r["id"], "name": p.get("name") or r["name"], "msgs": r["msgs"], "chats": r["chats"],
                    "last": r["last"] or "", "priority": p.get("priority") or "normal",
                    "described": any(str(p.get(f) or "").strip() for f in people.FIELDS), "ai": bool(p.get("ai")),
                    "username": p.get("username")})
    for uid, p in pp.items():
        if uid not in seen and uid.lstrip("-").isdigit():
            out.append({"id": int(uid), "name": p.get("name") or uid, "msgs": 0, "chats": 0, "last": "",
                        "priority": p.get("priority") or "normal",
                        "described": any(str(p.get(f) or "").strip() for f in people.FIELDS),
                        "ai": bool(p.get("ai")), "username": p.get("username")})
    if q:
        ql = q.lower()
        out = [x for x in out if ql in (x["name"] or "").lower() or ql in (x.get("username") or "").lower()]
    return {"people": out}


def person_view(app, uid: int, chat_id: int | None = None) -> dict:
    cfg = app.cfg()
    p = people.all_people(cfg).get(str(uid)) or {}
    st = app.store()
    try:
        chats = people.common_chats(st, uid)
        r = st.db.execute("SELECT sender, COUNT(*) FROM messages WHERE sender_id=?", (uid,)).fetchone()
        mem = st.db.execute("SELECT name, username FROM members WHERE user_id=? LIMIT 1", (uid,)).fetchone()
    finally:
        st.close()
    name = p.get("name") or (mem["name"] if mem else "") or (r[0] if r else "") or str(uid)
    role = people.role(p, chat_id)
    return {"id": uid, "name": name, "username": p.get("username") or (mem["username"] if mem else None),
            "priority": p.get("priority") or "normal", **{f: p.get(f) or "" for f in people.FIELDS},
            "aliases": ", ".join(p.get("aliases") or []), "ai": p.get("ai") or "", "ai_at": p.get("ai_at") or "",
            "card": p.get("card") or "", "card_at": p.get("card_at") or "", "card_url": p.get("card_url") or "",
            "chat": chat_id, "role": role.get("text") or "", "role_ai": role.get("ai") or "",
            "role_ai_at": role.get("ai_at") or "", "chats": chats, "msgs": r[1] if r else 0}


def save_person(app, body: dict) -> dict:
    uid = str(int(body["id"]))

    def go(raw):
        pp = raw.get("people") if isinstance(raw.get("people"), dict) else {}
        raw["people"] = pp
        pp[uid] = people.clean(body, pp.get(uid))
        return pp[uid]
    p = update(app, go)
    return {"person": p}


def bulk_people(app, body: dict) -> dict:
    ids = [str(int(i)) for i in body.get("ids") or [] if str(i).lstrip("-").isdigit()]
    pr = (body.get("set") or {}).get("priority")
    names = body.get("names") or {}
    if not ids or pr not in PRIO:
        raise ApiError("не выбраны люди или приоритет")

    def go(raw):
        pp = raw.get("people") if isinstance(raw.get("people"), dict) else {}
        raw["people"] = pp
        for uid in ids:
            pp[uid] = people.clean({"priority": pr, "name": names.get(uid) or ""}, pp.get(uid))
    update(app, go)
    return {"changed": len(ids)}


# ---------------------------------------------------------------- шаблоны
def templates_view(app, kind: str | None = None) -> dict:
    raw = app.raw()
    t = raw.get("templates") if isinstance(raw.get("templates"), dict) else {}
    out = {k: list(t.get(k) or []) for k in TEMPLATE_KINDS}
    # уже написанное — чтобы взять описание другого чата/человека за основу
    seen: dict[str, list[dict]] = {k: [] for k in TEMPLATE_KINDS}
    for r in (raw.get("telegram") or {}).get("chats") or []:
        if not isinstance(r, dict):
            continue
        if r.get("description"):
            seen["chat"].append({"name": r.get("title") or r.get("match"), "text": r["description"]})
        for tid, x in (r.get("topics") or {}).items():
            if isinstance(x, dict) and x.get("description"):
                seen["topic"].append({"name": x.get("title") or f"тема {tid}", "text": x["description"]})
    for uid, p in (raw.get("people") or {}).items():
        if not isinstance(p, dict):
            continue
        for f in people.FIELDS:
            if p.get(f):
                seen[f"person.{f}"].append({"name": p.get("name") or uid, "text": p[f]})
        for cid, rr in (p.get("roles") or {}).items():
            if isinstance(rr, dict) and rr.get("text"):
                seen["person.role"].append({"name": p.get("name") or uid, "text": rr["text"]})
    for k in seen:  # одинаковые тексты — один раз, последние сверху
        uniq, keys = [], set()
        for x in reversed(seen[k]):
            key = x["text"].strip().lower()
            if key not in keys:
                keys.add(key)
                uniq.append(x)
        seen[k] = uniq[:40]
    if kind:
        return {"templates": out.get(kind, []), "written": seen.get(kind, [])}
    return {"templates": out, "written": seen}


def save_template(app, body: dict) -> dict:
    kind = str(body.get("kind") or "")
    if kind not in TEMPLATE_KINDS:
        raise ApiError("неизвестный вид шаблона")

    def go(raw):
        t = raw.get("templates") if isinstance(raw.get("templates"), dict) else {}
        raw["templates"] = t
        lst = t.setdefault(kind, [])
        name = str(body.get("name") or "").strip()[:80]
        if body.get("delete"):
            t[kind] = [x for x in lst if x.get("name") != name]
            return {"deleted": True}
        text = str(body.get("text") or "").strip()[:6000]
        if not name or not text:
            raise ApiError("нужны название шаблона и текст")
        for x in lst:
            if x.get("name") == name:
                x["text"] = text
                break
        else:
            lst.append({"name": name, "text": text})
        return {"saved": True}
    return update(app, go)


# ---------------------------------------------------------------- ИИ-описания (фоновая задача)
def _contacts(card: dict) -> str:
    c = card.get("contacts") or {}
    if isinstance(c, str):
        return c[:500]
    vals = [str(v) for k in ("email", "phone", "other") for v in (c.get(k) or []) if v] if isinstance(c, dict) else []
    return ", ".join(dict.fromkeys(vals))[:500]


def _card_text(card: dict) -> str:
    bits = [f"{card.get('post')}" if card.get("post") else "", f"{card.get('dept')}" if card.get("dept") else "",
            ("компетенции: " + ", ".join(map(str, card.get("competences") or []))) if card.get("competences") else "",
            f"руководитель: {card['manager']}" if card.get("manager") else "", f"источник: {card.get('from') or 'вики'}"]
    return "; ".join(b for b in bits if b)[:2000]


def describe_job(app, body: dict, job) -> dict:
    """targets: chats [id], topics [[чат, тема, название]], people [id], roles [[человек, чат]];
    overwrite — описывать и то, у чего часть ИИ уже есть; apply_mode — режим из анализа для чатов без явного режима."""
    from ..describe import Describer, stamp
    from ..llm import LLMError, make_client
    cfg = app.cfg()
    tg = body.get("targets") or {}
    over = bool(body.get("overwrite"))
    apply_mode = body.get("apply_mode", True) is not False
    # продолжение после перезапуска или ошибки: «описать заново» не повторяет то, что описано уже в этой задаче
    fresh = str((body.get("_resume") or {}).get("first") or "") if over else ""

    def done_now(at: str) -> bool:
        return bool(fresh and at and str(at) >= fresh)
    pp = people.all_people(cfg)
    targets: list[tuple] = []
    tg = {k: list(v or []) for k, v in tg.items()}
    with_ = set(body.get("with") or [])
    if with_ & {"topics", "people", "roles"}:  # вместе с чатами — их темы и участники
        st = app.store()
        try:
            me = json.loads(st.kv_get("me") or "{}").get("id")
            seen_p = {int(x) for x in tg.get("people") or []}
            for cid in [int(c) for c in tg.get("chats") or []]:
                if "topics" in with_:
                    for t in st.topics(cid):
                        if t["topic_id"] != 1 or t.get("messages"):
                            tg.setdefault("topics", []).append([cid, t["topic_id"], t["title"]])
                if with_ & {"people", "roles"}:
                    rows = st.db.execute(
                        """SELECT sender_id, COUNT(*) AS n FROM messages WHERE chat_id=? AND is_out=0
                           AND sender_id IS NOT NULL GROUP BY sender_id ORDER BY n DESC LIMIT 40""", (cid,)).fetchall()
                    bots = {r[0] for r in st.db.execute("SELECT chat_id FROM chats WHERE kind='bot'").fetchall()}
                    for r in rows:
                        uid = r["sender_id"]
                        if uid == me or uid in bots or uid == cid:
                            continue
                        if "people" in with_ and uid not in seen_p:
                            seen_p.add(uid)
                            tg.setdefault("people", []).append(uid)
                        if "roles" in with_ and r["n"] >= 3:
                            tg.setdefault("roles", []).append([uid, cid])
        finally:
            st.close()
    for cid in tg.get("chats") or []:
        cid = int(cid)
        st = app.store()
        try:
            c = st.get_chat(cid)
        finally:
            st.close()
        if c is None:
            continue
        rule = cfg.chat_rule(c.title, c.username, cid, c.kind or "group")
        if rule["ai"] and (not over or done_now(rule["ai_at"])):
            continue
        targets.append(("chat", cid))
    for x in tg.get("topics") or []:
        cid, tid = int(x[0]), int(x[1])
        st = app.store()
        try:
            c = st.get_chat(cid)
        finally:
            st.close()
        rule = cfg.chat_rule(c.title if c else "", c.username if c else None, cid, (c.kind if c else "") or "group")
        tset = rule["topics"].get(str(tid)) or {}
        if tset.get("ai_description") and (not over or done_now(tset.get("ai_at"))):
            continue
        targets.append(("topic", cid, tid, x[2] if len(x) > 2 else ""))
    for uid in tg.get("people") or []:
        p = pp.get(str(int(uid))) or {}
        if p.get("ai") and (not over or done_now(p.get("ai_at"))):
            continue
        targets.append(("person", int(uid), p.get("name") or ""))
    for x in tg.get("roles") or []:
        uid, cid = int(x[0]), int(x[1])
        p = pp.get(str(uid)) or {}
        r_ = people.role(p, cid)
        if r_.get("ai") and (not over or done_now(r_.get("ai_at"))):
            continue
        targets.append(("role", uid, cid, p.get("name") or ""))
    if not targets:
        return {"done": 0, "skipped": 0, "failed": 0, "total": 0,
                "message": "всё выбранное уже описано ИИ (включите «описать заново», чтобы обновить)"}
    try:
        name, prof = cfg.llm_profile(body.get("provider") or None)
    except KeyError as e:
        raise ApiError(str(e)) from None
    llm = make_client(name, prof)
    live = app.live

    def fetch(chat_id: int, topic: int | None) -> int:
        if live.state != "online":
            return 0
        ids, _ = live.call(live.history(chat_id, limit=100, topic=topic), 120)
        return len(ids)

    # Описания копятся и пишутся в config.json пачками (раз в SAVE_EVERY штук или SAVE_SECONDS секунд и в конце),
    # а не после каждого: при 2000 чатах это 2000 полных перезаписей растущего файла. Не вышло записать —
    # задача идёт дальше, пачка ждёт следующей попытки.
    pending: list[tuple] = []
    last_flush = [time.monotonic()]

    def apply(raw: dict, kind: str, t: tuple, res: dict, at: str, title: str, chat_kind: str) -> None:
        if kind == "chat":
            r = _id_rule(raw, t[1], title)
            f = {"ai_description": res["text"], "ai_at": at, "ai_mode": res.get("mode") or "",
                 "ai_priority": res.get("priority") or ""}
            if apply_mode and res.get("mode") in ("incident", "alerts", "news") and r.get("kind", "chat") == "chat":
                f["mode"] = res["mode"]
            apply_fields(r, f, _base_prio(raw, chat_kind))
        elif kind == "topic":
            _apply_topics(raw, t[1], title, [t[2]], {"ai_description": res["text"], "ai_at": at},
                          {str(t[2]): t[3]})
        else:
            pp2 = raw.get("people") if isinstance(raw.get("people"), dict) else {}
            raw["people"] = pp2
            prev = pp2.get(str(t[1])) or {}
            if kind == "person":
                body = {"name": prev.get("name") or t[2]}
                if res.get("text"):
                    body["ai"] = res["text"]
                card = res.get("card") or {}
                if card:  # карточка из вики/адресной книги: ручные поля не затираются
                    for f, v in (("post", card.get("post")), ("dept", card.get("dept")), ("contacts", _contacts(card)),
                                 ("username", card.get("username"))):
                        if v and not prev.get(f):
                            body[f] = v
                    body["card"] = _card_text(card)
                new = people.clean(body, prev)
                if res.get("text"):
                    new["ai_at"] = at
                if card:
                    new["card_at"] = at
                    if card.get("url"):
                        new["card_url"] = str(card["url"])[:500]
            else:
                new = people.clean({"name": prev.get("name") or t[3], "chat": t[2], "role_ai": res["text"]}, prev)
                new["roles"][str(t[2])]["ai_at"] = at
            pp2[str(t[1])] = new

    def flush() -> None:
        if not pending:
            return
        batch = list(pending)
        update(app, lambda raw: [apply(raw, *x) for x in batch])
        del pending[:len(batch)]
        last_flush[0] = time.monotonic()

    def save(kind: str, t: tuple, res: dict) -> None:
        title, chat_kind = "", "group"
        if kind in ("chat", "topic"):
            st2 = app.store()
            try:
                c = st2.get_chat(t[1])
            finally:
                st2.close()
            title, chat_kind = (c.title or "", c.kind or "group") if c else ("", "group")
        pending.append((kind, t, res, stamp(), title, chat_kind))
        if len(pending) >= SAVE_EVERY or time.monotonic() - last_flush[0] >= SAVE_SECONDS:
            try:
                flush()
            except OSError as e:
                log.warning("описания не сохранены (повторю): %s", logs.err_info(e))
                job.add(f"⚠ не удалось сохранить описания ({len(pending)} шт.): {_file_error(e)} — повторю позже")
                last_flush[0] = time.monotonic()

    job.add(f"Описать: {len(targets)} (модель: {llm.label}). Ручные описания не меняются — ИИ пишет свою часть ниже.")
    st = app.store()
    save_err: OSError | None = None
    try:
        dflt = (cfg.get("describe") or {}).get("sources")
        srcs = body.get("sources") if isinstance(body.get("sources"), list) else dflt if isinstance(dflt, list) else []
        d = Describer(cfg, st, llm, save, progress=job.add, cancel=lambda: job.cancelled, fetch=fetch, sources=srcs)
        if d.sources:
            job.add("Другие источники: " + ", ".join(d.sources) + " (через плагины Maverick)")
        workers = max(1, min(int((cfg.get("describe") or {}).get("workers") or 2), 6))
        try:
            res = d.run(targets, workers=workers)
        except LLMError as e:
            raise ApiError(f"модель недоступна: {e}") from None
        finally:  # готовое сохраняется и при остановке, и при ошибке модели
            try:
                flush()
            except OSError as e:
                save_err = e
                log.warning("описания не сохранены: %s", logs.err_info(e))
                job.add(f"✗ не сохранены последние описания ({len(pending)} шт.): {_file_error(e)}")
    finally:
        st.close()
    _after_save(app)
    if save_err is not None:
        raise ApiError(f"не удалось сохранить последние описания ({len(pending)} шт.): {_file_error(save_err)}. "
                       "Запустите «Описать» ещё раз — уже сохранённые пропустятся.")
    job.add(f"Готово: описано {res['done']}, пропущено {res['skipped']}, ошибок {res['failed']}.")
    return res
