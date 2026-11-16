"""Сводка «От источника» в Markdown (Confluence, почта) и коротким текстом для «Избранного» Telegram."""
from __future__ import annotations

from html import escape

EXP_RU = {"support": "ждут от нас", "dev": "ждут разработку", "user": "ждут пользователя", "vendor": "ждут подрядчика",
          "nobody": "зависло", "unclear": "неясно, кто"}
ACT_RU = {"yes": "актуально", "no": "можно закрывать", "doubt": "уточнить"}
SRC = {"itsm": "ITSM", "tracker": "Трекер", "mail": "Почта", "rooms": "X5 Rooms", "meet": "Встречи", "telegram": "Telegram"}


def _e(s) -> str:
    return escape(str(s or ""), quote=False)


def render_source_markdown(d: dict) -> str:
    meta, src = d["meta"], d["source"]
    st = src.get("stats") or {}
    items = src.get("items") or []
    out = [f"# Разбор очереди: {src.get('query_text', '')}",
           f"_{meta.get('period_text', '')}_ · начато с: {SRC.get(src['start'], src['start'])} · записей: {len(items)}"
           + (f" из {src['total']}" if src.get("total") and src["total"] != len(items) else "")
           + f" · модель: {meta.get('provider', '')}", ""]
    if meta.get("extra_prompt"):
        out += [f"_Указания: {meta['extra_prompt']}_", ""]
    for k, v in (src.get("offline") or {}).items():
        out.append(f"> ⚠ {SRC.get(k, k)}: {'выключен в настройках' if v.get('state') == 'disabled' else 'плагин Maverick не подключён'}")
    if src.get("headline"):
        out += [src["headline"], ""]
    if src.get("answer"):
        out += ["## Ответ на указания", "", src["answer"], ""]
    if items:
        out += ["## Цифры", "", f"- записей: {st.get('total', 0)}"]
        if st.get("waiting") is not None:
            out.append(f"- в ожидании: {st['waiting']}")
        out += [f"- без движения {st.get('stale_days')}+ дн.: {st.get('stale', 0)}; старше {st.get('old_days')} дн.: {st.get('old', 0)}",
                f"- требуют внимания: {st.get('flagged', 0)}; обсуждались в Telegram: {st.get('in_telegram', 0)}; в почте: {st.get('in_mail', 0)}"]
        for key, title in (("by_status", "по статусам"), ("by_waiting", "чего ждут"), ("by_expects", "от кого ждут движения"),
                           ("by_actual", "актуальность"), ("by_problem", "общие проблемы"), ("by_release", "по релизам")):
            if st.get(key):
                out.append(f"- {title}: " + "; ".join(f"{a} — {b}" for a, b in st[key]))
        out.append("")
    if src.get("actions"):
        out += ["## Что сделать", ""] + [f"{i}. **{a['title']}**" + (f" ({', '.join(a['refs'])})" if a.get("refs") else "")
                                          + (f" — {a['why']}" if a.get("why") else "") for i, a in enumerate(src["actions"], 1)] + [""]
    if src.get("groups"):
        out += ["## Общие проблемы и задачи", ""]
        for g in src["groups"]:
            out.append(f"- **{g['title']}** ({', '.join(g.get('refs') or [])}" + (f"; задачи: {', '.join(g['tasks'])}" if g.get("tasks") else "")
                       + ")" + (f" — {g['why']}" if g.get("why") else "") + (f" Статус: {g['status']}." if g.get("status") else "")
                       + (f" Что делать: {g['next']}" if g.get("next") else ""))
        out.append("")
    if src.get("attention"):
        out += ["## Требует внимания", ""] + [f"- **{a['ref']}** {a.get('title', '')}: " + "; ".join(a.get("flags") or [])
                                               for a in src["attention"]] + [""]
    if src.get("close"):
        out += ["## Можно закрыть", ""] + [f"- {c['ref']} — {c.get('why', '')}" for c in src["close"]] + [""]
    if items:
        out += ["## Записи", "", "| Номер | Суть | Статус | Связано | Оценка | Ждут | Что сделать |", "|---|---|---|---|---|---|---|"]
        for it in items:
            v = it.get("verdict") or {}
            ln = ", ".join(f"{x['ref']} ({'закрыта' if x.get('closed') else x.get('status') or '?'})" for x in it.get("links") or [])

            def c(s):
                return str(s or "").replace("|", "/").replace("\n", " ")
            out.append(f"| {c(it['ref'])} | {c(it.get('title'))} | {c(it.get('status'))}"
                       + (f", ожидание: {c(it.get('waiting_reason'))}" if it.get("waiting") else "")
                       + f" | {c(ln)} | {c(ACT_RU.get(v.get('actual'), ''))} | {c(EXP_RU.get(v.get('expects'), ''))}"
                       + (f": {c(v.get('expects_what'))}" if v.get("expects_what") else "") + f" | {c(v.get('next'))} |")
        out.append("")
    sec = [s for s in src.get("secondary") or [] if s.get("found")]
    if sec:
        out += ["## " + ("Задачи трекера" if src["second"] == "tracker" else "Инциденты ITSM"), ""]
        out += [f"- **{s['ref']}** {s.get('title', '')} — {s.get('status', '')}" + (" (закрыта)" if s.get("closed") else "")
                + (f"; релиз {s['release']}" if s.get("release") else "") + f"; нужна: {', '.join(s.get('used_by') or [])}"
                for s in sec] + [""]
    if src.get("clarify"):
        out += ["## Что уточнить", ""] + [f"- {x}" for x in src["clarify"]] + [""]
    if meta.get("errors"):
        out += ["## Ошибки обработки", ""] + [f"- {e}" for e in meta["errors"]] + [""]
    return "\n".join(out)


def render_source_telegram(d: dict, limit: int = 3800) -> str:
    meta, src = d["meta"], d["source"]
    st = src.get("stats") or {}
    items = src.get("items") or []
    out = [f"<b>🎫 Разбор очереди: {_e(src.get('query_text'))}</b>", f"<i>{_e(meta.get('period_text'))}</i>", ""]
    if src.get("headline"):
        out += [_e(src["headline"]), ""]
    if items:
        bits = [f"записей {st.get('total', 0)}"]
        if st.get("waiting") is not None:
            bits.append(f"в ожидании {st['waiting']}")
        exp = dict(st.get("by_expects") or [])
        if exp.get("поддержка (мы)"):
            bits.append(f"ждут от нас {exp['поддержка (мы)']}")
        bits += [f"без движения {st.get('stale_days')}+ дн. — {st.get('stale', 0)}", f"требуют внимания {st.get('flagged', 0)}"]
        out += ["📊 " + _e(", ".join(bits)), ""]
    if src.get("answer"):
        out += ["<b>Ответ на указания:</b>", _e(src["answer"][:800]), ""]
    if src.get("actions"):
        out.append("<b>Что сделать:</b>")
        out += [f"{i}. {_e(a['title'])}" + (f" <i>({_e(', '.join(a['refs'][:6]))})</i>" if a.get("refs") else "")
                for i, a in enumerate(src["actions"][:8], 1)]
        out.append("")
    if src.get("groups"):
        out.append("<b>Общие проблемы:</b>")
        out += [f"• {_e(g['title'])} — {len(g.get('refs') or [])} зап." + (f", {_e(g['status'])}" if g.get("status") else "")
                for g in src["groups"][:6]]
        out.append("")
    if src.get("attention"):
        out.append("<b>Требует внимания:</b>")
        out += [f"• <b>{_e(a['ref'])}</b> {_e('; '.join(a.get('flags') or [])[:200])}" for a in src["attention"][:8]]
        out.append("")
    if src.get("close"):
        out.append("<b>Можно закрыть:</b> " + _e(", ".join(c["ref"] for c in src["close"][:12])))
        out.append("")
    if src.get("offline"):
        out.append("<i>⚠️ Не подключены: " + _e(", ".join(SRC.get(k, k) for k in src["offline"])) + "</i>")
    out.append(f"<i>{_e(meta.get('provider'))}</i>")
    text = "\n".join(out)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…\n<i>Полностью — в отчёте.</i>"
    return text
