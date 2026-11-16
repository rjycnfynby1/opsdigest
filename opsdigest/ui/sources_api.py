"""Источники через Maverick в окне: состояние плагинов (почта, ITSM, вики), проверка подключения, настройки
источников (какие плагины включены, как они называются в Maverick, что добавить в запрос) и какие источники
сводка сверяет с Telegram."""
from __future__ import annotations

import json

from .. import plugins


def _creds_for(app) -> dict:
    """Какая запись «Учётных данных» нужна каждому плагину (без секретов) — для подсказки при переподключении."""
    try:
        from ..secure import creds
        return creds.for_plugins(creds.load(app.need_ws()))
    except Exception:  # noqa: BLE001 — нет защиты или записей
        return {}


def _maverick_web(cfg, pc: dict) -> str:
    """Адрес веб-чата Maverick (кнопка «Открыть чат Maverick»): из настроек или по адресу API."""
    if pc.get("web_url"):
        return str(pc["web_url"])
    try:
        _n, prof = cfg.llm_profile("maverick")
    except KeyError:
        return ""
    u = str(prof.get("url") or prof.get("base_url") or "").strip().rstrip("/")
    for tail in ("/api/v1/query", "/api/v1", "/api"):
        if u.endswith(tail):
            u = u[: -len(tail)]
    return u


def route(h, method: str, path: str, b: dict, q: dict):
    from .server import Job, UserError
    app = h.app
    if path == "/api/plugins":
        cfg = app.cfg()
        st = app.store()
        try:
            plugins.ensure(st)
            n = {r[0]: r[1] for r in st.db.execute("SELECT source, COUNT(*) FROM ext_items WHERE found=1 GROUP BY source")}
            raw = app.raw()
            pc = raw.get("plugins") or {}
            dg = raw.get("digest") or {}
            gl = raw.get("glossary") or {}
            return {"available": plugins.available(cfg), "status": plugins.statuses(cfg, st), "saved": n,
                    "settings": {k: {"enabled": (pc.get(k) or {}).get("enabled", True) is not False,
                                     "name": (pc.get(k) or {}).get("name") or "", "hint": (pc.get(k) or {}).get("hint") or "",
                                     "body": json.dumps((pc.get(k) or {}).get("body") or {}, ensure_ascii=False)
                                     if (pc.get(k) or {}).get("body") else "",
                                     "url": (pc.get(k) or {}).get("url") or ""} for k in plugins.KINDS},
                    "check_minutes": pc.get("check_minutes") or 30,
                    "digest_sources": {k: bool((dg.get("sources") or {}).get(k)) for k in plugins.KINDS},
                    "cross_private": bool(dg.get("cross_private")),
                    "glossary": {"auto_collect": gl.get("auto_collect", True) is not False,
                                 "auto_process": gl.get("auto_process", True) is not False,
                                 "use_wiki": gl.get("use_wiki", True) is not False,
                                 "sources": gl.get("sources") if isinstance(gl.get("sources"), list) else ["wiki", "mail"]},
                    "describe_sources": (raw.get("describe") or {}).get("sources")
                    if isinstance((raw.get("describe") or {}).get("sources"), list) else ["wiki", "mail"],
                    "lookup_people": (raw.get("privacy") or {}).get("lookup_people", True) is not False,
                    "images": plugins.image_settings(raw),
                    "kinds": [{"k": k, "title": plugins.TITLES[k], "short": plugins.SHORT[k], "icon": plugins.ICONS[k],
                               "about": plugins.ABOUT[k]} for k in plugins.KINDS],
                    "patterns": ((raw.get("entities") or {}).get("patterns") or {}) or
                    (cfg.get("entities") or {}).get("patterns") or {},
                    "creds": _creds_for(app), "maverick_web": _maverick_web(cfg, pc), "web_url": pc.get("web_url") or ""}
        finally:
            st.close()
    if path == "/api/config/sources" and method == "POST":
        with app.cfg_lock:   # фоновые задачи тоже пишут config.json — по очереди, иначе изменения потеряются
            return _save_sources(app, b)
    if path == "/api/plugins/check" and method == "POST":
        kinds = [b["kind"]] if b.get("kind") in plugins.KINDS else list(plugins.KINDS)
        if isinstance(b.get("kinds"), list):   # окно подтверждения сводки: только выбранные источники
            kinds = [k for k in b["kinds"] if k in plugins.KINDS] or kinds

        def go(job: Job):
            cfg = app.cfg()
            st = app.store()
            try:
                if not plugins.available(cfg):
                    raise UserError("Maverick не настроен — шаг «Модель» → Maverick")
                out = {}
                for k in kinds:
                    if not plugins.enabled(cfg, k):
                        job.add(f"{plugins.TITLES[k]}: выключен в настройках")
                        continue
                    out[k] = plugins.check(cfg, st, k, force=True, progress=job.add)
                return {"status": plugins.statuses(cfg, st), "checked": out}
            finally:
                st.close()
        return {"job": app.start_job("plugins", go).id}
    if path == "/api/plugins/itsm" and method == "POST":  # карточка по номеру — из кеша или через плагин
        nums = [str(x) for x in b.get("numbers") or [] if str(x).strip()][:20]
        if not nums:
            raise UserError("не указан номер")

        def go(job: Job):
            cfg = app.cfg()
            st = app.store()
            try:
                from ..assistant import _ext_view
                from ..llm import LLMError
                try:
                    items = plugins.itsm_lookup(cfg, st, nums, progress=job.add)
                except plugins.PluginOffline as e:
                    raise UserError(str(e)) from None
                except (LLMError, OSError) as e:
                    raise UserError(f"Maverick не ответил: {str(e)[:200]}") from None
                return {"items": [_ext_view(x) | {"found": x["found"], "data": x["data"]} for x in items]}
            finally:
                st.close()
        return {"job": app.start_job("plugins", go).id}
    if path == "/api/plugins/saved":
        st = app.store()
        try:
            from ..assistant import _ext_view
            src = (q.get("source") or [None])[0]
            terms = [t for t in ((q.get("q") or [""])[0]).split() if t]
            if terms:
                items = plugins.search_items(st, terms, source=src, limit=100)
            else:
                plugins.ensure(st)
                rows = st.db.execute("SELECT * FROM ext_items WHERE found=1" + (" AND source=?" if src else "")
                                     + " ORDER BY fetched_at DESC LIMIT 100", ((src,) if src else ())).fetchall()
                items = [plugins.item_view(r) for r in rows]
            return {"items": [_ext_view(x) | {"fetched_at": x["fetched_at"]} for x in items]}
        finally:
            st.close()
    return None


def _save_sources(app, b: dict) -> dict:
    from .server import UserError
    raw = app.raw()
    pc = raw.setdefault("plugins", {})
    for k in plugins.KINDS:
        v = (b.get("settings") or {}).get(k)
        if not isinstance(v, dict):
            continue
        cur = pc.setdefault(k, {})
        cur["enabled"] = bool(v.get("enabled", True))
        for f in ("name", "hint", "url"):
            if f in v:
                cur[f] = str(v.get(f) or "").strip()[:500]
        if "body" in v:
            t = str(v.get("body") or "").strip()
            if not t:
                cur.pop("body", None)
            else:
                body = parse_body(t)
                if body is None:
                    raise UserError(f"{plugins.TITLES[k]}: «поля запроса» — не JSON. Пример: {{\"plugins\": [\"outlook\"]}} "
                                    "или plugins: outlook") from None
                if not isinstance(body, dict):
                    raise UserError(f"{plugins.TITLES[k]}: «поля запроса» должны быть объектом JSON {{…}}")
                cur["body"] = body
    if "web_url" in b:
        u = str(b.get("web_url") or "").strip()
        if u and not u.startswith(("http://", "https://")):
            raise UserError("адрес чата Maverick — с http:// или https://")
        pc["web_url"] = u
    if b.get("check_minutes"):
        pc["check_minutes"] = max(5, min(int(b["check_minutes"]), 720))
    if isinstance(b.get("digest_sources"), dict):
        dg = raw.setdefault("digest", {})
        dg["sources"] = {k: bool(b["digest_sources"].get(k)) for k in plugins.KINDS}
    if "cross_private" in b:
        raw.setdefault("digest", {})["cross_private"] = bool(b["cross_private"])
    if isinstance(b.get("glossary"), dict):
        g = raw.setdefault("glossary", {})
        for f in ("auto_collect", "auto_process", "use_wiki"):
            if f in b["glossary"]:
                g[f] = bool(b["glossary"][f])
        if isinstance(b["glossary"].get("sources"), list):
            g["sources"] = [k for k in b["glossary"]["sources"] if k in plugins.KINDS]
    if isinstance(b.get("describe_sources"), list):
        raw.setdefault("describe", {})["sources"] = [k for k in b["describe_sources"] if k in plugins.KINDS]
    if "lookup_people" in b:
        raw.setdefault("privacy", {})["lookup_people"] = bool(b["lookup_people"])
    if isinstance(b.get("images"), dict):
        im = raw.setdefault("plugins", {}).setdefault("images", {})
        for f in ("enabled", "describe"):
            if f in b["images"]:
                im[f] = bool(b["images"][f])
    if isinstance(b.get("patterns"), dict):
        import re
        pats = raw.setdefault("entities", {}).setdefault("patterns", {})
        for k in ("incident", "problem", "change", "task"):
            t = str(b["patterns"].get(k) or "").strip()
            if not t:
                continue
            try:
                re.compile(t)
            except re.error as e:
                raise UserError(f"шаблон номера «{k}»: {e}") from None
            pats[k] = t
    app.save(raw)
    return {"saved": True}


def parse_body(t: str):
    """«Поля запроса API»: объект JSON — как есть; иначе пары «ключ: значение» (через запятую, точку с запятой
    или с новой строки; значение — JSON, если читается, а «a, b» после ключа-списка — список строк). None — не понять."""
    import re
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    out: dict = {}
    for part in re.split(r"[;\n]+", t):
        part = part.strip().strip(",")
        if not part:
            continue
        m = re.match(r"^[\"']?([\w.\-]+)[\"']?\s*[:=]\s*(.+)$", part)
        if not m:
            return None
        key, val = m.group(1), m.group(2).strip()
        try:
            out[key] = json.loads(val)
        except ValueError:
            items = [x.strip().strip("\"'") for x in val.strip("[]").split(",") if x.strip()]
            out[key] = items if (len(items) > 1 or key.endswith("s") or val.startswith("[")) else (items[0] if items else "")
    return out or None
