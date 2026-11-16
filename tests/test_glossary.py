"""Глоссарий: статьи с несколькими значениями, поиск терминов в тексте, блок для модели, пополнение моделью,
вопросы пользователю и ответы, окно (API)."""
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна
from test_ui_media import setup_ws

from opsdigest.glossary import Glossary, apply_found, stem
from opsdigest.storage import Chat, Message, Store


def test_glossary_core():
    st = Store(":memory:")
    g = Glossary(st)
    assert g.seed() == 3 and g.seed() == 0
    assert g.count()["questions"] == 2
    # формы слова и номера по формату
    found = [e["term"] for e in g.match("По инцидентам INC0012345 и PRB0000777 — у аварии нет проблем")]
    assert set(found) == {"Инцидент", "Авария", "Проблема"}
    assert stem("инцидента") == stem("инциденты") == "инцидент"
    b = g.block("Заведите проблему и привяжите инциденты")
    assert "Проблема" in b and "значение 2" in b and "признаки:" in b and "номер: PRB" in b
    assert g.block("просто текст про погоду") == ""
    # дополнение предложением ИИ: подтверждённое не затирается, значение добавляется, синоним — тоже
    e = g.save({"term": "Инцидент", "aliases": ["инц"], "senses": [
        {"meaning": "Обращение (заявка) в ITSM на портале поддержки"},  # уже есть — не дублируем
        {"meaning": "Инцидент информационной безопасности", "how_to_tell": "ИБ, SOC"}]}, merge=True, source="telegram")
    assert e["status"] == "confirmed" and len(e["senses"]) == 3 and e["aliases"].count("инц") == 1
    # найденное моделью: новые черновики, уточнения существующих, неуверенное — мимо, вопросы — не дублируются
    data = {"terms": [{"term": "auth-svc", "kind": "service", "summary": "авторизация", "senses": [{"meaning": "вход"}],
                       "confidence": 0.9},
                      {"term": "ерунда", "confidence": 0.2}],
            "questions": [{"term": "auth-svc", "question": "Кто отвечает за auth-svc?"},
                          {"term": "auth-svc", "question": "Кто отвечает за сервис auth-svc?"}]}
    s = apply_found(g, data, -1, "тест")
    assert s == {"new": 1, "updated": 0, "questions": 1}
    assert g.get("auth-svc")["status"] == "draft" and g.get("ерунда") is None
    assert "[черновик]" in g.block("упал auth-svc")
    # закреплённое — в каждом запросе
    g.save(dict(g.get("Авария"), pinned=True))
    assert "Авария" in g.block("совсем другое")
    # выгрузка → загрузка в пустую базу
    data = g.export()
    st2 = Store(":memory:")
    r = Glossary(st2).import_(data)
    assert r["terms"] == 4 and Glossary(st2).get("инц")["term"] == "Инцидент"


def test_glossary_api(ui):  # noqa: F811
    app, call, llm_url, tmp = ui
    setup_ws(call, tmp, llm_url)
    from opsdigest.service import open_store
    st = open_store(app.cfg())
    st.upsert_chat(Chat(-1001, "Авария ЛК", "group"))
    st.add_messages([Message(-1001, 1, "2026-11-05T10:00:00+00:00", "Пётр", "auth-svc 4.12 откатили, ЛК поднялся"),
                     Message(-1001, 2, "2026-11-05T10:01:00+00:00", "Ольга", "заведите инц на ЛК")])
    st.close()
    j = call("/api/glossary")[1]
    assert j["counts"]["terms"] == 3 and len(j["questions"]) == 2
    # пополнение из чатов
    r = call("/api/glossary/scan", {})[1]
    res = wait_job(call, r["job"], 20)
    assert res["status"] == "done", res
    assert res["result"]["new"] == 1 and res["result"]["updated"] == 1 and res["result"]["questions"] == 1
    j = call("/api/glossary")[1]
    t = {x["term"]: x for x in j["terms"]}
    assert t["auth-svc"]["status"] == "draft" and t["auth-svc"]["refs"] == [[-1001, 1]]
    assert "инц" in t["Инцидент"]["aliases"]
    # повтор — нового нет
    r = call("/api/glossary/scan", {})[1]
    assert wait_job(call, r["job"], 10)["result"]["chats"] == 0
    # ответ на вопрос → статья уточнена и подтверждена
    q = next(x for x in j["questions"] if x["term"] == "auth-svc")
    r = call("/api/glossary/answer", {"id": q["id"], "answer": "Команда ЛК"})[1]
    assert wait_job(call, r["job"], 10)["status"] == "done"
    e = call("/api/glossary/term?term=auth-svc")[1]["term"]
    assert e["owner"] == "Команда ЛК" and e["status"] == "confirmed"
    assert all(x["id"] != q["id"] for x in call("/api/glossary")[1]["questions"])
    # своя статья, предложение по переписке, удаление
    call("/api/glossary/save", {"term": {"term": "ЛК", "kind": "app", "summary": "личный кабинет"}})
    r = call("/api/glossary/suggest", {"term": "ЛК"})[1]
    sj = wait_job(call, r["job"], 10)
    assert sj["result"]["entry"]["aliases"] == ["личный кабинет"] and sj["result"]["found"] == 2
    lk = call("/api/glossary/term?term=%D0%9B%D0%9A")[1]["term"]
    call("/api/glossary/delete", {"id": lk["id"]})
    assert call("/api/glossary/term?term=%D0%9B%D0%9A")[0] == 400
    # глоссарий подставляется в запрос к модели (ИИ в чате)
    from opsdigest.glossary import block_for
    st = open_store(app.cfg())
    assert "auth-svc" in block_for(st, "что с auth-svc?") and "Команда ЛК" in block_for(st, "что с auth-svc?")
    st.close()


def test_glossary_images():
    """Иллюстрации: описанные картинки подбираются к термину, и наоборот — термины к картинке."""
    st = Store(":memory:")
    st.upsert_chat(Chat(-1, "Авария ЛК", "group"))
    st.add_messages([Message(-1, 1, "2026-11-05T10:00:00+00:00", "Пётр", "[фото] вот"),
                     Message(-1, 2, "2026-11-05T10:01:00+00:00", "Пётр", "[фото] и вот")])
    for i, (sha, desc) in enumerate([("a" * 64, "Окно входа в личный кабинет, ошибка 502 auth-svc"),
                                     ("b" * 64, "График нагрузки CPU")], 1):
        st.add_media([{"chat_id": -1, "msg_id": i, "kind": "photo", "status": "ok", "path": f"m{i}.jpg", "sha256": sha}])
        st.save_description(sha, {"kind": "скриншот", "summary": desc, "details": "", "text": ""})
    g = Glossary(st)
    e = g.save({"term": "auth-svc", "aliases": ["авторизация"], "visual": "красная плашка 502 на входе"})
    c = g.images_for(e)
    assert [x["msg"] for x in c] == [1] and "502" in c[0]["summary"]
    g.save(dict(e, images=[c[0]["media"]]))
    assert g.get("auth-svc")["images"] == [c[0]["media"]]
    assert [t["term"] for t in g.for_media(c[0]["media"])] == ["auth-svc"]
    assert g.for_media(c[0]["media"])[0]["attached"]
    assert "как выглядит: красная плашка" in g.block("упал auth-svc")
