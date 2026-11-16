"""Сторож на настоящих процессах: запуск из OpsDigest.bat (opsdigest.app), сервер упал посреди ИИ-описаний —
поднят снова на том же порту и с тем же ключом окна, задача продолжена и доведена до конца; ключ данных
защищённой папки пережил сбой (окно — под ПИН-кодом, без пароля); завис — убит и поднят; «Выход» — всё закрыто."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fake_llm import start_server
from test_chats_api import fill

ROOT = Path(__file__).resolve().parent.parent


def alive(pid) -> bool:
    if not pid:
        return False
    st = Path(f"/proc/{pid}/stat")
    if st.exists():  # в контейнере без init завершённые процессы могут остаться «зомби»
        return st.read_text().split(") ", 1)[1][:1] not in ("Z", "X")
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def wait(pred, sec=30, step=0.2):
    end = time.time() + sec
    while time.time() < end:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 — сервер как раз перезапускается
            v = None
        if v:
            return v
        time.sleep(step)
    raise AssertionError("не дождался")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL/SIGSTOP — проверка для песочницы")
def test_supervisor_restarts_and_resumes(tmp_path):
    appdata = tmp_path / "appdata"
    env = {**os.environ, "APPDATA": str(appdata), "LOCALAPPDATA": str(tmp_path / "local"),
           "OPSDIGEST_NO_WINDOW": "1", "PYTHONPATH": str(ROOT), "OPSDIGEST_HANG_SECONDS": "6",
           "OPSDIGEST_PING_EVERY": "1"}
    for k in ("OPSDIGEST_PORT", "OPSDIGEST_TOKEN", "OPSDIGEST_SUPERVISED"):
        env.pop(k, None)
    settings = lambda: json.loads((appdata / "OpsDigest" / "app.json").read_text(encoding="utf-8"))  # noqa: E731
    llm_srv, llm_url = start_server()
    llm_srv.delay = 0.5
    r = subprocess.run([sys.executable, "-m", "opsdigest.app"], env=env, cwd=str(ROOT), capture_output=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout.decode() + r.stderr.decode()
    s0 = settings()
    port, token, sup = s0["port"], s0["token"], s0["supervisor_pid"]

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("X-Token", token)
        req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    try:
        boot = call("/api/ping")["boot"]
        assert call("/api/ping")["supervised"]
        # рабочая папка, модель, чаты в базе
        ws = tmp_path / "ws"
        assert call("/api/workspace/set", {"path": str(ws)})["ok"]
        call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "fake", "ctx": 32768}})
        from opsdigest.config import load_config
        from opsdigest.workspace import config_path
        fill(type("A", (), {"cfg": lambda self: load_config(config_path(ws))})())
        # ИИ-описания (медленная модель) — и сервер «падает» посреди
        j = call("/api/describe", {"targets": {"chats": [-1001, -1002, -1003], "people": [301, 302],
                                               "roles": [[301, -1002], [302, -1003]]}, "overwrite": True})
        assert j["ok"], j
        t = wait(lambda: call("/api/tasks")["tasks"][0])
        assert t["status"] == "running"
        time.sleep(1.2)
        os.kill(settings()["server_pid"], signal.SIGKILL)
        # поднят: тот же порт и ключ окна, другой процесс
        wait(lambda: call("/api/ping")["boot"] != boot, 30)
        s1 = settings()
        assert s1["port"] == port and s1["token"] == token and s1["supervisor_pid"] == sup
        # задача продолжилась сама и закончилась
        t = wait(lambda: (lambda x: x if x["status"] == "done" else None)(call("/api/tasks")["tasks"][0]), 60)
        assert t["attempts"] == 2 and t["result"]["failed"] == 0, t
        # защищённая папка: после сбоя данные открыты для задач, окно — под ПИН-кодом (пароль не нужен)
        j = call("/api/vault/setup", {"password": "очень-секретно", "password2": "очень-секретно", "pin": "2468",
                                      "lock_minutes": 10, "require_telegram": False})
        assert j["ok"], j
        wait(lambda: call(f"/api/jobs/{j['job']}")["status"] != "running")
        assert call("/api/ping")["locked"] is None
        boot = call("/api/ping")["boot"]
        os.kill(settings()["server_pid"], signal.SIGKILL)
        wait(lambda: call("/api/ping")["boot"] != boot, 30)
        assert call("/api/ping")["locked"] == "pin"
        assert call("/api/vault/pin", {"pin": "2468"})["status"] == "open"
        assert call("/api/chats/list")["ok"]
        # завис (не отвечает) — убит и поднят
        boot, old = call("/api/ping")["boot"], settings()["server_pid"]
        os.kill(old, signal.SIGSTOP)
        wait(lambda: call("/api/ping")["boot"] != boot, 40)
        assert not alive(old) and settings()["server_pid"] != old
        # второй запуск OpsDigest.bat — не второй экземпляр, а то же окно
        r = subprocess.run([sys.executable, "-m", "opsdigest.app"], env=env, cwd=str(ROOT), capture_output=True,
                           timeout=60)
        assert r.returncode == 0 and "уже работает" in r.stdout.decode()
        assert settings()["supervisor_pid"] == sup
        # «Выход» — сервер и сторож завершаются, никто не поднимает
        srv = settings()["server_pid"]
        assert call("/api/shutdown", {})["ok"]
        wait(lambda: not alive(sup) and not alive(srv), 20)
        log = (tmp_path / "local" / "OpsDigest" / "logs" / "supervisor.log").read_text(encoding="utf-8")
        assert "сервер завершился сам" in log and "не отвечает" in log and "выход" in log
    finally:
        for pid in (sup, (settings() or {}).get("server_pid")):
            try:
                if alive(pid):
                    os.kill(int(pid), signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                pass
        llm_srv.shutdown()
