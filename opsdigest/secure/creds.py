"""«Учётные данные»: логины, пароли, токены (PAT, API) для плагинов Maverick и других целей.

Где хранится: data/creds.json рабочей папки — только в защищённой папке (файл зашифрован ключом данных, как вся
папка); в открытой папке хранилище не работает. Секрет (пароль, токен) окно получает только после повторного ввода
пароля OpsDigest: ввод открывает доступ на OPEN_SECONDS, любая блокировка (ПИН, «Выход», бездействие) его закрывает;
5 неверных паролей подряд — полная блокировка программы. Список записей (название, вид, логин, адрес, для каких
плагинов) секретов не содержит. Секреты добавляются в список скрываемых строк журналов (logs.add_secrets).

Программа сама никуда учётные данные не отправляет: пользователь копирует нужное (буфер очищается через полминуты)
и вставляет в чат Maverick, когда тот просит переподключить плагин. Проверка плагинов подсказывает, какая запись нужна.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import files, vault

FILE = "data/creds.json"
OPEN_SECONDS = 120        # сколько секунд после ввода пароля можно смотреть и копировать без повторного ввода
MAX_TRIES = 5
KINDS = {"account": "учётная запись (логин и пароль)", "pat": "Personal Access Token (PAT)", "token": "токен / ключ API",
         "other": "другое"}
PLUGINS = ("mail", "itsm", "wiki", "rooms", "tracker", "meet")
FIELDS = ("title", "kind", "login", "url", "note", "auth")


class NeedPassword(Exception):
    """Чтобы показать секрет, нужно ввести пароль OpsDigest."""


class _Gate:
    def __init__(self):
        self.until = 0.0
        self.epoch = -1
        self.tries = 0
        self.lock = threading.Lock()


G = _Gate()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _path(ws) -> Path:
    return Path(ws) / FILE


def _need_protection(ws) -> None:
    if not vault.protected(Path(ws) / FILE):
        raise ValueError("учётные данные хранятся только в защищённой папке — включите защиту (раздел «Защита»)")


def load(ws) -> dict:
    p = _path(ws)
    if not p.is_file():
        return {"v": 1, "items": [], "plugins": {}}
    data = json.loads(files.read_text(p))
    data.setdefault("items", [])
    data.setdefault("plugins", {})
    _hide(data)
    return data


def _save(ws, data: dict) -> None:
    _need_protection(ws)
    p = _path(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    files.write_text(p, json.dumps(data, ensure_ascii=False, indent=1), encrypt=True)
    _hide(data)


def _hide(data: dict) -> None:
    """Секреты и логины — в список строк, которые журналы и отчёты об ошибках вырезают."""
    from .. import logs
    vals = []
    for it in data.get("items") or []:
        vals += [it.get("secret") or "", it.get("login") or ""]
    logs.add_secrets([v for v in vals if len(v) >= 4])


# ---------------------------------------------------------------- для окна (без секретов)
def view(item: dict) -> dict:
    out = {k: item.get(k) or "" for k in FIELDS}
    out.update({"id": item["id"], "main": bool(item.get("main")), "plugins": list(item.get("plugins") or []),
                "has_secret": bool(item.get("secret")), "secret_len": len(item.get("secret") or ""),
                "created": item.get("created"), "updated": item.get("updated"), "copied_at": item.get("copied_at"),
                "copies": int(item.get("copies") or 0)})
    return out


def listing(ws) -> dict:
    data = load(ws)
    return {"items": [view(x) for x in data["items"]], "plugins": for_plugins(data), "kinds": KINDS,
            "assigned": dict(data.get("plugins") or {}),
            "open": is_open(), "open_left": max(0, int(G.until - time.time())) if is_open() else 0,
            "protected": vault.protected(_path(ws))}


def for_plugins(data: dict) -> dict:
    """Какая запись нужна каждому плагину: назначенная явно, иначе отмеченная у записи, иначе основная."""
    items = {x["id"]: x for x in data.get("items") or []}
    main = next((x for x in items.values() if x.get("main")), None)
    out = {}
    for k in PLUGINS:
        cid = (data.get("plugins") or {}).get(k)
        it = items.get(cid) if cid else None
        it = it or next((x for x in items.values() if k in (x.get("plugins") or [])), None) or main
        out[k] = {"id": it["id"], "title": it.get("title") or "", "login": it.get("login") or "",
                  "kind": it.get("kind") or "account", "url": it.get("url") or "", "auth": it.get("auth") or ""} if it else None
    return out


def save(ws, entry: dict) -> dict:
    """Новая запись или изменение. Пустой secret при изменении — пароль остаётся прежним."""
    _need_protection(ws)
    data = load(ws)
    cid = str(entry.get("id") or "")
    cur = next((x for x in data["items"] if x["id"] == cid), None) if cid else None
    title = str(entry.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("укажите название записи")
    if cur is None:
        cur = {"id": "c" + secrets.token_hex(4), "created": _now()}
        data["items"].append(cur)
        if not str(entry.get("secret") or ""):
            raise ValueError("укажите пароль или токен")
    for k in FIELDS:
        if k in entry:
            cur[k] = str(entry.get(k) or "").strip()[:2000]
    if cur.get("kind") not in KINDS:
        cur["kind"] = "account"
    if str(entry.get("secret") or ""):
        cur["secret"] = str(entry["secret"])[:8000]
    if "plugins" in entry:
        cur["plugins"] = [k for k in entry.get("plugins") or [] if k in PLUGINS]
    if entry.get("main"):
        for x in data["items"]:
            x["main"] = x is cur
    elif "main" in entry:
        cur["main"] = False
    cur["updated"] = _now()
    _save(ws, data)
    return view(cur)


def delete(ws, cid: str) -> None:
    _need_protection(ws)
    data = load(ws)
    data["items"] = [x for x in data["items"] if x["id"] != cid]
    data["plugins"] = {k: v for k, v in (data.get("plugins") or {}).items() if v != cid}
    _save(ws, data)


def assign(ws, plugin: str, cid: str | None) -> None:
    """Плагину — конкретная запись (None — по умолчанию: отмеченная или основная)."""
    if plugin not in PLUGINS:
        raise ValueError("неизвестный плагин")
    _need_protection(ws)
    data = load(ws)
    if cid and not any(x["id"] == cid for x in data["items"]):
        raise ValueError("запись не найдена")
    pl = data.setdefault("plugins", {})
    if cid:
        pl[plugin] = cid
    else:
        pl.pop(plugin, None)
    _save(ws, data)


# ---------------------------------------------------------------- доступ к секретам
def is_open() -> bool:
    return vault.has_key() and G.epoch == vault.S.lock_epoch and time.time() < G.until and not vault.state_pin_locked()


def open_with_password(password: str) -> int:
    """Повторный ввод пароля OpsDigest → доступ к секретам на OPEN_SECONDS. Возвращает, сколько секунд."""
    with G.lock:
        try:
            vault.check_password(password)
        except vault.BadSecret:
            G.tries += 1
            if G.tries >= MAX_TRIES:
                G.tries = 0
                vault.lock_full()
                raise vault.BadSecret(f"пароль неверный {MAX_TRIES} раз — программа заблокирована полностью") from None
            raise vault.BadSecret(f"неверный пароль, осталось попыток: {MAX_TRIES - G.tries}") from None
        G.tries = 0
        G.epoch = vault.S.lock_epoch
        G.until = time.time() + OPEN_SECONDS
        return OPEN_SECONDS


def close() -> None:
    G.until = 0.0


def reveal(ws, cid: str, field: str = "secret", password: str | None = None) -> dict:
    """Секрет (или логин) записи — только после пароля. Отмечает, когда копировали."""
    if password:
        open_with_password(password)
    elif not is_open():
        raise NeedPassword("введите пароль OpsDigest, чтобы показать или скопировать")
    data = load(ws)
    it = next((x for x in data["items"] if x["id"] == cid), None)
    if it is None:
        raise ValueError("запись не найдена")
    if field not in ("secret", "login", "url"):
        raise ValueError("неизвестное поле")
    it["copied_at"] = _now()
    it["copies"] = int(it.get("copies") or 0) + 1
    _save(ws, data)
    return {"id": cid, "field": field, "value": it.get(field) or "", "open_left": max(0, int(G.until - time.time()))}
