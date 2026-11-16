"""HTML-отчёт: один самодостаточный файл. Открывается в любом браузере и на телефоне, его можно переслать.

Без внешних ресурсов (шрифты, скрипты) — работает и в закрытой сети.
"""
from __future__ import annotations

from html import escape

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6472;--line:#e3e6eb;--acc:#0f766e;--acc-soft:#e6f4f2;
--hi:#c2410c;--hi-soft:#fdeee6;--md:#a16207;--md-soft:#fdf6e3;--lo:#6b7280;--lo-soft:#f1f2f4;--code:#f3f4f6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111316;--card:#1a1d22;--ink:#e8eaee;--muted:#9aa1ad;--line:#2b2f36;
--acc:#2dd4bf;--acc-soft:#123330;--hi:#fb923c;--hi-soft:#3a2416;--md:#facc15;--md-soft:#332b10;--lo:#9ca3af;
--lo-soft:#24272c;--code:#23262c}}
:root[data-theme=dark]{--bg:#111316;--card:#1a1d22;--ink:#e8eaee;--muted:#9aa1ad;--line:#2b2f36;
--acc:#2dd4bf;--acc-soft:#123330;--hi:#fb923c;--hi-soft:#3a2416;--md:#facc15;--md-soft:#332b10;--lo:#9ca3af;
--lo-soft:#24272c;--code:#23262c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}
.chg{font-size:11.5px;padding:1px 7px;border-radius:999px;margin-right:4px;white-space:nowrap}
.chg.new{background:var(--hi-soft);color:var(--hi)}.chg.updated{background:var(--md-soft);color:var(--md)}
.chg.resolved{background:var(--acc-soft);color:var(--acc);}.chg.same{background:var(--lo-soft);color:var(--lo)}
.changes .item .t{font-weight:600}
.item.resolved .t{text-decoration:line-through;text-decoration-color:var(--muted)}
.extra{white-space:pre-wrap;font-size:13.5px;color:var(--muted);margin-top:6px}
header h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}
.chip{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--lo-soft);color:var(--muted);white-space:nowrap}
.chip.acc{background:var(--acc-soft);color:var(--acc)}
nav.toc{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 4px;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:2}
nav.toc a{font-size:13px;color:var(--acc);text-decoration:none;padding:3px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
section{margin-top:22px}
h2{font-size:16px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
h2 .n{font-size:12px;font-weight:500;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 8px}
.headline{font-size:16px;border-left:4px solid var(--acc)}
.row{display:flex;gap:10px;align-items:flex-start}
.dot{flex:none;width:10px;height:10px;border-radius:50%;margin-top:7px;background:var(--lo)}
.dot.high{background:var(--hi)}.dot.medium{background:var(--md)}
.t{font-weight:600}
.why{color:var(--muted);font-size:14px;margin-top:2px}
.meta{font-size:12px;color:var(--muted);margin-top:4px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.meta a{color:var(--acc);text-decoration:none}
.tag{font-size:11px;padding:1px 7px;border-radius:4px;background:var(--lo-soft);color:var(--muted);white-space:nowrap}
.tag.reply,.tag.incident{background:var(--hi-soft);color:var(--hi)}
.tag.action,.tag.follow_up,.tag.question{background:var(--md-soft);color:var(--md)}
.tag.decision{background:var(--acc-soft);color:var(--acc)}
.tag.who{background:var(--acc-soft);color:var(--acc)}.tag.maybe{background:var(--md-soft);color:var(--md)}
.tag.ctl{background:var(--md-soft);color:var(--md)}
.draft{margin-top:8px;background:var(--code);border-radius:8px;padding:8px 10px;font-size:14px;position:relative;white-space:pre-wrap}
.draft b{display:block;font-size:11px;color:var(--muted);font-weight:500;margin-bottom:2px}
.copy{position:absolute;top:6px;right:6px;font-size:11px;border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:6px;padding:1px 7px;cursor:pointer}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
@media (max-width:700px){.grid2{grid-template-columns:1fr}}
ul.plain{margin:0;padding-left:18px}ul.plain li{margin:2px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}
td a{color:var(--acc);text-decoration:none}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.filters button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:3px 11px;cursor:pointer}
.filters button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.filters input{flex:1;min-width:160px;font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:3px 12px}
details.chat{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 8px}
details.chat summary{list-style:none;cursor:pointer;padding:10px 14px;display:flex;gap:10px;align-items:center}
details.chat summary::-webkit-details-marker{display:none}
details.chat[open] summary{border-bottom:1px solid var(--line)}
.imp{flex:none;display:flex;gap:2px}.imp i{width:4px;height:14px;border-radius:2px;background:var(--line)}.imp i.f{background:var(--acc)}
.ct{flex:1;min-width:0}.ct .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.cb{padding:10px 14px}
.item{padding:8px 0;border-top:1px dashed var(--line)}.item:first-child{border-top:0}
.empty{color:var(--muted);font-size:14px}
.err{color:var(--hi);font-size:13px}
footer{margin-top:30px;color:var(--muted);font-size:12px}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.thumbs figure{margin:0;width:168px}
.thumbs img{width:168px;height:112px;object-fit:cover;border-radius:6px;border:1px solid var(--line);background:var(--code);display:block}
.thumbs figcaption{font-size:12px;color:var(--muted);line-height:1.3;margin-top:3px}
.src{font-size:11px;padding:1px 7px;border-radius:4px;white-space:nowrap;background:var(--lo-soft);color:var(--muted)}
.src.telegram{background:#e0f0fb;color:#1d6fa5}.src.mail{background:#efe7fb;color:#6d28d9}
.src.itsm{background:var(--hi-soft);color:var(--hi)}.src.wiki{background:var(--acc-soft);color:var(--acc)}
.cav{flex:none;width:28px;height:28px;border-radius:50%;object-fit:cover;display:inline-grid;place-items:center;color:#fff;
font-size:12px;font-weight:600;vertical-align:middle;margin-right:2px}
.cav.sm{width:20px;height:20px;font-size:9.5px}
.cav.c0{background:linear-gradient(#ff885e,#ff516a)}.cav.c1{background:linear-gradient(#ffcd6a,#ffa85c)}.cav.c2{background:linear-gradient(#a0de7e,#54cb68)}
.cav.c3{background:linear-gradient(#53edd6,#28c9b7)}.cav.c4{background:linear-gradient(#72d5fd,#2a9ef1)}.cav.c5{background:linear-gradient(#82b1ff,#665fff)}
.cav.c6{background:linear-gradient(#e0a2f3,#d669ed)}
.meta .who-chat{display:inline-flex;align-items:center;gap:5px}
.warnbox{background:var(--md-soft);color:var(--md);border-radius:10px;padding:10px 14px;margin:0 0 8px;font-size:14px}
.warnbox a{color:inherit}
.cross .conf{color:var(--hi);font-size:13px;margin-top:4px}.cross .next{font-size:13px;margin-top:4px}
:root[data-theme=dark] .src.telegram{background:#1e2c3a;color:#7cb8e8}:root[data-theme=dark] .src.mail{background:#2a1f3d;color:#c4b5fd}
"""

JS = r"""
try{const th=localStorage.getItem('od_theme');if(th==='dark'||th==='light')document.documentElement.dataset.theme=th}catch(e){}
function cp(b){const t=b.parentElement.querySelector('span').innerText;
 const done=()=>{b.textContent='Скопировано';setTimeout(()=>b.textContent='Копировать',1500)};
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done)}else{
 const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();
 try{document.execCommand('copy');done()}catch(e){}document.body.removeChild(a)}}
let F='all';
function flt(){const q=(document.getElementById('q').value||'').toLowerCase();
 document.querySelectorAll('details.chat').forEach(d=>{let ok=true;
  if(F==='high')ok=d.dataset.prio==='high';if(F==='react')ok=d.dataset.react==='1';
  if(F==='inc')ok=d.dataset.mode!=='chat'||d.dataset.inc==='1';
  if(q)ok=ok&&d.dataset.text.includes(q);d.style.display=ok?'':'none'})}
function setF(b,f){F=f;document.querySelectorAll('.filters button').forEach(x=>x.classList.toggle('on',x===b));flt()}
function openAll(v){document.querySelectorAll('details.chat').forEach(d=>{if(d.style.display!=='none')d.open=v})}
// ссылки на Telegram: в окне OpsDigest открываем чат прямо в его мессенджере, а не в браузере
document.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(!a)return;
 const u=a.getAttribute('href');if(!/^(https?:\/\/(t|telegram)\.me\/|tg:)/i.test(u))return;
 try{const o=window.opener;if(o&&!o.closed&&o.mxOpenLink){e.preventDefault();o.mxOpenLink(u);try{o.focus()}catch(x){}return}}catch(x){}
 if(/^https?:$/.test(location.protocol)&&/^\/reports\//.test(location.pathname)){e.preventDefault();
  const t=(new URLSearchParams(location.search)).get('t')||'';location.href='/#t='+encodeURIComponent(t)+'&open='+encodeURIComponent(u)}});
"""


def _e(s) -> str:
    return escape(str(s if s is not None else ""))


def _initials(t: str) -> str:
    import re
    w = [x for x in re.findall(r"[\wЁё]+", t or "") if not x.isdigit()]
    return ((w[0][0] + (w[1][0] if len(w) > 1 else "")) if w else "?").upper()


def _av(src: str | None, title: str, chat_id=None, sm: bool = False) -> str:
    """Аватарка чата: фото из Telegram (если скачано) или кружок с буквами, как в мессенджере."""
    cls = "cav sm" if sm else "cav"
    if src:
        return f"<img class='{cls}' src='{_e(src)}' alt='' loading='lazy'>"
    n = abs(int(chat_id)) if isinstance(chat_id, int) else sum(map(ord, title or ""))
    return f"<span class='{cls} c{n % 7}'>{_e(_initials(title))}</span>"


def _links(links: list[dict], chat_title: str = "", avatar: str | None = None, chat_id=None) -> str:
    parts = []
    if chat_title:
        parts.append(f"<span class='who-chat'>{_av(avatar, chat_title, chat_id, True)}{_e(chat_title)}</span>")
    for l in links or []:
        if l.get("url"):
            parts.append(f'<a href="{_e(l["url"])}" target="_blank" rel="noopener">#{_e(l["id"])}&nbsp;↗</a>')
        else:
            parts.append(f"<span>#{_e(l['id'])}</span>")
    return " ".join(parts)


def _draft(text: str | None) -> str:
    if not text:
        return ""
    return (f'<div class="draft"><b>Черновик ответа</b><span>{_e(text)}</span>'
            f'<button class="copy" onclick="cp(this)">Копировать</button></div>')


def _thumbs(imgs: list[dict] | None) -> str:
    """Изображения рядом с пунктом: файл лежит в папке media рабочей папки, подпись — описание модели."""
    if not imgs:
        return ""
    out = []
    for x in imgs:
        cap = _e(x.get("summary") or "изображение не описано")
        out.append(f"<figure><a href='{_e(x['src'])}' target='_blank' rel='noopener'><img loading='lazy' "
                   f"src='{_e(x['src'])}' alt='{cap}'></a><figcaption>#{_e(x['msg_id'])} · {cap}</figcaption></figure>")
    return "<div class='thumbs'>" + "".join(out) + "</div>"


TO_ME_RU = {"tag": ("вас отметили @", "reply"), "reply": ("ответ вам", "reply"),
            "name": ("по имени — проверьте, вам ли", "maybe"), "role": ("в вашей зоне", "")}


def _who(it: dict) -> str:
    """Кому адресовано и кто исполнитель — метки пункта."""
    out = []
    tm = TO_ME_RU.get(it.get("to_me") or "")
    if tm:
        out.append(f'<span class="tag {tm[1]}">{_e(tm[0])}</span>')
    o = it.get("owner")
    if o == "other":
        out.append(f'<span class="tag who">ведёт: {_e(it.get("owner_name") or "другой")}</span>')
    elif o == "nobody":
        out.append('<span class="tag maybe">без исполнителя</span>')
    elif o == "me":
        out.append('<span class="tag reply">на вас</span>')
    if it.get("control"):
        out.append('<span class="tag ctl">проследить</span>')
    return "".join(out)


CHANGE_RU = {"new": "новое", "updated": "изменилось", "resolved": "закрыто", "same": "без изменений"}


def _chg(kind: str | None) -> str:
    return f'<span class="chg {_e(kind)}">{_e(CHANGE_RU[kind])}</span>' if kind in CHANGE_RU else ""


def _item(it: dict) -> str:
    from . import TYPE_RU
    comp = f'<span class="tag">{_e(it["competence"])}</span>' if it.get("competence") else ""
    comp = _who(it) + comp
    return (f'<div class="item{" resolved" if it.get("change") == "resolved" else ""}"><div class="row">'
            f'<span class="dot {_e(it.get("priority"))}"></span><div style="flex:1">'
            f'{_chg(it.get("change")) if it.get("change") != "same" else ""}'
            f'<span class="tag {_e(it["type"])}">{_e(TYPE_RU.get(it["type"], it["type"]))}</span> '
            f'<span class="t">{_e(it.get("title"))}</span>'
            + (f'<div class="why">{_e(it["details"])}</div>' if it.get("details") else "")
            + f'<div class="meta">{comp}{_links(it.get("links", []))}</div>'
            + _thumbs(it.get("images")) + _draft(it.get("draft")) + "</div></div></div>")


SRC_RU = {"telegram": "Telegram", "mail": "Почта", "itsm": "ITSM", "wiki": "Вики", "rooms": "X5 Rooms",
          "tracker": "Трекер", "meet": "Встречи"}


def _srcs(xs) -> str:
    return "".join(f"<span class='src {_e(x)}'>{_e(SRC_RU.get(x, x))}</span>" for x in xs or [])


def _cross(cr: dict) -> str:
    """Раздел «Общая картина»: нити по всем источникам, только в Telegram / только в почте, недоступные плагины."""
    names = " + ".join(["Telegram"] + [SRC_RU.get(k, k) for k in cr.get("sources") or []])
    h = [f"<section id='cross' class='cross'><h2>Общая картина: {_e(names)} "
         f"<span class='n'>{len(cr.get('threads') or [])}</span></h2>"]
    for k, v in (cr.get("offline") or {}).items():
        links = " ".join(f"<a href='{_e(u)}' target='_blank' rel='noopener'>подключить ↗</a>" for u in v.get("links") or [])
        h.append(f"<div class='warnbox'>⚠ {_e(SRC_RU.get(k, k))}: плагин "
                 f"Maverick не подключён — переподключите его в чате Maverick. {_e((v.get('detail') or '')[:300])} {links}</div>")
    if cr.get("headline"):
        h.append(f"<div class='card headline'>{_e(cr['headline'])}</div>")
    for t in cr.get("threads") or []:
        refs = []
        for r in t.get("tg_refs") or []:
            ls = _links([x for x in r.get("links") or [] if x.get("id")], r.get("chat", ""), r.get("avatar"), r.get("chat_id"))
            refs.append(ls or _e(r.get("chat", "")))
        for m in t.get("mail_refs") or []:
            refs.append(f"<span>✉ {_e(m.get('subject'))} <small>{_e(m.get('date'))}</small></span>")
        for x in t.get("itsm_refs") or []:
            refs.append(f"<span class='tag incident'>{_e(x['number'])}</span> {_e(x.get('status'))}")
        for kind, ic in (("rooms", "💭"), ("tracker", "🗂"), ("meet", "🎙")):
            for x in t.get(kind + "_refs") or []:
                lbl = (x["id"] + " " if kind == "tracker" else "") + (x.get("title") or "")
                lbl_h = f"<a href='{_e(x['url'])}' target='_blank' rel='noopener'>{_e(lbl)}</a>" if x.get("url") else _e(lbl)
                refs.append(f"<span>{ic} {lbl_h} <small>{_e(x.get('status') or x.get('date') or '')}</small></span>")
        prio = "high" if t.get("importance", 3) >= 4 else "medium" if t.get("importance", 3) == 3 else "low"
        h.append(f"<div class='card'><div class='row'><span class='dot {prio}'></span><div style='flex:1'>"
                 f"<span class='t'>{_e(t['title'])}</span> {_srcs(t.get('sources'))}"
                 + (f" <span class='tag'>{_e(t['status'])}</span>" if t.get("status") else "")
                 + (" <span class='tag reply'>к вам</span>" if t.get("to_me") else "")
                 + (f"<div class='why'>{_e(t['summary'])}</div>" if t.get("summary") else "")
                 + (f"<div class='conf'>Расхождение: {_e(t['conflicts'])}</div>" if t.get("conflicts") else "")
                 + (f"<div class='next'><b>Что сделать:</b> {_e(t['next'])}</div>" if t.get("next") else "")
                 + (f"<div class='meta'>Участники: {_e(', '.join(t['participants']))}</div>" if t.get("participants") else "")
                 + f"<div class='meta'>{' · '.join(refs)}</div></div></div></div>")
    if cr.get("only_tg") or cr.get("only_mail"):
        h.append("<div class='grid2'>")
        if cr.get("only_tg"):
            h.append("<div class='card'><div class='t'>Только в Telegram</div><ul class='plain'>" + "".join(
                f"<li>{_e((x.get('ref') or {}).get('title') or x['tg'])} <small>({_e((x.get('ref') or {}).get('chat'))})</small>"
                f" — {_e(x.get('why'))}</li>" for x in cr["only_tg"]) + "</ul></div>")
        if cr.get("only_mail"):
            h.append("<div class='card'><div class='t'>Только в почте</div><ul class='plain'>" + "".join(
                f"<li>✉ {_e((x.get('ref') or {}).get('subject') or x['mail'])} — {_e(x.get('why'))}</li>"
                for x in cr["only_mail"]) + "</ul></div>")
        h.append("</div>")
    if cr.get("only_other"):
        ic = {"rooms": "💭", "tracker": "🗂", "meet": "🎙"}
        h.append("<div class='card'><div class='t'>Только в Rooms, трекере и на встречах</div><ul class='plain'>" + "".join(
            f"<li>{ic.get(x['source'], '')} {_e(((x.get('info') or {}).get('title')) or x['ref'])} — {_e(x.get('why'))}</li>"
            for x in cr["only_other"]) + "</ul></div>")
    if cr.get("actions"):
        h.append("<div class='card'><div class='t'>Что сделать по всем источникам</div><ul class='plain'>" + "".join(
            f"<li><b>{_e(a['title'])}</b>{(' — ' + _e(a.get('why'))) if a.get('why') else ''} {_srcs(a.get('sources'))}</li>"
            for a in cr["actions"]) + "</ul></div>")
    if cr.get("itsm"):
        h.append("<details class='chat'><summary><span class='ct'><span class='t'>Карточки ITSM: "
                 f"{len(cr['itsm'])}</span></span></summary><div class='cb'><table><tr><th>Номер</th><th>Суть</th>"
                 "<th>Статус</th><th>Группа / исполнитель</th></tr>" + "".join(
                     f"<tr><td><b>{_e(x['ref'])}</b></td><td>{_e(x['title'])}<div class='why'>"
                     f"{_e((x['data'].get('description') or '')[:300])}</div></td><td>{_e(x['status'])}</td>"
                     f"<td>{_e(x['data'].get('group') or '')} {_e(x['data'].get('assignee') or '')}</td></tr>"
                     for x in cr["itsm"]) + "</table></div></details>")
    if cr.get("mail"):
        h.append("<details class='chat'><summary><span class='ct'><span class='t'>Письма за период: "
                 f"{len(cr['mail'])}</span></span></summary><div class='cb'>" + "".join(
                     f"<div class='item'><b>{_e(m['data'].get('subject'))}</b> <small>{_e(m['data'].get('date'))} · "
                     f"{_e(m['data'].get('from'))}</small><div class='why'>{_e(m['data'].get('summary'))}</div></div>"
                     for m in cr["mail"]) + "</div></details>")
    if cr.get("meet"):
        h.append("<details class='chat'><summary><span class='ct'><span class='t'>Встречи за период: "
                 f"{len(cr['meet'])}</span></span></summary><div class='cb'>" + "".join(
                     f"<div class='item'><b>{_e(m['data'].get('title'))}</b> <small>{_e(m['data'].get('date'))}</small>"
                     f"<div class='why'>{_e(m['data'].get('summary'))}</div>"
                     + (f"<div class='next'><b>Вам:</b> {_e(m['data']['to_me'])}</div>" if m['data'].get('to_me') else "")
                     + "</div>" for m in cr["meet"]) + "</div></details>")
    if cr.get("tracker"):
        h.append("<details class='chat'><summary><span class='ct'><span class='t'>Задачи трекера: "
                 f"{len(cr['tracker'])}</span></span></summary><div class='cb'><table><tr><th>Ключ</th><th>Суть</th>"
                 "<th>Статус</th><th>Исполнитель</th></tr>" + "".join(
                     f"<tr><td><b>{_e(x['ref'])}</b></td><td>{_e(x['title'])}<div class='why'>"
                     f"{_e((x['data'].get('description') or '')[:300])}</div></td><td>{_e(x['status'])}</td>"
                     f"<td>{_e(x['data'].get('assignee') or '')}</td></tr>" for x in cr["tracker"]) + "</table></div></details>")
    if cr.get("rooms"):
        h.append("<details class='chat'><summary><span class='ct'><span class='t'>X5 Rooms за период: "
                 f"{len(cr['rooms'])}</span></span></summary><div class='cb'>" + "".join(
                     f"<div class='item'><b>{_e(m['data'].get('chat'))}</b> <small>{_e(m['data'].get('date'))} · "
                     f"{_e(m['data'].get('from'))}</small><div class='why'>{_e(m['data'].get('summary'))}</div></div>"
                     for m in cr["rooms"]) + "</div></details>")
    if cr.get("wiki"):
        h.append("<div class='card'><div class='t'>Статьи вики</div><ul class='plain'>" + "".join(
            f"<li><b>{_e(x['data'].get('term'))}</b>: " + (f"<a href='{_e(x['data']['url'])}' target='_blank' rel='noopener'>"
                                                          f"{_e(x['data'].get('title') or x['data']['url'])}</a> — "
                                                          if x["data"].get("url") else "")
            + f"{_e(x['data'].get('summary'))}</li>" for x in cr["wiki"]) + "</ul></div>")
    h.append("</section>")
    return "".join(h)


def _hist_chip(hs: dict) -> str:
    """Сколько взято из прошлых сводок — чтобы было видно, что модель не читала всё заново."""
    if not hs.get("on") or not (hs.get("reused") or hs.get("chained")):
        return ""
    t = f"из прошлых сводок: {hs.get('reused', 0) + hs.get('chained', 0)} чат(ов)"
    if hs.get("skipped_msgs"):
        t += f", не перечитано {hs['skipped_msgs']} сообщ."
    return f"<span class='chip'>{_e(t)}</span>"


def render_html(d: dict) -> str:
    from . import PERIOD_RU, PRIO_RU
    if (d.get("meta") or {}).get("mode") == "source":   # сводка «От источника» — свой вид
        from .source_html import render_source_html
        return render_source_html(d)
    meta, ov = d["meta"], d["overview"]
    title = f"Сводка за {meta.get('period_label') or PERIOD_RU.get(meta['period'], meta['period'])}"
    h: list[str] = []
    h.append(f"<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)} · "
             f"{_e(meta['period_text'])}</title><style>{CSS}</style></head><body><div class='wrap'>")
    h.append(f"<header><h1>{_e(title)}</h1><div class='sub'>{_e(meta['period_text'])}"
             + (f" · {_e(meta['me'])}" if meta.get("me") else "") + "</div><div class='chips'>"
             f"<span class='chip acc'>Telegram</span>"
             + "".join(f"<span class='chip acc'>{_e(SRC_RU.get(k, k))}</span>" for k in (d.get("cross") or {}).get("sources") or []
                       if k not in (d.get("cross") or {}).get("offline", {}))
             + f"<span class='chip'>чатов: {meta['chats']}</span>"
             f"<span class='chip'>сообщений: {meta['messages']}</span>"
             f"<span class='chip'>модель: {_e(meta['provider'])}</span>"
             + (f"<span class='chip'>изображений: {(meta.get('images') or {}).get('images', 0)}</span>"
                if (meta.get("images") or {}).get("images") else "") +
             f"<span class='chip'>собрано: {_e(meta.get('generated_local', ''))}</span>"
             + (f"<span class='chip'>выбрано чатов: {meta['chats_selected']}</span>" if meta.get("chats_selected") else "")
             + _hist_chip(meta.get("history") or {}) + "</div>"
             + (f"<div class='extra'>Дополнительные указания: {_e(meta['extra_prompt'])}</div>" if meta.get("extra_prompt") else "")
             + "</header>")
    cr = d.get("cross") or {}
    toc = [("chg", "Что изменилось", len(ov.get("changes") or [])),
           ("cross", "Все источники", len(cr.get("threads") or []) + len(cr.get("offline") or {})),
           ("top", "Нужна реакция", len(ov.get("top") or [])), ("ctl", "Проследить", len(ov.get("control") or [])),
           ("inc", "Аварии", len(ov.get("incidents") or [])),
           ("comp", "Компетенции", len(ov.get("competences") or [])), ("refs", "Номера", len(d.get("refs") or [])),
           ("chats", "Чаты", len(d.get("chats") or []))]
    h.append("<nav class='toc'>" + "".join(f"<a href='#{k}'>{v} · {n}</a>" for k, v, n in toc if n) + "</nav>")

    if ov.get("headline"):
        note = " <span class='chip'>собрано без общей сводки модели</span>" if ov.get("fallback") else ""
        h.append(f"<section><div class='card headline'>{_e(ov['headline'])}{note}</div></section>")
    if ov.get("changes"):
        prev = ((meta.get("history") or {}).get("prev") or {}).get("text") or ""
        by_key = {c["key"]: c for c in d.get("chats") or []}
        h.append(f"<section id='chg' class='changes'><h2>Что изменилось с прошлой сводки <span class='n'>"
                 f"{len(ov['changes'])}</span></h2>" + (f"<div class='why'>Прошлая сводка: {_e(prev)}</div>" if prev else ""))
        h.append("<div class='card'>" + "".join(
            f"<div class='item'><div class='row'><div style='flex:1'>{_chg(x.get('kind'))}<span class='t'>{_e(x['title'])}</span>"
            + (f" <span class='why'>· «{_e(by_key[x['chat']]['title'])}»</span>" if x.get("chat") in by_key else "")
            + (f"<div class='why'>{_e(x['details'])}</div>" if x.get("details") else "") + "</div></div></div>"
            for x in ov["changes"]) + "</div></section>")
    if d.get("cross"):
        h.append(_cross(d["cross"]))

    # --- нужна реакция
    h.append(f"<section id='top'><h2>Нужна реакция <span class='n'>{len(ov.get('top') or [])}</span></h2>")
    if ov.get("top"):
        for t in ov["top"]:
            h.append(f"<div class='card'><div class='row'><span class='dot {_e(t.get('priority'))}'></span>"
                     f"<div style='flex:1'><div class='t'>{_e(t['title'])}</div>"
                     + (f"<div class='why'>{_e(t['why'])}</div>" if t.get("why") else "")
                     + f"<div class='meta'>{_who(t)}{_links(t.get('links', []), t.get('chat_title', ''), t.get('chat_avatar'))}</div>"
                     + _thumbs(t.get("images")) + _draft(t.get("draft")) + "</div></div></div>")
    else:
        h.append("<div class='card empty'>Ничего срочного не найдено.</div>")
    h.append("</section>")

    # --- ведут другие: проследить
    if ov.get("control"):
        h.append(f"<section id='ctl'><h2>Ведут другие — проследить <span class='n'>{len(ov['control'])}</span></h2>")
        for t in ov["control"]:
            who = f"<span class='tag who'>ведёт: {_e(t['who'])}</span>" if t.get("who") else ""
            h.append(f"<div class='card'><div class='row'><span class='dot low'></span><div style='flex:1'>"
                     f"<div class='t'>{_e(t['title'])}</div>"
                     + (f"<div class='why'>{_e(t['why'])}</div>" if t.get("why") else "")
                     + f"<div class='meta'>{who}{_links(t.get('links', []), t.get('chat_title', ''), t.get('chat_avatar'))}</div>"
                     + _draft(t.get("draft")) + "</div></div></div>")
        h.append("</section>")

    # --- аварии
    if ov.get("incidents"):
        h.append(f"<section id='inc'><h2>Аварии и инциденты <span class='n'>{len(ov['incidents'])}</span></h2>")
        for x in ov["incidents"]:
            refs = " ".join(f"<span class='tag'>{_e(r)}</span>" for r in x.get("refs") or [])
            st = f"<span class='tag incident'>{_e(x['status'])}</span> " if x.get("status") else ""
            h.append(f"<div class='card'>{st}<span class='t'>{_e(x['title'])}</span>"
                     + (f"<div class='why'>{_e(x['details'])}</div>" if x.get("details") else "")
                     + f"<div class='meta'>{_e(', '.join(x.get('chat_titles') or []))} {refs}</div></div>")
        h.append("</section>")

    # --- компетенции / уточнить / к сведению
    if ov.get("competences"):
        h.append(f"<section id='comp'><h2>По компетенциям</h2><div class='grid2'>")
        for c in ov["competences"]:
            h.append(f"<div class='card'><div class='t'>{_e(c['name'])}</div><ul class='plain'>"
                     + "".join(f"<li>{_e(p)}</li>" for p in c.get("points") or []) + "</ul></div>")
        h.append("</div></section>")
    if ov.get("clarify") or ov.get("fyi"):
        h.append("<section><div class='grid2'>")
        if ov.get("clarify"):
            h.append("<div class='card'><div class='t'>Что уточнить</div><ul class='plain'>"
                     + "".join(f"<li>{_e(x)}</li>" for x in ov["clarify"]) + "</ul></div>")
        if ov.get("fyi"):
            h.append("<div class='card'><div class='t'>Полезно знать</div><ul class='plain'>"
                     + "".join(f"<li>{_e(x)}</li>" for x in ov["fyi"]) + "</ul></div>")
        h.append("</div></section>")

    # --- сквозные номера
    refs = d.get("refs") or []
    if refs:
        h.append(f"<section id='refs'><h2>Номера инцидентов и задач <span class='n'>{len(refs)}</span></h2>"
                 "<div class='card' style='padding:4px 6px;overflow-x:auto'><table><tr><th>Номер</th><th>Тип</th>"
                 "<th>Где упоминается</th></tr>")
        for g in refs[:80]:
            by_chat: dict[str, list[str]] = {}
            for m in g["mentions"]:
                a = (f"<a href='{_e(m['url'])}' target='_blank' rel='noopener'>#{m['msg_id']}</a>" if m.get("url")
                     else f"#{m['msg_id']}")
                by_chat.setdefault(m["chat"], []).append(a)
            where = "<br>".join(f"{_e(c)}: {' '.join(v[:6])}" + (" …" if len(v) > 6 else "") for c, v in by_chat.items())
            h.append(f"<tr><td><b>{_e(g['value'])}</b></td><td>{_e(g['type'])}</td><td>{where}</td></tr>")
        h.append("</table></div></section>")

    # --- по чатам
    chats = d.get("chats") or []
    h.append(f"<section id='chats'><h2>По чатам <span class='n'>{len(chats)}</span></h2>"
             "<div class='filters'><button class='on' onclick=\"setF(this,'all')\">Все</button>"
             "<button onclick=\"setF(this,'react')\">Нужна реакция</button>"
             "<button onclick=\"setF(this,'high')\">Высокий приоритет</button>"
             "<button onclick=\"setF(this,'inc')\">Аварии и алерты</button>"
             "<input id='q' placeholder='Поиск по чатам…' oninput='flt()'>"
             "<button onclick='openAll(true)'>Раскрыть</button><button onclick='openAll(false)'>Свернуть</button></div>")
    for c in chats:
        react = any(i["type"] in ("reply", "action", "follow_up", "question") and i.get("owner") != "other"
                    for i in c.get("items", []))
        inc = any(i["type"] == "incident" for i in c.get("items", []))
        imp = int(c.get("importance") or 1)
        bars = "".join(f"<i class='{'f' if k < imp else ''}'></i>" for k in range(5))
        text = f"{c['title']} {c.get('summary', '')}".lower()
        badges = [f"<span class='tag'>{_e({'private': 'личный', 'group': 'группа', 'channel': 'канал'}.get(c['kind'], c['kind']))}</span>"]
        if c.get("mode") == "incident":
            badges.append("<span class='tag incident'>аварийный</span>")
        elif c.get("mode") == "alerts":
            badges.append("<span class='tag follow_up'>алерты</span>")
        if react:
            badges.append("<span class='tag reply'>реакция</span>")
        info = f"{c['count']} сообщ." + (f" · непрочитано {c['unread']}" if c.get("unread") else "")
        h.append(f"<details class='chat' data-prio='{_e(c['priority'])}' data-react='{int(react)}' data-inc='{int(inc)}'"
                 f" data-mode='{_e(c.get('mode'))}' data-text='{_e(text)}'"
                 + (" open" if react and imp >= 4 else "") + ">"
                 f"<summary><span class='imp' title='важность {imp}/5'>{bars}</span>{_av(c.get('avatar'), c['title'], c.get('chat_id'))}<span class='ct'>"
                 f"<span class='t'>{_e(c['title'])}</span><span class='meta'>{''.join(badges)} {info} · приоритет "
                 f"{_e(PRIO_RU.get(c['priority'], c['priority']))}</span></span></summary><div class='cb'>")
        if c.get("missing"):
            h.append("<div class='err'>Модель не вернула сводку по этому чату.</div>")
        if c.get("summary"):
            h.append(f"<div>{_e(c['summary'])}</div>")
        if c.get("link"):
            h.append(f"<div class='meta'><a href='{_e(c['link'])}' target='_blank' rel='noopener'>Открыть чат ↗</a></div>")
        for it in c.get("items", []):
            h.append(_item(it))
        shown = {x["msg_id"] for it in c.get("items", []) for x in it.get("images") or []}
        rest = [x for x in c.get("images") or [] if x["msg_id"] not in shown]
        if rest:
            h.append(f"<div class='meta' style='margin-top:10px'>Изображения в переписке: {len(rest)}</div>"
                     + _thumbs(rest))
        h.append("</div></details>")
    h.append("</section>")

    errs = meta.get("errors") or []
    if errs:
        h.append("<section><h2>Ошибки обработки</h2><div class='card'>"
                 + "".join(f"<div class='err'>{_e(e)}</div>" for e in errs) + "</div></section>")
    h.append(f"<footer>OpsDigest · запросов к модели: {meta.get('llm_calls', 0)} · "
             f"отправлено ≈{int(meta.get('chars_in', 0)) // 1000} тыс. симв. · "
             "ссылки #N открывают сообщение в Telegram (в окне OpsDigest — в его мессенджере)</footer>")
    h.append(f"</div><script>{JS}</script></body></html>")
    return "".join(h)
