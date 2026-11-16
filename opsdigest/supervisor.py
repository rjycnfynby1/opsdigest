"""Сторож OpsDigest: держит сервер окна запущенным и поднимает его снова, если тот упал или завис.

Процессы:
  OpsDigest.bat → python -m opsdigest.app              запуск: если программа уже работает — открыть окно; иначе
                                                       запустить сторожа, дождаться ответа сервера, открыть окно
  pythonw -m opsdigest.app --supervise                 сторож (без консоли — его не закрыть случайно)
      └ python -m opsdigest.app --serve                сервер окна: Telegram, задачи, база

Сторож и сервер говорят по своим stdin/stdout строками «@@OD {json}»:
  сервер → сторож: ready (порт), key (ключ данных после входа по паролю), forget (полная блокировка),
                   quit («Выход»: не поднимать), update (перезапуск после обновления — его ведёт помощник updater);
  сторож → сервер: key (после перезапуска — чтобы задачи продолжились без пароля; окно закрыто ПИН-кодом).
Ключ живёт только в памяти этих двух процессов. Сторож закрыли (выход из Windows, диспетчер задач) — при
следующем запуске, как и раньше, нужен пароль.

Сервер перезапускается, если процесс завершился сам (сбой) или перестал отвечать на /api/ping дольше
HANG_SECONDS (сон компьютера не считается: после пробуждения отсчёт начинается заново).
"""
from __future__ import annotations

import base64
import collections
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import logs
from . import workspace as wsmod

PREFIX = "@@OD "
HANG_SECONDS = int(os.environ.get("OPSDIGEST_HANG_SECONDS") or 240)  # не отвечает столько (без учёта сна) — перезапуск
PING_EVERY = int(os.environ.get("OPSDIGEST_PING_EVERY") or 20)
RESTART_DELAYS = [1, 3, 10, 30, 60, 120, 300]
STABLE_SECONDS = 600        # проработал столько — счётчик сбоев сначала
CREATE_NO_WINDOW, DETACHED, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000008, 0x00000200, 0x01000000

log = logs.get("supervisor")


# ================================================================ сторона сервера
class Channel:
    """Связь сервера со сторожем. Создаётся в процессе --serve, если его запустил сторож."""

    def __init__(self):
        self._out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
        try:  # всё, что печатают print(), — в stderr (его читает сторож), чтобы не путалось с сообщениями
            os.dup2(2, 1)
        except OSError:
            pass
        self._lock = threading.Lock()
        self.on_key = None          # fn(ws: str, key: bytes)
        self.pending_key: tuple[str, bytes] | None = None
        threading.Thread(target=self._reader, daemon=True, name="supervisor-in").start()

    def send(self, kind: str, **kw) -> None:
        try:
            with self._lock:
                self._out.write(PREFIX + json.dumps({"t": kind, **kw}, ensure_ascii=False) + "\n")
                self._out.flush()
        except (OSError, ValueError):
            pass

    def send_key(self, ws, key: bytes | None) -> None:
        if ws and key:
            self.send("key", ws=str(ws), key=base64.b64encode(key).decode("ascii"))

    def _reader(self) -> None:
        try:
            for raw in sys.stdin.buffer:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith(PREFIX):
                    continue
                try:
                    msg = json.loads(line[len(PREFIX):])
                except ValueError:
                    continue
                if msg.get("t") == "key" and msg.get("key"):
                    item = (str(msg.get("ws") or ""), base64.b64decode(msg["key"]))
                    if self.on_key:
                        self.on_key(*item)
                    else:
                        self.pending_key = item
        except (OSError, ValueError):
            pass


CHANNEL: Channel | None = None


def channel() -> Channel | None:
    """Канал к сторожу (только в процессе --serve под сторожем)."""
    global CHANNEL
    if CHANNEL is None and os.environ.get("OPSDIGEST_SUPERVISED") == "1":
        CHANNEL = Channel()
    return CHANNEL


# ================================================================ сам сторож
def _ping(port: int, token: str, timeout: float = 5) -> dict | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # мимо прокси Windows
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-Token": token})
        with opener.open(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
            return j if j.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) != 0   # 0 — процесс уже завершился
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def python_console() -> str:
    """python.exe рядом с pythonw.exe (сервер пишет в stderr — ему нужен настоящий поток)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def python_windowless() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if os.name == "nt" and w.exists() else exe)


class _Lock:
    """Один сторож на пользователя: файл, запертый на всё время работы."""

    def __init__(self, path: Path):
        self.path = path
        self.f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.f.close()
            self.f = None
            return False


class Supervisor:
    def __init__(self, port: int = 0, token: str = "", root: Path | None = None):
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        self.root = root or Path(__file__).resolve().parent.parent
        self.key: tuple[str, bytes] | None = None
        self.proc: subprocess.Popen | None = None
        self.quit = False          # «Выход»: сервер завершился сам — не поднимать
        self.update = False        # обновление: перезапуск ведёт помощник updater
        self.ready = threading.Event()
        self.tail: collections.deque[str] = collections.deque(maxlen=60)
        self.restarts = 0
        self.started_at = 0.0
        self.boot = secrets.token_hex(4)

    # ---- процесс сервера
    def spawn(self) -> None:
        # порт — прежний (окно переподключится само); занят — сервер возьмёт свободный и сообщит его в «ready»
        env = {**os.environ, "OPSDIGEST_SUPERVISED": "1", "OPSDIGEST_PORT": str(self.port),
               "OPSDIGEST_TOKEN": self.token, "OPSDIGEST_SUPERVISOR_PID": str(os.getpid()),
               "PYTHONUTF8": "1", "PYTHONPATH": str(self.root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        kw: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "cwd": str(self.root), "env": env}
        if os.name == "nt":
            kw["creationflags"] = CREATE_NO_WINDOW
        self.ready.clear()
        self.quit = self.update = False
        self.proc = subprocess.Popen([python_console(), "-m", "opsdigest.app", "--serve"], **kw)
        self.started_at = time.time()
        threading.Thread(target=self._read_out, args=(self.proc,), daemon=True, name="srv-out").start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True, name="srv-err").start()
        if self.key:
            self._send(self.proc, "key", ws=self.key[0], key=base64.b64encode(self.key[1]).decode("ascii"))
        log.info("сервер запущен (pid %s, %s, перезапусков %d)", self.proc.pid,
                 "прежний порт" if self.port else "порт выберет сам", self.restarts)

    def _send(self, proc: subprocess.Popen, kind: str, **kw) -> None:
        try:
            proc.stdin.write((PREFIX + json.dumps({"t": kind, **kw}) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _read_out(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line.startswith(PREFIX):
                self.tail.append(line)
                continue
            try:
                msg = json.loads(line[len(PREFIX):])
            except ValueError:
                continue
            t = msg.get("t")
            if t == "ready":
                self.port = int(msg.get("port") or self.port)
                self.save_settings()
                self.ready.set()
            elif t == "key" and msg.get("key"):
                self.key = (str(msg.get("ws") or ""), base64.b64decode(msg["key"]))
            elif t == "forget":
                self.key = None
            elif t == "quit":
                self.quit = True
                self.key = None
            elif t == "update":
                self.update = True
                self.key = None

    def _read_err(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            self.tail.append(raw.decode("utf-8", "replace").rstrip())

    def save_settings(self) -> None:
        wsmod.save_app_settings({"port": self.port, "token": self.token, "supervisor_pid": os.getpid(),
                                 "server_pid": self.proc.pid if self.proc else None, "boot": self.boot})

    def kill(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(5)
            except Exception:  # noqa: BLE001
                pass

    # ---- главный цикл
    def run(self) -> int:
        crashes: list[float] = []
        from . import power
        power.no_throttling()   # сторож без окна — Windows не должна урезать ему процессор
        self.spawn()
        last_ok = time.time()
        last_tick = time.time()
        next_ping = time.time() + PING_EVERY
        while True:
            time.sleep(1)
            now = time.time()
            if now - last_tick > 30:  # компьютер спал: зависанием это не считаем
                log.info("пробуждение после сна (≈%d мин)", round((now - last_tick) / 60))
                last_ok = now
                next_ping = now + 5
            last_tick = now
            code = self.proc.poll()
            if code is not None:
                if self.quit or self.update:
                    log.info("сервер завершён (%s) — сторож тоже", "выход" if self.quit else "обновление")
                    return 0
                tail = logs.scrub("\n".join(list(self.tail)[-25:]))
                log.error("сервер завершился сам (код %s) — перезапускаю%s", code, f"\n{tail}" if tail.strip() else "")
                if now - self.started_at > STABLE_SECONDS:
                    crashes.clear()
                crashes.append(now)
                delay = RESTART_DELAYS[min(len(crashes), len(RESTART_DELAYS)) - 1]
                time.sleep(delay)
                self.restarts += 1
                self.spawn()
                last_ok = last_tick = time.time()
                next_ping = last_ok + PING_EVERY
                continue
            if now >= next_ping:
                next_ping = now + PING_EVERY
                if self.ready.is_set() and _ping(self.port, self.token, timeout=8):
                    last_ok = now
                elif now - last_ok > HANG_SECONDS:
                    log.error("сервер не отвечает %d с — перезапускаю", round(now - last_ok))
                    self.kill(self.proc)  # следующий круг увидит завершение и поднимет заново


def run_supervisor() -> int:
    logs.setup("supervisor", "supervisor.log")
    lock = _Lock(wsmod.app_dir() / "supervisor.lock")
    if not lock.acquire():
        log.info("сторож уже работает — второй не нужен")
        return 0
    sup = Supervisor(port=int(os.environ.get("OPSDIGEST_PORT") or 0), token=os.environ.pop("OPSDIGEST_TOKEN", ""))
    os.environ.pop("OPSDIGEST_PORT", None)
    log.info("сторож запущен (pid %s)", os.getpid())
    try:
        return sup.run()
    finally:
        if sup.proc and sup.proc.poll() is None and not (sup.quit or sup.update):
            sup.kill(sup.proc)


def start_detached(extra_env: dict | None = None) -> subprocess.Popen:
    """Запустить сторожа отдельно от консоли (закрытие консоли его не остановит)."""
    env = {**os.environ, **(extra_env or {})}
    root = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "cwd": str(root), "env": env, "close_fds": True}
    args = [python_windowless(), "-m", "opsdigest.app", "--supervise"]
    if os.name == "nt":
        for flags in (DETACHED | NEW_GROUP | BREAKAWAY, DETACHED | NEW_GROUP):
            try:  # BREAKAWAY — чтобы не закрылся вместе с заданием, в котором запущена консоль (если разрешено)
                return subprocess.Popen(args, creationflags=flags, **kw)
            except OSError:
                continue
    return subprocess.Popen(args, start_new_session=True, **kw)
