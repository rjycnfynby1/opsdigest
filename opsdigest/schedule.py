"""Сводки по расписанию: несколько расписаний, у каждого — свой шаблон сводки (период, чаты, указания, плагины).

Запускает их сама программа (сервер окна работает в фоне под сторожем, окно можно закрыть), а не Планировщик
Windows: так работает и защищённая папка — ключ данных уже в памяти, пароль не нужен.

Расписание (config.json → digest.schedules):
  {"id", "name", "enabled", "preset": id шаблона или "" (сутки, все чаты),
   "mode": "days" | "every",
   "days": [1..7] (1 — понедельник), "times": ["08:40", "13:00"]          — mode=days: по дням недели в эти часы
   "every_min": 180, "from": "09:00", "to": "21:00"                         — mode=every: каждые N минут в окне
   "send": true — отправить короткую сводку и отчёт в «Избранное» Telegram}
Состояние запусков — в базе (kv schedule_state): {id: {"last": ISO, "status", "error", "report", "planned"}}.

Срабатывает, только пока программа запущена и данные открыты (решение пользователя 09.11): если в плановое время
программа была закрыта, компьютер спал или выключен, данные закрыты паролем — этот запуск просто пропускается, без
попыток задним числом и без сообщений об ошибках. Если в плановое время программа работала, но шла другая сводка, —
запуск ждёт её конца (до MAX_WAIT).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, time, timedelta

DAY_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
ON_TIME = timedelta(minutes=2)    # проверка — раз в ~20 с; опоздание больше — значит, программа спала/была закрыта
MAX_WAIT = timedelta(hours=2)     # плановый запуск ждёт, пока закончится другая сводка, не дольше этого
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


class ScheduleError(ValueError):
    """Расписание задано неверно — текст понятен пользователю."""


def _t(s: str) -> time:
    m = TIME_RE.match(str(s or "").strip())
    if not m:
        raise ScheduleError(f"время «{str(s)[:10]}» — нужно ЧЧ:ММ, например 08:40")
    return time(int(m.group(1)), int(m.group(2)))


def normalize(e: dict) -> dict:
    """Проверить и привести расписание из окна к виду для config.json."""
    if not isinstance(e, dict):
        raise ScheduleError("пустое расписание")
    mode = e.get("mode") if e.get("mode") in ("days", "every") else "days"
    days = sorted({int(d) for d in (e.get("days") or []) if str(d).isdigit() and 1 <= int(d) <= 7})
    if not days:
        raise ScheduleError("выберите хотя бы один день недели")
    out = {"id": str(e.get("id") or "") or secrets.token_hex(4),
           "name": str(e.get("name") or "").strip()[:80],
           "enabled": e.get("enabled", True) is not False,
           "preset": str(e.get("preset") or ""), "mode": mode, "days": days, "send": bool(e.get("send", True))}
    if mode == "days":
        raw = e.get("times") or []
        if isinstance(raw, str):
            raw = re.split(r"[,;\s]+", raw)
        times = sorted({_t(x).strftime("%H:%M") for x in raw if str(x).strip()})
        if not times:
            raise ScheduleError("укажите время запуска, например 08:40 (можно несколько через запятую)")
        if len(times) > 24:
            raise ScheduleError("не больше 24 запусков в день")
        out["times"] = times
    else:
        try:
            every = int(round(float(e.get("every_min") or 0)))
        except (TypeError, ValueError):
            raise ScheduleError("интервал — числом") from None
        if every < 30:
            raise ScheduleError("не чаще раза в 30 минут — Maverick ведёт одну сессию за раз")
        if every > 24 * 60:
            raise ScheduleError("интервал больше суток — выберите «по дням недели»")
        a, b = _t(e.get("from") or "00:00"), _t(e.get("to") or "23:59")
        if (b.hour, b.minute) <= (a.hour, a.minute):
            raise ScheduleError("«до» должно быть позже «с» (в пределах одного дня)")
        out.update(every_min=every, **{"from": a.strftime("%H:%M"), "to": b.strftime("%H:%M")})
    if not out["name"]:
        out["name"] = describe(out)
    return out


def _times_of_day(e: dict, day: datetime) -> list[datetime]:
    """Моменты запуска в этот день (местное время, без пояса)."""
    if day.isoweekday() not in e.get("days", []):
        return []
    d0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
    if e.get("mode") == "every":
        a, b = _t(e.get("from") or "00:00"), _t(e.get("to") or "23:59")
        t = d0.replace(hour=a.hour, minute=a.minute)
        end = d0.replace(hour=b.hour, minute=b.minute)
        step = timedelta(minutes=max(30, int(e.get("every_min") or 60)))
        out = []
        while t <= end:
            out.append(t)
            t += step
        return out
    return [d0.replace(hour=_t(x).hour, minute=_t(x).minute) for x in e.get("times") or []]


def next_run(e: dict, now: datetime) -> datetime | None:
    """Ближайший запуск после now (местное время без пояса)."""
    for i in range(0, 9):
        for t in _times_of_day(e, now + timedelta(days=i)):
            if t > now:
                return t
    return None


def last_planned(e: dict, now: datetime) -> datetime | None:
    """Последний запуск по расписанию не позже now."""
    best = None
    for i in range(0, 8):
        for t in _times_of_day(e, now - timedelta(days=i)):
            if t <= now and (best is None or t > best):
                best = t
        if best is not None:
            return best
    return best


def due(e: dict, now: datetime, last_done: datetime | None, created: datetime | None = None) -> datetime | None:
    """Пора запускать? → плановое время запуска (оно запоминается, чтобы не запустить дважды) или None."""
    if not e.get("enabled", True):
        return None
    t = last_planned(e, now)
    if t is None:
        return None
    if last_done is not None and last_done >= t:
        return None
    if created is not None and created > t:   # расписание создали после этого времени — ждём следующего
        return None
    if now - t > ON_TIME:   # время прошло, пока программа не работала (сон, выключен) — пропускаем молча
        return None
    return t


def describe(e: dict) -> str:
    """«По будням в 08:40», «Пн, ср, пт в 09:00 и 17:00», «Каждые 3 ч с 09:00 до 21:00 по будням»."""
    days = e.get("days") or []
    if days == [1, 2, 3, 4, 5]:
        d = "по будням"
    elif days == [1, 2, 3, 4, 5, 6, 7]:
        d = "каждый день"
    elif days == [6, 7]:
        d = "по выходным"
    else:
        d = ", ".join(DAY_RU[i - 1] for i in days)
    if e.get("mode") == "every":
        m = int(e.get("every_min") or 60)
        h, mm = divmod(m, 60)
        step = " ".join(x for x in (f"{h} ч" if h else "", f"{mm} мин" if mm else "") if x)
        return f"Каждые {step} с {e.get('from')} до {e.get('to')}, {d}"
    times = e.get("times") or []
    t = " и ".join([", ".join(times[:-1]), times[-1]]) if len(times) > 1 else (times[0] if times else "—")
    return f"{d[:1].upper() + d[1:]} в {t}"


def parse_local(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
