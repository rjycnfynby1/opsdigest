"""Ядро Xray для OpsDigest: установка, запуск локального SOCKS-прокси, проверка серверов подписки.

Xray (github.com/XTLS/Xray-core, лицензия MPL-2.0) — то же ядро, что внутри Happ/v2rayN. Программа кладёт xray.exe
в %LOCALAPPDATA%\\OpsDigest\\xray и запускает его скрытым процессом: на 127.0.0.1 открывается по входу SOCKS5 на
каждый сервер подписки, Telegram (Telethon) подключается через выбранный вход. Системный VPN не включается —
через Xray идёт только трафик OpsDigest.
"""
from __future__ import annotations

import asyncio
import atexit
import dataclasses
import hashlib
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from .. import logs
from .links import Server, VpnError, xray_compatible

log = logs.get("vpn")

RELEASES_API = "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=15"
RELEASES_PAGE = "https://github.com/XTLS/Xray-core/releases"
LATEST_DOWNLOAD = "https://github.com/XTLS/Xray-core/releases/latest/download/"
MAX_SERVERS = 40


def xray_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    d = Path(base) / "OpsDigest" / "xray"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_name() -> str:
    m = platform.machine().lower()
    arm = m in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "Xray-windows-arm64-v8a.zip" if arm else ("Xray-windows-64.zip" if sys.maxsize > 2**32 else "Xray-windows-32.zip")
    if sys.platform == "darwin":
        return "Xray-macos-arm64-v8a.zip" if arm else "Xray-macos-64.zip"
    return "Xray-linux-arm64-v8a.zip" if arm else "Xray-linux-64.zip"


def exe_path() -> Path:
    override = os.environ.get("OPSDIGEST_XRAY")          # для тестов и ручной установки
    if override:
        return Path(override)
    return xray_dir() / ("xray.exe" if sys.platform == "win32" else "xray")


def _cmd() -> list[str]:
    exe = exe_path()
    return [sys.executable, str(exe)] if exe.suffix == ".py" else [str(exe)]


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def version() -> str | None:
    """Версия установленного Xray или None (нет файла, не запускается, удалён антивирусом)."""
    if not exe_path().exists():
        return None
    try:
        r = subprocess.run([*_cmd(), "version"], capture_output=True, text=True, timeout=15, creationflags=_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Xray\s+(\d+(?:\.\d+)+)", r.stdout or "")
    return m.group(1) if m else None


def status() -> dict:
    exe = exe_path()
    v = version()
    if v:
        return {"installed": True, "version": v}
    if exe.exists():
        return {"installed": False, "problem": "файл xray есть, но не запускается — заблокирован антивирусом или политикой?"}
    return {"installed": False}


# ============================================================== установка
def _get(url: str, timeout: float = 60, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "OpsDigest", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — только github.com
        return r.read()


def _why(e: Exception) -> str:
    code = getattr(e, "code", None)
    return f"HTTP {code}" if code else type(e).__name__


def _download(url: str, name: str, progress) -> str:
    progress(f"Скачиваю {name} (около 20 МБ)…")
    data = _get(url + name, 300)
    dgst = _get(url + name + ".dgst", 30).decode("utf-8", "replace")
    want = re.search(r"SHA2-256\s*[=:]\s*([0-9a-fA-F]{64})", dgst)
    if not want or want.group(1).lower() != hashlib.sha256(data).hexdigest():
        raise VpnError("контрольная сумма скачанного Xray не совпала — файл повреждён или подменён")
    return _install_zip(data, progress)


def install_from_github(progress=lambda s: None) -> str:
    """Скачать Xray с GitHub (через прокси Windows, как браузер), сверить SHA-256 и установить.

    Сначала — последний стабильный выпуск по прямой ссылке, если не вышло — через список выпусков (API)."""
    name = asset_name()
    manual = f"Скачайте {name} вручную со страницы {RELEASES_PAGE} и укажите файл в окне"
    progress("Скачиваю последний выпуск Xray с GitHub…")
    try:
        return _download(LATEST_DOWNLOAD, name, progress)
    except VpnError:
        raise
    except Exception as e:  # noqa: BLE001
        first = _why(e)
        log.info("Xray: прямая ссылка не сработала (%s), пробую список выпусков", first)
    try:
        releases = json.loads(_get(RELEASES_API, 30, "application/vnd.github+json"))
    except Exception as e:  # noqa: BLE001
        raise VpnError(f"GitHub недоступен с этого компьютера ({first}; {_why(e)}). {manual}") from None
    for rel in releases:
        assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or []}
        if name in assets and name + ".dgst" in assets:
            base = assets[name][: -len(name)]
            try:
                return _download(base, name, progress)
            except VpnError:
                raise
            except Exception as e:  # noqa: BLE001
                raise VpnError(f"не удалось скачать Xray ({_why(e)}). {manual}") from None
    raise VpnError(f"на GitHub не найден файл {name}. {manual}")


def install_from_zip(path: str | Path, progress=lambda s: None) -> str:
    p = Path(str(path).strip().strip('"'))
    if not p.exists():
        raise VpnError("файл не найден — укажите полный путь к скачанному архиву Xray (…zip)")
    if p.suffix.lower() == ".exe":
        return _install_exe(p.read_bytes(), progress)
    return _install_zip(p.read_bytes(), progress)


def _install_zip(data: bytes, progress) -> str:
    want = "xray.exe" if sys.platform == "win32" else "xray"
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise VpnError("это не zip-архив Xray") from None
    member = next((n for n in z.namelist() if n.rsplit("/", 1)[-1].lower() == want), None)
    if not member:
        raise VpnError(f"в архиве нет {want} — нужен архив {asset_name()}")
    return _install_exe(z.read(member), progress)


def _install_exe(blob: bytes, progress) -> str:
    stop_all()
    dst = exe_path()
    tmp = dst.with_suffix(dst.suffix + ".new")
    tmp.write_bytes(blob)
    if sys.platform != "win32":
        tmp.chmod(0o755)
    os.replace(tmp, dst)
    progress("Проверяю, что Xray запускается (и его не удалил антивирус)…")
    time.sleep(2)
    if not dst.exists():
        raise VpnError("файл Xray пропал сразу после установки — его удалил антивирус")
    v = version()
    if not v:
        raise VpnError("Xray установлен, но не запускается — похоже, его блокирует антивирус или политика компьютера")
    log.info("Xray установлен: %s", v)
    return v


# ============================================================== запуск
def _free_ports(n: int) -> list[int]:
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def build_config(servers: list[Server], ports: list[int], log_file: Path) -> dict:
    inbounds, outbounds, rules = [], [], []
    for i, (srv, port) in enumerate(zip(servers, ports)):
        inbounds.append({"tag": f"in{i}", "listen": "127.0.0.1", "port": port, "protocol": "socks",
                         "settings": {"auth": "noauth", "udp": False}})
        outbounds.append({"tag": f"s{i}", **srv.outbound})
        rules.append({"type": "field", "inboundTag": [f"in{i}"], "outboundTag": f"s{i}"})
    outbounds.append({"tag": "block", "protocol": "blackhole"})
    return {"log": {"loglevel": "warning", "access": "none", "error": str(log_file)},
            "inbounds": inbounds, "outbounds": outbounds, "routing": {"domainStrategy": "AsIs", "rules": rules}}


class XrayRunner:
    """Один процесс Xray на всё приложение; на каждый сервер — свой локальный SOCKS-порт."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.ports: dict[str, int] = {}
        self.names: dict[str, str] = {}
        self.skipped: dict[str, str] = {}   # ключ сервера → почему Xray его не принял
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, servers: list[Server]) -> dict[str, int]:
        with self.lock:
            servers = servers[:MAX_SERVERS]
            if self.running() and set(self.ports) | set(self.skipped) == {s.key for s in servers}:
                return dict(self.ports)
            self._stop()
            if not servers:
                raise VpnError("нет серверов для запуска")
            if not version():
                raise VpnError("Xray не установлен или не запускается — установите его в окне (шаг Telegram → VPN)")
            d = xray_dir()
            _kill_stale(d / "xray.pid")
            log_file = d / "xray.log"
            try:
                log_file.unlink()
            except OSError:
                pass
            cfg = d / "run.json"
            servers, ports = self._usable(servers, cfg, log_file)
            with open(d / "xray.out", "wb") as out:  # у процесса своя копия дескриптора
                self.proc = subprocess.Popen([*_cmd(), "run", "-c", str(cfg)], cwd=str(d), stdout=out,
                                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=_flags())
            (d / "xray.pid").write_text(str(self.proc.pid))
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    tail = _tail(d / "xray.out") or _tail(log_file)
                    self.proc = None
                    raise VpnError("Xray не запустился: " + (_reason(logs.scrub(tail)) or "без подробностей"))
                if _listening(ports[0]):
                    break
                time.sleep(0.2)
            else:
                self._stop()
                raise VpnError("Xray не открыл локальный порт за 10 с")
            self.ports = {s.key: p for s, p in zip(servers, ports)}
            self.names = {s.key: s.name for s in servers}
            log.info("Xray запущен: серверов %d, не принято %d", len(servers), len(self.skipped))
            return dict(self.ports)

    def _usable(self, servers: list[Server], cfg: Path, log_file: Path) -> tuple[list[Server], list[int]]:
        """Серверы, которые принимает установленный Xray, и порты для них; run.json уже записан.

        Один неподходящий сервер (устаревший параметр в подписке) не должен ронять весь Xray: сначала
        outbound приводится к нынешнему формату, затем `xray run -test` — и сервер, на котором проверка
        падает (ошибка называет его тег sN), исключается с причиной в self.skipped."""
        self.skipped = {}
        names = {}
        usable: list[Server] = []
        for srv in servers:
            ob, why = xray_compatible(srv.outbound)
            if why:
                self.skipped[srv.key] = why
                names[srv.key] = srv.name
            else:
                usable.append(dataclasses.replace(srv, outbound=ob))
        while True:
            if not usable:
                first = next(iter(self.skipped.values()), "")
                raise VpnError("Xray не принял ни одного сервера подписки" + (f": {first}" if first else ""))
            ports = _free_ports(len(usable))
            cfg.write_text(json.dumps(build_config(usable, ports, log_file), ensure_ascii=False), encoding="utf-8")
            try:
                r = subprocess.run([*_cmd(), "run", "-test", "-c", str(cfg)], capture_output=True, text=True,
                                   timeout=60, cwd=str(cfg.parent), creationflags=_flags(), errors="replace")
            except subprocess.TimeoutExpired:
                return usable, ports                       # проверка зависла — пусть скажет настоящий запуск
            if r.returncode == 0:
                break
            text = logs.scrub(" | ".join(l for l in ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines()
                                         if l.strip()))
            m = re.search(r"tag s(\d+)\b", text)
            if not m or int(m.group(1)) >= len(usable):
                raise VpnError("Xray не запустился: " + (_reason(text) or "без подробностей"))
            bad = usable.pop(int(m.group(1)))
            self.skipped[bad.key] = _reason(text)
            log.info("Xray не принял сервер: %s", self.skipped[bad.key])
        for key, why in self.skipped.items():
            log.info("VPN: сервер пропущен — %s", why)
        return usable, ports

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.ports = {}

    def stop(self) -> None:
        with self.lock:
            self._stop()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.3):
            return True
    except OSError:
        return False


def _reason(text: str) -> str:
    """Суть ошибки Xray: самое глубокое звено цепочки «… > … > причина», без служебного заголовка."""
    text = re.sub(r"^.*?Failed to start:\s*", "", text or "", flags=re.S)
    parts = [p.strip(" |") for p in re.split(r"\s>\s", text) if p.strip(" |")]
    if not parts:
        return text.strip()[:500]
    last = parts[-1]
    return (last if len(parts) == 1 else f"{last} (в {parts[1] if len(parts) > 2 else parts[0]})")[:500]


def _tail(p: Path, n: int = 3) -> str:
    try:
        lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        return ""
    return " | ".join(lines[-n:])


def _kill_stale(pid_file: Path) -> None:
    """Xray, оставшийся от прошлого запуска (окно закрыли аварийно), — завершить, если это действительно он."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        if sys.platform == "win32":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                               text=True, timeout=10, creationflags=_flags())
            if "xray" in (r.stdout or "").lower():
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10, creationflags=_flags())
        else:
            cmdline = Path(f"/proc/{pid}/cmdline")
            if cmdline.exists() and b"xray" in cmdline.read_bytes().lower():
                os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        pid_file.unlink()
    except OSError:
        pass


RUNNER = XrayRunner()


def stop_all() -> None:
    RUNNER.stop()


atexit.register(stop_all)  # окно закрыли — Xray не должен оставаться в фоне


# ============================================================== проверка серверов
async def _ping(host: str, port: int, timeout: float) -> float | None:
    t0 = time.monotonic()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return round((time.monotonic() - t0) * 1000)
    except (OSError, asyncio.TimeoutError):
        return None


async def test_servers(servers: list[Server], ports: dict[str, int], timeout: float = 10,
                       progress=lambda s: None) -> list[dict]:
    """Для каждого сервера: TCP до сервера напрямую (пинг) и ответ Telegram через него (главное)."""
    from ..netcheck import _telegram
    from ..sources.tg_proxy import ProxySpec
    sem = asyncio.Semaphore(6)

    async def one(s: Server) -> dict:
        async with sem:
            row = {"key": s.key, "name": s.name, "label": s.label(), "ping_ms": None, "tg_ok": False, "tg_ms": None,
                   "error": ""}
            row["ping_ms"] = await _ping(s.host, s.port, min(timeout, 5))
            port = ports.get(s.key)
            if not port:
                row["error"] = ("Xray не принял сервер: " + RUNNER.skipped[s.key]) if s.key in RUNNER.skipped \
                    else "не запущен"
                progress(f"✗ {s.name}: {row['error']}")
                return row
            t0 = time.monotonic()
            ok, line = await _telegram(ProxySpec("socks5", "127.0.0.1", port, raw="vpn"), None, timeout)
            row["tg_ok"] = ok
            if ok:
                row["tg_ms"] = round((time.monotonic() - t0) * 1000)
            else:
                row["error"] = line.lstrip("✗ ").replace("через этот вариант", "через этот сервер")
            progress(f"{'✓' if ok else '✗'} {s.name}: " + (f"Telegram отвечает ({row['tg_ms']} мс)" if ok else row["error"]))
            return row

    return list(await asyncio.gather(*(one(s) for s in servers[:MAX_SERVERS])))


def rank(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (not r.get("tg_ok"), r.get("tg_ms") or 10**9, r.get("ping_ms") or 10**9))
