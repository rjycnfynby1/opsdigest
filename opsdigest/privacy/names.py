"""Русские имена без внешних библиотек: склонение ФИО по падежам, уменьшительные формы, транслит, логины.

Правила склонения — упрощённый набор в духе библиотеки Petrovich: покрывают обычные имена, отчества и фамилии
(-ов/-ев/-ин, -ский/-цкий, -ая, на согласную, на -а/-я). Несклоняемые (-ко, -их/-ых, -о/-е/-и/-у/-ю, иностранные)
остаются как есть. Этого достаточно, чтобы находить имя в тексте в любом падеже и ставить имя в нужный падеж в отчёте.
"""
from __future__ import annotations

import re

CASES = ("nom", "gen", "dat", "acc", "ins", "pre")
VOWELS = set("аеёиоуыэюя")
SIBILANT = set("жшчщц")
VELAR = set("гкх")

# ---------------------------------------------------------------- имена и уменьшительные
# полное имя: (род, уменьшительные). Уменьшительные склоняются как обычные имена.
_NAMES = {
    # мужские
    "александр": ("m", "саша саня шура санёк санек алекс сашка"),
    "алексей": ("m", "лёша леша алёша алеша лёха леха лёшка"),
    "анатолий": ("m", "толя толик"),
    "андрей": ("m", "андрюша андрюха"),
    "антон": ("m", "антоша тоша"),
    "аркадий": ("m", "аркаша"),
    "арсений": ("m", "сеня арсюша"),
    "артём": ("m", "тёма тема артёмка"),
    "артем": ("m", "тема"),
    "артур": ("m", ""),
    "богдан": ("m", "богдаша"),
    "борис": ("m", "боря"),
    "вадим": ("m", "вадик дима"),
    "валентин": ("m", "валя"),
    "валерий": ("m", "валера"),
    "василий": ("m", "вася"),
    "виктор": ("m", "витя"),
    "виталий": ("m", "виталик"),
    "владимир": ("m", "вова володя вовка вован"),
    "владислав": ("m", "влад владик слава"),
    "всеволод": ("m", "сева"),
    "вячеслав": ("m", "слава славик"),
    "геннадий": ("m", "гена"),
    "георгий": ("m", "жора гоша"),
    "герман": ("m", "гера"),
    "глеб": ("m", ""),
    "григорий": ("m", "гриша"),
    "давид": ("m", "дава"),
    "даниил": ("m", "даня данила"),
    "данил": ("m", "даня"),
    "денис": ("m", "дэн"),
    "дмитрий": ("m", "дима митя димон димка"),
    "евгений": ("m", "женя жека"),
    "егор": ("m", "егорка"),
    "иван": ("m", "ваня ванёк ванек ванюша"),
    "игорь": ("m", "игорёк гоша"),
    "илья": ("m", "илюша"),
    "кирилл": ("m", "киря кирюша"),
    "константин": ("m", "костя костян"),
    "лев": ("m", "лёва лева"),
    "леонид": ("m", "лёня леня"),
    "максим": ("m", "макс максимка"),
    "марк": ("m", ""),
    "матвей": ("m", "мотя"),
    "михаил": ("m", "миша мишаня"),
    "никита": ("m", "никитос"),
    "николай": ("m", "коля колян"),
    "олег": ("m", "олежка"),
    "павел": ("m", "паша пашка"),
    "пётр": ("m", "петя"),
    "петр": ("m", "петя"),
    "роберт": ("m", ""),
    "родион": ("m", "родя"),
    "роман": ("m", "рома ромка"),
    "руслан": ("m", ""),
    "святослав": ("m", "свят слава"),
    "семён": ("m", "сёма сема"),
    "семен": ("m", "сема"),
    "сергей": ("m", "серёжа сережа серёга серега серж"),
    "станислав": ("m", "стас слава"),
    "степан": ("m", "стёпа степа"),
    "тимофей": ("m", "тима"),
    "тимур": ("m", "тима"),
    "фёдор": ("m", "федя"),
    "федор": ("m", "федя"),
    "филипп": ("m", "филя"),
    "эдуард": ("m", "эдик"),
    "юрий": ("m", "юра юрка"),
    "ярослав": ("m", "ярик слава"),
    "ринат": ("m", ""), "ренат": ("m", ""), "рустам": ("m", ""), "айдар": ("m", ""), "азат": ("m", ""),
    "тагир": ("m", ""), "марат": ("m", ""), "ильдар": ("m", ""), "ильнур": ("m", ""), "радик": ("m", ""),
    "эльдар": ("m", ""), "алмаз": ("m", ""), "рамиль": ("m", ""), "дамир": ("m", ""), "арслан": ("m", ""),
    "тимерхан": ("m", ""), "шамиль": ("m", ""), "эмиль": ("m", ""), "оскар": ("m", ""), "ян": ("m", ""),
    "вениамин": ("m", "веня"), "виль": ("m", ""), "захар": ("m", ""), "назар": ("m", ""), "платон": ("m", ""),
    "прохор": ("m", ""), "савелий": ("m", "савва"), "тарас": ("m", ""), "игнат": ("m", ""), "ефим": ("m", "фима"),
    "аркадий_": ("m", ""),
    # женские
    "александра": ("f", "саша шура сашенька"),
    "алёна": ("f", "алёнка"),
    "алена": ("f", ""),
    "алина": ("f", ""),
    "алиса": ("f", ""),
    "алла": ("f", ""),
    "анастасия": ("f", "настя настёна"),
    "ангелина": ("f", "геля"),
    "анна": ("f", "аня анюта нюра"),
    "антонина": ("f", "тоня"),
    "валентина": ("f", "валя"),
    "валерия": ("f", "лера"),
    "варвара": ("f", "варя"),
    "вера": ("f", "верочка"),
    "вероника": ("f", "ника"),
    "виктория": ("f", "вика"),
    "галина": ("f", "галя"),
    "дарья": ("f", "даша"),
    "диана": ("f", ""),
    "дина": ("f", ""),
    "ева": ("f", ""),
    "евгения": ("f", "женя"),
    "екатерина": ("f", "катя катюша"),
    "елена": ("f", "лена леночка"),
    "елизавета": ("f", "лиза"),
    "жанна": ("f", ""),
    "зинаида": ("f", "зина"),
    "зоя": ("f", ""),
    "инна": ("f", ""),
    "ирина": ("f", "ира иришка"),
    "камила": ("f", ""), "камилла": ("f", ""),
    "карина": ("f", ""),
    "кира": ("f", ""),
    "кристина": ("f", ""),
    "ксения": ("f", "ксюша"),
    "лариса": ("f", "лара"),
    "лидия": ("f", "лида"),
    "лилия": ("f", "лиля"),
    "любовь": ("f", "люба"),
    "людмила": ("f", "люда мила"),
    "маргарита": ("f", "рита"),
    "марина": ("f", ""),
    "мария": ("f", "маша маруся машенька"),
    "милана": ("f", ""),
    "надежда": ("f", "надя"),
    "наталья": ("f", "наташа"),
    "наталия": ("f", "наташа"),
    "нина": ("f", ""),
    "оксана": ("f", ""),
    "олеся": ("f", ""),
    "ольга": ("f", "оля олечка"),
    "полина": ("f", "поля"),
    "раиса": ("f", "рая"),
    "регина": ("f", ""),
    "светлана": ("f", "света"),
    "софия": ("f", "соня"),
    "софья": ("f", "соня"),
    "таисия": ("f", "тася"),
    "тамара": ("f", ""),
    "татьяна": ("f", "таня танюша"),
    "ульяна": ("f", "уля"),
    "юлия": ("f", "юля"),
    "яна": ("f", ""),
    "эльвира": ("f", ""), "эльмира": ("f", ""), "гульнара": ("f", ""), "лейла": ("f", ""), "алсу": ("f", ""),
    "динара": ("f", ""), "альбина": ("f", ""), "венера": ("f", ""), "лиана": ("f", ""), "майя": ("f", ""),
}
# уменьшительные, которые одинаковы для мужчин и женщин
_UNISEX_DIM = {"саша", "женя", "валя", "слава", "шура"}

# имена, совпадающие с обычными словами: ищем только с заглавной буквы и не в начале предложения
AMBIGUOUS = {"вера", "надежда", "любовь", "лев", "слава", "мир", "марк", "роза", "лилия", "ника", "ян", "ева",
             "ким", "макс", "мила", "поля", "тема", "свят", "майя", "венера", "алмаз", "паша", "роман", "виктория",
             "оскар", "радик", "лена", "виль", "дина", "алла", "рая", "кира", "назар", "захар", "платон", "света",
             "гена", "рома", "соня", "жора", "валя", "коля", "тоня", "люба", "мир", "тема", "ника"}

FIRST_NAMES: dict[str, str] = {}          # имя или уменьшительное → род (m / f / u)
CANON: dict[str, set[str]] = {}           # уменьшительное → полные имена


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _build() -> None:
    for full, (g, dims) in _NAMES.items():
        full = full.rstrip("_")
        FIRST_NAMES[_norm(full)] = g
        for d in dims.split():
            dn = _norm(d)
            prev = FIRST_NAMES.get(dn)
            FIRST_NAMES[dn] = "u" if (prev and prev != g) or dn in _UNISEX_DIM else g
            CANON.setdefault(dn, set()).add(_norm(full))


_build()


FIRST_FORMS: dict[str, str] = {}   # любая падежная форма имени из словаря → именительный падеж
AMB_FORMS: set[str] = set()         # формы имён, совпадающих с обычными словами


def _build_forms() -> None:
    for nom, g in FIRST_NAMES.items():
        forms = decline_first(nom, g if g in ("m", "f") else "m")
        amb = nom in AMBIGUOUS or any(c in AMBIGUOUS for c in CANON.get(nom, ()))
        for f in forms.values():
            f = _norm(f)
            FIRST_FORMS.setdefault(f, nom)
            if amb:
                AMB_FORMS.add(f)


def diminutives(first: str) -> list[str]:
    """Уменьшительные формы полного имени (нормализованные: строчные, ё→е)."""
    n = _norm(first)
    for full, (_g, dims) in _NAMES.items():
        if _norm(full.rstrip("_")) == n:
            return [_norm(d) for d in dims.split()]
    return []


def is_first_name(word: str) -> bool:
    return _norm(word) in FIRST_NAMES


def gender_of(first: str) -> str:
    """m / f / u (неизвестно)."""
    n = _norm(first)
    g = FIRST_NAMES.get(n)
    if g in ("m", "f"):
        return g
    if g == "u":
        return "u"
    if PATRONYMIC_RE.search(n):
        return "f" if n.endswith(("вна", "чна", "ична")) else "m"
    return "u"


PATRONYMIC_RE = re.compile(r"(ович|евич|ич|овна|евна|ична|инична)$")
SURNAME_RE = re.compile(r"(ов|ев|ёв|ин|ын|ова|ева|ёва|ина|ына|ский|цкий|ская|цкая|ской|цкой|енко|ко|ук|юк|чук|ич|ян|ец|ой|ий|ых|их)$")


def guess_gender_surname(surname: str) -> str:
    s = _norm(surname)
    if re.search(r"(ова|ева|ина|ына|ская|цкая|ая)$", s):
        return "f"
    if re.search(r"(ов|ев|ин|ын|ский|цкий|ой|ый|ий)$", s):
        return "m"
    return "u"


# ---------------------------------------------------------------- склонение
def _cap(form: str, like: str) -> str:
    if like.isupper() and len(like) > 1:
        return form.upper()
    if like[:1].isupper():
        return form[:1].upper() + form[1:]
    return form


def _ins_o(stem_last: str) -> str:
    """Окончание творительного -ом/-ем после основы (после шипящих и ц — -ем)."""
    return "ем" if stem_last in SIBILANT else "ом"


def _decline_a(word: str) -> dict[str, str]:
    """Существительные/имена на -а (Никита, Ольга, Саша, Шуба)."""
    st = word[:-1]
    last = st[-1:] if st else ""
    gen = st + ("и" if last in VELAR or last in SIBILANT else "ы")
    ins = st + ("ей" if last in SIBILANT else "ой")
    return {"nom": word, "gen": gen, "dat": st + "е", "acc": st + "у", "ins": ins, "pre": st + "е"}


def _decline_ya(word: str) -> dict[str, str]:
    """На -я: Илья, Таня, Мария (-ия)."""
    st = word[:-1]
    if st.endswith("и"):  # Мария, Лилия
        return {"nom": word, "gen": st + "и", "dat": st + "и", "acc": st + "ю", "ins": st + "ей", "pre": st + "и"}
    if st.endswith("ь"):  # Илья, Наталья, Дарья
        return {"nom": word, "gen": st + "и", "dat": st + "е", "acc": st + "ю",
                "ins": st + ("ёй" if word.lower() in ("илья",) else "ей"), "pre": st + "е"}
    return {"nom": word, "gen": st + "и", "dat": st + "е", "acc": st + "ю", "ins": st + "ей", "pre": st + "е"}


def _decline_cons_m(word: str, animate: bool = True) -> dict[str, str]:
    """Мужские на согласную, -й, -ий, -ь (Иван, Андрей, Василий, Игорь, Ткач)."""
    w = word
    low = w.lower()
    special = {"павел": "павл", "лев": "льв", "пётр": "петр", "петр": "петр"}
    if low in special:
        st = special[low]
        st = _cap(st, w)
        return {"nom": w, "gen": st + "а", "dat": st + "у", "acc": st + "а", "ins": st + "ом", "pre": st + "е"}
    if low.endswith("ий"):
        st = w[:-2] + "и"
        return {"nom": w, "gen": st + "я", "dat": st + "ю", "acc": st + "я", "ins": st + "ем", "pre": st + "и"}
    if low.endswith("й"):
        st = w[:-1]
        return {"nom": w, "gen": st + "я", "dat": st + "ю", "acc": st + "я", "ins": st + "ем", "pre": st + "е"}
    if low.endswith("ь"):
        st = w[:-1]
        return {"nom": w, "gen": st + "я", "dat": st + "ю", "acc": st + "я", "ins": st + "ем", "pre": st + "е"}
    last = low[-1:]
    return {"nom": w, "gen": w + "а", "dat": w + "у", "acc": w + "а" if animate else w,
            "ins": w + _ins_o(last), "pre": w + "е"}


def _indecl(word: str) -> dict[str, str]:
    return {c: word for c in CASES}


def decline_first(name: str, gender: str = "u") -> dict[str, str]:
    low = _norm(name)
    if not re.fullmatch(r"[а-я]+", low):
        return _indecl(name)
    g = gender if gender in ("m", "f") else (FIRST_NAMES.get(low) or "u")
    if low.endswith("а"):
        return _decline_a(name)
    if low.endswith("я"):
        return _decline_ya(name)
    if low[-1] in "оеиуюэы":
        return _indecl(name)
    if g == "f":
        if low.endswith("ь") and low in ("любовь",):
            st = name[:-3] + "ви"
            return {"nom": name, "gen": st, "dat": st, "acc": name, "ins": name[:-3] + "вью", "pre": st}
        return _indecl(name)  # женские на согласную (Рашель, Нинель) — не склоняем
    return _decline_cons_m(name)


def decline_patronymic(p: str, gender: str = "u") -> dict[str, str]:
    low = _norm(p)
    if low.endswith(("вна", "чна")):
        return _decline_a(p)
    if low.endswith("ич"):
        return {"nom": p, "gen": p + "а", "dat": p + "у", "acc": p + "а", "ins": p + "ем", "pre": p + "е"}
    return _indecl(p)


def decline_surname(s: str, gender: str = "u") -> dict[str, str]:
    low = _norm(s)
    if not re.fullmatch(r"[а-я\-]+", low):
        return _indecl(s)
    if "-" in s:  # двойная фамилия: склоняем обе части
        parts = [decline_surname(x, gender) for x in s.split("-")]
        return {c: "-".join(p[c] for p in parts) for c in CASES}
    g = gender if gender in ("m", "f") else guess_gender_surname(s)
    if re.search(r"(ова|ева|ина|ына)$", low) and g != "m":
        st = s[:-1]
        return {"nom": s, "gen": st + "ой", "dat": st + "ой", "acc": st + "у", "ins": st + "ой", "pre": st + "ой"}
    if re.search(r"(ов|ев|ин|ын)$", low) and g != "f":
        return {"nom": s, "gen": s + "а", "dat": s + "у", "acc": s + "а", "ins": s + "ым", "pre": s + "е"}
    if re.search(r"(ская|цкая|ая)$", low):
        st = s[:-2]
        return {"nom": s, "gen": st + "ой", "dat": st + "ой", "acc": st + "ую", "ins": st + "ой", "pre": st + "ой"}
    if re.search(r"(ский|цкий|кий|гий|хий)$", low):
        st = s[:-2]
        return {"nom": s, "gen": st + "ого", "dat": st + "ому", "acc": st + "ого", "ins": st + "им", "pre": st + "ом"}
    if re.search(r"(ый|ой)$", low):
        st = s[:-2]
        last = _norm(st[-1:])
        ins = "им" if last in VELAR or last in SIBILANT else "ым"
        return {"nom": s, "gen": st + "ого", "dat": st + "ому", "acc": st + "ого", "ins": st + ins, "pre": st + "ом"}
    if re.search(r"(ко|ых|их|аго|яго|[оеиуюэ])$", low):
        return _indecl(s)
    if low.endswith("а"):
        return _decline_a(s)
    if low.endswith("я"):
        return _decline_ya(s)
    if g == "f":
        return _indecl(s)  # женская фамилия на согласную (Шмидт, Ткач) не склоняется
    if low[-1] in VOWELS:
        return _indecl(s)
    return _decline_cons_m(s)


def all_forms(forms: dict[str, str]) -> set[str]:
    return {_norm(v) for v in forms.values() if v}


# ---------------------------------------------------------------- разбор отображаемого имени
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-']*")


def parse_display(name: str) -> dict:
    """«Иван Петров», «Петров Иван Сергеевич», «Ivan Petrov», «Иван 🚀» → части имени.

    {"first", "last", "patronymic", "gender", "tokens"} — поля могут быть пустыми."""
    tokens = _TOKEN_RE.findall(name or "")
    first = last = patr = ""
    rest = []
    for t in tokens:
        n = _norm(t)
        if not first and n in FIRST_NAMES:
            first = t
        elif not patr and PATRONYMIC_RE.search(n) and len(n) > 5 and re.fullmatch(r"[а-я]+", n):
            patr = t
        else:
            rest.append(t)
    for t in rest:
        if not last and len(t) > 1 and t[:1].isupper():
            last = t
    if not first and not last and rest:
        last = rest[0]
    gender = "u"
    if first:
        gender = gender_of(first)
    if gender == "u" and patr:
        gender = gender_of(patr)
    if gender == "u" and last:
        gender = guess_gender_surname(last)
    return {"first": first, "last": last, "patronymic": patr, "gender": gender, "tokens": tokens}


def inflect_display(display: str, case: str) -> str:
    """Отображаемое имя в нужном падеже: «Иван Петров» → «Ивана Петрова». Нерусские — как есть."""
    if case == "nom" or not display:
        return display
    info = parse_display(display)
    if not re.search(r"[А-Яа-яЁё]", display):
        return display
    g = info["gender"]
    out = display
    for part, fn in ((info["first"], decline_first), (info["patronymic"], decline_patronymic),
                     (info["last"], decline_surname)):
        if part and re.fullmatch(r"[А-Яа-яЁё\-]+", part):
            form = fn(part, g).get(case, part)
            out = re.sub(rf"(?<![\wА-Яа-яЁё]){re.escape(part)}(?![\wА-Яа-яЁё])", form, out, count=1)
    return out


# ---------------------------------------------------------------- транслит и логины
_TR_BASE = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
            "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
            "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
            "э": "e", "ю": "iu", "я": "ia"}
_TR_ALT = {"й": "y", "х": "h", "ю": "yu", "я": "ya", "щ": "sch", "ц": "c", "ё": "yo", "ж": "j", "кс": "x"}


def translit(word: str) -> set[str]:
    """Несколько распространённых вариантов латиницы: паспортный и «бытовой»."""
    w = _norm(word)
    if not re.fullmatch(r"[а-я]+", w):
        return {w} if w else set()
    a = "".join(_TR_BASE.get(c, c) for c in w)
    b = "".join(_TR_ALT.get(c, _TR_BASE.get(c, c)) for c in w)
    c = b.replace("ks", "x")
    d = "".join({"х": "h", "ю": "yu", "я": "ya", "й": "y", "ц": "ts"}.get(ch, _TR_BASE.get(ch, ch)) for ch in w)
    return {x for x in (a, b, c, d) if x}


def login_variants(first: str, last: str) -> set[str]:
    """Типичные логины и начала почтовых адресов: ipetrov, petrov_i, i.petrov, ivan.petrov, petrovi."""
    out: set[str] = set()
    if not last or len(_norm(last)) < 4:
        return out
    lasts = translit(last) if re.search(r"[а-яё]", _norm(last)) else {_norm(last)}
    firsts = (translit(first) if re.search(r"[а-яё]", _norm(first)) else {_norm(first)}) if first else set()
    for L in lasts:
        if len(L) < 4:
            continue
        out.add(L)
        for F in firsts:
            if not F:
                continue
            i = F[0]
            out |= {i + L, L + i, f"{i}.{L}", f"{L}.{i}", f"{i}_{L}", f"{L}_{i}", f"{F}.{L}", f"{F}_{L}", F + L,
                    f"{L}.{F}", f"{L}_{F}"}
    return out


def norm(s: str) -> str:
    return _norm(s)


_build_forms()
