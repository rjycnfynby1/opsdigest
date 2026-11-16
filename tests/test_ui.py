"""Сервер окна: защита, мастер настройки по шагам, сводка и отчёты (без Telegram — на демо-данных)."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from fake_llm import start_server


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from opsdigest.ui.server import App, make_server
    app = App(idle_exit=False)
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm_srv, llm_url = start_server()

    def call(path, body=None, token=True, host=None, raw=False):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Token", app.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                return (r.status, data) if raw else (r.status, json.loads(data))
        except urllib.error.HTTPError as e:
            data = e.read()
            return (e.code, data) if raw else (e.code, json.loads(data))

    yield app, call, llm_url, tmp_path
    srv.shutdown()
    srv.server_close()


def wait_job(call, job, seconds=10):
    for _ in range(int(seconds * 10)):
        code, j = call(f"/api/jobs/{job}")
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("задача не закончилась")


def test_security(ui):
    app, call, _, _ = ui
    code, html = call("/", token=False, raw=True)
    assert code == 200 and b"OpsDigest" in html
    assert call("/api/state", token=False)[0] == 403
    assert call("/api/state", host="evil.example:80")[0] == 403
    assert call("/api/state")[1]["steps"]["workspace"] is False


def test_wizard_and_digest(ui):
    app, call, llm_url, tmp = ui
    ws = tmp / "ws"
    code, st = call("/api/workspace/set", {"path": str(ws)})
    assert code == 200 and st["steps"]["workspace"] and (ws / "config.json").exists()
    # модель
    assert call("/api/llm/models", {"base_url": llm_url})[1]["models"] == ["fake-qwen3-instruct"]
    assert call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "auto", "ctx": 32768}})[0] == 200
    code, t = call("/api/llm/test", {})
    assert code == 200 and t["answer"] == "готов"
    code, e = call("/api/config/llm", {"provider": "claude", "claude": {"api_key": ""}})
    assert code == 400 and "ключ" in e["error"]
    call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url}})
    call("/api/llm/test", {})
    # Telegram: проверка полей, секрет не возвращается и не затирается пустым
    assert call("/api/config/telegram", {"api_id": "abc"})[0] == 400
    code, r = call("/api/config/telegram", {"api_id": "123", "api_hash": "h" * 32, "phone": "+7 (999) 111-22-33",
                                             "proxies": "tg://proxy?server=a.b&port=443&secret=dd00112233445566778899aabbccddeeff\nмусор"})
    assert r["proxies"] == ["mtproto-dd a.b:443"] and len(r["bad"]) == 1
    call("/api/config/telegram", {"api_id": "123", "api_hash": "", "phone": "+79991112233", "proxies": ""})
    cfgv = call("/api/config")[1]
    assert cfgv["telegram"]["api_hash_set"] and "h" * 32 not in json.dumps(cfgv)
    assert json.loads((ws / "config.json").read_text(encoding="utf-8"))["telegram"]["api_hash"] == "h" * 32
    # профиль
    call("/api/config/profile", {"name": "Алексей", "aliases": "Лёша, Лёш", "competences": [
        {"name": "Биллинг", "description": "списания", "keywords": "биллинг; BILL-"}, {"name": ""}]})
    prof = json.loads((ws / "config.json").read_text(encoding="utf-8"))["profile"]
    assert prof["aliases"] == ["Лёша", "Лёш"] and prof["competences"] == [
        {"name": "Биллинг", "description": "списания", "keywords": ["биллинг", "BILL-"]}]
    # чаты: правило только для изменённого
    call("/api/config/chats", {"chats": [{"id": -100, "title": "Флуд", "type": "group", "priority": "ignore", "kind": "chat"},
                                         {"id": 5, "title": "Иван", "type": "private", "priority": "high", "kind": "chat"}]})
    rules = json.loads((ws / "config.json").read_text(encoding="utf-8"))["telegram"]["chats"]
    assert rules[0] == {"match": "-100", "title": "Флуд", "priority": "normal", "kind": "chat", "exclude": True}
    assert len(rules) == 3
    st = call("/api/state")[1]["steps"]
    assert st["llm"] and st["telegram"] and st["profile"] and st["chats"] and not st["login"]
    # демо-переписка в базу рабочей папки → сводка без Telegram
    from opsdigest.config import load_config
    from opsdigest.demo import load_demo
    from opsdigest.digest import get_tz
    from opsdigest.service import open_store
    cfg = load_config(ws / "config.json")
    s = open_store(cfg)
    load_demo(cfg, s, get_tz("local"))
    s.close()
    code, r = call("/api/digest", {"period": "day", "sync": False})
    j = wait_job(call, r["job"])
    assert j["status"] == "done", j
    assert j["result"]["chats"] == 6 and j["result"]["top"] == 7 and any("Отчёт готов" in l for l in j["log"])
    reps = call("/api/reports")[1]["reports"]
    assert reps and reps[0]["name"] == j["result"]["report"] and reps[0]["top"] == 7 and reps[0]["md"]
    code, html = call(f"/reports/{reps[0]['name']}", raw=True)
    assert code == 200 and b"<!DOCTYPE html>" in html
    assert call(f"/reports/{reps[0]['name']}", token=False, raw=True)[0] == 403
    assert call("/reports/..%2Fconfig.json", raw=True)[0] == 404
    # вопрос
    code, r = call("/api/ask", {"question": "что с BILL-482?", "sync": False})
    j = wait_job(call, r["job"])
    assert j["status"] == "done" and "BILL-482" in j["result"]["answer"]


def test_workspace_remembered(ui, tmp_path):
    app, call, _, _ = ui
    call("/api/workspace/set", {"path": str(tmp_path / "ws2")})
    from opsdigest.workspace import load_app_settings
    assert load_app_settings()["workspace"] == str(tmp_path / "ws2")


def test_vpn_card(ui, monkeypatch):
    pytest.importorskip("telethon.network.mtprotoplainsender")
    from pathlib import Path
    from opsdigest.vpn import xray
    app, call, _, tmp = ui
    monkeypatch.setenv("LOCALAPPDATA", str(tmp / "local"))
    monkeypatch.setenv("OPSDIGEST_XRAY", str(Path(__file__).with_name("fake_xray.py")))
    assert call("/api/workspace/set", {"path": str(tmp / "ws"), "create": True})[0] == 200
    code, v = call("/api/vpn")
    assert code == 200 and v["xray"]["installed"] and v["rows"] == []
    link = ("vless://11111111-2222-3333-4444-555555555555@nl.example.com:443?type=tcp&security=reality&pbk=K"
            "&sni=www.microsoft.com&sid=ab#NL")
    try:
        code, j = call("/api/vpn/test", {"source": link, "enabled": True, "selected": "auto"})
        r = wait_job(call, j["job"], 90)   # на Windows процесс Xray и DNS заметно медленнее
        assert r["status"] == "done" and r["result"]["ok"] == 1, r
        code, v = call("/api/vpn")
        assert v["rows"][0]["tg_ok"] and v["servers"] == 1
        assert call("/api/config")[1]["telegram"]["vpn"] == {"enabled": True, "source": link, "selected": "auto"}
        code, j = call("/api/vpn/save", {"selected": v["rows"][0]["key"]})
        assert j["vpn"]["selected"] == v["rows"][0]["key"]
    finally:
        xray.stop_all()


def test_import_export_from_window(ui):
    app, call, _, tmp = ui
    ws = tmp / "ws3"
    assert call("/api/workspace/set", {"path": str(ws), "create": True})[0] == 200
    exp = ws / "DataExport_2026-11-02"
    exp.mkdir(parents=True)
    data = {"about": "x", "personal_information": {"user_id": 5, "first_name": "Я"},
            "chats": {"about": "x", "list": [{"name": "Дежурка", "type": "private_supergroup", "id": 42, "messages": [
                {"id": 1, "type": "message", "date_unixtime": "1793500000", "from": "Иван", "from_id": "user7",
                 "text": "INC0099999 горит"}]}]}}
    (exp / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    code, j = call("/api/import", {"path": ""})
    r = wait_job(call, j["job"])
    assert r["status"] == "done" and r["result"]["messages"] == 1, r
    st = call("/api/state")[1]
    assert st["base"]["messages"] == 1 and st["base"]["chats"] == 1 and st["steps"]["login"] is False


def test_window_has_messenger_markup():
    """Всё, к чему обращается messenger.js, есть в разметке окна; скрипт и стили подключены до основного кода."""
    import re
    from pathlib import Path
    ui = Path(__file__).resolve().parent.parent / "opsdigest" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    js = "".join((ui / f).read_text(encoding="utf-8") for f in ("messenger.js", "chats.js", "glossary.js"))
    ids = set(re.findall(r"\$\('([\w-]+)'\)", js))
    have = set(re.findall(r'id="([\w-]+)"', html + js))  # часть элементов скрипт создаёт сам
    assert ids - have == set()
    assert html.index('src="/static/messenger.js"') < html.index("const T = ")
    assert 'href="/static/messenger.css"' in html
    for fn in ("mxAuto()", "setMode('mx')", "updOpen()"):
        assert fn in html
