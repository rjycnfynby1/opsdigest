"""Самопроверка компьютера: что разрешают политики и сеть, всё ли установлено.

Результаты безопасны для пересылки: адреса серверов и прокси не показываются — только «доступен / нет».
"""
from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TELEGRAM_DC = ("149.154.167.50", 443)  # один из серверов Telegram (DC2)


def _ps(cmd: str, timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                           capture_output=True, timeout=timeout, creationflags=NOWIN)
        return r.returncode, (r.stdout or b"").decode("utf-8", "replace").strip() or (r.stderr or b"").decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        return -1, type(e).__name__


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ValueError, ImportError):
        return mod in sys.modules


def _tcp(host: str, port: int, timeout: float = 5) -> tuple[bool, str]:
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{time.time() - t0:.1f} с"
    except socket.gaierror:
        return False, "имя сервера не найдено (DNS)"
    except socket.timeout:
        return False, "нет ответа (таймаут)"
    except OSError as e:
        return False, type(e).__name__


def _https(url: str, timeout: float = 8) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "OpsDigest-diag"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return e.code < 500, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLError):
            return False, "ошибка сертификата (возможна проверка трафика в сети компании)"
        return False, type(reason).__name__ if not isinstance(reason, str) else "нет связи"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def run_checks(ws: Path | None = None, raw: dict | None = None) -> list[dict]:
    out: list[dict] = []

    def add(group: str, title: str, status: str, detail: str = "") -> None:
        out.append({"group": group, "title": title, "status": status, "detail": detail})

    win = sys.platform == "win32"
    # --- система
    add("Система", "Операционная система", "info", f"{platform.system()} {platform.release()} ({platform.version()}), {platform.machine()}")
    v = sys.version_info
    add("Система", "Python", "ok" if v >= (3, 10) else "fail",
        f"{platform.python_version()}, {'64' if sys.maxsize > 2**32 else '32'}-бит, "
        + ("окружение OpsDigest" if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "системный"))
    for mod, label, need in (("telethon", "Telethon", True), ("python_socks", "python-socks (SOCKS/HTTP-прокси)", True)):
        spec = _has(mod)
        ver = ""
        if spec:
            try:
                from importlib.metadata import version as _ver
                ver = _ver({"python_socks": "python-socks"}.get(mod, mod))
            except Exception:  # noqa: BLE001
                ver = "есть"
        add("Система", label, "ok" if spec else ("fail" if need else "info"), ver or ("нет" if need else "не установлен"))
    if v < (3, 11) and _has("python_socks") and not _has("async_timeout"):
        add("Система", "SOCKS/HTTP-прокси на Python 3.10", "warn", "нет пакета async-timeout — работают только ссылки tg://proxy (MTProxy)")
    try:
        c = sqlite3.connect(":memory:")
        c.execute("create virtual table t using fts5(x)")
        add("Система", "SQLite с поиском (FTS5)", "ok", sqlite3.sqlite_version)
    except Exception as e:  # noqa: BLE001
        add("Система", "SQLite с поиском (FTS5)", "fail", type(e).__name__)

    # --- папки
    app_root = Path(__file__).resolve().parent.parent
    add("Папки", "Папка программы доступна на запись", "ok" if os.access(app_root, os.W_OK) else "warn",
        "" if os.access(app_root, os.W_OK) else "кэш пакетов wheels сохранить не получится — это не страшно")
    if ws:
        okw = ws.exists() and os.access(ws, os.W_OK)
        add("Папки", "Рабочая папка", "ok" if okw else "fail", "" if okw else "нет доступа на запись")
        from .workspace import in_cloud_folder
        if in_cloud_folder(ws):
            add("Папки", "Рабочая папка вне облака", "warn", "папка в OneDrive/облаке — лучше перенести")
        add("Папки", "Вход в Telegram выполнен", "info", "да" if (ws / "data" / "telegram.session").exists() else "нет")
    else:
        add("Папки", "Рабочая папка", "warn", "не выбрана")

    # --- политики Windows
    if win:
        rc, mode = _ps("$ExecutionContext.SessionState.LanguageMode")
        add("Политики", "PowerShell: режим языка", "info", f"{mode or 'не запускается'} (программе не нужен)")
        rc, pol = _ps("Get-ExecutionPolicy")
        add("Политики", "PowerShell: политика выполнения", "info", f"{pol} (программе не мешает: скрипты не запускаются из файлов)")
        try:
            r = subprocess.run(["schtasks", "/Query", "/FO", "LIST"], capture_output=True, timeout=20, creationflags=NOWIN)
            add("Политики", "Планировщик задач (для ежедневной сводки)", "ok" if r.returncode == 0 else "warn",
                "" if r.returncode == 0 else "нет доступа — ежедневную сводку включить не получится, вручную всё работает")
        except Exception as e:  # noqa: BLE001
            add("Политики", "Планировщик задач (для ежедневной сводки)", "warn", type(e).__name__)
        from .app import _browser_app
        add("Политики", "Браузер для окна программы (Edge/Chrome)", "ok" if _browser_app() else "warn",
            "" if _browser_app() else "окно откроется в браузере по умолчанию")

    # --- сеть
    proxies_env = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    sys_proxy = bool(getattr(urllib.request, "getproxies_registry", lambda: {})())
    from .sources.webproxy import windows_proxy_for
    pac = bool(not sys_proxy and windows_proxy_for("https://example.com/"))
    add("Сеть", "Прокси для интернета", "info",
        ("задан в HTTPS_PROXY" if proxies_env else "") + ("; " if proxies_env and sys_proxy else "")
        + ("системный прокси Windows есть" if sys_proxy else ("" if proxies_env else "нет"))
        + ("; автонастройка Windows (PAC) выдаёт прокси" if pac else ""))
    ok, d = _https("https://pypi.org/simple/telethon/")
    add("Сеть", "Доступ к pypi.org (установка пакетов)", "ok" if ok else "warn",
        d if ok else f"{d} — пакеты ставьте из папки wheels")
    ok, d = _https("https://www.python.org/")
    add("Сеть", "Доступ к python.org (скачивание Python)", "ok" if ok else "info", d)
    ok, d = _tcp(*TELEGRAM_DC)
    add("Сеть", "Telegram напрямую", "ok" if ok else "info", d if ok else f"закрыт ({d}) — нужен прокси")
    tg = (raw or {}).get("telegram") or {}
    from .sources.tg_proxy import ProxyError, parse_proxy
    for i, p in enumerate(tg.get("proxies") or [], 1):
        try:
            spec = parse_proxy(p)
        except ProxyError:
            add("Сеть", f"Прокси {i}", "fail", "ссылка не распознана")
            continue
        host = spec.host.split("/")[0]
        ok, d = _tcp(host, spec.port)
        add("Сеть", f"Прокси {i} ({spec.mode})", "ok" if ok else "fail",
            f"порт открыт, {d}" if ok else f"не отвечает: {d}")
    try:
        from .vpn import xray as _xray
        xs = _xray.status()
        vpn_on = bool(((tg.get("vpn") or {}).get("enabled")))
        add("Сеть", "Ядро Xray (VPN по подписке)",
            "ok" if xs.get("installed") else ("fail" if xs.get("problem") or vpn_on else "info"),
            f"версия {xs['version']}" if xs.get("installed")
            else (xs.get("problem") or "не установлено") + (" — а VPN для Telegram включён" if vpn_on else ""))
    except Exception as e:  # noqa: BLE001
        add("Сеть", "Ядро Xray (VPN по подписке)", "warn", type(e).__name__)
    from .netcheck import clock_line, clock_offset, web_urls
    try:
        specs = [parse_proxy(p) for p in tg.get("proxies") or [] if p]
    except ProxyError:
        specs = []
    offset, _ = clock_offset(web_urls(specs), timeout=3)
    add("Сеть", "Точность часов (важно для прокси ee…)",
        "info" if offset is None else ("ok" if abs(offset) < 1.5 else "warn"), clock_line(offset).lstrip("✓⚠• "))
    llm = (raw or {}).get("llm") or {}
    prov = llm.get("provider")
    prof = (llm.get("profiles") or {}).get(prov) or {}
    url = prof.get("base_url") or prof.get("url")
    if url:
        m = re.match(r"https?://([^/:]+)(?::(\d+))?", url)
        if m:
            host, port = m.group(1), int(m.group(2) or (443 if url.startswith("https") else 80))
            ok, d = _tcp(host, port)
            where = "на этом компьютере" if host in ("localhost", "127.0.0.1") else "сервер"
            add("Сеть", f"Модель «{prov}» ({where})", "ok" if ok else "fail", d if ok else f"не отвечает: {d}")
    elif prov == "claude_code":
        exe = shutil.which("claude")
        add("Сеть", "Claude Code (команда claude)", "ok" if exe else "fail", "найден" if exe else "не найден в PATH")

    # --- время
    off = time.strftime("%z")
    add("Система", "Часовой пояс", "info", f"UTC{off[:3]}:{off[3:]}" if off else "?")
    return out


def summary(checks: list[dict]) -> str:
    icon = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}
    lines, group = [], None
    for c in checks:
        if c["group"] != group:
            group = c["group"]
            lines.append(f"[{group}]")
        lines.append(f"  {icon.get(c['status'], '?')} {c['title']}" + (f": {c['detail']}" if c["detail"] else ""))
    return "\n".join(lines)
