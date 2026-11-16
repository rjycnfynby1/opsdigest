"""Короткая сводка для отправки в Telegram (parse_mode=html). Черновики — в <code>: тап по ним копирует текст."""
from __future__ import annotations

from html import escape

DOT = {"high": "🔴", "medium": "🟠", "low": "⚪"}


def _e(s) -> str:
    return escape(str(s or ""), quote=False)


def render_telegram(d: dict, limit: int = 3800) -> str:
    if (d.get("meta") or {}).get("mode") == "source":
        from .source_text import render_source_telegram
        return render_source_telegram(d, limit)
    from . import PERIOD_RU
    meta, ov = d["meta"], d["overview"]
    out = [f"<b>📋 Сводка за {_e(meta.get('period_label') or PERIOD_RU.get(meta['period'], meta['period']))}</b>",
           f"<i>{_e(meta['period_text'])}</i>", ""]
    if ov.get("headline"):
        out += [_e(ov["headline"]), ""]
    if ov.get("changes"):
        ic = {"new": "🆕", "updated": "🔄", "resolved": "✅"}
        out.append("<b>Изменилось с прошлой сводки:</b>")
        out += [f"{ic.get(x.get('kind'), '•')} {_e(x['title'])}" for x in ov["changes"][:8]]
        out.append("")
    if ov.get("top"):
        out.append("<b>Нужна реакция:</b>")
        for i, t in enumerate(ov["top"], 1):
            link = next((l["url"] for l in t.get("links", []) if l.get("url")), None)
            chat = _e(t.get("chat_title", ""))
            chat = f'<a href="{escape(link)}">{chat}</a>' if link else chat
            out.append(f"{i}. {DOT.get(t.get('priority'), '🟠')} <b>{_e(t['title'])}</b> — {chat}")
            if t.get("why"):
                out.append(f"   {_e(str(t['why'])[:300])}")
            if t.get("draft"):
                out.append(f"   ✍️ <code>{_e(str(t['draft'])[:500])}</code>")
        out.append("")
    if ov.get("control"):
        out.append("<b>Ведут другие — проследить:</b>")
        for t in ov["control"][:6]:
            who = f" — ведёт {_e(t['who'])}" if t.get("who") else ""
            out.append(f"• {_e(t['title'])}{who} <i>({_e(t.get('chat_title', ''))})</i>")
        out.append("")
    if ov.get("incidents"):
        out.append("<b>Аварии и инциденты:</b>")
        for x in ov["incidents"]:
            st = f" — {_e(x['status'])}" if x.get("status") else ""
            chats = ", ".join(x.get("chat_titles") or [])
            out.append(f"• {_e(x['title'])}{st}" + (f" <i>({_e(chats)})</i>" if chats else ""))
        out.append("")
    if ov.get("clarify"):
        out.append("<b>Уточнить:</b>")
        out += [f"• {_e(x)}" for x in ov["clarify"]]
        out.append("")
    out.append(f"<i>Чатов: {meta['chats']}, сообщений: {meta['messages']} · {_e(meta['provider'])}</i>")
    if meta.get("errors"):
        out.append(f"<i>⚠️ Ошибок при обработке: {len(meta['errors'])} — см. отчёт</i>")
    text = "\n".join(out)
    if len(text) > limit:
        cut = text[:limit].rsplit("\n", 1)[0]
        text = cut + "\n…\n<i>Полностью — в отчёте.</i>"
    return text
