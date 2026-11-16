"""Сводка «От источника»: разбор начинается не с Telegram, а с другого источника.

Пример (запрос пользователя 09.11): взять в ITSM все незакрытые инциденты группы поддержки → по каждому открыть
карточку (статус, ожидание, журнал работ, задачи трекера из полей и комментариев) → открыть эти задачи в xTracker
(статус, релиз, кто сейчас должен действовать) → сгруппировать по общим проблемам и задачам, посчитать, оценить
актуальность и от кого что ждут → и только потом поискать номера в Telegram и почте: что там сказано и нет ли новее.

Можно начать и с трекера: задачи по условию → инциденты, которые на них ссылаются.

Шаги (каждый виден в журнале задачи):
  1. Список из стартового источника (плагин Maverick, порциями по page_size, до limit записей).
  2. Подробные карточки — для записей, у которых в списке нет журнала работ (порциями по 5, кеш cache_minutes).
  3. Связанное во втором источнике: ITSM → задачи трекера; трекер → инциденты ITSM.
  4. Telegram — только по номерам (база на этом компьютере, без модели), затем отдельный запрос к модели: что о номере
     сказано в чатах. Переписка, как и в обычной сводке, не встречается в одном запросе с данными других источников:
     дальше идут только выводы (решение пользователя 08.11).
  5. Почта, X5 Rooms, встречи — письма/сообщения, где упоминаются номера (плагины, порциями по 10 номеров).
  6. Модель: по каждой записи — актуальность, от кого ждут, общая проблема, что настораживает, что сделать
     (порциями по бюджету). Программа сама считает цифры и находит расхождения (задача закрыта, а инцидент ждёт;
     в чате новее, чем в ITSM; нет движения N дней; срок прошёл).
  7. Модель: общая картина — группы, что сделать, что можно закрыть, что уточнить, ответ на указания пользователя.

Всё, что уходит во внешнюю модель, проходит обезличивание (privacy.attach), как в обычной сводке.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import logs, plugins, prompts
from .digest import DigestCancelled, get_tz, msg_link, transcript_lines
from .llm.base import extract_json
from .storage import Store, iso, parse_iso

log = logs.get("source")
Progress = Callable[[str], None]

STARTS = ("tracker",)
SECOND = {"itsm": "tracker", "tracker": "itsm"}
LOOK_KINDS = ("telegram", "mail", "rooms", "meet")
STATES = {"open": "все незакрытые (новые, в работе, в ожидании)", "waiting": "только в ожидании (на паузе)",
          "work": "в работе, кроме ожидания", "new": "новые, ещё не взятые в работу"}
STATES_SHORT = {"open": "все незакрытые", "waiting": "в ожидании", "work": "в работе", "new": "новые"}
ITSM_TYPES = {"incident": "инциденты", "request": "запросы на обслуживание", "problem": "проблемы",
              "change": "изменения"}
EXPECTS_RU = {"support": "поддержка (мы)", "dev": "разработка", "user": "пользователь / инициатор",
              "vendor": "подрядчик / другая группа", "nobody": "никто — зависло", "unclear": "неясно"}
ACTUAL_RU = {"yes": "актуально", "no": "можно закрывать", "doubt": "уточнить"}
CLOSED_RE = re.compile(r"(?i)закрыт|выполнен|решен|решён|сделан|выпущен|в\s+проде|отклон|отмен|done|closed|"
                       r"resolved|fixed|released|rejected|won'?t\s*fix|cancel")

ITSM_FULL = ('{"number": "номер как в ITSM", "found": true, "type": "incident | request | problem | change", '
             '"title": "", "status": "статус как в ITSM", "priority": "", "service": "сервис/система", '
             '"group": "группа исполнителей", "assignee": "исполнитель", "opened": "дата открытия", '
             '"updated": "дата последнего изменения", "deadline": "срок решения/SLA, если есть", '
             '"waiting": "true, если запись в ожидании (на паузе), иначе false", '
             '"waiting_reason": "чего ждут — как написано в ITSM: информация от пользователя, релиз, подрядчик…", '
             '"waiting_for": "от кого ждут: поддержка | разработка | пользователь | подрядчик | другая группа | неясно", '
             '"waiting_until": "до какой даты, если указано", "waiting_task": "задача трекера из ожидания (ABC-123), если есть", '
             '"description": "суть в 1–3 предложениях", "tasks": ["все ключи задач трекера из любых полей и комментариев"], '
             '"related": ["связанные номера ITSM"], "comments": [{"date": "дата", "author": "кто", '
             '"text": "суть записи журнала работ или комментария"}], "resolution": "решение, если есть", "url": "ссылка"}')
TRACKER_FULL = ('{"key": "ключ задачи (ABC-123)", "found": true, "type": "задача | баг | история | эпик | подзадача", '
                '"title": "", "status": "статус как в трекере", "closed": "true, если задача закрыта, выполнена или '
                'выпущена", "resolution": "резолюция", "priority": "", "assignee": "исполнитель", "reporter": "автор", '
                '"project": "", "sprint": "", "release": "версия или релиз, в который входит (fix version)", '
                '"release_date": "дата релиза, если есть", "due": "срок", "updated": "дата последнего изменения", '
                '"waiting_for": "кто сейчас должен действовать: разработка | поддержка | аналитика | тестирование | '
                'заказчик | неясно", "waiting_what": "что именно от него ждут, если видно", '
                '"description": "суть в 1–3 предложениях", "comments": [{"date": "", "author": "", "text": "суть"}], '
                '"incidents": ["номера ITSM, связанные с задачей"], "related": ["связанные задачи"], "url": "ссылка"}')

SYSTEM = """Ты — ассистент инженера 2-й линии технической поддержки. Ты разбираешь его рабочую очередь: записи ITSM
(инциденты, запросы, проблемы) и задачи трекера разработки xTracker, а также что о них писали в чатах и почте.

О пользователе:
{profile}

Правила:
- Пиши по-русски, кратко и по делу. Опирайся только на данные ниже, ничего не выдумывай.
- Статус в ITSM и трекере главнее слов в чате; если они расходятся — отметь расхождение.
- Имена людей заменены кодами вида Ч12 — используй коды как есть, не склоняй и не расшифровывай.
- Отвечай строго в запрошенном формате JSON."""

MAP_TASK = """Сегодня {today}. Ниже — записи из очереди «{query}» ({n} шт.) со всем, что о них известно: карточка,
журнал работ, связанные {second}, выводы из чатов Telegram, письма.

По каждой записи реши:
- actual: актуальна ли сейчас — "yes" (есть что делать или чего ждать), "no" (по сути решена или потеряла смысл:
  задача выпущена, дубль, пользователь давно не отвечает — можно закрывать или предложить закрыть), "doubt" (неясно);
- actual_why: почему так, одной фразой;
- expects: от кого сейчас ожидается движение — "support" (от поддержки, то есть от пользователя и его группы),
  "dev" (разработка), "user" (пользователь или инициатор обращения), "vendor" (подрядчик, другая группа),
  "nobody" (никто — запись зависла), "unclear";
- expects_what: что именно ожидается, одной фразой;
- problem: короткое название общей проблемы — одинаковое у записей об одном и том же (например «двойные списания при
  автоплатеже»), чтобы их можно было сгруппировать;
- summary: суть и текущее положение по всем источникам, 1–2 предложения;
- flags: что настораживает — короткими фразами (задача закрыта, а запись ждёт релиза; в чате новее, чем в ITSM; нет
  движения давно; ждут от нас; срок прошёл; противоречия) — или пустой список;
- next: что конкретно сделать пользователю, или пусто;
- priority: "high" | "medium" | "low" — насколько срочно этим заняться.
{extra}
Строго JSON:
{{"items": [{{"ref": "номер записи", "actual": "yes", "actual_why": "", "expects": "dev", "expects_what": "",
"problem": "", "summary": "", "flags": [], "next": "", "priority": "medium"}}]}}

Записи:
{cards}"""

REDUCE_TASK = """Сегодня {today}. Ниже — разобранная очередь «{query}»: {n} записей с выводами по каждой и связанные
{second}. Цифры уже посчитаны программой (ниже) — не пересчитывай их, а опирайся на них.

Сделай общую картину:
- headline: 2–4 предложения — состояние очереди и главное;
- groups: записи с общей проблемой или общей задачей трекера — вместе (группа — от 2 записей; одиночные не нужны):
  название, номера записей, задачи трекера, что общего, общий статус, что делать с группой;
- actions: что сделать пользователю — по важности, конкретно, с номерами;
- close: что можно закрыть или предложить к закрытию, и почему;
- clarify: что уточнить и у кого (по ролям или кодам людей).
{extra}
Строго JSON:
{{"headline": "", "groups": [{{"title": "", "refs": ["…"], "tasks": ["ABC-1"], "why": "", "status": "", "next": ""}}],
"actions": [{{"title": "", "why": "", "refs": ["…"]}}], "close": [{{"ref": "", "why": ""}}], "clarify": [""]{answer}}}

Цифры:
{stats}

Записи:
{lines}"""

TG_TASK = """Ниже — выдержки из рабочих чатов Telegram, где упоминаются номера из очереди пользователя: {refs}.
По каждому номеру, который здесь действительно обсуждается, выпиши: что о нём сказано (суть, без пересказа всей
переписки), самое свежее по времени, кто чего ждёт (поддержка, разработка, пользователь…), есть ли сведения о статусе,
сроках, релизе. Номер, упомянутый вскользь или не о том, — пропусти. Ничего не выдумывай.
Строго JSON:
{{"refs": [{{"ref": "номер", "summary": "что известно из чатов", "latest": "самое свежее: дата и суть",
"date": "дата последнего упоминания ДД.ММ.ГГГГ", "waiting": "кто чего ждёт, или пусто", "msgs": [номера сообщений #N]}}]}}

Переписка:
{text}"""


class SourceError(RuntimeError):
    """Стартовый источник недоступен или не вернул ни одной записи — разбирать нечего."""


# ---------------------------------------------------------------- мелочи
def _s(v, n: int = 400) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "да", "yes", "1", "y")


def parse_date(s) -> datetime | None:
    """«05.11.2026», «05.11.2026 10:30», «2026-11-05», «2026-11-05T10:30:00+03:00» → datetime (UTC, если без пояса)."""
    t = _s(s, 40)
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:[ ,T]+(\d{1,2}):(\d{2}))?", t)
    if m:
        y = int(m.group(3))
        y += 2000 if y < 100 else 0
        try:
            return datetime(y, int(m.group(2)), int(m.group(1)), int(m.group(4) or 0), int(m.group(5) or 0),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _days(a: datetime | None, now: datetime) -> int | None:
    return None if a is None else max(0, (now - a).days)


def _items(data, key: str) -> list:
    if isinstance(data, dict):
        v = data.get(key)
        if v is None:
            v = data.get("items")
        return [x for x in v or [] if isinstance(x, dict)] if isinstance(v, list) else []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _comments(v) -> list[dict]:
    out = []
    for c in v or []:
        if isinstance(c, dict) and (c.get("text") or c.get("summary")):
            out.append({"date": _s(c.get("date"), 40), "author": _s(c.get("author") or c.get("from"), 80),
                        "text": _s(c.get("text") or c.get("summary"), 600)})
        elif isinstance(c, str) and c.strip():
            out.append({"date": "", "author": "", "text": _s(c, 600)})
    return out[:8]


def query_text(start: str, q: dict) -> str:
    """Коротко, что разбираем: «инциденты, запросы · все незакрытые · группа «L2-Биллинг» · приоритет 1–2»."""
    q = q or {}
    bits = []
    if start == "itsm":
        types = [ITSM_TYPES.get(t, t) for t in q.get("types") or ["incident"]]
        bits.append(", ".join(types))
    else:
        bits.append("задачи трекера")
    bits.append(STATES_SHORT.get(q.get("state") or "open", q.get("state") or ""))
    if q.get("group"):
        bits.append(("группа «" if start == "itsm" else "") + _s(q["group"], 120) + ("»" if start == "itsm" else ""))
    if q.get("from"):
        bits.append(f"с {q['from']}" + (f" до {q['to']}" if q.get("to") else ""))
    if q.get("text"):
        bits.append(_s(q["text"], 160))
    return " · ".join(b for b in bits if b)


def norm_query(start: str, q: dict | None) -> dict:
    q = dict(q or {})
    out = {"group": _s(q.get("group"), 200), "state": q.get("state") if q.get("state") in STATES else "open",
           "text": _s(q.get("text"), 1000)}
    if start == "itsm":
        types = [t for t in q.get("types") or [] if t in ITSM_TYPES] or ["incident"]
        out["types"] = types
    try:
        out["limit"] = max(1, min(int(q.get("limit") or 60), 300))
    except (TypeError, ValueError):
        out["limit"] = 60
    out["from"] = _s(q.get("from"), 40) or None
    out["to"] = _s(q.get("to"), 40) or None
    return out


def norm_look(look: dict | None) -> dict:
    look = dict(look or {})
    out = {k: bool(look.get(k, k in ("telegram", "mail"))) for k in LOOK_KINDS}
    out["second"] = look.get("second", True) is not False
    try:
        out["days"] = max(1, min(int(look.get("days") or 30), 3650))
    except (TypeError, ValueError):
        out["days"] = 30
    return out


# ---------------------------------------------------------------- запросы к плагинам
def _fields(rows: list[dict]) -> str:
    """Какие поля вернул плагин (без значений) — чтобы по журналу задачи было видно, чего не хватает."""
    keys: dict[str, None] = {}
    for r in rows[:5]:
        for k, v in r.items():
            if v not in (None, "", [], {}):
                keys.setdefault(str(k), None)
    return ", ".join(list(keys)[:24])


def _save_itsm(store: Store, it: dict, full: bool) -> dict | None:
    ref = plugins._norm_ref(it.get("number") or it.get("ref") or it.get("id") or it.get("номер") or "")
    if not ref:
        return None
    it = dict(it, number=ref)
    it["comments"] = _comments(it.get("comments"))
    if full:
        it["_full"] = True
    found = it.get("found", True) is not False
    plugins.save_item(store, "itsm", ref, it, title=_s(it.get("title"), 300), date=_s(it.get("updated") or it.get("opened"), 40),
                      status=_s(it.get("status"), 80), found=found)
    return plugins.cached(store, "itsm", ref)


def _save_task(store: Store, it: dict, full: bool) -> dict | None:
    ref = plugins._norm_ref(it.get("key") or it.get("ref") or it.get("id") or it.get("ключ") or "")
    if not ref:
        return None
    it = dict(it, key=ref)
    it["comments"] = _comments(it.get("comments"))
    if full:
        it["_full"] = True
    found = it.get("found", True) is not False
    plugins.save_item(store, "tracker", ref, it, title=_s(it.get("title"), 300), date=_s(it.get("updated"), 40),
                      status=_s(it.get("status"), 80), found=found)
    return plugins.cached(store, "tracker", ref)


def queue_list(cfg, store: Store, start: str, q: dict, *, progress: Progress = print, page: int = 20,
               cancel: Callable[[], bool] = lambda: False) -> dict:
    """Полный список записей стартового источника по условиям — порциями. {"items", "total", "pages", "truncated"}."""
    limit = q["limit"]
    state = STATES.get(q["state"], q["state"])
    if start == "itsm":
        what = "записей ITSM"
        cond = (f"тип — {', '.join(ITSM_TYPES[t] for t in q['types'])}; состояние — {state}"
                + (f"; группа исполнителей — «{q['group']}»" if q.get("group") else "")
                + (f"; созданные после {q['from']}" if q.get("from") else "")
                + (f"; созданные до {q['to']}" if q.get("to") else "")
                + (f"; дополнительно: {q['text']}" if q.get("text") else ""))
        schema, key = ITSM_FULL, "items"
    else:
        what = "задач xTracker"
        cond = (f"состояние — {state}" + (f"; проект, доска, фильтр или исполнитель — {q['group']}" if q.get("group") else "")
                + (f"; дополнительно: {q['text']}" if q.get("text") else ""))
        schema, key = TRACKER_FULL, "items"
    out: dict[str, dict] = {}
    total = None
    pages = 0
    a = 1
    while len(out) < limit:
        if cancel():
            raise DigestCancelled("сводка остановлена")
        b = min(a + page - 1, limit)
        progress(f"{plugins.SHORT[start]}: список — записи {a}–{b}…")
        task = (f"Это разбор очереди: нужен ПОЛНЫЙ список {what} по условиям, а не похожие записи и не примеры.\n"
                f"Условия: {cond}.\n"
                f"Верни записи с {a} по {b} из всех подходящих (сначала самые старые по дате открытия) и сколько всего "
                f"подходит. Если подходящих больше нет — пустой список.\n"
                f'Строго JSON: {{"total": число всех подходящих, "more": true если после этих есть ещё, "{key}": [{schema}]}}')
        # условия пользователя (имя группы, проект, свои слова) — как есть: это не имена людей, а то, по чему ищет ITSM
        allow = [x for x in (q.get("group"), q.get("text")) if x]
        data = plugins._ask(cfg, store, start, task, max_tokens=6000, progress=progress, images=False, allow=allow)
        pages += 1
        if pages == 1 and not isinstance(data, (dict, list)):
            raise ValueError(f"Maverick ответил не списком: {_s(data, 300)}")
        rows = _items(data, key)
        if pages == 1 and rows:
            progress(f"  поля в ответе: {_fields(rows)}")
        if isinstance(data, dict):
            try:
                total = int(data.get("total")) if data.get("total") not in (None, "") else total
            except (TypeError, ValueError):
                pass
        new = 0
        for it in rows:
            v = _save_itsm(store, it, False) if start == "itsm" else _save_task(store, it, False)
            if v and v["found"] and v["ref"] not in out:
                out[v["ref"]] = v
                new += 1
        more = isinstance(data, dict) and _truthy(data.get("more"))
        if not new or (len(rows) < (b - a + 1) and not more) or (total is not None and len(out) >= total):
            break
        a = b + 1
        if pages > limit // max(1, page) + 3:
            break
    items = list(out.values())[:limit]
    truncated = bool(total and total > len(items))
    progress(f"{plugins.SHORT[start]}: в очереди {len(items)}" + (f" из {total}" if total and total != len(items) else ""))
    return {"items": items, "total": total, "pages": pages, "truncated": truncated}


def _fresh(x: dict | None, minutes: float) -> bool:
    if not x or not x["data"].get("_full"):
        return False
    try:
        return datetime.now(timezone.utc) - parse_iso(x["fetched_at"]) < timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return False


def details(cfg, store: Store, kind: str, refs: list[str], *, progress: Progress = print, batch: int = 5,
            cache_minutes: float = 60, cancel: Callable[[], bool] = lambda: False) -> dict[str, dict]:
    """Подробные карточки (журнал работ, ожидание, связанные задачи/инциденты) — ITSM или трекер. {ref: item}."""
    out: dict[str, dict] = {}
    todo = []
    for r in dict.fromkeys(plugins._norm_ref(x) for x in refs if x):
        c = plugins.cached(store, kind, r)
        (out.__setitem__(r, c) if _fresh(c, cache_minutes) else todo.append(r))
    for i in range(0, len(todo), batch):
        if cancel():
            raise DigestCancelled("сводка остановлена")
        part = todo[i:i + batch]
        progress(f"{plugins.SHORT[kind]}: подробно {', '.join(part)}…")
        if kind == "itsm":
            task = (f"Подробные карточки ITSM для разбора очереди: {', '.join(part)}. По каждой — все поля ниже; "
                    "обязательно журнал работ и комментарии (последние 6 записей, сначала свежие), состояние ожидания "
                    "(чего и от кого ждут, до какой даты, задача из ожидания) и все задачи трекера, упомянутые в любых "
                    f"полях и комментариях.\nСтрого JSON: {{\"items\": [{ITSM_FULL}]}}\nЕсли запись не найдена — found=false.")
        else:
            task = (f"Подробно задачи xTracker для разбора очереди: {', '.join(part)}. По каждой — все поля ниже; "
                    "обязательно статус, закрыта ли, релиз и его дата, кто сейчас должен действовать и что от него ждут, "
                    "последние 4 комментария (сначала свежие) и номера инцидентов ITSM, связанных с задачей.\n"
                    f"Строго JSON: {{\"items\": [{TRACKER_FULL}]}}\nЕсли задача не найдена — found=false.")
        data = plugins._ask(cfg, store, kind, task, max_tokens=5000, progress=progress, images=False)
        got = set()
        if i == 0 and _items(data, "items"):
            progress(f"  поля в ответе: {_fields(_items(data, 'items'))}")
        for it in _items(data, "items"):
            v = _save_itsm(store, it, True) if kind == "itsm" else _save_task(store, it, True)
            if v:
                got.add(v["ref"])
                out[v["ref"]] = v
        for r in part:
            if r not in got:  # ответ без этой записи — «не найдена»; прежние данные (из списка) не затираем
                c = plugins.cached(store, kind, r)
                out[r] = c if c and c["found"] else (_save_itsm(store, {"number": r, "found": False}, True) if kind == "itsm"
                                                     else _save_task(store, {"key": r, "found": False}, True))
    return out


MENTION = {"mail": ("письма пользователя (входящие и отправленные)", "mail", plugins.MAIL_ITEM),
           "rooms": ("сообщения в чатах и каналах X5 Rooms", "messages", plugins.ROOMS_ITEM),
           "meet": ("встречи с расшифровкой в Scriber AI", "meetings", plugins.MEET_ITEM)}


def mentions(cfg, store: Store, kind: str, refs: list[str], days: int, *, progress: Progress = print, batch: int = 10,
             cancel: Callable[[], bool] = lambda: False) -> list[dict]:
    """Письма / сообщения Rooms / встречи, где упоминаются номера, — за последние days дней."""
    what, key, schema = MENTION[kind]
    out: dict[str, dict] = {}
    refs = list(dict.fromkeys(refs))
    for i in range(0, len(refs), batch):
        if cancel():
            raise DigestCancelled("сводка остановлена")
        part = refs[i:i + batch]
        progress(f"{plugins.SHORT[kind]}: ищу упоминания {', '.join(part[:4])}{'…' if len(part) > 4 else ''}")
        task = (f"Найди {what} за последние {days} дней, где упоминаются эти номера (в теме, тексте или вложениях): "
                f"{', '.join(part)}. Не больше 20, сначала свежие. В поле numbers укажи, какие из этих номеров есть в "
                f"письме или сообщении. Если ничего не нашлось — пустой список.\nСтрого JSON: {{\"{key}\": [{schema}]}}")
        data = plugins._ask(cfg, store, kind, task, max_tokens=4000, progress=progress)
        rows = _items(data, key)
        if kind == "mail":
            saved = plugins._save_mail(store, rows)
        elif kind == "rooms":
            saved = plugins._save_list(store, "rooms", rows, plugins._rooms_ref, lambda m: m.get("chat"))
        else:
            saved = plugins._save_list(store, "meet", rows, plugins._meet_ref, lambda m: m.get("title"))
        for x in saved:
            out[x["ref"]] = x
    return list(out.values())


# ---------------------------------------------------------------- Telegram: только по номерам
def tg_hits(cfg, store: Store, refs: list[str], since: str, per_ref: int = 6) -> dict[str, list[tuple[int, int, str]]]:
    """Где в базе Telegram упоминаются номера (без модели): {номер: [(chat_id, msg_id, date)]}, свежие последними."""
    from .messenger.ai import fts
    out: dict[str, list[tuple[int, int, str]]] = {}
    skip: dict[int, bool] = {}

    def ok(cid: int) -> bool:
        if cid not in skip:
            c = store.get_chat(cid)
            skip[cid] = bool(c and c.priority == "ignore")
        return not skip[cid]
    for ref in refs:
        seen: dict[tuple[int, int], str] = {}
        try:
            for r in store.entity_history(ref):
                if r["date"] >= since and ok(r["chat_id"]):
                    seen[(r["chat_id"], r["msg_id"])] = r["date"]
        except Exception as e:  # noqa: BLE001 — нет таблицы упоминаний (старая база)
            log.info("упоминания %s: %s", ref, logs.err_info(e))
        qs = [fts([ref])]
        digits = re.sub(r"^[A-ZА-Я]+0*", "", ref) if re.fullmatch(r"[A-ZА-Я]{2,5}\d{5,}", ref) else ""
        if len(digits) >= 5:
            qs.append(fts([digits]))  # «48190» вместо INC0048190 — так часто пишут в чатах
        for q in qs:
            if len(seen) >= per_ref * 3 or not q:
                continue
            try:
                for m, _title in store.search(q, since=since, limit=per_ref * 3):
                    if ok(m.chat_id):
                        seen.setdefault((m.chat_id, m.msg_id), m.date)
            except Exception as e:  # noqa: BLE001
                log.info("поиск %s: %s", ref, logs.err_info(e))
        if seen:
            out[ref] = sorted(((c, m, d) for (c, m), d in seen.items()), key=lambda x: x[2])[-per_ref:]
    return out


# ---------------------------------------------------------------- сборка
class SourceDigest:
    def __init__(self, cfg, store: Store, llm, progress: Progress = print, cancel: Callable[[], bool] | None = None,
                 extra: str = ""):
        from . import privacy
        self.cfg, self.store, self.llm, self.progress = cfg, store, llm, progress
        self.cancel = cancel or (lambda: False)
        self.extra = _s(extra, 4000) if extra else ""
        self.tz = get_tz(cfg.digest.get("timezone"))
        self.privacy = privacy.attach(llm, cfg, store)
        plugins.ensure(store)
        me = store.kv_get("me")
        self.me_name = json.loads(me).get("name", "") if me else ""
        self.system = SYSTEM.format(profile=prompts.profile_block(cfg.profile, self.me_name))
        sc = (cfg.digest.get("source") or {}) if isinstance(cfg.digest.get("source"), dict) else {}
        self.page = int(sc.get("page_size") or 20)
        self.cache_minutes = float(sc.get("cache_minutes") or 60)
        self.stale_days = int(sc.get("stale_days") or 7)
        self.old_days = int(sc.get("old_days") or 30)
        self.details_mode = str(sc.get("details") or "auto")
        self.errors: list[str] = []
        self.offline: dict[str, dict] = {}
        self.run_id = os.urandom(4).hex()

    # -------------------------------------------------------- служебное
    def check(self) -> None:
        if self.cancel():
            raise DigestCancelled("сводка остановлена")

    def budget(self) -> int:
        cc = int(self.cfg.digest.get("chunk_chars") or 0)
        if cc > 0:
            return cc
        return max(6000, min(int((self.llm.context_tokens - 4500) * 2.3), 30000))

    def _ask_json(self, user: str, max_tokens: int = 4000):
        from .glossary import block_for
        user = prompts.with_glossary(block_for(self.store, user[:20000], scope="digest"), user)
        raw = self.llm.complete(self.system, user, max_tokens=max_tokens)
        try:
            return extract_json(raw)
        except ValueError:
            self.progress("  ответ модели — не JSON, прошу исправить…")
            return extract_json(self.llm.complete("Ты исправляешь JSON.", prompts.repair_prompt(raw), max_tokens=max_tokens))

    def _guard(self, kind: str, what: str, fn, default=None):
        """Вызов плагина: «не подключён» → offline (в отчёт), ошибка → errors; разбор продолжается без этого источника."""
        from .llm import LLMError
        try:
            return fn()
        except plugins.PluginOffline as e:
            self.offline[kind] = {"state": e.status.get("state", "offline"), "detail": _s(e.status.get("detail"), 300),
                                  "links": e.status.get("links") or []}
            self.progress(f"  ⚠ {e}")
        except (LLMError, OSError, ValueError) as e:
            self.errors.append(f"{what}: {_s(e, 300)}")
            self.progress(f"  ⚠ {what}: {_s(e, 160)}")
            log.warning("%s: %s", what, logs.err_info(e))
        return default

    # -------------------------------------------------------- шаги
    def build(self, start: str, query: dict, look: dict, now: datetime | None = None) -> dict:
        from .llm import LLMError
        if start not in STARTS:
            raise SourceError(f"неизвестный стартовый источник: {start}")
        now = now or datetime.now(timezone.utc)
        q, lk = norm_query(start, query), norm_look(look)
        second = SECOND[start]
        qtext = query_text(start, q)
        since = now - timedelta(days=lk["days"])
        self.progress(f"От источника: {plugins.TITLES[start]} — {qtext}")
        if self.extra:
            self.progress("Указания для этой сводки: " + self.extra[:160] + ("…" if len(self.extra) > 160 else ""))

        # 1. список
        try:
            plugins.need(self.cfg, self.store, start, self.progress)
            lst = queue_list(self.cfg, self.store, start, q, progress=self.progress, page=self.page, cancel=self.cancel)
        except plugins.PluginOffline as e:
            raise SourceError(str(e)) from None
        except (LLMError, OSError, ValueError) as e:
            raise SourceError(f"{plugins.TITLES[start]}: список не получен — {_s(e, 300)}") from None
        if not lst["items"]:
            self.progress(f"{plugins.SHORT[start]}: по этим условиям записей нет.")
        self.check()

        # 2. подробные карточки
        refs = [x["ref"] for x in lst["items"]]
        need = refs if self.details_mode == "always" else [] if self.details_mode == "never" else \
            [x["ref"] for x in lst["items"] if not x["data"].get("comments")]
        full = dict((x["ref"], x) for x in lst["items"])
        if need:
            got = self._guard(start, f"{plugins.SHORT[start]}: подробные карточки",
                              lambda: details(self.cfg, self.store, start, need, progress=self.progress,
                                              cache_minutes=self.cache_minutes, cancel=self.cancel), {}) or {}
            for r, v in got.items():
                if v and v["found"] and r in full:
                    full[r] = v
        prim = [full[r] for r in refs]
        self.check()

        # 3. связанное во втором источнике
        links: dict[str, list[str]] = {}
        for x in prim:
            links[x["ref"]] = self._linked(start, x)
        sec_refs = list(dict.fromkeys(r for v in links.values() for r in v))
        sec: dict[str, dict] = {}
        if lk["second"] and sec_refs:
            if plugins.enabled(self.cfg, second):
                self.progress(f"{plugins.SHORT[second]}: связанных записей — {len(sec_refs)}")
                sec = self._guard(second, f"{plugins.SHORT[second]}: связанные записи",
                                  lambda: details(self.cfg, self.store, second, sec_refs[:120], progress=self.progress,
                                                  cache_minutes=self.cache_minutes, cancel=self.cancel), {}) or {}
            else:
                self.offline[second] = {"state": "disabled", "detail": "источник выключен в настройках", "links": []}
        self.check()

        # 4. Telegram — только по номерам
        all_refs = refs + [r for r in sec_refs if r in sec and sec[r]["found"]]
        tg: dict[str, dict] = {}
        tg_info = {"refs": 0, "msgs": 0, "chats": 0}
        if lk["telegram"] and all_refs:
            tg, tg_info = self._telegram(all_refs, iso(since))
        self.check()

        # 5. почта, Rooms, встречи
        ext: dict[str, list[dict]] = {}
        for kind in ("mail", "rooms", "meet"):
            if not lk.get(kind) or not all_refs:
                continue
            if not plugins.enabled(self.cfg, kind):
                self.offline[kind] = {"state": "disabled", "detail": "источник выключен в настройках", "links": []}
                continue
            got = self._guard(kind, plugins.SHORT[kind], lambda k=kind: mentions(
                self.cfg, self.store, k, all_refs, lk["days"], progress=self.progress, cancel=self.cancel), []) or []
            ext[kind] = got
            self.check()
        by_ref_ext = self._ext_by_ref(ext, all_refs)

        # 6. разбор по записям
        items = [self._item(start, x, links.get(x["ref"], []), sec, tg, by_ref_ext, now) for x in prim]
        secondary = self._secondary(second, sec, links, tg, by_ref_ext, now)
        self._code_flags(start, items, {s["ref"]: s for s in secondary}, now)
        self.progress(f"Модель ({self.llm.label}): разбираю записи — {len(items)}…")
        self._map(start, qtext, items, {s["ref"]: s for s in secondary}, now)
        self.check()

        # 7. общая картина
        stats = self._stats(start, items, secondary)
        ov = self._reduce(start, qtext, items, secondary, stats, now) if items else {}
        attention = self._attention(items)
        res = {
            "start": start, "start_title": plugins.TITLES[start], "second": second,
            "second_title": plugins.TITLES[second], "query": q, "query_text": qtext, "look": lk,
            "total": lst["total"], "truncated": lst["truncated"], "pages": lst["pages"],
            "items": items, "secondary": secondary, "stats": stats, "attention": attention,
            "headline": _s(ov.get("headline"), 1500), "groups": ov.get("groups") or [], "actions": ov.get("actions") or [],
            "close": ov.get("close") or [], "clarify": ov.get("clarify") or [], "answer": _s(ov.get("answer"), 4000),
            "tg": tg_info, "offline": self.offline, "errors": self.errors,
        }
        if not res["headline"]:
            res["headline"] = self._fallback_headline(start, items, stats)
        return res

    def _linked(self, start: str, x: dict) -> list[str]:
        """Номера второго источника, на которые ссылается запись (поля + текст журнала/комментариев)."""
        d = x["data"]
        blob = " ".join(str(v) for v in plugins._flat({k: v for k, v in d.items() if not str(k).startswith("_")}))
        if start == "itsm":
            keys = [plugins._norm_ref(k) for k in (d.get("tasks") or []) + [d.get("waiting_task") or ""] if k]
            keys += plugins.task_keys(self.cfg, blob, self.store)
            own = {x["ref"]}
            return [k for k in dict.fromkeys(keys) if k and k not in own and re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", k)]
        nums = [plugins._norm_ref(n) for n in (d.get("incidents") or []) if n]
        nums += plugins.ticket_numbers(self.cfg, blob, self.store)
        return [n for n in dict.fromkeys(nums) if n and n != x["ref"]]

    def _telegram(self, refs: list[str], since: str) -> tuple[dict[str, dict], dict]:
        from .llm import LLMError
        self.progress(f"Telegram: ищу номера в переписке ({len(refs)}) — по базе на этом компьютере…")
        hits = tg_hits(self.cfg, self.store, refs, since)
        info = {"refs": len(hits), "msgs": sum(len(v) for v in hits.values()),
                "chats": len({c for v in hits.values() for c, _m, _d in v})}
        self.progress(f"Telegram: упоминаются {info['refs']} из {len(refs)} номеров — сообщений {info['msgs']} "
                      f"в {info['chats']} чатах")
        if not hits:
            return {}, info
        # выдержки по чатам: упоминание ± соседние сообщения
        blocks: list[tuple[list[str], str, dict[int, set[int]]]] = []
        for ref, hs in hits.items():
            by_chat: dict[int, dict[int, object]] = {}
            for cid, mid, _d in hs:
                for m in self.store.context(cid, mid, before=2, after=2):
                    by_chat.setdefault(cid, {})[m.msg_id] = m
            parts = []
            ids: dict[int, set[int]] = {}
            for cid, ms in by_chat.items():
                c = self.store.get_chat(cid)
                msgs = [ms[k] for k in sorted(ms)]
                media = self.store.media_of(cid, [m.msg_id for m in msgs])
                parts.append(f"=== номер {ref} · чат «{c.title if c else cid}» ===\n" +
                             "\n".join(transcript_lines(msgs, self.tz, None, media)))
                ids[cid] = set(ms)
            blocks.append(([ref], "\n".join(parts)[-9000:], ids))
        # порции по бюджету
        budget = self.budget()
        batches: list[tuple[list[str], str, dict[str, dict[int, set[int]]]]] = []
        cur_refs: list[str] = []
        cur_txt: list[str] = []
        cur_ids: dict[str, dict[int, set[int]]] = {}
        for rs, txt, ids in blocks:
            if cur_txt and sum(map(len, cur_txt)) + len(txt) > budget:
                batches.append((cur_refs, "\n\n".join(cur_txt), cur_ids))
                cur_refs, cur_txt, cur_ids = [], [], {}
            cur_refs += rs
            cur_txt.append(txt)
            cur_ids[rs[0]] = ids
        if cur_txt:
            batches.append((cur_refs, "\n\n".join(cur_txt), cur_ids))
        out: dict[str, dict] = {}
        for i, (rs, txt, ids) in enumerate(batches, 1):
            self.check()
            self.progress(f"Telegram: что сказано о номерах — запрос {i}/{len(batches)} ({len(rs)} номеров)…")
            try:
                data = self._ask_json(TG_TASK.format(refs=", ".join(rs), text=txt), max_tokens=3000)
            except (LLMError, OSError, ValueError) as e:
                self.errors.append(f"Telegram, выводы по номерам ({', '.join(rs[:5])}): {_s(e, 200)}")
                data = {}
            got = {plugins._norm_ref(r.get("ref") or ""): r for r in _items(data, "refs")}
            for ref in rs:
                r = got.get(ref)
                hs = hits.get(ref) or []
                last = max((d for _c, _m, d in hs), default="")
                links = []
                want = {int(x) for x in re.findall(r"\d+", " ".join(map(str, (r or {}).get("msgs") or [])))}
                pool = ids.get(ref) or {}
                chosen = [(c, m) for c in pool for m in sorted(pool[c]) if m in want] or [(c, m) for c, m, _d in hs]
                for cid, mid in chosen[-6:]:
                    ch = self.store.get_chat(cid)
                    if ch is None:
                        continue
                    links.append({"chat": ch.title, "chat_id": cid, "id": mid, "url": msg_link(ch, mid)})
                out[ref] = {"summary": _s((r or {}).get("summary"), 800), "latest": _s((r or {}).get("latest"), 400),
                            "date": _s((r or {}).get("date"), 40), "waiting": _s((r or {}).get("waiting"), 300),
                            "last": last, "count": len(hs), "links": links, "discussed": bool(r)}
        return out, info

    def _ext_by_ref(self, ext: dict[str, list[dict]], refs: list[str]) -> dict[str, dict[str, list[dict]]]:
        """Письма/сообщения/встречи → к каким номерам относятся (по полю numbers и по тексту)."""
        out: dict[str, dict[str, list[dict]]] = {}
        want = set(refs)
        for kind, rows in ext.items():
            for x in rows:
                d = x["data"]
                nums = {plugins._norm_ref(n) for n in d.get("numbers") or []}
                blob = " ".join(str(v) for v in plugins._flat(d)).upper()
                nums |= {r for r in want if r in blob}
                for n in nums & want:
                    out.setdefault(n, {}).setdefault(kind, []).append(x)
        return out

    def _item(self, start: str, x: dict, linked: list[str], sec: dict, tg: dict, ext: dict, now: datetime) -> dict:
        d = x["data"]
        it = {"ref": x["ref"], "kind": start, "title": _s(x["title"] or d.get("title"), 300),
              "status": _s(x["status"] or d.get("status"), 80), "type": _s(d.get("type"), 40),
              "priority": _s(d.get("priority"), 40), "url": _s(d.get("url"), 400),
              "assignee": _s(d.get("assignee"), 120), "description": _s(d.get("description"), 1200),
              "resolution": _s(d.get("resolution"), 600), "comments": _comments(d.get("comments")),
              "updated": _s(d.get("updated"), 40), "linked": linked,
              "tg": tg.get(x["ref"]), "ext": {k: [self._ext_view(e) for e in v[:6]] for k, v in (ext.get(x["ref"]) or {}).items()}}
        if start == "itsm":
            it.update(group=_s(d.get("group"), 160), service=_s(d.get("service"), 160), opened=_s(d.get("opened"), 40),
                      deadline=_s(d.get("deadline"), 40), waiting=_truthy(d.get("waiting")) or bool(_s(d.get("waiting_reason"))),
                      waiting_reason=_s(d.get("waiting_reason"), 300), waiting_for=_s(d.get("waiting_for"), 80),
                      waiting_until=_s(d.get("waiting_until"), 40), waiting_task=plugins._norm_ref(d.get("waiting_task") or ""),
                      related=[_s(r, 40) for r in d.get("related") or []][:10])
        else:
            closed = _truthy(d.get("closed")) or bool(CLOSED_RE.search(it["status"]))
            it.update(closed=closed, release=_s(d.get("release"), 120), release_date=_s(d.get("release_date"), 40),
                      due=_s(d.get("due"), 40), waiting_for=_s(d.get("waiting_for"), 80),
                      waiting_what=_s(d.get("waiting_what"), 300), project=_s(d.get("project"), 120),
                      sprint=_s(d.get("sprint"), 80), opened="", waiting=False, waiting_reason="")
        op = parse_date(it.get("opened"))
        up = parse_date(it.get("updated"))
        it["age_days"] = _days(op, now)
        it["idle_days"] = _days(up, now)
        it["links"] = [{"ref": r, "found": bool(sec.get(r) and sec[r]["found"]),
                        "title": _s((sec.get(r) or {}).get("title"), 200), "status": _s((sec.get(r) or {}).get("status"), 80),
                        "closed": self._closed(sec.get(r)) if sec.get(r) else None} for r in linked]
        it["flags"], it["code_flags"] = [], []
        it["verdict"] = {}
        return it

    @staticmethod
    def _closed(x: dict | None) -> bool:
        if not x or not x["found"]:
            return False
        d = x["data"]
        return _truthy(d.get("closed")) or bool(CLOSED_RE.search(x.get("status") or d.get("status") or ""))

    @staticmethod
    def _ext_view(x: dict) -> dict:
        d = x["data"]
        return {"ref": x["ref"], "source": x["source"], "title": _s(d.get("subject") or d.get("title") or d.get("chat"), 200),
                "date": _s(d.get("date"), 40), "from": _s(d.get("from"), 120), "summary": _s(d.get("summary") or d.get("text"), 500),
                "url": _s(d.get("url"), 400)}

    def _secondary(self, kind: str, sec: dict, links: dict[str, list[str]], tg: dict, ext: dict, now: datetime) -> list[dict]:
        users: dict[str, list[str]] = {}
        for r, ls in links.items():
            for s in ls:
                users.setdefault(s, []).append(r)
        out = []
        for ref, x in sec.items():
            d = (x or {}).get("data") or {}
            found = bool(x and x["found"])
            v = {"ref": ref, "kind": kind, "found": found, "title": _s((x or {}).get("title") or d.get("title"), 300),
                 "status": _s((x or {}).get("status") or d.get("status"), 80), "used_by": users.get(ref, []),
                 "url": _s(d.get("url"), 400), "assignee": _s(d.get("assignee"), 120),
                 "updated": _s(d.get("updated"), 40), "comments": _comments(d.get("comments"))[:4],
                 "description": _s(d.get("description"), 600), "tg": tg.get(ref),
                 "ext": {k: [self._ext_view(e) for e in v2[:4]] for k, v2 in (ext.get(ref) or {}).items()}}
            if kind == "tracker":
                v.update(closed=self._closed(x), release=_s(d.get("release"), 120), release_date=_s(d.get("release_date"), 40),
                         due=_s(d.get("due"), 40), waiting_for=_s(d.get("waiting_for"), 80),
                         waiting_what=_s(d.get("waiting_what"), 300), resolution=_s(d.get("resolution"), 300))
            else:
                v.update(closed=self._closed(x), waiting=_truthy(d.get("waiting")) or bool(_s(d.get("waiting_reason"))),
                         waiting_reason=_s(d.get("waiting_reason"), 300), waiting_for=_s(d.get("waiting_for"), 80),
                         group=_s(d.get("group"), 160))
            v["idle_days"] = _days(parse_date(v["updated"]), now)
            out.append(v)
        out.sort(key=lambda s: (-len(s["used_by"]), s["ref"]))
        return out

    def _code_flags(self, start: str, items: list[dict], sec: dict[str, dict], now: datetime) -> None:
        """Расхождения, которые видно без модели."""
        for it in items:
            f = []
            if start == "itsm":
                for ln in it["links"]:
                    s = sec.get(ln["ref"])
                    if not ln["found"]:
                        if s is not None:
                            f.append(f"задача {ln['ref']} не найдена в трекере")
                        continue
                    if ln["closed"] and (it["waiting"] or ln["ref"] == it.get("waiting_task")):
                        f.append(f"задача {ln['ref']} уже закрыта ({ln['status'] or 'закрыта'}), а запись всё ещё ждёт")
                    elif s and s.get("release_date"):
                        rd = parse_date(s["release_date"])
                        if rd and rd < now and not ln["closed"]:
                            f.append(f"релиз задачи {ln['ref']} ({s['release_date']}) прошёл, а задача не закрыта")
                wf = (it.get("waiting_for") or "").lower()
                if it["waiting"] and re.search(r"поддерж|support|2-?[яй]? ?лин|l2\b", wf):
                    f.append("ждут от поддержки — то есть от вас")
                wu = parse_date(it.get("waiting_until"))
                if wu and wu < now:
                    f.append(f"срок ожидания ({it['waiting_until']}) прошёл")
            else:
                for ln in it["links"]:
                    s = sec.get(ln["ref"])
                    if it.get("closed") and s and s.get("found") and not s.get("closed"):
                        f.append(f"задача закрыта, а инцидент {ln['ref']} ещё открыт ({ln['status'] or '?'})")
            dl = parse_date(it.get("deadline") or it.get("due"))
            if dl and dl < now and not it.get("closed"):
                f.append(f"срок ({it.get('deadline') or it.get('due')}) прошёл")
            if it["idle_days"] is not None and it["idle_days"] >= self.stale_days:
                f.append(f"нет движения {it['idle_days']} дн.")
            t = it.get("tg") or {}
            up = parse_date(it.get("updated"))
            if t.get("last") and up and t.get("discussed"):
                try:
                    tl = parse_iso(t["last"])
                except ValueError:
                    tl = None
                if tl and tl - up > timedelta(hours=12):
                    f.append(f"в Telegram обсуждали позже последнего обновления "
                             f"({tl.astimezone(self.tz):%d.%m.%Y})")
            it["code_flags"] = f

    def _card(self, start: str, it: dict, sec: dict[str, dict]) -> str:
        """Запись для модели: карточка, связанное, выводы из Telegram, письма — одним блоком."""
        head = [it["ref"], it.get("type"), f"«{it['title']}»" if it["title"] else "", f"статус: {it['status']}" if it["status"] else "",
                f"приоритет: {it['priority']}" if it.get("priority") else "", f"сервис: {it['service']}" if it.get("service") else "",
                f"открыт {it['opened']}" if it.get("opened") else "", f"обновлён {it['updated']}" if it.get("updated") else "",
                f"исполнитель: {it['assignee']}" if it.get("assignee") else "", f"срок: {it.get('deadline') or it.get('due')}"
                if it.get("deadline") or it.get("due") else ""]
        lines = ["=== " + " · ".join(h for h in head if h)]
        if start == "itsm" and it["waiting"]:
            lines.append("Ожидание: " + "; ".join(x for x in (it["waiting_reason"], f"ждут от: {it['waiting_for']}" if it["waiting_for"] else "",
                                                             f"задача {it['waiting_task']}" if it["waiting_task"] else "",
                                                             f"до {it['waiting_until']}" if it["waiting_until"] else "") if x))
        if start == "tracker":
            lines.append("Задача: " + "; ".join(x for x in ("закрыта" if it.get("closed") else "не закрыта",
                                                           f"релиз {it['release']}" if it.get("release") else "",
                                                           f"дата релиза {it['release_date']}" if it.get("release_date") else "",
                                                           f"действовать: {it['waiting_for']}" if it.get("waiting_for") else "",
                                                           it.get("waiting_what") or "") if x))
        if it["description"]:
            lines.append("Суть: " + it["description"])
        if it.get("resolution"):
            lines.append("Решение: " + it["resolution"])
        if it["comments"]:
            lines.append("Журнал (свежие первыми): " + " | ".join(
                f"{c['date']} {c['author']}: {c['text'][:300]}".strip() for c in it["comments"][:6]))
        for ln in it["links"]:
            s = sec.get(ln["ref"])
            if not s:
                lines.append(f"Связано: {ln['ref']} — не проверено")
                continue
            if not s["found"]:
                lines.append(f"Связано: {ln['ref']} — не найдено")
                continue
            bits = [f"«{s['title']}»" if s["title"] else "", f"статус: {s['status']}" if s["status"] else "",
                    "закрыта" if s.get("closed") else "", f"релиз {s['release']}" if s.get("release") else "",
                    f"дата релиза {s['release_date']}" if s.get("release_date") else "",
                    f"действовать: {s['waiting_for']}" if s.get("waiting_for") else "", s.get("waiting_what") or "",
                    f"ожидание: {s['waiting_reason']}" if s.get("waiting_reason") else "",
                    f"обновлено {s['updated']}" if s.get("updated") else "",
                    ("последний комментарий: " + s["comments"][0]["text"][:240]) if s.get("comments") else ""]
            lines.append(f"Связано: {ln['ref']} — " + "; ".join(b for b in bits if b))
        t = it.get("tg") or {}
        if t.get("discussed"):
            lines.append("Telegram (выводы по чатам): " + "; ".join(x for x in (t["summary"], f"свежее: {t['latest']}" if t["latest"] else "",
                                                                                f"ждут: {t['waiting']}" if t["waiting"] else "") if x))
        elif t.get("count"):
            lines.append(f"Telegram: упоминается {t['count']} раз, но по сути не обсуждается")
        for kind, rows in (it.get("ext") or {}).items():
            for e in rows[:4]:
                lines.append(f"{plugins.SHORT[kind]}: {e['date']} «{e['title']}» — {e['summary'][:300]}")
        if it["code_flags"]:
            lines.append("Программа заметила: " + "; ".join(it["code_flags"]))
        return "\n".join(lines)

    def _map(self, start: str, qtext: str, items: list[dict], sec: dict[str, dict], now: datetime) -> None:
        from .llm import LLMError
        if not items:
            return
        cards = [(it, self._card(start, it, sec)) for it in items]
        budget = self.budget()
        batches: list[list[tuple[dict, str]]] = [[]]
        size = 0
        for it, c in cards:
            if batches[-1] and (size + len(c) > budget or len(batches[-1]) >= 12):
                batches.append([])
                size = 0
            batches[-1].append((it, c[:budget]))
            size += len(c)
        extra = prompts.extra_block(self.extra) if self.extra else ""
        today = now.astimezone(self.tz).strftime("%d.%m.%Y")
        second_w = "задачи трекера" if start == "itsm" else "инциденты ITSM"
        for i, b in enumerate(batches, 1):
            self.check()
            if len(batches) > 1:
                self.progress(f"  порция {i}/{len(batches)}: {len(b)} записей")
            try:
                data = self._ask_json(MAP_TASK.format(today=today, query=qtext, n=len(b), second=second_w,
                                                      extra=("\n" + extra + "\n") if extra else "",
                                                      cards="\n\n".join(c for _it, c in b)), max_tokens=min(800 * len(b) + 800, 8000))
            except (LLMError, OSError, ValueError) as e:
                self.errors.append(f"разбор записей (порция {i}): {_s(e, 200)}")
                data = {}
            got = {plugins._norm_ref(r.get("ref") or ""): r for r in _items(data, "items")}
            for it, _c in b:
                r = got.get(it["ref"])
                if r is None:
                    it["verdict"] = self._guess(start, it)
                    continue
                v = {"actual": r.get("actual") if r.get("actual") in ACTUAL_RU else "doubt",
                     "actual_why": _s(r.get("actual_why"), 300),
                     "expects": r.get("expects") if r.get("expects") in EXPECTS_RU else "unclear",
                     "expects_what": _s(r.get("expects_what"), 300), "problem": _s(r.get("problem"), 160),
                     "summary": _s(r.get("summary"), 600), "next": _s(r.get("next"), 400),
                     "priority": r.get("priority") if r.get("priority") in ("high", "medium", "low") else "medium"}
                it["verdict"] = v
                it["flags"] = [_s(f, 240) for f in r.get("flags") or [] if _s(f)][:6]

    def _guess(self, start: str, it: dict) -> dict:
        """Модель не ответила по записи — самое простое по полям карточки (помечено как без модели)."""
        wf = (it.get("waiting_for") or "").lower()
        exp = ("support" if re.search(r"поддерж|support", wf) else "dev" if re.search(r"разраб|dev", wf)
               else "user" if re.search(r"пользоват|инициат|клиент|заказчик", wf)
               else "vendor" if re.search(r"подряд|групп|vendor", wf) else "unclear")
        return {"actual": "doubt", "actual_why": "модель не разобрала эту запись", "expects": exp,
                "expects_what": it.get("waiting_reason") or it.get("waiting_what") or "", "problem": "",
                "summary": it.get("description") or "", "next": "", "priority": "medium", "no_model": True}

    def _stats(self, start: str, items: list[dict], secondary: list[dict]) -> dict:
        def top(c: Counter, n: int = 12) -> list[list]:
            return [[k, v] for k, v in c.most_common(n) if k]
        st: dict = {"total": len(items)}
        st["by_status"] = top(Counter(it["status"] or "—" for it in items))
        st["by_type"] = top(Counter(it.get("type") or "—" for it in items))
        if start == "itsm":
            w = [it for it in items if it["waiting"]]
            st["waiting"] = len(w)
            st["by_waiting"] = top(Counter((it["waiting_reason"] or "причина не указана")[:80] for it in w))
            st["by_waiting_for"] = top(Counter((it["waiting_for"] or "не указано")[:60] for it in w))
            st["by_group"] = top(Counter(it.get("group") or "—" for it in items))
        else:
            st["closed"] = sum(1 for it in items if it.get("closed"))
            st["by_release"] = top(Counter(it.get("release") or "без релиза" for it in items))
        st["by_expects"] = [[EXPECTS_RU[k], v] for k, v in Counter(it["verdict"].get("expects", "unclear") for it in items).most_common()]
        st["by_actual"] = [[ACTUAL_RU[k], v] for k, v in Counter(it["verdict"].get("actual", "doubt") for it in items).most_common()]
        st["by_problem"] = top(Counter(it["verdict"].get("problem") for it in items if it["verdict"].get("problem")), 15)
        st["old"] = sum(1 for it in items if (it["age_days"] or 0) >= self.old_days)
        st["stale"] = sum(1 for it in items if (it["idle_days"] or 0) >= self.stale_days)
        st["old_days"], st["stale_days"] = self.old_days, self.stale_days
        st["with_links"] = sum(1 for it in items if it["links"])
        st["secondary"] = sum(1 for s in secondary if s["found"])
        st["secondary_missing"] = sum(1 for s in secondary if not s["found"])
        st["secondary_closed"] = sum(1 for s in secondary if s["found"] and s.get("closed"))
        st["shared"] = [[s["ref"], len(s["used_by"])] for s in secondary if len(s["used_by"]) >= 2][:15]
        st["in_telegram"] = sum(1 for it in items if (it.get("tg") or {}).get("count"))
        for kind in ("mail", "rooms", "meet"):
            st["in_" + kind] = sum(1 for it in items if (it.get("ext") or {}).get(kind))
        st["flagged"] = sum(1 for it in items if it["flags"] or it["code_flags"])
        return st

    @staticmethod
    def _stats_text(st: dict) -> str:
        out = [f"всего записей: {st['total']}"]
        for k, title in (("by_status", "по статусам"), ("by_waiting", "в ожидании — чего ждут"),
                         ("by_waiting_for", "в ожидании — от кого"), ("by_expects", "от кого ждут движения (оценка)"),
                         ("by_actual", "актуальность (оценка)"), ("by_release", "по релизам")):
            if st.get(k):
                out.append(f"{title}: " + ", ".join(f"{a} — {b}" for a, b in st[k]))
        if "waiting" in st:
            out.append(f"в ожидании: {st['waiting']}")
        out.append(f"старше {st['old_days']} дн.: {st['old']}; без движения {st['stale_days']}+ дн.: {st['stale']}")
        if st.get("shared"):
            out.append("общие связанные записи: " + ", ".join(f"{a} — у {b} записей" for a, b in st["shared"]))
        out.append(f"связанных найдено: {st['secondary']}, из них закрыто: {st['secondary_closed']}, "
                   f"не найдено: {st['secondary_missing']}")
        out.append(f"обсуждались в Telegram: {st['in_telegram']}; в почте: {st['in_mail']}")
        return "\n".join(out)

    def _reduce(self, start: str, qtext: str, items: list[dict], secondary: list[dict], stats: dict,
                now: datetime) -> dict:
        from .llm import LLMError
        lines = []
        for it in items:
            v = it["verdict"]
            lk = ", ".join(f"{ln['ref']} ({'закрыта' if ln['closed'] else ln['status'] or '?'})" for ln in it["links"] if ln["found"])
            bits = [it["ref"], f"«{it['title'][:120]}»", it["status"], ("ждёт: " + it["waiting_reason"][:100]) if it.get("waiting") else "",
                    f"связано: {lk}" if lk else "", f"проблема: {v.get('problem')}" if v.get("problem") else "",
                    f"оценка: {ACTUAL_RU.get(v.get('actual'), '?')}", f"ждут от: {EXPECTS_RU.get(v.get('expects'), '?')}"
                    + (f" — {v['expects_what'][:120]}" if v.get("expects_what") else ""),
                    ("суть: " + v["summary"][:240]) if v.get("summary") else "",
                    ("настораживает: " + "; ".join((it["code_flags"] + it["flags"])[:4])) if it["code_flags"] or it["flags"] else ""]
            lines.append("- " + "; ".join(b for b in bits if b))
        sec_lines = [f"- {s['ref']} «{s['title'][:100]}» — {s['status'] or '?'}" + ("; закрыта" if s.get("closed") else "")
                     + f"; нужна записям: {', '.join(s['used_by'][:8])}" for s in secondary if s["found"]][:60]
        body = "\n".join(lines)
        if sec_lines:
            body += "\n\nСвязанные " + ("задачи трекера" if start == "itsm" else "инциденты ITSM") + ":\n" + "\n".join(sec_lines)
        budget = self.budget() * 2
        if len(body) > budget:
            body = body[:budget] + "\n… (список обрезан)"
        extra = prompts.extra_block(self.extra) if self.extra else ""
        self.progress("Модель: общая картина очереди…")
        try:
            data = self._ask_json(REDUCE_TASK.format(
                today=now.astimezone(self.tz).strftime("%d.%m.%Y"), query=qtext, n=len(items),
                second="задачи трекера" if start == "itsm" else "инциденты ITSM",
                extra=("\n" + extra + "\nОтветь на эти указания и вопрос пользователя в поле answer.\n") if extra else "",
                answer=', "answer": "ответ на указания и вопрос пользователя"' if extra else "",
                stats=self._stats_text(stats), lines=body), max_tokens=5000)
        except (LLMError, OSError, ValueError) as e:
            self.errors.append(f"общая картина: {_s(e, 200)}")
            return {}
        if not isinstance(data, dict):
            return {}
        refs = {it["ref"] for it in items}
        groups = []
        for g in data.get("groups") or []:
            if not isinstance(g, dict) or not g.get("title"):
                continue
            rs = [plugins._norm_ref(r) for r in g.get("refs") or [] if plugins._norm_ref(r) in refs]
            groups.append({"title": _s(g["title"], 200), "refs": rs, "tasks": [plugins._norm_ref(t) for t in g.get("tasks") or []][:10],
                           "why": _s(g.get("why"), 500), "status": _s(g.get("status"), 200), "next": _s(g.get("next"), 400)})
        acts = [{"title": _s(a.get("title"), 300), "why": _s(a.get("why"), 400),
                 "refs": [plugins._norm_ref(r) for r in a.get("refs") or []][:12]}
                for a in data.get("actions") or [] if isinstance(a, dict) and a.get("title")]
        close = [{"ref": plugins._norm_ref(c.get("ref")), "why": _s(c.get("why"), 300)}
                 for c in data.get("close") or [] if isinstance(c, dict) and c.get("ref")]
        clar = [_s(c, 300) for c in data.get("clarify") or [] if _s(c)]
        return {"headline": data.get("headline"), "groups": groups, "actions": acts, "close": close, "clarify": clar,
                "answer": data.get("answer")}

    @staticmethod
    def _attention(items: list[dict]) -> list[dict]:
        order = {"high": 0, "medium": 1, "low": 2}
        out = [{"ref": it["ref"], "title": it["title"], "flags": list(dict.fromkeys(it["code_flags"] + it["flags"])),
                "priority": it["verdict"].get("priority", "medium")} for it in items if it["code_flags"] or it["flags"]]
        out.sort(key=lambda x: (order.get(x["priority"], 1), -len(x["flags"])))
        return out

    @staticmethod
    def _fallback_headline(start: str, items: list[dict], st: dict) -> str:
        if not items:
            return "По этим условиям записей нет."
        w = f", в ожидании — {st['waiting']}" if st.get("waiting") is not None else ""
        return (f"В очереди {st['total']} записей{w}; без движения {st['stale_days']}+ дн. — {st['stale']}; "
                f"требуют внимания — {st['flagged']}.")


# ---------------------------------------------------------------- данные отчёта
def build(cfg, store: Store, llm, *, start: str, query: dict | None, look: dict | None, extra: str = "",
          progress: Progress = print, cancel: Callable[[], bool] | None = None, now: datetime | None = None) -> dict:
    """Сводка «От источника» целиком — в том же виде, что обычная (meta, overview…), плюс раздел source."""
    now = now or datetime.now(timezone.utc)
    sd = SourceDigest(cfg, store, llm, progress=progress, cancel=cancel, extra=extra)
    src = sd.build(start, query or {}, look or {}, now)
    tz = sd.tz
    since = now - timedelta(days=src["look"]["days"])
    label = f"очередь {plugins.SHORT[start]}" + (f" «{src['query']['group']}»" if src["query"].get("group") else "")
    data = {
        "meta": {
            "mode": "source", "period": "source", "since": iso(since), "until": iso(now),
            "period_text": f"на {now.astimezone(tz):%d.%m.%Y %H:%M} · упоминания за {src['look']['days']} дн.",
            "period_label": label, "extra_prompt": sd.extra or None, "timezone": str(tz),
            "generated": iso(datetime.now(timezone.utc)), "generated_local": datetime.now(tz).strftime("%d.%m.%Y %H:%M"),
            "provider": llm.label, "chats": src["tg"]["chats"], "messages": src["tg"]["msgs"],
            "llm_calls": llm.calls, "chars_in": llm.chars_in, "chars_out": llm.chars_out, "errors": src["errors"],
            "me": sd.me_name or cfg.profile.get("name", ""), "history": {"on": False},
            "privacy": ({"persons": len(sd.privacy.persons), **sd.privacy.stats} if sd.privacy is not None else None),
        },
        "overview": {"headline": src["headline"], "top": [], "control": [], "incidents": [], "competences": [],
                     "clarify": src["clarify"], "fyi": [], "changes": []},
        "chats": [], "refs": [], "source": src,
    }
    log.info("от источника %s: записей %d, связанных %d, запросов к модели %d, ошибок %d", start, len(src["items"]),
             len(src["secondary"]), llm.calls, len(src["errors"]))
    return data
