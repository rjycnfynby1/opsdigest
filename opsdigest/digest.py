"""Построение сводки.

1. Берём из базы чаты с сообщениями за период (кроме исключённых), важные — первыми.
   Новые изображения периода обязательно описываются моделью (если не описаны раньше) — описание встаёт в переписку
   вместо «[фото]»; содержимое лёгких документов — тоже.
2. map: переписка пакетами (несколько мелких чатов в одном запросе, большой чат — частями) -> сводка по чату.
3. reduce: сводки чатов + сквозные упоминания INC/задач -> «главное», «нужна реакция», инциденты, компетенции.
4. Если reduce не получился — собираем «главное» из пунктов чатов без LLM.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable

from . import logs, prompts
from . import namesakes as namesakes_mod
from .llm import LLMClient, LLMError
from .media import annotate
from .llm.base import extract_json
from .storage import IMAGE_KINDS, Chat, Message, Store, iso, parse_iso

PRIO_ORDER = {"high": 0, "normal": 1, "low": 2}
ITEM_PRIO = {"high": 0, "medium": 1, "low": 2}
ACTION_TYPES = {"reply", "action", "follow_up", "question", "incident"}
TO_ME = {"tag", "reply", "name", "role", "no"}
OWNERS = {"me", "other", "nobody", "unclear"}
CHANGES = {"new", "updated", "same", "resolved"}   # что с пунктом по сравнению с прошлой сводкой (history.py)
KIND_RU = {"private": "личный", "group": "группа", "channel": "канал", "bot": "бот"}
MAX_MSG_CHARS = 1200


# ---------------------------------------------------------------- время
def get_tz(name: str | None) -> tzinfo:
    if not name or name == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — на Windows без пакета tzdata зон нет
        m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?", name)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
        return datetime.now().astimezone().tzinfo or timezone.utc


def period_range(period: str, tz: tzinfo, now: datetime | None = None, since: datetime | None = None,
                 until: datetime | None = None) -> tuple[datetime, datetime]:
    """day — последние 24 часа; week — 7 дней; month — 30 дней; since/until — явные границы."""
    now = now or datetime.now(tz)
    until = until or now
    if since is None:
        days = {"day": 1, "week": 7, "month": 30}.get(period, 1)
        since = until - timedelta(days=days)
    return since, until


class PeriodError(ValueError):
    """Период сводки задан неверно — текст понятен пользователю."""


def hours_label(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return " ".join(x for x in (f"{h} ч" if h else "", f"{m} мин" if m else "") if x) or "0 мин"


def _local_dt(s: str, tz: tzinfo, end: bool = False) -> datetime:
    """«2026-11-09», «2026-11-09T14:30» или «2026-11-09 14:30» — время пользователя (часовой пояс сводки)."""
    s = str(s or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end:   # «по 09.11» — весь этот день
            d += timedelta(days=1)
        return d.replace(tzinfo=tz)
    raise PeriodError(f"не понял дату «{s[:30]}» — нужен формат ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ")


def resolve_period(b: dict, tz: tzinfo, now: datetime | None = None) -> dict:
    """Период сводки из запроса окна → {since, until, kind, label, minutes}.

    period: day | week | month — последние сутки/7/30 дней; hours — последние hours ч + minutes мин (h1/h2/h3 —
    то же); custom — since/until (дата или дата и время; until пусто — сейчас). Старое окно присылало period=day и
    since=ГГГГ-ММ-ДД — это «с даты по сейчас»."""
    now = (now or datetime.now(tz)).astimezone(tz)
    p = str(b.get("period") or "day").strip().lower()
    if re.fullmatch(r"h\d{1,3}", p):
        b = {**b, "hours": int(p[1:])}
        p = "hours"
    if b.get("since") and p in ("day", "week", "month", "since"):
        p = "custom"
    if p == "hours":
        try:
            minutes = round(float(b.get("hours") or 0) * 60 + float(b.get("minutes") or 0))
        except (TypeError, ValueError):
            raise PeriodError("часы и минуты — числами") from None
        if minutes < 5:
            raise PeriodError("период слишком короткий — хотя бы 5 минут")
        if minutes > 60 * 24 * 366:
            raise PeriodError("период больше года — выберите «Свой период» с датами")
        since = now - timedelta(minutes=minutes)
        return {"since": since, "until": now, "kind": "hours", "label": hours_label(minutes), "minutes": minutes}
    if p == "custom":
        if not b.get("since"):
            raise PeriodError("укажите, с какого времени собрать сводку")
        since = _local_dt(b["since"], tz)
        until = _local_dt(b["until"], tz, end=True) if b.get("until") else now
        if until > now:
            until = now
        if since >= until:
            raise PeriodError("начало периода позже конца (или в будущем) — проверьте даты")
        if until - since > timedelta(days=366):
            raise PeriodError("период больше года — сузьте его")
        minutes = round((until - since).total_seconds() / 60)
        return {"since": since, "until": until, "kind": "custom", "label": "период", "minutes": minutes}
    if p not in ("day", "week", "month"):
        raise PeriodError(f"неизвестный период «{p[:20]}»")
    since, until = period_range(p, tz, now=now)
    return {"since": since, "until": until, "kind": p, "label": {"day": "сутки", "week": "неделю", "month": "месяц"}[p],
            "minutes": round((until - since).total_seconds() / 60)}


class DigestCancelled(Exception):
    """Сводку остановили (кнопкой «Остановить» или из «Задач»)."""
    cancelled = True


def fmt_period(since: datetime, until: datetime, tz: tzinfo) -> str:
    a, b = since.astimezone(tz), until.astimezone(tz)
    return f"{a:%d.%m.%Y %H:%M} — {b:%d.%m.%Y %H:%M}"


# ---------------------------------------------------------------- ссылки
def chat_link(chat: Chat, last_msg_id: int | None = None) -> str | None:
    """Ссылка на чат: по @username, иначе — на последнее сообщение периода (для супергрупп и каналов)."""
    if chat.source != "telegram":
        return None
    if chat.username:
        return f"https://t.me/{chat.username}"
    return msg_link(chat, last_msg_id) if last_msg_id else None


def msg_link(chat: Chat, msg_id: int) -> str | None:
    if chat.source != "telegram":
        return None
    cid = chat.chat_id
    if chat.kind in ("group", "channel") and chat.username:
        return f"https://t.me/{chat.username}/{msg_id}"
    if cid <= -1000000000000:
        return f"https://t.me/c/{-cid - 1000000000000}/{msg_id}"
    return None


# ---------------------------------------------------------------- переписка в текст
class Redactor:
    def __init__(self, rules: list[dict]):
        self.rules = [(re.compile(r["pattern"]), r.get("replace", "[скрыто]")) for r in rules or [] if r.get("pattern")]

    def __call__(self, text: str) -> str:
        for rx, rep in self.rules:
            text = rx.sub(rep, text)
        return text


_TAG_RE = re.compile(r"^\[([^\]]*)\]\s*")


def with_media(text: str, x: dict | None, full: bool = False) -> str:
    """Текст сообщения, где пометка «[фото]»/«[файл: …]» заменена описанием вложения из базы."""
    if not x:
        return text
    fwd = ""
    m = _TAG_RE.match(text)
    if m:
        fwd = "[переслано] " if "переслано" in m.group(1) else ""
        text = text[m.end():]
    return f"{fwd}{annotate(x, full=full)} {text}".strip()


def transcript_lines(msgs: list[Message], tz: tzinfo, redact: Callable[[str], str] | None = None,
                     media: dict[int, dict] | None = None, mine: set[int] | None = None) -> list[str]:
    """Переписка для модели. Метки: «↩#N» — ответ на N, «↩#N(Я)» — ответ на сообщение пользователя,
    «@me» — пользователя отметили через @ник (Telegram сам ставит этот признак)."""
    lines, day = [], None
    mine = set(mine or ()) | {m.msg_id for m in msgs if m.is_out}
    for m in msgs:
        dt = parse_iso(m.date).astimezone(tz)
        if dt.date() != day:
            day = dt.date()
            lines.append(f"— {dt:%d.%m.%Y} —")
        text = m.text.replace("\r", " ").replace("\n", " ⏎ ")
        if len(text) > MAX_MSG_CHARS:
            text = text[:MAX_MSG_CHARS] + " …"
        if media and m.msg_id in media:
            text = with_media(text, media[m.msg_id])
        if redact:
            text = redact(text)
        who = "Я" if m.is_out else (m.sender or "?")
        extra = ""
        to_me = bool(m.reply_to) and m.reply_to in mine
        if m.reply_to:
            extra += f" ↩#{m.reply_to}" + ("(Я)" if to_me else "")
        if m.mentioned and not m.is_out and not to_me:  # Telegram ставит mentioned и на ответ пользователю
            extra += " @me"
        lines.append(f"#{m.msg_id} [{dt:%H:%M}] {who}:{extra} {text}")
    return lines


def chat_header(key: str, chat: Chat, part: str = "", notes: list[str] | None = None) -> str:
    h = (f"=== ЧАТ {key} | «{chat.title}» | {KIND_RU.get(chat.kind, chat.kind)} | приоритет: {chat.priority}"
         f" | режим: {chat.mode}")
    if chat.unread:
        h += f" | непрочитано: {chat.unread}"
    if part:
        h += f" | {part}"
    h += " ==="
    rule = prompts.MODE_RULES.get(chat.mode, "")
    note = (chat.note or "").strip()
    extra = " ".join(x for x in (rule, prompts.chat_note_line(note) if note else "") if x)
    out = h + (f"\n({extra})" if extra else "")
    for n in notes or []:
        out += f"\n({n})"
    return out


def chat_notes(cfg, store: Store, chat: Chat, name_forms: set[str], me_id: int | None) -> list[str]:
    notes = []
    if chat.kind != "private":
        ns = namesakes_mod.chat_namesakes(store, chat.chat_id, name_forms, me_id)
        if ns:
            notes.append("Тёзки пользователя в этом чате: " + ", ".join(ns) +
                         " — обращение только по имени может быть к ним, а не к пользователю")
    try:
        from . import people
        pn = people.chat_notes(cfg, store, chat.chat_id)
    except Exception:  # noqa: BLE001 — заметки о людях не должны ломать сводку
        pn = []
    if pn:
        notes.append("Участники чата — заметки пользователя о них (зона ответственности; с чем к ним можно "
                     "обратиться; с чем они приходят к пользователю): " + " | ".join(pn))
    return notes


# ---------------------------------------------------------------- пакеты для map
@dataclass
class ChatWork:
    key: str
    chat: Chat
    msgs: list[Message]
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # тёзки пользователя, описания участников
    sig: str = ""                                    # отпечаток переписки — для точного повтора (history.py)
    history: str = ""                                # "" | chain — опора на прошлые сводки | context — только для отметок
    covered: int = 0                                 # сколько сообщений уже разобрано в прошлых сводках


@dataclass
class Batch:
    blocks: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    chars: int = 0


def make_batches(works: list[ChatWork], budget: int, max_chats: int = 8) -> list[Batch]:
    batches: list[Batch] = []
    cur = Batch()
    for w in works:
        body = "\n".join(w.lines)
        if len(body) + 300 > budget:  # большой чат — частями, каждая часть отдельным запросом
            parts, buf, size = [], [], 0
            for line in w.lines:
                if size + len(line) + 1 > budget - 400 and buf:
                    parts.append(buf)
                    buf, size = [], 0
                buf.append(line)
                size += len(line) + 1
            if buf:
                parts.append(buf)
            for i, p in enumerate(parts, 1):
                b = Batch()
                block = chat_header(w.key, w.chat, f"часть {i}/{len(parts)}", w.notes) + "\n" + "\n".join(p)
                b.blocks.append(block)
                b.keys.append(w.key)
                b.chars = len(block)
                batches.append(b)
            continue
        block = chat_header(w.key, w.chat, notes=w.notes) + "\n" + body
        if cur.keys and (cur.chars + len(block) > budget or len(cur.keys) >= max_chats):
            batches.append(cur)
            cur = Batch()
        cur.blocks.append(block)
        cur.keys.append(w.key)
        cur.chars += len(block) + 1
    if cur.keys:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------- нормализация ответов модели
def _as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _ints(xs) -> list[int]:
    out = []
    for x in _as_list(xs):
        m = re.search(r"\d+", str(x))
        if m:
            out.append(int(m.group(0)))
    return out


def norm_item(it: dict) -> dict:
    t = str(it.get("type", "fyi")).strip().lower().replace("-", "_")
    p = str(it.get("priority", "medium")).strip().lower()
    draft = it.get("draft")
    if isinstance(draft, str) and draft.strip().lower() in ("", "null", "none", "нет"):
        draft = None
    comp = it.get("competence")
    if isinstance(comp, str) and comp.strip().lower() in ("", "null", "none", "нет"):
        comp = None
    to_me = str(it.get("to_me") or "").strip().lower()
    owner = str(it.get("owner") or "").strip().lower()
    who = it.get("owner_name")
    if not isinstance(who, str) or who.strip().lower() in ("", "null", "none", "нет"):
        who = None
    ctl = it.get("control")
    ctl = ctl is True or str(ctl).strip().lower() in ("true", "1", "да", "yes")
    t = t if t in {"reply", "action", "follow_up", "decision", "incident", "question", "fyi"} else "fyi"
    if owner == "other" and t == "reply":  # чужую задачу «ответить» не ставим — проследить
        t, ctl = "follow_up", True
    return {
        "type": t,
        "priority": p if p in ITEM_PRIO else "medium",
        "title": str(it.get("title") or "").strip(),
        "details": str(it.get("details") or "").strip(),
        "competence": comp,
        "msgs": _ints(it.get("msgs") or it.get("message_ids")),
        "draft": draft,
        "to_me": to_me if to_me in TO_ME else "",
        "owner": owner if owner in OWNERS else "",
        "owner_name": who.strip() if who else None,
        "control": bool(ctl),
        "change": ch if (ch := str(it.get("change") or "").strip().lower()) in CHANGES else "",
    }


def merge_chat(parts: list[dict]) -> dict:
    if len(parts) == 1:
        return parts[0]
    return {
        "summary": " ".join(p.get("summary", "") for p in parts if p.get("summary")),
        "status": parts[-1].get("status", "info"),
        "importance": max(int(p.get("importance") or 1) for p in parts),
        "items": [i for p in parts for i in p.get("items", [])],
    }


# ---------------------------------------------------------------- основной класс
class DigestBuilder:
    def __init__(self, cfg, store: Store, llm: LLMClient, progress: Callable[[str], None] = print,
                 describe: bool | None = None, vision_llm: LLMClient | None = None,
                 cancel: Callable[[], bool] | None = None, extra: str = "", history: bool | None = None):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.progress = progress
        self.cancel = cancel or (lambda: False)
        self.extra = str(extra or "").strip()[:4000]   # указания пользователя для этой сводки
        # опираться на прошлые сводки (history.py): по умолчанию — да; digest.history = false в настройках — нет
        self.history = (cfg.digest.get("history", True) is not False) if history is None else bool(history)
        self.run_id = os.urandom(6).hex()
        self.hist_stats = {"reused": 0, "chained": 0, "skipped_msgs": 0, "context": 0}
        self.prev: dict | None = None
        media_cfg = cfg.get("media") or {}
        self.describe = bool(media_cfg.get("describe_in_digest", True)) if describe is None else describe
        self.vision_llm = vision_llm
        self.image_stats: dict = {}
        self.glossary_stats: dict = {}
        self.tz = get_tz(cfg.digest.get("timezone"))
        self.errors: list[str] = []
        self.redact = Redactor((cfg.get("privacy") or {}).get("redact") or []) if llm.external else None
        from . import privacy
        p = privacy.attach(llm, cfg, store)
        if vision_llm is not None and vision_llm is not llm:
            privacy.attach(vision_llm, cfg, store, registry=p.reg if p else None)
        self.privacy = p
        me = store.kv_get("me")
        self.me_name = json.loads(me).get("name", "") if me else ""
        self.me_id = json.loads(me).get("id") if me else None
        self.system = prompts.system_prompt(cfg.profile, self.me_name)
        from . import namesakes
        self.name_forms = namesakes.my_name_forms(cfg.profile, self.me_name)

    def chat_notes(self, chat: Chat) -> list[str]:
        """Что модели знать о людях чата: тёзки пользователя и заметки пользователя об участниках."""
        return chat_notes(self.cfg, self.store, chat, self.name_forms, self.me_id)

    def budget(self) -> int:
        cc = int(self.cfg.digest.get("chunk_chars") or 0)
        if cc > 0:
            return cc
        out_tokens = int(self.cfg.digest.get("max_output_tokens") or 4096)
        est = int((self.llm.context_tokens - out_tokens - 2500) * 2.4)
        return max(6000, min(est, 30000))

    def _ask_json(self, user: str, max_tokens: int):
        raw = self.llm.complete(self.system, user, max_tokens=max_tokens)
        try:
            return extract_json(raw)
        except ValueError:
            self.progress("  ответ модели — не JSON, прошу исправить…")
            fixed = self.llm.complete("Ты исправляешь JSON.", prompts.repair_prompt(raw), max_tokens=max_tokens)
            return extract_json(fixed)

    # -------------------------------------------------------- темы форумов
    def _topics(self, chat: Chat, msgs: list[Message]):
        """Форум: исключённые темы — убрать; переписку — по темам, у каждой — заголовок с её важностью и описанием.
        Возвращает (сообщения, функция строк или None для обычного чата)."""
        if not any(m.topic for m in msgs):
            return msgs, None
        rule = self.cfg.chat_rule(chat.title, chat.username, chat.chat_id, chat.kind or "group")
        sets = rule.get("topics") or {}
        titles = {t["topic_id"]: t["title"] for t in self.store.topics(chat.chat_id)}
        groups: dict[int, list[Message]] = {}
        for m in msgs:
            tid = m.topic or 1
            if (sets.get(str(tid)) or {}).get("exclude"):
                continue
            groups.setdefault(tid, []).append(m)
        if not groups:
            return [], None
        order = sorted(groups, key=lambda t: ({"high": 0, "normal": 1, "low": 2}.get(
            (sets.get(str(t)) or {}).get("priority") or "normal", 1), -len(groups[t])))
        kept = [m for t in order for m in groups[t]]

        def lines(media, mine):
            out = []
            for t in order:
                s = sets.get(str(t)) or {}
                head = f"--- тема «{s.get('title') or titles.get(t) or ('Общая' if t == 1 else t)}»"
                bits = []
                if s.get("priority") in ("high", "low"):
                    bits.append("важность: " + {"high": "высокая", "low": "низкая"}[s["priority"]])
                if s.get("description"):
                    bits.append("описание пользователя: " + s["description"])
                if s.get("ai_description"):
                    bits.append("анализ ИИ: " + s["ai_description"])
                out.append(head + (" (" + "; ".join(bits) + ")" if bits else "") + " ---")
                out += transcript_lines(groups[t], self.tz, self.redact, media, mine)
            return out
        return kept, lines

    # -------------------------------------------------------- map
    def _map(self, works: list[ChatWork], period: str, ready: dict[str, dict] | None = None,
             span: tuple[str, str] = ("", "")) -> dict[str, dict]:
        """ready — чаты, уже разобранные раньше (точный повтор, без новых сообщений): модель их не читает.
        Каждый разобранный чат сразу записывается в историю (history.py) — остановка сводки не теряет сделанное."""
        ready = ready or {}
        by_key = {w.key: w for w in works}
        out_tokens = int(self.cfg.digest.get("max_output_tokens") or 4096)
        todo = [w for w in works if w.key not in ready]
        batches = make_batches(todo, self.budget())
        results: dict[str, list[dict]] = {}
        left: dict[str, int] = {}
        for b in batches:
            for k in b.keys:
                left[k] = left.get(k, 0) + 1
        failed: set[str] = set()

        def done(keys: list[str], ok: bool) -> None:
            for k in keys:
                left[k] -= 1
                if not ok:
                    failed.add(k)
                if left[k] == 0 and k not in failed and k in results:
                    self._remember(by_key[k], merge_chat(results[k]), span)
        for i, b in enumerate(batches, 1):
            self.check()
            names = ", ".join(by_key[k].chat.title for k in b.keys)
            self.progress(f"Сводка {i}/{len(batches)}: {names[:90]} ({b.chars // 1000} тыс. симв.)")
            try:
                data = self._ask_json(self._with_glossary(prompts.map_prompt(period, b.blocks, b.keys, self.extra),
                                                          "\n".join(b.blocks)), out_tokens)
                self._glossary_found(data)
            except (LLMError, ValueError) as e:
                self.check()   # оборванный остановкой запрос — не ошибка пакета, а конец сводки
                logs.get("digest").warning("пакет %d/%d (%d тыс. симв.): %s", i, len(batches), b.chars // 1000,
                                           logs.err_info(e))
                self.errors.append(f"пакет {i} ({names[:60]}): {e}")
                self.progress(f"  ошибка: {e}")
                done(b.keys, False)
                continue
            items = data.get("chats") if isinstance(data, dict) else data
            for c in _as_list(items):
                if not isinstance(c, dict):
                    continue
                key = str(c.get("chat") or c.get("key") or "").strip()
                if key not in b.keys and len(b.keys) == 1:
                    key = b.keys[0]
                if key not in by_key:
                    continue
                results.setdefault(key, []).append({
                    "summary": str(c.get("summary") or "").strip(),
                    "status": str(c.get("status") or "info").strip().lower(),
                    "importance": max(1, min(5, int(_ints(c.get("importance"))[0] if _ints(c.get("importance")) else 3))),
                    "items": [norm_item(x) for x in _as_list(c.get("items")) if isinstance(x, dict)],
                })
            done(b.keys, True)
        # модель пропустила чат в пакете — переспросить по одному (один раз)
        missing = [w for w in todo if w.key not in results]
        for w in missing:
            if len("\n".join(w.lines)) > self.budget():
                continue
            self.check()
            self.progress(f"Досводка: {w.chat.title}")
            try:
                block = chat_header(w.key, w.chat, notes=w.notes) + "\n" + "\n".join(w.lines)
                data = self._ask_json(prompts.map_prompt(period, [block], [w.key], self.extra), out_tokens)
                items = data.get("chats") if isinstance(data, dict) else data
                for c in _as_list(items)[:1]:
                    results[w.key] = [{
                        "summary": str(c.get("summary") or "").strip(),
                        "status": str(c.get("status") or "info").strip().lower(),
                        "importance": max(1, min(5, (_ints(c.get("importance")) or [3])[0])),
                        "items": [norm_item(x) for x in _as_list(c.get("items")) if isinstance(x, dict)],
                    }]
                    self._remember(w, results[w.key][0], span)
            except (LLMError, ValueError) as e:
                self.check()
                self.errors.append(f"{w.chat.title}: {e}")
        out = {k: merge_chat(v) for k, v in results.items()}
        out.update(ready)
        return out

    def _remember(self, w: ChatWork, res: dict, span: tuple[str, str]) -> None:
        """Разобранный чат — в историю сводок (сразу, по одному)."""
        if not w.sig:
            return
        try:
            from . import history
            history.save(self.store, self.run_id, w.chat.chat_id, span[0], span[1], [m.msg_id for m in w.msgs],
                         w.sig, self.extra, res)
        except Exception as e:  # noqa: BLE001 — история не должна ломать сводку
            logs.get("digest").info("история сводок: не записано: %s", logs.err_info(e))

    def _prepare_history(self, works: list[ChatWork], s: str, u: str, medias: dict) -> dict[str, dict]:
        """Опора на прошлые сводки (history.py): точный повтор — готовый результат; сообщения, уже разобранные в
        прошлых сводках, заменяются их кратким содержанием; иначе — прошлая сводка как контекст для отметок."""
        from . import history
        ready: dict[str, dict] = {}
        try:
            history.ensure(self.store)
        except Exception as e:  # noqa: BLE001
            logs.get("digest").info("история сводок недоступна: %s", logs.err_info(e))
            return ready
        for w in works:
            w.sig = history.chat_sig(w.lines, w.notes, self.extra)
            if not self.history:
                continue
            got = history.find_exact(self.store, w.chat.chat_id, w.sig)
            if got is not None:
                ready[w.key] = {**got, "items": [norm_item(x) for x in got.get("items") or [] if isinstance(x, dict)],
                                "reused": "same"}
                self.hist_stats["reused"] += 1
                continue
            ids = [m.msg_id for m in w.msgs]
            ch = history.chain(self.store, w.chat.chat_id, ids, self.extra)
            if ch:
                last = ch[-1]["last_msg"]
                new = [m for m in w.msgs if m.msg_id > last]
                if not new:   # в чате ничего нового с прошлой сводки — её результат и есть ответ
                    res = ch[-1]["result"] or {}
                    ready[w.key] = {**res, "items": [norm_item(x) for x in res.get("items") or [] if isinstance(x, dict)],
                                    "reused": "quiet"}
                    self.hist_stats["reused"] += 1
                    continue
                kept, lines_fn = self._topics(w.chat, new)
                mine = namesakes_mod.mine_targets(self.store, w.chat.chat_id, kept, "telegram")
                body = lines_fn(medias.get(w.key) or {}, mine) if lines_fn else                     transcript_lines(kept, self.tz, self.redact, medias.get(w.key) or {}, mine)
                w.lines = [history.context_block(ch, self.tz, last)] + body
                w.history, w.covered = "chain", len(w.msgs) - len(new)
                self.hist_stats["chained"] += 1
                self.hist_stats["skipped_msgs"] += w.covered
                continue
            prev = history.latest_before(self.store, w.chat.chat_id, s, self.extra)
            if prev:
                w.lines = [history.context_block([prev], self.tz, None)] + w.lines
                w.history = "context"
                self.hist_stats["context"] += 1
        return ready

    def check(self) -> None:
        """Сводку остановили — дальше не идём (запросы к модели этой задачи уже оборваны)."""
        if self.cancel():
            raise DigestCancelled("сводка остановлена")

    # -------------------------------------------------------- глоссарий
    def _with_glossary(self, user: str, text: str) -> str:
        from .glossary import block_for
        return prompts.with_glossary(block_for(self.store, text, scope="digest"), user)

    def _glossary_found(self, data) -> None:
        """Новые и неясные термины, которые модель заметила по ходу сводки, — в глоссарий (черновики и вопросы)."""
        if not isinstance(data, dict) or not isinstance(data.get("glossary"), dict):
            return
        try:
            from .glossary import Glossary, apply_found
            st = apply_found(Glossary(self.store), data["glossary"], None, "замечено при сводке")
            self.glossary_stats = {k: self.glossary_stats.get(k, 0) + v for k, v in st.items()}
        except Exception as e:  # noqa: BLE001
            logs.get("digest").info("глоссарий не пополнен: %s", logs.err_info(e))

    def _collect_terms(self, works: list[ChatWork]) -> None:
        """Все термины переписки периода — в очередь глоссария (без модели; обработка — отдельной задачей)."""
        from .glossary_auto import note_many
        items = [(m.text, "telegram", f"{w.chat.chat_id}:{m.msg_id}") for w in works for m in w.msgs if m.text]
        n = note_many(self.cfg, self.store, items)
        self.glossary_stats["queued"] = self.glossary_stats.get("queued", 0) + n
        if n:
            self.progress(f"Глоссарий: новых кандидатов в термины — {n} (обработаются отдельной задачей)")

    # -------------------------------------------------------- сквозные упоминания
    def _refs(self, since: str, until: str, key_of: dict[int, str]) -> list[dict]:
        groups: dict[str, dict] = {}
        for r in self.store.entities_between(since, until):
            if r["chat_id"] not in key_of:
                continue
            g = groups.setdefault(r["value"], {"value": r["value"], "type": r["etype"], "mentions": []})
            chat = self.store.get_chat(r["chat_id"], r["source"])
            g["mentions"].append({"chat": r["chat_title"] or "", "chat_key": key_of.get(r["chat_id"]),
                                  "msg_id": r["msg_id"], "date": r["date"],
                                  "url": msg_link(chat, r["msg_id"]) if chat else None})
        out = []
        for g in groups.values():
            g["chats"] = len({m["chat_key"] for m in g["mentions"]})
            g["count"] = len(g["mentions"])
            out.append(g)
        out.sort(key=lambda g: (-g["chats"], -g["count"], g["value"]))
        return out

    # -------------------------------------------------------- reduce
    def _reduce(self, chats: list[dict], refs: list[dict], period: str) -> dict | None:
        compact = []
        for c in chats:
            if not c.get("summary") and not c.get("items"):
                continue
            compact.append({"chat": c["key"], "title": c["title"], "mode": c["mode"], "importance": c["importance"],
                            "summary": c["summary"],
                            "items": [{k: v for k, v in i.items() if k in ("type", "priority", "title", "details",
                                                                          "competence", "msgs", "draft", "to_me",
                                                                          "owner", "owner_name", "control",
                                                                          "change") and v}
                                      for i in c["items"]]})
        if not compact:
            return None
        refs_text = "\n".join(
            f"{g['value']}: " + ", ".join(sorted({m['chat'] for m in g['mentions']}))
            for g in refs if g["chats"] > 1 or g["type"] in ("incident", "change", "problem"))[:4000]
        top_n = int(self.cfg.digest.get("top_actions") or 10)
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) > self.budget() * 1.5:  # слишком много — отдаём модели только важное
            compact = sorted(compact, key=lambda c: -c["importance"])
            while len(json.dumps(compact, ensure_ascii=False)) > self.budget() * 1.5 and len(compact) > 3:
                compact.pop()
            text = json.dumps(compact, ensure_ascii=False)
        self.check()
        self.progress("Общая картина (reduce)…")
        try:
            prev = ""
            if self.prev:
                from . import history
                prev = history.prev_brief(self.prev, self.tz)
            data = self._ask_json(self._with_glossary(prompts.reduce_prompt(period, text, refs_text, top_n, self.extra,
                                                                            prev), text),
                                  int(self.cfg.digest.get("max_output_tokens") or 4096))
        except (LLMError, ValueError) as e:
            self.check()
            logs.get("digest").warning("общая сводка: %s", logs.err_info(e))
            self.errors.append(f"общая сводка: {e}")
            return None
        if not isinstance(data, dict):
            return None
        return {
            "headline": str(data.get("headline") or "").strip(),
            "top": [{"chat": str(t.get("chat") or ""), "title": str(t.get("title") or ""),
                     "why": str(t.get("why") or ""), "priority": str(t.get("priority") or "medium").lower(),
                     "msgs": _ints(t.get("msgs")), "draft": norm_item({"draft": t.get("draft")})["draft"],
                     "to_me": norm_item(t)["to_me"], "owner": norm_item(t)["owner"]}
                    for t in _as_list(data.get("top")) if isinstance(t, dict)
                    and str(t.get("owner") or "").lower() != "other"][:top_n],
            "control": [{"chat": str(t.get("chat") or ""), "title": str(t.get("title") or ""),
                         "who": norm_item({"owner_name": t.get("who")})["owner_name"] or "",
                         "why": str(t.get("why") or ""), "msgs": _ints(t.get("msgs")),
                         "draft": norm_item({"draft": t.get("draft")})["draft"]}
                        for t in _as_list(data.get("control")) if isinstance(t, dict)][:top_n],
            "incidents": [{"title": str(x.get("title") or ""), "status": str(x.get("status") or ""),
                           "chats": [str(c) for c in _as_list(x.get("chats"))], "details": str(x.get("details") or ""),
                           "refs": [str(r) for r in _as_list(x.get("refs"))]}
                          for x in _as_list(data.get("incidents")) if isinstance(x, dict)],
            "competences": [{"name": str(x.get("name") or ""), "points": [str(p) for p in _as_list(x.get("points"))]}
                            for x in _as_list(data.get("competences")) if isinstance(x, dict)],
            "clarify": [str(x) for x in _as_list(data.get("clarify")) if x],
            "fyi": [str(x) for x in _as_list(data.get("fyi")) if x],
            "changes": [{"kind": k if (k := str(x.get("kind") or "").lower()) in ("new", "updated", "resolved") else "updated",
                         "title": str(x.get("title") or ""), "details": str(x.get("details") or ""),
                         "chat": str(x.get("chat") or "")}
                        for x in _as_list(data.get("changes")) if isinstance(x, dict) and x.get("title")][:15]
            if self.prev else [],
        }

    @staticmethod
    def fallback_overview(chats: list[dict], top_n: int) -> dict:
        items, ctl = [], []
        rank = {"tag": 0, "reply": 0, "name": 1, "role": 2}
        for c in chats:
            for it in c["items"]:
                if it.get("owner") == "other" or (it.get("control") and it["type"] == "follow_up"):
                    if it.get("control") or it["type"] == "follow_up":
                        ctl.append((ITEM_PRIO.get(it["priority"], 1), -c["importance"], c, it))
                elif it["type"] in ACTION_TYPES:
                    items.append((rank.get(it.get("to_me") or "", 3), ITEM_PRIO.get(it["priority"], 1),
                                  -c["importance"], c, it))
        items.sort(key=lambda x: x[:3])
        ctl.sort(key=lambda x: x[:2])
        top = [{"chat": c["key"], "title": it["title"], "why": it["details"], "priority": it["priority"],
                "msgs": it["msgs"], "draft": it["draft"], "to_me": it.get("to_me", ""), "owner": it.get("owner", "")}
               for *_, c, it in items[:top_n]]
        control = [{"chat": c["key"], "title": it["title"], "who": it.get("owner_name") or "", "why": it["details"],
                    "msgs": it["msgs"], "draft": it["draft"]} for *_, c, it in ctl[:top_n]]
        incidents = [{"title": it["title"], "status": "", "chats": [c["key"]], "details": it["details"], "refs": []}
                     for c in chats for it in c["items"] if it["type"] == "incident"]
        heads = [c["summary"] for c in sorted(chats, key=lambda c: -c["importance"]) if c.get("summary")][:2]
        return {"headline": " ".join(heads), "top": top, "control": control, "incidents": incidents,
                "competences": [], "clarify": [], "fyi": [], "fallback": True}

    # -------------------------------------------------------- всё вместе
    # -------------------------------------------------------- изображения периода
    def _describe_period(self, chat_ids: list[int], s: str, u: str) -> None:
        """Все новые изображения периода — описать (обязательно). Уже описанные берутся из кеша."""
        rows = self.store.media_list(chat_ids=chat_ids, since=s, until=u, kinds=IMAGE_KINDS)
        if not rows:
            return
        not_loaded = sum(1 for r in rows if r["status"] != "ok")
        todo = [r for r in rows if r["status"] == "ok" and not r["described"] and r.get("desc_state") != "unsupported"]
        self.image_stats = {"images": len(rows), "not_downloaded": not_loaded, "to_describe": len(todo)}
        if not_loaded:
            self.errors.append(f"изображения: {not_loaded} из {len(rows)} не скачаны (нет связи с Telegram или файл "
                               "удалён) — в сводке они без описания")
        if not todo:
            return
        from .vision import Describer, VisionUnavailable
        d = Describer(self.cfg, self.store, self.vision_llm, progress=self.progress)
        try:
            d.describe(todo, label=" за период")
        except VisionUnavailable as e:
            self.errors.append(f"изображения не описаны: {e}")
            self.progress(f"  ✗ {e}")
        except (LLMError, KeyError) as e:
            self.errors.append(f"изображения не описаны: {e}")
        self.image_stats.update(d.stats)
        self.progress(d.summary_line())
        if d.stats["failed"]:
            self.errors.append(f"изображения: {d.stats['failed']} не удалось описать"
                               + (f" ({d.errors[0][:160]})" if d.errors else ""))

    def _avatar(self, chat_id: int) -> str | None:
        """Аватарка чата для отчёта (если скачана — мессенджером или при синхронизации)."""
        try:
            from .avatars import report_src
            return report_src(self.cfg, self.store, chat_id, self.cfg.path(self.cfg.digest.get("output_dir") or "reports"))
        except Exception:  # noqa: BLE001 — без аватарки отчёт не хуже
            return None

    def _report_images(self, chat: Chat, media: dict[int, dict], wanted: set[int], limit: int = 12) -> list[dict]:
        out_dir = self.cfg.path(self.cfg.digest.get("output_dir") or "reports")
        rows = [x for x in media.values() if x.get("kind") in IMAGE_KINDS and x["status"] == "ok" and x.get("path")]
        rows.sort(key=lambda x: (x["msg_id"] not in wanted, -(x.get("importance") or 0), x["msg_id"]))
        res = []
        for x in rows[:limit]:
            if x["msg_id"] not in wanted and (x.get("importance") or 3) < 2:
                continue
            abs_path = self.cfg.path(x["path"])
            try:
                rel = os.path.relpath(abs_path, out_dir).replace("\\", "/")
                src = "/".join(urllib.parse.quote(part) for part in rel.split("/"))
            except ValueError:  # другой диск Windows
                src = abs_path.as_uri()
            res.append({"msg_id": x["msg_id"], "media_id": x["id"], "src": src, "path": x["path"],
                        "summary": x.get("summary") or "", "kind": x.get("d_kind") or "",
                        "importance": x.get("importance"), "url": msg_link(chat, x["msg_id"])})
        return res

    def build(self, since: datetime, until: datetime, period: str = "custom", source: str = "telegram",
              only: set[int] | None = None, label: str = "") -> dict:
        """only — сводка только по этим чатам (выбраны в окне; исключённые тоже, раз их выбрали явно)."""
        s, u = iso(since), iso(until)
        max_per_chat = int(self.cfg.digest.get("max_messages_per_chat") or 600)
        try:
            from .chatrules import apply_chat_rules
            apply_chat_rules(self.cfg, self.store)  # настройки чатов из окна — даже без новой синхронизации
        except Exception as e:  # noqa: BLE001
            logs.get("digest").warning("правила чатов не применены: %s", logs.err_info(e))
        if only:
            active = [cn for cn in self.store.active_chats(s, u, source, include_ignored=True) if cn[0].chat_id in only]
            self.progress(f"Выбрано чатов: {len(only)}, из них с сообщениями за период: {len(active)}")
        else:
            active = self.store.active_chats(s, u, source)
        if self.extra:
            self.progress("Дополнительные указания для этой сводки: " + self.extra[:160].replace("\n", " ")
                          + ("…" if len(self.extra) > 160 else ""))
        active.sort(key=lambda cn: (PRIO_ORDER.get(cn[0].priority, 1), -cn[1]))
        if self.describe and active:
            self._describe_period([c.chat_id for c, _ in active], s, u)
            self.check()
        works: list[ChatWork] = []
        medias: dict[str, dict[int, dict]] = {}
        total = 0
        for i, (chat, _n) in enumerate(active, 1):
            if i % 20 == 0:
                self.check()
            msgs = self.store.messages(chat.chat_id, s, u, source)
            if len(msgs) > max_per_chat:
                msgs = msgs[-max_per_chat:]
            if not msgs:
                continue
            total += len(msgs)
            key = f"c{i}"
            msgs, lines_fn = self._topics(chat, msgs)
            if not msgs:
                continue
            medias[key] = self.store.media_of(chat.chat_id, [m.msg_id for m in msgs], source)
            mine = namesakes_mod.mine_targets(self.store, chat.chat_id, msgs, source)
            lines = lines_fn(medias[key], mine) if lines_fn else \
                transcript_lines(msgs, self.tz, self.redact, medias[key], mine)
            works.append(ChatWork(key, chat, msgs, lines, self.chat_notes(chat)))
        period_text = fmt_period(since, until, self.tz)
        self.check()
        self._collect_terms(works)
        try:
            getattr(self.llm, "model", None)  # для «auto» узнать имя модели заранее — для понятного лога
        except Exception:  # noqa: BLE001
            pass
        ready = self._prepare_history(works, s, u, medias) if works else {}
        hs = self.hist_stats
        if hs["reused"] or hs["chained"]:
            self.progress(f"Опора на прошлые сводки: без изменений — {hs['reused']} чат(ов) (взяты из прошлых сводок), "
                          f"ещё в {hs['chained']} модель прочтёт только новое — уже разобрано {hs['skipped_msgs']} "
                          f"сообщений")
        self.progress(f"Чатов с сообщениями: {len(works)}, сообщений: {total}. Модель: {self.llm.label}")
        mapped = self._map(works, period_text, ready, (s, u)) if works else {}
        chats = []
        for w in works:
            r = mapped.get(w.key, {"summary": "", "status": "info", "importance": 1, "items": [],
                                   "missing": True})
            for it in r["items"]:
                known = {m.msg_id for m in w.msgs}
                it["msgs"] = [m for m in it["msgs"] if m in known] or it["msgs"][:3]
                it["links"] = [{"id": m, "url": msg_link(w.chat, m)} for m in it["msgs"]]
            r["items"].sort(key=lambda it: (ITEM_PRIO.get(it["priority"], 1), it["type"] not in ACTION_TYPES))
            wanted = {m for it in r["items"] for m in it["msgs"]}
            imgs = self._report_images(w.chat, medias.get(w.key) or {}, wanted)
            for it in r["items"]:
                it["images"] = [x for x in imgs if x["msg_id"] in it["msgs"]]
            chats.append({"key": w.key, "chat_id": w.chat.chat_id, "title": w.chat.title, "kind": w.chat.kind,
                          "avatar": self._avatar(w.chat.chat_id),
                          "mode": w.chat.mode, "priority": w.chat.priority, "unread": w.chat.unread,
                          "count": len(w.msgs), "link": chat_link(w.chat, w.msgs[-1].msg_id), "images": imgs, **r})
        chats.sort(key=lambda c: (-c["importance"], PRIO_ORDER.get(c["priority"], 1), -c["count"]))
        key_of = {w.chat.chat_id: w.key for w in works}
        refs = self._refs(s, u, key_of)
        top_n = int(self.cfg.digest.get("top_actions") or 10)
        if self.history:
            try:
                from . import history
                self.prev = history.prev_digest(self.store, s, u)
            except Exception as e:  # noqa: BLE001
                logs.get("digest").info("прошлая сводка не найдена: %s", logs.err_info(e))
        overview = self._reduce(chats, refs, period_text) if chats else None
        if overview is not None and not overview["headline"] and not overview["top"]:
            self.errors.append("общая сводка: модель вернула пустой результат")
            overview = None
        if not overview:
            overview = self.fallback_overview(chats, top_n)
        logs.get("digest").info(
            "сводка: чатов %d, сообщений %d, запросов к модели %d, отправлено %d тыс. симв., ошибок %d, без общей сводки: %s",
            len(works), total, self.llm.calls, self.llm.chars_in // 1000, len(self.errors), bool(overview.get("fallback")))
        if self.privacy is not None:
            self.progress("Обезличивание: " + self.privacy.stats_line())
        by_key = {c["key"]: c for c in chats}
        overview.setdefault("control", [])
        for t in overview["top"] + overview["control"]:
            c = by_key.get(t["chat"])
            t["chat_title"] = c["title"] if c else t["chat"]
            t["chat_avatar"] = (c or {}).get("avatar")
            w = next((w for w in works if w.key == t["chat"]), None)
            t["links"] = [{"id": m, "url": msg_link(w.chat, m)} for m in t["msgs"]] if w else []
            t["images"] = [x for x in (c or {}).get("images", []) if x["msg_id"] in t["msgs"]]
        for x in overview["incidents"]:
            x["chat_titles"] = [by_key[k]["title"] if k in by_key else k for k in x["chats"]]
        return {
            "meta": {
                "period": period, "since": s, "until": u, "period_text": period_text,
                "period_label": label or None, "extra_prompt": self.extra or None,
                "chats_selected": len(only) if only else None,
                "history": {**self.hist_stats, "on": self.history,
                            "prev": ({"since": self.prev["since"], "until": self.prev["until"],
                                      "text": fmt_period(datetime.fromisoformat(self.prev["since"]),
                                                         datetime.fromisoformat(self.prev["until"]), self.tz)}
                                     if self.prev else None)},
                "timezone": str(self.tz), "generated": iso(datetime.now(timezone.utc)),
                "generated_local": datetime.now(self.tz).strftime("%d.%m.%Y %H:%M"),
                "provider": self.llm.label, "chats": len(works), "messages": total,
                "llm_calls": self.llm.calls, "chars_in": self.llm.chars_in, "chars_out": self.llm.chars_out,
                "errors": self.errors, "me": self.me_name or self.cfg.profile.get("name", ""),
                "images": self.image_stats, "glossary": self.glossary_stats,
                "privacy": ({"persons": len(self.privacy.persons), **self.privacy.stats}
                            if self.privacy is not None else None),
            },
            "overview": overview,
            "chats": chats,
            "refs": refs,
        }
