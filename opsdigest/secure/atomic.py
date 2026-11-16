"""Надёжные чтение и замена файлов на Windows.

В Windows файл нельзя заменить (os.replace), пока его кто-то держит открытым: другой поток программы читает
config.json, антивирус или OneDrive проверяют только что записанный файл. Тогда os.replace падает с
PermissionError (WinError 5 «Отказано в доступе» или 32 «файл занят»), хотя через долю секунды всё бы прошло.

Поэтому:
- внутри программы чтение и запись одного и того же файла идут по очереди (замок на путь, под ним только работа
  с диском — без чужого кода, взаимных блокировок не бывает);
- от чужих процессов спасают повторы с паузами (всего ~10 с), и только потом — ошибка.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

# паузы между попытками, всего ~10 с: антивирус обычно отпускает файл за доли секунды
DELAYS = (0.02, 0.05, 0.1, 0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.0)
BUSY_WINERRORS = {5, 32, 33}   # отказано в доступе, файл занят другим процессом, часть файла заблокирована

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
sleep = time.sleep   # тесты подменяют, чтобы не ждать


def lock(path: str | Path) -> threading.RLock:
    """Замок на файл: внутри процесса чтение и запись одного пути — по очереди."""
    k = os.path.normcase(os.path.abspath(str(path)))
    with _guard:
        lk = _locks.get(k)
        if lk is None:
            lk = _locks[k] = threading.RLock()
        return lk


def busy(e: BaseException) -> bool:
    """Ошибка «файл занят» (на Windows проходит сама), а не настоящий запрет доступа."""
    if isinstance(e, PermissionError):
        return True
    return isinstance(e, OSError) and getattr(e, "winerror", None) in BUSY_WINERRORS


def retry(fn: Callable[[], T]) -> T:
    """fn() с повторами, пока файл занят."""
    for d in DELAYS:
        try:
            return fn()
        except OSError as e:
            if not busy(e):
                raise
            sleep(d)
    return fn()


def replace(tmp: str | Path, dst: str | Path) -> None:
    retry(lambda: os.replace(tmp, dst))


def read_bytes(path: str | Path) -> bytes:
    p = Path(path)
    with lock(p):
        return retry(p.read_bytes)


def write_bytes(path: str | Path, data: bytes) -> None:
    """Атомарная запись: во временный файл рядом, затем замена. При неудаче временный файл убирается."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with lock(p):
        try:
            retry(lambda: tmp.write_bytes(data))
            replace(tmp, p)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
