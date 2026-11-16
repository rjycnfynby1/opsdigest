"""Тёзки пользователя: кто ещё в чате носит то же имя (например, Алексей или Лёша).

Обращение по имени без @ника — не обязательно к пользователю: в рабочих чатах часто есть его тёзки.
Модели в заголовке чата перечисляются участники с тем же именем, а метки в переписке разделяют точные признаки
(«@me» — отметили через @ник, «↩#N(Я)» — ответ на сообщение пользователя) и обращение только по имени.
"""
from __future__ import annotations

import re

from .privacy import names as N

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def _bases(word: str) -> set[str]:
    """Полные имена и уменьшительные формы для слова из сообщения."""
    n = N.norm(word)
    out: set[str] = set()
    if n in N.FIRST_NAMES:
        out.add(n)
    elif n in N.FIRST_FORMS:
        out.add(N.FIRST_FORMS[n])
    else:  # звательная форма «Коль», «Сань», «Вань»
        for tail in ("я", "а"):
            if n + tail in N.FIRST_NAMES:
                out.add(n + tail)
    for b in list(out):
        out |= N.CANON.get(b, set())
    return out


def my_name_forms(profile: dict, me_name: str = "") -> set[str]:
    """Все формы имени пользователя: падежи, уменьшительные, звательные («коль»), а также его обращения из профиля."""
    words: list[str] = []
    for s in [profile.get("name") or "", me_name or ""]:
        words += _WORD.findall(s)[:1] + _WORD.findall(s)[1:2]  # имя обычно первое, но бывает «Фамилия Имя»
    aliases = [a for a in profile.get("aliases") or [] if a]
    for a in aliases:
        words += _WORD.findall(str(a))
    bases: set[str] = set()
    for w in words:
        bases |= _bases(w)
    forms: set[str] = set()
    for b in bases:
        g = N.gender_of(b)
        variants = [b] + N.diminutives(b)
        for v in variants:
            vf = N.all_forms(N.decline_first(v, g if g in ("m", "f") else "m"))
            forms |= vf
            if len(v) >= 4 and v[-1] == "я":
                forms.add(v[:-1] + "ь")  # «Коль», «Вань»
            elif len(v) >= 4 and v[-1] == "а":
                forms.add(v[:-1])        # «Саш», «Миш»
    for a in aliases:  # обращения из профиля — как написаны (кроме длинных фраз)
        if len(_WORD.findall(str(a))) == 1:
            forms.add(N.norm(str(a).strip()))
    return {f for f in forms if len(f) >= 3}


def is_namesake(display: str, forms: set[str]) -> bool:
    toks = [N.norm(t) for t in _WORD.findall(display or "")]
    return any(t in forms for t in toks[:3])


def chat_namesakes(store, chat_id: int, forms: set[str], me_id: int | None = None, limit: int = 8,
                   source: str = "telegram") -> list[str]:
    """Участники чата (писавшие в нём и известные по списку участников) с тем же именем, что у пользователя."""
    if not forms:
        return []
    out: list[str] = []
    seen: set = set()
    try:
        rows = store.db.execute(
            """SELECT sender_id, sender, COUNT(*) AS n FROM messages WHERE source=? AND chat_id=? AND is_out=0
               AND sender IS NOT NULL AND sender!='' GROUP BY sender_id, sender ORDER BY n DESC""",
            (source, chat_id)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    try:
        rows = list(rows) + list(store.db.execute(
            "SELECT user_id AS sender_id, name AS sender, 0 AS n FROM members WHERE chat_id=?", (chat_id,)).fetchall())
    except Exception:  # noqa: BLE001 — таблицы участников ещё нет
        pass
    for r in rows:
        sid, name = r["sender_id"], r["sender"] or ""
        if (me_id and sid == me_id) or (sid or name) in seen:
            continue
        seen.add(sid or name)
        if is_namesake(name, forms):
            out.append(name)
        if len(out) >= limit:
            break
    return out


def mine_targets(store, chat_id: int, msgs, source: str = "telegram") -> set[int]:
    """Номера сообщений пользователя, на которые отвечают в msgs (для метки «↩#N(Я)»)."""
    want = {m.reply_to for m in msgs if getattr(m, "reply_to", None)}
    if not want:
        return set()
    mine = {m.msg_id for m in msgs if m.is_out and m.msg_id in want}
    rest = list(want - {m.msg_id for m in msgs})
    for i in range(0, len(rest), 500):
        part = rest[i:i + 500]
        try:
            mine |= {r[0] for r in store.db.execute(
                f"SELECT msg_id FROM messages WHERE source=? AND chat_id=? AND is_out=1 AND msg_id IN "
                f"({','.join('?' * len(part))})", (source, chat_id, *part)).fetchall()}
        except Exception:  # noqa: BLE001
            break
    return mine
