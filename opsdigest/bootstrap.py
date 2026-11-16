"""Проверка и установка зависимостей перед запуском окна (вызывает OpsDigest.bat). Только стандартная библиотека.

- Всё уже стоит — выходит сразу.
- Есть папка wheels/ рядом с программой — ставит из неё, без интернета.
- Иначе ставит из интернета (pip; корпоративный прокси — переменная HTTPS_PROXY) и складывает пакеты в wheels/,
  чтобы папку с программой можно было унести на компьютер без интернета.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
WHEELS = ROOT / "wheels"


STAMP = Path(sys.prefix) / "opsdigest-deps.stamp"   # внутри окружения: новое окружение — новая проверка


def _req_names() -> list[str]:
    try:
        lines = REQ.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ["telethon", "python-socks"]
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line.split("#", 1)[0])
        if m:
            out.append(m.group(1))
    return out


def _stamp() -> str:
    """Отпечаток списка пакетов и папки wheels: изменился (пришло обновление) — проверить установку ещё раз."""
    h = hashlib.sha256()
    try:
        h.update(REQ.read_bytes())
    except OSError:
        pass
    if WHEELS.is_dir():
        for w in sorted(p.name for p in WHEELS.glob("*.whl")):
            h.update(w.encode())
    return h.hexdigest()[:16]


def missing() -> list[str]:
    out = [m for m in ("telethon", "python_socks") if importlib.util.find_spec(m) is None]
    for name in _req_names():
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            if name.replace("-", "_").lower() not in out:
                out.append(name)
    return out


def changed() -> bool:
    try:
        return STAMP.read_text(encoding="utf-8").strip() != _stamp()
    except OSError:
        return True


def _mark() -> None:
    try:
        STAMP.write_text(_stamp(), encoding="utf-8")
    except OSError:
        pass


def _pip_log() -> Path:
    from .logs import log_dir
    return log_dir() / "pip.log"


def pip(*args: str) -> int:
    """pip с выводом в logs\\pip.log (в отчёт об ошибке он попадёт уже обезличенным)."""
    cmd = [sys.executable, "-m", "pip", "--disable-pip-version-check", *args]
    with open(_pip_log(), "a", encoding="utf-8", errors="replace") as f:
        f.write(f"\n$ pip {' '.join(a for a in args if not a.startswith(('-q',)))}\n")
        f.flush()
        start = f.tell()
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    if rc and "--no-index" not in args:
        recent = _pip_log().read_bytes()[start:].decode("utf-8", errors="replace")
        if "Missing dependencies for SOCKS support" in recent:
            # pip needs PySocks, not python-socks. Bootstrap from a local wheel
            # without changing the user's proxy or pip configuration.
            wheel = WHEELS / "PySocks-1.7.1-py3-none-any.whl"
            if wheel.is_file():
                print("  добавляю поддержку SOCKS для pip из локального пакета…", flush=True)
                if pip("install", "--no-index", "--no-deps", str(wheel)) == 0:
                    with open(_pip_log(), "a", encoding="utf-8", errors="replace") as f:
                        return subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
            else:
                print("  Для SOCKS-прокси нужен локальный wheels/PySocks-1.7.1-py3-none-any.whl.\n"
                      "  Добавьте его с компьютера с доступом к PyPI; настройки прокси сохранены.", flush=True)
    return rc


def main() -> int:
    from . import logs
    log = logs.setup("bootstrap")
    if not missing():
        if changed() and WHEELS.is_dir() and any(WHEELS.glob("*.whl")):
            # после обновления программы: требования могли поменяться — довести пакеты до нужных версий из wheels
            rc = pip("install", "-q", "--no-index", "--find-links", str(WHEELS), "-r", str(REQ))
            log.info("пакеты после обновления: код pip %s", rc)
            if rc != 0 and missing():
                print("\n".join(logs.tail(_pip_log(), 15)), flush=True)
                return 1
        _mark()
        return 0
    log.info("нет пакетов: %s; Python %s; папка wheels: %s", ", ".join(missing()), sys.version.split()[0],
             "есть" if WHEELS.is_dir() and any(WHEELS.glob("*.whl")) else "нет")
    print("Ставлю пакеты: " + ", ".join(missing()), flush=True)
    rc = 1
    if WHEELS.is_dir() and any(WHEELS.glob("*.whl")):
        print("  из папки wheels (без интернета)…", flush=True)
        rc = pip("install", "-q", "--no-index", "--find-links", str(WHEELS), "-r", str(REQ))
    if rc != 0 or missing():
        print("  из интернета (pip)…", flush=True)
        rc = pip("install", "-q", "-r", str(REQ))
        if rc == 0:
            print("  сохраняю пакеты в wheels\\ — для переноса на компьютер без интернета…", flush=True)
            if pip("wheel", "-q", "-r", str(REQ), "-w", str(WHEELS)) != 0:
                print("  (не получилось — не страшно)", flush=True)
    if rc != 0 or missing():
        log.error("пакеты не установились: код pip %s, всё ещё нет: %s", rc, ", ".join(missing()))
        print("\n".join(logs.tail(_pip_log(), 15)), flush=True)
        print("\n[!] Пакеты не установились.\n"
              "    Если интернет идёт через корпоративный прокси, перед запуском выполните в этом окне:\n"
              "      set HTTPS_PROXY=http://адрес-прокси:порт\n"
              "    или принесите папку wheels с компьютера, где OpsDigest уже запускался, и положите рядом с OpsDigest.bat.",
              flush=True)
        return 1
    log.info("пакеты установлены")
    _mark()
    print("Готово.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
