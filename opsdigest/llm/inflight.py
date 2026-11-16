"""Идущие запросы к моделям (Maverick и другие): чья это задача, как их оборвать, какие сессии Maverick не закрыты.

Зачем: запрос к модели может висеть долго — после сна компьютера соединение мёртвое, а чтение ждёт до таймаута
(600 с) и ещё два повтора; задача окна всё это время «идёт», и сводка отвечала «дождитесь окончания текущей задачи».
Здесь:
  * каждое HTTP-соединение клиентов LLM регистрируется (через свой urllib-обработчик) вместе с задачей окна, которая
    его открыла (bind — поток задачи; carry — потоки её пула);
  * cancel_jobs(ids) — пометить задачи остановленными и оборвать их соединения: ожидание чтения сразу кончается
    ошибкой Cancelled (повторов не будет);
  * abort_stale() — после сна оборвать все соединения: они мёртвые, запрос сразу повторится (ConnectionError);
  * TCP keepalive на каждом соединении — мёртвую связь видно через ~2 минуты, а не через 10;
  * sessions — сессии Maverick, оставшиеся открытыми (ждут ответа человека — hitl, или ещё обрабатываются):
    их закрывает MaverickClient.release_session (перед сводкой — все).
"""
from __future__ import annotations

import http.client
import socket
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Iterable

_local = threading.local()
_lock = threading.Lock()
_conns: dict[int, tuple[Any, str | None]] = {}     # id(соединения) → (соединение, задача)
_cancelled: dict[str, str] = {}                     # задача → причина остановки
SESSIONS: dict[str, dict] = {}                      # session_id Maverick → {why, at, hitl, host, owner}
KEEPALIVE = (60, 10)                                # TCP keepalive: после 60 с тишины, проба каждые 10 с


class Cancelled(RuntimeError):
    """Запрос оборван: задачу остановили (например, перед сводкой)."""


# ---------------------------------------------------------------- чья задача
def bind(job_id: str | None) -> None:
    """Поток задачи окна: все запросы к моделям из него принадлежат этой задаче."""
    _local.job = job_id


def current() -> str | None:
    return getattr(_local, "job", None)


def carry(fn: Callable) -> Callable:
    """Обернуть функцию для пула потоков: в рабочем потоке — та же задача, что у вызывающего."""
    job = current()

    def run(*a, **kw):
        old = current()
        bind(job)
        try:
            return fn(*a, **kw)
        finally:
            bind(old)
    return run


def is_cancelled(job_id: str | None = None) -> str | None:
    j = job_id if job_id is not None else current()
    return _cancelled.get(j) if j else None


def check() -> None:
    """Перед новым запросом: задачу остановили — дальше не идём."""
    why = is_cancelled()
    if why:
        raise Cancelled(why)


def sleep(seconds: float) -> None:
    """Пауза между повторами, которую прерывает остановка задачи."""
    end = time.time() + max(0.0, seconds)
    while True:
        check()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.5, left))


def forget(job_id: str | None) -> None:
    """Задача закончилась — её отметка об остановке больше не нужна."""
    if job_id:
        with _lock:
            _cancelled.pop(job_id, None)


# ---------------------------------------------------------------- соединения
def _keepalive(sock) -> None:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle, intvl = KEEPALIVE
        if sys.platform == "win32" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, intvl * 1000))
        else:
            for name, val in (("TCP_KEEPIDLE", idle), ("TCP_KEEPINTVL", intvl), ("TCP_KEEPCNT", 6)):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), val)
    except (OSError, ValueError, AttributeError):
        pass


class _Tracked:
    _od_aborted: str | None = None
    _od_retry = False

    def connect(self):
        with _lock:
            _conns[id(self)] = (self, current())
        _local.conn = self
        super().connect()  # type: ignore[misc]
        if self._od_aborted:  # оборвали, пока соединялись
            self._kill()
            raise ConnectionAbortedError(self._od_aborted)
        _keepalive(self.sock)  # type: ignore[attr-defined]

    def close(self):
        with _lock:
            _conns.pop(id(self), None)
        super().close()  # type: ignore[misc]

    def _kill(self) -> None:
        s = getattr(self, "sock", None)
        if s is None:
            return
        try:
            s.shutdown(socket.SHUT_RDWR)   # будит поток, который ждёт ответа
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass


class _HTTPConn(_Tracked, http.client.HTTPConnection):
    pass


class _HTTPSConn(_Tracked, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConn, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConn, req, context=self._context)


def urlopen(req, timeout: float | None = None, context=None):
    """Как urllib.request.urlopen (прокси из настроек Windows/переменных — тоже), но соединение можно оборвать."""
    check()
    _local.conn = None
    opener = urllib.request.build_opener(_HTTPHandler(), _HTTPSHandler(context=context))
    return opener.open(req, timeout=timeout)


def aborted() -> tuple[str | None, bool]:
    """Соединение этого потока оборвали снаружи? → (причина, повторять ли запрос)."""
    c = getattr(_local, "conn", None)
    if c is None or not c._od_aborted:
        why = is_cancelled()
        return (why, False) if why else (None, False)
    return c._od_aborted, c._od_retry


def _abort(pick: Callable[[str | None], bool], reason: str, retry: bool) -> int:
    with _lock:
        victims = [c for c, j in _conns.values() if pick(j)]
        for c in victims:   # оборванное уже не считается идущим (и не будет оборвано второй раз)
            _conns.pop(id(c), None)
    for c in victims:
        c._od_aborted, c._od_retry = reason, retry
        c._kill()
    return len(victims)


def cancel_jobs(job_ids: Iterable[str], reason: str) -> int:
    """Остановить задачи: новые запросы — Cancelled, идущие — оборвать. Возвращает, сколько соединений оборвано."""
    ids = {j for j in job_ids if j}
    with _lock:
        for j in ids:
            _cancelled[j] = reason
    return _abort(lambda j: j in ids, reason, False)


def abort_all(reason: str, keep: Iterable[str] = ()) -> int:
    """Оборвать все идущие запросы, кроме задач keep (и без повторов)."""
    k = set(keep)
    return _abort(lambda j: j not in k, reason, False)


def abort_stale(reason: str = "компьютер спал — соединение устарело") -> int:
    """После сна: соединения мёртвые — оборвать, запросы сразу повторятся."""
    return _abort(lambda j: True, reason, True)


def open_count(job_id: str | None = None) -> int:
    with _lock:
        return sum(1 for _c, j in _conns.values() if job_id is None or j == job_id)


# ---------------------------------------------------------------- сессии Maverick
def note_session(sid: str | None, why: str, *, host: str = "", hitl: dict | None = None, owner: str = "service") -> None:
    """Сессия Maverick осталась открытой (ждёт человека или ещё работает). owner: service — служебный запрос
    программы (можно удалить); portal — беседа ассистента «как на портале» (только остановить)."""
    if not sid:
        return
    with _lock:
        SESSIONS[str(sid)] = {"why": why, "at": time.time(), "hitl": hitl or {}, "host": host, "owner": owner,
                              "job": current()}


def drop_session(sid: str | None) -> None:
    if sid:
        with _lock:
            SESSIONS.pop(str(sid), None)


def open_sessions() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in SESSIONS.items()}
