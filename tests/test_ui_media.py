"""Окно: раздел «Чаты» (настройки, описание, исключение), изображения чата, файлы, проверка модели на картинки."""
import hashlib
import json

import fake_llm
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна

from opsdigest import media
from opsdigest.storage import Chat, Message


def setup_ws(call, tmp, llm_url):
    ws = tmp / "ws"
    assert call("/api/workspace/set", {"path": str(ws)})[0] == 200
    call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "fake", "ctx": 32768}})
    assert call("/api/llm/test", {})[0] == 200
    return ws


def fill_db(app):
    from opsdigest.service import open_store
    cfg = app.cfg()
    st = open_store(cfg)
    st.upsert_chat(Chat(-1001, "Авария ЛК", "group"))
    st.upsert_chat(Chat(-1002, "Флуд", "group"))
    st.upsert_chat(Chat(5, "Иван", "private"))
    st.add_messages([Message(-1001, i, f"2026-11-0{i}T10:00:00+00:00", "Пётр", f"[фото] скрин {i}") for i in (1, 2, 3)])
    st.add_messages([Message(5, 1, "2026-11-01T10:00:00+00:00", "Иван", "привет")])
    ids = []
    for i in (1, 2, 3):
        data = b"\xff\xd8\xff\xe0" + f"ui{i}".encode() * 40
        st.add_media([{"chat_id": -1001, "msg_id": i, "date": f"2026-11-0{i}T10:00:00+00:00", "kind": "photo",
                       "status": "pending"}])
        row = st.media_of(-1001, [i])[i]
        if i < 3:  # третье не скачано (Telegram в тесте нет)
            rel = media.rel_path(cfg, st, row, "Авария ЛК")
            cfg.path(rel).parent.mkdir(parents=True, exist_ok=True)
            cfg.path(rel).write_bytes(data)
            st.set_media_file(row["id"], rel, hashlib.sha256(data).hexdigest())
            fake_llm.IMAGES[hashlib.sha256(data).hexdigest()] = {
                "kind": "скриншот", "summary": f"Скрин ошибки {i}", "details": "", "text": f"ERR{i}", "importance": 4}
        ids.append(row["id"])
    st.close()
    return ids


def test_chats_settings_and_images(ui):  # noqa: F811
    app, call, llm_url, tmp = ui
    ws = setup_ws(call, tmp, llm_url)
    ids = fill_db(app)
    try:
        code, j = call("/api/chats/list")
        rows = {c["id"]: c for c in j["chats"]}
        assert code == 200 and rows[-1001]["images"] == 3 and rows[-1001]["images_ok"] == 2
        assert rows[5]["priority"] == "high" and not rows[-1002]["excluded"] and j["stats"]["images"] == 3
        # один чат: описание и исключение
        desc = "Я — L2 по оплатам. Следить за ошибками оплаты; графики дежурств не важны."
        call("/api/chat/save", {"chat": {"id": -1001, "title": "Авария ЛК", "type": "group", "priority": "high",
                                         "kind": "incident", "description": desc}})
        call("/api/chat/save", {"chat": {"id": -1002, "title": "Флуд", "type": "group", "priority": "normal",
                                         "exclude": True}})
        rules = json.loads((ws / "config.json").read_text(encoding="utf-8"))["telegram"]["chats"]
        mine = {r["match"]: r for r in rules}
        assert mine["-1001"]["description"] == desc and mine["-1001"]["kind"] == "incident"
        assert mine["-1002"]["exclude"] is True
        rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
        assert rows[-1001]["description"] == desc and rows[-1002]["excluded"] and rows[-1002]["priority"] == "normal"
        from opsdigest.service import open_store
        st = open_store(app.cfg())
        assert st.get_chat(-1001).note == desc and st.get_chat(-1002).priority == "ignore"
        st.close()
        # сохранение всей таблицы не теряет описание и правила чатов, которых нет в форме
        call("/api/config/chats", {"chats": [{"id": 5, "title": "Иван", "type": "private", "priority": "low"}]})
        mine = {r["match"]: r for r in json.loads((ws / "config.json").read_text(encoding="utf-8"))["telegram"]["chats"]}
        assert mine["5"]["priority"] == "low" and mine["-1001"]["description"] == desc
        # изображения чата: Telegram не настроен — описываются скачанные
        code, r = call("/api/chat/images", {"chat_id": -1001})
        j = wait_job(call, r["job"], 20)
        assert j["status"] == "done", j
        assert j["result"]["described"] == 2 and j["result"]["total_described"] == 2
        rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
        assert rows[-1001]["images_described"] == 2
        side = list((ws / "media").rglob("*.jpg.md"))
        assert len(side) == 2 and "Я — L2 по оплатам" not in side[0].read_text(encoding="utf-8")
        # файл изображения — по номеру и по пути (для отчётов), только с токеном
        code, data = call(f"/media-file/{ids[0]}", raw=True)
        assert code == 200 and data.startswith(b"\xff\xd8")
        assert call(f"/media-file/{ids[0]}", token=False, raw=True)[0] == 403
        assert call(f"/media-file/{ids[2]}", raw=True)[0] == 404
        rel = next((ws / "media").rglob("*.jpg")).relative_to(ws / "media").as_posix()
        import urllib.parse
        assert call("/media/" + urllib.parse.quote(rel), raw=True)[0] == 200
        assert call("/media/..%2Fconfig.json", raw=True)[0] == 404
        # отменить можно только существующую задачу
        assert call(f"/api/jobs/{r['job']}/cancel", {})[0] == 200
        assert call("/api/jobs/nope/cancel", {})[0] == 400
    finally:
        fake_llm.IMAGES.clear()


def test_vision_settings_and_export_errors(ui):  # noqa: F811
    app, call, llm_url, tmp = ui
    setup_ws(call, tmp, llm_url)
    code, j = call("/api/vision/test", {"provider": ""})
    r = wait_job(call, j["job"], 20)
    assert r["status"] == "done" and r["result"]["ok"], r
    assert call("/api/config/media", {"vision_provider": "claude"})[0] == 400  # не настроена
    code, j = call("/api/config/media", {"auto_describe_max": "5", "describe_workers": 99, "light_max_mb": "2.5"})
    assert code == 200 and j["media"]["auto_describe_max"] == 5 and j["media"]["describe_workers"] == 8
    assert call("/api/config")[1]["media"]["light_max_mb"] == 2.5
    # выгрузка без настроенного Telegram — понятная ошибка
    code, j = call("/api/export", {"since": "2026-10-01"})
    r = wait_job(call, j["job"], 20)
    assert r["status"] == "error" and "TG_API_ID" in r["error"]
    assert call("/api/export", {"since": "01.10.2026"})[0] == 400
    # пока идёт выгрузка, сводка строится по базе без синхронизации
    from opsdigest.ui.server import Job
    fake = Job("export")
    app.jobs[fake.id] = fake
    assert call("/api/state")[1]["background"][0]["kind"] == "export"
    from opsdigest.config import load_config
    from opsdigest.demo import load_demo
    from opsdigest.digest import get_tz
    from opsdigest.service import open_store
    cfg = load_config(tmp / "ws" / "config.json")
    s = open_store(cfg)
    load_demo(cfg, s, get_tz("local"))
    s.close()
    code, j = call("/api/digest", {"period": "day", "sync": True, "preempt": False})
    r = wait_job(call, j["job"], 20)
    assert r["status"] == "done" and any("Идёт выгрузка истории" in l for l in r["log"])
    assert not fake.cancelled   # без «остановить остальное» выгрузка не тронута
    assert call("/api/chats/load", {})[0] == 200  # задача сама скажет, что Telegram занят
    fake.status = "done"


def test_maverick_image_format_in_window(ui):  # noqa: F811
    app, call, llm_url, tmp = ui
    host = llm_url.rsplit("/v1", 1)[0]
    ws = tmp / "ws"
    call("/api/workspace/set", {"path": str(ws)})
    assert call("/api/config/media", {"maverick_image_body": "{}"})[0] == 400  # Maverick ещё не настроен
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    assert call("/api/config/media", {"maverick_image_body": "{not json"})[0] == 400
    assert call("/api/config/media", {"maverick_image_body": '{"message": "{{prompt}}"}'})[0] == 400  # без картинки
    tmpl = '{"message": "{{prompt}}", "files": [{"content": "{{image_base64}}", "filename": "{{image_name}}"}]}'
    code, j = call("/api/config/media", {"maverick_image_body": tmpl})
    assert code == 200 and j["media"]["maverick"] and "image_base64" in j["media"]["maverick_image_body"]
    # проверка: формат из шаблона работает, журнал попыток — в рабочей папке
    from fake_llm import _H  # noqa: F401 — сервер модели из фикстуры
    srv = next(s for s in __import__("gc").get_objects() if type(s).__name__ == "HTTPServer"
               and getattr(s, "server_address", (None, 0))[1] == int(host.rsplit(":", 1)[1]))
    srv.mav_images = "files_obj"
    code, j = call("/api/vision/test", {"provider": ""})
    r = wait_job(call, j["job"], 30)
    assert r["status"] == "done" and r["result"]["ok"], r
    prof = json.loads((ws / "config.json").read_text(encoding="utf-8"))["llm"]["profiles"]["maverick"]
    assert prof["image_mode"] == {"template": True} and (ws / "data" / "maverick-images.txt").exists()
    # формат перестал работать — при проверке подбирается заново
    srv.mav_images = "strict"
    code, j = call("/api/vision/test", {})
    r = wait_job(call, j["job"], 30)
    assert r["status"] == "done" and r["result"]["ok"], r
    assert any("подбираю заново" in l for l in r["log"])
    # шаблон стёрт — формат сохраняется найденный
    call("/api/config/media", {"maverick_image_body": ""})
    prof = json.loads((ws / "config.json").read_text(encoding="utf-8"))["llm"]["profiles"]["maverick"]
    assert "image_body" not in prof
