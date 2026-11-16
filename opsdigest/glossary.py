"""Глоссарий и база знаний: термины, их значения и как их различать, форматы номеров, кто отвечает.

Зачем: модель путает «инцидент»-обращение в ITSM и «инцидент»-чат малой аварии, «проблему» в разговоре и PRB в
problem management, не знает внутренних сервисов, модулей и команд. Глоссарий подставляется в каждый запрос к модели
(сводка, ИИ в чате, вопросы, ИИ-описания) — только статьи терминов, которые встретились в тексте, плюс закреплённые.

Как пополняется:
  * при каждой сводке модель заодно отмечает новые и неясные термины (поле glossary в ответе map);
  * фоновая задача «Пополнить из чатов» читает переписку чатов (с последнего прохода) и предлагает статьи;
  * неясное и пересекающееся превращается в вопросы пользователю — ответ обновляет статью;
  * недостающее можно поискать в вики через Maverick (его Confluence).
Предложения ИИ — черновики (status=draft), их видно и модели (с пометкой), подтверждённые пользователем — confirmed.
Хранится в базе (зашифрованной вместе с ней), выгружается и загружается файлом JSON — перенести на другой компьютер.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import logs
from .storage import Store, iso

log = logs.get("glossary")

SCHEMA = """
CREATE TABLE IF NOT EXISTS glossary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,       -- термин в нижнем регистре, ё→е
    term        TEXT NOT NULL,
    kind        TEXT,
    aliases     TEXT,                       -- JSON: другие написания, сокращения
    senses      TEXT,                       -- JSON: [{meaning, how_to_tell, id_format, owner, example}]
    summary     TEXT,
    details     TEXT,
    owner       TEXT,
    related     TEXT,                       -- JSON: связанные термины
    status      TEXT DEFAULT 'draft',       -- draft (ИИ) | confirmed (пользователь) | hidden
    pinned      INTEGER DEFAULT 0,          -- подставлять в каждый запрос
    source      TEXT,
    refs        TEXT,                       -- JSON: [[чат, сообщение], …] — где встречалось
    hits        INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS gl_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    term        TEXT,
    question    TEXT NOT NULL,
    options     TEXT,                       -- JSON: варианты ответа от модели
    context     TEXT,                       -- откуда вопрос (чат, фрагмент)
    refs        TEXT,
    status      TEXT DEFAULT 'open',        -- open | answered | dismissed
    answer      TEXT,
    created_at  TEXT,
    answered_at TEXT
);
CREATE TABLE IF NOT EXISTS gl_scan (
    chat_id     INTEGER PRIMARY KEY,
    last_msg    INTEGER DEFAULT 0,
    scanned_at  TEXT
);
"""

KINDS = {"service": "сервис / система", "app": "приложение", "module": "модуль / функция", "team": "команда",
         "person": "человек / роль", "api": "API / интеграция", "process": "процесс", "entity": "сущность / тип заявки",
         "instruction": "инструкция", "problem": "типовая проблема и решение", "abbr": "сокращение / жаргон",
         "other": "другое"}

# начальные статьи — со слов пользователя (06.11): одинаковые слова с разным смыслом
SEED = [
    {"term": "Инцидент", "kind": "entity", "aliases": ["инцидента", "инциденты", "INC", "инц"], "status": "confirmed",
     "summary": "Два значения: обращение в ITSM и чат-инцидент (малая авария) — различать по контексту и номеру.",
     "senses": [
         {"meaning": "Обращение (заявка) в ITSM на портале поддержки — от пользователя или по алерту",
          "how_to_tell": "есть номер обращения ITSM; речь о заявке, её статусе, группе, SLA, решении",
          "id_format": r"INC\d{5,}"},
         {"meaning": "Чат-инцидент: автоматически созданный дежурным чат по росту ошибок/обращений — как авария, но "
                     "без влияния на критичную инфраструктуру и без потерь",
          "how_to_tell": "речь о чате или разборе сбоя, участниках, восстановлении; название чата «Инцидент …»"}]},
    {"term": "Авария", "kind": "entity", "aliases": ["аварии", "аварию", "аварийный"], "status": "confirmed",
     "summary": "Крупный сбой: дежурный регистрирует аварию на портале, автоматически создаётся чат с нужными участниками.",
     "senses": [{"meaning": "Сбой с влиянием на критичную инфраструктуру или с потерями; для разбора автоматически "
                            "создаётся чат «Авария …»",
                 "how_to_tell": "чат аварии, статус (идёт / локализована / решена), влияние, восстановление"}]},
    {"term": "Проблема", "kind": "entity", "aliases": ["проблемы", "проблему", "PRB", "problem"], "status": "confirmed",
     "summary": "Два значения: бытовое «проблема» и сущность problem management в ITSM (к ней привязывают инциденты).",
     "senses": [
         {"meaning": "Обычное слово: у пользователя проблема, опишите проблему",
          "how_to_tell": "нет номера и речи о процессе problem management"},
         {"meaning": "Проблема в ITSM (problem management): заводится по повторяющимся инцидентам, инциденты к ней "
                     "привязываются",
          "how_to_tell": "номер проблемы ITSM, «привязать к проблеме», корневая причина, known error",
          "id_format": r"PRB\d{5,}"}]},
]
SEED_QUESTIONS = [
    {"term": "Инцидент", "question": "Как выглядит номер инцидента (обращения) в вашем ITSM? Например: INC0012345.",
     "options": ["INC + 7 цифр (INC0012345)", "IM + цифры", "SD + цифры"]},
    {"term": "Проблема", "question": "Как выглядит номер проблемы в ITSM (problem management)? Например: PRB0001234.",
     "options": ["PRB + 7 цифр (PRB0001234)", "PM + цифры"]},
]

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_.]*")
_ENDINGS = sorted("""а я о е ы и у ю ь й ой ей ом ем ам ям ах ях ами ями ов ев ий ый ая яя ое ее ые ие ую юю его ого ему
ому им ым их ых ою ею ия ии ию ией иям иях ями ость ости остью ться ть ся сь""".split(), key=len, reverse=True)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower().replace("ё", "е"))


def stem(w: str) -> str:
    """Грубая основа слова для поиска в тексте: «инцидента» → «инцидент», «аварии» → «авар»."""
    w = norm(w)
    if len(w) <= 4 or not re.search(r"[а-я]", w):
        return w
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 4:
            return w[:-len(e)]
    return w


def _j(x, default):
    try:
        v = json.loads(x) if isinstance(x, str) and x else x
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


GL_COLUMNS = {"visual": "TEXT", "images": "TEXT",   # как выглядит; прикреплённые изображения (номера вложений)
              "examples": "TEXT", "links": "TEXT"}  # примеры употребления; ссылки на вики [{title, url}]


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)
    have = {r[1] for r in store.db.execute("PRAGMA table_info(glossary)")}
    for col, decl in GL_COLUMNS.items():
        if col not in have:
            store.db.execute(f"ALTER TABLE glossary ADD COLUMN {col} {decl}")
    store.db.commit()


class Glossary:
    def __init__(self, store: Store):
        self.store = store
        ensure(store)
        self._index = None

    # ------------------------------------------------------------ чтение
    def row(self, r) -> dict:
        d = dict(r)
        d["aliases"] = _j(d.get("aliases"), [])
        d["senses"] = _j(d.get("senses"), [])
        d["related"] = _j(d.get("related"), [])
        d["refs"] = _j(d.get("refs"), [])
        d["images"] = _j(d.get("images"), [])
        d["visual"] = d.get("visual") or ""
        d["examples"] = _j(d.get("examples"), [])
        d["links"] = _j(d.get("links"), [])
        d["pinned"] = bool(d.get("pinned"))
        return d

    def all(self, include_hidden: bool = False) -> list[dict]:
        q = "SELECT * FROM glossary" + ("" if include_hidden else " WHERE status!='hidden'") + " ORDER BY term"
        return [self.row(r) for r in self.store.db.execute(q).fetchall()]

    def get(self, term_or_id) -> dict | None:
        if isinstance(term_or_id, int) or str(term_or_id).isdigit():
            r = self.store.db.execute("SELECT * FROM glossary WHERE id=?", (int(term_or_id),)).fetchone()
        else:
            r = self.store.db.execute("SELECT * FROM glossary WHERE key=?", (norm(str(term_or_id)),)).fetchone()
            if r is None:  # по синониму
                k = norm(str(term_or_id))
                for e in self.all(True):
                    if k in {norm(a) for a in e["aliases"]}:
                        return e
        return self.row(r) if r else None

    def count(self) -> dict:
        r = self.store.db.execute("""SELECT COUNT(*), SUM(status='confirmed'), SUM(status='draft') FROM glossary
                                     WHERE status!='hidden'""").fetchone()
        q = self.store.db.execute("SELECT COUNT(*) FROM gl_questions WHERE status='open'").fetchone()[0]
        return {"terms": r[0] or 0, "confirmed": r[1] or 0, "draft": r[2] or 0, "questions": q}

    # ------------------------------------------------------------ запись
    def save(self, e: dict, *, merge: bool = False, source: str = "user") -> dict:
        """Создать или обновить статью. merge — дополнить существующую (предложение ИИ): значения и синонимы
        добавляются, ручной текст и подтверждённый статус не затираются."""
        term = str(e.get("term") or "").strip()[:120]
        if not term:
            raise ValueError("нет термина")
        now = iso(datetime.now(timezone.utc))
        old = self.get(e["id"]) if e.get("id") else self.get(term)
        aliases = [str(a).strip()[:80] for a in e.get("aliases") or [] if str(a).strip()]
        senses = [self._sense(s) for s in e.get("senses") or [] if isinstance(s, dict)]
        senses = [s for s in senses if s.get("meaning")]
        refs = [list(x)[:2] for x in e.get("refs") or [] if isinstance(x, (list, tuple)) and len(x) >= 2]
        if old and merge:
            al = old["aliases"] + [a for a in aliases if norm(a) not in {norm(x) for x in old["aliases"]}
                                   and norm(a) != old["key"]]
            have = [norm(s.get("meaning", ""))[:60] for s in old["senses"]]
            ss = old["senses"] + [s for s in senses if norm(s["meaning"])[:60] not in have
                                  and not any(_similar(s["meaning"], o.get("meaning", "")) for o in old["senses"])]
            rf = (old["refs"] + [r for r in refs if r not in old["refs"]])[-30:]
            new = dict(old, aliases=al[:30], senses=ss[:8], refs=rf, hits=(old.get("hits") or 0) + 1, updated_at=now,
                       examples=_merge_list(old.get("examples") or [], _examples(e.get("examples")), 10),
                       links=_merge_links(old.get("links") or [], _links(e.get("links"))))
            for k in ("summary", "owner", "kind", "details", "visual"):
                if not new.get(k) and e.get(k):
                    new[k] = str(e[k]).strip()[:4000]
        else:
            new = dict(old or {}, term=term, kind=(e.get("kind") if e.get("kind") in KINDS else (old or {}).get("kind")
                                                   or "other"),
                       aliases=aliases, senses=senses, summary=str(e.get("summary") or "").strip()[:600],
                       details=str(e.get("details") or "").strip()[:6000], owner=str(e.get("owner") or "").strip()[:300],
                       related=[str(x).strip()[:80] for x in e.get("related") or [] if str(x).strip()][:20],
                       refs=refs or (old or {}).get("refs") or [], updated_at=now,
                       status=e.get("status") if e.get("status") in ("draft", "confirmed", "hidden")
                       else (old or {}).get("status") or ("confirmed" if source == "user" else "draft"),
                       pinned=bool(e.get("pinned")) if "pinned" in e else bool((old or {}).get("pinned")),
                       visual=str(e.get("visual") if "visual" in e else (old or {}).get("visual") or "").strip()[:3000],
                       images=[int(i) for i in (e.get("images") if "images" in e else (old or {}).get("images")) or []
                               if str(i).isdigit()][:24],
                       examples=_examples(e["examples"]) if "examples" in e else (old or {}).get("examples") or [],
                       links=_links(e["links"]) if "links" in e else (old or {}).get("links") or [],
                       source=(old or {}).get("source") or source, hits=(old or {}).get("hits") or 0)
        key = norm(new["term"])
        vals = (key, new["term"], new.get("kind") or "other", json.dumps(new.get("aliases") or [], ensure_ascii=False),
                json.dumps(new.get("senses") or [], ensure_ascii=False), new.get("summary") or "",
                new.get("details") or "", new.get("owner") or "", json.dumps(new.get("related") or [], ensure_ascii=False),
                new.get("status") or "draft", int(bool(new.get("pinned"))), new.get("source") or source,
                json.dumps(new.get("refs") or [], ensure_ascii=False), int(new.get("hits") or 0), now,
                new.get("visual") or "", json.dumps(new.get("images") or []),
                json.dumps(new.get("examples") or [], ensure_ascii=False), json.dumps(new.get("links") or [], ensure_ascii=False))
        if old:
            other = self.store.db.execute("SELECT id FROM glossary WHERE key=? AND id!=?", (key, old["id"])).fetchone()
            if other:
                raise ValueError(f"термин «{new['term']}» уже есть в глоссарии")
            self.store.db.execute("""UPDATE glossary SET key=?, term=?, kind=?, aliases=?, senses=?, summary=?, details=?,
                                     owner=?, related=?, status=?, pinned=?, source=?, refs=?, hits=?, updated_at=?,
                                     visual=?, images=?, examples=?, links=? WHERE id=?""", vals + (old["id"],))
            gid = old["id"]
        else:
            cur = self.store.db.execute("""INSERT INTO glossary(key, term, kind, aliases, senses, summary, details, owner,
                                           related, status, pinned, source, refs, hits, updated_at, visual, images,
                                           examples, links, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                        vals + (now,))
            gid = cur.lastrowid
        self.store.db.commit()
        self._index = None
        return self.get(gid)

    @staticmethod
    def _sense(s: dict) -> dict:
        out = {k: str(s.get(k) or "").strip()[:600] for k in ("meaning", "how_to_tell", "id_format", "owner", "example")}
        if out["id_format"]:
            try:
                re.compile(out["id_format"])
            except re.error:
                out["id_format"] = re.escape(out["id_format"])
        return {k: v for k, v in out.items() if v}

    def delete(self, gid: int) -> None:
        self.store.db.execute("DELETE FROM glossary WHERE id=?", (gid,))
        self.store.db.commit()
        self._index = None

    def seed(self) -> int:
        """Начальные статьи и вопросы — если глоссарий пуст."""
        if self.store.db.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]:
            return 0
        for e in SEED:
            self.save(e, source="user")
        for q in SEED_QUESTIONS:
            self.ask(q["term"], q["question"], q.get("options"), "начальная настройка глоссария")
        return len(SEED)

    # ------------------------------------------------------------ вопросы пользователю
    def ask(self, term: str, question: str, options: Iterable[str] | None = None, context: str = "",
            refs: list | None = None) -> int | None:
        question = str(question or "").strip()[:600]
        if not question:
            return None
        dup = self.store.db.execute("SELECT id FROM gl_questions WHERE lower(term)=lower(?) AND status='open'",
                                    (term or "",)).fetchall()
        if len(dup) >= 3:  # по одному термину не больше трёх открытых вопросов
            return None
        for r in self.store.db.execute("SELECT id, question FROM gl_questions WHERE lower(term)=lower(?)",
                                       (term or "",)).fetchall():
            if _similar(r["question"], question):
                return None
        cur = self.store.db.execute(
            "INSERT INTO gl_questions(term, question, options, context, refs, created_at) VALUES (?,?,?,?,?,?)",
            (str(term or "").strip()[:120], question, json.dumps([str(o)[:200] for o in options or []][:6],
                                                                 ensure_ascii=False),
             str(context or "")[:1500], json.dumps(refs or [], ensure_ascii=False), iso(datetime.now(timezone.utc))))
        self.store.db.commit()
        return cur.lastrowid

    def questions(self, status: str = "open") -> list[dict]:
        rows = self.store.db.execute("SELECT * FROM gl_questions WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = _j(d.get("options"), [])
            d["refs"] = _j(d.get("refs"), [])
            out.append(d)
        return out

    def close_question(self, qid: int, answer: str | None, status: str = "answered") -> dict | None:
        r = self.store.db.execute("SELECT * FROM gl_questions WHERE id=?", (qid,)).fetchone()
        if not r:
            return None
        self.store.db.execute("UPDATE gl_questions SET status=?, answer=?, answered_at=? WHERE id=?",
                              (status, answer, iso(datetime.now(timezone.utc)), qid))
        self.store.db.commit()
        return dict(r)

    # ------------------------------------------------------------ поиск терминов в тексте
    def _build(self):
        idx: dict[str, set[int]] = {}
        phrases: list[tuple[str, int]] = []
        regexes: list[tuple[re.Pattern, int]] = []
        entries = {e["id"]: e for e in self.all()}
        for e in entries.values():
            for name in [e["term"]] + e["aliases"]:
                n = norm(name)
                if not n:
                    continue
                if " " in n:
                    phrases.append((n, e["id"]))
                else:
                    idx.setdefault(stem(n) if len(n) > 4 else n, set()).add(e["id"])
            for s in e["senses"]:
                if s.get("id_format"):
                    try:
                        regexes.append((re.compile(r"(?<![A-Za-z0-9])" + s["id_format"] + r"(?![0-9])", re.I), e["id"]))
                    except re.error:
                        pass
        self._index = (idx, phrases, regexes, entries)
        return self._index

    def match(self, text: str, limit: int = 25, pinned: bool = True) -> list[dict]:
        """Статьи терминов, встретившихся в тексте (самые частые первыми), плюс закреплённые."""
        idx, phrases, regexes, entries = self._index or self._build()
        if not entries:
            return []
        counts: dict[int, int] = {}
        low = norm(text)
        for w in _WORD.findall(text or ""):
            n = norm(w).strip("-_.")
            for k in {n, stem(n)} if len(n) > 4 else {n}:
                for gid in idx.get(k, ()):
                    counts[gid] = counts.get(gid, 0) + 1
        for p, gid in phrases:
            if p in low:
                counts[gid] = counts.get(gid, 0) + low.count(p)
        for rx, gid in regexes:
            n = len(rx.findall(text or ""))
            if n:
                counts[gid] = counts.get(gid, 0) + n
        pin = [gid for gid, e in entries.items() if e["pinned"]] if pinned else []
        order = pin + [g for g, _ in sorted(counts.items(), key=lambda x: -x[1]) if g not in pin]
        return [entries[g] for g in order[:limit]]

    def block(self, text: str, limit_chars: int = 2600) -> str:
        """Текст для запроса к модели: «Глоссарий пользователя: …» по терминам из текста. Пусто — если нечего."""
        es = self.match(text)
        if not es:
            return ""
        lines, size = [], 0
        for e in es:
            line = entry_line(e)
            if size + len(line) > limit_chars:
                break
            lines.append(line)
            size += len(line)
        if not lines:
            return ""
        return ("Глоссарий пользователя (термины из этого текста; «черновик» — предложено ИИ и не подтверждено; "
                "если у термина несколько значений — выбирай по признакам):\n" + "\n".join(lines))

    # ------------------------------------------------------------ изображения ↔ термины
    def images_for(self, e: dict, limit: int = 24) -> list[dict]:
        """Описанные изображения, которые могут иллюстрировать термин: поиск по описаниям картинок (и тексту на них)."""
        names = [e["term"]] + list(e.get("aliases") or [])
        words = [w for n in names for w in _WORD.findall(n) if len(w) >= 2][:12]
        if not words:
            return []
        q = " OR ".join('"' + w.replace('"', "") + '"' + ("*" if len(w) > 4 else "") for w in
                        dict.fromkeys(stem(w) if len(w) > 4 else w for w in words))
        try:
            rows = self.store.db.execute(
                """SELECT x.id, x.chat_id, x.msg_id, x.date, d.summary, d.kind AS d_kind, c.title AS chat_title,
                          bm25(media_fts) AS score
                   FROM media_fts f JOIN media x ON x.id=f.rowid JOIN image_desc d ON d.sha256=x.sha256
                   LEFT JOIN chats c ON c.chat_id=x.chat_id
                   WHERE media_fts MATCH ? AND x.kind IN ('photo','image') AND x.status='ok'
                   ORDER BY score LIMIT ?""", (q, limit * 2)).fetchall()
        except Exception as err:  # noqa: BLE001 — неудачный запрос FTS
            log.info("картинки для термина: %s", err)
            return []
        out, seen = [], set()
        for r in rows:
            key = (r["summary"] or "")[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append({"media": r["id"], "chat": r["chat_id"], "msg": r["msg_id"], "date": r["date"],
                        "summary": r["summary"] or "", "kind": r["d_kind"] or "", "chat_title": r["chat_title"] or ""})
            if len(out) >= limit:
                break
        return out

    def for_media(self, media_id: int, limit: int = 8) -> list[dict]:
        """Термины глоссария, о которых изображение (по его описанию и тексту на нём) и к которым оно прикреплено."""
        r = self.store.db.execute("""SELECT d.summary, d.details, d.text FROM media x JOIN image_desc d ON d.sha256=x.sha256
                                     WHERE x.id=?""", (media_id,)).fetchone()
        found = self.match(" ".join(x or "" for x in r), limit, pinned=False) if r else []
        attached = [e for e in self.all() if media_id in (e.get("images") or [])]
        out, seen = [], set()
        for e in attached + found:
            if e["id"] not in seen:
                seen.add(e["id"])
                out.append({"id": e["id"], "term": e["term"], "summary": e.get("summary") or "",
                            "attached": e in attached})
        return out[:limit]

    def names(self, limit: int = 400) -> list[str]:
        return [r[0] for r in self.store.db.execute(
            "SELECT term FROM glossary WHERE status!='hidden' ORDER BY hits DESC, term LIMIT ?", (limit,)).fetchall()]

    # ------------------------------------------------------------ выгрузка / загрузка
    def export(self) -> dict:
        return {"version": 1, "exported": iso(datetime.now(timezone.utc)),
                "terms": [{k: e[k] for k in ("term", "kind", "aliases", "senses", "summary", "details", "owner",
                                             "related", "status", "pinned", "visual")} for e in self.all(True)],
                "questions": [{k: q[k] for k in ("term", "question", "options", "context")} for q in self.questions()]}

    def import_(self, data: dict) -> dict:
        n = 0
        for e in data.get("terms") or []:
            if isinstance(e, dict) and e.get("term"):
                old = self.get(e["term"])
                if old and old["status"] == "confirmed" and e.get("status") != "confirmed":
                    self.save(e, merge=True, source="import")
                else:
                    self.save(dict(e, id=old["id"]) if old else e, source="import")
                n += 1
        q = 0
        for x in data.get("questions") or []:
            if isinstance(x, dict) and self.ask(x.get("term") or "", x.get("question") or "", x.get("options"),
                                                x.get("context") or ""):
                q += 1
        return {"terms": n, "questions": q}


def _examples(x) -> list[str]:
    return [str(v).strip()[:400] for v in (x if isinstance(x, list) else [x] if x else []) if str(v).strip()][:10]


def _links(x) -> list[dict]:
    out = []
    for v in x if isinstance(x, list) else []:
        if isinstance(v, str) and v.startswith("http"):
            v = {"url": v}
        if isinstance(v, dict) and (v.get("url") or v.get("title")):
            out.append({"title": str(v.get("title") or "").strip()[:200], "url": str(v.get("url") or "").strip()[:600]})
    return out[:10]


def _merge_list(a: list, b: list, n: int) -> list:
    have = {norm(x)[:80] for x in a}
    return (a + [x for x in b if norm(x)[:80] not in have])[-n:]


def _merge_links(a: list, b: list) -> list:
    have = {(x.get("url") or x.get("title") or "").lower() for x in a}
    return (a + [x for x in b if (x.get("url") or x.get("title") or "").lower() not in have])[-10:]


def _similar(a: str, b: str) -> bool:
    wa = {stem(w) for w in _WORD.findall(a or "") if len(w) > 3}
    wb = {stem(w) for w in _WORD.findall(b or "") if len(w) > 3}
    if not wa or not wb:
        return False
    return len(wa & wb) / max(1, min(len(wa), len(wb))) >= 0.6


def entry_line(e: dict) -> str:
    head = f"- {e['term']}" + (f" ({', '.join(e['aliases'][:5])})" if e["aliases"] else "") + \
        (f" [{KINDS.get(e.get('kind') or 'other', '')}]" if e.get("kind") and e["kind"] != "other" else "") + \
        (" [черновик]" if e.get("status") == "draft" else "")
    parts = []
    if e.get("summary"):
        parts.append(e["summary"])
    for i, s in enumerate(e["senses"], 1):
        bit = (f"значение {i}: " if len(e["senses"]) > 1 else "") + s.get("meaning", "")
        if s.get("how_to_tell"):
            bit += f" (признаки: {s['how_to_tell']})"
        if s.get("id_format"):
            bit += f" (номер: {s['id_format']})"
        if s.get("owner"):
            bit += f" (отвечает: {s['owner']})"
        parts.append(bit)
    if e.get("owner"):
        parts.append(f"отвечает: {e['owner']}")
    if e.get("visual"):
        parts.append(f"как выглядит: {e['visual'][:200]}")
    if e.get("details"):
        parts.append(e["details"][:300])
    return head + ": " + "; ".join(parts)


def block_for(store: Store | None, text: str, limit_chars: int = 2600, scope: str | None = "all") -> str:
    """Безопасно для любого места: нет базы, нет таблиц, пустой глоссарий — пустая строка.

    scope — область навыка (digest, assistant, describe, glossary, vision, search): вместе с глоссарием подставляются
    правила и поправки пользователя для этой области (skill.py); None — только глоссарий."""
    if store is None or not text:
        return ""
    rules = ""
    if scope is not None:
        try:
            from . import skill
            rules = skill.rules_block(store, text, scope)
            if scope in ("search", "describe", "glossary"):
                lb = skill.links_block(store, text)
                rules = "\n\n".join(x for x in (rules, lb) if x)
        except Exception as e:  # noqa: BLE001 — навык не должен ломать сводку
            log.info("правила навыка не подставлены: %s", logs.err_info(e))
    try:
        gl = Glossary(store).block(text, limit_chars)
    except Exception as e:  # noqa: BLE001 — глоссарий не должен ломать сводку
        log.info("глоссарий не подставлен: %s", logs.err_info(e))
        gl = ""
    return "\n\n".join(x for x in (rules, gl) if x)


# ---------------------------------------------------------------- пополнение моделью
EXTRACT_SCHEMA = """{"terms": [
   {"term": "термин как пишут в чатах", "kind": "service | app | module | team | person | api | process | entity | instruction | problem | abbr | other",
    "aliases": ["другие написания"], "summary": "одна строка: что это",
    "senses": [{"meaning": "значение", "how_to_tell": "как понять, что речь о нём", "id_format": "регулярка номера или пусто",
                "owner": "кто отвечает (команда/человек) или пусто"}],
    "known": "имя существующего термина глоссария, если это его новое значение или синоним, иначе пусто",
    "msgs": [123], "confidence": 0.7}],
 "questions": [{"term": "термин", "question": "что спросить у пользователя (коротко)", "options": ["вариант 1", "вариант 2"],
                "msgs": [123]}]}"""

EXTRACT_RULES = """Ты ведёшь глоссарий инженера технической поддержки: внутренние сервисы, приложения и модули, команды,
API и интеграции, процессы (ITSM, problem management, дежурства), типы заявок и их номера, жаргон и сокращения,
типовые проблемы и их решения. Из переписки выпиши термины, которых нет в глоссарии или у которых появилось новое
значение. Не выписывай общеизвестные слова (сервер, логи, пароль), имена людей без роли, мемы.
Если слово используется в разных смыслах (например, «инцидент» — обращение в ITSM и чат малой аварии; «проблема» —
бытовое слово и сущность problem management) — опиши каждое значение и признаки, как их различать.
Если что-то неясно (два термина пересекаются, непонятно, что значит сокращение, кто отвечает) — задай вопрос
пользователю (questions), с вариантами ответа, если можешь предположить. Не больше 8 терминов и 3 вопросов.
Ничего не выдумывай: только то, что видно из переписки. Отвечай только JSON."""


def apply_found(g: Glossary, data: dict, chat_id: int | None = None, context: str = "",
                stats: dict | None = None) -> dict:
    """Найденное моделью (terms, questions) → черновики статей и вопросы пользователю."""
    stats = stats if stats is not None else {"new": 0, "updated": 0, "questions": 0}
    if not isinstance(data, dict):
        return stats
    for t in data.get("terms") or []:
        if not isinstance(t, dict) or not str(t.get("term") or "").strip():
            continue
        try:
            conf = float(t.get("confidence") if t.get("confidence") is not None else 0.6)
        except (TypeError, ValueError):
            conf = 0.6
        if conf < 0.35:
            continue
        refs = [[chat_id, int(m)] for m in t.get("msgs") or [] if str(m).isdigit()][:5] if chat_id else []
        target = str(t.get("known") or "").strip() or t["term"]
        existed = g.get(target) or g.get(t["term"])
        e = {"term": existed["term"] if existed else t["term"], "kind": t.get("kind"),
             "aliases": ([t["term"]] if existed and norm(t["term"]) != existed["key"] else []) + list(t.get("aliases") or []),
             "summary": t.get("summary"), "senses": t.get("senses") or [], "refs": refs}
        try:
            g.save(e, merge=bool(existed), source="telegram")
        except ValueError:
            continue
        stats["updated" if existed else "new"] += 1
    for q in data.get("questions") or []:
        if not isinstance(q, dict):
            continue
        refs = [[chat_id, int(m)] for m in q.get("msgs") or [] if str(m).isdigit()][:5] if chat_id else []
        if g.ask(q.get("term") or "", q.get("question") or "", q.get("options"), context, refs):
            stats["questions"] += 1

    return stats


class Extractor:
    """Фоновое пополнение глоссария по переписке. save — сразу в базу (черновики), вопросы — в gl_questions."""

    def __init__(self, cfg, store: Store, llm, progress: Callable[[str], None] = print,
                 cancel: Callable[[], bool] | None = None):
        self.cfg, self.store, self.llm, self.progress = cfg, store, llm, progress
        self.cancel = cancel or (lambda: False)
        self.g = Glossary(store)
        from . import privacy
        privacy.attach(llm, cfg, store)
        self.budget = max(6000, min(int((llm.context_tokens - 3500) * 2.2), 22000))
        self.stats = {"chats": 0, "requests": 0, "new": 0, "updated": 0, "questions": 0, "failed": 0}

    def apply(self, data: dict, chat_id: int | None = None, context: str = "") -> None:
        apply_found(self.g, data, chat_id, context, self.stats)

    def scan_chat(self, chat_id: int, since_msg: int = 0, max_parts: int = 3) -> int:
        from .describe import _lines
        c = self.store.get_chat(chat_id)
        rows = self.store.db.execute("SELECT * FROM messages WHERE chat_id=? AND msg_id>? ORDER BY msg_id DESC LIMIT 1500",
                                     (chat_id, since_msg)).fetchall()
        msgs = [self.store._msg(r) for r in reversed(rows)]
        if not msgs:
            return 0
        text = _lines(self.cfg, self.store, chat_id, msgs)
        parts, cur, size = [], [], 0
        for line in text.splitlines():
            if size + len(line) > self.budget and cur:
                parts.append("\n".join(cur))
                cur, size = [], 0
            cur.append(line)
            size += len(line) + 1
        if cur:
            parts.append("\n".join(cur))
        parts = parts[-max_parts:]  # самое свежее
        from .llm import LLMError
        from .llm.base import extract_json
        for p in parts:
            if self.cancel():
                break
            known = self.g.block(p, 2000) or "—"
            names = ", ".join(self.g.names(250)) or "—"
            user = (f"Чат «{c.title if c else chat_id}».\nЕсть в глоссарии (названия): {names}\n\n{known}\n\n"
                    f"Переписка:\n{p}\n\nСхема ответа (строго JSON):\n{EXTRACT_SCHEMA}")
            try:
                raw = self.llm.complete(EXTRACT_RULES, user, max_tokens=1800)
                self.stats["requests"] += 1
                self.apply(extract_json(raw), chat_id, f"чат «{c.title if c else chat_id}»")
            except (LLMError, ValueError) as e:
                self.stats["failed"] += 1
                self.progress(f"  ✗ {str(e)[:160]}")
        self.store.db.execute("""INSERT INTO gl_scan(chat_id, last_msg, scanned_at) VALUES (?,?,?)
                                 ON CONFLICT(chat_id) DO UPDATE SET last_msg=excluded.last_msg, scanned_at=excluded.scanned_at""",
                              (chat_id, max(m.msg_id for m in msgs), iso(datetime.now(timezone.utc))))
        self.store.db.commit()
        self.stats["chats"] += 1
        return len(msgs)

    def run(self, chat_ids: list[int], fresh: bool = False) -> dict:
        last = {r[0]: r[1] for r in self.store.db.execute("SELECT chat_id, last_msg FROM gl_scan").fetchall()}
        for i, cid in enumerate(chat_ids, 1):
            if self.cancel():
                break
            c = self.store.get_chat(cid)
            n = self.scan_chat(cid, 0 if fresh else last.get(cid, 0))
            if n:
                self.progress(f"[{i}/{len(chat_ids)}] {c.title if c else cid}: сообщений {n} → новых терминов "
                              f"{self.stats['new']}, уточнено {self.stats['updated']}, вопросов {self.stats['questions']}")
        return self.stats


def answer_question(cfg, store: Store, llm, qid: int, answer: str) -> dict:
    """Ответ пользователя на вопрос → статья уточняется (моделью, если она есть; иначе ответ дописывается)."""
    g = Glossary(store)
    q = g.close_question(qid, answer)
    if q is None:
        raise ValueError("вопрос не найден")
    e = g.get(q["term"]) if q["term"] else None
    if llm is None:
        if e:
            g.save(dict(e, details=((e.get("details") or "") + f"\nВопрос: {q['question']} Ответ: {answer}").strip(),
                        status="confirmed"))
        else:
            g.save({"term": q["term"] or answer[:40], "summary": answer, "status": "confirmed"})
        return {"updated": True}
    from .llm.base import extract_json
    from . import privacy
    privacy.attach(llm, cfg, store)
    cur = json.dumps({k: e[k] for k in ("term", "kind", "aliases", "summary", "senses", "details", "owner")},
                     ensure_ascii=False) if e else "{}"
    user = (f"Статья глоссария (JSON): {cur}\n\nВопрос пользователю: {q['question']}\nЕго ответ: {answer}\n\n"
            "Обнови статью с учётом ответа (ответ пользователя — главный источник). Верни статью целиком в том же "
            "формате JSON: term, kind, aliases, summary, senses [{meaning, how_to_tell, id_format, owner}], details, owner.")
    raw = llm.complete("Ты аккуратно редактируешь глоссарий инженера поддержки. Отвечай только JSON.", user, max_tokens=1500)
    data = extract_json(raw)
    if not isinstance(data, dict) or not data.get("term"):
        raise ValueError("модель не вернула статью")
    data["status"] = "confirmed"
    if e:
        data["id"] = e["id"]
        data.setdefault("pinned", e["pinned"])
        data.setdefault("related", e["related"])
        data["refs"] = e["refs"]
    return {"entry": g.save(data, source="user")}
