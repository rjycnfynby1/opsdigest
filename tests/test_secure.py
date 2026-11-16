"""Защита данных: шифр, пароль/ПИН/код восстановления, зашифрованная база, перенос открытых файлов, окно."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from opsdigest import workspace as wsmod
from opsdigest.config import load_config
from opsdigest.secure import crypto, files, memdb, vault
from opsdigest.storage import Chat, Message, Store


@pytest.fixture(autouse=True)
def _reset():
    yield
    vault.lock_full()
    memdb.close_all()
    vault.bind(None)


def test_crypto_roundtrip_and_tamper():
    k = crypto.new_key()
    data = os.urandom(3 * crypto.CHUNK + 123)
    blob = crypto.seal(k, data)
    assert crypto.is_sealed(blob) and data not in blob
    assert crypto.open_(k, blob) == data
    with pytest.raises(crypto.CryptoError):
        crypto.open_(crypto.new_key(), blob)
    bad = bytearray(blob)
    bad[40] ^= 1
    with pytest.raises(crypto.CryptoError):
        crypto.open_(k, bytes(bad))
    assert crypto.seal(k, b"x") != crypto.seal(k, b"x")  # случайный nonce


def test_vault_password_pin_recovery(tmp_path):
    ws = wsmod.init_workspace(tmp_path / "w")
    code = vault.create(ws, "test-pass-01", "4321")
    assert vault.status() == "open"
    vault.lock_pin()
    assert vault.status() == "pin"
    with pytest.raises(vault.BadSecret):
        vault.unlock_pin("0000")
    vault.unlock_pin("4321")
    assert vault.status() == "open"
    vault.lock_full()
    assert vault.status() == "full"
    with pytest.raises(vault.BadSecret):
        vault.unlock("неверный-пароль", ws)
    vault.unlock("test-pass-01", ws)
    vault.lock_full()
    vault.recover(code.lower().replace("-", " "), "new-pass-01", ws)
    vault.lock_full()
    vault.unlock("new-pass-01", ws)
    # 5 неверных ПИН — полная блокировка
    vault.lock_pin()
    for _ in range(4):
        with pytest.raises(vault.BadSecret):
            vault.unlock_pin("9999")
    with pytest.raises(vault.BadSecret, match="полностью"):
        vault.unlock_pin("9999")
    assert vault.status() == "full"


def test_idle_timeout_locks(tmp_path):
    ws = wsmod.init_workspace(tmp_path / "w")
    vault.create(ws, "test-pass-01", "1234", {"lock_minutes": 1})
    vault.S.last_activity = time.time() - 120
    assert vault.status() == "pin"


def _seed(ws: Path):
    cfg = load_config(wsmod.config_path(ws))
    st = Store(cfg.path(cfg["storage"]["path"]))
    st.upsert_chat(Chat(chat_id=1, title="Секретный чат", kind="group"))
    st.add_messages([Message(1, 1, "2026-11-05T10:00:00+00:00", "Иван Петров", "привет секрет")])
    folder = ws / "media" / "Секретный чат [1]" / "2026-11"
    folder.mkdir(parents=True)
    (folder / "x_m1.jpg").write_bytes(b"\xff\xd8\xff" + "секрет".encode())
    (folder / "x_m1.jpg.md").write_text("описание: секрет", encoding="utf-8")
    st.add_media([{"chat_id": 1, "msg_id": 1, "date": "2026-11-05T10:00:00+00:00", "kind": "photo", "status": "ok",
                   "path": "media/Секретный чат [1]/2026-11/x_m1.jpg"}])
    st.close()
    import urllib.parse
    src = "/".join(urllib.parse.quote(x) for x in "../media/Секретный чат [1]/2026-11/x_m1.jpg".split("/"))
    (ws / "reports" / "2026-11-05_1000_day.html").write_text(
        f"<p>отчёт секрет</p><a href='{src}'><img src='{src}'></a>", encoding="utf-8")
    (ws / "data" / "telegram.session").write_bytes(b"SQLite format 3\x00 " + "секрет".encode())
    raw = wsmod.read_config(ws)
    raw["telegram"]["api_hash"] = "секретный-хеш"
    wsmod.write_config(ws, raw)
    return cfg


def _leaks(ws: Path) -> list[str]:
    out = []
    for p in ws.rglob("*"):
        if p.is_file() and p.name not in ("vault.json", "ПРОЧТИ.txt"):
            b = p.read_bytes()
            if "секрет".encode() in b or "Секретный".encode() in str(p).encode() or not crypto.is_sealed(b):
                out.append(str(p.relative_to(ws)))
    return out


def test_migrate_and_encrypted_store(tmp_path):
    ws = wsmod.init_workspace(tmp_path / "w")
    _seed(ws)
    vault.create(ws, "test-pass-01", "1234")
    from opsdigest.secure.migrate import migrate_workspace
    res = migrate_workspace(load_config(wsmod.config_path(ws)), progress=lambda s: None)
    assert res["media"] == 1 and res["reports"] == 1
    assert _leaks(ws) == []
    assert not (ws / "data" / "opsdigest.db").exists() and (ws / "data" / "opsdigest.db.odx").exists()
    vault.lock_full()
    with pytest.raises(vault.Locked):
        load_config(wsmod.config_path(ws))
    vault.unlock("test-pass-01", ws)
    cfg = load_config(wsmod.config_path(ws))
    assert cfg.telegram["api_hash"] == "секретный-хеш"
    st = Store(cfg.path(cfg["storage"]["path"]))
    assert st.db.execute("SELECT text FROM messages").fetchone()[0] == "привет секрет"
    row = st.get_media(1)
    assert row["path"].startswith("media/c") and "Секрет" not in row["path"]
    assert files.read_bytes(cfg.path(row["path"])).startswith(b"\xff\xd8\xff")
    # запись → снимок на диск → после полной блокировки и входа данные на месте
    st.add_messages([Message(1, 2, "2026-11-05T10:01:00+00:00", "Иван Петров", "второе")])
    st.close()
    vault.lock_full()
    vault.unlock("test-pass-01", ws)
    st = Store(cfg.path(cfg["storage"]["path"]))
    assert st.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert _leaks(ws) == []


def test_session_encrypted(tmp_path):
    pytest.importorskip("telethon.sessions")
    ws = wsmod.init_workspace(tmp_path / "w")
    vault.create(ws, "test-pass-01", "1234")
    from opsdigest.secure.tgsession import session_for
    s = session_for(ws / "data" / "telegram")
    s.set_dc(2, "149.154.167.51", 443)
    s.save()
    raw = (ws / "data" / "telegram.session").read_bytes()
    assert crypto.is_sealed(raw)
    s2 = session_for(ws / "data" / "telegram")
    assert s2.dc_id == 2 and s2.server_address == "149.154.167.51"


# ---------------------------------------------------------------- окно
@pytest.fixture()
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    from opsdigest.ui.server import App, make_server
    app = App(idle_exit=False)
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Token", app.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (json.loads(data) if r.headers.get_content_type() == "application/json" else data)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield app, call, tmp_path
    srv.shutdown()
    srv.server_close()


def _wait(call, job, seconds=20):
    for _ in range(seconds * 10):
        code, j = call(f"/api/jobs/{job}")
        if j.get("status") != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("задача не закончилась")


def test_ui_setup_unlock_pin_telegram_gate(ui):
    app, call, tmp = ui
    ws = tmp / "w"
    assert call("/api/workspace/set", {"path": str(ws)})[0] == 200
    _seed(ws)
    st = call("/api/state")[1]
    assert st["vault"]["status"] == "none"
    assert call("/api/vault/setup", {"password": "test-pass-01", "password2": "другой", "pin": "1234"})[0] == 400
    code, j = call("/api/vault/setup", {"password": "test-pass-01", "password2": "test-pass-01", "pin": "1234"})
    assert code == 200 and len(j["recovery"]) == 29
    recovery = j["recovery"]
    assert _wait(call, j["job"])["status"] == "done"
    assert _leaks(ws) == []
    # пароль верный, но Telegram «не вошли» (в базе нет владельца) — данные закрыты
    st = call("/api/state")[1]
    assert st["locked"] == "telegram"
    assert call("/api/reports")[0] == 423
    assert call("/api/config")[0] == 200
    # «вошли в Telegram»: есть сессия и владелец в базе
    cfg = load_config(wsmod.config_path(ws))
    s = Store(cfg.path(cfg["storage"]["path"]))
    s.kv_set("me", json.dumps({"id": 777, "name": "Я"}))
    s.close()
    st = call("/api/state")[1]
    assert st.get("locked") is None and st["vault"]["telegram_ok"]
    code, j = call("/api/reports")
    assert code == 200 and j["reports"][0]["name"] == "2026-11-05_1000_day.html"
    code, body = call("/reports/2026-11-05_1000_day.html")
    assert code == 200 and "отчёт секрет".encode() in body
    # экспорт: расшифрованный файл с картинкой внутри
    code, j = call("/api/reports/export", {"name": "2026-11-05_1000_day.html", "reveal": False})
    assert code == 200
    out = Path(j["html"]).read_text(encoding="utf-8")
    assert "отчёт секрет" in out and "data:image/jpeg;base64," in out
    # блокировка ПИН-кодом
    assert call("/api/vault/lock", {})[1]["status"] == "pin"
    assert call("/api/reports")[0] == 423
    assert call("/api/vault/pin", {"pin": "0000"})[0] == 400
    assert call("/api/vault/pin", {"pin": "1234"})[1]["status"] == "open"
    # другая учётка Telegram — данные снова закрыты
    s = Store(cfg.path(cfg["storage"]["path"]))
    s.kv_set("me", json.dumps({"id": 888, "name": "Чужой"}))
    s.close()
    assert call("/api/state")[1]["locked"] == "telegram"
    # выключить требование Telegram можно только открытой программой; полная блокировка — пароль
    assert call("/api/vault/lock", {"full": True})[1]["status"] == "full"
    assert call("/api/state")[1]["locked"] == "full"
    assert call("/api/config")[0] == 423
    assert call("/api/vault/unlock", {"password": "плохой"})[0] == 400
    assert call("/api/vault/unlock", {"password": "test-pass-01"})[1]["status"] == "open"
    # чужая учётка в базе: снять условие паролем нельзя — только кодом восстановления
    assert call("/api/vault/settings", {"require_telegram": False})[0] == 400
    assert call("/api/vault/settings", {"require_telegram": False, "code": recovery, "lock_minutes": 5})[0] == 200
    st = call("/api/state")[1]
    assert st.get("locked") is None and st["vault"]["lock_minutes"] == 5


# ---------------------------------------------------------------- Windows: файл занят (антивирус, другой поток)
def test_atomic_replace_retries_when_file_busy(tmp_path, monkeypatch):
    from opsdigest.secure import atomic
    monkeypatch.setattr(atomic, "sleep", lambda s: None)
    p = tmp_path / "config.json"
    p.write_text("старое", encoding="utf-8")
    real = os.replace
    fails = {"n": 2}

    def busy_replace(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(13, "Отказано в доступе")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", busy_replace)
    files.write_text(p, "новое")                     # два раза «занят» — третья попытка проходит
    assert p.read_text(encoding="utf-8") == "новое" and not (tmp_path / "config.json.tmp").exists()
    fails["n"] = 10 ** 6                              # занят всё время — ошибка, старый файл цел, мусора нет
    with pytest.raises(PermissionError):
        files.write_text(p, "третье")
    assert p.read_text(encoding="utf-8") == "новое" and not (tmp_path / "config.json.tmp").exists()
    e = OSError(1, "sharing violation")
    e.winerror = 32
    assert atomic.busy(e) and not atomic.busy(FileNotFoundError(2, "нет"))
    calls = {"n": 0}

    def missing():
        calls["n"] += 1
        raise FileNotFoundError(2, "нет")
    with pytest.raises(FileNotFoundError):
        atomic.retry(missing)
    assert calls["n"] == 1                            # настоящие ошибки не повторяются


def test_atomic_reads_and_writes_do_not_overlap(tmp_path, monkeypatch):
    """В Windows замена падает, пока файл открыт на чтение: внутри программы чтение и запись идут по очереди."""
    from opsdigest.secure import atomic
    p = tmp_path / "config.json"
    files.write_text(p, json.dumps({"n": 0}))
    state = {"readers": 0, "collisions": 0}
    real_replace, real_read = os.replace, Path.read_bytes

    def slow_read(self):
        state["readers"] += 1
        try:
            time.sleep(0.001)
            return real_read(self)
        finally:
            state["readers"] -= 1

    def checking_replace(src, dst):
        if state["readers"]:
            state["collisions"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "read_bytes", slow_read)
    monkeypatch.setattr(os, "replace", checking_replace)
    stop = time.time() + 0.5

    def reader():
        while time.time() < stop:
            json.loads(files.read_text(p))

    ts = [threading.Thread(target=reader) for _ in range(3)]
    for t in ts:
        t.start()
    for i in range(50):
        files.write_text(p, json.dumps({"n": i}))
    for t in ts:
        t.join()
    assert state["collisions"] == 0 and json.loads(files.read_text(p)) == {"n": 49}
    assert atomic.lock(p) is atomic.lock(str(p))
