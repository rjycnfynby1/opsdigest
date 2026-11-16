"""Сессия Telegram в зашифрованном файле (data/telegram.session).

Файл сессии — это ключ к аккаунту: кто его скопирует, тот входит в Telegram без кода. Поэтому при включённой
защите сессия живёт в памяти, а на диск пишется только зашифрованной. Старый открытый файл переносится сам.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import files, vault


def session_for(path: str | Path):
    """То, что передать в TelegramClient: путь (без защиты) или зашифрованная сессия."""
    p = Path(str(path))
    full = p if p.suffix == ".session" else p.with_name(p.name + ".session")
    if not vault.protected(full):
        return str(path)
    return make_encrypted(full)


def make_encrypted(full: Path):
    from telethon.sessions import SQLiteSession

    class EncryptedSession(SQLiteSession):
        def __init__(self, enc_path: Path):
            self._enc_path = enc_path
            self._preloaded = None
            if enc_path.exists():
                data = files.read_bytes(enc_path)  # открытая (старая) — как есть, зашифрованная — расшифровать
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.deserialize(data)
                self._preloaded = conn
            super().__init__(None)
            if self._preloaded is not None and not files.is_sealed(enc_path):
                self.save()  # перенос старой открытой сессии

        def _cursor(self):
            if self._conn is None:
                self._conn = self._preloaded or sqlite3.connect(":memory:", check_same_thread=False)
            return self._conn.cursor()

        def save(self):
            if self._conn is None:
                return
            self._conn.commit()
            try:
                files.write_bytes(self._enc_path, self._conn.serialize(), encrypt=True)
            except vault.Locked:
                pass  # программа заблокирована полностью — сохраним при следующем входе

        def close(self):
            self.save()

        def delete(self):
            try:
                self._enc_path.unlink()
                return True
            except OSError:
                return False

    return EncryptedSession(full)


def exists(ws: str | Path) -> bool:
    return (Path(ws) / "data" / "telegram.session").exists()
