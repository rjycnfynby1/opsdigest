"""Долгие задачи, которые OpsDigest доводит до конца: выгрузка истории, изображения чата, ИИ-описания, глоссарий.

Список хранится в data/tasks.json рабочей папки (в защищённой папке — зашифрован, как всё остальное): вид задачи
и параметры, с которыми её запустили из окна. Задача заканчивается, только когда выполнена или когда её
остановил пользователь (пауза; продолжить можно из окна «Задачи»). Иначе программа запускает её снова:
  — после перезапуска (сбой, сон компьютера, обновление, «Выход» и новый запуск) — как только данные открыты;
  — после ошибки (нет связи, модель не ответила) — с растущей паузой: 1, 2, 5, 10, 20, 30 минут.
Каждая из этих задач умеет продолжать с места остановки (выгрузка — по сохранённым диапазонам, изображения —
по кешу описаний, ИИ-описания — пропускают уже описанное, глоссарий — по отметкам прочитанного).

Состояния: running (идёт сейчас) | queued (ждёт запуска) | retry (ошибка, повтор в next_at) | paused | done.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import logs

log = logs.get("tasks")

# вид → (маршрут окна, которым задача запускается, название)
KINDS = {
    "export": ("/api/export", "Выгрузка истории"),
    "images": ("/api/chat/images", "Изображения чата"),
    "describe": ("/api/describe", "ИИ-описания"),
    "glossary": ("/api/glossary/scan", "Глоссарий: чтение чатов"),
    "glossary_auto": ("/api/glossary/process", "Глоссарий: обработка очереди терминов"),
}
BACKOFF = [60, 120, 300, 600, 1200, 1800]   # пауза перед повтором после ошибки, с
AWAKE_RETRIES = len(BACKOFF)                # столько повторов после ошибок (~1 час) компьютер не засыпает
KEEP_DONE = 20                              # сколько выполненных показывать
KEEP_DONE_DAYS = 7
PRIVATE = ("_task", "_resume")              # служебные поля запроса — в параметры задачи не попадают


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_body(body: dict) -> dict:
    return {k: v for k, v in (body or {}).items() if k not in PRIVATE}


def _sig(kind: str, body: dict) -> str:
    return kind + json.dumps(clean_body(body), sort_keys=True, ensure_ascii=False, default=str)


def backoff(attempt: int) -> int:
    return BACKOFF[min(max(attempt, 1), len(BACKOFF)) - 1]


def title(kind: str, body: dict, chat_title: Callable[[int], str] | None = None) -> str:
    b = clean_body(body)
    base = KINDS.get(kind, ("", kind))[1]
    extra = []
    if kind == "export":
        ids = b.get("chat_ids") or []
        if len(ids) == 1 and chat_title:
            extra.append(f"«{chat_title(int(ids[0]))}»")
        elif ids:
            extra.append(f"чатов: {len(ids)}")
        else:
            extra.append("все чаты")
        if b.get("since"):
            extra.append(f"с {str(b['since'])[:10]}")
    elif kind == "images":
        if b.get("chat_id") not in (None, "") and chat_title:
            try:
                extra.append(f"«{chat_title(int(b['chat_id']))}»")
            except (TypeError, ValueError):
                pass
        if b.get("since"):
            extra.append(f"с {str(b['since'])[:10]}")
    elif kind == "describe":
        t = b.get("targets") or {}
        parts = [f"{n} {w}" for n, w in ((len(t.get("chats") or []), "чатов"), (len(t.get("topics") or []), "тем"),
                                        (len(t.get("people") or []), "людей"), (len(t.get("roles") or []), "ролей")) if n]
        extra += parts
        if b.get("overwrite"):
            extra.append("заново")
    elif kind == "glossary":
        ids = b.get("chats") or []
        extra.append(f"чатов: {len(ids)}" if ids else "чаты с новым")
    return base + (" — " + ", ".join(x for x in extra if x) if extra else "")


class TaskBook:
    """Список задач рабочей папки. read/write — функции чтения и записи файла (в защищённой папке —
    с шифрованием); пока данные закрыты, список не читается, а запись откладывается до следующей удачной."""

    def __init__(self, path: Callable[[], Path | None], read: Callable[[Path], str], write: Callable[[Path, str], None],
                 can_read: Callable[[], bool] = lambda: True):
        self._path, self._read, self._write, self._can = path, read, write, can_read
        self.items: dict[str, dict] = {}
        self.loaded_for: Path | None = None
        self.dirty = False
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- файл
    def load(self, force: bool = False) -> bool:
        """Прочитать список, если ещё не прочитан для текущей папки. False — нельзя (данные закрыты, нет папки)."""
        p = self._path()
        with self.lock:
            if p is None:
                self.items, self.loaded_for = {}, None
                return False
            if self.loaded_for == p and not force:
                return True
            if not self._can():
                return False
            items: dict[str, dict] = {}
            if p.is_file():
                try:
                    data = json.loads(self._read(p))
                    for r in data.get("tasks") or []:
                        if isinstance(r, dict) and r.get("id") and r.get("kind") in KINDS:
                            if r.get("status") == "running":  # этот процесс её не запускал: прервана — продолжить
                                r["status"] = "queued"
                                r["job"] = None
                            items[r["id"]] = r
                except Exception as e:  # noqa: BLE001 — испорченный список не должен ронять программу
                    log.warning("список задач не прочитан: %s", logs.err_info(e))
            self.items, self.loaded_for, self.dirty = items, p, False
            return True

    def save(self) -> bool:
        with self.lock:
            p = self.loaded_for
            if p is None:
                return False
            self._prune()
            data = {"v": 1, "tasks": sorted(self.items.values(), key=lambda r: r.get("created") or "")}
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                self._write(p, json.dumps(data, ensure_ascii=False, indent=1))
                self.dirty = False
                return True
            except Exception as e:  # noqa: BLE001 — нет ключа, файл занят: запишем при следующем изменении
                self.dirty = True
                log.info("список задач пока не записан: %s", logs.err_info(e))
                return False

    def _prune(self) -> None:
        done = sorted((r for r in self.items.values() if r.get("status") == "done"),
                      key=lambda r: r.get("done_at") or "", reverse=True)
        cutoff = datetime.now(timezone.utc).timestamp() - KEEP_DONE_DAYS * 86400
        for i, r in enumerate(done):
            try:
                old = datetime.fromisoformat(r.get("done_at") or "").timestamp() < cutoff
            except ValueError:
                old = True
            if i >= KEEP_DONE or old:
                self.items.pop(r["id"], None)

    # ---------------------------------------------------------------- жизненный цикл
    def begin(self, kind: str, body: dict, job_id: str, name: str = "") -> dict:
        """Задача запущена (из окна или повтором). body["_task"] — продолжение существующей."""
        with self.lock:
            self.load()
            rec = self.items.get(str((body or {}).get("_task") or ""))
            if rec is None:
                sig = _sig(kind, body)
                for old in list(self.items.values()):  # та же задача уже стоит на паузе/в очереди — заменяем
                    if old.get("status") != "running" and _sig(old["kind"], old.get("body") or {}) == sig:
                        self.items.pop(old["id"], None)
                rec = {"id": secrets.token_hex(5), "kind": kind, "body": clean_body(body), "title": name or title(kind, body),
                       "created": now_iso(), "first": now_iso(), "attempts": 0}
                self.items[rec["id"]] = rec
            rec.update(status="running", job=job_id, started=now_iso(), next_at=0, attempts=int(rec.get("attempts") or 0) + 1)
            if name:
                rec["title"] = name
            self.save()
            return dict(rec)

    def finish(self, task_id: str, *, ok: bool, cancelled: str | None = None, error: str | None = None,
               last: str = "", result: Any = None) -> dict | None:
        """Задача закончилась в этом процессе. cancelled: "user" — пауза, "system" — прервали (выход,
        блокировка) — продолжить при первой возможности."""
        with self.lock:
            rec = self.items.get(task_id)
            if rec is None:
                return None
            rec["job"] = None
            rec["last"] = (last or "")[:300]
            if cancelled == "user":
                rec.update(status="paused", paused_at=now_iso())
            elif cancelled:
                rec.update(status="queued", next_at=0)
            elif ok:
                rec.update(status="done", done_at=now_iso(), error=None)
                if isinstance(result, dict):
                    rec["result"] = {k: result[k] for k in ("done", "failed", "skipped", "total", "chats", "messages",
                                                           "described", "new", "updated", "questions", "message")
                                     if k in result and not isinstance(result[k], (list, dict))}
            else:
                wait = backoff(int(rec.get("attempts") or 1))
                rec.update(status="retry", error=(error or "ошибка")[:400], next_at=time.time() + wait)
            self.save()
            return dict(rec)

    def later(self, task_id: str, error: str, wait: float | None = None) -> None:
        """Запустить не вышло (Telegram занят, нет связи) — попробовать позже."""
        with self.lock:
            rec = self.items.get(task_id)
            if rec is None or rec.get("status") == "running":
                return
            rec["attempts"] = int(rec.get("attempts") or 0) + 1
            rec.update(status="retry", error=(error or "")[:400],
                       next_at=time.time() + (wait if wait is not None else backoff(rec["attempts"])))
            self.save()

    def interrupt_running(self) -> None:
        """Программа закрывается или блокируется полностью: идущие задачи продолжатся при следующей возможности."""
        with self.lock:
            changed = False
            for r in self.items.values():
                if r.get("status") == "running":
                    r.update(status="queued", job=None, next_at=0)
                    changed = True
            if changed or self.dirty:
                self.save()

    def pause(self, task_id: str) -> dict | None:
        with self.lock:
            rec = self.items.get(task_id)
            if rec and rec.get("status") in ("queued", "retry"):
                rec.update(status="paused", paused_at=now_iso(), next_at=0)
                self.save()
            return dict(rec) if rec else None

    def resume(self, task_id: str) -> dict | None:
        with self.lock:
            rec = self.items.get(task_id)
            if rec and rec.get("status") in ("paused", "retry"):
                rec.update(status="queued", next_at=0)
                self.save()
            return dict(rec) if rec else None

    def remove(self, task_id: str) -> bool:
        with self.lock:
            rec = self.items.get(task_id)
            if not rec or rec.get("status") == "running":
                return False
            self.items.pop(task_id, None)
            self.save()
            return True

    def woke(self) -> None:
        """Компьютер проснулся: ошибки, скорее всего, из-за сна — повторить через несколько секунд."""
        with self.lock:
            soon = time.time() + 15
            for r in self.items.values():
                if r.get("status") == "retry" and float(r.get("next_at") or 0) > soon:
                    r["next_at"] = soon

    # ---------------------------------------------------------------- выборки
    def due(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self.lock:
            out = [dict(r) for r in self.items.values()
                   if r.get("status") == "queued" or (r.get("status") == "retry" and float(r.get("next_at") or 0) <= now)]
        return sorted(out, key=lambda r: r.get("created") or "")

    def active(self) -> bool:
        """Есть задачи, которые надо довести до конца (идут, ждут запуска или повтора)."""
        with self.lock:
            return any(r.get("status") in ("running", "queued", "retry") for r in self.items.values())

    def wants_awake(self) -> bool:
        """Держать компьютер без сна: задача идёт или вот-вот запустится; после ошибки — только первый час
        повторов (AWAKE_RETRIES), иначе, например, выгрузка без связи с Telegram не давала бы ему спать никогда."""
        with self.lock:
            return any(r.get("status") in ("running", "queued")
                       or (r.get("status") == "retry" and int(r.get("attempts") or 0) <= AWAKE_RETRIES)
                       for r in self.items.values())

    def count_active(self) -> int:
        with self.lock:
            return sum(1 for r in self.items.values() if r.get("status") in ("running", "queued", "retry"))

    def get(self, task_id: str) -> dict | None:
        with self.lock:
            r = self.items.get(task_id)
            return dict(r) if r else None

    def view(self) -> list[dict]:
        order = {"running": 0, "retry": 1, "queued": 2, "paused": 3, "done": 4}
        with self.lock:
            rows = [{k: r.get(k) for k in ("id", "kind", "title", "status", "attempts", "error", "last", "job",
                                           "created", "started", "done_at", "paused_at", "result")}
                    | {"retry_in": max(0, round(float(r.get("next_at") or 0) - time.time())) if r.get("status") == "retry" else None}
                    for r in self.items.values()]
        return sorted(rows, key=lambda r: (order.get(r["status"] or "", 9), r.get("created") or ""))
