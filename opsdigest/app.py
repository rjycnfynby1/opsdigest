"""Запуск OpsDigest.

  python -m opsdigest.app              (его вызывает OpsDigest.bat) — если программа уже работает, показать окно;
                                       иначе запустить сторожа, дождаться сервера и открыть окно.
  python -m opsdigest.app --supervise  сторож: держит сервер запущенным, поднимает после сбоя (opsdigest/supervisor.py)
  python -m opsdigest.app --serve      сервер окна (под сторожем; без него — тоже работает, но не поднимется сам)

Сервер работает постоянно: закрытое окно, блокировка экрана и сон компьютера его не останавливают.
Остановить — кнопкой «Выход» в окне.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import logs
from . import workspace as wsmod

START_WAIT = 90   # сколько ждать первого ответа сервера при запуске


def _alive(port: int, token: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-Token": token})
        with opener.open(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception:  # noqa: BLE001
        return False


def _browser_app() -> str | None:
    """Edge или Chrome — чтобы открыть окно без адресной строки (--app)."""
    cands = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        w = shutil.which(name)
        if w:
            return w
    return None


def open_window(url: str, force: bool = False) -> None:
    if os.environ.get("OPSDIGEST_NO_WINDOW") and not force:
        return
    if os.environ.get("OPSDIGEST_EMBEDDED") == "1":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpsDigest"
        shell = base / "embedded-builder/OpsDigest.Embedded.exe"
        if not shell.is_file():
            shell = base / "embedded-prototype/OpsDigest.Embedded.exe"
        if not shell.is_file():
            raise RuntimeError("Сначала соберите встроенную оболочку: tools/embedded-browser/build.ps1")
        subprocess.Popen([str(shell), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    exe = _browser_app()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1180,900", "--new-window"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


# ================================================================ запуск из OpsDigest.bat
def launch() -> int:
    _utf8()
    log = logs.setup("launch", "supervisor.log")
    st = wsmod.load_app_settings()
    port, token = st.get("port"), st.get("token")
    if port and token and _alive(int(port), token):
        open_window(f"http://127.0.0.1:{port}/#t={token}")
        print("OpsDigest уже работает — открыл окно.")
        return 0
    from .supervisor import pid_alive, start_detached
    old_boot = st.get("boot")
    spawned = not pid_alive(st.get("supervisor_pid"))
    if spawned:
        print("Запускаю OpsDigest…")
        start_detached()
    else:  # сторож есть, сервер как раз перезапускается — подождать его
        print("OpsDigest перезапускается — жду…")
        old_boot = object()
    t0 = time.monotonic()
    end = t0 + START_WAIT
    while time.monotonic() < end:
        time.sleep(0.5)
        if not spawned and time.monotonic() - t0 > 30:  # «сторож» оказался чужим процессом с тем же номером
            start_detached()
            spawned = True
        st = wsmod.load_app_settings()
        port, token = st.get("port"), st.get("token")
        if port and token and st.get("boot") != old_boot and _alive(int(port), token):
            open_window(f"http://127.0.0.1:{port}/#t={token}")
            print("OpsDigest работает. Окно можно закрывать — программа продолжит работу в фоне; "
                  "остановить — кнопкой «Выход» в окне.")
            log.info("запуск: сервер отвечает")
            return 0
    log.error("запуск: сервер не ответил за %d с", START_WAIT)
    print(f"\n  [!] OpsDigest не ответил за {START_WAIT} с. Подробности — в отчёте об ошибке.")
    return 1


# ================================================================ сервер окна
def serve() -> int:
    _utf8()
    log = logs.setup("app")
    from . import supervisor
    from .ui.server import App, make_server
    ch = supervisor.channel()
    app = App(supervisor=ch)
    want = int(os.environ.get("OPSDIGEST_PORT") or 0)
    os.environ.pop("OPSDIGEST_PORT", None)
    moved = False
    try:  # после перезапуска и обновления — тот же порт и ключ: открытое окно переподключится само
        srv = make_server(app, want)
    except OSError:
        srv, moved = make_server(app, 0), True
    url = f"http://127.0.0.1:{app.port}/#t={app.token}"
    if ch:
        ch.send("ready", port=app.port)
    else:
        wsmod.save_app_settings({"port": app.port, "token": app.token})
        print(f"OpsDigest работает: {url}\nОстановить — кнопкой «Выход» в окне или Ctrl+C здесь.")
    import platform
    log.info("старт: версия %s, Python %s (%s), %s %s, окно: %s%s", __import__("opsdigest").__version__,
             platform.python_version(), platform.machine(), platform.system(), platform.release(),
             "Edge/Chrome --app" if _browser_app() else "браузер по умолчанию", ", под сторожем" if ch else "")
    if moved or not ch:  # порт заняли — у открытого окна старый адрес: открыть новое
        open_window(url, force=moved)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("сервер окна остановился: %s\n%s", logs.err_info(e), logs.frames(e))
        raise
    finally:
        app.stop()
    log.info("остановлен")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--serve" in args:
        return serve()
    if "--supervise" in args:
        from .supervisor import run_supervisor
        return run_supervisor()
    return launch()


if __name__ == "__main__":
    sys.exit(main())
