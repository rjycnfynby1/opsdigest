"""«Сводка»: шаблоны (сохранённые настройки сводки) и сводки по расписанию.

Шаблон (config.json → digest.presets): {"id", "name", "saved", "settings": {period, hours, minutes, since, until,
chat_ids, chat_titles, extra_prompt, sources, sync, send, preempt}} — то, что окно присылает в /api/digest.
Расписания — opsdigest/schedule.py; их запускает App.schedule_tick() (раз в ~20 с, пока программа работает).
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime

from .. import logs
from .. import schedule as S
from .. import workspace as wsmod

log = logs.get("schedule")
PRESET_KEYS = ("period", "hours", "minutes", "since", "until", "chat_ids", "chat_titles", "extra_prompt", "sources",
               "sync", "send", "preempt", "history", "mode", "start", "query", "look")
MAX_PRESETS = 40
MAX_SCHEDULES = 20


def _clean_settings(st: dict) -> dict:
    from .server import UserError
    if not isinstance(st, dict):
        raise UserError("нет настроек для шаблона")
    out = {}
    if st.get("mode") == "source":   # «От источника»: без периода и чатов — что разбираем и где искать номера
        from .. import source_digest as sdm
        start = st.get("start") if st.get("start") in sdm.STARTS else "itsm"
        out = {"mode": "source", "period": "source", "start": start,
               "query": sdm.norm_query(start, st.get("query") if isinstance(st.get("query"), dict) else {}),
               "look": sdm.norm_look(st.get("look") if isinstance(st.get("look"), dict) else {})}
        if str(st.get("extra_prompt") or "").strip():
            out["extra_prompt"] = str(st["extra_prompt"]).strip()[:4000]
        for k in ("sync", "send", "preempt"):
            if k in st and st[k] is not None:
                out[k] = bool(st[k])
        return out
    for k in PRESET_KEYS:
        if k not in st or st[k] is None or k in ("mode", "start", "query", "look"):
            continue
        v = st[k]
        if k in ("period", "since", "until"):
            out[k] = str(v)[:32]
        elif k in ("hours", "minutes"):
            try:
                out[k] = max(0, float(v))
            except (TypeError, ValueError):
                continue
        elif k == "chat_ids":
            ids = [int(x) for x in v if str(x).lstrip("-").isdigit()][:2000] if isinstance(v, list) else []
            if ids:
                out[k] = ids
        elif k == "chat_titles":
            if isinstance(v, dict):
                out[k] = {str(a)[:24]: str(b)[:120] for a, b in list(v.items())[:2000]}
        elif k == "extra_prompt":
            if str(v).strip():
                out[k] = str(v).strip()[:4000]
        elif k == "sources":
            if isinstance(v, dict):
                out[k] = {str(a)[:16]: bool(b) for a, b in v.items()}
        else:
            out[k] = bool(v)
    if not out.get("period"):
        raise UserError("в шаблоне нет периода")
    return out


def presets(raw: dict) -> list[dict]:
    return [p for p in ((raw.get("digest") or {}).get("presets") or []) if isinstance(p, dict) and p.get("id")]


def schedules(raw: dict) -> list[dict]:
    return [x for x in ((raw.get("digest") or {}).get("schedules") or []) if isinstance(x, dict) and x.get("id")]


def preset_text(p: dict | None) -> str:
    """Коротко, что в шаблоне: «3 ч · 4 чата · почта, ITSM · есть указания»."""
    if not p:
        return "сутки · все чаты · источники из настроек"
    s = p.get("settings") or {}
    if s.get("mode") == "source":
        from .. import plugins
        from .. import source_digest as sdm
        lk = s.get("look") or {}
        also = [n for k, n in (("telegram", "Telegram"), ("mail", "почта"), ("rooms", "Rooms"), ("meet", "встречи")) if lk.get(k)]
        return (f"от источника: {plugins.SHORT.get(s.get('start'), s.get('start'))} — "
                f"{sdm.query_text(s.get('start') or 'itsm', s.get('query') or {})}"
                + (f" · + {', '.join(also)}" if also else "") + (" · есть указания" if s.get("extra_prompt") else ""))
    per = s.get("period") or "day"
    if per in ("h1", "h2", "h3"):
        t = f"{per[1:]} ч"
    elif per == "hours":
        from ..digest import hours_label
        t = hours_label(round(float(s.get("hours") or 0) * 60 + float(s.get("minutes") or 0)))
    elif per == "custom":
        t = "с " + str(s.get("since") or "").replace("T", " ") + (" по " + str(s["until"]).replace("T", " ") if s.get("until") else "")
    else:
        t = {"day": "сутки", "week": "неделя", "month": "месяц", "last": "с прошлой сводки"}.get(per, per)
    n = len(s.get("chat_ids") or [])
    bits = [t, f"чатов: {n}" if n else "все чаты"]
    src = [k for k, v in (s.get("sources") or {}).items() if v]
    if src:
        from ..plugins import SHORT
        bits.append(", ".join(SHORT.get(k, k) for k in src))
    if s.get("extra_prompt"):
        bits.append("есть указания")
    return " · ".join(bits)


def state_get(app) -> dict:
    st = app.store()
    try:
        return json.loads(st.kv_get("schedule_state") or "{}")
    except ValueError:
        return {}
    finally:
        st.close()


def state_put(app, sid: str, **kw) -> None:
    with app.cfg_lock:
        st = app.store()
        try:
            cur = json.loads(st.kv_get("schedule_state") or "{}")
            cur[sid] = {**(cur.get(sid) or {}), **kw}
            st.kv_set("schedule_state", json.dumps(cur, ensure_ascii=False))
        finally:
            st.close()


def view(app) -> dict:
    raw = app.raw()
    ps = presets(raw)
    by_id = {p["id"]: p for p in ps}
    state = state_get(app)
    now = datetime.now()
    out = []
    for e in schedules(raw):
        nx = S.next_run(e, now) if e.get("enabled", True) else None
        stt = state.get(e["id"]) or {}
        p = by_id.get(e.get("preset") or "")
        out.append({**e, "text": S.describe(e), "next": nx.isoformat(timespec="minutes") if nx else None,
                    "preset_name": p["name"] if p else ("" if not e.get("preset") else "шаблон удалён"),
                    "preset_text": preset_text(p), "state": stt,
                    "pending": e["id"] in getattr(app, "_sched_pending", {})})
    legacy = wsmod.schedule_status()
    return {"schedules": out, "presets": [{"id": p["id"], "name": p["name"], "text": preset_text(p)} for p in ps],
            "legacy": bool(legacy.get("enabled")), "legacy_at": ((raw.get("ui") or {}).get("schedule") or {}).get("at"),
            "autostart": wsmod.autostart_status(), "supervised": app.sup is not None}


def _update(app, fn):
    with app.cfg_lock:
        raw = app.raw()
        res = fn(raw)
        app.save(raw)
        return res


def route(h, method: str, path: str, b: dict, q: dict):
    from .server import UserError
    app = h.app
    if path == "/api/digest/presets":
        raw = app.raw()
        return {"presets": [dict(p, text=preset_text(p)) for p in presets(raw)]}
    if path == "/api/digest/presets/save" and method == "POST":
        name = str(b.get("name") or "").strip()[:80]
        if not name:
            raise UserError("введите название шаблона")
        settings = _clean_settings(b.get("settings"))

        def go(raw):
            dg = raw.setdefault("digest", {})
            lst = presets(raw)
            pid = str(b.get("id") or "") or next((p["id"] for p in lst if p.get("name", "").lower() == name.lower()), "")
            pid = pid or secrets.token_hex(4)
            rec = {"id": pid, "name": name, "settings": settings, "saved": datetime.now().isoformat(timespec="minutes")}
            old = [p for p in lst if p["id"] != pid]
            if len(old) >= MAX_PRESETS:
                raise UserError(f"шаблонов уже {MAX_PRESETS} — удалите ненужные")
            dg["presets"] = old + [rec]
            return pid
        pid = _update(app, go)
        log.info("шаблон сводки сохранён")
        return {"id": pid, **route(h, "GET", "/api/digest/presets", {}, {})}
    if path == "/api/digest/presets/delete" and method == "POST":
        pid = str(b.get("id") or "")

        def go(raw):
            dg = raw.setdefault("digest", {})
            used = [x["name"] for x in schedules(raw) if x.get("preset") == pid]
            if used and not b.get("force"):
                raise UserError("шаблон используется в расписании: " + ", ".join(used) +
                                " — сначала выберите там другой шаблон")
            dg["presets"] = [p for p in presets(raw) if p["id"] != pid]
        _update(app, go)
        return route(h, "GET", "/api/digest/presets", {}, {})
    if path == "/api/schedules":
        return view(app)
    if path == "/api/schedules/save" and method == "POST":
        try:
            e = S.normalize(b.get("schedule") or {})
        except S.ScheduleError as err:
            raise UserError(str(err)) from None

        def go(raw):
            dg = raw.setdefault("digest", {})
            lst = schedules(raw)
            if e["preset"] and e["preset"] not in {p["id"] for p in presets(raw)}:
                raise UserError("шаблон не найден — выберите другой")
            prev = next((x for x in lst if x["id"] == e["id"]), None)
            if prev is None and len(lst) >= MAX_SCHEDULES:
                raise UserError(f"расписаний уже {MAX_SCHEDULES}")
            # время создания/изменения: запуск «за прошлое время» не делаем
            e["created"] = datetime.now().isoformat(timespec="seconds")
            dg["schedules"] = [x for x in lst if x["id"] != e["id"]] + [e] if prev is None else \
                [e if x["id"] == e["id"] else x for x in lst]
        _update(app, go)
        app._sched_pending.pop(e["id"], None)
        log.info("расписание сводки сохранено: %s", S.describe(e))
        return {"id": e["id"], **view(app)}
    if path == "/api/schedules/toggle" and method == "POST":
        sid, on = str(b.get("id") or ""), bool(b.get("enabled"))

        def go(raw):
            for x in schedules(raw):
                if x["id"] == sid:
                    x["enabled"] = on
                    x["created"] = datetime.now().isoformat(timespec="seconds")
        _update(app, go)
        if not on:
            app._sched_pending.pop(sid, None)
        return view(app)
    if path == "/api/schedules/delete" and method == "POST":
        sid = str(b.get("id") or "")

        def go(raw):
            raw.setdefault("digest", {})["schedules"] = [x for x in schedules(raw) if x["id"] != sid]
        _update(app, go)
        app._sched_pending.pop(sid, None)
        return view(app)
    if path == "/api/schedules/run" and method == "POST":   # «Запустить сейчас» — проверить расписание
        e = next((x for x in schedules(app.raw()) if x["id"] == str(b.get("id") or "")), None)
        if e is None:
            raise UserError("расписание не найдено")
        job = run(app, e, datetime.now(), manual=True)
        return {"job": job.id}
    if path == "/api/schedules/migrate" and method == "POST":   # старое расписание Планировщика Windows → новое
        raw = app.raw()
        old = (raw.get("ui") or {}).get("schedule") or {}
        e = S.normalize({"name": "Каждое утро", "mode": "days", "times": [old.get("at") or "08:40"],
                         "days": [1, 2, 3, 4, 5] if old.get("weekdays_only", True) else [1, 2, 3, 4, 5, 6, 7],
                         "preset": "", "send": True})

        def go(raw):
            e["created"] = datetime.now().isoformat(timespec="seconds")
            raw.setdefault("digest", {})["schedules"] = schedules(raw) + [e]
        _update(app, go)
        ok, msg = wsmod.schedule_disable()
        log.info("старое расписание Планировщика перенесено (удалено: %s)", ok)
        return {"message": "Перенесено. Задача Планировщика Windows удалена." if ok else
                f"Перенесено, но задачу Планировщика удалить не удалось: {msg[:200]}", **view(app)}
    return None


def body_of(app, e: dict) -> dict:
    """Запрос сводки по расписанию: настройки шаблона + отправка в «Избранное»."""
    raw = app.raw()
    p = next((x for x in presets(raw) if x["id"] == e.get("preset")), None)
    s = dict((p or {}).get("settings") or {"period": "day"})
    body = {k: s[k] for k in ("period", "hours", "minutes", "since", "until", "chat_ids", "extra_prompt", "sources",
                              "history", "mode", "start", "query", "look") if k in s}
    tg = app.tg_ok_cached()   # нет входа в Telegram — сводка по базе, без отправки
    body.update(sync=bool(s.get("sync", True)) and tg, send=bool(e.get("send", True)) and tg, preempt=True)
    return body


def run(app, e: dict, planned: datetime, manual: bool = False):
    """Запустить сводку по расписанию (как запрос окна /api/digest) и записать, чем закончилась."""
    h = app.handler_cls.__new__(app.handler_cls)
    body = body_of(app, e)
    job = h._digest_job(body)
    job.title = f"По расписанию: {e.get('name') or S.describe(e)}" + (" (вручную)" if manual else "")
    job.info = {**job.info, "schedule": e["id"]}
    job.add(f"Сводка по расписанию «{e.get('name') or S.describe(e)}»" + (" — запуск вручную" if manual else
                                                                          f", плановое время {planned:%H:%M}"))
    if not manual:
        state_put(app, e["id"], planned=planned.isoformat(timespec="seconds"),
                  started=datetime.now().isoformat(timespec="seconds"), status="running", job=job.id, error=None)
    else:
        state_put(app, e["id"], started=datetime.now().isoformat(timespec="seconds"), status="running", job=job.id,
                  error=None)

    def watch():
        import time
        while job.status == "running":
            time.sleep(2)
        r = job.result if isinstance(job.result, dict) else {}
        try:
            state_put(app, e["id"], status="done" if job.status == "done" else ("stopped" if job.abandoned else "error"),
                      finished=datetime.now().isoformat(timespec="seconds"), report=r.get("report"),
                      error=(job.error or "")[:300] or None, send_error=r.get("send_error"),
                      sync_error=r.get("sync_error"), top=r.get("top"))
        except Exception as err:  # noqa: BLE001 — данные закрыли, пока шла сводка
            log.info("итог расписания не записан: %s", logs.err_info(err))
    threading.Thread(target=watch, daemon=True, name="schedule-watch").start()
    return job
