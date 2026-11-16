"""Реестр людей для обезличивания: кто есть в переписке и как его могут назвать в тексте.

Источники: отправители сообщений и личные чаты (из базы), владелец аккаунта (kv «me») и профиль «О себе»,
ручные дополнения из config.json → privacy.people ([{"name": "Иван Петров", "aliases": ["Палыч"]}]).
Для каждого человека собираются все формы: имя/фамилия/отчество во всех падежах, уменьшительные, латиница,
логины (ipetrov, i.petrov), ники и прозвища.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import names as N

BOT_RE = re.compile(r"(?i)(\bbot\b|_bot$|бот\b|\bbot_|robot|alert|zabbix|grafana|prometheus|jenkins|gitlab|jira)")


# служебные учётки и слова, которые не бывают именами людей: «Поддержка МП», «Группа мониторинга», «Дежурный L2».
# Без этого «группа», «поддержка» и названия групп ITSM кодировались как люди (запрос к плагину ITSM не находил группу)
TEAM_RE = re.compile(r"(?i)(?<![\wЁё])(групп\w*|поддержк\w*|служб\w*|отдел|отдела|отделу|отделом|отделе|"
                     r"команд[аеуыо]й?|дежурн\w*|сервис\w*|мониторинг\w*|линия|линии|линию|смена|смены|смене|"
                     r"support\w*|team\w*|helpdesk|servicedesk|service\w*|оповещ\w*|уведомлен\w*|алерт\w*)(?![\wЁё])")


def _name_word(w: str) -> bool:
    """Может ли слово (форма имени, фамилии, прозвища) быть именем — не служебное слово."""
    return not TEAM_RE.search(w or "")


@dataclass
class Person:
    key: str
    display: str
    first: str = ""
    last: str = ""
    patronymic: str = ""
    gender: str = "u"
    usernames: set[str] = field(default_factory=set)
    nicks: set[str] = field(default_factory=set)
    is_me: bool = False
    unknown: bool = False      # найден в тексте, в реестре его не было
    maybe_me: bool = False     # названный только по имени, как у пользователя: он сам или его тёзка
    first_forms: set[str] = field(default_factory=set)
    last_forms: set[str] = field(default_factory=set)
    patr_forms: set[str] = field(default_factory=set)
    logins: set[str] = field(default_factory=set)

    def build(self) -> "Person":
        info = N.parse_display(self.display)
        self.first = self.first or info["first"]
        self.last = self.last or info["last"]
        self.patronymic = self.patronymic or info["patronymic"]
        if self.gender == "u":
            self.gender = info["gender"]
        if self.is_me:  # Уменьшительное из профиля — это имя, а не уникальное прозвище: так зовут и тёзок
            for n in list(self.nicks):
                w = N.norm(n.strip())
                if " " in w:
                    continue
                if w in N.FIRST_FORMS or w + "я" in N.FIRST_NAMES or w + "а" in N.FIRST_NAMES \
                        or (w.endswith("ь") and w[:-1] + "я" in N.FIRST_NAMES):
                    self.nicks.discard(n)
                    self.first_forms.add(w)
                    nom = N.FIRST_FORMS.get(w)
                    if nom:
                        self.first_forms |= N.all_forms(N.decline_first(nom, self.gender if self.gender != "u" else "m"))
        if self.first:
            self.first_forms |= N.all_forms(N.decline_first(self.first, self.gender))
            for d in N.diminutives(self.first):
                self.first_forms |= N.all_forms(N.decline_first(d, self.gender))
        if self.patronymic:
            self.patr_forms |= N.all_forms(N.decline_patronymic(self.patronymic, self.gender))
        if self.last and len(self.last) > 1:
            if re.search(r"[А-Яа-яЁё]", self.last):
                self.last_forms |= N.all_forms(N.decline_surname(self.last, self.gender))
                self.logins |= N.translit(self.last) if len(self.last) >= 4 else set()
            else:
                self.last_forms.add(N.norm(self.last))
        self.logins |= N.login_variants(self.first, self.last)
        self.logins = {x for x in self.logins if len(x) >= 4}
        return self


class Registry:
    def __init__(self, keep: list[str] | None = None):
        self.people: dict[str, Person] = {}
        self.keep = {N.norm(k) for k in keep or [] if k}
        self.me: Person | None = None
        self._ready = False

    # ------------------------------------------------------------ наполнение
    def add(self, key: str, display: str, **kw) -> Person | None:
        display = (display or "").strip()
        if not display or BOT_RE.search(display) or TEAM_RE.search(display):
            return None
        p = self.people.get(key)
        if p is None:
            p = Person(key=key, display=display, **kw)
            self.people[key] = p
        else:
            for k, v in kw.items():
                if isinstance(v, set):
                    getattr(p, k).update(v)
                elif v and not getattr(p, k):
                    setattr(p, k, v)
        self._ready = False
        return p

    def finish(self) -> "Registry":
        if self._ready:
            return self
        self.by_display: dict[str, str] = {}
        self.first: dict[str, set[str]] = {}
        self.last: dict[str, set[str]] = {}
        self.patr: dict[str, set[str]] = {}
        self.nick: dict[str, str] = {}
        self.login: dict[str, str] = {}
        self.username: dict[str, str] = {}
        for p in self.people.values():
            p.build()
            one_name = " " not in p.display.strip() and N.norm(p.display.strip()) in N.FIRST_FORMS
            if len(p.display) >= 3 and N.norm(p.display) not in self.keep and not one_name:
                # Уменьшительное одним словом — не отображаемое имя, а просто имя: тёзок разбирает поиск по словам
                self.by_display.setdefault(p.display, p.key)
            for f in p.first_forms:
                if _name_word(f):
                    self.first.setdefault(f, set()).add(p.key)
            for f in p.last_forms:
                if len(f) >= 3 and f not in self.keep and _name_word(f):
                    self.last.setdefault(f, set()).add(p.key)
            for f in p.patr_forms:
                self.patr.setdefault(f, set()).add(p.key)
            for n in p.nicks:
                if len(n) >= 2 and N.norm(n) not in self.keep and _name_word(n):
                    self.nick[N.norm(n)] = p.key
            for u in p.usernames:
                if u:
                    self.username[u.lower().lstrip("@")] = p.key
            for lg in p.logins:
                if lg not in self.keep:
                    self.login.setdefault(lg, p.key)
        self._display_re = None
        if self.by_display:
            alts = sorted(self.by_display, key=len, reverse=True)
            self._display_re = re.compile(
                r"(?<![\wА-Яа-яЁё@])(" + "|".join(re.escape(a) for a in alts) + r")(?![\wА-Яа-яЁё])")
        self._ready = True
        return self

    @property
    def display_re(self):
        self.finish()
        return self._display_re


def build_registry(cfg, store=None) -> Registry:
    """Реестр из базы и настроек. Без базы — только владелец и ручные записи."""
    priv = (cfg.get("privacy") if hasattr(cfg, "get") else {}) or {}
    reg = Registry(keep=list(priv.get("keep") or []) + ["Maverick", "Telegram", "OpsDigest"])
    prof = (cfg.get("profile") if hasattr(cfg, "get") else {}) or {}
    me_info: dict = {}
    if store is not None:
        try:
            me_info = json.loads(store.kv_get("me") or "{}")
        except (ValueError, TypeError):
            me_info = {}
    me_display = me_info.get("name") or prof.get("name") or ""
    if me_display:
        me = reg.add(f"u{me_info.get('id')}" if me_info.get("id") else "me", me_display, is_me=True,
                     usernames={me_info["username"]} if me_info.get("username") else set(),
                     nicks={a for a in prof.get("aliases") or [] if a} | ({prof["name"]} if prof.get("name") else set()))
        reg.me = me
    if store is not None:
        try:
            rows = store.db.execute(
                """SELECT m.sender_id AS sid, m.sender AS name, c.kind AS kind, c.title AS chat_title
                   FROM messages m LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id
                   WHERE m.is_out=0 AND m.sender IS NOT NULL AND m.sender!=''
                   GROUP BY m.sender_id, m.sender""").fetchall()
        except Exception:  # noqa: BLE001 — нет таблиц (пустая база)
            rows = []
        for r in rows:
            name = r["name"]
            if r["kind"] == "channel" and name == r["chat_title"]:
                continue  # пост канала: «автор» — сам канал
            key = f"u{r['sid']}" if r["sid"] else "n" + N.norm(name)
            if reg.me and key == reg.me.key:
                continue
            reg.add(key, name)
        try:
            for r in store.db.execute("SELECT chat_id, title, username FROM chats WHERE kind='private'").fetchall():
                if r["title"]:
                    reg.add(f"u{r['chat_id']}", r["title"], usernames={r["username"]} if r["username"] else set())
        except Exception:  # noqa: BLE001
            pass
    for uid, p in ((cfg.get("people") if hasattr(cfg, "get") else None) or {}).items():  # заметки о людях
        if not isinstance(p, dict):
            continue
        aliases = {str(a).strip() for a in p.get("aliases") or [] if str(a).strip()}
        key = f"u{uid}"
        if reg.me and key == reg.me.key:
            continue
        if key in reg.people:
            reg.people[key].nicks |= aliases
        elif (p.get("name") or "").strip():
            reg.add(key, p["name"].strip(), nicks=aliases,
                    usernames={p["username"]} if p.get("username") else set())
    for i, p in enumerate(priv.get("people") or []):
        if not isinstance(p, dict) or not (p.get("name") or "").strip():
            continue
        name = p["name"].strip()
        existing = next((x for x in reg.people.values() if N.norm(x.display) == N.norm(name)), None)
        aliases = {a.strip() for a in p.get("aliases") or [] if str(a).strip()}
        if existing:
            existing.nicks |= aliases
        else:
            reg.add(f"manual{i}", name, nicks=aliases)
    return reg.finish()
