"""Глоссарий в окне: список и поиск, статья, вопросы пользователю и ответы, пополнение из чатов (фоновая задача
«glossary»), поиск термина в вики через Maverick, предложение статьи по переписке, выгрузка и загрузка JSON."""
from __future__ import annotations

import json

from .. import glossary as G


def _store(h):
    return h.app.store()


def route(h, method: str, path: str, b: dict, q: dict):  # noqa: C901 — таблица маршрутов
    from .server import Job, UserError
    app = h.app
    qv = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
    if path == "/api/glossary":
        st = _store(h)
        try:
            g = G.Glossary(st)
            g.seed()
            terms = g.all(include_hidden=qv("hidden") == "1")
            qs = g.questions()
            ids = {r[0] for e in terms for r in e["refs"]} | {r[0] for x in qs for r in x["refs"] if r}
            titles = {}
            for cid in ids:
                c = st.get_chat(int(cid)) if str(cid).lstrip("-").isdigit() else None
                if c:
                    titles[str(cid)] = c.title
            return {"terms": terms, "counts": g.count(), "questions": qs, "kinds": G.KINDS, "titles": titles}
        finally:
            st.close()
    if path == "/api/glossary/queue":  # очередь терминов: собраны автоматически, ждут обработки
        from .. import glossary_auto as GA
        st = _store(h)
        try:
            status = qv("status") or "pending"
            return {"items": GA.queue(st, status, int(qv("limit") or 300), qv("q") or ""), "counts": GA.counts(st)}
        finally:
            st.close()
    if path == "/api/glossary/term":
        st = _store(h)
        try:
            e = G.Glossary(st).get(qv("id") or qv("term") or "")
            if e is None:
                raise UserError("термин не найден")
            return {"term": e}
        finally:
            st.close()
    if path == "/api/glossary/images":  # подбор иллюстраций к термину + уже прикреплённые
        st = _store(h)
        try:
            g = G.Glossary(st)
            e = g.get(qv("id") or "")
            if e is None:
                raise UserError("термин не найден")
            att = []
            for mid in e["images"]:
                x = st.get_media(mid)
                if x:
                    att.append({"media": mid, "chat": x["chat_id"], "msg": x["msg_id"], "summary": x.get("summary") or "",
                                "chat_title": x.get("chat_title") or ""})
            cand = [c for c in g.images_for(e) if c["media"] not in e["images"]]
            return {"attached": att, "candidates": cand}
        finally:
            st.close()
    if path == "/api/glossary/for_media":
        st = _store(h)
        try:
            mid = int(qv("id") or 0)
            return {"terms": G.Glossary(st).for_media(mid)}
        finally:
            st.close()
    if path == "/api/glossary/export":
        st = _store(h)
        try:
            return {"data": G.Glossary(st).export()}
        finally:
            st.close()
    if method != "POST":
        return None
    if path == "/api/glossary/save":
        st = _store(h)
        try:
            return {"term": G.Glossary(st).save(b.get("term") or {}, source="user")}
        finally:
            st.close()
    if path == "/api/glossary/delete":
        st = _store(h)
        try:
            G.Glossary(st).delete(int(b["id"]))
            return {}
        finally:
            st.close()
    if path == "/api/glossary/import":
        data = b.get("data")
        if isinstance(data, str):
            data = json.loads(data)
        st = _store(h)
        try:
            return G.Glossary(st).import_(data or {})
        finally:
            st.close()
    if path == "/api/glossary/ask":  # свой вопрос в список (например, «что такое X» из мессенджера)
        st = _store(h)
        try:
            return {"id": G.Glossary(st).ask(b.get("term") or "", b.get("question") or "", b.get("options"),
                                             b.get("context") or "", b.get("refs"))}
        finally:
            st.close()
    if path == "/api/glossary/dismiss":
        st = _store(h)
        try:
            G.Glossary(st).close_question(int(b["id"]), None, "dismissed")
            return {}
        finally:
            st.close()

    def llm_for(cfg, provider=None):
        from ..llm import make_client
        try:
            name, prof = cfg.llm_profile(provider or None)
        except KeyError as e:
            raise UserError(str(e)) from None
        return make_client(name, prof)

    if path == "/api/glossary/answer":
        qid, answer = int(b["id"]), str(b.get("answer") or "").strip()
        if not answer:
            raise UserError("напишите ответ")

        def go(job: Job):
            cfg = app.cfg()
            st = app.store()
            try:
                llm = None if b.get("plain") else llm_for(cfg, b.get("provider"))
                job.add("Уточняю статью глоссария…")
                return G.answer_question(cfg, st, llm, qid, answer)
            finally:
                st.close()
        return {"job": app.start_job("gl", go).id}
    if path == "/api/glossary/scan":
        if app.running("glossary"):
            raise UserError("глоссарий уже пополняется — дождитесь окончания или остановите")

        def go(job: Job):
            cfg = app.cfg()
            st = app.store()
            try:
                ids = [int(x) for x in b.get("chats") or [] if str(x).lstrip("-").isdigit()]
                last = {r[0]: r[1] for r in st.db.execute("SELECT chat_id, last_msg FROM gl_scan").fetchall()} \
                    if st.db.execute("SELECT name FROM sqlite_master WHERE name='gl_scan'").fetchone() else {}
                if ids and b.get("_resume"):  # продолжение после перезапуска: уже прочитанные до конца — пропустить
                    mx = {r[0]: r[1] for r in st.db.execute(
                        f"SELECT chat_id, MAX(msg_id) FROM messages WHERE chat_id IN ({','.join('?' * len(ids))}) "
                        "GROUP BY chat_id", ids).fetchall()}
                    left = [i for i in ids if (mx.get(i) or 0) > last.get(i, 0)]
                    if len(left) < len(ids):
                        job.add(f"Уже прочитано раньше: {len(ids) - len(left)} из {len(ids)} чатов — продолжаю с остальных.")
                    if not left:
                        return {"chats": 0, "message": "все выбранные чаты уже прочитаны"}
                    ids = left
                if not ids:  # все включённые чаты, где есть новое с прошлого прохода, — сначала самые активные
                    rows = st.db.execute(
                        """SELECT c.chat_id, MAX(m.msg_id) AS mx, COUNT(*) AS n FROM chats c JOIN messages m
                           ON m.chat_id=c.chat_id WHERE COALESCE(c.priority,'normal')!='ignore'
                           AND c.kind IN ('group','channel','private') GROUP BY c.chat_id ORDER BY MAX(m.date) DESC""").fetchall()
                    ids = [r["chat_id"] for r in rows if r["mx"] > last.get(r["chat_id"], 0)][:int(b.get("limit") or 40)]
                if not ids:
                    return {"chats": 0, "message": "нового с прошлого прохода нет"}
                ex = G.Extractor(cfg, st, llm_for(cfg, b.get("provider")), progress=job.add,
                                 cancel=lambda: job.cancelled)
                job.add(f"Читаю чатов: {len(ids)} (модель: {ex.llm.label}). Новые термины — черновики, неясное — вопросы вам.")
                res = ex.run(ids, fresh=bool(b.get("fresh")))
                job.add(f"Готово: новых терминов {res['new']}, уточнено {res['updated']}, вопросов {res['questions']}.")
                return res
            finally:
                st.close()
        return {"job": app.start_job("glossary", go, task=b).id}
    if path == "/api/glossary/queue/set" and method == "POST":  # «не термин» / вернуть в очередь
        from .. import glossary_auto as GA
        st = _store(h)
        try:
            keys = [str(k) for k in (b.get("keys") or [b.get("key")]) if k]
            status = b.get("status") if b.get("status") in ("pending", "ignored", "done") else "ignored"
            for k in keys:
                GA.set_status(st, k, status, "вручную" if status != "pending" else "")
            return {"counts": GA.counts(st)}
        finally:
            st.close()
    if path == "/api/glossary/process" and method == "POST":  # обработать очередь моделью (долгая задача)
        if app.running("glossary_auto"):
            raise UserError("очередь глоссария уже обрабатывается")

        def go(job: Job):
            from .. import glossary_auto as GA
            cfg = app.cfg()
            st = app.store()
            try:
                llm = llm_for(cfg, b.get("provider"))
                keys = [str(k) for k in b.get("keys") or [] if k]
                job.add(f"Обрабатываю очередь терминов (модель: {llm.label}); ясное — статьи-черновики, неясное — вопросы вам.")
                return GA.process(cfg, st, llm, limit=int(b.get("limit") or 30), min_count=int(b.get("min_count") or 1),
                                  keys=keys or None, progress=job.add, cancel=lambda: job.cancelled)
            finally:
                st.close()
        return {"job": app.start_job("glossary_auto", go, task=b).id}
    if path in ("/api/glossary/wiki", "/api/glossary/suggest"):
        term = str(b.get("term") or "").strip()
        if not term:
            raise UserError("не указан термин")

        def go(job: Job):
            cfg = app.cfg()
            st = app.store()
            try:
                if path.endswith("/wiki"):
                    from ..messenger import ai
                    from .. import plugins
                    if plugins.enabled(cfg, "wiki"):
                        st_ = plugins.check(cfg, st, "wiki", progress=job.add)
                        if st_.get("state") != "ok":
                            raise UserError(str(plugins.PluginOffline("wiki", st_)))
                    job.add("Спрашиваю Maverick (Confluence, ITSM)…")
                    try:
                        r = ai.maverick_lookup(cfg, st, term, question=(
                            f"Что такое «{term}» в нашей компании? Найди в Confluence (вики), ITSM и других доступных "
                            "источниках: определение, к какой системе или процессу относится, кто отвечает (команда), "
                            "как выглядят номера, если это сущность, ссылки на статьи. Если значений несколько — все."))
                    except ValueError as e:
                        raise UserError(str(e)) from None
                    return {"text": r["text"], "model": r.get("model")}
                return suggest(cfg, st, llm_for(cfg, b.get("provider")), term, job.add)
            finally:
                st.close()
        return {"job": app.start_job("gl", go).id}
    return None


def suggest(cfg, store, llm, term: str, progress) -> dict:
    """Статья по переписке: где встречается термин (поиск по базе) → модель предлагает определение и значения."""
    from ..ask import fts_query
    from ..llm.base import extract_json
    from .. import privacy
    hits = store.search(fts_query(term) or f'"{term}"', limit=30, include_excluded=True)
    if not hits:
        return {"entry": {"term": term}, "note": "в переписке (в базе) термин не встречается"}
    lines = [f"«{t}» #{m.msg_id} {m.sender}: {m.text[:300]}" for m, t in hits]
    privacy.attach(llm, cfg, store)
    progress(f"Найдено упоминаний: {len(hits)}; предлагаю статью ({llm.label})…")
    g = G.Glossary(store)
    user = (f"Термин: «{term}». Где он встречается в рабочей переписке:\n" + "\n".join(lines) + "\n\n" +
            (g.block("\n".join(lines), 1500) or "") +
            "\n\nПредложи статью глоссария. Строго JSON: {\"term\", \"kind\", \"aliases\": [], \"summary\", "
            "\"senses\": [{\"meaning\", \"how_to_tell\", \"id_format\", \"owner\"}], \"owner\", \"questions\": "
            "[\"что неясно — спросить пользователя\"]}. Ничего не выдумывай.")
    data = extract_json(llm.complete(G.EXTRACT_RULES, user, max_tokens=1200))
    if not isinstance(data, dict):
        raise ValueError("модель не вернула статью")
    data["term"] = data.get("term") or term
    return {"entry": data, "found": len(hits), "refs": [[m.chat_id, m.msg_id] for m, _ in hits[:10]]}
