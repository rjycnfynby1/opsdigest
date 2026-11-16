"""Аватарки чатов и людей (фото профиля Telegram) — для мессенджера и отчётов.

Хранятся в media/avatars/ рабочей папки (в защищённой — зашифрованы, как все вложения), имя файла безликое
(a + 12 знаков хеша номера). Таблица avatars: чей, какое фото (photo_id), где лежит, состояние (ok | none).
Номер фото текущего чата хранится в chats.photo: изменилось — аватарка скачивается заново.

Откуда берутся: мессенджер (Live) — при открытии списка чатов окно просит нужные (видимые) аватарки по одной,
сводка с синхронизацией — для чатов периода. Без связи с Telegram показываются уже скачанные.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .storage import Store, iso, parse_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS avatars (
    peer_id     INTEGER PRIMARY KEY,
    photo_id    TEXT,
    path        TEXT,
    status      TEXT,       -- ok | none (фото нет или не удалось получить)
    fetched_at  TEXT
);
"""
RETRY_NONE = timedelta(days=1)   # «фото нет» — проверить снова не раньше чем через сутки


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()


def photo_id(entity: Any) -> str:
    """Номер текущего фото профиля сущности Telethon; '' — фото нет."""
    p = getattr(entity, "photo", None)
    if p is None or type(p).__name__.endswith("Empty"):
        return ""
    pid = getattr(p, "photo_id", None)
    return str(pid) if pid else ""


def rel_path(cfg, peer_id: int) -> str:
    from .media import media_root
    root = media_root(cfg)
    name = "a" + hashlib.sha256(f"opsdigest-avatar:{int(peer_id)}".encode()).hexdigest()[:12] + ".jpg"
    return str((root / "avatars" / name).relative_to(cfg.path("."))).replace("\\", "/")


def row(store: Store, peer_id: int) -> dict | None:
    ensure(store)
    r = store.db.execute("SELECT * FROM avatars WHERE peer_id=?", (int(peer_id),)).fetchone()
    return dict(r) if r else None


def path_of(cfg, store: Store, peer_id: int) -> Path | None:
    """Файл скачанной аватарки (или None)."""
    r = row(store, peer_id)
    if not r or r["status"] != "ok" or not r["path"]:
        return None
    p = cfg.path(r["path"])
    return p if p.is_file() else None


def current_photo(store: Store, peer_id: int) -> str | None:
    """Номер фото чата по последнему списку чатов (None — не знаем)."""
    try:
        r = store.db.execute("SELECT photo FROM chats WHERE chat_id=?", (int(peer_id),)).fetchone()
    except Exception:  # noqa: BLE001 — старая база без колонки
        return None
    return r["photo"] if r and r["photo"] is not None else None


def needs_fetch(store: Store, peer_id: int, pid: str | None = None) -> bool:
    r = row(store, peer_id)
    pid = current_photo(store, peer_id) if pid is None else pid
    if r is None:
        return pid != ""
    if r["status"] == "none":
        if pid:  # фото появилось
            return True
        try:
            return datetime.now(timezone.utc) - parse_iso(r["fetched_at"]) > RETRY_NONE and pid is None
        except (TypeError, ValueError):
            return True
    if pid is not None and pid != (r["photo_id"] or ""):
        return True
    return False


def _save(store: Store, peer_id: int, pid: str, path: str | None, status: str) -> None:
    ensure(store)
    store.db.execute("""INSERT INTO avatars(peer_id, photo_id, path, status, fetched_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(peer_id) DO UPDATE SET photo_id=excluded.photo_id, path=excluded.path,
                        status=excluded.status, fetched_at=excluded.fetched_at""",
                     (int(peer_id), pid, path, status, iso(datetime.now(timezone.utc))))
    store.db.commit()


async def fetch(client, store: Store, cfg, entity: Any, peer_id: int | None = None, *, force: bool = False) -> Path | None:
    """Скачать маленькую аватарку сущности (если её нет или фото сменилось). Возвращает файл или None."""
    from .secure import files
    pid = photo_id(entity)
    if peer_id is None:
        from telethon import utils
        peer_id = utils.get_peer_id(entity)
    if not pid:  # фото нет (или скрыто) — запомнить, чтобы не спрашивать снова
        r = row(store, peer_id)
        if r is None or r["status"] != "none" or r["photo_id"]:
            _save(store, peer_id, "", None, "none")
        return None
    if not force and not needs_fetch(store, peer_id, pid):
        return path_of(cfg, store, peer_id)
    try:
        data = await client.download_profile_photo(entity, file=bytes, download_big=False)
    except Exception as e:  # noqa: BLE001 — обрыв связи, лимит Telegram: не запоминаем, попробуем в другой раз
        if type(e).__name__ in ("FloodWaitError", "ConnectionError", "TimeoutError", "OSError"):
            return path_of(cfg, store, peer_id)
        data = None  # нет прав, фото скрыто настройками приватности
    if not data:
        _save(store, peer_id, pid, None, "none")
        return None
    rel = rel_path(cfg, peer_id)
    p = cfg.path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    files.write_bytes(p, data)
    _save(store, peer_id, pid, rel, "ok")
    return p


def report_src(cfg, store: Store, peer_id: int, out_dir: Path) -> str | None:
    """Относительная ссылка на аватарку из папки отчётов (как у миниатюр изображений) или None."""
    import os
    import urllib.parse
    p = path_of(cfg, store, peer_id)
    if p is None:
        return None
    try:
        rel = os.path.relpath(p, out_dir).replace("\\", "/")
        return "/".join(urllib.parse.quote(x) for x in rel.split("/"))
    except ValueError:  # другой диск Windows
        return p.as_uri()
