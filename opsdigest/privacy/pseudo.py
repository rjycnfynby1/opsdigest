"""Обезличивание текста для внешней модели и обратная подстановка в её ответе.

Session — один запрос к модели: своя карта «код → человек» со случайными номерами (privacy.Privacy.begin заводит
новую на каждый запрос). Внутри запроса один человек — один код, в соседнем запросе у него уже другой номер, поэтому по
журналам модели нельзя сшить человека между запросами и заданиями. Карта живёт только в памяти, пока идёт запрос.

Люди → «Ч417» (пользователь — тоже случайный номер, модели он назван в описании «О пользователе»), телефоны → «Тел…»,
почты → «Почта…». В ответе коды заменяются обратно, имя ставится в падеж по предлогу или глаголу перед кодом
(«с Ч417» → «с Иваном Петровым»). Без randomize (проверки) — по порядку: Ч0 (пользователь), Ч1, Ч2…
"""
from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass

from . import names as N
from .registry import Person, Registry

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:[-'][A-Za-zА-Яа-яЁё]+)*")
CYR_CAP = re.compile(r"^[А-ЯЁ]")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w+])(?:(?:\+7|8|7)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
                      r"|\+\d{1,3}[\s\-]?\(?\d{2,4}\)?[\s\-]?\d{2,4}[\s\-]?\d{2,4}(?:[\s\-]?\d{1,4})?)(?![\w])")
HANDLE_STOP = {"here", "all", "channel", "everyone", "admin", "admins"}
HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
TME_RE = re.compile(r"(?i)\b(?:https?://)?(?:t\.me|telegram\.me)/(?!c/|joinchat|\+|s/)([A-Za-z][A-Za-z0-9_]{3,31})\b")

# окончания фамилий для незнакомых людей (без реестра): только с заглавной и не в начале предложения
UNKNOWN_SURNAME_RE = re.compile(
    r"^[А-ЯЁ][а-яё]{2,}(?:(?:ов|ев|ёв|ин|ын)(?:а|у|ым|е|ой|ою|ы|ых)?|(?:ск|цк)(?:ий|ого|ому|им|ом|ая|ой|ую)"
    r"|енко|(?:юк|чук)(?:а|у|ом|е)?|(?:ян)(?:а|у|ом|е)?|швили|дзе)$")
UNKNOWN_STOP = {N.norm(x) for x in (
    "Ростов Ростова Ростове Ростову Саратов Саратова Саратове Тамбов Тамбова Псков Пскова Пскове Киров Кирова Кирове "
    "Королёв Королёва Кишинёв Кишинёва Калинин Калинина Пушкин Пушкина Пушкине Иванов Иваново Чехов Чехова Озёрск "
    "Российский Российская Российской Российского Российскую Московский Московская Московской Московского "
    "Петербургский Петербургская Ленинградский Ленинградская Казанский Казанская Уральский Уральская Сибирский "
    "Сибирская Европейский Европейская Невский Невского Невском Кутузовский Ленинский Ленинского Ленинском "
    "Мичуринский Вернадского Кировский Октябрьский Октябрьская Советский Советская Красногорск Ногинск Гагарин "
    "Гагарина Ленина Ленин Маркса Энгельса Горького Жуковский Жуковского Одинцово Пушкинская Пушкинской "
    "Тверской Тверская Арбатская Киевский Киевская Белорусский Белорусская Курский Курская Казанский "
    "Ярославский Ярославская Рижский Рижская Савеловский Павелецкий Павелецкая Новосибирский Новосибирская "
    "Самарский Самарская Самарской Самарского Башкирский Татарский Татарская Чувашский Удмуртский Приволжский "
    "Северный Южный Западный Восточный Центральный Дальневосточный Сколковский Сколково").split()}

# падеж по предлогу / глаголу перед кодом
PREP_CASE = {
    "у": "gen", "от": "gen", "для": "gen", "без": "gen", "до": "gen", "из": "gen", "около": "gen", "возле": "gen",
    "кроме": "gen", "после": "gen", "вместо": "gen", "среди": "gen", "против": "gen", "мимо": "gen", "со стороны": "gen",
    "к": "dat", "ко": "dat", "по": "dat", "благодаря": "dat", "навстречу": "dat",
    "с": "ins", "со": "ins", "над": "ins", "под": "ins", "перед": "ins", "между": "ins", "за": "ins",
    "о": "pre", "об": "pre", "обо": "pre", "при": "pre",
    "про": "acc", "через": "acc", "на": "acc",
}
VERB_CASE = [
    (re.compile(r"^(на)?пиш|^написа|^позвон|^звон|^ответ|^переда|^сообщ|^напомн|^отправ|^пересл|^перешл|^помо[гч]|"
                r"^сказ|^скаж|^дай|^дать|^даст|^поруч|^предлож|^показ|^зада[йт]|^адресова|^пожалова|^доложи|"
                r"^отпиш|^отпиc|^написал|^кин[ьу]|^скин"), "dat"),
    (re.compile(r"^попрос|^спрос|^позов|^позва|^уведом|^подключ|^пинг|^тегн|^отмет|^привлеч|^привлек|^добав|"
                r"^назнач|^поблагодар|^д[её]рн|^жд|^подожд|^попроб|^предупред|^информир|^проинформир|"
                r"^эскалир|^беспоко|^побеспоко|^тормош|^пнуть|^пни|^зов|^прос|^благодар|^поздрав|^видел|^увидел|"
                r"^подожд|^ожида|^застав|^убед|^понял|^поним|^слуша|^послуша|^нашл|^найт|^найд|^вызва|^вызыв|"
                r"^поддерж|^спраши|^опрос|^позов"), "acc"),
]
CONJ = {"и", "или", "либо", ","}
CODE_RE = re.compile(r"(?<![\wА-Яа-яЁё])Ч-?(\d{1,4})(?:-[а-яё]{1,3})?(?![\w])")
TCODE_RE = re.compile(r"(?<![\wА-Яа-яЁё])(Тел|Почта)(\d{1,4})(?![\w])")


@dataclass
class _Span:
    start: int
    end: int
    key: str


class Session:
    """Карта одного задания. Потокобезопасна (картинки описываются параллельно)."""

    def __init__(self, registry: Registry, me_label: str = "", randomize: bool = False):
        self.reg = registry.finish()
        self._rnd = random.SystemRandom() if randomize else None
        self._used: set[int] = set()
        self.codes: dict[str, str] = {}        # person key → «Ч7»
        self.people: dict[str, Person] = {}    # «Ч7» → Person
        self.extra: dict[str, str] = {}        # «Т1»/«E1» → исходное значение
        self.extra_codes: dict[str, str] = {}
        self.unknown: dict[str, Person] = {}
        self.stats = {"people": 0, "contacts": 0, "guard": 0}
        self._n = 0
        self._lock = threading.RLock()
        self.me_label = me_label
        if self.reg.me:
            c = self._next("Ч")
            self.codes[self.reg.me.key] = c
            self.people[c] = self.reg.me
        self.me_code = self.codes.get(self.reg.me.key) if self.reg.me else None

    def _next(self, kind: str) -> str:
        """Следующий код: по порядку (Ч0, Ч1…) или случайный (Ч417) — без повторов внутри запроса."""
        if self._rnd is None:
            if kind == "Ч":
                n = self._n
                self._n += 1
            else:
                n = sum(1 for k in self.extra if k.startswith(kind)) + 1
            return f"{kind}{n}"
        lo, hi = (100, 999) if len(self._used) < 700 else (1000, 9999)
        while True:
            n = self._rnd.randint(lo, hi)
            if n not in self._used:
                self._used.add(n)
                return f"{kind}{n}"

    # ------------------------------------------------------------ коды
    def code(self, key: str, person: Person | None = None) -> str:
        with self._lock:
            c = self.codes.get(key)
            if c:
                return c
            c = self._next("Ч")
            self.codes[key] = c
            self.people[c] = person or self.reg.people.get(key) or self.unknown.get(key) or Person(key, key)
            return c

    def _extra(self, kind: str, value: str) -> str:
        with self._lock:
            c = self.extra_codes.get(value)
            if c:
                return c
            c = self._next(kind)
            self.extra[c] = value
            self.extra_codes[value] = c
            return c

    def _unknown(self, display: str) -> str:
        key = "x:" + N.norm(display)
        with self._lock:
            p = self.unknown.get(key)
            if p is None:
                p = Person(key=key, display=display, unknown=True)
                self.unknown[key] = p
        return self.code(key, p)

    # ------------------------------------------------------------ туда
    def apply(self, text: str) -> str:
        if not text:
            return text
        reg = self.reg
        # 1. почты, телефоны, ссылки на профили, @ники
        text = EMAIL_RE.sub(lambda m: self._email(m.group(0)), text)
        text = PHONE_RE.sub(lambda m: self._phone(m.group(0)), text)
        text = TME_RE.sub(lambda m: self._handle(m.group(1), link=True), text)
        text = HANDLE_RE.sub(lambda m: self._handle(m.group(1)), text)
        # 2. имена в точности как в Telegram (с эмодзи, латиницей, должностями в скобках)
        if reg.display_re is not None:
            text = reg.display_re.sub(lambda m: self._count(self.code(reg.by_display[m.group(1)])), text)
        # 3. имена, фамилии, отчества, уменьшительные, логины, прозвища — по словам
        return self._words(text)

    def _count(self, code: str) -> str:
        self.stats["people"] += 1
        return code

    def _email(self, e: str) -> str:
        self.stats["contacts"] += 1
        return self._extra("Почта", e)

    def _phone(self, p: str) -> str:
        digits = re.sub(r"\D", "", p)
        if len(digits) < 10 or len(digits) > 15:
            return p
        self.stats["contacts"] += 1
        return self._extra("Тел", p)

    def _handle(self, h: str, link: bool = False) -> str:
        if h.lower() in HANDLE_STOP:
            return "@" + h
        key = self.reg.username.get(h.lower())
        if key:
            return self._count(self.code(key))
        if link:
            return f"t.me/{h}"  # публичный канал/бот — не человек
        return self._count(self._unknown("@" + h))

    def _blocks_senders(self, text: str) -> list[tuple[int, set[str]]]:
        """Для разрешения тёзок: в каком блоке (чате) кто пишет — по строкам «#12 [10:00] Ч5: …»."""
        out: list[tuple[int, set[str]]] = []
        cur: set[str] = set()
        start = 0
        for m in re.finditer(r"(?m)^(===[^\n]*|#\d+ \[[^\]]*\] (Ч\d+)[:\s])", text):
            if m.group(1).startswith("==="):
                out.append((start, cur))
                cur, start = set(), m.start()
            elif m.group(2):
                p = self.people.get(m.group(2))
                if p:
                    cur.add(p.key)
        out.append((start, cur))
        return out

    def _line_senders(self, text: str) -> tuple[list[int], list[str | None]]:
        starts, keys = [0], [None]
        for m in re.finditer(r"(?m)^#\d+ \[[^\]]*\] (Ч\d+)[:\s]", text):
            p = self.people.get(m.group(1))
            starts.append(m.start())
            keys.append(p.key if p else None)
        for m in re.finditer(r"(?m)^(?!#\d+ \[)", text):
            if m.start() not in starts:
                starts.append(m.start())
                keys.append(None)
        order = sorted(range(len(starts)), key=lambda i: starts[i])
        return [starts[i] for i in order], [keys[i] for i in order]

    def _words(self, text: str) -> str:
        reg = self.reg
        toks = [(m.start(), m.end(), m.group(0)) for m in WORD_RE.finditer(text)]
        blocks = self._blocks_senders(text)
        self._ls, self._lk = self._line_senders(text)
        spans: list[_Span] = []
        i = 0
        while i < len(toks):
            s, e, w = toks[i]
            nw = N.norm(w)
            if nw in reg.keep or re.fullmatch(r"(?:Ч|Тел|Почта)\d*", w):
                i += 1
                continue
            got = self._match_at(text, toks, i, blocks)
            if got:
                n, key, person = got
                spans.append(_Span(toks[i][0], toks[i + n - 1][1], key))
                if person is not None:
                    with self._lock:
                        person = self.unknown.setdefault(key, person) if person.unknown else person
                    if key not in self.codes:
                        self.code(key, person)
                i += n
                continue
            i += 1
        # второй проход: незнакомые фамилии, найденные в середине предложения, — и в начале предложений
        forms = {f: k for k, p in self.unknown.items() if p.last for f in N.all_forms(N.decline_surname(p.last))}
        taken = [(sp.start, sp.end) for sp in spans]
        if forms:
            for s, e, w in toks:
                if w[:1].isupper() and N.norm(w) in forms and not any(a <= s < b for a, b in taken):
                    spans.append(_Span(s, e, forms[N.norm(w)]))
            spans.sort(key=lambda sp: sp.start)
        out = []
        pos = 0
        for sp in spans:
            out.append(text[pos:sp.start])
            out.append(self._count(self.code(sp.key)))
            pos = sp.end
        out.append(text[pos:])
        return "".join(out)

    @staticmethod
    def _sentence_start(text: str, start: int) -> bool:
        """Слово стоит в начале предложения или сообщения (там заглавная буква ничего не значит)."""
        i = start - 1
        while i >= 0 and text[i] in " \t«\"'(—-":
            i -= 1
        return i < 0 or text[i] in ".!?…:\n⏎"

    def _initial_after(self, text: str, end: int) -> int:
        """«Петров И.» / «Петров И.С.» — длина инициалов после слова (0 — нет)."""
        m = re.match(r"\s+[А-ЯЁA-Z]\.(?:\s?[А-ЯЁA-Z]\.)?", text[end:end + 8])
        return len(m.group(0)) if m else 0

    def _resolve(self, keys: set[str], pos: int, blocks, first_only: bool = False) -> str | None:
        """Один человек из нескольких с одинаковым именем: тот, кто пишет в этом чате, или уже упомянутый.

        first_only — упоминание только по имени: автор сообщения сам себя по имени не зовёт, его исключаем."""
        if first_only and getattr(self, "_ls", None):
            import bisect
            i = bisect.bisect_right(self._ls, pos) - 1
            author = self._lk[i] if i >= 0 else None
            if author in keys:
                keys = keys - {author}
                if not keys:
                    return None
        if len(keys) == 1:
            return next(iter(keys))
        here: set[str] = set()
        for start, senders in blocks:
            if start <= pos:
                here = senders
        cand = keys & here
        if len(cand) == 1:
            return next(iter(cand))
        me = self.reg.me.key if self.reg.me else None
        mentioned = (keys & set(self.codes)) - {me}  # пользователь в карте всегда — это не «уже упомянут»
        if len(mentioned) == 1:
            return next(iter(mentioned))
        return None

    def _maybe_me(self, word: str, nw: str) -> tuple[str, Person]:
        """Упоминание только по имени, как у пользователя: отдельный код «он или тёзка» (не код пользователя)."""
        nom = N.FIRST_FORMS.get(nw, nw)
        key = "?me:" + nom
        with self._lock:
            p = self.unknown.get(key)
            if p is None:
                disp = nom[:1].upper() + nom[1:]
                p = Person(key=key, display=disp, first=disp, unknown=True, maybe_me=True,
                           gender=self.reg.me.gender if self.reg.me else "u")
                self.unknown[key] = p
        return key, p

    def maybe_me_codes(self) -> list[tuple[str, str]]:
        """[(код, имя)] — кого в этом запросе назвали только по имени пользователя."""
        return [(c, p.display) for c, p in self.people.items() if getattr(p, "maybe_me", False)]

    @staticmethod
    def _new(nom: str) -> tuple[str, Person]:
        disp = nom[:1].upper() + nom[1:]
        key = "x:" + N.norm(nom)
        return key, Person(key=key, display=disp, unknown=True)

    def _match_at(self, text: str, toks, i: int, blocks):  # noqa: C901 — набор правил по порядку
        """(сколько слов занято, ключ человека, Person для новых) или None."""
        reg = self.reg
        s, e, w = toks[i]
        nw = N.norm(w)
        cap = w[:1].isupper()
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        adjacent = nxt is not None and re.fullmatch(r"[ \t]+", text[e:nxt[0]] or "x") is not None
        nn = N.norm(nxt[2]) if nxt else ""
        ncap = bool(nxt) and nxt[2][:1].isupper()
        amb = nw in N.AMB_FORMS
        start = self._sentence_start(text, s)
        name_ok = not amb or (cap and (not start or text[e:e + 1] == ","))
        # --- два слова: Имя Фамилия / Фамилия Имя / Имя Отчество (известные люди)
        if adjacent:
            if nw in reg.first and nn in reg.last and ncap:
                both = reg.first[nw] & reg.last[nn]
                if both:
                    return 2, self._resolve(both, s, blocks) or next(iter(both)), None
            if cap and nw in reg.last and nn in reg.first:
                both = reg.last[nw] & reg.first[nn]
                if both:
                    return 2, self._resolve(both, s, blocks) or next(iter(both)), None
            if nw in reg.first and nn in reg.patr:
                both = reg.first[nw] & reg.patr[nn]
                if both:
                    return 2, self._resolve(both, s, blocks) or next(iter(both)), None
        # --- фамилия известного человека — только с заглавной
        if cap and nw in reg.last:
            key = self._resolve(reg.last[nw], s, blocks)
            if key:
                return 1, key, None
            k, p = self._new(w)
            return 1, k, p
        # --- прозвища из реестра, логины, ники без @
        if nw in reg.nick and (cap or len(nw) >= 4):
            return 1, reg.nick[nw], None
        low = w.lower()
        if len(low) >= 4 and re.fullmatch(r"[a-z][a-z0-9._]*", low):
            if low in reg.login:
                return 1, reg.login[low], None
            if low in reg.username and ("_" in low or re.search(r"\d", low) or len(low) >= 6):
                return 1, reg.username[low], None
        # --- имя или уменьшительное известного человека
        if nw in reg.first and name_ok:
            me = reg.me.key if reg.me else None
            if me in reg.first[nw]:  # так зовут и пользователя: он это или тёзка — решает модель по контексту
                return (1, *self._maybe_me(w, nw))
            key = self._resolve(reg.first[nw], s, blocks, first_only=True)
            if key:
                return 1, key, None
            k, p = self._new(N.FIRST_FORMS.get(nw, nw))  # тёзки — общий код «Иван (?)»
            return 1, k, p
        # --- незнакомые люди: имя из словаря (+ фамилия рядом), инициалы + фамилия
        if nw in N.FIRST_FORMS and nw not in reg.keep and name_ok:
            nom = N.FIRST_FORMS[nw]
            if adjacent and ncap and nn not in N.FIRST_FORMS and UNKNOWN_SURNAME_RE.match(nxt[2]) \
                    and nn not in UNKNOWN_STOP:
                k, p = self._new(f"{w} {nxt[2]}")
                p.display = f"{w[:1].upper() + w[1:]} {nxt[2]}"
                return 2, k, p
            k, p = self._new(nom)
            return 1, k, p
        if cap and len(nw) >= 5 and nw not in UNKNOWN_STOP and UNKNOWN_SURNAME_RE.match(w):
            ini = self._initial_after(text, e)
            before = re.search(r"[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.\s?)?$", text[max(0, s - 6):s])
            if ini or before or not start:
                lemma = surname_lemma(w)
                k, p = self._new(lemma)
                p.display, p.last = lemma, lemma
                return 1, k, p
        return None

    # ------------------------------------------------------------ страховка
    def guard(self, text: str) -> str:
        """Вторая проверка: не осталось ли известных фамилий, отображаемых имён и ников в любом регистре."""
        reg = self.reg

        def fix(m: re.Match) -> str:
            w = m.group(0)
            nw = N.norm(w)
            key = None
            if nw in reg.last and len(nw) >= 5 and not re.search(r"(ый|ий|ой|ая|ого|ому|ую)$", nw):
                key = next(iter(reg.last[nw]))
            elif w.lower() in reg.username and len(w) >= 5:
                key = reg.username[w.lower()]
            elif w.lower() in reg.login and len(w) >= 6:
                key = reg.login[w.lower()]
            if key is None or nw in reg.keep:
                return w
            self.stats["guard"] += 1
            return self.code(key)
        return WORD_RE.sub(fix, text)

    def outgoing(self, text: str) -> str:
        return self.guard(self.apply(text))

    def stats_line(self) -> str:
        return (f"обезличено: людей {len([p for p in self.people.values() if not p.is_me])}, упоминаний "
                f"{self.stats['people']}, контактов {self.stats['contacts']}"
                + (f", страховка {self.stats['guard']}" if self.stats["guard"] else ""))

    # ------------------------------------------------------------ обратно
    def restore(self, text: str, json_safe: bool = True) -> str:
        if not text:
            return text
        last_case: dict[int, str] = {}

        def person_name(code: str, case: str) -> str:
            p = self.people.get(code)
            if p is None:
                return f"неизвестный ({code})"
            if p.is_me and self.me_label:
                disp = self.me_label
            else:
                disp = p.display
            if case != "nom" and (not p.unknown or p.last or N.norm(disp) in N.FIRST_NAMES or " " in disp):
                disp = N.inflect_display(disp, case)
            if json_safe:
                disp = disp.replace("\\", "/").replace('"', "″")
            return disp

        def repl(m: re.Match) -> str:
            code = f"Ч{int(m.group(1))}"
            case = _case_before(text, m.start(), last_case)
            last_case[m.end()] = case
            return person_name(code, case)
        out = CODE_RE.sub(repl, text)

        def repl_t(m: re.Match) -> str:
            v = self.extra.get(f"{m.group(1)}{int(m.group(2))}")
            return v if v is not None else m.group(0)
        return TCODE_RE.sub(repl_t, out)

    def describe(self) -> list[dict]:
        """Карта задания для окна: код → кто (локально, не уходит в модель)."""
        return [{"code": c, "name": p.display, "unknown": p.unknown, "me": p.is_me}
                for c, p in sorted(self.people.items(), key=lambda x: int(x[0][1:]))]


def surname_lemma(w: str) -> str:
    """Начальная форма незнакомой фамилии по падежной: «Сидорова»/«Сидорову» → «Сидоров», «Невского» → «Невский»."""
    m = re.match(r"^(.+?(?:ов|ев|ёв|ин|ын))(?:а|у|ым|е|ой|ою|ы|ых)?$", w)
    if m:
        return m.group(1)
    m = re.match(r"^(.+?(?:ск|цк))(?:ий|ого|ому|им|ом|ая|ой|ую)$", w)
    if m:
        return m.group(1) + "ий"
    m = re.match(r"^(.+?(?:юк|чук|ян))(?:а|у|ом|е)?$", w)
    if m:
        return m.group(1)
    return w


def _case_before(text: str, pos: int, last_case: dict[int, str]) -> str:
    before = text[max(0, pos - 40):pos]
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+|,", before)
    if not words:
        return "nom"
    w1 = words[-1].lower()
    if w1 in CONJ:
        # «с Ч7 и Ч8» — падеж как у предыдущего кода
        m = re.search(r"(?:[ЧчCc][\s\-]?\d{1,4})\s*(?:,|и|или|либо)\s*$", before)
        if m:
            end = pos - (len(before) - m.end()) - len(re.search(r"\s*(?:,|и|или|либо)\s*$", before).group(0))
            for k, v in last_case.items():
                if abs(k - end) <= 2:
                    return v
        return "nom"
    if len(words) >= 2 and f"{words[-2].lower()} {w1}" in PREP_CASE:
        return PREP_CASE[f"{words[-2].lower()} {w1}"]
    if w1 in PREP_CASE:
        return PREP_CASE[w1]
    for rx, case in VERB_CASE:
        if rx.search(w1):
            return case
    return "nom"
