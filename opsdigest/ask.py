"""Вопросы по накопленной переписке: поиск по базе (FTS5, в т.ч. по описаниям изображений и содержимому файлов)
+ ответ модели со ссылками на сообщения.

Изображения: если среди найденного есть неописанные картинки, модель сначала решает (по тексту вокруг), какие из них
нужны для ответа, — они описываются (и запоминаются навсегда), затем строится ответ. Если для ответа нужен
нескачанный тяжёлый файл, модель называет его («НУЖНЫ ФАЙЛЫ: m12») — окно предлагает докачать.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import logs, prompts
from .digest import Redactor, get_tz, with_media
from .llm import LLMClient, LLMError
from .media import KIND_RU, human_size
from .storage import IMAGE_KINDS, Message, Store, iso, parse_iso

STOP = set("""а без бы был была были было в вам вас весь во вот все всё всего всех вы где да даже для до его
ее её если есть еще ещё же за здесь и из или им их к как ко когда кто ли либо мне может мы на над надо наш не
нет ни них но ну о об однако он она они оно от очень по под при про с со так также такой там те тем то того
тоже той только том ты у уже хотя чего чей чем что чтобы чье чья эта эти это я был какие какой какая какое
было ли скажи расскажи покажи найди почему зачем сколько""".split())
NEED_RE = re.compile(r"(?im)^\W*НУЖН\w*\s+ФАЙЛ\w*\s*:\s*(.+)$")


def fts_query(question: str) -> str:
    words = re.findall(r"[\w\-]+", question.lower())
    terms = []
    for w in words:
        if w in STOP or len(w) < 3:
            continue
        if re.fullmatch(r"[a-z]+[-_]?\d+", w):  # номера: inc0012345, prj-12
            terms.append(f'"{w}"')
            continue
        w = w.replace("-", " ")
        stem = w[:6] if len(w) > 6 else w  # грубая основа для русской морфологии
        terms.append(f'"{stem}"*')
    return " OR ".join(dict.fromkeys(terms))


def _context(store: Store, hits: list[tuple[Message, str]]) -> list[tuple[str, Message]]:
    seen: set[tuple[int, int]] = set()
    out: list[tuple[str, Message]] = []
    for m, title in hits[:15]:
        for c in store.context(m.chat_id, m.msg_id, before=2, after=2):
            if (c.chat_id, c.msg_id) in seen:
                continue
            seen.add((c.chat_id, c.msg_id))
            out.append((title or str(c.chat_id), c))
    return out


def _media_map(store: Store, ctx: list[tuple[str, Message]]) -> dict[tuple[int, int], dict]:
    by_chat: dict[int, list[int]] = {}
    for _, m in ctx:
        by_chat.setdefault(m.chat_id, []).append(m.msg_id)
    out: dict[tuple[int, int], dict] = {}
    for cid, ids in by_chat.items():
        for mid, x in store.media_of(cid, ids).items():
            out[(cid, mid)] = x
    return out


def answer(cfg, store: Store, llm: LLMClient, question: str, days: int = 14,
           progress: Callable[[str], None] = print, vision_llm: LLMClient | None = None,
           auto_describe: int | None = None) -> dict:
    """Ответ на вопрос: {"answer", "images": [...], "need_files": [...], "described": n}."""
    tz = get_tz(cfg.digest.get("timezone"))
    since = iso(datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    q = fts_query(question)
    hits = store.search(q, since=since, limit=40) if q else []
    progress(f"Найдено сообщений по запросу: {len(hits)}")
    redact = Redactor((cfg.get("privacy") or {}).get("redact") or []) if llm.external else None
    from . import privacy
    p = privacy.attach(llm, cfg, store)
    if vision_llm is not None and vision_llm is not llm:
        privacy.attach(vision_llm, cfg, store, registry=p.reg if p else None)
    ctx = _context(store, hits)
    media = _media_map(store, ctx)

    # ---- модель решает, какие неописанные изображения прочитать
    limit = int((cfg.get("media") or {}).get("auto_describe_max", 8)) if auto_describe is None else auto_describe
    undesc = [x for x in media.values() if x.get("kind") in IMAGE_KINDS and x["status"] == "ok"
              and not x["described"] and x.get("desc_state") != "unsupported"]
    described = 0
    if undesc and limit > 0:
        from .vision import Describer, VisionUnavailable, pick_important
        chosen = pick_important(llm, question, store, undesc, limit, tz)
        if chosen:
            progress(f"Модель решила прочитать изображений: {len(chosen)} из {len(undesc)} неописанных")
            d = Describer(cfg, store, vision_llm, progress=progress)
            try:
                d.describe(chosen)
            except VisionUnavailable as e:
                progress(f"  ✗ изображения не прочитаны: {e}")
            except LLMError as e:
                progress(f"  ✗ изображения не прочитаны: {e}")
            described = d.stats["described"]
            media = _media_map(store, ctx)

    # ---- переписка для модели
    blocks: dict[str, list[str]] = {}
    for title, c in ctx:
        dt = parse_iso(c.date).astimezone(tz)
        text = c.text.replace("\n", " ⏎ ")[:800]
        x = media.get((c.chat_id, c.msg_id))
        if x:
            text = with_media(text, x, full=True)
        if redact:
            text = redact(text)
        who = "Я" if c.is_out else c.sender
        blocks.setdefault(title, []).append(f"#{c.msg_id} [{dt:%d.%m %H:%M}] {who}: {text}")
    notes = {}
    for title, c in ctx:  # описание чата от пользователя — модели в заголовок блока
        if title not in notes:
            ch = store.get_chat(c.chat_id)
            notes[title] = f" (описание чата от пользователя: {ch.note})" if ch and ch.note else ""
    context = "\n\n".join(f"=== «{t}»{notes.get(t, '')} ===\n" + "\n".join(lines) for t, lines in blocks.items())
    last = store.last_digest()
    digest_text = ""
    if last:
        ov = last.get("overview") or {}
        digest_text = ov.get("headline", "") + "\n" + "\n".join(
            f"- {t.get('chat_title', '')}: {t.get('title', '')}" for t in ov.get("top", [])[:10])
    from .glossary import block_for
    out = llm.complete(prompts.ASK_SYSTEM, prompts.with_glossary(block_for(store, question + "\n" + context, scope="assistant"),
                                                                 prompts.ask_prompt(question, context, digest_text)),
                       max_tokens=2000)

    # ---- какие файлы модель просит докачать
    need: list[dict] = []
    m = NEED_RE.search(out)
    if m:
        out = (out[:m.start()] + out[m.end():]).strip()
        for mid in dict.fromkeys(int(x) for x in re.findall(r"m(\d+)", m.group(1))):
            row = store.get_media(mid)
            if row:
                need.append({"id": mid, "name": row.get("name") or KIND_RU.get(row.get("kind") or "", "файл"),
                             "kind": row.get("kind"), "size": human_size(row.get("size")), "status": row["status"],
                             "chat": row.get("chat_title") or ""})
    # ---- изображения для показа рядом с ответом: сначала те, на чьи сообщения сослался ответ
    cited = {int(x) for x in re.findall(r"#(\d+)", out)}
    imgs = [x for x in media.values() if x.get("kind") in IMAGE_KINDS and x["status"] == "ok" and x.get("path")]
    imgs.sort(key=lambda x: (x["msg_id"] not in cited, not x["described"], -(x.get("importance") or 0)))
    images = [{"id": x["id"], "msg_id": x["msg_id"], "chat": x.get("chat_title") or "", "summary": x.get("summary") or "",
               "date": parse_iso(x["date"]).astimezone(tz).strftime("%d.%m %H:%M") if x.get("date") else ""}
              for x in imgs[:8] if x["described"] or x["msg_id"] in cited]
    logs.get("ask").info("вопрос: найдено %d, контекст %d сообщ., прочитано изображений %d, просит файлов %d",
                         len(hits), len(ctx), described, len(need))
    return {"answer": out, "images": images, "need_files": need, "described": described}
