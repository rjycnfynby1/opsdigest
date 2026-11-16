"""Раздел «Ассистент» в окне: беседы с историей, ответ по всем источникам (фоновая задача), развернуть вопрос из
мессенджера в беседу."""
from __future__ import annotations

from .. import assistant as A


def route(h, method: str, path: str, b: dict, q: dict):
    from .server import Job, UserError
    app = h.app
    qv = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
    if path == "/api/asst/threads":
        st = app.store()
        try:
            return {"threads": A.threads(st, qv("q") or ""), "kinds": A.KIND_RU}
        finally:
            st.close()
    if path == "/api/asst/thread":
        st = app.store()
        try:
            t = A.thread(st, int(qv("id") or 0))
            if t is None:
                raise UserError("беседа не найдена")
            chat = t["meta"].get("chat")
            if chat is not None:
                c = st.get_chat(int(chat))
                t["meta"]["chat_title"] = c.title if c else str(chat)
            t["files"] = A.files_of(st, t["id"])
            fid = t["meta"].get("feedback")
            if fid:
                from .. import skill
                fb = skill.feedback_get(st, int(fid))
                t["feedback"] = {"status": fb["status"], "target": fb["target"]} if fb else None
            return {"thread": t}
        finally:
            st.close()
    if method != "POST":
        return None
    if path == "/api/asst/new":
        meta = {k: b[k] for k in ("chat", "ids", "quote", "topic", "period", "sources") if b.get(k) not in (None, "", [])}
        st = app.store()
        try:
            kind = b.get("kind") if b.get("kind") in A.KIND_RU else ("selection" if meta.get("chat") is not None else "free")
            tid = A.create(st, str(b.get("title") or "Новая беседа"), kind, meta)
            return {"id": tid}
        finally:
            st.close()
    if path == "/api/asst/update":
        st = app.store()
        try:
            A.update(st, int(b["id"]), title=b.get("title"), pinned=b.get("pinned") if "pinned" in b else None)
            return {"ok": True}
        finally:
            st.close()
    if path == "/api/asst/delete":
        st = app.store()
        try:
            A.delete(st, int(b["id"]))
            return {"ok": True}
        finally:
            st.close()
    if path == "/api/asst/ask":
        import copy
        question = str(b.get("question") or "").strip()
        files = [int(x) for x in b.get("files") or [] if str(x).isdigit()]
        if not question and not files:
            raise UserError("введите вопрос")
        tid = int(b.get("thread") or 0)
        if tid:  # ответ на уточняющий вопрос по отзыву — в разбор отзыва
            from .. import skill
            st = app.store()
            try:
                t = A.thread(st, tid)
                fid = (t or {}).get("meta", {}).get("feedback")
                fb = skill.feedback_get(st, int(fid)) if fid else None
            finally:
                st.close()
            if fb and fb["status"] == "question" and question:
                from .skill_api import _feedback_job
                return {"job": _feedback_job(h, int(fid), question, b.get("provider")).id}

        def go(job: Job):
            from ..llm import LLMError
            from ..messenger.ai import make_llm
            cfg = app.cfg()
            st = app.store()
            try:
                t = tid or A.create(st, (question or "Вложения")[:80], "maverick" if b.get("maverick") else "free", {})
                job.info["thread"] = t
                try:
                    llm = None if b.get("maverick") else make_llm(cfg, str(b.get("provider") or "") or None)
                    return A.answer(cfg, st, llm, t, question, opts={
                        "sources": b.get("sources") or {}, "period": b.get("period"), "since": b.get("since"),
                        "files": files, "maverick": bool(b.get("maverick")), "raw": copy.deepcopy(app.raw()),
                        "answers": b.get("answers") if isinstance(b.get("answers"), dict) else None},
                        progress=job.add)
                except (ValueError, LLMError, KeyError) as e:
                    if not job.cancelled:   # остановили кнопкой — заметку «остановлено» пишет остановка
                        A.add(st, t, "note", f"✗ {e}")
                    raise UserError(str(e)) from None
            finally:
                st.close()
        job = app.start_job("asst", go)
        job.title = "Ассистент" + (" (Maverick как на портале)" if b.get("maverick") else "") + \
            (f": «{question[:60]}{'…' if len(question) > 60 else ''}»" if question else "")
        job.info = {"thread": tid or None}
        return {"job": job.id}
    return None
