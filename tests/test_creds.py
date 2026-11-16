"""«Учётные данные»: только в защищённой папке, файл зашифрован, секрет — только после пароля OpsDigest
(доступ на 2 минуты, гаснет при блокировке), 5 неверных паролей — полная блокировка, подсказка для плагинов."""
import json
import time

import pytest
from test_secure import _reset, _wait, ui  # noqa: F401 — фикстуры

from opsdigest import logs
from opsdigest.secure import creds as C
from opsdigest.secure import vault


def test_creds_vault(tmp_path):
    ws = tmp_path / "w"
    (ws / "data").mkdir(parents=True)
    vault.bind(ws)
    with pytest.raises(ValueError, match="защищённой папке"):
        C.save(ws, {"title": "Основная", "login": "ivanov", "secret": "TestVal01"})
    vault.create(ws, "test-pass-01", "1234", {"require_telegram": False})
    a = C.save(ws, {"title": "Основная учётная запись", "login": "ivanov", "secret": "TestVal01", "main": True})
    b = C.save(ws, {"title": "Confluence PAT", "kind": "pat", "url": "https://wiki.local", "secret": "pat-test-000000",
                    "plugins": ["wiki"], "auth": "PAT"})
    with pytest.raises(ValueError):
        C.save(ws, {"title": "без пароля"})
    raw = (ws / C.FILE).read_bytes()
    assert raw.startswith(b"ODX1") and b"TestVal01" not in raw and b"ivanov" not in raw   # файл зашифрован
    lst = C.listing(ws)
    assert "secret" not in json.dumps(lst["items"]) .replace('"has_secret"', "").replace('"secret_len"', "")
    assert not any("TestVal01" in json.dumps(x) for x in lst["items"])
    # плагины: вики — свой токен, почта и ITSM — основная запись
    fp = lst["plugins"]
    assert fp["wiki"]["id"] == b["id"] and fp["mail"]["id"] == a["id"] and fp["itsm"]["login"] == "ivanov"
    C.assign(ws, "mail", b["id"])
    assert C.listing(ws)["plugins"]["mail"]["id"] == b["id"]
    C.assign(ws, "mail", None)
    # секрет — только после пароля; логин можно и так, но тоже через пароль-окно
    with pytest.raises(C.NeedPassword):
        C.reveal(ws, a["id"])
    with pytest.raises(vault.BadSecret, match="осталось попыток: 4"):
        C.open_with_password("не тот")
    assert C.reveal(ws, a["id"], password="test-pass-01")["value"] == "TestVal01"
    assert C.is_open() and C.reveal(ws, b["id"])["value"] == "pat-test-000000"     # окно 2 минуты
    assert C.listing(ws)["items"][0]["copies"] == 1
    vault.lock_pin()                                                           # любая блокировка закрывает доступ
    assert not C.is_open()
    vault.unlock_pin("1234")
    with pytest.raises(C.NeedPassword):
        C.reveal(ws, a["id"])
    # изменение без нового пароля — пароль прежний
    C.save(ws, {"id": a["id"], "title": "Основная", "secret": ""})
    assert C.reveal(ws, a["id"], password="test-pass-01")["value"] == "TestVal01"
    # секреты и логины вырезаются из журналов
    assert "TestVal01" not in logs.scrub("ошибка: TestVal01 у ivanov")
    # 5 неверных паролей подряд — полная блокировка
    for _ in range(4):
        with pytest.raises(vault.BadSecret):
            C.open_with_password("x")
    with pytest.raises(vault.BadSecret, match="заблокирована полностью"):
        C.open_with_password("x")
    assert vault.status() == "full"
    vault.unlock("test-pass-01")
    C.delete(ws, b["id"])
    assert [x["title"] for x in C.listing(ws)["items"]] == ["Основная"]


def test_creds_window(ui):  # noqa: F811
    app, call, tmp = ui
    ws = tmp / "w"
    assert call("/api/workspace/set", {"path": str(ws)})[0] == 200
    code, j = call("/api/vault/setup", {"password": "test-pass-01", "password2": "test-pass-01", "pin": "1234",
                                        "require_telegram": False})
    assert code == 200 and _wait(call, j["job"])["status"] == "done"
    code, j = call("/api/creds/save", {"item": {"title": "Основная", "login": "ivanov", "secret": "Tok-test-000000",
                                                "main": True}})
    assert code == 200 and j["items"][0]["has_secret"] and "Tok-test-000000" not in json.dumps(j)
    cid = j["items"][0]["id"]
    code, e = call("/api/creds/reveal", {"id": cid, "field": "secret"})
    assert code == 400 and "пароль OpsDigest" in e["error"]
    assert call("/api/creds/open", {"password": "нет"})[0] == 400
    assert call("/api/creds/open", {"password": "test-pass-01"})[1]["open_left"] == C.OPEN_SECONDS
    assert call("/api/creds/reveal", {"id": cid, "field": "secret"})[1]["value"] == "Tok-test-000000"
    call("/api/creds/close", {})
    assert call("/api/creds/reveal", {"id": cid, "field": "secret"})[0] == 400
    # подсказка для плагинов — без секрета
    p = call("/api/creds")[1]
    assert p["plugins"]["mail"]["login"] == "ivanov" and "Tok-test-000000" not in json.dumps(p)
    time.sleep(0)
