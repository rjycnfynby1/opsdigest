"""HTML-отчёт сводки «От источника» (разбор очереди ITSM или задач трекера) — тот же вид, что у обычной сводки."""
from __future__ import annotations

from .html import CSS, JS, SRC_RU, _e, _links

CSS2 = """
.big{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 8px}
.big div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:110px}
.big b{display:block;font-size:20px;line-height:1.2}.big span{font-size:12px;color:var(--muted)}
.big div.hot b{color:var(--hi)}
.kv td:last-child{text-align:right;white-space:nowrap;color:var(--muted)}
a.ref,span.ref{font-size:12px;padding:1px 7px;border-radius:4px;background:var(--hi-soft);color:var(--hi);text-decoration:none;
white-space:nowrap;font-weight:600}
a.ref.t2,span.ref.t2{background:var(--acc-soft);color:var(--acc)}
.ref.closed{text-decoration:line-through}
.tag.exp{background:var(--md-soft);color:var(--md)}.tag.no{background:var(--acc-soft);color:var(--acc)}
.tag.doubt{background:var(--lo-soft);color:var(--muted)}.tag.wait{background:var(--md-soft);color:var(--md)}
.flags{margin:6px 0 0;padding-left:18px;color:var(--hi);font-size:13px}
.sub2{font-size:12.5px;color:var(--muted);margin-top:6px}
.blk{margin-top:8px;font-size:14px}.blk>b{font-size:12px;color:var(--muted);font-weight:500;display:block}
.journal{font-size:13px;color:var(--muted)}.journal div{padding:2px 0;border-top:1px dashed var(--line)}
.answer{white-space:pre-wrap}
"""

JS2 = r"""
let SF='all';
function sflt(){const q=(document.getElementById('sq').value||'').toLowerCase();
 document.querySelectorAll('details.it').forEach(d=>{let ok=SF==='all'||(' '+d.dataset.f+' ').includes(' '+SF+' ');
  if(q)ok=ok&&d.dataset.text.includes(q);d.style.display=ok?'':'none'})}
function setS(b,f){SF=f;document.querySelectorAll('#items .filters button').forEach(x=>x.classList.toggle('on',x===b));sflt()}
function openS(v){document.querySelectorAll('details.it').forEach(d=>{if(d.style.display!=='none')d.open=v})}
document.addEventListener('click',e=>{const a=e.target.closest('a.ref[href^="#r-"]');if(!a)return;
 const d=document.getElementById(a.getAttribute('href').slice(1));if(d&&d.tagName==='DETAILS'){d.open=true;d.style.display=''}});
"""

EXP_RU = {"support": "ждут от нас", "dev": "ждут разработку", "user": "ждут пользователя", "vendor": "ждут подрядчика",
          "nobody": "зависло", "unclear": "неясно, кто"}
ACT_RU = {"yes": "актуально", "no": "можно закрывать", "doubt": "уточнить"}
ACT_CLS = {"yes": "", "no": "no", "doubt": "doubt"}


def _aid(ref: str) -> str:
    return "r-" + "".join(ch if ch.isalnum() or ch == "-" else "_" for ch in str(ref))


def _ref(ref: str, known: set[str], cls: str = "", closed: bool = False, title: str = "") -> str:
    c = f"ref {cls}" + (" closed" if closed else "")
    t = f" title='{_e(title)}'" if title else ""
    if ref in known:
        return f"<a class='{c}' href='#{_aid(ref)}'{t}>{_e(ref)}</a>"
    return f"<span class='{c}'{t}>{_e(ref)}</span>"


def _table(rows: list, title: str) -> str:
    if not rows:
        return ""
    return (f"<div class='card'><div class='t'>{_e(title)}</div><table class='kv'>"
            + "".join(f"<tr><td>{_e(a)}</td><td>{_e(b)}</td></tr>" for a, b in rows) + "</table></div>")


def _ext_list(ext: dict) -> str:
    out = []
    ic = {"mail": "✉", "rooms": "💭", "meet": "🎙"}
    for kind, rows in (ext or {}).items():
        for x in rows:
            t = _e(x.get("title") or "")
            if x.get("url"):
                t = f"<a href='{_e(x['url'])}' target='_blank' rel='noopener'>{t}</a>"
            out.append(f"<div>{ic.get(kind, '•')} <b>{t}</b> <small>{_e(x.get('date'))}"
                       + (f" · {_e(x['from'])}" if x.get("from") else "") + f"</small> — {_e(x.get('summary'))}</div>")
    return "".join(out)


def _tg(t: dict | None) -> str:
    if not t or not (t.get("count") or t.get("discussed")):
        return ""
    body = []
    if t.get("summary"):
        body.append(_e(t["summary"]))
    if t.get("latest"):
        body.append(f"<div class='sub2'>Свежее: {_e(t['latest'])}</div>")
    if t.get("waiting"):
        body.append(f"<div class='sub2'>Ждут: {_e(t['waiting'])}</div>")
    if not t.get("discussed"):
        body.append(f"<span class='why'>упоминается {t.get('count')} раз, по сути не обсуждается</span>")
    by_chat: dict[str, list[dict]] = {}
    for ln in t.get("links") or []:
        by_chat.setdefault(ln["chat"], []).append(ln)
    links = " · ".join(_links(v, c, None, v[0].get("chat_id")) for c, v in by_chat.items())
    return f"<div class='blk'><b>Telegram</b>{''.join(body)}<div class='meta'>{links}</div></div>"


def _item(it: dict, src: dict, known: set[str], sec: dict[str, dict]) -> str:
    v = it.get("verdict") or {}
    flags = list(dict.fromkeys((it.get("code_flags") or []) + (it.get("flags") or [])))
    f = ["att" if flags else "", "exp-" + (v.get("expects") or "unclear"), "act-" + (v.get("actual") or "doubt"),
         "wait" if it.get("waiting") else ""]
    text = " ".join([it["ref"], it.get("title") or "", it.get("status") or "", v.get("summary") or "", v.get("problem") or "",
                     " ".join(it.get("linked") or [])]).lower()
    prio = v.get("priority") or "medium"
    tags = [f"<span class='tag'>{_e(it['status'])}</span>" if it.get("status") else ""]
    if it.get("waiting"):
        tags.append(f"<span class='tag wait'>ожидание{': ' + _e(it['waiting_reason'][:60]) if it.get('waiting_reason') else ''}</span>")
    if v.get("actual"):
        tags.append(f"<span class='tag {ACT_CLS.get(v['actual'], '')}'>{_e(ACT_RU.get(v['actual'], v['actual']))}</span>")
    if v.get("expects"):
        tags.append(f"<span class='tag exp'>{_e(EXP_RU.get(v['expects'], v['expects']))}</span>")
    for ln in it.get("links") or []:
        s = sec.get(ln["ref"]) or {}
        tags.append(_ref(ln["ref"], set(), "t2", bool(s.get("closed")), (s.get("status") or "") + " " + (s.get("title") or "")))
    head = (f"<summary><span class='dot {_e(prio)}' style='margin-top:0'></span><span class='ct'><span class='t'>"
            f"{_e(it['ref'])} · {_e(it.get('title') or '')}</span><span class='meta'>{''.join(tags)}"
            + (f" · без движения {it['idle_days']} дн." if (it.get("idle_days") or 0) >= 1 else "") + "</span></span></summary>")
    body = []
    if v.get("summary"):
        body.append(f"<div>{_e(v['summary'])}</div>")
    if v.get("no_model"):
        body.append("<div class='err'>Модель не разобрала эту запись — показано по полям карточки.</div>")
    if v.get("expects_what"):
        body.append(f"<div class='sub2'><b>Ждут:</b> {_e(EXPECTS_FULL.get(v.get('expects'), ''))} — {_e(v['expects_what'])}</div>")
    if v.get("actual_why"):
        body.append(f"<div class='sub2'><b>{_e(ACT_RU.get(v.get('actual'), ''))}:</b> {_e(v['actual_why'])}</div>")
    if v.get("next"):
        body.append(f"<div class='next' style='margin-top:6px'><b>Что сделать:</b> {_e(v['next'])}</div>")
    if flags:
        body.append("<ul class='flags'>" + "".join(f"<li>{_e(x)}</li>" for x in flags) + "</ul>")
    card = [f"тип: {it['type']}" if it.get("type") else "", f"приоритет: {it['priority']}" if it.get("priority") else "",
            f"группа: {it['group']}" if it.get("group") else "", f"сервис: {it['service']}" if it.get("service") else "",
            f"исполнитель: {it['assignee']}" if it.get("assignee") else "", f"открыт {it['opened']}" if it.get("opened") else "",
            f"обновлён {it['updated']}" if it.get("updated") else "", f"срок {it.get('deadline') or it.get('due')}"
            if it.get("deadline") or it.get("due") else "", f"релиз {it['release']}" if it.get("release") else "",
            f"дата релиза {it['release_date']}" if it.get("release_date") else ""]
    if it.get("waiting"):
        card.append("ожидание: " + ", ".join(x for x in (it.get("waiting_reason"), f"от: {it['waiting_for']}" if it.get("waiting_for") else "",
                                                       f"задача {it['waiting_task']}" if it.get("waiting_task") else "",
                                                       f"до {it['waiting_until']}" if it.get("waiting_until") else "") if x))
    url = f" · <a href='{_e(it['url'])}' target='_blank' rel='noopener'>открыть ↗</a>" if it.get("url") else ""
    body.append(f"<div class='meta'>{_e(' · '.join(c for c in card if c))}{url}</div>")
    if it.get("description"):
        body.append(f"<div class='blk'><b>Суть</b>{_e(it['description'])}</div>")
    if it.get("links"):
        rows = []
        for ln in it["links"]:
            s = sec.get(ln["ref"]) or {}
            if not s:
                rows.append(f"<div>{_ref(ln['ref'], known, 't2')} — не проверялось</div>")
                continue
            if not s.get("found"):
                rows.append(f"<div>{_ref(ln['ref'], known, 't2')} — не найдено</div>")
                continue
            bits = [s.get("title"), s.get("status"), "закрыта" if s.get("closed") and src["second"] == "tracker" else "",
                    f"релиз {s['release']}" if s.get("release") else "", f"от {s['release_date']}" if s.get("release_date") else "",
                    f"действовать: {s['waiting_for']}" if s.get("waiting_for") else "", s.get("waiting_what") or "",
                    f"ожидание: {s['waiting_reason']}" if s.get("waiting_reason") else ""]
            last = (s.get("comments") or [{}])[0].get("text") if s.get("comments") else ""
            rows.append(f"<div>{_ref(ln['ref'], known, 't2', bool(s.get('closed')))} {_e(' · '.join(b for b in bits if b))}"
                        + (f"<div class='sub2'>Последний комментарий: {_e(last[:300])}</div>" if last else "") + "</div>")
        title = "Задачи трекера" if src["second"] == "tracker" else "Инциденты ITSM"
        body.append(f"<div class='blk'><b>{title}</b>{''.join(rows)}</div>")
    body.append(_tg(it.get("tg")))
    ext = _ext_list(it.get("ext"))
    if ext:
        body.append(f"<div class='blk'><b>Почта, Rooms, встречи</b>{ext}</div>")
    if it.get("comments"):
        body.append("<details class='blk'><summary><b style='display:inline'>Журнал работ "
                    f"({len(it['comments'])})</b></summary><div class='journal'>" + "".join(
                        f"<div><small>{_e(c.get('date'))} {_e(c.get('author'))}</small> {_e(c.get('text'))}</div>"
                        for c in it["comments"]) + "</div></details>")
    return (f"<details class='chat it' id='{_aid(it['ref'])}' data-f='{_e(' '.join(x for x in f if x))}' "
            f"data-text='{_e(text)}'>{head}<div class='cb'>{''.join(body)}</div></details>")


EXPECTS_FULL = {"support": "от поддержки (от вас)", "dev": "от разработки", "user": "от пользователя",
                "vendor": "от подрядчика / другой группы", "nobody": "ни от кого — зависло", "unclear": "неясно, от кого"}


def render_source_html(d: dict) -> str:
    meta, src = d["meta"], d["source"]
    items, sec_list = src.get("items") or [], src.get("secondary") or []
    known = {it["ref"] for it in items} | {s["ref"] for s in sec_list if s.get("found")}
    sec = {s["ref"]: s for s in sec_list}
    st = src.get("stats") or {}
    title = f"Разбор очереди: {src.get('query_text') or ''}"
    look = src.get("look") or {}
    h: list[str] = [f"<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
                    f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
                    f"<style>{CSS}{CSS2}</style></head><body><div class='wrap'>"]
    off = src.get("offline") or {}
    chips = [f"<span class='chip acc'>Начато с: {_e(SRC_RU.get(src['start'], src['start']))}</span>"]
    if look.get("second") and src["second"] not in off:
        chips.append(f"<span class='chip acc'>{_e(SRC_RU.get(src['second'], src['second']))}</span>")
    for k in ("telegram", "mail", "rooms", "meet"):
        if look.get(k) and k not in off:
            chips.append(f"<span class='chip acc'>{_e(SRC_RU.get(k, 'Telegram' if k == 'telegram' else k))}</span>")
    total = src.get("total")
    chips += [f"<span class='chip'>записей: {len(items)}" + (f" из {total}" if total and total != len(items) else "") + "</span>",
              f"<span class='chip'>модель: {_e(meta.get('provider'))}</span>",
              f"<span class='chip'>собрано: {_e(meta.get('generated_local', ''))}</span>"]
    h.append(f"<header><h1>{_e(title)}</h1><div class='sub'>{_e(meta.get('period_text'))}"
             + (f" · {_e(meta['me'])}" if meta.get("me") else "") + f"</div><div class='chips'>{''.join(chips)}</div>"
             + (f"<div class='extra'>Указания: {_e(meta['extra_prompt'])}</div>" if meta.get("extra_prompt") else "")
             + "</header>")
    for k, v in off.items():
        links = " ".join(f"<a href='{_e(u)}' target='_blank' rel='noopener'>подключить ↗</a>" for u in v.get("links") or [])
        what = "выключен в настройках" if v.get("state") == "disabled" else "плагин Maverick не подключён — переподключите его в чате Maverick"
        h.append(f"<div class='warnbox'>⚠ {_e(SRC_RU.get(k, k))}: {what}. {_e((v.get('detail') or '')[:300])} {links}</div>")
    if src.get("truncated"):
        h.append(f"<div class='warnbox'>Разобраны первые {len(items)} из {total} записей — увеличьте «не больше N записей» "
                 "или сузьте условия.</div>")
    toc = [("ans", "Ответ", 1 if src.get("answer") else 0), ("nums", "Цифры", st.get("total", 0)),
           ("act", "Что сделать", len(src.get("actions") or [])), ("grp", "Группы", len(src.get("groups") or [])),
           ("att", "Требует внимания", len(src.get("attention") or [])), ("cls", "Можно закрыть", len(src.get("close") or [])),
           ("items", "Записи", len(items)), ("sec", "Задачи трекера" if src["second"] == "tracker" else "Инциденты ITSM",
                                             sum(1 for s in sec_list if s.get("found"))),
           ("clar", "Уточнить", len(src.get("clarify") or []))]
    h.append("<nav class='toc'>" + "".join(f"<a href='#{k}'>{v} · {n}</a>" if k != "ans" else f"<a href='#{k}'>{v}</a>"
                                           for k, v, n in toc if n) + "</nav>")
    if src.get("headline"):
        h.append(f"<section><div class='card headline'>{_e(src['headline'])}</div></section>")
    if src.get("answer"):
        h.append(f"<section id='ans'><h2>Ответ на ваши указания</h2><div class='card answer'>{_e(src['answer'])}</div></section>")

    # цифры
    if items:
        big = [("записей", st.get("total"), False)]
        if st.get("waiting") is not None:
            big.append(("в ожидании", st["waiting"], False))
        if st.get("closed") is not None:
            big.append(("закрыто задач", st["closed"], False))
        exp = dict(st.get("by_expects") or [])
        if exp.get("поддержка (мы)"):
            big.append(("ждут от нас", exp["поддержка (мы)"], True))
        big += [(f"без движения {st.get('stale_days')}+ дн.", st.get("stale"), bool(st.get("stale"))),
                (f"старше {st.get('old_days')} дн.", st.get("old"), False),
                ("требуют внимания", st.get("flagged"), bool(st.get("flagged"))),
                ("обсуждались в Telegram", st.get("in_telegram"), False), ("в почте", st.get("in_mail"), False)]
        h.append("<section id='nums'><h2>Цифры</h2><div class='big'>" + "".join(
            f"<div class='{'hot' if hot else ''}'><b>{_e(n if n is not None else 0)}</b><span>{_e(t)}</span></div>"
            for t, n, hot in big) + "</div><div class='grid2'>"
            + _table(st.get("by_status"), "По статусам") + _table(st.get("by_waiting"), "В ожидании — чего ждут")
            + _table(st.get("by_waiting_for"), "В ожидании — от кого (по ITSM)")
            + _table(st.get("by_expects"), "От кого ждут движения (оценка)")
            + _table(st.get("by_actual"), "Актуальность (оценка)") + _table(st.get("by_problem"), "Общие проблемы")
            + _table(st.get("by_release"), "По релизам")
            + _table([[a, f"у {b} записей"] for a, b in st.get("shared") or []],
                     "Общие задачи трекера" if src["second"] == "tracker" else "Общие инциденты")
            + "</div></section>")

    if src.get("actions"):
        h.append(f"<section id='act'><h2>Что сделать <span class='n'>{len(src['actions'])}</span></h2><div class='card'>"
                 + "".join(f"<div class='item'><span class='t'>{_e(a['title'])}</span> "
                           + " ".join(_ref(r, known) for r in a.get("refs") or [])
                           + (f"<div class='why'>{_e(a['why'])}</div>" if a.get("why") else "") + "</div>"
                           for a in src["actions"]) + "</div></section>")
    if src.get("groups"):
        h.append(f"<section id='grp'><h2>Общие проблемы и задачи <span class='n'>{len(src['groups'])}</span></h2>")
        for g in src["groups"]:
            tasks = " ".join(_ref(t, known, "t2", bool((sec.get(t) or {}).get("closed")),
                                  (sec.get(t) or {}).get("status") or "") + (f" <small>{_e((sec.get(t) or {}).get('status'))}</small>"
                                                                             if (sec.get(t) or {}).get("status") else "")
                             for t in g.get("tasks") or [])
            h.append(f"<div class='card'><div class='t'>{_e(g['title'])} <span class='n' style='font-weight:400;color:var(--muted)'>"
                     f"· {len(g.get('refs') or [])} зап.</span></div>"
                     + (f"<div class='why'>{_e(g['why'])}</div>" if g.get("why") else "")
                     + (f"<div class='sub2'><b>Статус:</b> {_e(g['status'])}</div>" if g.get("status") else "")
                     + (f"<div class='next' style='margin-top:4px'><b>Что делать:</b> {_e(g['next'])}</div>" if g.get("next") else "")
                     + f"<div class='meta'>{' '.join(_ref(r, known) for r in g.get('refs') or [])} {tasks}</div></div>")
        h.append("</section>")
    if src.get("attention"):
        h.append(f"<section id='att'><h2>Требует внимания <span class='n'>{len(src['attention'])}</span></h2><div class='card'>"
                 + "".join(f"<div class='item'><div class='row'><span class='dot {_e(a.get('priority'))}'></span><div>"
                           f"{_ref(a['ref'], known)} <span class='t'>{_e(a.get('title'))}</span><ul class='flags'>"
                           + "".join(f"<li>{_e(x)}</li>" for x in a.get("flags") or []) + "</ul></div></div></div>"
                           for a in src["attention"]) + "</div></section>")
    if src.get("close"):
        h.append(f"<section id='cls'><h2>Можно закрыть или предложить к закрытию <span class='n'>{len(src['close'])}</span></h2>"
                 "<div class='card'><ul class='plain'>" + "".join(
                     f"<li>{_ref(c['ref'], known)} — {_e(c.get('why'))}</li>" for c in src["close"]) + "</ul></div></section>")

    # записи
    h.append(f"<section id='items'><h2>Записи <span class='n'>{len(items)}</span></h2>"
             "<div class='filters'><button class='on' onclick=\"setS(this,'all')\">Все</button>"
             "<button onclick=\"setS(this,'att')\">Требуют внимания</button>"
             "<button onclick=\"setS(this,'exp-support')\">Ждут от нас</button>"
             "<button onclick=\"setS(this,'exp-dev')\">Ждут разработку</button>"
             "<button onclick=\"setS(this,'exp-user')\">Ждут пользователя</button>"
             "<button onclick=\"setS(this,'act-no')\">Можно закрывать</button>"
             + ("<button onclick=\"setS(this,'wait')\">В ожидании</button>" if src["start"] == "itsm" else "")
             + "<input id='sq' placeholder='Поиск: номер, задача, слово…' oninput='sflt()'>"
             "<button onclick='openS(true)'>Раскрыть</button><button onclick='openS(false)'>Свернуть</button></div>")
    if not items:
        h.append("<div class='card empty'>По этим условиям записей нет.</div>")
    order = {"high": 0, "medium": 1, "low": 2}
    for it in sorted(items, key=lambda x: (order.get((x.get("verdict") or {}).get("priority"), 1),
                                           -len((x.get("code_flags") or []) + (x.get("flags") or [])))):
        h.append(_item(it, src, known, sec))
    h.append("</section>")

    # связанные
    found = [s for s in sec_list if s.get("found")]
    if sec_list:
        ttl = "Задачи трекера" if src["second"] == "tracker" else "Инциденты ITSM"
        h.append(f"<section id='sec'><h2>{ttl} <span class='n'>{len(found)}</span></h2><div class='card' "
                 "style='padding:4px 6px;overflow-x:auto'><table><tr><th>Номер</th><th>Суть</th><th>Статус</th>"
                 "<th>Кто действует / релиз</th><th>Нужна записям</th></tr>")
        for s in sec_list:
            key = f"<a href='{_e(s['url'])}' target='_blank' rel='noopener'>{_e(s['ref'])}</a>" if s.get("url") else _e(s["ref"])
            if not s.get("found"):
                h.append(f"<tr id='{_aid(s['ref'])}'><td><b>{key}</b></td><td colspan='3' class='why'>не найдено</td>"
                         f"<td>{' '.join(_ref(r, known) for r in s.get('used_by') or [])}</td></tr>")
                continue
            who = "; ".join(x for x in (s.get("waiting_for"), s.get("waiting_what"), f"релиз {s['release']}" if s.get("release") else "",
                                         s.get("release_date"), s.get("waiting_reason")) if x)
            last = (s.get("comments") or [{}])[0].get("text") if s.get("comments") else ""
            tg = (s.get("tg") or {})
            h.append(f"<tr id='{_aid(s['ref'])}'><td><b>{key}</b></td><td>{_e(s.get('title'))}"
                     + (f"<div class='why'>{_e(last[:240])}</div>" if last else "")
                     + (f"<div class='why'>Telegram: {_e(tg.get('summary') or ('упоминается ' + str(tg.get('count')) + ' раз'))}</div>"
                        if tg.get("count") else "")
                     + f"</td><td>{_e(s.get('status'))}{' ✓' if s.get('closed') else ''}</td><td>{_e(who)}</td>"
                     f"<td>{' '.join(_ref(r, known) for r in s.get('used_by') or [])}</td></tr>")
        h.append("</table></div></section>")
    if src.get("clarify"):
        h.append("<section id='clar'><h2>Что уточнить</h2><div class='card'><ul class='plain'>"
                 + "".join(f"<li>{_e(x)}</li>" for x in src["clarify"]) + "</ul></div></section>")
    errs = meta.get("errors") or []
    if errs:
        h.append("<section><h2>Ошибки обработки</h2><div class='card'>"
                 + "".join(f"<div class='err'>{_e(e)}</div>" for e in errs) + "</div></section>")
    tg = src.get("tg") or {}
    h.append(f"<footer>OpsDigest · от источника: {_e(SRC_RU.get(src['start'], src['start']))} · запросов к модели: "
             f"{meta.get('llm_calls', 0)} · отправлено ≈{int(meta.get('chars_in', 0)) // 1000} тыс. симв."
             + (f" · Telegram: номеров найдено {tg.get('refs', 0)}, сообщений {tg.get('msgs', 0)} в {tg.get('chats', 0)} чатах"
                if look.get("telegram") else "")
             + " · ссылки #N открывают сообщение в Telegram (в окне OpsDigest — в его мессенджере)</footer>")
    h.append(f"</div><script>{JS}{JS2}</script></body></html>")
    return "".join(h)
