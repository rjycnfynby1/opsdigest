"""Раздел «Чаты»: массовые изменения, правила по названию, темы форумов, люди (роль в чате), шаблоны, ИИ-описания."""
import json

from test_ui import ui, wait_job  # noqa: F401 — фикстура окна
from test_ui_media import setup_ws

from opsdigest.storage import Chat, Message


def fill(app):
    from opsdigest.service import open_store
    st = open_store(app.cfg())
    st.upsert_chat(Chat(-1001, "Авария ЛК INC0012345", "group"))
    st.upsert_chat(Chat(-1002, "L2 поддержка", "group"))
    st.upsert_chat(Chat(-1003, "Релизы (форум)", "group"))
    st.upsert_chat(Chat(7, "Погода бот", "bot"))
    st.set_chat_state(-1003, forum=True)
    st.add_messages([Message(-1001, i, f"2026-11-0{i}T10:00:00+00:00", "Пётр Сидоров", f"падение {i}", sender_id=301)
                     for i in (1, 2)])
    st.add_messages([Message(-1002, i, f"2026-11-0{i}T11:00:00+00:00", "Пётр Сидоров" if i % 2 else "Ольга Ли",
                             f"вопрос по биллингу {i}", sender_id=301 if i % 2 else 302) for i in range(1, 6)])
    st.add_messages([Message(-1003, 10 + i, f"2026-11-0{i}T12:00:00+00:00", "Ольга Ли", f"релиз {i}",
                             sender_id=302, topic=5) for i in (1, 2, 3)])
    st.add_messages([Message(-1003, 20, "2026-11-03T12:00:00+00:00", "Пётр Сидоров", "общая", sender_id=301)])
    st.upsert_topics(-1003, [{"id": 5, "title": "Релизы", "unread": 2}, {"id": 1, "title": "Общая"}])
    st.close()


def test_chats_api(ui):  # noqa: F811
    app, call, llm_url, tmp = ui
    ws = setup_ws(call, tmp, llm_url)
    fill(app)
    cfgf = ws / "config.json"
    j = call("/api/chats/list")[1]
    rows = {c["id"]: c for c in j["chats"]}
    assert rows[-1003]["forum"] and rows[-1003]["topics"] == 2 and rows[7]["excluded"], {k: v for k, v in rows[7].items() if k in ("excluded", "priority", "by", "explicit")}
    # массово: исключить и включить, режим
    call("/api/chats/bulk", {"ids": [-1001, -1002], "set": {"mode": "incident", "exclude": True}})
    rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
    assert rows[-1001]["excluded"] and rows[-1002]["mode"] == "incident"
    call("/api/chats/bulk", {"ids": [-1002, 7], "set": {"exclude": False, "mode": "chat"}})
    rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
    assert not rows[-1002]["excluded"] and not rows[7]["excluded"] and rows[-1001]["excluded"]
    # правило по названию — для будущих чатов
    r = call("/api/chats/rule", {"match": "платеж", "mode": "incident", "exclude": True})
    assert r[0] == 200
    assert call("/api/chats/rule", {"match": "re:(", "mode": "incident"})[0] == 400
    from opsdigest.service import open_store
    st = open_store(app.cfg())
    st.upsert_chat(Chat(-1009, "Сбой: платежи", "group"))
    st.close()
    rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
    assert rows[-1009]["excluded"] and rows[-1009]["mode"] == "incident" and rows[-1009]["by"] == "платеж"
    rules = call("/api/chats/list")[1]["rules"]
    i = next(x["i"] for x in rules if x["match"] == "платеж")
    call("/api/chats/rule", {"delete": i})
    assert not any(x["match"] == "платеж" for x in call("/api/chats/list")[1]["rules"])
    # темы форума: важность, исключение, описание; сводка без исключённой темы
    t = call("/api/chat/topics?chat=-1003")[1]["topics"]
    assert {x["topic_id"]: x["messages"] for x in t} == {5: 3, 1: 1}
    call("/api/chat/topics", {"chat": -1003, "topics": [5], "set": {"priority": "high", "description": "релизы ЛК"}})
    call("/api/chat/topics", {"chat": -1003, "topics": [1], "set": {"exclude": True}})
    t = {x["topic_id"]: x for x in call("/api/chat/topics?chat=-1003")[1]["topics"]}
    assert t[5]["priority"] == "high" and t[5]["description"] == "релизы ЛК" and t[1]["excluded"]
    from opsdigest.digest import DigestBuilder
    cfg = app.cfg()
    st = open_store(cfg)
    msgs = st.messages(-1003, "2026-01-01", "2027-01-01")
    from opsdigest.llm import make_client
    b = DigestBuilder(cfg, st, make_client(*cfg.llm_profile()), progress=lambda s: None)
    kept, fn = b._topics(st.get_chat(-1003), msgs)
    lines = fn({}, set())
    assert [m.msg_id for m in kept] == [11, 12, 13] and "тема «Релизы» (важность: высокая; описание пользователя: релизы ЛК)" in lines[0]
    st.close()
    # люди: участники чата, заметка общая для всех чатов, роль — своя в каждом чате
    m = call("/api/chat/members?chat=-1002")[1]["members"]
    assert {x["name"] for x in m} == {"Пётр Сидоров", "Ольга Ли"}
    call("/api/person", {"id": 301, "name": "Пётр Сидоров", "priority": "high", "area": "биллинг",
                         "ask": "списания", "chat": -1002, "role": "дежурный разработчик"})
    p = call("/api/person?id=301&chat=-1002")[1]
    assert p["area"] == "биллинг" and p["role"] == "дежурный разработчик" and p["priority"] == "high"
    assert {c["id"] for c in p["chats"]} == {-1001, -1002, -1003}
    p2 = call("/api/person?id=301&chat=-1001")[1]
    assert p2["area"] == "биллинг" and p2["role"] == ""
    m = {x["id"]: x for x in call("/api/chat/members?chat=-1002")[1]["members"]}
    assert m[301]["described"] and m[301]["role_text"] == "дежурный разработчик"
    from opsdigest import people
    st = open_store(app.cfg())
    notes = people.chat_notes(app.cfg(), st, -1002)
    assert notes and "важный" in notes[0] and "роль в этом чате: дежурный разработчик" in notes[0]
    st.close()
    pl = {x["id"]: x for x in call("/api/people")[1]["people"]}
    assert pl[301]["described"] and pl[302]["msgs"] == 5 and pl[301]["chats"] == 3
    call("/api/people/bulk", {"ids": [302], "set": {"priority": "low"}, "names": {"302": "Ольга Ли"}})
    assert {x["id"]: x for x in call("/api/people")[1]["people"]}[302]["priority"] == "low"
    # шаблоны и уже написанные описания
    call("/api/templates", {"kind": "chat", "name": "Команда", "text": "Моя команда. Важно: вопросы ко мне."})
    tt = call("/api/templates?kind=person.area")[1]
    assert tt["written"][0]["text"] == "биллинг"
    assert call("/api/templates?kind=chat")[1]["templates"][0]["name"] == "Команда"
    assert call("/api/templates", {"kind": "zzz", "name": "a", "text": "b"})[0] == 400
    # ИИ-описания: ручное описание не трогается, часть ИИ — отдельно; авария — кратко, режим «авария»
    call("/api/chat/save", {"chat": {"id": -1002, "title": "L2 поддержка", "type": "group", "priority": "high",
                                     "kind": "chat", "description": "Моя команда"}})
    r = call("/api/describe", {"targets": {"chats": [-1001, -1002, 7], "topics": [[-1003, 5, "Релизы"]],
                                           "people": [301], "roles": [[301, -1002]]}})[1]
    j = wait_job(call, r["job"], 30)
    assert j["status"] == "done", (j["error"], j["log"][-8:])
    assert j["result"]["done"] == 5 and j["result"]["skipped"] == 1, j  # у бота нет переписки
    rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
    assert rows[-1002]["description"] == "Моя команда" and "Рабочий чат команды L2" in rows[-1002]["ai"]
    assert rows[-1001]["ai"].startswith("Чат аварии/инцидента (с 01.11.2026 по 02.11.2026): недоступен личный кабинет")
    assert "Итог: откат" in rows[-1001]["ai"]
    t = {x["topic_id"]: x for x in call("/api/chat/topics?chat=-1003")[1]["topics"]}
    assert t[5]["ai"].startswith("Тема про релизы") and t[5]["description"] == "релизы ЛК"
    p = call("/api/person?id=301&chat=-1002")[1]
    assert p["ai"].startswith("Разработчик биллинга") and p["role_ai"] == "чинит баги биллинга в этом чате"
    assert p["role"] == "дежурный разработчик" and p["area"] == "биллинг"
    st = open_store(app.cfg())
    assert st.get_chat(-1002).note == "Моя команда\nАнализ ИИ: " + rows[-1002]["ai"]
    st.close()
    # повтор без «заново» — всё уже описано
    r = call("/api/describe", {"targets": {"chats": [-1002], "people": [301]}})[1]
    j = wait_job(call, r["job"], 10)
    assert j["result"]["total"] == 0
    # таблица целиком не теряет части ИИ и темы
    call("/api/config/chats", {"chats": [{"id": -1003, "title": "Релизы (форум)", "type": "group", "priority": "low"}]})
    raw = json.loads(cfgf.read_text(encoding="utf-8"))
    r3 = next(x for x in raw["telegram"]["chats"] if x["match"] == "-1003")
    assert r3["topics"]["5"]["ai_description"] and r3["priority"] == "low"


def test_describe_survives_busy_config(ui, monkeypatch):  # noqa: F811
    """Windows: config.json заняли (антивирус, OneDrive) — описание не падает, сохраняет позже; итог виден."""
    import os

    from opsdigest.secure import atomic
    from opsdigest.ui import chats_api
    app, call, llm_url, tmp = ui
    ws = setup_ws(call, tmp, llm_url)
    fill(app)
    monkeypatch.setattr(atomic, "sleep", lambda s: None)
    monkeypatch.setattr(chats_api, "SAVE_EVERY", 1)
    real = os.replace
    state = {"fail": 0, "writes": 0}

    def flaky(src, dst):
        if os.path.basename(str(dst)) == "config.json":
            state["writes"] += 1
            if state["fail"] > 0:
                state["fail"] -= 1
                raise PermissionError(13, "Отказано в доступе")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)

    def ai():
        return {c["id"]: c.get("ai") or "" for c in call("/api/chats/list")[1]["chats"]}

    # занято ненадолго — запись проходит повтором, без предупреждений
    state["fail"] = 3
    j = wait_job(call, call("/api/describe", {"targets": {"chats": [-1001, -1002]}})[1]["job"], 30)
    assert j["status"] == "done" and j["result"]["done"] == 2, (j["error"], j["log"][-6:])
    assert not any("⚠" in x for x in j["log"]) and ai()[-1001] and ai()[-1002]
    # занято долго (все попытки одной записи) — задача идёт дальше, пачка сохраняется следующей записью
    state["fail"] = len(atomic.DELAYS) + 1
    j = wait_job(call, call("/api/describe", {"targets": {"chats": [-1001, -1002], "topics": [[-1003, 5, "Релизы"]]},
                                              "overwrite": True})[1]["job"], 30)
    assert j["status"] == "done" and j["result"]["done"] == 3, (j["error"], j["log"][-6:])
    assert any("не удалось сохранить описания" in x and "занят" in x for x in j["log"]), j["log"]
    t = {x["topic_id"]: x for x in call("/api/chat/topics?chat=-1003")[1]["topics"]}
    assert t[5]["ai"].startswith("Тема про релизы")
    # пачками: 10 описаний — одна запись config.json, а не десять
    monkeypatch.setattr(chats_api, "SAVE_EVERY", 10)
    before = state["writes"]
    j = wait_job(call, call("/api/describe", {"targets": {"chats": [-1001, -1002]}, "overwrite": True})[1]["job"], 30)
    assert j["status"] == "done" and state["writes"] - before <= 2, state
    # занято до конца — понятная ошибка, а не трассировка; ручные настройки не испорчены
    state["fail"] = 10 ** 6
    j = wait_job(call, call("/api/describe", {"targets": {"chats": [-1002]}, "overwrite": True})[1]["job"], 30)
    state["fail"] = 0
    assert j["status"] == "error" and "Запустите «Описать» ещё раз" in j["error"], j
    assert "Traceback" not in "\n".join(j["log"])
    assert json.loads((ws / "config.json").read_text(encoding="utf-8"))["telegram"]["chats"]
