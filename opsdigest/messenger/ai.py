"""ИИ прямо в чате: сводка чата за период, вопрос по сообщению или выделенному фрагменту, черновик ответа,
поиск связанного в других чатах (по базе, без модели) и разбор найденного моделью, запрос в Maverick
(его плагины — почта, ITSM, трекер, Confluence).

Всё, что уходит во внешнюю модель, обезличивается и смягчается (privacy.attach). Черновик никогда не
отправляется сам — окно вставляет его в поле ввода.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import prompts
from ..ask import STOP
from ..digest import KIND_RU, get_tz, transcript_lines
from ..llm import LLMClient, make_client
from ..storage import Message, Store, iso, parse_iso
from .views import snippet

Progress = Callable[[str], None]

SYSTEM = """Ты — ассистент инженера технической поддержки (2-я линия). Работаешь прямо в его Telegram: читаешь переписку
одного чата и помогаешь с ней.

О пользователе:
{profile}

Правила:
- Пиши по-русски, кратко и по делу. Опирайся только на приведённые сообщения, ничего не выдумывай.
- Ссылайся на сообщения их номерами в формате #123 — пользователь кликнет и перейдёт к сообщению.
- Формат строки переписки: #номер [время] Автор: текст. «Я» — сам пользователь. Вложения — в квадратных скобках.
{rules}
- Оформление — простой Markdown: заголовки ###, списки «- », **жирный**. Без таблиц."""

SUMMARY_TASK = """Сделай сводку переписки чата за период по разделам (пустые разделы пропускай):
### Главное
2–4 предложения: о чём говорили и к чему пришли.
### Нужна ваша реакция
Что ждут именно от пользователя: вопросы к нему (@ник, ответ на его сообщение, обращение по имени, если ясно, что к нему,
а не к тёзке), просьбы, обещания, которые он не выполнил (с номерами сообщений). То, что взял другой, сюда не пиши.
### Ведут другие — проследить
Кто какую задачу взял и стоит ли пользователю проконтролировать исполнение.
### Без исполнителя
Вопросы и задачи, которые никто не взял (особенно в зоне пользователя).
### Решения и договорённости
### Открытые вопросы
Что зависло, кто кого ждёт.
### Номера
Инциденты, задачи, обращения, хосты — с кратким пояснением.
"""


def make_llm(cfg, provider: str | None = None) -> LLMClient:
    try:
        name, prof = cfg.llm_profile(provider or None)
    except KeyError:
        raise ValueError(f"модель «{provider}» не настроена — шаг «Модель»") from None
    return make_client(name, prof)


def _system(cfg, store: Store) -> str:
    import json
    me = store.kv_get("me")
    me_name = json.loads(me).get("name", "") if me else ""
    return SYSTEM.format(profile=prompts.profile_block(cfg.profile, me_name),
                         rules=prompts.ADDRESS_RULES + "\n" + prompts.OWNER_RULES)


def _attach(llm: LLMClient, cfg, store: Store):
    from .. import privacy
    return privacy.attach(llm, cfg, store)


def _gl(store: Store, text: str) -> str:
    """Глоссарий пользователя по терминам текста — в начало запроса (пусто, если нечего)."""
    from ..glossary import block_for
    b = block_for(store, text, scope="assistant")
    return b + "\n\n" if b else ""


def budget(llm: LLMClient) -> int:
    return max(6000, min(int((llm.context_tokens - 3500) * 2.4), 60000))


def _chat_head(store: Store, chat_id: int, cfg=None) -> tuple[str, str]:
    c = store.get_chat(chat_id)
    if not c:
        return str(chat_id), ""
    head = f"Чат «{c.title}» ({KIND_RU.get(c.kind, c.kind)})"
    if c.note:
        head += "\n" + prompts.chat_note_line(c.note)
    rule = prompts.MODE_RULES.get(c.mode, "")
    if rule:
        head += "\n" + rule
    if cfg is not None:
        import json

        from .. import namesakes
        from ..digest import chat_notes
        me = json.loads(store.kv_get("me") or "{}")
        forms = namesakes.my_name_forms(cfg.profile, me.get("name", ""))
        for n in chat_notes(cfg, store, c, forms, me.get("id")):
            head += "\n" + n
    return c.title, head


def _lines(cfg, store: Store, chat_id: int, msgs: list[Message]) -> list[str]:
    from ..namesakes import mine_targets
    tz = get_tz(cfg.digest.get("timezone"))
    media = store.media_of(chat_id, [m.msg_id for m in msgs]) if msgs else {}
    return transcript_lines(msgs, tz, None, media, mine_targets(store, chat_id, msgs))


def _chunks(lines: list[str], size: int) -> list[str]:
    out, cur, n = [], [], 0
    for line in lines:
        if n + len(line) > size and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line[:size])
        n += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def cited(text: str, store: Store, chat_id: int | None = None) -> list[int]:
    ids = [int(x) for x in dict.fromkeys(re.findall(r"#(\d{1,9})\b", text or ""))]
    if chat_id is None or not ids:
        return ids
    have = {r["msg_id"] for r in store.rows_by_ids(chat_id, ids)}
    return [i for i in ids if i in have]


def period_text(since: datetime, until: datetime, tz) -> str:
    a, b = since.astimezone(tz), until.astimezone(tz)
    if a.date() == b.date():
        return f"{a:%d.%m.%Y} {a:%H:%M}–{b:%H:%M}"
    return f"{a:%d.%m.%Y %H:%M} — {b:%d.%m.%Y %H:%M}"


# ================================================================ сводка чата
def summarize_chat(cfg, store: Store, llm: LLMClient, chat_id: int, since: datetime, until: datetime,
                   progress: Progress = print, topic: int | None = None) -> dict:
    tz = get_tz(cfg.digest.get("timezone"))
    msgs = store.messages(chat_id, iso(since), iso(until), topic=topic)
    title, head = _chat_head(store, chat_id, cfg)
    per = period_text(since, until, tz)
    if not msgs:
        return {"text": f"За период {per} сообщений в чате «{title}» нет (в базе).", "refs": [], "count": 0}
    _attach(llm, cfg, store)
    system = _system(cfg, store)
    lines = _lines(cfg, store, chat_id, msgs)
    parts = _chunks(lines, budget(llm))
    progress(f"Сообщений: {len(msgs)}; модель: {llm.label}" + (f"; частей: {len(parts)}" if len(parts) > 1 else ""))
    if len(parts) == 1:
        text = llm.complete(system, _gl(store, head + "\n" + "\n".join(lines)) + f"{head}\nПериод: {per}.\n\n{SUMMARY_TASK}\nПереписка:\n{parts[0]}",
                            max_tokens=1800)
    else:
        notes = []
        for i, p in enumerate(parts, 1):
            progress(f"Часть {i} из {len(parts)}…")
            notes.append(llm.complete(system, _gl(store, head + "\n" + "\n".join(lines)) + f"{head}\nПериод: {per}. Это часть {i} из {len(parts)} переписки.\n"
                                      "Законспектируй её пунктами: события, вопросы к пользователю, решения, номера. "
                                      f"Указывай номера сообщений #N.\n\nПереписка:\n{p}", max_tokens=1200))
        progress("Собираю итог…")
        joined = "\n\n".join(f"--- часть {i} ---\n{n}" for i, n in enumerate(notes, 1))
        text = llm.complete(system, _gl(store, head + "\n" + "\n".join(lines)) + f"{head}\nПериод: {per}.\nНиже — конспекты частей переписки по порядку.\n\n"
                            f"{SUMMARY_TASK}\nКонспекты:\n{joined}", max_tokens=1800)
    return {"text": text.strip(), "refs": cited(text, store, chat_id), "count": len(msgs), "period": per,
            "model": llm.label}


# ================================================================ вопрос по чату / сообщению
def _focus(store: Store, chat_id: int, ids: list[int], before: int = 15, after: int = 10) -> list[Message]:
    seen, out = set(), []
    for mid in ids[:5]:
        for m in store.context(chat_id, mid, before=before, after=after):
            if m.msg_id not in seen:
                seen.add(m.msg_id)
                out.append(m)
    return sorted(out, key=lambda m: m.msg_id)


def _recent(store: Store, chat_id: int, n: int = 120, topic: int | None = None) -> list[Message]:
    rows = store.page(chat_id, limit=n, topic=topic)
    return [store._msg(r) for r in reversed(rows)]


def ask_chat(cfg, store: Store, llm: LLMClient, chat_id: int, question: str, *, ids: list[int] | None = None,
             quote: str = "", progress: Progress = print, topic: int | None = None) -> dict:
    title, head = _chat_head(store, chat_id, cfg)
    msgs = _focus(store, chat_id, ids) if ids else _recent(store, chat_id, topic=topic)
    if not msgs:
        return {"text": "В базе нет сообщений этого чата — откройте его, чтобы подгрузить переписку.", "refs": []}
    _attach(llm, cfg, store)
    lines = _lines(cfg, store, chat_id, msgs)
    ctx = "\n".join(lines)[-budget(llm):]
    q = f"Вопрос пользователя: {question.strip()}"
    if quote:
        q += f"\nВыделенный фрагмент{' (из сообщения #' + str(ids[0]) + ')' if ids else ''}: «{quote.strip()[:3000]}»"
    elif ids:
        q += f"\nРечь о сообщении #{ids[0]}."
    progress(f"Контекст: {len(msgs)} сообщ.; модель: {llm.label}")
    text = llm.complete(_system(cfg, store), _gl(store, f"{q}\n{ctx}") + f"{head}\n\n{q}\n\nПереписка:\n{ctx}",
                        max_tokens=1500)
    return {"text": text.strip(), "refs": cited(text, store, chat_id), "model": llm.label}


# ================================================================ черновик ответа
def draft_reply(cfg, store: Store, llm: LLMClient, chat_id: int, *, reply_to: int | None = None, hint: str = "",
                quote: str = "", progress: Progress = print, topic: int | None = None) -> dict:
    title, head = _chat_head(store, chat_id, cfg)
    msgs = _focus(store, chat_id, [reply_to], before=25, after=5) if reply_to else _recent(store, chat_id, 60, topic)
    if not msgs:
        return {"text": "", "note": "нет переписки для контекста"}
    _attach(llm, cfg, store)
    ctx = "\n".join(_lines(cfg, store, chat_id, msgs))[-budget(llm):]
    task = ("Напиши черновик сообщения, которое ПОЛЬЗОВАТЕЛЬ (автор «Я») отправит в этот чат"
            + (f" в ответ на сообщение #{reply_to}" if reply_to else "") + ". От первого лица пользователя, коротко, "
            "вежливо, по существу, в стиле его прежних сообщений. Не обращайся к самому пользователю. Если для ответа "
            "не хватает данных — черновик-уточнение. Верни ТОЛЬКО текст сообщения, без пояснений, кавычек и Markdown.")
    if quote:
        task += f"\nОтвечаем на фрагмент: «{quote.strip()[:2000]}»."
    if hint.strip():
        task += f"\nПожелание пользователя к ответу: {hint.strip()[:1000]}"
    progress(f"Пишу черновик ({llm.label})…")
    text = llm.complete(_system(cfg, store), _gl(store, ctx) + f"{head}\n\n{task}\n\nПереписка:\n{ctx}",
                        max_tokens=700).strip()
    text = re.sub(r"^(черновик|ответ)\s*:\s*", "", text, flags=re.I).strip().strip("«»\"").strip()
    return {"text": text, "model": llm.label}


# ================================================================ связанное в других чатах
NUM_RE = re.compile(r"(?i)\b(?:inc|req|chg|rfc|sr|prb|task|crq|wo)[-_ ]?\d{3,}\b|\b[a-z][a-z0-9]{1,9}-\d{1,7}\b")


def key_terms(text: str, n: int = 8) -> list[str]:
    """Самые «говорящие» слова фрагмента: номера, хосты, слова с цифрами, длинные слова."""
    text = text or ""
    nums = [m.group(0) for m in NUM_RE.finditer(text)]
    words = re.findall(r"[\w][\w.\-]{2,}", text.lower())
    scored = []
    for w in dict.fromkeys(words):
        if w in STOP or w.isdigit():
            continue
        score = len(w) + (8 if re.search(r"\d", w) else 0) + (6 if "." in w or "-" in w else 0)
        scored.append((score, w))
    scored.sort(reverse=True)
    out = list(dict.fromkeys([x.lower() for x in nums] + [w for _, w in scored]))
    return out[:n]


def fts(terms: list[str]) -> str:
    q = []
    for t in terms:
        t = t.strip(".-_")
        if not t:
            continue
        if re.search(r"\d", t) or len(t) <= 6:
            q.append('"' + t.replace('"', "") + '"')
        else:
            q.append('"' + t[:6] + '"*')
    return " OR ".join(dict.fromkeys(q))


def related(store: Store, text: str, *, chat_id: int | None = None, msg_id: int | None = None,
            limit: int = 40, other_chats_only: bool = True) -> dict:
    terms = key_terms(text)
    q = fts(terms)
    if not q:
        return {"terms": [], "groups": [], "total": 0}
    hits = store.search(q, limit=limit * 2)
    groups: dict[int, dict] = {}
    n = 0
    for m, title in hits:
        if other_chats_only and chat_id is not None and m.chat_id == chat_id:
            continue
        if m.chat_id == chat_id and m.msg_id == msg_id:
            continue
        g = groups.setdefault(m.chat_id, {"chat": m.chat_id, "title": title or str(m.chat_id), "items": []})
        if len(g["items"]) < 6:
            g["items"].append({"id": m.msg_id, "date": m.date, "from": "Вы" if m.is_out else m.sender,
                               "text": snippet(m.text, 220)})
            n += 1
        if n >= limit:
            break
    gs = sorted(groups.values(), key=lambda g: max(i["date"] for i in g["items"]), reverse=True)
    return {"terms": terms, "groups": gs, "total": n}


def related_analyze(cfg, store: Store, llm: LLMClient, text: str, *, chat_id: int | None = None,
                    msg_id: int | None = None, progress: Progress = print) -> dict:
    found = related(store, text, chat_id=chat_id, msg_id=msg_id, limit=25)
    if not found["groups"]:
        return {"text": "В других чатах (в базе) ничего похожего не нашлось. Ключевые слова: "
                        + (", ".join(found["terms"]) or "—"), "refs": [], "found": found}
    _attach(llm, cfg, store)
    tz = get_tz(cfg.digest.get("timezone"))
    blocks, refs = [], []
    for g in found["groups"][:10]:
        ids = [i["id"] for i in g["items"]]
        msgs = _focus(store, g["chat"], ids, before=2, after=2)
        lines = transcript_lines(msgs, tz, None, store.media_of(g["chat"], [m.msg_id for m in msgs]))
        blocks.append(f"=== «{g['title']}» ===\n" + "\n".join(lines))
        refs += [{"chat": g["chat"], "id": m.msg_id, "title": g["title"]} for m in msgs]
    ctx = "\n\n".join(blocks)[-budget(llm):]
    progress(f"Найдено в других чатах: {found['total']} сообщ. в {len(found['groups'])} чатах; разбираю ({llm.label})…")
    task = ("Пользователь выделил фрагмент переписки и хочет знать, где ещё это обсуждалось. Ниже — найденные по "
            "ключевым словам сообщения из других чатов (не все могут быть связаны). Ответь: где и когда обсуждалось то же "
            "самое (чат, #номер), чем закончилось, кто занимался, есть ли номера инцидентов/задач, что из этого полезно "
            "пользователю сейчас. Несвязанное не упоминай. Если связанного нет — так и скажи.")
    out = llm.complete(_system(cfg, store), _gl(store, text + "\n" + ctx) +
                       f"{task}\n\nФрагмент: «{text.strip()[:3000]}»\n\nНайденное:\n{ctx}",
                       max_tokens=1500)
    want = set(cited(out, store))
    return {"text": out.strip(), "refs": [r for r in refs if r["id"] in want], "found": found, "model": llm.label}


# ================================================================ объяснить: что происходит, термины, к кому идти
EXPLAIN_TASK = """Пользователь не до конца понимает, что происходит в этом месте переписки, и просит объяснить. Ответь разделами
(пустые пропускай):
### Что происходит
2–5 предложений простыми словами.
### Термины
Каждый термин, сокращение, сервис или номер из фрагмента, который может быть непонятен: что он значит здесь. Если по
глоссарию у термина несколько значений (например, «инцидент» — обращение в ITSM или чат малой аварии) — какое именно и
по каким признакам. Если термина нет в глоссарии и из переписки не ясно — так и напиши: «нет в глоссарии — уточните».
### Кому это отнести
Кто за это отвечает и к кому обратиться (по заметкам о людях, глоссарию и переписке), нужно ли что-то делать самому
пользователю — с учётом его зоны ответственности.
### Что можно сделать
Конкретные шаги, если они есть. Ссылайся на сообщения #номер."""


def explain(cfg, store: Store, llm: LLMClient, chat_id: int, *, ids: list[int] | None = None, text: str = "",
            use_maverick: bool = True, progress: Progress = print, topic: int | None = None) -> dict:
    title, head = _chat_head(store, chat_id, cfg)
    msgs = _focus(store, chat_id, ids, before=12, after=6) if ids else _recent(store, chat_id, 50, topic)
    if not msgs and not text.strip():
        return {"text": "Нечего объяснять: в базе нет переписки этого чата.", "refs": []}
    _attach(llm, cfg, store)
    ctx = "\n".join(_lines(cfg, store, chat_id, msgs))[-budget(llm):] if msgs else ""
    frag = text.strip() or (next((m.text for m in msgs if ids and m.msg_id == ids[0]), "") if msgs else "")
    wiki = ""
    profiles = (cfg.get("llm") or {}).get("profiles") or {}
    is_mav = getattr(llm, "name", "") == "maverick"
    if use_maverick and "maverick" in profiles and not is_mav:
        try:
            progress("Спрашиваю Maverick: вики (Confluence) и ITSM…")
            wiki = maverick_lookup(cfg, store, frag or ctx[-2500:], chat_id=chat_id, question=(
                "Объясни термины, сокращения и номера из этого фрагмента рабочей переписки так, как они понимаются в "
                "нашей компании: найди в Confluence (вики), ITSM и других доступных источниках, что это за сервисы, "
                "процессы, команды, кто за них отвечает. Для каждой находки — источник и одна-две строки."),
                progress=lambda s: None)["text"]
        except Exception as e:  # noqa: BLE001 — без Maverick объясняем по глоссарию и переписке
            progress(f"Maverick не ответил ({str(e)[:120]}) — объясняю по глоссарию и переписке.")
    task = EXPLAIN_TASK + ("\nПоищи также в доступных тебе источниках (Confluence, ITSM) — укажи, что нашёл." if is_mav else "")
    user = (f"{head}\n\n{task}\n\n" + (f"Фрагмент, который нужно объяснить: «{frag[:3000]}»\n\n" if frag else "") +
            (f"Переписка вокруг:\n{ctx}\n\n" if ctx else "") +
            (f"Что нашёл корпоративный ассистент Maverick (вики, ITSM):\n{wiki[:6000]}\n" if wiki else ""))
    progress(f"Объясняю ({llm.label})…")
    out = llm.complete(_system(cfg, store), _gl(store, frag + "\n" + ctx + "\n" + wiki) + user, max_tokens=1800)
    return {"text": out.strip(), "refs": cited(out, store, chat_id), "model": llm.label + (" + Maverick" if wiki else "")}


# ================================================================ Maverick: почта, ITSM, трекер, Confluence
def maverick_lookup(cfg, store: Store, text: str, *, chat_id: int | None = None, question: str = "",
                    progress: Progress = print) -> dict:
    profiles = (cfg.get("llm") or {}).get("profiles") or {}
    if "maverick" not in profiles:
        raise ValueError("Maverick не настроен — шаг «Модель» → Maverick")
    from .. import plugins
    llm = plugins.client(cfg, store, "any")  # своя вводная: «используй плагины» (обычная запрещает инструменты)
    title = ""
    if chat_id is not None:
        c = store.get_chat(chat_id)
        title = c.title if c else ""
    terms = key_terms(text, 10)
    task = (question.strip() or "Найди в корпоративных источниках, доступных тебе (почта, ITSM — инциденты и обращения, "
            "трекер задач, Confluence), всё, что связано с этим фрагментом рабочей переписки: похожие инциденты и их "
            "решения, задачи, письма, статьи. Для каждой находки — источник, номер или ссылка и одна строка сути. "
            "Если доступа к источнику нет — так и скажи.")
    prompt = (f"{task}\n\nФрагмент из Telegram-чата{(' «' + title + '»') if title else ''}:\n«{text.strip()[:4000]}»\n\n"
              f"Ключевые слова для поиска: {', '.join(terms) or '—'}")
    progress("Спрашиваю Maverick (почта, ITSM, трекер, Confluence)…")
    out = llm.complete("Ты — корпоративный ассистент. Ищи по доступным тебе источникам и отвечай по-русски, кратко.",
                       prompt, max_tokens=1800)
    return {"text": out.strip(), "refs": [], "model": llm.label}


def parse_period(period: str, since: str | None, until: str | None, tz, read_in: int = 0,
                 store: Store | None = None, chat_id: int | None = None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    p = (period or "day").lower()
    if p == "custom" and since:
        a = datetime.strptime(since[:10], "%Y-%m-%d").replace(tzinfo=tz)
        b = (datetime.strptime(until[:10], "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)) if until else now
        return a, min(b, now)
    if p == "unread" and store is not None and chat_id is not None:
        rows = store.page(chat_id, after=read_in or 0, limit=1)
        if rows:
            return parse_iso(rows[0]["date"]) - timedelta(seconds=1), now
        return now - timedelta(hours=24), now
    if p == "today":
        local = now.astimezone(tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0), now
    hours = {"hour": 1, "3h": 3, "day": 24, "3d": 72, "week": 168, "month": 720}.get(p, 24)
    return now - timedelta(hours=hours), now
