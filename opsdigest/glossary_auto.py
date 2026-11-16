"""Глоссарий пополняется сам: каждый термин из всего, что разбирает программа, — в очередь, очередь — моделью.

Сбор (note) — без модели, на этом компьютере: после каждой сводки (переписка периода, сводки чатов), ИИ в чате и
ассистента (вопрос, контекст, ответ), ответов плагинов Maverick (письма, карточки ITSM, статьи вики) выписываются
кандидаты в термины: сокращения (ITSM, SLA, ЦОД), названия систем латиницей (Grafana, KeyCloak), слова с цифрами
(k8s, S3), короткие названия в «кавычках». Уже известные глоссарию термины и синонимы, имена людей, номера заявок,
общие слова — не берутся. Для каждого кандидата хранится, сколько раз и где встретился, и до 4 примеров употребления.

Обработка (process) — моделью, фоновой задачей «Глоссарий: обработать очередь» (сама после сводки, если включено
glossary.auto_process, или кнопкой): по каждому термину собираются примеры из Telegram (поиск по базе), письма и
карточки ITSM из кеша, статьи вики (плагин Confluence, если подключён) — и модель решает:
  * термин ясен → статья-черновик: что это, значения и как их различать, примеры, ссылки на вики, иллюстрации
    (описанные изображения, где он встречается);
  * не ясен → вопрос пользователю (с вариантами), ответ пользователя уточняет статью;
  * не термин → «не термин», больше не предлагается.
Пользователь может заполнить любой термин очереди вручную или отметить «не термин».
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Callable

from . import logs
from .storage import Store, iso

log = logs.get("glossary")
Progress = Callable[[str], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS gl_pending (
    key         TEXT PRIMARY KEY,           -- термин в нижнем регистре, ё→е
    term        TEXT NOT NULL,
    count       INTEGER DEFAULT 0,          -- в скольких текстах встретился
    sources     TEXT,                       -- JSON: {"telegram": 3, "mail": 1, …}
    contexts    TEXT,                       -- JSON: [{"src", "text", "ref"}] — до 4 примеров
    status      TEXT DEFAULT 'pending',     -- pending | done | question | ignored | failed
    attempts    INTEGER DEFAULT 0,
    note        TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_gl_pending_status ON gl_pending(status, count);
"""

SRC_RU = {"telegram": "Telegram", "digest": "сводка", "mail": "почта", "itsm": "ITSM", "wiki": "вики",
          "assistant": "ассистент", "chat_ai": "ИИ в чате", "user": "вручную", "rooms": "X5 Rooms",
          "tracker": "трекер", "meet": "встречи", "feedback": "отзыв"}

# общие слова и сокращения, которые не нужно объяснять
COMMON = set("""ok ок ид id it ит pc пк usb http https url uri www ip tcp udp dns ssl tls json xml html css js api
cpu ram ssd hdd gb mb kb tb гб мб кб тб os ос vpn wifi wi-fi lan wan sms pdf doc docx xls xlsx ppt pptx txt csv zip rar
utf utf-8 iso gmt utc msk мск рф ru en eng рус faq fyi asap pm am tbd n/a nb ps зп ип ооо оао пао зао нии гост
ну да нет чат бот тг tg vs etc ii iii iv vi vii viii ix xi xii тк т.к т.е тд т.д т.п см стр
the and for you not are but all any can has have was were with this that from your our out get set new old yes no
hello thanks please sorry error errors warning info debug test tests prod dev stage ok? okay done fix fixed bug bugs
null none true false todo admin user users server servers client clients service services file files log logs""".split())

_TOKEN = re.compile(r"(?<![\w@/.#-])([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9]*(?:[-_.+][A-Za-zА-Яа-яЁё0-9]+)*)(?![\w@-])")
_QUOTED = re.compile(r"[«\"]([A-Za-zА-Яа-яЁё][^«»\"\n]{1,38}?)[»\"]")
_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\w+|[A-Za-z]:\\\S+|(?:/[\w.-]+){2,}")
_HAS_CYR = re.compile(r"[А-Яа-яЁё]")


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower().replace("ё", "е"))


def _known(store: Store) -> set[str]:
    from .glossary import Glossary, norm
    g = Glossary(store)
    out = set()
    for e in g.all(True):
        out.add(e["key"])
        out |= {norm(a) for a in e.get("aliases") or []}
    return out


def _people(cfg, store: Store) -> set[str]:
    """Слова, которые — имена людей (из реестра обезличивания): их в глоссарий не берём."""
    try:
        from .privacy import build_registry
        reg = build_registry(cfg, store)
        out = set()
        for p in (getattr(reg, "people", {}) or {}).values():
            names = [p.display, p.first, p.last, *p.usernames, *p.nicks, *p.first_forms, *p.last_forms, *p.logins]
            for f in names:
                for w in re.findall(r"\w+", str(f or "").lower()):
                    if len(w) >= 3:
                        out.add(w.replace("ё", "е"))
        return out
    except Exception:  # noqa: BLE001 — без реестра просто не фильтруем имена
        return set()


def candidates(text: str, *, numbers=None, quoted: bool = True) -> list[tuple[str, str]]:
    """Кандидаты в термины из текста: [(термин, пример употребления)]. Без модели.
    quoted=False — не брать названия в «кавычках» (в найденном плагинами так оформлены темы писем и статей)."""
    text = text or ""
    clean = _URL.sub(" ", text)
    cyr = bool(_HAS_CYR.search(clean))
    out: dict[str, tuple[str, str]] = {}

    def ctx(i: int, j: int) -> str:
        a = max(0, clean.rfind("\n", 0, i) + 1, i - 140)
        b = clean.find("\n", j)
        b = min(b if b >= 0 else len(clean), j + 140)
        return clean[a:b].strip()[:300]

    for m in (_QUOTED.finditer(clean) if quoted else ()):
        t = m.group(1).strip(" .,:;")
        if 2 <= len(t) <= 38 and len(t.split()) <= 4 and not re.fullmatch(r"[\d\W]+", t):
            out.setdefault(_norm(t), (t, ctx(m.start(), m.end())))
    for m in _TOKEN.finditer(clean):
        w = m.group(1)
        lw = _norm(w)
        if len(w) < 2 or len(w) > 40 or lw in COMMON or lw.isdigit():
            continue
        letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", w)
        if not letters:
            continue
        is_abbr = 2 <= len(letters) <= 10 and letters.isupper() and len(w) <= 14
        is_camel = bool(re.search(r"[a-zа-яё][A-ZА-ЯЁ]", w)) and len(w) >= 4
        is_digit_mix = bool(re.search(r"\d", w)) and len(w) <= 16 and (
            len(letters) >= 2 or bool(re.fullmatch(r"[A-ZА-ЯЁ]{1,4}\d{1,2}", w)))
        is_latin_in_ru = cyr and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9.+_-]{2,}", w)) and len(letters) >= 4
        if not (is_abbr or is_camel or is_digit_mix or is_latin_in_ru):
            continue
        if is_digit_mix and re.fullmatch(r"[A-Za-z]{1,5}[-_]?\d{3,}", w):  # номера заявок и задач — это ITSM, не термины
            continue
        if re.fullmatch(r"v?\d+(?:\.\d+)+", lw) or re.fullmatch(r"\d+[a-zа-я]{1,3}", lw):  # версии, «10мин», «5gb»
            continue
        if numbers and any(rx.fullmatch(w) for _e, rx in numbers):
            continue
        out.setdefault(lw, (w, ctx(m.start(), m.end())))
    return list(out.values())[:80]


def note(cfg, store: Store, text: str, source: str, ref: str | None = None, *, known: set | None = None,
         people: set | None = None) -> int:
    """Выписать термины из текста в очередь. Возвращает, сколько новых кандидатов добавлено. Ошибки — молча."""
    if not text or not text.strip():
        return 0
    try:
        if cfg is not None and ((cfg.get("glossary") or {}).get("auto_collect", True) is False):
            return 0
        ensure(store)
        from .plugins import number_patterns
        found = candidates(text, numbers=number_patterns(cfg) if cfg is not None else None,
                           quoted=source not in ("mail", "itsm", "wiki", "rooms", "tracker", "meet"))
        if not found:
            return 0
        known = known if known is not None else _known(store)
        people = people if people is not None else (_people(cfg, store) if cfg is not None else set())
        now = iso(datetime.now(timezone.utc))
        added = 0
        for term, example in found:
            key = _norm(term)
            if key in known or key in people:
                continue
            r = store.db.execute("SELECT * FROM gl_pending WHERE key=?", (key,)).fetchone()
            if r is None:
                store.db.execute("""INSERT INTO gl_pending(key, term, count, sources, contexts, first_seen, last_seen)
                                    VALUES (?,?,?,?,?,?,?)""",
                                 (key, term, 1, json.dumps({source: 1}), json.dumps(
                                     [{"src": source, "text": example, "ref": ref}], ensure_ascii=False), now, now))
                added += 1
                continue
            srcs = json.loads(r["sources"] or "{}")
            srcs[source] = srcs.get(source, 0) + 1
            ctxs = json.loads(r["contexts"] or "[]")
            if len(ctxs) < 4 and example and all(c.get("text") != example for c in ctxs):
                ctxs.append({"src": source, "text": example, "ref": ref})
            store.db.execute("UPDATE gl_pending SET count=count+1, sources=?, contexts=?, last_seen=? WHERE key=?",
                             (json.dumps(srcs), json.dumps(ctxs, ensure_ascii=False), now, key))
        store.db.commit()
        return added
    except Exception as e:  # noqa: BLE001 — пополнение глоссария не должно ломать сводку и ответы
        log.info("очередь глоссария не пополнена: %s", logs.err_info(e))
        return 0


def note_many(cfg, store: Store, items: list[tuple[str, str, str | None]]) -> int:
    """Несколько текстов сразу: [(текст, источник, ссылка)] — словари известного считаются один раз."""
    if not items:
        return 0
    try:
        ensure(store)
        known, people = _known(store), _people(cfg, store)
    except Exception:  # noqa: BLE001
        return 0
    return sum(note(cfg, store, t, s, r, known=known, people=people) for t, s, r in items if t)


# ---------------------------------------------------------------- очередь для окна
def queue(store: Store, status: str = "pending", limit: int = 300, q: str = "") -> list[dict]:
    ensure(store)
    args: list = [status]
    where = "status=?"
    if q:
        where += " AND key LIKE ?"
        args.append(f"%{_norm(q)}%")
    rows = store.db.execute(f"SELECT * FROM gl_pending WHERE {where} ORDER BY count DESC, last_seen DESC LIMIT ?",
                            (*args, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d.get("sources") or "{}")
        d["contexts"] = json.loads(d.get("contexts") or "[]")
        out.append(d)
    return out


def counts(store: Store) -> dict:
    ensure(store)
    return {r[0]: r[1] for r in store.db.execute("SELECT status, COUNT(*) FROM gl_pending GROUP BY status")}


def set_status(store: Store, key: str, status: str, note_: str = "") -> None:
    ensure(store)
    store.db.execute("UPDATE gl_pending SET status=?, note=?, done_at=? WHERE key=?",
                     (status, note_[:300], iso(datetime.now(timezone.utc)), _norm(key)))
    store.db.commit()


# ---------------------------------------------------------------- обработка моделью
def _sources(cfg) -> list[str]:
    """Где искать значения терминов: glossary.sources (по умолчанию — вики и почта)."""
    v = (cfg.get("glossary") or {}).get("sources")
    return [str(x) for x in v] if isinstance(v, list) else ["wiki", "mail"]


RULES = """Ты ведёшь глоссарий инженера технической поддержки (2-я линия): внутренние сервисы, системы и модули,
команды, процессы (ITSM, problem management, дежурства), типы заявок, жаргон и сокращения. Тебе дают кандидатов в
термины с примерами, где они встретились (Telegram, почта, ITSM, X5 Rooms, трекер задач, расшифровки встреч), и
найденное в вики (Confluence). Правила и поправки пользователя (если есть) — важнее твоих догадок.
Для каждого кандидата реши:
  * is_term=false — если это не термин (обычное слово, имя человека, опечатка, кусок кода, мусор);
  * clear=true — если по примерам и вики ясно, что это: дай краткое определение, значения (если их несколько — как
    различать), 1–3 примера употребления (своими словами или из примеров), ссылки на статьи вики, если они есть;
  * clear=false — если неясно или спорно: сформулируй вопрос пользователю и 2–4 варианта ответа, если можешь
    предположить; определение тогда — только то, что известно точно (или пусто).
Ничего не выдумывай. Отвечай только JSON."""

SCHEMA_OUT = """{"terms": [{"term": "как в списке", "is_term": true, "clear": true,
  "kind": "service | app | module | team | person | api | process | entity | instruction | problem | abbr | other",
  "summary": "одна строка: что это", "aliases": ["другие написания"],
  "senses": [{"meaning": "значение", "how_to_tell": "как понять, что речь о нём", "owner": "кто отвечает или пусто"}],
  "examples": ["пример употребления"], "links": [{"title": "статья вики", "url": "https://…"}],
  "question": "вопрос пользователю, если clear=false", "options": ["вариант 1", "вариант 2"]}]}"""


def _evidence(cfg, store: Store, item: dict, limit: int = 4) -> list[str]:
    """Примеры употребления: сохранённые + поиск по Telegram + письма/ITSM из кеша."""
    lines = [f"[{SRC_RU.get(c.get('src'), c.get('src'))}] {c.get('text')}" for c in item.get("contexts") or []
             if c.get("text")]
    try:
        q = '"' + item["term"].replace('"', "") + '"'
        for m, title in store.search(q, limit=limit):
            t = re.sub(r"\s+", " ", m.text or "")[:240]
            if t and all(t[:80] not in x for x in lines):
                lines.append(f"[Telegram, «{title}»] {t}")
    except Exception:  # noqa: BLE001 — FTS не понял запрос
        pass
    try:
        from .plugins import item_line, search_items
        for x in search_items(store, [item["term"]], limit=3):
            lines.append(item_line(x)[:400])
    except Exception:  # noqa: BLE001
        pass
    return lines[:8]


def process(cfg, store: Store, llm, *, limit: int = 30, min_count: int = 1, keys: list[str] | None = None,
            use_wiki: bool | None = None, progress: Progress = print, cancel: Callable[[], bool] | None = None) -> dict:
    """Обработать очередь: статьи-черновики, вопросы пользователю, «не термин»."""
    from . import privacy
    from .glossary import Glossary
    from .llm import LLMError
    from .llm.base import extract_json
    ensure(store)
    cancel = cancel or (lambda: False)
    gcfg = cfg.get("glossary") or {}
    use_wiki = gcfg.get("use_wiki", True) is not False if use_wiki is None else use_wiki
    batch = max(1, min(int(gcfg.get("process_batch") or 6), 12))
    if keys:
        items = [x for x in queue(store, "pending", 2000) + queue(store, "failed", 500) + queue(store, "question", 500)
                 if x["key"] in {_norm(k) for k in keys}]
    else:
        items = [x for x in queue(store, "pending", 2000) if x["count"] >= min_count][:limit]
    stats = {"terms": len(items), "done": 0, "questions": 0, "ignored": 0, "failed": 0, "wiki": 0}
    if not items:
        return stats
    privacy.attach(llm, cfg, store)
    g = Glossary(store)
    wiki_ok = None
    src_ok: dict[str, bool] = {}
    for i in range(0, len(items), batch):
        if cancel():
            break
        part = items[i:i + batch]
        names = [x["term"] for x in part]
        progress(f"Глоссарий: {i + 1}–{i + len(part)} из {len(items)}: {', '.join(names)[:120]}")
        wiki_lines: dict[str, str] = {}
        if use_wiki and "wiki" in _sources(cfg) and wiki_ok is not False:
            from . import plugins
            if plugins.enabled(cfg, "wiki"):
                try:
                    found = plugins.wiki_lookup(cfg, store, names, progress=progress)
                    wiki_ok = True
                    for x in found:
                        wiki_lines[_norm(x["data"].get("term") or x["ref"])] = plugins.item_line(x)
                    stats["wiki"] += len(found)
                except plugins.PluginOffline as e:
                    wiki_ok = False
                    progress(f"  вики недоступна: {e}")
                except (LLMError, OSError, ValueError) as e:
                    wiki_ok = False
                    progress(f"  вики не ответила: {str(e)[:160]}")
        usage: dict[str, list[str]] = {}  # другие источники: почта, Rooms, трекер, встречи, ITSM (glossary.sources)
        from . import plugins
        for kind in [k for k in _sources(cfg) if k != "wiki" and plugins.enabled(cfg, k) and src_ok.get(k) is not False]:
            try:
                for t, d in plugins.terms_usage(cfg, store, kind, names, progress=progress).items():
                    bits = [d.get("meaning") or ""] + [str(e) for e in (d.get("examples") or [])[:2]]
                    usage.setdefault(t, []).append(f"[{plugins.SHORT[kind]}] " + "; ".join(b for b in bits if b)[:400])
                src_ok[kind] = True
            except plugins.PluginOffline as e:
                src_ok[kind] = False
                progress(f"  {plugins.SHORT[kind]} недоступен: {str(e)[:120]}")
            except (LLMError, OSError, ValueError) as e:
                src_ok[kind] = False
                progress(f"  {plugins.SHORT[kind]} не ответил: {str(e)[:160]}")
        blocks = []
        for x in part:
            ev = _evidence(cfg, store, x) + usage.get(x["term"].lower(), [])
            w = wiki_lines.get(x["key"])
            blocks.append(f"### {x['term']} (встречался {x['count']} раз: "
                          + ", ".join(f"{SRC_RU.get(k, k)} {v}" for k, v in x["sources"].items()) + ")\n"
                          + "\n".join("- " + e for e in ev) + (f"\nВики: {w}" if w else "\nВики: не найдено или не искали"))
        from .glossary import block_for
        known = block_for(store, "\n".join(blocks), 1500, scope="glossary")
        user = ((known + "\n\n" if known else "") + "Кандидаты в термины:\n\n" + "\n\n".join(blocks) +
                f"\n\nСхема ответа (строго JSON):\n{SCHEMA_OUT}")
        try:
            data = extract_json(llm.complete(RULES, user, max_tokens=2600))
        except (LLMError, ValueError, OSError) as e:
            stats["failed"] += len(part)
            for x in part:
                store.db.execute("UPDATE gl_pending SET attempts=attempts+1, status=CASE WHEN attempts>=2 THEN 'failed' "
                                 "ELSE status END, note=? WHERE key=?", (str(e)[:300], x["key"]))
            store.db.commit()
            progress(f"  ✗ {str(e)[:160]}")
            continue
        got = {}
        for t in (data.get("terms") if isinstance(data, dict) else data) or []:
            if isinstance(t, dict) and t.get("term"):
                got[_norm(t["term"])] = t
        for x in part:
            t = got.get(x["key"])
            if t is None:
                store.db.execute("UPDATE gl_pending SET attempts=attempts+1 WHERE key=?", (x["key"],))
                continue
            refs = [[int(c["ref"].split(":")[0]), int(c["ref"].split(":")[1])] for c in x["contexts"]
                    if re.fullmatch(r"-?\d+:\d+", str(c.get("ref") or ""))][:5]
            if t.get("is_term") is False:
                set_status(store, x["key"], "ignored", "не термин (ИИ)")
                stats["ignored"] += 1
                continue
            entry = {"term": x["term"], "kind": t.get("kind"), "summary": t.get("summary") or "",
                     "aliases": t.get("aliases") or [], "senses": t.get("senses") or [], "refs": refs,
                     "examples": t.get("examples") or [], "links": t.get("links") or []}
            try:
                e = None
                if t.get("clear", True) is not False or entry["summary"]:
                    e = g.save(entry, merge=bool(g.get(x["term"])), source="auto")
                    imgs = [im["media"] for im in g.images_for(e, 3)]
                    if imgs and not e.get("images"):
                        g.save(dict(e, images=imgs), source="auto")
                if t.get("clear", True) is False:
                    ctx = "; ".join(c.get("text", "")[:160] for c in x["contexts"][:2])
                    g.ask(x["term"], t.get("question") or f"Что означает «{x['term']}»?", t.get("options"),
                          f"автоматически, встречался {x['count']} раз: {ctx}", refs)
                    set_status(store, x["key"], "question", "нужен ответ пользователя")
                    stats["questions"] += 1
                else:
                    set_status(store, x["key"], "done", "статья-черновик")
                    stats["done"] += 1
            except ValueError as err:
                set_status(store, x["key"], "failed", str(err))
                stats["failed"] += 1
        store.db.commit()
    progress(f"Глоссарий: новых статей {stats['done']}, вопросов к вам {stats['questions']}, "
             f"не термины {stats['ignored']}" + (f", ошибок {stats['failed']}" if stats["failed"] else ""))
    return stats
