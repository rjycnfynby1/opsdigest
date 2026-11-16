"""Отчёт об ошибке для пересылки разработчику: самопроверка + обезличенные журналы в одном текстовом файле.

Запуск без окна (если программа не стартует): python -m opsdigest.report — файл появится в «Загрузках».
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, logs

HEADER = """OpsDigest — отчёт об ошибке
Версия {ver} · создан {when}

В отчёте нет переписки, названий чатов, имён, телефонов, адресов серверов, ссылок и ключей:
они заменены метками вроде <host>, <path>, <secret>, <скрыто>. Просмотрите файл перед отправкой.
"""


def _ws_and_raw() -> tuple[Path | None, dict | None]:
    from .workspace import load_app_settings, read_config
    ws = load_app_settings().get("workspace")
    if ws and Path(ws).exists():
        raw = read_config(ws)
        logs.add_secrets_from_config(raw)
        logs.add_secrets([ws])
        return Path(ws), raw
    return None, None


def build(checks: list[dict] | None = None, ws: Path | None = None, raw: dict | None = None) -> str:
    from .diag import run_checks, summary
    if ws is None and raw is None:
        ws, raw = _ws_and_raw()
    if checks is None:
        checks = run_checks(ws, raw)
    d = logs.log_dir()
    parts = [HEADER.format(ver=__version__, when=datetime.now().strftime("%d.%m.%Y %H:%M")),
             "== Проверка компьютера ==", summary(checks), ""]
    if raw:
        llm = raw.get("llm") or {}
        tg = raw.get("telegram") or {}
        from .sources.tg_proxy import ProxyError, parse_proxy
        modes = []
        for p in tg.get("proxies") or []:
            try:
                modes.append(parse_proxy(p).mode)
            except ProxyError:
                modes.append("не распознан")
        parts += ["== Настройки (без значений) ==",
                  f"модель: {llm.get('provider')} ({((llm.get('profiles') or {}).get(llm.get('provider')) or {}).get('type')})",
                  f"ключи Telegram заданы: {bool(tg.get('api_id') and tg.get('api_hash'))}; прокси: {', '.join(modes) or 'нет'}",
                  f"правил для чатов: {len(tg.get('chats') or [])}; компетенций: {len((raw.get('profile') or {}).get('competences') or [])}",
                  f"изображения: модель {((raw.get('media') or {}).get('vision_provider') or 'как для сводок')}, формат Maverick "
                  f"найден: {bool(((llm.get('profiles') or {}).get('maverick') or {}).get('image_mode'))}",
                  ""]
        try:  # только числа: сколько сообщений и вложений в базе
            from .secure import vault
            if ws is not None and (ws / "data" / "opsdigest.db").exists() and not vault.protected(ws):
                from .storage import Store
                st = Store(ws / "data" / "opsdigest.db")
                n = st.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                ms = st.media_stats()
                st.close()
                parts[-1:-1] = [f"база: сообщений {n}; изображений {ms['images']} (скачано {ms['images_ok']}, "
                                f"описано {ms['described']}); файлов-ссылок {ms['refs']}"]
        except Exception:  # noqa: BLE001 — отчёт должен собраться в любом случае
            pass
    for name, n in (("opsdigest.log", 300), ("supervisor.log", 80), ("launcher.log", 60), ("pip.log", 60),
                    ("voice-install.log", 80)):
        lines = logs.tail(d / name, n)
        if lines:
            parts += [f"== {name} (последние {len(lines)} строк) ==", *lines, ""]
    return logs.scrub("\n".join(parts))


def save(text: str) -> Path:
    home = Path.home()
    folder = next((p for p in (home / "Downloads", home / "Desktop", home) if p.exists()), home)
    path = folder / f"OpsDigest_отчёт_{datetime.now():%Y-%m-%d_%H%M}.txt"
    path.write_text(text, encoding="utf-8")
    (logs.log_dir() / "last_report.txt").write_text(text, encoding="utf-8")
    return path


def reveal(path: Path) -> None:
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError:
        pass


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print("Собираю отчёт (самопроверка займёт до минуты)…", flush=True)
    path = save(build())
    print(f"Готово: {path}\nЕго можно переслать — в нём нет переписки и личных данных.", flush=True)
    if "--open" in sys.argv:
        reveal(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
