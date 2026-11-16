"""Обезличивание (имена → коды и обратно) и смягчение мата перед отправкой во внешнюю модель."""
import json

from opsdigest.llm.base import LLMClient
from opsdigest.privacy import Session, attach, build_registry, names as N, recent
from opsdigest.privacy.soften import has_obscene, soften
from opsdigest.storage import Chat, Message, Store


class Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _store():
    st = Store(":memory:")
    st.upsert_chat(Chat(chat_id=-1001, title="Авария биллинг INC0012345", kind="group"))
    st.upsert_chat(Chat(chat_id=555, title="Мария Кузнецова", kind="private"))
    st.add_messages([
        Message(-1001, 1, "2026-11-05T10:00:00+00:00", "Иван Петров", "текст", sender_id=101),
        Message(-1001, 2, "2026-11-05T10:01:00+00:00", "Мария Кузнецова", "текст", sender_id=555),
        Message(-1001, 3, "2026-11-05T10:02:00+00:00", "Олег 🚀", "текст", sender_id=102),
        Message(-1001, 4, "2026-11-05T10:03:00+00:00", "Zabbix Bot", "PROBLEM: disk", sender_id=103),
    ])
    st.kv_set("me", json.dumps({"id": 999, "name": "Александр Смирнов", "username": "asmirnov"}))
    return st


def _cfg(**priv):
    return Cfg(profile={"name": "Александр Смирнов", "aliases": ["Санёк"]}, privacy=priv)


TRANSCRIPT = """=== ЧАТ c1 | «Авария биллинг INC0012345» | группа ===
#1 [10:00] Иван Петров: Саша, глянь INC0012345, выгрузка по SLA пустая. Позвони Кузнецовой
#2 [10:01] Мария Кузнецова: Петров, я уже написала Маше из биллинга
#3 [10:02] Олег 🚀: ipetrov@corp.ru пишет, что Ивану Петрову некогда, звоните +7 912 345-67-89
#4 [10:03] Zabbix Bot: PROBLEM: disk full on db01
=== ЧАТ c2 | «Мария Кузнецова» | личный ===
#5 [10:04] Мария Кузнецова: Санёк, попроси И. Сидорова. Сидоров тоже ждёт. Слава богу, база поднялась."""

REAL = ["Иван", "Ивану", "Петров", "Мария", "Кузнецов", "Олег", "Саша", "Санёк", "Сидоров", "Маше",
        "ipetrov@corp.ru", "345-67-89", "Смирнов"]


def test_declension():
    assert N.decline_first("Иван")["gen"] == "Ивана"
    assert N.decline_first("Мария")["dat"] == "Марии"
    assert N.decline_first("Илья")["ins"] == "Ильёй"
    assert N.decline_first("Павел")["gen"] == "Павла"
    assert N.decline_surname("Петров", "m")["ins"] == "Петровым"
    assert N.decline_surname("Кузнецова", "f")["acc"] == "Кузнецову"
    assert N.decline_surname("Чайковский", "m")["dat"] == "Чайковскому"
    assert N.decline_surname("Шевченко", "m")["gen"] == "Шевченко"
    assert N.decline_surname("Ткач", "f")["gen"] == "Ткач"
    assert N.inflect_display("Иван Петров", "ins") == "Иваном Петровым"
    assert N.inflect_display("Мария Кузнецова", "gen") == "Марии Кузнецовой"
    assert N.inflect_display("Ivan Petrov", "gen") == "Ivan Petrov"
    assert "ipetrov" in N.login_variants("Иван", "Петров")


def test_outgoing_hides_people_and_keeps_work_terms():
    reg = build_registry(_cfg(), _store())
    s = Session(reg, me_label="Александр")
    out = s.outgoing(TRANSCRIPT)
    for w in REAL:
        assert w not in out, w
    for w in ("INC0012345", "SLA", "биллинг", "Zabbix Bot", "PROBLEM: disk full on db01", "Слава богу"):
        assert w in out, w
    codes = {d["name"]: d["code"] for d in s.describe()}
    assert codes["Александр Смирнов"] == "Ч0"
    # «Саша», «Санёк» — только имя, как у пользователя: отдельный код «он или тёзка», не код пользователя
    assert "Ч0" not in out
    mm = dict(s.maybe_me_codes())
    assert {n for n in mm.values()} >= {"Саша", "Санек"} or {n for n in mm.values()} >= {"Саша", "Санёк"}, mm
    assert all(c in out for c in mm)
    # один человек — один код во всём тексте
    assert out.count(codes["Иван Петров"]) >= 3
    # автор сообщения сам себя не зовёт: «Маше» — другой человек, а не Мария Кузнецова
    assert codes.get("Маша") and codes["Маша"] != codes["Мария Кузнецова"]
    # незнакомая фамилия в начале предложения — тем же кодом, что и с инициалом
    assert out.count(codes["Сидоров"]) == 2


def test_restore_with_cases_and_json_safety():
    reg = build_registry(_cfg(), _store())
    s = Session(reg, me_label="Александр")
    s.outgoing(TRANSCRIPT)
    c = {d["name"]: d["code"] for d in s.describe()}
    iv, mk = c["Иван Петров"], c["Мария Кузнецова"]
    ans = f'{{"title": "{iv} просит Ч0 позвонить {mk}", "details": "связаться с {iv} и {mk}; ответ от {mk}"}}'
    back = s.restore(ans)
    data = json.loads(back)
    assert data["title"] == "Иван Петров просит Александра позвонить Марии Кузнецовой"
    assert data["details"] == "связаться с Иваном Петровым и Марией Кузнецовой; ответ от Марии Кузнецовой"
    assert s.restore("Почта1, Тел1") == "ipetrov@corp.ru, +7 912 345-67-89"
    assert s.restore("Ч99 ответил") == "неизвестный (Ч99) ответил"
    assert s.restore("ключи c1 и c2 не трогаем") == "ключи c1 и c2 не трогаем"


def test_new_session_new_numbers():
    reg = build_registry(_cfg(), _store())
    a, b = Session(reg), Session(reg)
    a.outgoing("Олег написал Ивану Петрову")
    b.outgoing("Иван Петров написал Олегу")
    ca = {d["name"]: d["code"] for d in a.describe()}
    cb = {d["name"]: d["code"] for d in b.describe()}
    assert ca["Иван Петров"] != cb["Иван Петров"]


def test_manual_aliases_and_keep_words():
    cfg = _cfg(people=[{"name": "Иван Петров", "aliases": ["Палыч"]}], keep=["Петров"])
    reg = build_registry(cfg, _store())
    s = Session(reg)
    out = s.outgoing("Палыч сказал, что Петров-сервер упал")
    assert "Палыч" not in out
    assert "Петров" in out  # слово из «не трогать»


def test_digest_many_requests_restored():
    """Сводка из нескольких запросов (map по чатам + reduce): у каждого запроса свои коды, отчёт — с настоящими именами."""
    import re
    from datetime import datetime, timedelta, timezone

    from opsdigest.config import Config, DEFAULTS
    from opsdigest.digest import DigestBuilder
    from opsdigest.storage import iso
    st = _store()
    now = datetime.now(timezone.utc)
    st.add_messages([Message(-1001, 10, iso(now - timedelta(hours=1)), "Иван Петров", "Мария, позвони Олегу",
                             sender_id=101),
                     Message(555, 11, iso(now - timedelta(minutes=30)), "Мария Кузнецова", "Иван Петров просит звонок",
                             sender_id=555)])
    sent = []

    class Fake(LLMClient):
        def _complete(self, system, user, max_tokens):
            sent.append(user)
            codes = re.findall(r"#\d+ \[\d\d:\d\d\] (Ч\d+):", user)
            if "Сводки чатов" in user:
                c = re.findall(r"Ч\d+", user)
                return json.dumps({"headline": f"{c[0]} ждёт звонка", "top": []}, ensure_ascii=False)
            keys = re.findall(r"=== ЧАТ (c\d+)", user)
            return json.dumps({"chats": [{"chat": k, "summary": f"{codes[0]} написал" if codes else "",
                                          "importance": 3, "items": []} for k in keys]}, ensure_ascii=False)
    cfg = Config(json.loads(json.dumps(DEFAULTS)), __import__("pathlib").Path("."))
    cfg.data["digest"]["chunk_chars"] = 200  # каждый чат — отдельными запросами (частями)
    llm = Fake("maverick", {"type": "maverick"})
    data = DigestBuilder(cfg, st, llm, progress=lambda s: None, describe=False).build(now - timedelta(days=1), now)
    assert len(sent) >= 3
    assert all("Петров" not in u and "Кузнецов" not in u for u in sent)
    summaries = {c["title"]: c["summary"] for c in data["chats"]}
    assert summaries["Авария биллинг INC0012345"] == "Иван Петров написал"
    assert summaries["Мария Кузнецова"] == "Мария Кузнецова написал"
    assert data["overview"]["headline"].endswith("ждёт звонка") and "Ч" not in data["overview"]["headline"]
    assert data["meta"]["privacy"]["requests"] == len(sent)


def test_soften_examples():
    cases = {
        "Ч4: бля, опять наебнулась выгрузка по SLA, это пиздец":
            "Ч4: опять сломалась выгрузка по SLA, это серьёзная проблема",
        "сделали хуйню": "могли бы сделать лучше",
        "это полная хуйня": "это можно сделать лучше",
        "иди нахуй": "[резкий отказ]",
        "Ч7 — долбоёб": "Ч7 — человек, который мог бы работать лучше",
        "эта сука не отвечает": "этот человек, который мог бы работать лучше, не отвечает",
        "хуёвая выгрузка": "плохая выгрузка",
        "мы проебали дедлайн": "мы упустили дедлайн",
        "нахуя ты это сделал": "зачем ты это сделал",
        "п и з д е ц": "серьёзная проблема",
        "х*йня какая-то": "ерунда какая-то",
        "тупой скрипт": "неудачный скрипт",
        "ты тупой?": "ты невнимательный?",
    }
    for src, want in cases.items():
        assert soften(src) == want, (src, soften(src))
    for safe in ("застрахуйте сервер", "себе на заметку", "мудрый совет", "учеба", "Себастьян", "INC0012345",
                 "Хуавей", "мандарин", "херес", "небо", "хлеб"):
        assert soften(safe) == safe, safe
    assert not has_obscene(soften("ебашим до ночи, нахуй всё, бляха-муха, x*й, pizdec"))


class _Echo(LLMClient):
    """Модель, которая записывает запрос и отвечает кодами."""
    seen: list = []

    def _complete(self, system, user, max_tokens):
        _Echo.seen.append((system, user))
        codes = sorted(set(__import__("re").findall(r"Ч\d+", user)))
        return json.dumps({"who": " и ".join(codes)}, ensure_ascii=False)


def test_llm_client_boundary():
    st = _store()
    llm = _Echo("maverick", {"type": "maverick"})
    p = attach(llm, _cfg(), st)
    assert p is not None
    out = llm.complete("Ты помогаешь Александру Смирнову", "Иван Петров: бля, база наебнулась, позвони Кузнецовой")
    system, user = _Echo.seen[-1]
    assert "Смирнов" not in system and "Петров" not in user and "Кузнецов" not in user
    assert "бля" not in user and "наебнулась" not in user and "сломалась" in user
    who = json.loads(out)["who"]
    assert "Иван Петров" in who and "Мария Кузнецова" in who
    assert recent()[-1]["user"] == user
    # в каждом запросе свои случайные коды: один и тот же человек под разными номерами
    seen_codes = set()
    for _ in range(6):
        llm.complete("сводка", "Иван Петров: база упала")
        seen_codes |= set(__import__("re").findall(r"Ч\d+", _Echo.seen[-1][1]))
    assert len(seen_codes) >= 3
    assert "сам пользователь" in _Echo.seen[-1][0]
    # локальная модель — без обезличивания
    loc = _Echo("local", {"type": "openai", "base_url": "http://localhost:1235/v1"})
    assert attach(loc, _cfg(), st) is None
    assert attach(_Echo("x", {"type": "maverick"}), _cfg(pseudonymize="off"), st) is None


def test_namesake_is_not_the_user():
    """Обращение по имени — не обязательно к пользователю: учитываются тёзки и точные признаки."""
    st = Store(":memory:")
    st.upsert_chat(Chat(chat_id=-1002, title="Дежурка", kind="group"))
    st.add_messages([Message(-1002, 1, "2026-11-05T10:00:00+00:00", "Иван Иванов", "текст", sender_id=201),
                     Message(-1002, 2, "2026-11-05T10:00:00+00:00", "Ольга Ли", "текст", sender_id=202)])
    st.kv_set("me", json.dumps({"id": 999, "name": "Иван Иванов", "username": "ivan_i"}))
    cfg = Cfg(profile={"name": "Иван Иванов", "aliases": ["Ваня", "Вань"]}, privacy={})
    reg = build_registry(cfg, st)
    s = Session(reg)
    out = s.outgoing("#1 [10:00] Мария Иванова: Ваня, глянь INC1. @ivan_i тоже посмотри. Иван Иванов в курсе. "
                     "Коль, ау")
    me = s.me_code
    assert out.count(me) == 2, out  # @ник и полное имя
    mm = dict(s.maybe_me_codes())
    assert mm and all(c != me for c in mm)
    assert "Ваня" not in out and "Вань" not in out
    from opsdigest.privacy import Privacy, Request
    req = Request(Privacy(reg), s)
    pre = req.preamble()
    assert "тёзок" in pre and next(iter(mm)) in pre
    assert s.restore(f"{next(iter(mm))} просили глянуть") in ("Ваня просили глянуть", "Вань просили глянуть")


def test_digest_markers_and_namesakes():
    from datetime import timezone
    from opsdigest import namesakes
    from opsdigest.digest import transcript_lines
    st = Store(":memory:")
    st.upsert_chat(Chat(chat_id=-1003, title="Команда", kind="group"))
    msgs = [Message(-1003, 1, "2026-11-05T10:00:00+00:00", "Я", "мой вопрос", is_out=True),
            Message(-1003, 2, "2026-11-05T10:01:00+00:00", "Иван Иванов", "отвечаю", sender_id=201,
                    reply_to=1, mentioned=True),
            Message(-1003, 3, "2026-11-05T10:02:00+00:00", "Ольга Ли", "@kput глянь", sender_id=202, mentioned=True),
            Message(-1003, 4, "2026-11-05T10:03:00+00:00", "Мария Иванова", "Ваня, а ты?", sender_id=202, reply_to=2)]
    st.add_messages(msgs)
    lines = transcript_lines(msgs, timezone.utc)
    assert "↩#1(Я)" in lines[2] and "@me" not in lines[2]
    assert "@me" in lines[3]
    assert "↩#2" in lines[4] and "(Я)" not in lines[4] and "@me" not in lines[4]
    forms = namesakes.my_name_forms({"name": "Иван Иванов", "aliases": ["Ваня"]})
    assert {"ваня", "вани", "ивану"} <= forms
    assert namesakes.chat_namesakes(st, -1003, forms, 999) == ["Иван Иванов"]
