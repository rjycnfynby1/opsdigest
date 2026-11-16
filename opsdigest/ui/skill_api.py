"""Навык в окне: правила и поправки, опорные ссылки, отзывы на любой результат, журнал изменений с отменой,
применение действий из ассистента, выгрузка/загрузка навыка (JSON и читаемый SKILL.md)."""
from __future__ import annotations

import json

from .. import skill


def _ctx(app, raw: dict, st):
    return skill.Ctx(raw, st, app.cfg())


def _auto(app) -> str:
    v = str((app.raw().get("skill") or {}).get("auto_apply") or "knowledge")
    return v if v in ("knowledge", "none") else "knowledge"


def _start_describe(h):
    """Запуск ИИ-описания из действия навыка — тем же путём, что кнопка «✦ Описать ИИ» в окне."""
    def go(body: dict):
        res = h._api("POST", "/api/describe", body, {})
        return (res or {}).get("job")
    return go


def apply_plan(h, st, raw: dict, plan: list[dict], idx: list[int] | None, ref: dict) -> list[dict]:
    """Применить ждущие действия плана (idx — какие; None — все ждущие). raw меняется на месте."""
    ctx = _ctx(h.app, raw, st)
    for i, p in enumerate(plan):
        if p.get("state") != "pending" or (idx is not None and i not in idx):
            continue
        try:
            r = skill.apply(ctx, p["action"], source="assistant", ref=ref, start_describe=_start_describe(h))
            p.update(state="done", journal=r.get("journal"), job=r.get("job"), count=r.get("count", p.get("count")))
        except (ValueError, KeyError, TypeError) as e:
            p.update(state="error", error=str(e)[:200])
    return plan


def route(h, method: str, path: str, b: dict, q: dict):
    from .chats_api import _after_save, update
    from .server import Job, UserError
    app = h.app
    qv = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
    if path == "/api/skill" and method == "GET":
        st = app.store()
        try:
            return {"rules": skill.rules(st), "links": skill.links(st), "counts": skill.counts(st),
                    "feedback": skill.feedback_list(st, 60), "journal": skill.journal(st, 60),
                    "scopes": skill.SCOPES, "targets": skill.TARGETS, "auto_apply": _auto(app)}
        finally:
            st.close()
    if path == "/api/skill/export" and method == "GET":
        st = app.store()
        try:
            if qv("format") == "md":
                return {"name": "SKILL.md", "text": skill.skill_md(st)}
            return {"name": "opsdigest-skill.json", "data": skill.export(st)}
        finally:
            st.close()
    if method != "POST":
        return None
    try:
        if path == "/api/skill/rule":
            st = app.store()
            try:
                return {"rule": skill.rule_save(st, b, source="user")}
            finally:
                st.close()
        if path == "/api/skill/rule/delete":
            st = app.store()
            try:
                skill.rule_delete(st, int(b["id"]))
                return {"ok": True}
            finally:
                st.close()
        if path == "/api/skill/link":
            st = app.store()
            try:
                return {"link": skill.link_save(st, b)}
            finally:
                st.close()
        if path == "/api/skill/link/delete":
            st = app.store()
            try:
                skill.link_delete(st, int(b["id"]))
                return {"ok": True}
            finally:
                st.close()
        if path == "/api/skill/settings":
            v = str(b.get("auto_apply") or "knowledge")
            if v not in ("knowledge", "none"):
                raise UserError("auto_apply: knowledge | none")
            raw = app.raw()
            raw.setdefault("skill", {})["auto_apply"] = v
            app.save(raw)
            return {"auto_apply": v}
        if path == "/api/skill/import":
            data = b.get("data")
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict):
                raise UserError("нужен файл навыка (JSON)")
            st = app.store()
            try:
                return skill.import_(st, data)
            finally:
                st.close()
        if path == "/api/skill/undo":
            st = app.store()
            try:
                res = update(app, lambda raw: skill.undo(_ctx(app, raw, st), int(b["journal"])))
            finally:
                st.close()
            _after_save(app)
            return res
        if path == "/api/skill/apply":  # действия из сообщения ассистента («Применить»)
            from .. import assistant as A
            mid = int(b.get("msg") or 0)
            st = app.store()
            try:
                A.ensure(st)
                r = st.db.execute("SELECT thread_id, data FROM asst_msgs WHERE id=?", (mid,)).fetchone()
                if not r:
                    raise UserError("сообщение не найдено")
                data = json.loads(r["data"] or "{}")
                plan = data.get("plan") or []
                idx = [int(i) for i in b["idx"]] if isinstance(b.get("idx"), list) else None
                if b.get("cancel"):
                    for i, p in enumerate(plan):
                        if p.get("state") == "pending" and (idx is None or i in idx):
                            p["state"] = "cancelled"
                else:
                    update(app, lambda raw: apply_plan(h, st, raw, plan, idx, {"thread": r["thread_id"], "msg": mid}))
                data["plan"] = plan
                st.db.execute("UPDATE asst_msgs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), mid))
                st.db.commit()
                fid = data.get("feedback")
                if fid and not any(p.get("state") == "pending" for p in plan):
                    st.db.execute("UPDATE feedback SET status='done' WHERE id=? AND status='pending'", (int(fid),))
                    st.db.commit()
            finally:
                st.close()
            _after_save(app)
            return {"plan": plan}
        if path == "/api/feedback":  # отзыв на любой результат → навык (фоновая задача)
            text = str(b.get("text") or "").strip()
            rating = int(b.get("rating") or 0)
            if not text and not rating:
                raise UserError("напишите, что хорошо или что поправить")
            ref = b.get("ref") if isinstance(b.get("ref"), dict) else {}
            excerpt = str(b.get("excerpt") or "")
            if ref.get("report"):  # отзыв об отчёте: модели — его Markdown-версия (короче HTML, всё содержание)
                excerpt = _report_text(app, str(ref["report"])) or excerpt
            st = app.store()
            try:
                fid = skill.feedback_add(st, str(b.get("target") or "other"), text, rating=rating, excerpt=excerpt, ref=ref)
            finally:
                st.close()
            return {"feedback": fid, "job": _feedback_job(h, fid, None, b.get("provider")).id}
        if path == "/api/feedback/answer":
            ans = str(b.get("answer") or "").strip()
            if not ans:
                raise UserError("пустой ответ")
            return {"job": _feedback_job(h, int(b["id"]), ans, b.get("provider")).id}
    except (KeyError, ValueError) as e:
        raise UserError(str(e)) from None
    return None


def _report_text(app, name: str) -> str:
    import re
    from ..secure import files as sfiles
    if not re.fullmatch(r"[\w.\-]+\.(?:html|md)", name):
        return ""
    md = app.need_ws() / "reports" / re.sub(r"\.html$", ".md", name)
    try:
        return sfiles.read_text(md)[:9000] if md.is_file() else ""
    except OSError:
        return ""


def _feedback_job(h, fid: int, answer: str | None, provider=None):
    import copy
    from .server import Job, UserError
    app = h.app

    def go(job: Job):
        from ..llm import LLMError
        from ..messenger.ai import make_llm
        cfg = app.cfg()
        st = app.store()
        try:
            try:
                llm = make_llm(cfg, str(provider or "") or None)
            except (ValueError, LLMError, KeyError) as e:
                raise UserError(f"модель недоступна: {e}") from None
            auto = _auto(app)
            raw = copy.deepcopy(app.raw())  # только для чтения: изменения чатов и людей ждут «Применить»
            try:
                res = skill.feedback_process(_ctx(app, raw, st), llm, fid, answer=answer, progress=job.add, auto=auto,
                                             start_describe=_start_describe(h))
            except LLMError as e:
                st.db.execute("UPDATE feedback SET status='failed' WHERE id=?", (fid,))
                st.db.commit()
                raise UserError(f"модель не ответила: {str(e)[:200]}") from None
        finally:
            st.close()
        done = [p for p in res.get("plan") or [] if p.get("state") == "done"]
        if res.get("status") == "question":
            job.add("Нужно уточнение — вопрос в ассистенте.")
        else:
            job.add(f"Учтено: изменений {len(done)}" + (", ждут подтверждения: " + str(sum(
                1 for p in res.get('plan') or [] if p.get('state') == 'pending')) if any(
                p.get('state') == 'pending' for p in res.get('plan') or []) else ""))
        return res
    return app.start_job("feedback", go)
