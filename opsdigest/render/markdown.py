"""Markdown-версия сводки для отправки в Confluence, почту или заметки."""
from __future__ import annotations

from . import TYPE_RU, PRIO_RU, PERIOD_RU


def _images_md(imgs: list[dict] | None) -> list[str]:
    if not imgs:
        return []
    return ["   Изображения: " + "; ".join(f"[#{x['msg_id']}]({x['src']}) {x.get('summary') or ''}".strip()
                                         for x in imgs)]


def render_markdown(d: dict) -> str:
    if (d.get("meta") or {}).get("mode") == "source":
        from .source_text import render_source_markdown
        return render_source_markdown(d)
    meta, ov = d["meta"], d["overview"]
    
    # Заголовок
    out = [f"# Сводка за {meta.get('period_label') or PERIOD_RU.get(meta['period'], meta['period'])}",
           f"_{meta['period_text']}_ · чатов: {meta['chats']} · сообщений: {meta['messages']} · модель: {meta['provider']}",
           ""]
    
    # Заголовок сводки
    if ov.get("headline"):
        out.append(ov["headline"])
        out.append("")
    if meta.get("extra_prompt"):
        out += [f"_Дополнительные указания: {meta['extra_prompt']}_", ""]
    if ov.get("changes"):
        ru = {"new": "новое", "updated": "изменилось", "resolved": "закрыто"}
        prev = ((meta.get("history") or {}).get("prev") or {}).get("text")
        out.append("## Что изменилось с прошлой сводки" + (f" ({prev})" if prev else ""))
        out += [f"- **{ru.get(x.get('kind'), x.get('kind'))}:** {x['title']}" + (f" — {x['details']}" if x.get("details") else "")
                for x in ov["changes"]]
        out.append("")
    
    cr = d.get("cross") or {}
    if cr:
        ru = {"mail": "почта", "itsm": "ITSM", "wiki": "вики", "rooms": "X5 Rooms", "tracker": "трекер", "meet": "встречи"}
        out.append("## Общая картина: " + " + ".join(["Telegram"] + [ru.get(k, k) for k in cr.get("sources") or []]))
        for k, v in (cr.get("offline") or {}).items():
            out.append(f"> ⚠ {k}: плагин Maverick не подключён — переподключите его в чате Maverick. {v.get('detail', '')[:200]}")
        if cr.get("headline"):
            out.append(cr["headline"])
        for t in cr.get("threads") or []:
            src = ", ".join(t.get("sources") or [])
            out.append(f"- **{t['title']}** ({src}{'; ' + t['status'] if t.get('status') else ''}) — {t.get('summary', '')}"
                       + (f" _Расхождение: {t['conflicts']}_" if t.get("conflicts") else "")
                       + (f" **Что сделать:** {t['next']}" if t.get("next") else ""))
        for x in cr.get("only_other") or []:
            out.append(f"- только в {ru.get(x['source'], x['source'])}: {((x.get('info') or {}).get('title')) or x['ref']}"
                       + (f" — {x['why']}" if x.get("why") else ""))
        for m in cr.get("meet") or []:
            if m["data"].get("to_me"):
                out.append(f"- встреча «{m['data'].get('title')}» ({m['data'].get('date', '')}): вам — {m['data']['to_me']}")
        for a in cr.get("actions") or []:
            out.append(f"- [ ] {a['title']}" + (f" — {a['why']}" if a.get("why") else ""))
        out.append("")

    # Нужна реакция
    if ov.get("top"):
        out.append("## Нужна реакция")
        for i, t in enumerate(ov["top"], 1):
            links = []
            for link in t.get("links", []):
                if link.get("url"):
                    links.append(f"[#{link['id']}]({link['url']})")
                else:
                    links.append(f"#{link['id']}")
            
            chat_title = t.get("chat_title", "")
            link_str = " ".join(links) if links else ""
            
            out.append(f"{i}. **{t['title']}** — {chat_title} {link_str}")
            
            if t.get("why"):
                out.append(f"   {t['why']}")
            out += _images_md(t.get("images"))

            if t.get("draft"):
                draft_lines = t["draft"].splitlines()
                out.append("   > " + draft_lines[0])
                for line in draft_lines[1:]:
                    out.append("   > " + line)
        out.append("")
    
    # Ведут другие
    if ov.get("control"):
        out.append("## Ведут другие — проследить")
        for t in ov["control"]:
            links = " ".join(f"[#{l['id']}]({l['url']})" if l.get("url") else f"#{l['id']}" for l in t.get("links", []))
            who = f" (ведёт: {t['who']})" if t.get("who") else ""
            out.append(f"- **{t['title']}**{who} — {t.get('chat_title', '')} {links}".rstrip())
            if t.get("why"):
                out.append(f"   {t['why']}")
        out.append("")

    # Аварии и инциденты
    if ov.get("incidents"):
        out.append("## Аварии и инциденты")
        for x in ov["incidents"]:
            status = f" — {x['status']}" if x.get("status") else ""
            chat_titles = ", ".join(x.get("chat_titles", []))
            details = f"   {x['details']}" if x.get("details") else ""
            
            out.append(f"- **{x['title']}**{status} ({chat_titles})")
            if details:
                out.append(details)
        out.append("")
    
    # По компетенциям
    if ov.get("competences"):
        out.append("## По компетенциям")
        # Проверяем тип данных: может быть словарём или списком
        competences = ov["competences"]
        if isinstance(competences, dict):
            for name, points in competences.items():
                out.append(f"### {name}")
                for point in points:
                    out.append(f"- {point}")
        elif isinstance(competences, list):
            # Если это список, то каждый элемент — словарь с ключами 'name' и 'points'
            for item in competences:
                name = item.get('name', '')
                points = item.get('points', [])
                out.append(f"### {name}")
                for point in points:
                    out.append(f"- {point}")
        out.append("")
    
    # Что уточнить
    if ov.get("clarify"):
        out.append("## Что уточнить")
        for x in ov["clarify"]:
            out.append(f"- {x}")
        out.append("")
    
    # Полезно знать
    if ov.get("fyi"):
        out.append("## Полезно знать")
        for x in ov["fyi"]:
            out.append(f"- {x}")
        out.append("")
    
    # По чатам
    if d.get("chats"):
        out.append("## По чатам")
        for c in d["chats"]:
            out.append(f"### {c['title']}")
            
            msg_count = c.get("count", 0)
            priority = PRIO_RU.get(c.get('priority'), c.get('priority', 'не указан'))
            out.append(f"_{msg_count} сообщ. · приоритет {priority}_")
            out.append("")
            
            if c.get("summary"):
                out.append(c["summary"])
                out.append("")
                
            for item in c.get("items", []):
                links = []
                for link in item.get("links", []):
                    if link.get("url"):
                        links.append(f"[#{link['id']}]({link['url']})")
                    else:
                        links.append(f"#{link['id']}")
                
                link_str = " ".join(links) if links else ""
                
                out.append(f"- [{TYPE_RU.get(item['type'], item['type'])}] **{item['title']}** {link_str}")
                out += _images_md(item.get("images"))

                if item.get("draft"):
                    draft_lines = item["draft"].splitlines()
                    out.append("   > " + draft_lines[0])
                    for line in draft_lines[1:]:
                        out.append("   > " + line)
            out.append("")
    
    # Убираем пустую строку в конце
    if out and out[-1] == "":
        out.pop()
        
    return "\n".join(out) + "\n"
