"""ИИ-описания: чаты, темы форумов, люди (общее — по всем чатам, и роль в конкретном чате).

Ручное описание пользователя остаётся как есть; ИИ пишет свою часть отдельно (ai_description у чата и темы, ai у
человека, roles[чат].ai — роль в чате). Повторный анализ обновляет только часть ИИ. Всё, что уходит во внешнюю модель,
обезличивается (privacy.attach). Результат каждого описания сохраняется сразу — задачу можно остановить и продолжить.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from . import logs, prompts
from .digest import KIND_RU, get_tz, transcript_lines
from .llm import LLMClient, LLMError, inflight
from .llm.base import extract_json
from .namesakes import mine_targets
from .storage import Message, Store, iso, parse_iso

log = logs.get("describe")
Progress = Callable[[str], None]

SYSTEM = """Ты помогаешь инженеру технической поддержки (2-я линия) разобраться в его рабочих чатах Telegram и людях в них.
Пиши по-русски, кратко, по делу, без воды. Опирайся только на переписку, ничего не выдумывай; если чего-то не видно —
не пиши об этом. Сообщения самого пользователя помечены автором «Я».

О пользователе:
{profile}

Отвечай только JSON по схеме, без пояснений и без markdown."""

CHAT_SCHEMA = """{"type": "команда | разработка | смежная команда | авария | инцидент | алерты | новости | бот | личный | другое",
 "summary": "2–4 предложения: что за чат, что в нём обсуждают, кто основные участники",
 "topics": ["основные темы, до 6"],
 "user_role": "роль пользователя в чате и что от него обычно ждут — или null",
 "use": "как пользователю использовать этот чат: за чем следить, что можно пропускать",
 "incident": {"what": "что случилось", "status": "закрыта | идёт | неясно", "outcome": "чем кончилось или null",
              "started": "ДД.ММ.ГГГГ или null", "ended": "ДД.ММ.ГГГГ или null"},
 "mode": "chat | incident | alerts | news",
 "priority": "high | normal | low"}"""

INCIDENT_HINT = ("Название похоже на чат аварии или инцидента. Если это так — заполни incident: что случилось, закрыта "
                 "ли авария, чем кончилось, когда началась и закончилась; summary — 1–2 предложения. Иначе incident = null.")

TOPIC_SCHEMA = """{"summary": "1–3 предложения: о чём эта тема и кто в ней основные участники",
 "use": "за чем пользователю следить в этой теме или что можно пропускать",
 "priority": "high | normal | low"}"""

PERSON_SCHEMA = """{"summary": "1–3 предложения: кто этот человек по переписке — чем занимается, в какой команде, роль",
 "knows": ["в чём разбирается, системы и темы — до 8"],
 "asks": "с какими вопросами он обычно приходит / о чём спрашивает других",
 "answers": "на какие вопросы он сам отвечает другим, в чём ему доверяют",
 "with_user": "о чём он общается с пользователем (автор «Я») и с чем может прийти к нему — или null"}"""

ROLE_SCHEMA = """{"role": "1–2 предложения: роль этого человека в этом чате — чем занимается здесь, что берёт на себя,
кого и о чём спрашивает"}"""

INCIDENT_RE = re.compile(r"(?i)авари|инцидент|incident|\bINC[-_ ]?\d|сбой|outage|\bP[0-3]\b|\bSEV[-_ ]?\d")


# ---------------------------------------------------------------- переписка для модели
def _days_ago(date: str | None) -> int | None:
    if not date:
        return None
    try:
        return max(0, (datetime.now(timezone.utc) - parse_iso(date)).days)
    except ValueError:
        return None


def _sample(store: Store, chat_id: int, budget: int, topic: int | None = None, head: int = 25) -> list[Message]:
    """Начало (для аварий важно, с чего началось) и конец переписки — в пределах бюджета символов."""
    t, ta = store.topic_sql(topic)
    rows = store.db.execute(f"SELECT * FROM messages WHERE chat_id=?{t} ORDER BY msg_id DESC LIMIT 400",
                            (chat_id, *ta)).fetchall()
    tail = [store._msg(r) for r in reversed(rows)]
    first = [store._msg(r) for r in store.db.execute(
        f"SELECT * FROM messages WHERE chat_id=?{t} ORDER BY msg_id LIMIT ?", (chat_id, *ta, head)).fetchall()]
    ids = {m.msg_id for m in tail}
    first = [m for m in first if m.msg_id not in ids]
    out: list[Message] = []
    size = 0
    for m in reversed(tail):
        size += len(m.text) + 40
        if size > budget * 0.8:
            break
        out.append(m)
    out.reverse()
    keep_first = []
    for m in first:
        size += len(m.text) + 40
        if size > budget:
            break
        keep_first.append(m)
    return keep_first + out


def _lines(cfg, store: Store, chat_id: int, msgs: list[Message]) -> str:
    tz = get_tz(cfg.digest.get("timezone"))
    media = store.media_of(chat_id, [m.msg_id for m in msgs]) if msgs else {}
    lines = transcript_lines(msgs, tz, None, media, mine_targets(store, chat_id, msgs))
    return "\n".join(x[:700] for x in lines)


def _authors(store: Store, chat_id: int, topic: int | None = None, n: int = 10) -> str:
    t, ta = store.topic_sql(topic)
    rows = store.db.execute(f"""SELECT sender, COUNT(*) AS k FROM messages WHERE chat_id=? AND is_out=0{t}
                                AND sender IS NOT NULL AND sender!='' GROUP BY sender_id ORDER BY k DESC LIMIT ?""",
                            (chat_id, *ta, n)).fetchall()
    return ", ".join(f"{r['sender']} ({r['k']})" for r in rows) or "—"


def _clean(x) -> str:
    if x is None:
        return ""
    if isinstance(x, list):
        return ", ".join(str(i).strip() for i in x if str(i).strip())
    s = str(x).strip()
    return "" if s.lower() in ("null", "none", "нет", "-", "—") else s


# ---------------------------------------------------------------- тексты описаний
def chat_text(d: dict, last_days: int | None) -> str:
    inc = d.get("incident") if isinstance(d.get("incident"), dict) else None
    parts = []
    if inc and _clean(inc.get("what")):
        st = _clean(inc.get("status"))
        when = ""
        if _clean(inc.get("started")):
            when = f" (с {_clean(inc['started'])}" + (f" по {_clean(inc['ended'])})" if _clean(inc.get("ended")) else ")")
        ago = f"; последнее сообщение {last_days} дн. назад" if last_days is not None else ""
        parts.append(f"Чат аварии/инцидента{when}: {_clean(inc['what'])}. Статус: {st or 'неясно'}{ago}.")
        if _clean(inc.get("outcome")):
            parts.append(f"Итог: {_clean(inc['outcome'])}.")
        if _clean(d.get("summary")) and _clean(d.get("summary")) not in parts[0]:
            parts.append(_clean(d["summary"]))
        return " ".join(parts)
    if _clean(d.get("summary")):
        parts.append(_clean(d["summary"]))
    if _clean(d.get("topics")):
        parts.append("Темы: " + _clean(d["topics"]) + ".")
    if _clean(d.get("user_role")):
        parts.append("Ваша роль: " + _clean(d["user_role"]).rstrip(".") + ".")
    if _clean(d.get("use")):
        parts.append("Как использовать: " + _clean(d["use"]).rstrip(".") + ".")
    if last_days is not None and last_days > 60:
        parts.append(f"Последняя активность — {last_days} дн. назад.")
    return " ".join(parts)


def person_text(d: dict) -> str:
    parts = []
    if _clean(d.get("summary")):
        parts.append(_clean(d["summary"]))
    if _clean(d.get("knows")):
        parts.append("Разбирается: " + _clean(d["knows"]) + ".")
    if _clean(d.get("asks")):
        parts.append("Обычно спрашивает: " + _clean(d["asks"]).rstrip(".") + ".")
    if _clean(d.get("answers")):
        parts.append("Отвечает другим: " + _clean(d["answers"]).rstrip(".") + ".")
    if _clean(d.get("with_user")):
        parts.append("С вами: " + _clean(d["with_user"]).rstrip(".") + ".")
    return " ".join(parts)


# ---------------------------------------------------------------- исполнитель
class Describer:
    """Описывает цели по одной (параллельно — workers), после каждой сохраняет результат через save(...)."""

    def __init__(self, cfg, store: Store, llm: LLMClient, save: Callable[[str, tuple, dict], None],
                 progress: Progress = print, cancel: Callable[[], bool] | None = None,
                 fetch: Callable[[int, int | None], int] | None = None, sources: list[str] | None = None):
        self.cfg, self.store, self.llm = cfg, store, llm
        from . import plugins
        self.sources = [k for k in (sources or []) if k in plugins.KINDS and plugins.enabled(cfg, k)]
        self._src_off: set[str] = set()
        self.save, self.progress = save, progress
        self.cancel = cancel or (lambda: False)
        self.fetch = fetch
        from . import privacy
        privacy.attach(llm, cfg, store)
        me = json.loads(store.kv_get("me") or "{}")
        self.me_id = me.get("id")
        self.system = SYSTEM.format(profile=prompts.profile_block(cfg.profile, me.get("name", "")))
        self.budget = max(6000, min(int((llm.context_tokens - 3000) * 2.2), 24000))
        self.stats = {"done": 0, "skipped": 0, "failed": 0}
        self.errors: list[str] = []
        self._lock = threading.Lock()

    def _ask(self, user: str) -> dict:
        """Запрос к модели — в рабочем потоке, без обращений к базе (соединение SQLite — одно на поток окна)."""
        raw = self.llm.complete(self.system, user, max_tokens=1200)
        try:
            d = extract_json(raw)
        except ValueError:
            d = extract_json(self.llm.complete("Ты исправляешь JSON.", prompts.repair_prompt(raw), max_tokens=1200))
        if not isinstance(d, dict):
            raise ValueError("модель вернула не объект")
        return d

    # -------------------------------------------------------- чат
    def chat(self, chat_id: int) -> dict | None:
        c = self.store.get_chat(chat_id)
        if c is None:
            return None
        n = self.store.db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
        if n < 20 and self.fetch is not None:
            try:
                n += self.fetch(chat_id, None)
            except Exception as e:  # noqa: BLE001 — нет связи: описываем по тому, что есть
                log.info("чат не догружен: %s", type(e).__name__)
        msgs = _sample(self.store, chat_id, self.budget)
        if not msgs:
            return {"skip": "в базе нет переписки этого чата (выгрузите историю или подключитесь к Telegram)"}
        rule = self.cfg.chat_rule(c.title, c.username, chat_id, c.kind or "group")
        last = self.store.db.execute("SELECT MAX(date) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
        last = max(str(last or ""), str(self._top_date(chat_id) or "")) or None
        days = _days_ago(last)
        hint = INCIDENT_HINT if INCIDENT_RE.search(c.title or "") or rule["kind"] == "incident" else \
            "Если это чат аварии или инцидента — заполни incident, иначе incident = null."
        ext = self._ext_chat(c, msgs)
        user = (f"Чат «{c.title}» ({KIND_RU.get(c.kind, c.kind)}). Сообщений в базе: {n}. "
                f"Последнее сообщение: {(last or '?')[:10]}" + (f" ({days} дн. назад)" if days is not None else "") + ".\n"
                f"Описание пользователя: {rule['description'] or '—'}\n"
                f"Чаще всего пишут: {_authors(self.store, chat_id)}\n{hint}\n\n"
                f"Переписка (начало и последние сообщения):\n{_lines(self.cfg, self.store, chat_id, msgs)}\n\n"
                + (f"{ext}\n\n" if ext else "") + f"Опиши чат. Схема ответа (строго JSON):\n{CHAT_SCHEMA}")
        def done(d: dict) -> dict:
            mode = str(d.get("mode") or "").lower()
            prio = str(d.get("priority") or "").lower()
            return {"text": chat_text(d, days), "mode": mode if mode in ("chat", "incident", "alerts", "news") else "",
                    "priority": prio if prio in ("high", "normal", "low") else ""}
        return {"user": self._gl(user), "done": done}

    # -------------------------------------------------------- другие источники (плагины Maverick)
    def _guard(self, kind: str, fn):
        """Запрос к плагину: недоступный один раз — больше в этой задаче не спрашиваем."""
        from . import plugins
        if kind in self._src_off:
            return []
        try:
            return fn() or []
        except plugins.PluginOffline as e:
            self._src_off.add(kind)
            self.progress(f"  {plugins.SHORT.get(kind, kind)}: не подключён — пропускаю ({str(e)[:100]})")
        except (LLMError, OSError, ValueError) as e:
            self._src_off.add(kind)
            self.progress(f"  {plugins.SHORT.get(kind, kind)}: {str(e)[:120]}")
        return []

    def _ext_chat(self, c, msgs: list[Message]) -> str:
        """Что о чате известно из вики, ITSM, трекера, почты, Rooms, встреч — строками для запроса."""
        from . import plugins
        if not self.sources:
            return ""
        text = "\n".join(m.text or "" for m in msgs)
        lines: list[str] = []
        if "wiki" in self.sources:
            lines += [plugins.item_line(x) for x in self._guard("wiki", lambda: plugins.chats_lookup(
                self.cfg, self.store, [{"title": c.title, "username": c.username}], progress=self.progress))]
        if "itsm" in self.sources:
            nums = plugins.ticket_numbers(self.cfg, text, self.store)[:8]
            if nums:
                lines += [plugins.item_line(x) for x in self._guard("itsm", lambda: plugins.itsm_lookup(
                    self.cfg, self.store, nums, progress=self.progress)) if x.get("found")]
        if "tracker" in self.sources:
            keys = plugins.task_keys(self.cfg, text, self.store)[:8]
            if keys:
                lines += [plugins.item_line(x) for x in self._guard("tracker", lambda: plugins.tracker_lookup(
                    self.cfg, self.store, keys, progress=self.progress)) if x.get("found")]
        for kind in ("mail", "rooms", "meet"):  # поиск по теме — один запрос на чат: только при небольшом выборе
            if kind in self.sources and getattr(self, "heavy_ok", True):
                q = f"чат «{c.title}»: о чём он, кто в нём, последние события"
                lines += [plugins.item_line(x) for x in self._guard(kind, lambda k=kind: plugins.search(
                    self.cfg, self.store, k, q, progress=self.progress))[:5]]
        lines = [x[:600] for x in lines if x]
        return ("Что известно о чате из других источников (вики, ITSM, трекер, почта, Rooms, встречи — используй, "
                "но переписка главнее):\n" + "\n".join(lines[:16])) if lines else ""

    def _ext_person(self, user_id: int, name: str) -> tuple[str, dict | None]:
        """Карточка сотрудника из вики и адресной книги: строка для запроса и сама карточка (для полей человека)."""
        from . import people as P
        from . import plugins
        kinds = [k for k in ("wiki", "mail") if k in self.sources]
        if not kinds:
            return "", None
        p = P.all_people(self.cfg).get(str(user_id)) or {}
        r = self.store.db.execute("SELECT username FROM members WHERE user_id=? AND username IS NOT NULL LIMIT 1",
                                  (user_id,)).fetchone() if self._has_members() else None
        username = p.get("username") or (r[0] if r else "")
        got = []
        for kind in kinds:
            if got:
                break
            got = self._guard(kind, lambda k=kind: plugins.people_lookup(
                self.cfg, self.store, [{"name": name, "username": username}], kinds=(k,), progress=self.progress))
        if not got:
            return "", None
        card = got[0]["data"]
        return "Карточка сотрудника (вики / адресная книга):\n" + plugins.item_line(got[0])[:800], card

    def _prefetch(self, targets: list[tuple]) -> None:
        """Вики и адресная книга — пачками заранее (10 чатов / 10 человек в одном запросе), дальше — из кеша."""
        from . import plugins
        if "wiki" in self.sources:
            chats = []
            for t in targets:
                if t[0] == "chat":
                    c = self.store.get_chat(t[1])
                    if c:
                        chats.append({"title": c.title, "username": c.username})
            for i in range(0, len(chats), 10):
                if self.cancel() or "wiki" in self._src_off:
                    break
                self._guard("wiki", lambda part=chats[i:i + 10]: plugins.chats_lookup(self.cfg, self.store, part,
                                                                                   progress=self.progress))
        kinds = [k for k in ("wiki", "mail") if k in self.sources]
        if kinds:
            from . import people as P
            pp = P.all_people(self.cfg)
            ps = [{"name": t[2] if len(t) > 2 else "", "username": (pp.get(str(t[1])) or {}).get("username") or ""}
                  for t in targets if t[0] == "person"]
            ps = [p for p in ps if p["name"] or p["username"]]
            for i in range(0, len(ps), 10):
                if self.cancel():
                    break
                self._guard(kinds[0], lambda part=ps[i:i + 10]: plugins.people_lookup(
                    self.cfg, self.store, part, kinds=tuple(k for k in kinds if k not in self._src_off),
                    progress=self.progress))

    def _has_members(self) -> bool:
        return bool(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='members'").fetchone())

    def _gl(self, user: str) -> str:
        from .glossary import block_for
        return prompts.with_glossary(block_for(self.store, user, 2000, scope="describe"), user)

    def _top_date(self, chat_id: int) -> str | None:
        r = self.store.db.execute("SELECT top_date FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return r[0] if r else None

    # -------------------------------------------------------- тема
    def topic(self, chat_id: int, topic_id: int, title: str = "") -> dict | None:
        c = self.store.get_chat(chat_id)
        t, ta = self.store.topic_sql(topic_id)
        n = self.store.db.execute(f"SELECT COUNT(*) FROM messages WHERE chat_id=?{t}", (chat_id, *ta)).fetchone()[0]
        if n < 15 and self.fetch is not None:
            try:
                n += self.fetch(chat_id, topic_id)
            except Exception as e:  # noqa: BLE001
                log.info("тема не догружена: %s", type(e).__name__)
        msgs = _sample(self.store, chat_id, int(self.budget * 0.7), topic_id, head=15)
        if not msgs:
            return {"skip": "в базе нет сообщений этой темы"}
        ext = ""
        if "wiki" in self.sources and c is not None:
            from . import plugins
            got = self._guard("wiki", lambda: plugins.chats_lookup(
                self.cfg, self.store, [{"title": f"{c.title} — {title or topic_id}"}], progress=self.progress))
            ext = ("Из вики: " + "; ".join(plugins.item_line(x)[:500] for x in got) + "\n") if got else ""
        user = (f"Чат-форум «{c.title if c else chat_id}», тема «{title or topic_id}». Сообщений в базе: {n}.\n" + ext +
                f"Чаще всего пишут: {_authors(self.store, chat_id, topic_id)}\n\n"
                f"Переписка темы:\n{_lines(self.cfg, self.store, chat_id, msgs)}\n\n"
                f"Опиши тему. Схема ответа (строго JSON):\n{TOPIC_SCHEMA}")
        def done(d: dict) -> dict:
            text = " ".join(x for x in (_clean(d.get("summary")),
                                        ("Как использовать: " + _clean(d.get("use")).rstrip(".") + ".")
                                        if _clean(d.get("use")) else "") if x)
            prio = str(d.get("priority") or "").lower()
            return {"text": text, "priority": prio if prio in ("high", "normal", "low") else ""}
        return {"user": self._gl(user), "done": done}

    # -------------------------------------------------------- человек: по всем чатам
    def person(self, user_id: int, name: str = "") -> dict | None:
        rows = self.store.db.execute(
            """SELECT m.*, c.title AS chat_title FROM messages m LEFT JOIN chats c ON c.chat_id=m.chat_id
               WHERE m.sender_id=? AND m.is_out=0 ORDER BY m.date DESC LIMIT 300""", (user_id,)).fetchall()
        if not rows:
            ext, card = self._ext_person(user_id, name) if name else ("", None)
            if card:  # писем нет, но карточка нашлась — сохраняем её без описания ИИ
                return {"card_only": card}
            return {"skip": "в базе нет его сообщений"}
        name = name or rows[0]["sender"] or str(user_id)
        tz = get_tz(self.cfg.digest.get("timezone"))
        lines, size, chats = [], 0, set()
        for r in rows:
            m = self.store._msg(r)
            target = ""
            if m.reply_to:
                t = self.store.db.execute("SELECT sender, is_out, text FROM messages WHERE chat_id=? AND msg_id=?",
                                          (m.chat_id, m.reply_to)).fetchone()
                if t:
                    who = "Я" if t["is_out"] else (t["sender"] or "?")
                    target = f" (в ответ {who}: «{(t['text'] or '')[:140]}»)"
            dt = parse_iso(m.date).astimezone(tz)
            line = f"[{dt:%d.%m.%Y}] «{r['chat_title'] or m.chat_id}»: {m.text[:400]}{target}"
            size += len(line)
            if size > self.budget * 0.7:
                break
            lines.append(line)
            chats.add(m.chat_id)
        lines.reverse()
        # что отвечали ему и о чём спрашивали его
        to_him = self.store.db.execute(
            """SELECT r.sender, r.is_out, r.text, c.title AS chat_title FROM messages r
               JOIN messages m ON m.chat_id=r.chat_id AND m.msg_id=r.reply_to AND m.sender_id=?
               LEFT JOIN chats c ON c.chat_id=r.chat_id ORDER BY r.date DESC LIMIT 60""", (user_id,)).fetchall()
        rep, size2 = [], 0
        for r in to_him:
            who = "Я" if r["is_out"] else (r["sender"] or "?")
            line = f"«{r['chat_title'] or ''}» {who}: {(r['text'] or '')[:250]}"
            size2 += len(line)
            if size2 > self.budget * 0.25:
                break
            rep.append(line)
        total = self.store.db.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM messages WHERE sender_id=?",
                                      (user_id,)).fetchone()
        ext, card = self._ext_person(user_id, name)
        user = (f"Человек: {name}. Его сообщений в базе: {total[0]} в {total[1]} чатах.\n"
                + (f"{ext}\n" if ext else "") + "\n"
                "Его сообщения (дата, чат, текст; в скобках — на что он отвечал):\n" + "\n".join(lines) +
                ("\n\nЧто отвечали ему или спрашивали у него:\n" + "\n".join(rep) if rep else "") +
                f"\n\nОпиши этого человека для пользователя. Схема ответа (строго JSON):\n{PERSON_SCHEMA}")
        return {"user": self._gl(user), "done": lambda d: {"text": person_text(d), "card": card}}

    # -------------------------------------------------------- человек: роль в чате
    def role(self, user_id: int, chat_id: int, name: str = "") -> dict | None:
        c = self.store.get_chat(chat_id)
        rows = self.store.db.execute("""SELECT * FROM messages WHERE chat_id=? AND sender_id=? ORDER BY msg_id DESC
                                        LIMIT 120""", (chat_id, user_id)).fetchall()
        if not rows:
            return {"skip": "в этом чате он ничего не писал (в базе)"}
        name = name or rows[0]["sender"] or str(user_id)
        mine = [self.store._msg(r) for r in reversed(rows)]
        ctx_ids = {m.reply_to for m in mine if m.reply_to}
        ctx = [self.store._msg(r) for r in self.store.rows_by_ids(chat_id, ctx_ids)] if ctx_ids else []
        msgs = sorted({m.msg_id: m for m in mine + ctx}.values(), key=lambda m: m.msg_id)
        text = _lines(self.cfg, self.store, chat_id, msgs)[-int(self.budget * 0.8):]
        user = (f"Чат «{c.title if c else chat_id}». Человек: {name}.\n"
                f"Его сообщения в этом чате (и сообщения, на которые он отвечал):\n{text}\n\n"
                f"Схема ответа (строго JSON):\n{ROLE_SCHEMA}")
        return {"user": self._gl(user), "done": lambda d: {"text": _clean(d.get("role"))}}

    # -------------------------------------------------------- всё по списку
    def _prepare(self, t: tuple) -> dict | None:
        kind = t[0]
        if kind == "chat":
            return self.chat(t[1])
        if kind == "topic":
            return self.topic(t[1], t[2], t[3] if len(t) > 3 else "")
        if kind == "person":
            return self.person(t[1], t[2] if len(t) > 2 else "")
        return self.role(t[1], t[2], t[3] if len(t) > 3 else "")

    def run(self, targets: list[tuple], workers: int = 2) -> dict:
        """targets: ("chat", id) | ("topic", chat, topic, title) | ("person", uid, name) | ("role", uid, chat, name).

        База читается и пишется только в этом потоке; параллельно идут лишь запросы к модели."""
        from concurrent.futures import FIRST_COMPLETED, wait
        total = len(targets)
        n = 0
        self.heavy_ok = total <= 30
        if self.sources and not self.heavy_ok and set(self.sources) & {"mail", "rooms", "meet"}:
            self.progress("Почта, Rooms и встречи для описаний чатов — только при выборе до 30 (иначе слишком много запросов "
                          "к Maverick); вики, ITSM и трекер — для всех.")
        self._prefetch(targets)
        label = {"chat": "чат", "topic": "тема", "person": "человек", "role": "роль"}

        def finish(t: tuple, prep: dict, fut) -> None:
            nonlocal n
            n += 1
            try:
                res = prep["done"](fut.result())
            except (LLMError, ValueError) as e:
                self.stats["failed"] += 1
                self.errors.append(f"{label[t[0]]} {t[1]}: {e}")
                self.progress(f"[{n}/{total}] ✗ {label[t[0]]}: {str(e)[:160]}")
                if getattr(e, "fatal", False):
                    raise
                return
            if not res.get("text") and not res.get("card"):
                self.stats["skipped"] += 1
                self.progress(f"[{n}/{total}] — {label[t[0]]}: пустой ответ")
                return
            self.save(t[0], t, res)
            self.stats["done"] += 1
            self.progress(f"[{n}/{total}] ✓ {label[t[0]]}: {self._title(t)}")

        pending: list = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for t in targets:
                if self.cancel():
                    break
                prep = self._prepare(t)
                if prep and prep.get("card_only"):
                    n += 1
                    self.save(t[0], t, {"text": "", "card": prep["card_only"]})
                    self.stats["done"] += 1
                    self.progress(f"[{n}/{total}] ✓ {label[t[0]]}: {self._title(t)} — карточка из вики/почты")
                    continue
                if not prep or prep.get("skip"):
                    n += 1
                    self.stats["skipped"] += 1
                    self.progress(f"[{n}/{total}] — {label[t[0]]}: {(prep or {}).get('skip') or 'нечего описывать'}")
                    continue
                pending.append((t, prep, ex.submit(inflight.carry(self._ask), prep["user"])))
                while len(pending) >= max(1, workers):
                    wait([f for _, _, f in pending], return_when=FIRST_COMPLETED)
                    for item in [x for x in pending if x[2].done()]:
                        pending.remove(item)
                        finish(*item)
            for item in pending:
                finish(item[0], item[1], item[2])
        return dict(self.stats, errors=self.errors[:20], total=total)

    def _title(self, t: tuple) -> str:
        if t[0] == "chat":
            c = self.store.get_chat(t[1])
            return c.title if c else str(t[1])
        if t[0] == "topic":
            return str(t[3] if len(t) > 3 else t[2])
        return str(t[2] if t[0] == "person" and len(t) > 2 else (t[3] if len(t) > 3 else t[1]))


def stamp() -> str:
    return iso(datetime.now(timezone.utc))
