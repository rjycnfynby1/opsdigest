"""Источники через корпоративного агента Maverick: почта, ITSM (инциденты, проблемы, изменения), Confluence (вики).

Плагины подключаются в чате Maverick, и бывает, что подключение «забывается» (истёк вход, нужно заново дать
согласие). Поэтому перед обращением к плагину программа проверяет, отвечает ли он (удачная проверка помнится
plugins.check_minutes минут). Если плагин не отвечает, выводы на пустом ответе не строятся: источник помечается
«не подключён», окно показывает, что ответил Maverick (часто там ссылка для повторного входа), а переподключает
плагин пользователь сам в чате Maverick. Логины и пароли программа в Maverick не передаёт.

Найденное (письма, карточки инцидентов, статьи вики) хранится в базе (таблица ext_items — шифруется вместе с базой):
для поиска «везде», ассистента и глоссария; повторный запрос по тому же номеру берётся из кеша.
Всё, что уходит в Maverick, проходит обезличивание (privacy.attach), как любые запросы к внешней модели.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from . import logs
from .storage import Store, iso, parse_iso

log = logs.get("plugins")
Progress = Callable[[str], None]

KINDS = ("mail", "wiki", "rooms", "tracker", "meet")
TITLES = {"mail": "Почта", "itsm": "ITSM (инциденты и проблемы)", "wiki": "Confluence (вики)",
          "rooms": "X5 Rooms (мессенджер)", "tracker": "xTracker (задачи и разработка)",
          "meet": "Scriber AI (расшифровки встреч)"}
SHORT = {"mail": "Почта", "itsm": "ITSM", "wiki": "Вики", "rooms": "Rooms", "tracker": "Трекер", "meet": "Встречи"}
ICONS = {"mail": "✉", "itsm": "🎫", "wiki": "📚", "rooms": "💭", "tracker": "🗂", "meet": "🎙"}
WHAT = {"mail": "почты (Outlook / Exchange)", "itsm": "ITSM (инциденты, проблемы, изменения)",
        "wiki": "Confluence (база знаний, вики)", "rooms": "мессенджера X5 Rooms (чаты, каналы, личные сообщения)",
        "tracker": "трекера задач и разработки xTracker (задачи, баги, спринты)",
        "meet": "Scriber AI (записи и расшифровки встреч)",
        "any": "почты, ITSM, X5 Rooms, трекера задач xTracker, Scriber AI и Confluence — какие подходят"}
PROBE = {"mail": "найди одно самое последнее письмо во входящих и назови только дату его получения",
         "itsm": "найди любой один инцидент (например, самый последний) и назови только его номер",
         "wiki": "найди любую одну страницу Confluence и назови только её заголовок",
         "rooms": "найди одно самое последнее сообщение в любом чате и назови только название чата и дату",
         "tracker": "найди любую одну задачу (например, последнюю обновлённую) и назови только её ключ",
         "meet": "найди одну последнюю встречу с расшифровкой и назови только её название и дату"}
ABOUT = {"mail": "Письма за период, поиск писем по теме, адресная книга (должности и контакты коллег).",
         "itsm": "Карточки инцидентов, проблем и изменений по номерам; похожие записи.",
         "wiki": "Статьи базы знаний: термины, сервисы, инструкции, описания чатов, люди и должности.",
         "rooms": "Сообщения в чатах и каналах X5 Rooms за период, поиск по теме, упоминания вас.",
         "tracker": "Задачи и баги по ключам (ABC-123), ваши задачи за период, поиск похожих.",
         "meet": "Встречи за период: итоги, решения, поручения вам; поиск по расшифровкам."}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,          -- mail | itsm | wiki | rooms | tracker | meet | person | chatinfo
    ref         TEXT NOT NULL,          -- номер (INC…), письмо (хеш темы и даты), статья (адрес) или термин
    title       TEXT,
    date        TEXT,
    status      TEXT,
    data        TEXT,                   -- JSON целиком, как вернул Maverick
    text        TEXT,                   -- всё одной строкой — для поиска
    found       INTEGER DEFAULT 1,      -- 0: Maverick ответил, что не нашёл
    fetched_at  TEXT,
    UNIQUE(source, ref)
);
CREATE INDEX IF NOT EXISTS ix_ext_items_src ON ext_items(source, fetched_at);
"""

TOOLS_PREAMBLE = ("Это запрос программы OpsDigest от имени пользователя (инженер 2-й линии поддержки). Для ответа "
                  "используй подключённый плагин {what}{name}. Ничего не выдумывай: только то, что вернул плагин. "
                  "Если плагин не подключён, не отвечает или требует входа/согласия — так прямо и напиши (со ссылкой "
                  "для подключения, если она есть). Ответ — строго в запрошенном формате.")

NEGATIVE = re.compile(
    r"(?i)не\s*подключ|нет\s+доступа|не\s+удалось\s+(?:подключ|получить\s+доступ|авториз)|требует(?:ся)?\s+"
    r"(?:вход|авториз|подключ|согласи|повторн)|авторизуй|войдите|выполните\s+вход|дайте\s+согласие|"
    r"переподключ|не\s+настроен|недоступ|истек|истёк|недействител|unauthori[sz]ed|not\s+connected|no\s+access|"
    r"forbidden|sign\s*in|log\s*in\s+required|consent|reconnect|expired|plugin\s+(?:is\s+)?(?:disabled|unavailable)")
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


class PluginOffline(RuntimeError):
    """Плагин Maverick сейчас не отвечает — переподключить в чате Maverick."""

    def __init__(self, kind: str, status: dict):
        self.kind, self.status = kind, status
        detail = (status.get("detail") or "").strip()
        super().__init__(f"{TITLES.get(kind, kind)}: плагин Maverick не подключён — переподключите его в чате "
                         f"Maverick" + (f" (ответ: {detail[:200]})" if detail else ""))


# ---------------------------------------------------------------- настройки
def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()


def available(cfg) -> bool:
    return "maverick" in (((cfg.get("llm") or {}).get("profiles")) or {})


def plugin_cfg(cfg, kind: str) -> dict:
    p = (cfg.get("plugins") or {}).get(kind) or {}
    return p if isinstance(p, dict) else {}


def enabled(cfg, kind: str) -> bool:
    if kind == "itsm":
        return False
    return available(cfg) and plugin_cfg(cfg, kind).get("enabled", True) is not False


def client(cfg, store: Store | None, kind: str):
    """Отдельный клиент Maverick для запросов к плагину: своя вводная («используй плагин…») и поля запроса."""
    if kind == "itsm":
        raise ValueError("ITSM через Maverick отключён. Используйте выгрузки RPA.")
    from . import privacy
    from .llm import make_client
    name, prof = cfg.llm_profile("maverick")
    prof = copy.deepcopy(prof)
    pc = plugin_cfg(cfg, kind) if kind in KINDS else {}
    nm = str(pc.get("name") or "").strip()
    pre = TOOLS_PREAMBLE.format(what=WHAT.get(kind, kind), name=f" «{nm}»" if nm else "")
    hint = str(pc.get("hint") or "").strip()
    extra = ""
    if store is not None:  # опорные ссылки пользователя и его правила поиска (навык)
        try:
            from . import skill
            extra = skill.plugin_context(store, kind)
        except Exception as e:  # noqa: BLE001 — навык не должен ломать запросы к плагинам
            log.info("навык не подставлен в запрос к плагину: %s", logs.err_info(e))
    prof["preamble"] = pre + ("\n" + hint if hint else "") + ("\n" + extra if extra else "")
    body = pc.get("body")
    if isinstance(body, dict) and body:
        prof["extra_body"] = body
    prof.setdefault("retries", 1)
    llm = make_client(name, prof)
    privacy.attach(llm, cfg, store)
    return llm


# ---------------------------------------------------------------- проверка подключения
def _statuses(store: Store) -> dict:
    try:
        v = json.loads(store.kv_get("plugin_status") or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def statuses(cfg, store: Store) -> dict:
    """Состояние каждого источника для окна: enabled, state (ok | offline | error | unknown), at, detail, links."""
    st = _statuses(store)
    out = {}
    for k in KINDS:
        s = dict(st.get(k) or {"state": "unknown"})
        s.update({"kind": k, "title": TITLES[k], "enabled": enabled(cfg, k), "configured": available(cfg)})
        out[k] = s
    return out


def _save_status(store: Store, kind: str, status: dict) -> None:
    st = _statuses(store)
    st[kind] = status
    store.kv_set("plugin_status", json.dumps(st, ensure_ascii=False))


def parse_check(text: str) -> tuple[str, str]:
    """Ответ Maverick на проверку → (ok | offline | unknown, пояснение)."""
    from .llm.base import extract_json
    t = (text or "").strip()
    if not t:
        return "offline", "пустой ответ"
    try:
        data = extract_json(t)
    except ValueError:
        data = None
    if isinstance(data, dict) and "connected" in data:
        ok = data.get("connected")
        ok = ok if isinstance(ok, bool) else str(ok).strip().lower() in ("true", "да", "yes", "1")
        why = str(data.get("reason") or data.get("detail") or data.get("sample") or "").strip()
        if ok and not NEGATIVE.search(str(data.get("reason") or "")):
            return "ok", why
        return "offline", why or "плагин сообщил, что не подключён"
    if NEGATIVE.search(t):
        return "offline", t[:400]
    return "ok", t[:200]


def check(cfg, store: Store, kind: str, *, force: bool = False, progress: Progress | None = None) -> dict:
    """Проверить, отвечает ли плагин (с кешем на plugins.check_minutes). Возвращает состояние."""
    now = datetime.now(timezone.utc)
    cur = _statuses(store).get(kind) or {}
    minutes = float((cfg.get("plugins") or {}).get("check_minutes") or 30)
    if not force and cur.get("state") == "ok" and cur.get("at"):
        try:
            if now - parse_iso(cur["at"]) < timedelta(minutes=minutes):
                return cur
        except ValueError:
            pass
    if not available(cfg):
        return {"state": "error", "detail": "Maverick не настроен (шаг «Модель»)", "at": iso(now)}
    if progress:
        progress(f"Проверяю подключение плагина: {TITLES[kind]}…")
    from .llm import LLMError
    try:
        llm = client(cfg, store, kind)
        llm.retries = 0
        ans = llm.complete("", f"Служебная проверка подключения. Через плагин {WHAT[kind]} {PROBE[kind]}. "
                               'Ответь строго одной строкой JSON: {"connected": true или false, "sample": "что нашёл", '
                               '"reason": "если не подключён — почему и как подключить (ссылка)"}', max_tokens=300)
        state, detail = parse_check(ans)
    except (LLMError, OSError, ValueError, KeyError) as e:
        state, detail = "error", str(e)[:300]
    status = {"state": state, "detail": detail, "links": URL_RE.findall(detail)[:3], "at": iso(now)}
    _save_status(store, kind, status)
    log.info("плагин %s: %s", kind, state)
    if progress:
        progress(f"  {TITLES[kind]}: " + {"ok": "подключён ✓", "offline": "не подключён ✗ — переподключите в чате Maverick",
                                          "error": "Maverick не ответил ✗"}.get(state, state))
    return status


def need(cfg, store: Store, kind: str, progress: Progress | None = None) -> None:
    """Перед обращением к плагину: если не отвечает — PluginOffline (выводы на пустом ответе не строим)."""
    if not enabled(cfg, kind):
        raise PluginOffline(kind, {"state": "disabled", "detail": "источник выключен в настройках"})
    st = check(cfg, store, kind, progress=progress)
    if st.get("state") != "ok":
        raise PluginOffline(kind, st)


def mark_failed(store: Store, kind: str, detail: str) -> None:
    """Запрос к плагину вернул «не подключён» — следующая проверка будет настоящей, окно покажет предупреждение."""
    _save_status(store, kind, {"state": "offline", "detail": detail[:400], "links": URL_RE.findall(detail)[:3],
                               "at": iso(datetime.now(timezone.utc))})


def _ask(cfg, store: Store, kind: str, task: str, *, max_tokens: int = 2500, progress: Progress | None = None,
         allow: list[str] | None = None, images: bool = True):
    """Запрос к плагину с проверкой подключения. Ответ — JSON (dict) или текст, если JSON не получился.

    allow — строки, которые уходят без обезличивания (имя и логин человека при поиске его карточки).
    images — скачать и описать изображения из ответа (вложения Maverick и ссылки в JSON) — см. ext_images."""
    from .llm.base import extract_json
    need(cfg, store, kind, progress)
    llm = client(cfg, store, kind)
    if allow:
        llm.privacy_allow = list(allow)
    if images and image_settings(cfg)["enabled"]:
        task += IMAGES_ASK
    raw = llm.complete("", task, max_tokens=max_tokens)
    try:
        data = extract_json(raw)
    except ValueError:
        data = None
    if not isinstance(data, (dict, list)) and NEGATIVE.search(raw or ""):
        mark_failed(store, kind, raw or "")
        raise PluginOffline(kind, {"state": "offline", "detail": raw[:300]})
    if images and isinstance(data, (dict, list)):
        try:
            ext_images(cfg, store, llm, kind, data, getattr(llm, "last_data", None), progress=progress)
        except Exception as e:  # noqa: BLE001 — картинки не должны ломать ответ плагина
            log.info("изображения из ответа плагина не обработаны: %s", logs.err_info(e))
    return data if data is not None else (raw or "")


# ---------------------------------------------------------------- кеш найденного
def _norm_ref(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


def save_item(store: Store, source: str, ref: str, data: dict, *, title: str = "", date: str = "",
              status: str = "", found: bool = True) -> None:
    ensure(store)
    text = " ".join(str(v) for v in _flat(data) if v)[:20000]
    store.db.execute(
        """INSERT INTO ext_items(source, ref, title, date, status, data, text, found, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source, ref) DO UPDATE SET title=excluded.title, date=excluded.date, status=excluded.status,
           data=excluded.data, text=excluded.text, found=excluded.found, fetched_at=excluded.fetched_at""",
        (source, ref, title[:500], date[:40], status[:80], json.dumps(data, ensure_ascii=False), text, int(found),
         iso(datetime.now(timezone.utc))))
    store.db.commit()


def _flat(x) -> Iterable:
    if isinstance(x, dict):
        for v in x.values():
            yield from _flat(v)
    elif isinstance(x, list):
        for v in x:
            yield from _flat(v)
    elif x is not None:
        yield x


def cached(store: Store, source: str, ref: str, max_age_hours: float | None = None) -> dict | None:
    ensure(store)
    r = store.db.execute("SELECT * FROM ext_items WHERE source=? AND ref=?", (source, ref)).fetchone()
    if not r:
        return None
    if max_age_hours is not None:
        try:
            if datetime.now(timezone.utc) - parse_iso(r["fetched_at"]) > timedelta(hours=max_age_hours):
                return None
        except (TypeError, ValueError):
            return None
    return item_view(r)


def item_view(r) -> dict:
    try:
        data = json.loads(r["data"] or "{}")
    except ValueError:
        data = {}
    return {"id": r["id"], "source": r["source"], "ref": r["ref"], "title": r["title"] or "", "date": r["date"] or "",
            "status": r["status"] or "", "found": bool(r["found"]), "fetched_at": r["fetched_at"], "data": data}


def search_items(store: Store, terms: list[str], source: str | None = None, limit: int = 20) -> list[dict]:
    """Поиск по сохранённым письмам, карточкам ITSM и статьям вики (без запроса к Maverick)."""
    ensure(store)
    terms = [t for t in dict.fromkeys(x.strip().lower() for x in terms) if len(t) >= 2][:12]
    if not terms:
        return []
    where = " OR ".join("LOWER(text) LIKE ? OR LOWER(ref) LIKE ? OR LOWER(title) LIKE ?" for _ in terms)
    args: list = []
    for t in terms:
        args += [f"%{t}%"] * 3
    src = " AND source=?" if source else ""
    rows = store.db.execute(f"SELECT * FROM ext_items WHERE found=1 AND ({where}){src} ORDER BY fetched_at DESC LIMIT 400",
                            (*args, *([source] if source else []))).fetchall()
    scored = []
    for r in rows:
        blob = f"{r['ref']} {r['title']} {r['text']}".lower()
        scored.append((sum(1 for t in terms if t in blob), r))
    scored.sort(key=lambda x: -x[0])
    return [item_view(r) for _, r in scored[:limit]]


# ---------------------------------------------------------------- номера инцидентов и проблем
def number_patterns(cfg, store: Store | None = None) -> list[tuple[str, re.Pattern]]:
    """Шаблоны номеров ITSM: настройки entities (incident, problem, change) + форматы из глоссария (id_format)."""
    out: list[tuple[str, re.Pattern]] = []
    pats = ((cfg.get("entities") or {}).get("patterns") or {}) if cfg is not None else {}
    for et in ("incident", "problem", "change"):
        if pats.get(et):
            try:
                out.append((et, re.compile(pats[et], re.I)))
            except re.error:
                pass
    if store is not None:
        try:
            from .glossary import Glossary
            for e in Glossary(store).all():
                for s in e.get("senses") or []:
                    f = str(s.get("id_format") or "").strip()
                    if f and len(f) < 120:
                        try:
                            out.append(("glossary", re.compile(r"\b" + f.strip("^$") + r"\b", re.I)))
                        except re.error:
                            continue
        except Exception:  # noqa: BLE001 — глоссарий не должен ломать поиск номеров
            pass
    return out


def ticket_numbers(cfg, text: str, store: Store | None = None, pats=None) -> list[str]:
    seen: dict[str, None] = {}
    for _et, rx in pats if pats is not None else number_patterns(cfg, store):
        for m in rx.finditer(text or ""):
            seen.setdefault(m.group(0).upper().replace("_", "").replace(" ", ""), None)
    return list(seen)


# ---------------------------------------------------------------- ITSM
ITSM_SCHEMA = ('{"items": [{"number": "номер как в ITSM", "found": true, "type": "incident | problem | change | request", '
               '"title": "", "status": "", "priority": "", "service": "сервис/система", "group": "группа исполнителей", '
               '"assignee": "исполнитель", "opened": "дата", "updated": "дата", "description": "суть в 1–3 '
               'предложениях", "resolution": "решение, если есть", "related": ["связанные номера"], '
               '"last_note": "последняя запись в журнале работ"}]}')


def itsm_lookup(cfg, store: Store, numbers: list[str], *, progress: Progress = print, batch: int = 8,
                limit: int = 40) -> list[dict]:
    """Карточки инцидентов/проблем по номерам: из кеша (plugins.itsm.cache_hours) или через плагин ITSM."""
    hours = float(plugin_cfg(cfg, "itsm").get("cache_hours") or 6)
    nums = [n for n in dict.fromkeys(_norm_ref(n) for n in numbers if n)][:limit]
    out, todo = [], []
    for n in nums:
        c = cached(store, "itsm", n, hours)
        (out.append(c) if c else todo.append(n))
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        progress(f"ITSM: запрашиваю {', '.join(part)}…")
        data = _ask(cfg, store, "itsm",
                    "Найди в ITSM карточки этих записей и верни по каждой основные поля: " + ", ".join(part) +
                    f".\nСтрого JSON по схеме:\n{ITSM_SCHEMA}\nЕсли запись не найдена — found=false.", progress=progress)
        items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
        got = set()
        for it in items or []:
            if not isinstance(it, dict):
                continue
            ref = _norm_ref(it.get("number") or "")
            if not ref:
                continue
            got.add(ref)
            save_item(store, "itsm", ref, it, title=str(it.get("title") or ""), date=str(it.get("updated") or it.get("opened") or ""),
                      status=str(it.get("status") or ""), found=it.get("found", True) is not False)
            out.append(cached(store, "itsm", ref))
        for n in part:
            if n not in got:  # ответ без этой записи — не нашли; помним, чтобы не спрашивать каждый раз
                save_item(store, "itsm", n, {"number": n, "found": False}, found=False)
    return [x for x in out if x]


def itsm_search(cfg, store: Store, query: str, *, progress: Progress = print) -> list[dict]:
    """Похожие инциденты и проблемы по описанию (когда номеров нет)."""
    progress("ITSM: ищу похожие инциденты и проблемы…")
    data = _ask(cfg, store, "itsm", "Найди в ITSM инциденты и проблемы, похожие на это описание (до 8 самых "
                                    f"похожих, сначала свежие):\n«{query.strip()[:2500]}»\nСтрого JSON по схеме:\n{ITSM_SCHEMA}",
                progress=progress)
    items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get("number") and it.get("found", True) is not False:
            ref = _norm_ref(it["number"])
            save_item(store, "itsm", ref, it, title=str(it.get("title") or ""), date=str(it.get("updated") or ""),
                      status=str(it.get("status") or ""))
            out.append(cached(store, "itsm", ref))
    return [x for x in out if x]


# ---------------------------------------------------------------- почта
MAIL_ITEM = ('{"id": "m1", "subject": "тема", "date": "дата", "from": "отправитель", "to": "кому", '
             '"summary": "суть письма в 1–3 предложениях", "numbers": ["INC…"], "needs_reply": false, '
             '"importance": 3, "thread": "к какой переписке/теме относится"}')


def _mail_ref(m: dict) -> str:
    key = f"{m.get('subject') or ''}|{m.get('date') or ''}|{m.get('from') or ''}"
    return "M" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14].upper()


def _save_mail(store: Store, items: list) -> list[dict]:
    out = []
    for m in items or []:
        if not isinstance(m, dict) or not (m.get("subject") or m.get("summary")):
            continue
        ref = _mail_ref(m)
        save_item(store, "mail", ref, m, title=str(m.get("subject") or ""), date=str(m.get("date") or ""))
        v = cached(store, "mail", ref)
        if v:
            v["data"]["id"] = m.get("id") or v["data"].get("id")
            out.append(v)
    return out


def mail_digest(cfg, store: Store, since: datetime, until: datetime, points: list[dict], *,
                progress: Progress = print) -> dict:
    """Почта за период + сверка с пунктами Telegram (они уже очищены: без переписки, имена — коды)."""
    a, b = since.strftime("%d.%m.%Y %H:%M"), until.strftime("%d.%m.%Y %H:%M")
    pts = json.dumps(points, ensure_ascii=False)[:7000]
    progress("Почта: сводка за период и сверка с пунктами Telegram…")
    data = _ask(cfg, store, "mail",
                f"Посмотри письма пользователя за период {a} — {b} (входящие и отправленные).\n"
                "1) Перечисли значимые письма: рабочие темы, события, проблемы, номера инцидентов и задач, что требует "
                "ответа пользователя. Рассылки и уведомления без значения — пропусти. Не больше 30 писем.\n"
                "2) Проверь в почте каждый пункт из Telegram ниже: есть ли письма о том же (по номерам, системам, "
                "событиям, участникам). Имена людей в пунктах заменены кодами — ищи по сути и номерам.\n\n"
                f"Пункты Telegram:\n{pts}\n\nСтрого JSON:\n"
                '{"mail": [' + MAIL_ITEM + '], "matches": [{"tg": "t1", "found": true, "mail": ["m1"], '
                '"detail": "что в почте об этом"}]}', max_tokens=4000, progress=progress)
    if not isinstance(data, dict):
        return {"mail": [], "matches": [], "note": str(data)[:2000]}
    mail = _save_mail(store, data.get("mail") or [])
    matches = [m for m in data.get("matches") or [] if isinstance(m, dict) and m.get("tg")]
    return {"mail": mail, "matches": matches}


def mail_search(cfg, store: Store, query: str, *, period: str = "", progress: Progress = print) -> list[dict]:
    progress("Почта: ищу письма по теме…")
    data = _ask(cfg, store, "mail", f"Найди письма пользователя по теме (до 12 самых подходящих, сначала свежие)"
                                    f"{(' за период ' + period) if period else ''}:\n«{query.strip()[:2500]}»\n"
                                    f'Строго JSON: {{"mail": [{MAIL_ITEM}]}}', progress=progress)
    items = data.get("mail") if isinstance(data, dict) else data if isinstance(data, list) else []
    return _save_mail(store, items)


# ---------------------------------------------------------------- вики
WIKI_ITEM = ('{"term": "термин или тема", "found": true, "title": "заголовок статьи", "url": "ссылка", '
             '"space": "пространство", "summary": "суть в 2–4 предложениях", "owner": "кто отвечает, если указано"}')


def wiki_lookup(cfg, store: Store, terms: list[str], *, context: str = "", progress: Progress = print,
                max_age_hours: float = 72) -> list[dict]:
    """Статьи Confluence по терминам/темам (кеш 3 дня)."""
    terms = [t.strip() for t in dict.fromkeys(terms) if t and t.strip()][:10]
    out, todo = [], []
    for t in terms:
        c = cached(store, "wiki", _norm_ref(t), max_age_hours)
        (out.append(c) if c else todo.append(t))
    if todo:
        progress(f"Вики: ищу {', '.join(todo)[:120]}…")
        data = _ask(cfg, store, "wiki", "Найди в Confluence статьи по этим терминам и темам (для каждого — самую "
                                        "подходящую статью): " + "; ".join(todo) +
                    (f"\nКонтекст, в котором они встретились: «{context.strip()[:1500]}»" if context.strip() else "") +
                    f'\nСтрого JSON: {{"items": [{WIKI_ITEM}]}}\nЕсли статьи нет — found=false.', progress=progress)
        items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
        got = set()
        for it in items or []:
            if not isinstance(it, dict) or not it.get("term"):
                continue
            ref = _norm_ref(it["term"])
            got.add(ref)
            save_item(store, "wiki", ref, it, title=str(it.get("title") or ""), found=it.get("found", True) is not False)
            out.append(cached(store, "wiki", ref))
        for t in todo:
            if _norm_ref(t) not in got:
                save_item(store, "wiki", _norm_ref(t), {"term": t, "found": False}, found=False)
    return [x for x in out if x and x["found"]]


def wiki_search(cfg, store: Store, query: str, *, progress: Progress = print) -> list[dict]:
    progress("Вики: ищу статьи по теме…")
    data = _ask(cfg, store, "wiki", f"Найди в Confluence статьи по теме (до 6 самых подходящих):\n«{query.strip()[:2500]}»"
                                    f'\nСтрого JSON: {{"items": [{WIKI_ITEM}]}}', progress=progress)
    items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get("found", True) is not False and (it.get("title") or it.get("url")):
            ref = _norm_ref(it.get("url") or it.get("title"))
            save_item(store, "wiki", ref, it, title=str(it.get("title") or ""))
            out.append(cached(store, "wiki", ref))
    return [x for x in out if x]


# ---------------------------------------------------------------- для запросов к модели
def item_line(x: dict) -> str:
    """Одна строка о найденном для запроса к модели (без лишних полей)."""
    d = x.get("data") or {}
    if x["source"] == "itsm":
        bits = [d.get("number") or x["ref"], d.get("type"), d.get("title"), f"статус: {d.get('status')}" if d.get("status") else "",
                f"приоритет: {d.get('priority')}" if d.get("priority") else "", f"сервис: {d.get('service')}" if d.get("service") else "",
                f"группа: {d.get('group')}" if d.get("group") else "", f"исполнитель: {d.get('assignee')}" if d.get("assignee") else "",
                f"открыт {d.get('opened')}" if d.get("opened") else "", f"обновлён {d.get('updated')}" if d.get("updated") else "",
                d.get("description"), f"решение: {d.get('resolution')}" if d.get("resolution") else "",
                f"связано: {', '.join(map(str, d.get('related') or []))}" if d.get("related") else "",
                f"последняя запись: {d.get('last_note')}" if d.get("last_note") else ""]
        line = "[ITSM] " + "; ".join(str(b) for b in bits if b)
    elif x["source"] == "mail":
        bits = [d.get("date"), f"от {d.get('from')}" if d.get("from") else "", f"«{d.get('subject')}»" if d.get("subject") else "",
                d.get("summary"), ("номера: " + ", ".join(d.get("numbers") or [])) if d.get("numbers") else "",
                "ждут ответа" if d.get("needs_reply") else ""]
        line = f"[почта {d.get('id') or x['ref']}] " + "; ".join(str(b) for b in bits if b)
    elif x["source"] == "rooms":
        bits = [d.get("date"), f"чат «{d.get('chat')}»" if d.get("chat") else "", f"от {d.get('from')}" if d.get("from") else "",
                d.get("summary") or d.get("text"), ("номера: " + ", ".join(map(str, d.get("numbers") or []))) if d.get("numbers") else "",
                "упомянули вас" if d.get("mention") else "", "ждут ответа" if d.get("needs_reply") else ""]
        line = f"[Rooms {d.get('id') or x['ref']}] " + "; ".join(str(b) for b in bits if b)
    elif x["source"] == "tracker":
        bits = [d.get("key") or x["ref"], d.get("type"), d.get("title"), f"статус: {d.get('status')}" if d.get("status") else "",
                f"приоритет: {d.get('priority')}" if d.get("priority") else "",
                f"исполнитель: {d.get('assignee')}" if d.get("assignee") else "",
                f"спринт: {d.get('sprint')}" if d.get("sprint") else "", f"обновлена {d.get('updated')}" if d.get("updated") else "",
                d.get("description"), f"последний комментарий: {d.get('last_comment')}" if d.get("last_comment") else "",
                f"связано: {', '.join(map(str, d.get('related') or []))}" if d.get("related") else ""]
        line = "[трекер] " + "; ".join(str(b) for b in bits if b)
    elif x["source"] == "meet":
        acts = "; ".join(f"{a.get('who') or '?'}: {a.get('what')}" + (f" (до {a['due']})" if a.get("due") else "")
                         for a in d.get("actions") or [] if isinstance(a, dict) and a.get("what"))
        bits = [d.get("date"), f"«{d.get('title')}»" if d.get("title") else "", d.get("summary"),
                ("решения: " + "; ".join(map(str, d.get("decisions") or []))) if d.get("decisions") else "",
                ("поручения: " + acts) if acts else "", f"вам: {d.get('to_me')}" if d.get("to_me") else ""]
        line = f"[встреча {d.get('id') or x['ref']}] " + "; ".join(str(b) for b in bits if b)
    elif x["source"] == "person":
        c = d.get("contacts") or {}
        cont = ", ".join(str(v) for k in ("email", "phone", "other") for v in (c.get(k) or []) if v) if isinstance(c, dict) else ""
        bits = [d.get("name"), f"@{d['username']}" if d.get("username") else "", d.get("post"), d.get("dept"),
                ("компетенции: " + ", ".join(map(str, d.get("competences") or []))) if d.get("competences") else "",
                f"руководитель: {d.get('manager')}" if d.get("manager") else "", cont]
        line = f"[сотрудник, {d.get('from') or 'вики'}] " + "; ".join(str(b) for b in bits if b)
    elif x["source"] == "chatinfo":
        bits = [d.get("chat"), d.get("purpose"), f"ведёт: {d.get('owner')}" if d.get("owner") else "", d.get("rules"),
                d.get("url")]
        line = "[о чате, вики] " + "; ".join(str(b) for b in bits if b)
    else:
        bits = [d.get("title"), d.get("url"), d.get("summary"), f"отвечает: {d.get('owner')}" if d.get("owner") else ""]
        line = f"[вики: {d.get('term') or x['ref']}] " + "; ".join(str(b) for b in bits if b)
    imgs = [": ".join(x for x in dict.fromkeys((str(i.get("about") or ""), str(i.get("desc") or ""))) if x)
            for i in d.get("images") or [] if isinstance(i, dict)]
    imgs = [i for i in imgs if i]
    if imgs:
        line += " | изображения: " + "; ".join(i[:240] for i in imgs[:4])
    return line


# ---------------------------------------------------------------- X5 Rooms (мессенджер)
ROOMS_ITEM = ('{"id": "r1", "chat": "чат или канал", "date": "дата", "from": "автор", "summary": "суть в 1–2 '
              'предложениях", "numbers": ["INC…", "ABC-123"], "mention": false, "needs_reply": false, "importance": 3}')


def _rooms_ref(m: dict) -> str:
    key = f"{m.get('chat') or ''}|{m.get('date') or ''}|{m.get('from') or ''}|{(m.get('summary') or '')[:60]}"
    return "R" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14].upper()


def _save_list(store: Store, source: str, items, ref_of, title_of, date_key: str = "date") -> list[dict]:
    out = []
    for m in items or []:
        if not isinstance(m, dict) or m.get("found") is False:
            continue
        ref = ref_of(m)
        if not ref:
            continue
        save_item(store, source, ref, m, title=str(title_of(m) or ""), date=str(m.get(date_key) or ""),
                  status=str(m.get("status") or ""))
        v = cached(store, source, ref)
        if v:
            v["data"]["id"] = m.get("id") or v["data"].get("id")
            out.append(v)
    return out


def rooms_digest(cfg, store: Store, since: datetime, until: datetime, points: list[dict], *,
                 progress: Progress = print) -> dict:
    """Сообщения X5 Rooms за период (что важно пользователю) + сверка с пунктами Telegram (очищенными)."""
    a, b = since.strftime("%d.%m.%Y %H:%M"), until.strftime("%d.%m.%Y %H:%M")
    pts = json.dumps(points, ensure_ascii=False)[:6000]
    progress("X5 Rooms: сообщения за период и сверка с Telegram…")
    data = _ask(cfg, store, "rooms",
                f"Посмотри чаты, каналы и личные сообщения пользователя в X5 Rooms за период {a} — {b}.\n"
                "1) Перечисли значимое: рабочие события, проблемы, решения, номера инцидентов и задач, где упомянули "
                "пользователя или ждут его ответа. Флуд, поздравления и автоуведомления без значения — пропусти. Не больше 30.\n"
                "2) Проверь каждый пункт из Telegram ниже: обсуждали ли то же в Rooms (по номерам, системам, событиям). "
                "Имена людей в пунктах заменены кодами — ищи по сути и номерам.\n\n"
                f"Пункты Telegram:\n{pts}\n\nСтрого JSON:\n"
                '{"messages": [' + ROOMS_ITEM + '], "matches": [{"tg": "t1", "found": true, "rooms": ["r1"], '
                '"detail": "что в Rooms об этом"}]}', max_tokens=4000, progress=progress)
    if not isinstance(data, dict):
        return {"messages": [], "matches": [], "note": str(data)[:2000]}
    msgs = _save_list(store, "rooms", data.get("messages") or [], _rooms_ref, lambda m: m.get("chat"))
    return {"messages": msgs, "matches": [m for m in data.get("matches") or [] if isinstance(m, dict) and m.get("tg")]}


def rooms_search(cfg, store: Store, query: str, *, period: str = "", progress: Progress = print) -> list[dict]:
    progress("X5 Rooms: ищу сообщения по теме…")
    data = _ask(cfg, store, "rooms", f"Найди в X5 Rooms сообщения по теме (до 12 самых подходящих, сначала свежие)"
                                     f"{(' за период ' + period) if period else ''}:\n«{query.strip()[:2500]}»\n"
                                     f'Строго JSON: {{"messages": [{ROOMS_ITEM}]}}', progress=progress)
    items = data.get("messages") if isinstance(data, dict) else data if isinstance(data, list) else []
    return _save_list(store, "rooms", items, _rooms_ref, lambda m: m.get("chat"))


# ---------------------------------------------------------------- xTracker (задачи и разработка)
TRACKER_SCHEMA = ('{"items": [{"key": "ключ задачи как в трекере (ABC-123)", "found": true, "type": "задача | баг | '
                  'история | эпик | подзадача", "title": "", "status": "", "priority": "", "assignee": "исполнитель", '
                  '"reporter": "автор", "project": "", "sprint": "", "updated": "дата", "due": "срок", '
                  '"description": "суть в 1–3 предложениях", "last_comment": "последний комментарий", '
                  '"related": ["связанные задачи и инциденты"], "url": "ссылка"}]}')
TASK_STOP = {"UTF", "ISO", "SHA", "AES", "RFC", "TLS", "SSL", "MD", "HTTP", "IPV", "GOST", "ГОСТ", "CVE", "COVID",
             "WIN", "X", "RSA", "SHA1", "BASE", "PEP", "ECMA", "IEEE", "GMT", "UTC", "MSK", "AD", "P", "SEV", "PO", "DDR"}
TASK_RE_DEFAULT = r"\b[A-Z][A-Z0-9]{1,11}-\d{1,7}\b"


def task_keys(cfg, text: str, store: Store | None = None) -> list[str]:
    """Ключи задач трекера (ABC-123) из текста: шаблон entities.patterns.task, без номеров ITSM и «UTF-8»."""
    pats = ((cfg.get("entities") or {}).get("patterns") or {}) if cfg is not None else {}
    try:
        rx = re.compile(pats.get("task") or TASK_RE_DEFAULT)
    except re.error:
        rx = re.compile(TASK_RE_DEFAULT)
    itsm = {n.upper() for n in ticket_numbers(cfg, text, store)}
    out: dict[str, None] = {}
    for m in rx.finditer(text or ""):
        k = m.group(0).upper()
        prefix = k.split("-", 1)[0]
        if prefix in TASK_STOP or k in itsm or re.fullmatch(r"[A-Z]\d*", prefix) and len(prefix) < 2:
            continue
        if re.fullmatch(r"(?:INC|INS|IM|SD|PRB|PM|CHG|RFC|REQ|RITM|TASK)\d*", prefix):
            continue
        out.setdefault(k, None)
    return list(out)


def tracker_lookup(cfg, store: Store, keys: list[str], *, progress: Progress = print, batch: int = 8,
                   limit: int = 40) -> list[dict]:
    """Задачи по ключам: из кеша (plugins.tracker.cache_hours, по умолчанию 6 ч) или через плагин трекера."""
    hours = float(plugin_cfg(cfg, "tracker").get("cache_hours") or 6)
    keys = [k for k in dict.fromkeys(_norm_ref(k) for k in keys if k)][:limit]
    out, todo = [], []
    for k in keys:
        c = cached(store, "tracker", k, hours)
        (out.append(c) if c else todo.append(k))
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        progress(f"Трекер: запрашиваю {', '.join(part)}…")
        data = _ask(cfg, store, "tracker", "Найди в xTracker эти задачи и верни по каждой основные поля: " +
                    ", ".join(part) + f".\nСтрого JSON по схеме:\n{TRACKER_SCHEMA}\nЕсли задача не найдена — found=false.",
                    progress=progress)
        items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
        got = set()
        for it in items or []:
            if not isinstance(it, dict) or not it.get("key"):
                continue
            ref = _norm_ref(it["key"])
            got.add(ref)
            save_item(store, "tracker", ref, it, title=str(it.get("title") or ""), date=str(it.get("updated") or ""),
                      status=str(it.get("status") or ""), found=it.get("found", True) is not False)
            out.append(cached(store, "tracker", ref))
        for k in part:
            if k not in got:
                save_item(store, "tracker", k, {"key": k, "found": False}, found=False)
    return [x for x in out if x]


def tracker_search(cfg, store: Store, query: str, *, progress: Progress = print) -> list[dict]:
    progress("Трекер: ищу похожие задачи…")
    data = _ask(cfg, store, "tracker", "Найди в xTracker задачи и баги, похожие на это описание (до 8 самых "
                                       f"подходящих, сначала свежие):\n«{query.strip()[:2500]}»\n"
                                       f"Строго JSON по схеме:\n{TRACKER_SCHEMA}", progress=progress)
    items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
    return _save_list(store, "tracker", items, lambda it: _norm_ref(it.get("key") or ""), lambda it: it.get("title"),
                      "updated")


def tracker_mine(cfg, store: Store, since: datetime, until: datetime, *, progress: Progress = print) -> list[dict]:
    """Задачи пользователя (исполнитель, автор, наблюдатель, упомянут), изменившиеся за период."""
    a, b = since.strftime("%d.%m.%Y %H:%M"), until.strftime("%d.%m.%Y %H:%M")
    progress("Трекер: ваши задачи, изменившиеся за период…")
    data = _ask(cfg, store, "tracker", f"Найди в xTracker задачи пользователя (он исполнитель, автор, наблюдатель или "
                                       f"его упомянули), которые менялись за период {a} — {b}: новые комментарии, смена "
                                       f"статуса, сроки. Не больше 25, сначала важные.\nСтрого JSON по схеме:\n{TRACKER_SCHEMA}",
                max_tokens=3500, progress=progress)
    items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
    return _save_list(store, "tracker", items, lambda it: _norm_ref(it.get("key") or ""), lambda it: it.get("title"),
                      "updated")


# ---------------------------------------------------------------- Scriber AI (расшифровки встреч)
MEET_ITEM = ('{"id": "s1", "title": "название встречи", "date": "дата и время", "participants": ["участники"], '
             '"summary": "итог в 2–4 предложениях", "decisions": ["решения"], "actions": [{"who": "кто", "what": "что '
             'сделать", "due": "срок"}], "numbers": ["INC…", "ABC-123"], "to_me": "что поручили или ждут от '
             'пользователя — или пусто", "url": "ссылка на запись"}')


def _meet_ref(m: dict) -> str:
    key = f"{m.get('title') or ''}|{m.get('date') or ''}"
    return "S" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14].upper()


def meet_digest(cfg, store: Store, since: datetime, until: datetime, *, progress: Progress = print) -> list[dict]:
    """Встречи пользователя за период: итоги, решения, поручения (особенно ему)."""
    a, b = since.strftime("%d.%m.%Y %H:%M"), until.strftime("%d.%m.%Y %H:%M")
    progress("Scriber AI: встречи за период…")
    data = _ask(cfg, store, "meet", f"Найди в Scriber AI встречи пользователя за период {a} — {b} (где он был "
                                    "участником или организатором) и по каждой расшифровке выпиши итог, решения и "
                                    "поручения — особенно ему. Не больше 15.\n"
                                    f'Строго JSON: {{"meetings": [{MEET_ITEM}]}}', max_tokens=4000, progress=progress)
    items = data.get("meetings") if isinstance(data, dict) else data if isinstance(data, list) else []
    return _save_list(store, "meet", items, _meet_ref, lambda m: m.get("title"))


def meet_search(cfg, store: Store, query: str, *, period: str = "", progress: Progress = print) -> list[dict]:
    progress("Scriber AI: ищу в расшифровках встреч…")
    data = _ask(cfg, store, "meet", "Найди в расшифровках встреч (Scriber AI) обсуждения этой темы (до 8 встреч, "
                                    f"сначала свежие){(' за период ' + period) if period else ''}:\n«{query.strip()[:2500]}»\n"
                                    f'Строго JSON: {{"meetings": [{MEET_ITEM}]}}', progress=progress)
    items = data.get("meetings") if isinstance(data, dict) else data if isinstance(data, list) else []
    return _save_list(store, "meet", items, _meet_ref, lambda m: m.get("title"))


# ---------------------------------------------------------------- единый поиск по источнику
def search(cfg, store: Store, kind: str, query: str, *, period: str = "", progress: Progress = print) -> list[dict]:
    """Поиск по теме в одном источнике (для «найти везде», ассистента, глоссария)."""
    if kind == "mail":
        return mail_search(cfg, store, query, period=period, progress=progress)
    if kind == "itsm":
        nums = ticket_numbers(cfg, query, store)
        return (itsm_lookup(cfg, store, nums, progress=progress) if nums else []) + itsm_search(cfg, store, query, progress=progress)
    if kind == "wiki":
        return wiki_search(cfg, store, query, progress=progress)
    if kind == "rooms":
        return rooms_search(cfg, store, query, period=period, progress=progress)
    if kind == "tracker":
        keys = task_keys(cfg, query, store)
        return (tracker_lookup(cfg, store, keys, progress=progress) if keys else []) + tracker_search(cfg, store, query, progress=progress)
    if kind == "meet":
        return meet_search(cfg, store, query, period=period, progress=progress)
    raise ValueError(f"неизвестный источник: {kind}")


# ---------------------------------------------------------------- люди и чаты по вики и адресной книге
PERSON_SCHEMA = ('{"people": [{"name": "ФИО как в карточке", "username": "логин Telegram без @, если указан", '
                 '"found": true, "post": "должность", "dept": "подразделение / команда / структура", '
                 '"org": "юрлицо или направление", "competences": ["в чём разбирается, за что отвечает"], '
                 '"manager": "руководитель", "contacts": {"email": [], "phone": [], "other": ["Rooms, Telegram…"]}, '
                 '"url": "ссылка на карточку или страницу"}]}')
CHAT_SCHEMA = ('{"chats": [{"chat": "название чата как в запросе", "found": true, "purpose": "зачем нужен чат, что в нём '
               'решают", "owner": "кто ведёт / отвечает", "rules": "правила и регламент, если есть", '
               '"people": ["кто в нём обычно отвечает и за что"], "url": "ссылка на страницу"}]}')


def people_lookup(cfg, store: Store, persons: list[dict], *, kinds: Iterable[str] = ("wiki", "mail"),
                  progress: Progress = print, max_age_hours: float = 24 * 14) -> list[dict]:
    """Карточки сотрудников: должность, подразделение, компетенции, контакты — в вики и адресной книге почты.

    В запрос уходит только имя и логин Telegram (исключение из обезличивания — privacy.lookup_people, по умолчанию
    включено: искать человека, не называя его, нельзя), без переписки. Кеш — 2 недели."""
    allow_names = (cfg.get("privacy") or {}).get("lookup_people", True) is not False
    if not allow_names:
        return []
    out = []
    todo = []
    for p in persons[:20]:
        name = str(p.get("name") or "").strip()
        user = str(p.get("username") or "").strip().lstrip("@")
        if not name and not user:
            continue
        ref = _norm_ref("@" + user if user else name)
        c = cached(store, "person", ref, max_age_hours)
        (out.append(c) if c and c["found"] else todo.append((ref, name, user)) if not c else None)
    for kind in kinds:
        if not todo or not enabled(cfg, kind):
            continue
        where = "в Confluence (страницы команд, оргструктура, контакты, дежурства)" if kind == "wiki" else \
            "в адресной книге почты (Outlook / Exchange: должность, подразделение, телефоны, руководитель)"
        lst = "; ".join(f"{n or '?'}" + (f" (Telegram @{u})" if u else "") for _, n, u in todo)
        progress(f"{SHORT[kind]}: ищу карточки сотрудников ({len(todo)})…")
        allow = [n for _, n, _u in todo if n] + [u for *_, u in todo if u] + [f"@{u}" for *_, u in todo if u]
        try:
            data = _ask(cfg, store, kind, f"Найди {where} карточки этих сотрудников и верни, что о них известно: {lst}.\n"
                                          f"Строго JSON по схеме:\n{PERSON_SCHEMA}\nЕсли не найден — found=false.",
                        progress=progress, allow=allow, images=False)
        except PluginOffline:
            continue
        items = data.get("people") if isinstance(data, dict) else data if isinstance(data, list) else []
        left = []
        for ref, n, u in todo:
            hit = next((it for it in items or [] if isinstance(it, dict) and it.get("found", True) is not False and (
                (u and str(it.get("username") or "").lstrip("@").lower() == u.lower()) or
                (n and _same_person(n, str(it.get("name") or ""))))), None)
            if hit:
                hit = dict(hit, **{"from": SHORT[kind]})
                save_item(store, "person", ref, hit, title=str(hit.get("name") or n))
                out.append(cached(store, "person", ref))
            else:
                left.append((ref, n, u))
        todo = left
    for ref, n, u in todo:  # не нашли нигде — помним, чтобы не спрашивать каждый раз
        save_item(store, "person", ref, {"name": n, "username": u, "found": False}, found=False)
    return [x for x in out if x and x.get("found")]


def _same_person(a: str, b: str) -> bool:
    wa = {w.lower().replace("ё", "е") for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", a)}
    wb = {w.lower().replace("ё", "е") for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", b)}
    return bool(wa and wb) and len(wa & wb) >= min(2, len(wa), len(wb))


def chats_lookup(cfg, store: Store, chats: list[dict], *, progress: Progress = print,
                 max_age_hours: float = 24 * 7) -> list[dict]:
    """Что о чатах Telegram написано в вики: зачем нужен, кто ведёт, правила (кеш — неделя).

    Название чата — не персональные данные; если в нём есть имя человека, оно обезличивается как обычно."""
    out, todo = [], []
    for c in chats[:12]:
        t = str(c.get("title") or "").strip()
        if not t:
            continue
        ref = _norm_ref(t)
        hit = cached(store, "chatinfo", ref, max_age_hours)
        (out.append(hit) if hit else todo.append(c))
    if todo and enabled(cfg, "wiki"):
        lst = "; ".join(f"«{c['title']}»" + (f" (t.me/{c['username']})" if c.get("username") else "") for c in todo)
        progress(f"Вики: ищу описания чатов ({len(todo)})…")
        data = _ask(cfg, store, "wiki", "Найди в Confluence упоминания этих чатов Telegram (страницы команд, регламенты, "
                                        f"дежурства, списки чатов): {lst}.\nСтрого JSON по схеме:\n{CHAT_SCHEMA}\n"
                                        "Если о чате ничего нет — found=false.", progress=progress, images=False)
        items = data.get("chats") if isinstance(data, dict) else data if isinstance(data, list) else []
        for c in todo:
            ref = _norm_ref(c["title"])
            hit = next((it for it in items or [] if isinstance(it, dict) and _norm_ref(it.get("chat") or "") == ref), None)
            if hit and hit.get("found", True) is not False:
                save_item(store, "chatinfo", ref, hit, title=str(c["title"]))
                out.append(cached(store, "chatinfo", ref))
            else:
                save_item(store, "chatinfo", ref, {"chat": c["title"], "found": False}, found=False)
    return [x for x in out if x and x.get("found")]


# ---------------------------------------------------------------- изображения из ответов плагинов
IMAGES_ASK = ('\nЕсли в найденном есть изображения (скриншоты, схемы, вложения) — добавь в соответствующий объект поле '
              '"images": [{"name": "имя файла", "about": "что на изображении, если видишь", "url": "ссылка на файл", '
              '"artifact": "номер вложения Maverick, если есть"}].')
EXT_MEDIA_SQL = """CREATE TABLE IF NOT EXISTS ext_media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    ref         TEXT,
    name        TEXT,
    mime        TEXT,
    size        INTEGER,
    sha256      TEXT,
    path        TEXT,
    about       TEXT,
    status      TEXT,             -- ok | failed | skipped
    detail      TEXT,
    created_at  TEXT,
    UNIQUE(source, sha256)
)"""
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}


def image_settings(cfg) -> dict:
    s = ((cfg.get("plugins") or {}).get("images") or {}) if cfg is not None else {}
    return {"enabled": s.get("enabled", True) is not False, "describe": s.get("describe", True) is not False,
            "max": max(0, min(int(s.get("max") or 4), 12)), "max_mb": float(s.get("max_mb") or 8)}


def _sniff(b: bytes) -> str:
    if b.startswith(b"\x89PNG"):
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:2] == b"BM":
        return "image/bmp"
    return ""


def _image_refs(data, resp) -> list[tuple[dict | None, dict]]:
    """(объект, к которому относится картинка, сведения о картинке) — из JSON ответа и из вложений Maverick."""
    out: list[tuple[dict | None, dict]] = []

    def walk(x, owner):
        if isinstance(x, dict):
            imgs = x.get("images")
            if isinstance(imgs, list):
                for im in imgs:
                    if isinstance(im, dict) and (im.get("url") or im.get("artifact") or im.get("data")):
                        out.append((x, im))
                    elif isinstance(im, str) and (im.startswith(("http", "data:image")) or re.fullmatch(r"art_[0-9a-f]{8,}", im)):
                        out.append((x, {"url": im} if not im.startswith("art_") else {"artifact": im}))
            for k, v in x.items():
                if k != "images":
                    walk(v, x)
        elif isinstance(x, list):
            for v in x:
                walk(v, owner)
    walk(data, None)
    arts = (resp or {}).get("file_artifacts") if isinstance(resp, dict) else None
    first = None
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                first = v[0]
                break
    for a in arts if isinstance(arts, list) else []:
        if isinstance(a, str):
            a = {"artifact": a}
        if isinstance(a, dict):
            aid = a.get("artifact") or a.get("artifact_id") or a.get("id") or a.get("file_id")
            mime = str(a.get("content_type") or a.get("mime_type") or a.get("mime") or "")
            if mime and not mime.startswith("image/"):
                continue
            info = {"artifact": aid, "name": a.get("filename") or a.get("name") or "", "url": a.get("url")
                    or a.get("download_url") or "", "about": a.get("description") or ""}
            holder = first if first is not None else (data if isinstance(data, dict) else None)
            if holder is not None:  # вложение ответа — к первой находке (её покажут вместе с картинкой)
                holder.setdefault("images", []).append(info)
            out.append((first, info))
    return out


def _fetch_image(llm, info: dict, max_bytes: int) -> tuple[bytes, str]:
    import base64
    import urllib.parse
    import urllib.request
    d = str(info.get("data") or "")
    u = str(info.get("url") or "")
    if d or u.startswith("data:image"):
        raw = (d or u).split(",", 1)[-1]
        return base64.b64decode(raw[: max_bytes * 2]), ""
    host = llm.host() if hasattr(llm, "host") else ""
    headers: dict = {}
    if info.get("artifact"):
        u = f"{host}/api/v1/artifacts/{urllib.parse.quote(str(info['artifact']))}/download"
    if not u.startswith(("http://", "https://")):
        raise ValueError("нет адреса")
    if host and u.startswith(host) and hasattr(llm, "_headers"):
        headers = llm._headers()  # вложения Maverick — с его ключом; чужие адреса — без ключа
    req = urllib.request.Request(u, headers=headers)
    ctx = llm._ssl_context() if hasattr(llm, "_ssl_context") else None
    from .llm import inflight
    with inflight.urlopen(req, timeout=60, context=ctx) as r:  # noqa: S310 — адрес из ответа корпоративного агента
        b = r.read(max_bytes + 1)
        if len(b) > max_bytes:
            raise ValueError("слишком большой файл")
        return b, (r.headers.get("Content-Type") or "").split(";")[0]


def ext_images(cfg, store: Store, llm, kind: str, data, resp, *, progress: Progress | None = None) -> int:
    """Скачать изображения из ответа плагина и описать их (как картинки Telegram): описание — в объект ответа
    (поле images[].desc), файл — в media/ext (в защищённой папке — зашифрован), описание — кеш по sha256."""
    s = image_settings(cfg)
    refs = _image_refs(data, resp)[: s["max"]]
    if not refs:
        return 0
    from .secure import files as sfiles
    store.db.execute(EXT_MEDIA_SQL)
    n = 0
    for owner, info in refs:
        try:
            b, mime = _fetch_image(llm, info, int(s["max_mb"] * 2**20))
            mime = _sniff(b) or mime
            if mime not in IMAGE_MIMES:
                raise ValueError(f"не изображение ({mime or '?'})")
        except Exception as e:  # noqa: BLE001 — нет доступа к файлу: остаётся то, что написал плагин
            info["status"] = "failed"
            info["detail"] = str(e)[:120]
            continue
        sha = hashlib.sha256(b).hexdigest()
        rel = f"media/ext/{kind}/{sha[:20]}.{mime.split('/')[-1].replace('jpeg', 'jpg')}"
        path = cfg.path(rel)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            sfiles.write_bytes(path, b)
        store.db.execute("""INSERT INTO ext_media(source, ref, name, mime, size, sha256, path, about, status, created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, sha256) DO UPDATE SET about=excluded.about""",
                         (kind, "", str(info.get("name") or "")[:200], mime, len(b), sha, rel,
                          str(info.get("about") or "")[:800], "ok", iso(datetime.now(timezone.utc))))
        mid = store.db.execute("SELECT id FROM ext_media WHERE source=? AND sha256=?", (kind, sha)).fetchone()[0]
        store.db.commit()
        info.update({"media": mid, "mime": mime, "sha256": sha, "status": "ok"})
        desc = store.description(sha)
        if desc is None and s["describe"]:
            desc = describe_image(cfg, store, b, mime, context=_owner_text(owner), source=kind, progress=progress)
        if desc:
            info["desc"] = " ".join(x for x in (desc.get("summary"), desc.get("text")) if x)[:600]
        n += 1
    return n


def _owner_text(owner) -> str:
    if not isinstance(owner, dict):
        return ""
    return "; ".join(str(owner.get(k)) for k in ("title", "subject", "chat", "key", "number", "summary") if owner.get(k))[:600]


def describe_image(cfg, store: Store, data: bytes, mime: str, *, context: str = "", source: str = "",
                   progress: Progress | None = None) -> dict | None:
    """Описать одно изображение моделью для картинок (media.vision_provider), кеш — image_desc по sha256."""
    from . import privacy, prompts
    from .llm import LLMError
    from .vision import VisionUnavailable, make_vision_llm, parse_description
    sha = hashlib.sha256(data).hexdigest()
    got = store.description(sha)
    if got:
        return got
    try:
        llm = make_vision_llm(cfg)
    except (VisionUnavailable, KeyError, LLMError) as e:
        log.info("изображение не описано: %s", e)
        return None
    privacy.attach(llm, cfg, store)
    if progress:
        progress(f"  описываю изображение из {SHORT.get(source, source) or 'источника'}…")
    user = ("Опиши изображение из рабочего источника " + (SHORT.get(source, source) or "") +
            (f". Откуда оно: {context}" if context else "") + ".\nОпиши изображение. importance: 1 — шум, 3 — рабочая "
            "информация, 5 — авария или ошибка, требующая реакции.\nСхема ответа (строго JSON):\n" + prompts.IMAGE_SCHEMA)
    try:
        raw = llm.complete(prompts.IMAGE_SYSTEM, user, max_tokens=1000, images=[(data, mime)])
    except (LLMError, OSError) as e:
        log.info("изображение не описано: %s", logs.err_info(e))
        return None
    d = parse_description(raw)
    d.update(model=llm.label, created_at=iso(datetime.now(timezone.utc)), _source=source)
    store.save_description(sha, d)
    return d


def ext_media_file(store: Store, mid: int) -> dict | None:
    try:
        store.db.execute(EXT_MEDIA_SQL)
        r = store.db.execute("SELECT * FROM ext_media WHERE id=?", (mid,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return dict(r) if r else None


# ---------------------------------------------------------------- термины в источниках (для глоссария)
def terms_usage(cfg, store: Store, kind: str, terms: list[str], *, progress: Progress = print,
                max_age_hours: float = 72) -> dict[str, dict]:
    """Как термины употребляются в источнике (почта, Rooms, трекер, встречи, ITSM): значение по найденному и примеры.
    Кеш — 3 дня (ext_items, source=<вид>, ref=TERM:<термин>). Возвращает {термин в нижнем регистре: данные}."""
    terms = [t.strip() for t in dict.fromkeys(terms) if t and t.strip()][:10]
    out: dict[str, dict] = {}
    todo = []
    for t in terms:
        c = cached(store, kind, "TERM:" + _norm_ref(t), max_age_hours)
        if c:
            if c["found"]:
                out[t.lower()] = c["data"]
        else:
            todo.append(t)
    if not todo:
        return out
    where = {"mail": "в письмах пользователя", "rooms": "в чатах X5 Rooms", "tracker": "в задачах xTracker",
             "meet": "в расшифровках встреч Scriber AI", "itsm": "в инцидентах и проблемах ITSM",
             "wiki": "в Confluence"}.get(kind, "")
    progress(f"{SHORT.get(kind, kind)}: ищу, как употребляются термины ({len(todo)})…")
    data = _ask(cfg, store, kind, f"Найди {where} употребления этих терминов и сокращений: " + "; ".join(todo) +
                ". Для каждого: что он означает судя по найденному (если понятно) и 1–2 коротких примера.\n"
                'Строго JSON: {"items": [{"term": "как в списке", "found": true, "meaning": "что это или пусто", '
                '"examples": ["пример"], "where": "где встречается (тема письма, задача, встреча)"}]}', progress=progress,
                images=False)
    items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
    got = set()
    for it in items or []:
        if isinstance(it, dict) and it.get("term"):
            ref = "TERM:" + _norm_ref(it["term"])
            ok = it.get("found", True) is not False and bool(it.get("meaning") or it.get("examples"))
            save_item(store, kind, ref, it, title=str(it["term"]), found=ok)
            got.add(ref)
            if ok:
                out[str(it["term"]).lower()] = it
    for t in todo:
        if "TERM:" + _norm_ref(t) not in got:
            save_item(store, kind, "TERM:" + _norm_ref(t), {"term": t, "found": False}, found=False)
    return out
