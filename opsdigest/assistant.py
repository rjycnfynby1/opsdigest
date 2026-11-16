"""Ассистент — отдельный раздел-чат: вопросы без привязки к одному чату, по всем источникам, с историей.

История (таблицы asst_threads / asst_msgs, в базе — шифруется вместе с ней): обычные беседы, вопросы по выделенному
в мессенджере (их можно «развернуть» сюда), запросы ИИ в чате и сводки — всё видно списком слева.

Как отвечает (тот же принцип слоёв, что у общей картины — cross.py):
  1. Telegram: поиск по всем чатам базы (ключевые слова и номера вопроса/выделенного, за выбранный период) +
     выделенное сообщение с соседями. Если подключены другие источники — сначала модель отдельным запросом
     выписывает из переписки только то, что относится к вопросу (обезличенно), и дальше идут только эти выводы.
  2. Плагины Maverick: почта, ITSM (номера — карточки, без номеров — похожие), вики. В Maverick уходит только вопрос,
     выделенный фрагмент и ключевые слова — не переписка.
  3. Ответ: выводы по Telegram + найденное в плагинах + глоссарий + история беседы → модель → ответ со ссылками
     (сообщения Telegram — #номер, письма, номера ITSM, статьи вики).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import logs, plugins, prompts
from .storage import Store, iso, parse_iso

log = logs.get("assistant")
Progress = Callable[[str], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS asst_threads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    kind        TEXT DEFAULT 'free',        -- free | selection | chat | digest | glossary
    meta        TEXT,                       -- JSON: {chat, ids, quote, topic, period, report, sources}
    pinned      INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS asst_msgs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL,
    role        TEXT NOT NULL,              -- user | assistant | note
    text        TEXT,
    data        TEXT,                       -- JSON: refs, sources, found, model, offline
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_asst_msgs_thread ON asst_msgs(thread_id, id);
CREATE TABLE IF NOT EXISTS asst_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER,
    msg_id      INTEGER,                    -- сообщение беседы, к которому приложен (NULL — ещё не отправлен)
    role        TEXT,                       -- user (вложил пользователь) | assistant (файл из ответа, Maverick)
    name        TEXT,
    mime        TEXT,
    size        INTEGER,
    sha256      TEXT,
    path        TEXT,                       -- data/asst/… (в защищённой папке — зашифрован)
    text        TEXT,                       -- текст документа (txt, docx, xlsx…) или описание изображения
    created_at  TEXT
);
"""

KIND_RU = {"free": "беседа", "selection": "по выделенному", "chat": "ИИ в чате", "digest": "сводка", "glossary": "глоссарий",
           "feedback": "отзыв", "maverick": "Maverick", "command": "указание"}

SYSTEM = """Ты — рабочий ассистент инженера технической поддержки (2-я линия). У тебя есть выдержки из его Telegram
(рабочие чаты), его почты, ITSM (инциденты, проблемы, изменения) и вики компании — то, что найдено по вопросу.

О пользователе:
{profile}

Правила:
- Пиши по-русски, по делу. Опирайся только на приведённые данные и историю беседы; чего нет — так и скажи, и подскажи,
  где ещё поискать. Ничего не выдумывай.
- Ссылайся на источники: сообщения Telegram — номером в формате #123 (номер сообщения из выдержки), письма — «письмо
  m1», номера ITSM — как есть (INC…), статьи вики — названием.
- Если источники расходятся (в чате «решено», в ITSM открыт) — отметь это.
- Оформление — простой Markdown: заголовки ###, списки «- », **жирный**. Без таблиц.
- Если вопрос неясен и без уточнения ответ выйдет неверным (непонятно, о какой системе, каком периоде, каком чате) —
  не отвечай наугад: коротко скажи, что нашёл, и задай один уточняющий вопрос блоком
  ```ask {{"question": "…", "options": ["вариант", "…"]}}``` (варианты — если можешь предположить).
- Если пользователь просит что-то изменить в программе (описания чатов, людей, глоссарий, правила) — так и скажи и
  добавь блок ```actions [ … ]``` (формат — как в справке по действиям, если она дана ниже; иначе просто опиши)."""

FINDINGS_TASK = """Выпиши из переписки Telegram ниже всё, что относится к вопросу пользователя: факты, события, решения, кто
чем занимается, номера, статусы, даты — кратко, пунктами, со ссылками на сообщения (#номер). Не отвечай на вопрос
целиком и не делай выводов сверх переписки. Если ничего не относится — напиши «в Telegram ничего по вопросу»."""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()


def _now() -> str:
    return iso(datetime.now(timezone.utc))


# ---------------------------------------------------------------- история
def create(store: Store, title: str, kind: str = "free", meta: dict | None = None) -> int:
    ensure(store)
    now = _now()
    cur = store.db.execute("INSERT INTO asst_threads(title, kind, meta, created_at, updated_at) VALUES (?,?,?,?,?)",
                           ((title or "Новая беседа")[:200], kind, json.dumps(meta or {}, ensure_ascii=False), now, now))
    store.db.commit()
    return int(cur.lastrowid)


def add(store: Store, tid: int, role: str, text: str, data: dict | None = None) -> int:
    ensure(store)
    now = _now()
    cur = store.db.execute("INSERT INTO asst_msgs(thread_id, role, text, data, created_at) VALUES (?,?,?,?,?)",
                           (tid, role, text or "", json.dumps(data or {}, ensure_ascii=False), now))
    store.db.execute("UPDATE asst_threads SET updated_at=? WHERE id=?", (now, tid))
    store.db.commit()
    return int(cur.lastrowid)


def log_exchange(store: Store, kind: str, title: str, question: str, answer: str, data: dict | None = None,
                 meta: dict | None = None) -> int | None:
    """Запрос из другого раздела (ИИ в чате, сводка) — в историю ассистента. Ошибки — молча."""
    try:
        tid = create(store, title, kind, meta)
        if question:
            add(store, tid, "user", question)
        add(store, tid, "assistant", answer, data)
        return tid
    except Exception as e:  # noqa: BLE001
        log.info("история ассистента не записана: %s", logs.err_info(e))
        return None


def threads(store: Store, q: str = "", limit: int = 200) -> list[dict]:
    ensure(store)
    args: list = []
    where = ""
    if q.strip():
        where = ("WHERE LOWER(t.title) LIKE ? OR t.id IN (SELECT thread_id FROM asst_msgs WHERE LOWER(text) LIKE ?)")
        args = [f"%{q.strip().lower()}%"] * 2
    rows = store.db.execute(
        f"""SELECT t.*, (SELECT COUNT(*) FROM asst_msgs m WHERE m.thread_id=t.id) AS n,
                   (SELECT text FROM asst_msgs m WHERE m.thread_id=t.id ORDER BY id DESC LIMIT 1) AS last
            FROM asst_threads t {where} ORDER BY t.pinned DESC, t.updated_at DESC LIMIT ?""", (*args, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        d["last"] = re.sub(r"\s+", " ", d.get("last") or "")[:140]
        d["pinned"] = bool(d.get("pinned"))
        out.append(d)
    return out


def thread(store: Store, tid: int) -> dict | None:
    ensure(store)
    r = store.db.execute("SELECT * FROM asst_threads WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["meta"] = json.loads(d.get("meta") or "{}")
    d["pinned"] = bool(d.get("pinned"))
    d["messages"] = []
    for m in store.db.execute("SELECT * FROM asst_msgs WHERE thread_id=? ORDER BY id", (tid,)).fetchall():
        x = dict(m)
        x["data"] = json.loads(x.get("data") or "{}")
        d["messages"].append(x)
    return d


def update(store: Store, tid: int, *, title: str | None = None, pinned: bool | None = None,
           meta: dict | None = None) -> None:
    ensure(store)
    if title is not None:
        store.db.execute("UPDATE asst_threads SET title=? WHERE id=?", (title.strip()[:200] or "Беседа", tid))
    if pinned is not None:
        store.db.execute("UPDATE asst_threads SET pinned=? WHERE id=?", (int(pinned), tid))
    if meta is not None:
        store.db.execute("UPDATE asst_threads SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), tid))
    store.db.commit()


def delete(store: Store, tid: int) -> None:
    ensure(store)
    store.db.execute("DELETE FROM asst_msgs WHERE thread_id=?", (tid,))
    store.db.execute("DELETE FROM asst_threads WHERE id=?", (tid,))
    store.db.commit()


# ---------------------------------------------------------------- поиск по Telegram
def period_since(period: str, since: str | None, tz) -> str | None:
    now = datetime.now(timezone.utc)
    p = (period or "all").lower()
    if p == "custom" and since:
        try:
            return iso(datetime.strptime(since[:10], "%Y-%m-%d").replace(tzinfo=tz))
        except ValueError:
            return None
    hours = {"day": 24, "3d": 72, "week": 168, "month": 720, "quarter": 2160}.get(p)
    return iso(now - timedelta(hours=hours)) if hours else None


def tg_evidence(cfg, store: Store, text: str, *, since: str | None = None, focus: dict | None = None,
                budget: int = 24000, max_chats: int = 10, only_chat: int | None = None) -> dict:
    """Выдержки из Telegram по вопросу: выделенное (с соседями) + найденное во всех чатах базы."""
    from .digest import get_tz, transcript_lines
    from .messenger.ai import fts, key_terms
    tz = get_tz(cfg.digest.get("timezone"))
    blocks, refs, seen = [], [], set()
    size = 0
    if focus and focus.get("chat") is not None:
        cid = int(focus["chat"])
        ids = [int(i) for i in focus.get("ids") or []][:3]
        msgs = []
        for mid in ids:
            msgs += store.context(cid, mid, before=10, after=6)
        if not ids:
            rows = store.page(cid, limit=40, topic=focus.get("topic"))
            msgs = [store._msg(r) for r in reversed(rows)]
        msgs = sorted({m.msg_id: m for m in msgs}.values(), key=lambda m: m.msg_id)
        c = store.get_chat(cid)
        title = c.title if c else str(cid)
        if msgs:
            lines = transcript_lines(msgs, tz, None, store.media_of(cid, [m.msg_id for m in msgs]))
            b = f"=== чат «{title}» (выделенное сообщение и соседние) ===\n" + "\n".join(lines)
            blocks.append(b[-8000:])
            size += len(blocks[-1])
            for m in msgs:
                seen.add((cid, m.msg_id))
                refs.append({"chat": cid, "id": m.msg_id, "title": title})
    terms = key_terms(text, 10)
    q = fts(terms)
    hits = []
    if q:
        try:
            hits = store.search(q, since=since, limit=80)
        except Exception:  # noqa: BLE001 — FTS не понял запрос
            hits = []
    groups: dict[int, list] = {}
    titles: dict[int, str] = {}
    for m, title in hits:
        if (m.chat_id, m.msg_id) in seen or (only_chat is not None and m.chat_id != only_chat):
            continue
        groups.setdefault(m.chat_id, [])
        if len(groups[m.chat_id]) < 6:
            groups[m.chat_id].append(m.msg_id)
        titles[m.chat_id] = title or str(m.chat_id)
    found_msgs = 0
    for cid in list(groups)[:max_chats]:
        msgs = []
        for mid in groups[cid]:
            msgs += store.context(cid, mid, before=2, after=2)
        msgs = sorted({m.msg_id: m for m in msgs}.values(), key=lambda m: m.msg_id)
        lines = transcript_lines(msgs, tz, None, store.media_of(cid, [m.msg_id for m in msgs]))
        b = f"=== чат «{titles[cid]}» ===\n" + "\n".join(lines)
        if size + len(b) > budget:
            b = b[: max(0, budget - size)]
            if len(b) < 300:
                break
        blocks.append(b)
        size += len(b)
        found_msgs += len(groups[cid])
        refs += [{"chat": cid, "id": m.msg_id, "title": titles[cid]} for m in msgs]
    return {"text": "\n\n".join(blocks), "refs": refs, "terms": terms, "chats": len(groups), "hits": found_msgs}


def _digest_context(store: Store, since: str | None) -> str:
    """Последняя сводка за период (заголовок, что требует реакции, аварии) — коротко."""
    try:
        data = store.last_digest()
    except Exception:  # noqa: BLE001
        return ""
    if not data:
        return ""
    meta = data.get("meta") or {}
    if since and (meta.get("until") or "") < since:
        return ""
    ov = data.get("overview") or {}
    lines = [f"Последняя сводка ({meta.get('period_text', '')}): {ov.get('headline', '')}"]
    lines += [f"- нужна реакция: {t.get('title')} ({t.get('chat_title', '')})" for t in (ov.get("top") or [])[:8]]
    lines += [f"- авария/инцидент: {x.get('title')} — {x.get('status', '')}" for x in (ov.get("incidents") or [])[:6]]
    src = data.get("source") or {}
    if src:  # сводка «От источника» — очередь ITSM / задачи трекера
        lines[0] = f"Последняя сводка — разбор очереди «{src.get('query_text', '')}» ({meta.get('generated_local', '')}): " \
                   f"{ov.get('headline', '')}"
        lines += [f"- требует внимания: {a.get('ref')} {a.get('title', '')} — {'; '.join(a.get('flags') or [])[:200]}"
                  for a in (src.get("attention") or [])[:8]]
        lines += [f"- группа: {g.get('title')} ({', '.join(g.get('refs') or [])})" for g in (src.get("groups") or [])[:6]]
    cr = data.get("cross") or {}
    if cr.get("headline"):
        lines.append(f"Общая картина по всем источникам: {cr['headline']}")
    return "\n".join(lines)[:3000]


# ---------------------------------------------------------------- вложения беседы
def file_save(cfg, store: Store, tid: int | None, name: str, mime: str, data: bytes, *, role: str = "user") -> dict:
    """Сохранить файл беседы (вложение пользователя или файл из ответа) в data/asst/ — в защищённой папке зашифрован.
    Текст документов извлекается сразу (для запроса к модели и поиска)."""
    import hashlib
    from .media import extract_text, sniff_mime
    from .secure import files as sfiles
    ensure(store)
    sha = hashlib.sha256(data).hexdigest()
    mime = sniff_mime(data[:16]) or mime or "application/octet-stream"
    ext = (name.rsplit(".", 1)[-1].lower() if "." in name else mime.split("/")[-1])[:8]
    ext = re.sub(r"[^a-z0-9]", "", ext) or "bin"
    rel = f"data/asst/{sha[:20]}.{ext}"
    path = cfg.path(rel)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sfiles.write_bytes(path, data)
    text = ""
    if not mime.startswith("image/"):
        try:
            text = extract_text(path, name, 30000) or ""
        except Exception as e:  # noqa: BLE001 — не текстовый документ
            log.info("текст вложения не извлечён: %s", type(e).__name__)
    cur = store.db.execute("""INSERT INTO asst_files(thread_id, role, name, mime, size, sha256, path, text, created_at)
                              VALUES (?,?,?,?,?,?,?,?,?)""",
                           (tid, role, (name or "файл")[:200], mime, len(data), sha, rel, text, _now()))
    store.db.commit()
    return file_get(store, int(cur.lastrowid)) or {}


def file_get(store: Store, fid: int) -> dict | None:
    ensure(store)
    r = store.db.execute("SELECT * FROM asst_files WHERE id=?", (fid,)).fetchone()
    return _file_view(r) if r else None


def _file_view(r) -> dict:
    d = dict(r)
    d["image"] = str(d.get("mime") or "").startswith("image/")
    d["has_text"] = bool(d.get("text"))
    d.pop("text", None)
    return d


def files_of(store: Store, tid: int) -> list[dict]:
    ensure(store)
    return [_file_view(r) for r in store.db.execute("SELECT * FROM asst_files WHERE thread_id=? ORDER BY id", (tid,))]


def _bind(store: Store, tid: int, ids: list[int], mid: int) -> list[dict]:
    out = []
    for fid in ids:
        store.db.execute("UPDATE asst_files SET thread_id=?, msg_id=? WHERE id=? AND (msg_id IS NULL OR msg_id=?)",
                         (tid, mid, fid, mid))
        f = file_get(store, fid)
        if f:
            out.append(f)
    store.db.commit()
    return out


def _files_context(cfg, store: Store, files: list[dict], progress: Progress) -> tuple[str, list]:
    """Вложения пользователя → текст для модели (документы — текстом, картинки — описанием; модели с картинками
    получают их как есть — images)."""
    from .secure import files as sfiles
    parts, images = [], []
    for f in files:
        row = store.db.execute("SELECT text, path, sha256 FROM asst_files WHERE id=?", (f["id"],)).fetchone()
        if f["image"]:
            desc = store.description(row["sha256"]) if row else None
            if desc is None and row:
                try:
                    data = sfiles.read_bytes(cfg.path(row["path"]))
                    desc = plugins.describe_image(cfg, store, data, f["mime"], context=f"вложение «{f['name']}» в беседе",
                                                  source="ассистент", progress=progress)
                    images.append((data, f["mime"]))
                except OSError:
                    desc = None
            text = " ".join(x for x in ((desc or {}).get("summary"), (desc or {}).get("details"), (desc or {}).get("text")) if x)
            parts.append(f"Вложение-изображение «{f['name']}»: " + (text or "описание недоступно (модель для картинок не настроена)"))
        elif row and row["text"]:
            parts.append(f"Вложение «{f['name']}» (текст):\n{row['text'][:12000]}")
        else:
            parts.append(f"Вложение «{f['name']}» ({f['mime']}, {f['size']} байт) — текст не извлекается")
    return "\n\n".join(parts), images


# ---------------------------------------------------------------- ответ
def _wants(cfg, opts: dict) -> dict:
    src = {"telegram": True, **{k: False for k in plugins.KINDS}}
    src.update({k: bool(v) for k, v in (opts.get("sources") or {}).items() if k in src})
    for k in plugins.KINDS:
        if src[k] and not plugins.enabled(cfg, k):
            src[k] = False
    return src


def answer(cfg, store: Store, llm, tid: int, question: str, *, opts: dict | None = None,
           progress: Progress = print) -> dict:
    """Ответ в беседе tid. opts: sources {telegram, mail, itsm, wiki, rooms, tracker, meet}, period, since,
    files [номера вложений], maverick (как на портале), raw (config.json — для указаний «поставь/запомни…»),
    auto (что применять сразу). Сохраняет вопрос и ответ."""
    from . import privacy, skill
    from .digest import get_tz
    from .glossary import block_for
    from .llm import LLMError
    ensure(store)
    opts = opts or {}
    th = thread(store, tid)
    if th is None:
        raise ValueError("беседа не найдена")
    meta = th["meta"]
    question = question.strip()
    fids = [int(x) for x in opts.get("files") or [] if str(x).isdigit()]
    if not question and not fids:
        raise ValueError("введите вопрос")
    tz = get_tz(cfg.digest.get("timezone"))
    src = _wants(cfg, opts)
    since = period_since(opts.get("period") or meta.get("period") or "all", opts.get("since"), tz)
    history = [m for m in th["messages"] if m["role"] in ("user", "assistant")][-8:]
    qid = add(store, tid, "user", question, {"sources": [k for k, v in src.items() if v], "period": opts.get("period"),
                                             "maverick": bool(opts.get("maverick"))})
    files = _bind(store, tid, fids, qid) if fids else []
    if files:
        store.db.execute("UPDATE asst_msgs SET data=? WHERE id=?", (json.dumps(
            {"sources": [k for k, v in src.items() if v], "files": files}, ensure_ascii=False), qid))
        store.db.commit()
    if len(th["messages"]) == 0 and (th["title"] in ("", "Новая беседа")):
        update(store, tid, title=(question or files[0]["name"])[:80])

    if opts.get("maverick"):  # как в чате на портале Maverick
        return _maverick_turn(cfg, store, tid, question, files, meta, progress, opts.get("answers"))
    if opts.get("raw") is not None and skill.looks_like_command(question):
        return _command_turn(cfg, store, llm, tid, question, history, opts, progress)

    privacy.attach(llm, cfg, store)
    quote = str(meta.get("quote") or "")
    query = question + ("\n" + quote if quote else "")
    found: dict = {"telegram": 0, **{k: 0 for k in plugins.KINDS}}
    offline: dict = {}
    refs: list[dict] = []
    ext: list[dict] = []
    att_text, images = _files_context(cfg, store, files, progress) if files else ("", [])
    if att_text:
        query += "\n" + att_text[:3000]

    # 1. Telegram
    tg_text = ""
    if src["telegram"]:
        progress("Ищу в Telegram по всем чатам…")
        only = int(meta["chat"]) if (opts.get("sources") or {}).get("telegram") == "this" and meta.get("chat") is not None else None
        ev = tg_evidence(cfg, store, query, since=since, focus=meta if meta.get("chat") is not None else None,
                         budget=max(8000, min(int((llm.context_tokens - 4000) * 2.2), 30000)), only_chat=only)
        refs = ev["refs"]
        found["telegram"] = ev["hits"]
        progress(f"Telegram: найдено сообщений {ev['hits']} в {ev['chats']} чатах" + (", плюс выделенное" if meta.get("chat") is not None else ""))
        tg_text = ev["text"]
    dg = _digest_context(store, since)
    plug = any(src[k] for k in plugins.KINDS)
    if tg_text and plug:  # слой 1: из переписки — только выводы по вопросу; дальше переписка не идёт
        progress("Выписываю из переписки то, что относится к вопросу…")
        try:
            tg_text = llm.complete(_system(cfg, store), block_for(store, tg_text, scope="assistant") + "\n\n" +
                                   f"Вопрос пользователя: {question}\n" + (f"Выделенный фрагмент: «{quote[:2000]}»\n" if quote else "")
                                   + f"\n{FINDINGS_TASK}\n\nПереписка:\n{tg_text}", max_tokens=1500).strip()
        except (LLMError, OSError) as e:
            tg_text = ""
            offline["telegram"] = {"detail": str(e)[:200]}

    # 2. плагины Maverick — только вопрос, выделенное и ключевые слова
    from .messenger.ai import key_terms
    terms = key_terms(query, 8)
    pq = question + (f"\nФрагмент: «{quote[:1500]}»" if quote else "") + (f"\nКлючевые слова: {', '.join(terms)}" if terms else "")

    def guarded(kind, fn):
        try:
            return fn() or []
        except plugins.PluginOffline as e:
            offline[kind] = {"state": e.status.get("state"), "detail": e.status.get("detail", ""), "links": e.status.get("links") or []}
            progress(f"  {e}")
        except (LLMError, OSError, ValueError) as e:
            offline[kind] = {"state": "error", "detail": str(e)[:200]}
            progress(f"  {plugins.TITLES[kind]}: {str(e)[:160]}")
        return []

    period_txt = f"с {parse_iso(since).astimezone(tz):%d.%m.%Y}" if since else ""
    if src["itsm"]:
        nums = plugins.ticket_numbers(cfg, query + "\n" + tg_text, store)
        items = guarded("itsm", lambda: plugins.itsm_lookup(cfg, store, nums, progress=progress)) if nums else []
        if not nums or re.search(r"(?i)похож|аналог|такое было|раньше|инцидент|проблем|сбой|ошибк", question):
            items += guarded("itsm", lambda: plugins.itsm_search(cfg, store, pq, progress=progress))
        items = [x for x in {x["ref"]: x for x in items}.values() if x.get("found")]
        ext += items
        found["itsm"] = len(items)
    if src["tracker"]:
        keys = plugins.task_keys(cfg, query + "\n" + tg_text, store)
        items = guarded("tracker", lambda: plugins.tracker_lookup(cfg, store, keys, progress=progress)) if keys else []
        if not keys or re.search(r"(?i)похож|задач|баг|ошибк|доработ", question):
            items += guarded("tracker", lambda: plugins.tracker_search(cfg, store, pq, progress=progress))
        items = [x for x in {x["ref"]: x for x in items}.values() if x.get("found")]
        ext += items
        found["tracker"] = len(items)
    for kind in ("mail", "wiki", "rooms", "meet"):
        if src[kind]:
            items = guarded(kind, lambda k=kind: plugins.search(cfg, store, k, pq, period=period_txt, progress=progress))
            ext += items
            found[kind] = len(items)
    # кеш: найденное раньше по тем же словам (без запроса к Maverick)
    local = [x for x in plugins.search_items(store, terms + plugins.ticket_numbers(cfg, query, store), limit=8)
             if x["ref"] not in {e["ref"] for e in ext}] if terms else []

    # 3. ответ
    parts = []
    if history:
        parts.append("История беседы:\n" + "\n".join(
            f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['text'][:1500]}" for m in history))
    if meta.get("chat") is not None and quote:
        parts.append(f"Пользователь выделил фрагмент в Telegram: «{quote[:2000]}»")
    if att_text:
        parts.append("Пользователь приложил к вопросу:\n" + att_text[:14000])
    if tg_text:
        parts.append(("Что нашлось в Telegram (выводы по переписке):\n" if plug else "Выдержки из Telegram:\n") + tg_text)
    elif src["telegram"]:
        parts.append("В Telegram (в базе) по вопросу ничего не нашлось.")
    if dg:
        parts.append(dg)
    if ext:
        parts.append("Найдено в других источниках (через плагины Maverick):\n" + "\n".join(plugins.item_line(x)[:900] for x in ext))
    if local:
        parts.append("Найденное раньше (сохранено в программе):\n" + "\n".join(plugins.item_line(x)[:600] for x in local))
    if offline:
        parts.append("Недоступно сейчас: " + ", ".join(plugins.TITLES.get(k, k) for k in offline))
    gl = block_for(store, query + "\n" + tg_text[:4000] + "\n" + " ".join(plugins.item_line(x) for x in ext)[:4000],
                   scope="assistant")
    user = (gl + "\n\n" if gl else "") + "\n\n".join(parts) + f"\n\nВопрос пользователя: {question or '(см. вложения)'}"
    progress(f"Отвечаю ({llm.label})…")
    text = llm.complete(_system(cfg, store), user, max_tokens=2200).strip()
    text, acts, ask = skill.parse_blocks(text)
    from .messenger.ai import cited
    want = set(cited(text, store))
    data = {"refs": [r for r in refs if r["id"] in want][:60], "ext": [_ext_view(x) for x in ext + local][:30],
            "found": found, "offline": offline, "model": llm.label, "sources": [k for k, v in src.items() if v]}
    media = _cited_media(store, data["refs"])
    if media:
        data["media"] = media
    if ask:
        data["ask"] = ask
    if acts and opts.get("raw") is not None:
        ctx = skill.Ctx(opts["raw"], store, cfg)
        data["plan"] = skill.run_actions(ctx, acts, source="assistant", ref={"thread": tid},
                                         auto=opts.get("auto") or _auto(cfg))
    mid = add(store, tid, "assistant", text, data)
    try:
        from .glossary_auto import note_many
        note_many(cfg, store, [(question, "assistant", None), (text, "assistant", None)] +
                  [(plugins.item_line(x), x["source"], None) for x in ext])
    except Exception:  # noqa: BLE001
        pass
    return {"thread": tid, "message": mid, "text": text, **data}


def _auto(cfg) -> str:
    v = str((cfg.get("skill") or {}).get("auto_apply") or "knowledge")
    return v if v in ("knowledge", "none") else "knowledge"


def _cited_media(store: Store, refs: list[dict], limit: int = 8) -> list[dict]:
    """Картинки и файлы из сообщений, на которые сослался ответ, — приложить к ответу."""
    out = []
    for r in refs:
        try:
            got = store.media_of(int(r["chat"]), [int(r["id"])])
        except Exception:  # noqa: BLE001
            continue
        x = got.get(int(r["id"])) if isinstance(got, dict) else None
        if isinstance(x, dict) and x.get("id") and x.get("status") == "ok" and not any(o["media"] == x["id"] for o in out):
            out.append({"media": x["id"], "kind": x.get("kind") or "", "name": x.get("file_name") or x.get("name") or "",
                        "chat": r["chat"], "msg": r["id"], "summary": (x.get("summary") or "")[:200]})
        if len(out) >= limit:
            break
    return out[:limit]


def _command_turn(cfg, store: Store, llm, tid: int, question: str, history: list[dict], opts: dict,
                  progress: Progress) -> dict:
    """Указание пользователя («всем чатам с … поставь…», «запомни, что INC… — это…») → действия навыка."""
    from . import privacy, skill
    ctx = skill.Ctx(opts["raw"], store, cfg)
    progress("Разбираю указание…")
    privacy.attach(llm, cfg, store)
    hist = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['text'][:800]}" for m in history[-6:])
    user = ("Что сейчас есть в программе:\n" + skill.catalog(ctx, question) +
            (f"\n\nИстория беседы:\n{hist}" if hist else "") + f"\n\nУказание пользователя: {question}")
    raw = llm.complete(skill.COMMAND_SYSTEM.replace("{actions}", skill.ACTIONS_HELP), user, max_tokens=2500)
    text, acts, ask = skill.parse_blocks(raw)
    data: dict = {"model": llm.label, "command": True}
    if ask and not acts:
        data["ask"] = ask
    if acts:
        data["plan"] = skill.run_actions(ctx, acts, source="assistant", ref={"thread": tid},
                                         auto=opts.get("auto") or _auto(cfg))
    if not text:
        text = "Готово." if acts else "Не понял, что изменить — уточните, пожалуйста."
    mid = add(store, tid, "assistant", text, data)
    return {"thread": tid, "message": mid, "text": text, **data}


def _maverick_turn(cfg, store: Store, tid: int, question: str, files: list[dict], meta: dict, progress: Progress,
                   answers: dict | None = None) -> dict:
    """Режим «как на портале»: сообщение — прямо в сессию Maverick, файлы — туда и обратно."""
    from . import maverick_chat
    from .secure import files as sfiles
    att = []
    for f in files:
        row = store.db.execute("SELECT path FROM asst_files WHERE id=?", (f["id"],)).fetchone()
        if row:
            att.append({"name": f["name"], "mime": f["mime"], "data": sfiles.read_bytes(cfg.path(row["path"]))})
    saved: list[dict] = []

    def save_file(name: str, mime: str, data: bytes) -> dict:
        x = file_save(cfg, store, tid, name, mime, data, role="assistant")
        saved.append(x)
        return x
    res = maverick_chat.converse(cfg, store, tid, question, att, meta=meta, save_file=save_file, progress=progress,
                                 answers=answers)
    new_meta = dict(meta, mav_session=res.get("session") or meta.get("mav_session"))
    if res.get("ask"):  # Maverick ждёт ответа: следующий ответ пользователя — на этот вопрос
        new_meta["mav_hitl"] = res["ask"].get("pending") or {}
    else:
        new_meta.pop("mav_hitl", None)
    if new_meta != meta:
        update(store, tid, meta=new_meta)
    data = {"model": "Maverick (как на портале)", "maverick": True, "files": [f for f in res["files"] if f.get("id")],
            "files_failed": [f for f in res["files"] if not f.get("id")]}
    if res.get("ask"):
        data["ask"] = res["ask"]
    mid = add(store, tid, "assistant", res["text"] or ("" if res.get("ask") else "—"), data)
    if saved:
        store.db.executemany("UPDATE asst_files SET msg_id=? WHERE id=?", [(mid, x["id"]) for x in saved])
        store.db.commit()
    for x in saved:  # картинки из ответа — описать (поиск, глоссарий); ошибки — молча
        if x.get("image"):
            try:
                row = store.db.execute("SELECT path FROM asst_files WHERE id=?", (x["id"],)).fetchone()
                plugins.describe_image(cfg, store, sfiles.read_bytes(cfg.path(row["path"])), x["mime"],
                                       context="файл из ответа Maverick", source="Maverick", progress=progress)
            except Exception:  # noqa: BLE001
                pass
    try:
        from .glossary_auto import note_many
        note_many(cfg, store, [(res["text"], "assistant", None)])
    except Exception:  # noqa: BLE001
        pass
    return {"thread": tid, "message": mid, "text": res["text"], **data}


def _ext_view(x: dict) -> dict:
    d = x.get("data") or {}
    return {"source": x["source"], "ref": x["ref"], "title": x.get("title") or d.get("subject") or d.get("term") or "",
            "status": x.get("status") or "", "date": x.get("date") or d.get("date") or "",
            "url": d.get("url") or "", "summary": (d.get("summary") or d.get("description") or "")[:400],
            "from": d.get("from") or d.get("chat") or "", "number": d.get("number") or d.get("key") or "",
            "images": [{"media": i["media"], "desc": (i.get("desc") or i.get("about") or "")[:300], "name": i.get("name") or ""}
                       for i in d.get("images") or [] if isinstance(i, dict) and i.get("media")][:4]}


def _system(cfg, store: Store) -> str:
    me = store.kv_get("me")
    me_name = json.loads(me).get("name", "") if me else ""
    return SYSTEM.format(profile=prompts.profile_block(cfg.profile, me_name))


# ---------------------------------------------------------------- «везде»: выделенное → все чаты и плагины
def everywhere(cfg, store: Store, text: str, *, chat_id: int | None = None, msg_id: int | None = None,
               sources: dict | None = None, progress: Progress = print) -> dict:
    """Найти выделенное в Telegram и в выбранных источниках (Telegram — по базе, без модели).

    sources: {telegram: true | "this" (только этот чат) | false, mail, itsm, wiki, rooms, tracker, meet}."""
    from .llm import LLMError
    from .messenger.ai import key_terms, related
    sources = dict(sources or {})
    tg = sources.get("telegram", True)
    out: dict = {k: [] for k in plugins.KINDS}
    out.update({"telegram": [], "offline": {}, "sources": []})
    if tg:
        out["telegram"] = related(store, text, chat_id=chat_id, msg_id=msg_id, other_chats_only=False)
        if tg == "this" and chat_id is not None and isinstance(out["telegram"], dict):
            out["telegram"] = _only_chat(out["telegram"], chat_id)
        out["sources"].append("telegram")
    src = _wants(cfg, {"sources": {k: v for k, v in sources.items() if k != "telegram"}})
    terms = key_terms(text, 8)
    pq = f"«{text.strip()[:2000]}»" + (f"\nКлючевые слова: {', '.join(terms)}" if terms else "")
    for kind in plugins.KINDS:
        if not src.get(kind):
            continue
        out["sources"].append(kind)
        try:
            items = [x for x in {x["ref"]: x for x in plugins.search(cfg, store, kind, pq, progress=progress)}.values()
                     if x.get("found")]
            out[kind] = [_ext_view(x) for x in items]
        except plugins.PluginOffline as e:
            out["offline"][kind] = {"state": e.status.get("state"), "detail": e.status.get("detail", ""),
                                    "links": e.status.get("links") or []}
        except (LLMError, OSError, ValueError) as e:
            out["offline"][kind] = {"state": "error", "detail": str(e)[:200]}
    nums = plugins.ticket_numbers(cfg, text, store) + plugins.task_keys(cfg, text, store)
    want_src = [k for k in plugins.KINDS if src.get(k)] or None
    out["saved"] = [_ext_view(x) for x in plugins.search_items(store, terms + nums, limit=10)
                    if (want_src is None or x["source"] in want_src)
                    and not any(x["ref"] == y["ref"] for k in plugins.KINDS for y in out[k])]
    return out


def _only_chat(rel: dict, chat_id: int) -> dict:
    """Результат related() → только сообщения этого чата (выбор «только этот чат»)."""
    out = dict(rel)
    for k, v in rel.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "chat" in v[0]:
            out[k] = [x for x in v if int(x.get("chat") or 0) == int(chat_id)]
    return out
