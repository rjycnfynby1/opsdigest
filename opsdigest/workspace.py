"""Рабочая папка пользователя и настройки приложения.

Рабочая папка (её выбирают в окне) хранит всё личное: config.json (настройки и ключи), data/ (база, сессия Telegram),
reports/ (отчёты). Папка с программой при этом остаётся общей — её можно обновлять и давать коллегам.
Последняя выбранная рабочая папка запоминается в %APPDATA%\\OpsDigest\\app.json.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "OpsDigest daily"


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "OpsDigest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_app_settings() -> dict:
    from .secure import atomic
    p = app_dir() / "app.json"
    try:
        return json.loads(atomic.read_bytes(p).decode("utf-8"))
    except (OSError, ValueError):
        return {}


def save_app_settings(data: dict) -> None:
    cur = load_app_settings()
    cur.update(data)
    _atomic_write(app_dir() / "app.json", json.dumps(cur, ensure_ascii=False, indent=1))


def default_workspace() -> Path:
    # не в OneDrive: там синхронизация мешает базе SQLite, а сессии Telegram нечего делать в облаке
    return Path.home() / "OpsDigest"


def in_cloud_folder(path: str | Path) -> bool:
    s = str(path).lower()
    return any(x in s for x in ("onedrive", "dropbox", "google drive", "yandex.disk", "яндекс.диск"))


# Стартовые настройки новой рабочей папки (то же, что config.example.toml, но в JSON)
STARTER: dict = {
    "profile": {"name": "", "role": "", "aliases": [], "responsibilities": "", "ignore_topics": [],
                "competences": []},
    "telegram": {
        "api_id": "", "api_hash": "", "phone": "", "proxies": [], "try_direct": False,
        "first_sync_days": 2, "max_messages_per_chat": 400,
        "defaults": {"private": "high", "group": "normal", "channel": "low", "bot": "ignore"},
        "chats": [
            {"match": "Авария", "priority": "high", "kind": "incident"},
            {"match": "re:(?i)мониторинг|алерт|alert|zabbix|grafana", "priority": "normal", "kind": "alerts"},
        ],
    },
    "llm": {"provider": "local", "profiles": {
        "local": {"type": "openai", "base_url": "http://localhost:1235/v1", "api_key": "lm-studio", "model": "auto",
                  "prefer": "instruct", "max_context_tokens": 32768, "timeout": 900},
    }},
    "digest": {"timezone": "local", "top_actions": 10},
    "privacy": {"redact": [
        {"pattern": r"\+?\d[\d\-\s()]{9,}\d", "replace": "[телефон]"},
        {"pattern": r"[\w.+-]+@[\w-]+\.[\w.]+", "replace": "[email]"},
        {"pattern": r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b", "replace": "[карта]"},
    ]},
}


def config_path(ws: str | Path) -> Path:
    return Path(ws) / "config.json"


def init_workspace(ws: str | Path) -> Path:
    ws = Path(ws).expanduser()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "data").mkdir(exist_ok=True)
    (ws / "reports").mkdir(exist_ok=True)
    cp = config_path(ws)
    if not cp.exists():
        write_config(ws, copy.deepcopy(STARTER))
    readme = ws / "ПРОЧТИ.txt"
    if not readme.exists():
        readme.write_text("Рабочая папка OpsDigest.\n\nconfig.json — настройки и ключи, data\\ — база переписки и сессия "
                          "Telegram,\nreports\\ — отчёты. Папку не выкладывайте в общий доступ: в ней доступ к вашему "
                          "Telegram.\n", encoding="utf-8")
    return ws


def read_config(ws: str | Path) -> dict:
    """config.json (зашифрованный, если включена защита: без ключа — ошибка Locked)."""
    from .secure import files
    p = config_path(ws)
    try:
        return json.loads(files.read_text(p, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return copy.deepcopy(STARTER)


def write_config(ws: str | Path, data: dict) -> None:
    from .secure import files
    files.write_text(config_path(ws), json.dumps(data, ensure_ascii=False, indent=2))


def _atomic_write(path: Path, text: str) -> None:
    from .secure import atomic
    atomic.write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------- выбор папки (внутри окна программы)
def places() -> list[dict]:
    """Быстрые ссылки: домашняя папка, рабочий стол, документы, загрузки, диски."""
    home = Path.home()
    cands = [("Домашняя папка", home), ("Рабочий стол", home / "Desktop"), ("Документы", home / "Documents"),
             ("Загрузки", home / "Downloads")]
    od = os.environ.get("OneDrive")
    if od:
        cands += [("Рабочий стол (OneDrive)", Path(od) / "Desktop"), ("Документы (OneDrive)", Path(od) / "Documents")]
    out = [{"name": n, "path": str(p)} for n, p in cands if p.is_dir()]
    if sys.platform == "win32":
        import string
        out += [{"name": f"Диск {c}:", "path": f"{c}:\\"} for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    else:
        out.append({"name": "/", "path": "/"})
    return out


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith((".", "$")) or entry.name in ("System Volume Information", "AppData"):
        return True
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & (0x2 | 0x4))  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def list_dirs(path: str) -> dict:
    """Вложенные папки (без файлов и скрытых). Пустой путь — быстрые ссылки."""
    pl = places()
    if not path:
        return {"path": "", "parent": "", "entries": [], "places": pl, "cloud": False}
    p = Path(os.path.expandvars(path.strip().strip('"'))).expanduser()
    if not p.is_dir():
        raise ValueError(f"папки нет: {p}")
    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and not _hidden(e):
                        entries.append({"name": e.name, "path": e.path})
                except OSError:
                    continue
    except PermissionError:
        raise ValueError("нет доступа к этой папке") from None
    entries.sort(key=lambda x: x["name"].lower())
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "entries": entries[:800], "places": pl, "cloud": in_cloud_folder(p)}


def make_dir(path: str, name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in name for c in '\\/:*?"<>|'):
        raise ValueError('недопустимое имя папки (нельзя \\ / : * ? " < > |)')
    target = Path(path) / name
    target.mkdir(parents=False, exist_ok=True)
    return str(target)


# ---------------------------------------------------------------- ежедневная сводка (Планировщик задач Windows)
def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


_SCHED_CACHE: list = [0.0, None]


def schedule_status() -> dict:
    """Старое расписание через Планировщик Windows (до сессии 13). schtasks — отдельный процесс на каждый вызов,
    а окно спрашивает состояние часто: помним ответ минуту."""
    if sys.platform != "win32":
        return {"supported": False, "enabled": False}
    import time as _time
    if _SCHED_CACHE[1] is not None and _time.time() - _SCHED_CACHE[0] < 60:
        return dict(_SCHED_CACHE[1])
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _SCHED_CACHE[:] = [_time.time(), {"supported": True, "enabled": r.returncode == 0}]
    return dict(_SCHED_CACHE[1])


def schedule_enable(ws: str | Path, at: str = "08:40", weekdays_only: bool = True) -> tuple[bool, str]:
    """Создать задачу Планировщика: сводка за сутки в «Избранное» в указанное время, без окон."""
    _SCHED_CACHE[1] = None
    if sys.platform != "win32":
        return False, "расписание доступно только в Windows"
    ws = Path(ws)
    app_root = Path(__file__).resolve().parent.parent
    script = ws / "data" / "scheduled_digest.pyw"
    script.write_text(
        "# Запускается Планировщиком задач Windows. Создано OpsDigest — можно удалить вместе с задачей.\n"
        "import sys\n"
        f"sys.path.insert(0, {str(app_root)!r})\n"
        f"log = open({str(ws / 'data' / 'scheduled.log')!r}, 'a', encoding='utf-8')\n"
        "sys.stdout = sys.stderr = log\n"
        "import datetime; print('====', datetime.datetime.now(), flush=True)\n"
        "from opsdigest.__main__ import main\n"
        f"main(['--config', {str(config_path(ws))!r}, 'digest', '--period', 'day', '--send'])\n",
        encoding="utf-8")
    args = ["schtasks", "/Create", "/TN", TASK_NAME, "/ST", at, "/F", "/TR", f'"{_pythonw()}" "{script}"']
    args[4:4] = ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI"] if weekdays_only else ["/SC", "DAILY"]
    r = subprocess.run(args, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    msg = (r.stdout or r.stderr).decode("cp866", errors="replace").strip()
    return r.returncode == 0, msg


def schedule_disable() -> tuple[bool, str]:
    _SCHED_CACHE[1] = None
    if sys.platform != "win32":
        return False, "расписание доступно только в Windows"
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0, (r.stdout or r.stderr).decode("cp866", errors="replace").strip()


# ---------------------------------------------------------------- запуск при входе в Windows
AUTOSTART_NAME = "OpsDigest.cmd"


def _startup_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    base = os.environ.get("APPDATA")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if base else None


def autostart_status() -> dict:
    d = _startup_dir()
    return {"supported": d is not None, "enabled": bool(d and (d / AUTOSTART_NAME).is_file())}


def autostart_set(on: bool) -> tuple[bool, str]:
    """Ярлык в «Автозагрузке» пользователя (без прав администратора): OpsDigest.bat при входе в Windows."""
    d = _startup_dir()
    if d is None:
        return False, "автозапуск доступен только в Windows"
    f = d / AUTOSTART_NAME
    try:
        if not on:
            f.unlink(missing_ok=True)
            return True, "выключен"
        bat = Path(__file__).resolve().parent.parent / "OpsDigest.bat"
        d.mkdir(parents=True, exist_ok=True)
        f.write_text("@echo off\r\nchcp 65001 >nul\r\nrem OpsDigest autostart (turn off in OpsDigest: Tasks)\r\n"
                     f'start "OpsDigest" /min cmd /c "{bat}"\r\n', encoding="utf-8")
        return True, "включён"
    except OSError as e:
        return False, f"не удалось изменить автозагрузку: {e.strerror or e}"
