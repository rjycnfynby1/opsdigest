"""Зашифрованная база: SQLite работает в памяти, на диске — только зашифрованный снимок (data/opsdigest.db.odx).

  • при первом обращении снимок расшифровывается в память; старая открытая база (opsdigest.db) переносится
    и удаляется вместе с -wal/-shm;
  • все Store процесса делят одно соединение (под замком — выгрузка, сводка и окно работают одновременно);
  • изменения сохраняются снимком: не чаще раза в минуту, а также при блокировке, выходе и окончании задач.
    При аварийном завершении теряется не больше минуты работы (новое догрузится из Telegram).
"""
from __future__ import annotations

import atexit
import sqlite3
import threading
import time
from pathlib import Path

from . import files, vault

SAVE_EVERY = 60.0
SLOW_SECONDS = 3.0   # запрос дольше — в журнал (без параметров): все остальные ждали замка базы столько же
_DBS: dict[str, "SharedDB"] = {}
_GLOBAL = threading.Lock()
_DDL = ("ALTER", "DROP")


def enc_path(path: Path) -> Path:
    return path.with_name(path.name + ".odx")


def exists(path: str | Path) -> bool:
    p = Path(path)
    return p.exists() or enc_path(p).exists()


class _Result:
    """Результат запроса, полностью прочитанный под замком (курсоры не делятся между потоками)."""

    def __init__(self, rows: list, rowcount: int, lastrowid, description):
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def fetchall(self) -> list:
        r = self._rows[self._i:]
        self._i = len(self._rows)
        return r

    def fetchmany(self, n: int = 1) -> list:
        r = self._rows[self._i:self._i + n]
        self._i += len(r)
        return r

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r

    def close(self) -> None:
        pass


class LockedConn:
    """Обёртка соединения: тот же интерфейс, что у sqlite3.Connection, но потокобезопасно и с отметкой изменений."""

    def __init__(self, shared: "SharedDB"):
        self._s = shared

    @property
    def row_factory(self):
        return self._s.conn.row_factory

    @row_factory.setter
    def row_factory(self, v):
        self._s.conn.row_factory = v

    def _run(self, fn, sql: str):
        with self._s.lock:
            t0 = time.monotonic()
            before = self._s.conn.total_changes
            cur = fn()
            rows = cur.fetchall() if cur.description else []
            res = _Result(rows, cur.rowcount, cur.lastrowid, cur.description)
            cur.close()
            took = time.monotonic() - t0
            if took > SLOW_SECONDS:
                _slow(sql, took, len(rows))
            head = sql.lstrip()[:6].upper()
            if self._s.conn.total_changes != before or head.startswith(_DDL) or (
                    head == "CREATE" and "IF NOT EXISTS" not in sql.upper()):
                self._s.dirty = True
            return res

    def execute(self, sql: str, params=()):
        return self._run(lambda: self._s.conn.execute(sql, params), sql)

    def executemany(self, sql: str, seq):
        return self._run(lambda: self._s.conn.executemany(sql, seq), sql)

    def executescript(self, script: str):
        with self._s.lock:
            before = self._s.conn.total_changes
            self._s.conn.executescript(script)
            if self._s.conn.total_changes != before or "ALTER " in script.upper():
                self._s.dirty = True

    def commit(self) -> None:
        with self._s.lock:
            self._s.conn.commit()

    def rollback(self) -> None:
        with self._s.lock:
            self._s.conn.rollback()

    def close(self) -> None:  # общее соединение закрывается только при блокировке/выходе
        pass

    def cursor(self):
        return self


def _slow(sql: str, took: float, rows: int) -> None:
    """Медленный запрос — в журнал: текст SQL (без параметров — данных пользователя там нет) и время."""
    try:
        from .. import logs
        logs.get("db").warning("медленный запрос к базе: %.1f с, строк %d — %s", took, rows,
                               " ".join(sql.split())[:300])
    except Exception:  # noqa: BLE001
        pass


class SharedDB:
    def __init__(self, path: Path):
        self.path = path
        self.enc = enc_path(path)
        self.lock = threading.RLock()
        self.dirty = False
        self.saved_at = time.time()
        self.plain_left: list[Path] = []
        self.conn = self._load()

    def _load(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        if self.enc.exists():
            data = bytearray(files.read_bytes(self.enc))
            if len(data) > 20:
                data[18] = data[19] = 1
            conn.deserialize(bytes(data))
        elif self.path.exists():  # открытая база от прошлых версий — переносим и удаляем
            src = sqlite3.connect(str(self.path), timeout=60)
            try:
                src.backup(conn)
            finally:
                src.close()
            self.dirty = True
            self.plain_left = [self.path, self.path.with_name(self.path.name + "-wal"),
                               self.path.with_name(self.path.name + "-shm")]
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, force: bool = False) -> bool:
        with self.lock:
            if not self.dirty and not force and not self.plain_left:
                return False
            self.conn.commit()
            data = bytearray(self.conn.serialize())
            self.dirty = False
        if len(data) > 20:
            data[18] = data[19] = 1
        files.write_bytes(self.enc, bytes(data), encrypt=True)
        self.saved_at = time.time()
        left = []
        for p in self.plain_left:  # открытая старая база: удалить (если файл ещё занят — при следующем сохранении)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                left.append(p)
        self.plain_left = left
        return True

    def close(self) -> None:
        try:
            self.save()
        finally:
            with self.lock:
                self.conn.close()


def connect(path: str | Path) -> LockedConn:
    p = Path(path).resolve()
    vault.key()  # без ключа — понятная ошибка «нужен пароль»
    with _GLOBAL:
        db = _DBS.get(str(p))
        if db is None:
            db = SharedDB(p)
            _DBS[str(p)] = db
            _ensure_flusher()
    return LockedConn(db)


def flush_all(force: bool = False) -> int:
    n = 0
    for db in list(_DBS.values()):
        try:
            n += int(db.save(force))
        except vault.Locked:
            pass
    return n


def close_all() -> None:
    """Сохранить и выгрузить из памяти все базы (перед тем, как забыть ключ)."""
    with _GLOBAL:
        dbs = list(_DBS.values())
        _DBS.clear()
    for db in dbs:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


_flusher: threading.Thread | None = None


def _ensure_flusher() -> None:
    global _flusher
    if _flusher and _flusher.is_alive():
        return

    def loop():
        while True:
            time.sleep(10)
            for db in list(_DBS.values()):
                if db.dirty and time.time() - db.saved_at >= SAVE_EVERY and vault.has_key():
                    try:
                        db.save()
                    except Exception:  # noqa: BLE001 — попробуем в следующий раз
                        pass
    _flusher = threading.Thread(target=loop, daemon=True, name="db-snapshot")
    _flusher.start()


vault.S.on_full_lock.append(close_all)
atexit.register(lambda: flush_all() if vault.has_key() else None)
