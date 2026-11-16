"""Чтение и запись файлов рабочей папки с шифрованием.

Если защита включена (data/vault.json), всё записывается зашифрованным; при чтении зашифрованный файл
расшифровывается в памяти, старый открытый — читается как есть (для перехода). Имя и место файла не меняются,
поэтому остальной код работает с путями как раньше — меняются только чтение и запись содержимого.
"""
from __future__ import annotations

import io
from pathlib import Path

from . import atomic, crypto, vault


def _head_raw(path: str | Path, n: int) -> bytes:
    def go() -> bytes:
        with open(path, "rb") as f:
            return f.read(n)
    with atomic.lock(path):
        return atomic.retry(go)


def is_sealed(path: str | Path) -> bool:
    try:
        return crypto.is_sealed(_head_raw(path, len(crypto.MAGIC)))
    except OSError:
        return False


def read_bytes(path: str | Path) -> bytes:
    data = atomic.read_bytes(path)
    if crypto.is_sealed(data):
        return crypto.open_(vault.key(), data)
    return data


def read_text(path: str | Path, encoding: str = "utf-8") -> str:
    return read_bytes(path).decode(encoding)


def head(path: str | Path, n: int = 16) -> bytes:
    """Первые байты содержимого (для определения типа файла)."""
    if is_sealed(path):
        return read_bytes(path)[:n]
    return _head_raw(path, n)


def size(path: str | Path) -> int:
    """Размер содержимого (без служебной части шифра)."""
    s = Path(path).stat().st_size
    return s - len(crypto.MAGIC) - crypto.NONCE - crypto.TAG if is_sealed(path) else s


def must_encrypt(path: str | Path) -> bool:
    return vault.protected(path)


def write_bytes(path: str | Path, data: bytes, encrypt: bool | None = None) -> None:
    """Атомарная запись; в защищённой папке — зашифрованной (без ключа — ошибка, а не открытый файл).

    На Windows замена файла, который кто-то держит открытым, повторяется с паузами (secure/atomic.py)."""
    p = Path(path)
    if encrypt is None:
        encrypt = must_encrypt(p)
    if encrypt:
        data = crypto.seal(vault.key(), data)
    atomic.write_bytes(p, data)


def write_text(path: str | Path, text: str, encoding: str = "utf-8", encrypt: bool | None = None) -> None:
    write_bytes(path, text.encode(encoding), encrypt=encrypt)


def open_read(path: str | Path):
    """Файловый объект для чтения содержимого (для zipfile и т.п.)."""
    return io.BytesIO(read_bytes(path))


def encrypt_in_place(path: str | Path) -> bool:
    """Зашифровать открытый файл на месте (при включении защиты). True — был зашифрован сейчас."""
    p = Path(path)
    if not p.is_file() or is_sealed(p):
        return False
    write_bytes(p, atomic.read_bytes(p), encrypt=True)
    return True
