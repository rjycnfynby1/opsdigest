"""История сводок: новая сводка опирается на прошлые и показывает, что изменилось.

Каждый разобранный моделью чат сохраняется сразу (таблица digest_chats) — даже если сводку потом остановили или она
упала: что модель уже прочитала, не читается заново.

  • точный повтор: те же сообщения чата (тот же текст, те же указания) уже разбирались — результат берётся из базы,
    модель не нужна (повтор после обрыва связи, повторный запуск, тихий чат);
  • опора на прошлые сводки: сообщения, которые уже вошли в прошлые сводки (цепочка сводок, покрывающих начало
    периода), модель заново не читает — вместо них получает краткое содержание этих сводок и открытые пункты, а
    целиком — только новые сообщения. Так сводка за неделю строится из сводок за дни, а «сутки» каждые 3 часа
    читают только последние 3 часа;
  • изменения: по каждому чату модель видит прошлую сводку и отмечает пункты: new — новое, updated — изменилось,
    same — без изменений, resolved — закрыто; в общей картине — раздел «Что изменилось с прошлой сводки».

Опора на прошлые сводки — только если дополнительные указания те же (иначе прошлая сводка могла опустить нужное).
Выключается галочкой «Опираться на прошлые сводки» (digest.history = false — по умолчанию в настройках).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .storage import Store, iso

VERSION = "h1"          # меняется вместе с промптом map — старые результаты тогда не берутся как точный повтор
MAX_CHAIN = 12          # больше прошлых сводок одного чата в контекст не кладём
CONTEXT_CHARS = 3500    # сколько места в запросе на прошлые сводки одного чата

SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_chats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run        TEXT,
    chat_id    INTEGER NOT NULL,
    since      TEXT,
    until      TEXT,
    first_msg  INTEGER,
    last_msg   INTEGER,
    n          INTEGER,
    sig        TEXT,
    extra      TEXT,
    result     TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_digest_chats_chat ON digest_chats(chat_id, last_msg);
CREATE INDEX IF NOT EXISTS ix_digest_chats_sig ON digest_chats(sig);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def _h(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]


def extra_sig(extra: str) -> str:
    return _h("extra", " ".join(str(extra or "").split()))


def chat_sig(lines: list[str], notes: list[str], extra: str) -> str:
    """Отпечаток того, что модель увидит по чату: переписка (с описаниями вложений), заметки, указания."""
    return _h(VERSION, "\n".join(lines), "\n".join(notes or []), extra_sig(extra))


def find_exact(store: Store, chat_id: int, sig: str) -> dict | None:
    r = store.db.execute("SELECT result, created_at FROM digest_chats WHERE chat_id=? AND sig=? ORDER BY id DESC LIMIT 1",
                         (chat_id, sig)).fetchone()
    if not r:
        return None
    try:
        res = json.loads(r[0])
    except ValueError:
        return None
    return res if isinstance(res, dict) else None


def save(store: Store, run: str, chat_id: int, since: str, until: str, msg_ids: list[int], sig: str, extra: str,
         result: dict) -> None:
    if not msg_ids:
        return
    keep = {k: result.get(k) for k in ("summary", "status", "importance", "items")}
    store.db.execute(
        "INSERT INTO digest_chats(run, chat_id, since, until, first_msg, last_msg, n, sig, extra, result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run, chat_id, since, until, min(msg_ids), max(msg_ids), len(msg_ids), sig, extra_sig(extra),
         json.dumps(keep, ensure_ascii=False), iso(datetime.now(timezone.utc))))
    store.db.commit()


def chain(store: Store, chat_id: int, msg_ids: list[int], extra: str) -> list[dict]:
    """Прошлые сводки этого чата, которые подряд покрывают начало периода: [{since, until, first_msg, last_msg,
    result}], по порядку. Сообщения с id ≤ последнего last_msg цепочки уже разобраны."""
    if not msg_ids:
        return []
    ids = sorted(msg_ids)
    es = extra_sig(extra)
    rows = store.db.execute(
        "SELECT since, until, first_msg, last_msg, result FROM digest_chats WHERE chat_id=? AND extra=? "
        "AND last_msg>=? AND first_msg<=? ORDER BY id DESC LIMIT 400", (chat_id, es, ids[0], ids[-1])).fetchall()
    rows = [dict(r) for r in rows]
    out: list[dict] = []
    cursor = ids[0]
    while len(out) < MAX_CHAIN:
        cands = [r for r in rows if r["first_msg"] <= cursor <= r["last_msg"]]
        if not cands:
            break
        best = max(cands, key=lambda r: (r["last_msg"], r["until"] or ""))
        try:
            best["result"] = json.loads(best["result"]) if isinstance(best["result"], str) else best["result"]
        except ValueError:
            break
        out.append(best)
        nxt = [i for i in ids if i > best["last_msg"]]
        if not nxt:
            break
        cursor = nxt[0]
    return out


def latest_before(store: Store, chat_id: int, before: str, extra: str, days: int = 14) -> dict | None:
    """Последняя прошлая сводка чата, закончившаяся до начала периода (не дальше days дней), — контекст для
    отметок «что изменилось», когда сообщения периода ещё ни в одну сводку не входили."""
    try:
        lo = iso(datetime.fromisoformat(before) - timedelta(days=days))
    except ValueError:
        lo = ""
    r = store.db.execute("SELECT since, until, first_msg, last_msg, result FROM digest_chats WHERE chat_id=? AND extra=? "
                         "AND until<=? AND until>=? ORDER BY until DESC, id DESC LIMIT 1",
                         (chat_id, extra_sig(extra), before, lo)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["result"] = json.loads(d["result"])
    except ValueError:
        return None
    return d


def _when(s: str, tz) -> str:
    try:
        return datetime.fromisoformat(s).astimezone(tz).strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return str(s or "")[:16]


def context_block(rows: list[dict], tz, covered_upto: int | None) -> str:
    """Текст для заголовка чата: что было в прошлых сводках (кратко) и открытые пункты с номерами сообщений."""
    if not rows:
        return ""
    parts = []
    for r in rows:
        res = r.get("result") or {}
        items = [i for i in res.get("items") or [] if i.get("change") != "resolved"]
        it = "; ".join(f"[{i.get('type')}] {i.get('title')}" + (f" (#{', #'.join(str(m) for m in i.get('msgs')[:3])})"
                                                                 if i.get("msgs") else "") for i in items[:8])
        parts.append(f"— {_when(r['since'], tz)}–{_when(r['until'], tz)}: {res.get('summary') or 'без важного'}"
                     + (f" Пункты: {it}" if it else ""))
    text = "\n".join(parts)
    if len(text) > CONTEXT_CHARS:   # длинная цепочка — оставить последние сводки
        text = "…\n" + text[-CONTEXT_CHARS:]
    head = ("ПРОШЛЫЕ СВОДКИ ПО ЭТОМУ ЧАТУ" + (f" — сообщения до #{covered_upto} уже разобраны в них, ниже — только "
                                               "новые сообщения" if covered_upto else " (для отметок, что изменилось)"))
    return f"[{head}:\n{text}]"


def prev_digest(store: Store, since: str, until: str) -> dict | None:
    """Прошлая общая сводка — последняя законченная до конца текущего периода (не старше 14 дней от его начала)."""
    try:
        lo = iso(datetime.fromisoformat(since) - timedelta(days=14))
    except ValueError:
        lo = ""
    r = store.db.execute("SELECT id, period, since, until, created_at, data FROM digests WHERE until<? AND until>=? "
                         "AND period NOT IN ('demo', 'source') ORDER BY until DESC, id DESC LIMIT 1", (until, lo)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r["data"])
    except ValueError:
        return None
    return {"id": r["id"], "since": r["since"], "until": r["until"], "data": d}


def prev_brief(prev: dict, tz) -> str:
    """Главное из прошлой сводки — для раздела «что изменилось» (заголовок, пункты, аварии со статусами)."""
    d = prev.get("data") or {}
    ov = d.get("overview") or {}
    lines = [f"Прошлая сводка ({_when(prev['since'], tz)}–{_when(prev['until'], tz)}): {ov.get('headline') or '—'}"]
    for t in (ov.get("top") or [])[:10]:
        lines.append(f"- нужна реакция: {t.get('title')} («{t.get('chat_title') or t.get('chat')}»)")
    for t in (ov.get("control") or [])[:6]:
        lines.append(f"- проследить: {t.get('title')} («{t.get('chat_title') or t.get('chat')}»)")
    for x in (ov.get("incidents") or [])[:8]:
        lines.append(f"- авария: {x.get('title')} — {x.get('status') or 'неясно'}")
    return "\n".join(lines)[:4000]
