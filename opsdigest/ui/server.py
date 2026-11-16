"""Локальный сервер окна OpsDigest: страница мастера настройки + JSON API.

Слушает только 127.0.0.1. Каждый запрос к API и отчётам должен нести токен (заголовок X-Token или ?t=),
который знает только открытое окно, — так чужие сайты в браузере не могут обратиться к серверу.
Telegram (Telethon, asyncio) работает в отдельном потоке с циклом событий; долгие операции — фоновые задания,
окно опрашивает их ход.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import __version__, logs, power
from .. import tasks as tasksmod
from .. import workspace as wsmod
from ..config import load_config
from ..llm import inflight
from ..messenger import Live, LiveError
from ..messenger import views as mviews
from ..secure import files as sfiles
from ..secure import memdb, vault

UI_DIR = Path(__file__).resolve().parent
log = logs.get("ui")
REPORT_RE = re.compile(r"^[\w\-.]+\.(html|md|json)$")
BACKGROUND = {"export", "images", "fetch"}   # долгие задачи: не мешают сводке по базе, держат Telegram
LONG = BACKGROUND | {"describe", "glossary", "glossary_auto"}  # долгие задачи, которые не мешают сводке (ИИ-описания Telegram не держат)
# что можно, пока данные закрыты: «full»/«pin» — только вход; «telegram» — ещё настройка и вход в Telegram
OPEN_ALWAYS = {"/api/ping", "/api/state", "/api/shutdown", "/api/diag", "/api/report", "/api/fs/list", "/api/fs/mkdir",
               "/api/workspace/set", "/api/ui"}
OPEN_TELEGRAM = {"/api/config", "/api/config/telegram", "/api/telegram/proxies", "/api/telegram/send_code",
                 "/api/telegram/sign_in", "/api/vpn", "/api/vpn/save", "/api/vpn/install", "/api/vpn/test",
                 "/api/vpn/stop", "/api/update/status", "/api/update/check", "/api/update/install",
                 "/api/update/restart", "/api/update/zips", "/api/update/token"}
PARALLEL = {"asst", "plugins"}   # ассистент и проверка плагинов не мешают сводке
# перед сводкой останавливается всё, кроме этого: установка, шифрование, загрузка экспорта — их нельзя прерывать,
# и Maverick они не занимают
NO_PREEMPT = {"secure", "update", "import", "voice", "vpn-install"}
JOB_TITLES = {"ai": "ИИ в чате", "asst": "ассистент", "plugins": "запрос к плагинам Maverick", "digest": "прошлая сводка",
              "demo": "пробная сводка", "ask": "вопрос", "feedback": "разбор отзыва", "gl": "статья глоссария",
              "vision": "проверка картинок", "describe": "ИИ-описания", "glossary": "глоссарий: чтение чатов",
              "glossary_auto": "глоссарий: очередь терминов", "export": "выгрузка истории", "images": "изображения чата",
              "fetch": "докачка файла", "chats": "список чатов", "proxies": "проверка прокси", "vpn-test": "проверка VPN",
              "diag": "диагностика", "report": "отчёт об ошибке"}
UI_MODULES = ("mx", "digest", "rpa", "asst", "gl")   # разделы окна, которые включаются рубильником в настройках
UI_DEFAULTS = {"mx": False, "digest": True, "rpa": True, "asst": True, "gl": True}


def ui_settings() -> dict:
    u = wsmod.load_app_settings().get("ui") or {}
    mods = dict(UI_DEFAULTS)
    mods.update({k: bool(v) for k, v in (u.get("modules") or {}).items() if k in UI_MODULES})
    return {"modules": mods, "theme": u.get("theme") if u.get("theme") in ("auto", "light", "dark") else "auto",
            "tab": u.get("tab") or ""}


CTYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
          "bmp": "image/bmp", "md": "text/markdown; charset=utf-8", "txt": "text/plain; charset=utf-8",
          "log": "text/plain; charset=utf-8", "pdf": "application/pdf", "ogg": "audio/ogg", "oga": "audio/ogg",
          "opus": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "mp4": "video/mp4",
          "webm": "video/webm", "mov": "video/quicktime", "mkv": "video/x-matroska"}
STATIC = {"design-system.js": "application/javascript; charset=utf-8", "design-system.css": "text/css; charset=utf-8", "rpa.js": "application/javascript; charset=utf-8", "messenger.js": "application/javascript; charset=utf-8", "messenger.css": "text/css; charset=utf-8",
          "chats.js": "application/javascript; charset=utf-8", "glossary.js": "application/javascript; charset=utf-8",
          "tasks.js": "application/javascript; charset=utf-8", "modules.js": "application/javascript; charset=utf-8",
          "modules.css": "text/css; charset=utf-8", "assistant.js": "application/javascript; charset=utf-8",
          "creds.js": "application/javascript; charset=utf-8", "skill.js": "application/javascript; charset=utf-8",
          "voice.js": "application/javascript; charset=utf-8", "digest.js": "application/javascript; charset=utf-8",
          "digest_source.js": "application/javascript; charset=utf-8"}
MAX_UPLOAD = 512 * 2**20


class Job:
    def __init__(self, kind: str):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.status = "running"
        self.log: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.cancelled = False
        self.cancel_by: str | None = None   # user — пауза по кнопке; system — выход/блокировка (продолжить потом)
        self.task_id: str | None = None     # долгая задача из списка «Задачи» (opsdigest/tasks.py)
        self.abandoned: str | None = None   # остановлена снаружи (перед сводкой): поток может ещё доработать, но
                                            # задача уже не «идёт» и ничему не мешает
        self.meter: dict = {}               # ход для полосы в окне: шаг, проценты, скорость, сколько осталось
        self.title = ""                     # что именно идёт — для окна «Задачи» («Сводка за 3 ч, 12 чатов»)
        self.info: dict = {}                # для окна: беседа ассистента, период сводки…

    def cancel(self, by_user: bool = True) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        self.cancel_by = "user" if by_user else "system"
        self.add("Останавливаю… (текущая порция закончится и всё сохранится)" if by_user else
                 "Прерываю: программа закрывается или блокируется — продолжу при следующей возможности.")

    def abandon(self, reason: str) -> None:
        """Остановить короткую задачу сразу: окно получает ошибку, сводка больше её не ждёт."""
        if self.status != "running":
            return
        self.cancelled, self.cancel_by = True, "system"
        self.abandoned = reason
        self.add(f"⏹ Остановлено: {reason}.")
        self.error, self.status = f"Остановлено: {reason}", "error"

    def add(self, line: str) -> None:
        for part in str(line).splitlines() or [""]:
            self.log.append(part)
        if len(self.log) > 2000:
            del self.log[:500]

    def view(self, since: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "log": self.log[since:],
                "log_total": len(self.log), "result": self.result, "error": self.error,
                "seconds": round(time.time() - self.started, 1), "cancelled": self.cancelled, "meter": self.meter,
                "title": self.title, "info": self.info, "stopped": bool(self.abandoned)}


class App:
    def __init__(self, idle_exit: bool = False, supervisor=None, resume: bool = True):
        """supervisor — канал к сторожу (opsdigest/supervisor.py); resume — продолжать задачи из списка сам.
        idle_exit больше не используется: сервер не завершается сам, когда окно закрыто или компьютер спит."""
        # ключ окна: новый при каждом запуске; после перезапуска и обновления — прежний (его передаёт сторож/помощник)
        self.token = os.environ.pop("OPSDIGEST_TOKEN", "") or secrets.token_urlsafe(24)
        self.port = 0
        self.boot = secrets.token_hex(4)    # другой после перезапуска — окно понимает, что надо перечитать состояние
        self.sup = supervisor
        self.last_ping = time.time()
        self.jobs: dict[str, Job] = {}
        self.tg_lock = threading.Lock()
        self.cfg_lock = threading.RLock()   # config.json: окно и фоновые задачи пишут по очереди
        self.login_src = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="tg-loop").start()
        ws = wsmod.load_app_settings().get("workspace")
        self.ws: Path | None = Path(ws) if ws and Path(ws).exists() else None
        vault.bind(self.ws)
        self.server: ThreadingHTTPServer | None = None
        self.handler_cls = None
        self.live = Live(self)   # мессенджер: постоянное подключение к Telegram (запускается, когда окно его открыло)
        self.tasks = tasksmod.TaskBook(
            lambda: (self.ws / "data" / "tasks.json") if self.ws else None, sfiles.read_text,
            lambda p, text: sfiles.write_text(p, text), can_read=lambda: vault.status() != "full")
        self.keep_awake = wsmod.load_app_settings().get("keep_awake", True) is not False
        kd = wsmod.load_app_settings().get("keep_display", "auto")
        self.display_mode = kd if kd in ("auto", "on", "off") else "auto"
        self.sleeps: list[dict] = []          # когда компьютер спал (или процесс стоял) в этом запуске
        self.slept_while_busy = False         # спал, хотя шли задачи: «авто» начинает держать экран
        power.no_throttling()                 # Windows не урезает процессор программе без видимого окна
        self._stop = threading.Event()
        self._sched_pending: dict[str, datetime] = {}   # сводки по расписанию, которые ждут конца другой сводки
        self._sched_at = 0.0
        if self.sup is not None:  # ключ данных — сторожу: после сбоя задачи продолжатся без пароля
            vault.S.on_open.append(lambda ws, key: self.sup.send_key(ws, key))
            vault.S.on_full_lock.append(lambda: self.sup.send("forget"))
            self.sup.on_key = self._adopt_key
            if self.sup.pending_key:
                self._adopt_key(*self.sup.pending_key)
        if resume:
            threading.Thread(target=self._resumer, daemon=True, name="tasks").start()

    # ------------------------------------------------------------ помощники
    def run_async(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def run_async_job(self, coro, job: "Job"):
        """Как run_async, но остановка задачи (кнопка «Остановить») прерывает и работу с Telegram."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        while True:
            try:
                return fut.result(0.5)
            except TimeoutError:
                if job.cancelled:
                    fut.cancel()
                    from ..digest import DigestCancelled
                    raise DigestCancelled("остановлено") from None

    def need_ws(self) -> Path:
        if not self.ws:
            raise UserError("сначала выберите рабочую папку (шаг 1)")
        return self.ws

    def cfg(self):
        return load_config(wsmod.config_path(self.need_ws()))

    def raw(self) -> dict:
        raw = wsmod.read_config(self.need_ws())
        logs.add_secrets_from_config(raw)
        logs.add_secrets([str(self.ws)])
        return raw

    def save(self, data: dict) -> None:
        wsmod.write_config(self.need_ws(), data)

    def store(self):
        from ..service import open_store
        return open_store(self.cfg())

    def start_job(self, kind: str, fn: Callable[[Job], Any], task: dict | None = None) -> Job:
        """task — параметры запуска долгой задачи (тело запроса окна): она попадёт в список «Задачи» и будет
        доведена до конца — после перезапуска программы и после ошибки (см. opsdigest/tasks.py)."""
        job = Job(kind)
        self.jobs[job.id] = job
        if task is not None and kind in tasksmod.KINDS and self.ws:
            try:
                rec = self.tasks.begin(kind, task, job.id, self.task_title(kind, task))
                job.task_id = rec["id"]
                if rec.get("attempts", 1) > 1:
                    job.add(f"Продолжаю задачу (запуск {rec['attempts']}) — сделанное раньше не повторяется.")
            except Exception as e:  # noqa: BLE001 — список задач не должен мешать самой задаче
                log.warning("задача %s: не записана в список: %s", kind, logs.err_info(e))

        def run():
            log.info("задача %s: старт", kind)
            inflight.bind(job.id)   # запросы к моделям из этого потока — этой задачи (их можно оборвать)
            try:
                result = fn(job)
                if job.abandoned:   # остановили снаружи — результат уже никому не нужен
                    log.info("задача %s: доработала после остановки (%s)", kind, job.abandoned)
                    return
                job.result = result
                job.status = "done"
                r = job.result if isinstance(job.result, dict) else {}
                extra = ", ".join(f"{k}={r[k] if not isinstance(r[k], list) else len(r[k])}" for k in
                                  ("chats", "messages", "top", "errors", "rows", "any_ok") if k in r)
                log.info("задача %s: готово за %.1f с%s", kind, time.time() - job.started, f" ({extra})" if extra else "")
            except UserError as e:
                if not job.abandoned:
                    job.error, job.status = str(e), "error"
                log.info("задача %s: отказ: %s", kind, e)
            except Exception as e:  # noqa: BLE001
                if job.abandoned or (job.cancelled and getattr(e, "cancelled", False)):
                    job.error = job.error or f"Остановлено: {job.abandoned or 'задачу остановили'}"
                    job.status = "error"
                    log.info("задача %s: остановлена (%s)", kind, job.abandoned or job.cancel_by)
                else:
                    job.error, job.status = _human_error(e), "error"
                    job.add(traceback.format_exc(limit=3))
                    log.warning("задача %s: ошибка %s\n%s", kind, logs.err_info(e), logs.frames(e))
            except BaseException as e:  # noqa: BLE001 — иначе задача навсегда осталась бы «идёт» и мешала сводке
                job.error, job.status = f"Задача прервана: {type(e).__name__}", "error"
                log.warning("задача %s: прервана %s", kind, type(e).__name__)
                raise
            finally:
                inflight.bind(None)
                inflight.forget(job.id)
                if vault.has_key():
                    try:  # зашифрованный снимок базы — сразу после задачи
                        memdb.flush_all()
                    except Exception as e:  # noqa: BLE001
                        log.warning("снимок базы не сохранён: %s", logs.err_info(e))
                if job.task_id:
                    self._task_done(job)
                self.power_update()
        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        self.power_update()
        return job

    # ------------------------------------------------------------ долгие задачи: список, повтор, продолжение
    def task_title(self, kind: str, body: dict) -> str:
        def chat_title(cid: int) -> str:
            try:
                st = self.store()
                try:
                    c = st.get_chat(int(cid))
                finally:
                    st.close()
                return (c.title if c else "") or str(cid)
            except Exception:  # noqa: BLE001
                return str(cid)
        return tasksmod.title(kind, body, chat_title)

    def _task_done(self, job: Job) -> None:
        last = next((x for x in reversed(job.log) if x.strip() and not x.startswith(("Traceback", "  "))), "")
        cancelled = job.cancel_by if job.cancelled else None
        if not cancelled and isinstance(job.result, dict) and job.result.get("cancelled"):
            cancelled = "user"
        try:
            rec = self.tasks.finish(job.task_id, ok=job.status == "done", cancelled=cancelled, error=job.error,
                                    last=last, result=job.result)
        except Exception as e:  # noqa: BLE001
            log.warning("список задач: %s", logs.err_info(e))
            return
        if rec and rec.get("status") == "retry":
            wait = round(float(rec.get("next_at") or 0) - time.time())
            job.add(f"↻ Повторю автоматически через {max(1, wait // 60)} мин (попытка {rec['attempts'] + 1}). "
                    "Остановить — в окне «Задачи».")
            log.info("задача %s: повтор через %d с", job.kind, wait)

    def _resumer(self) -> None:
        """Раз в несколько секунд: запустить задачи, которые ждут (после перезапуска, после ошибки)."""
        last = time.time()
        while not self._stop.wait(5):
            now = time.time()
            woke = now - last > 60   # компьютер спал (или процесс стоял)
            if woke:
                self._woke(last, now)
            last = now
            try:
                self.resume_tick(woke)
            except Exception as e:  # noqa: BLE001
                log.warning("задачи: %s", logs.err_info(e))
            if now - self._sched_at >= 20:
                self._sched_at = now
                try:
                    self.schedule_tick()
                except Exception as e:  # noqa: BLE001
                    log.warning("расписание сводок: %s", logs.err_info(e))

    def schedule_tick(self, now: datetime | None = None) -> list[str]:
        """Сводки по расписанию. Только пока программа работает и данные открыты; плановое время, которое прошло,
        пока программа спала или была закрыта, пропускается молча. Если идёт другая сводка или ответ ассистента —
        запуск ждёт их конца (до schedule.MAX_WAIT). Возвращает id запущенных расписаний."""
        from .. import schedule as S
        from . import schedule_api as SA
        started: list[str] = []
        if not self.ws or vault.status() == "full":
            return started
        if vault.status() != "none" and vault.settings().get("require_telegram", True) and not self.tg_ok_cached():
            return started
        try:
            raw = self.raw()
        except (vault.Locked, OSError):
            return started
        items = {x["id"]: x for x in SA.schedules(raw) if x.get("enabled", True)}
        if not items and not self._sched_pending:
            return started
        now = now or datetime.now()
        state = SA.state_get(self)
        for e in items.values():
            t = S.due(e, now, S.parse_local((state.get(e["id"]) or {}).get("planned")), S.parse_local(e.get("created")))
            if t is not None and e["id"] not in self._sched_pending:
                self._sched_pending[e["id"]] = t
                log.info("расписание: пора (%s)", S.describe(e))
        for sid, t in sorted(self._sched_pending.items(), key=lambda kv: kv[1]):
            e = items.get(sid)
            if e is None or now - t > S.MAX_WAIT:
                self._sched_pending.pop(sid, None)
                if e is not None:
                    log.info("расписание: запуск %s пропущен — всё это время шла другая сводка", t.strftime("%H:%M"))
                continue
            if any(j.status == "running" and not j.task_id and j.kind not in LONG and j.kind not in NO_PREEMPT
                   and j.kind not in ("chats", "diag", "report") for j in list(self.jobs.values())):
                break   # идёт сводка, ответ ассистента, ИИ в чате — подождём, не обрываем
            self._sched_pending.pop(sid, None)
            try:
                SA.run(self, e, t)
                started.append(sid)
                log.info("расписание: сводка запущена (%s)", S.describe(e))
            except UserError as err:
                SA.state_put(self, sid, planned=t.isoformat(timespec="seconds"), status="error", error=str(err)[:300],
                             finished=datetime.now().isoformat(timespec="seconds"))
                log.info("расписание: не запустилось: %s", err)
            break   # по одной сводке за раз
        return started

    def _woke(self, slept_at: float, now: float) -> None:
        """Компьютер спал (или Windows остановила процесс): записать это и оборвать устаревшие соединения —
        запросы к модели повторятся сразу, а не через 10 минут ожидания ответа по мёртвой связи."""
        running = [j for j in self.jobs.values() if j.status == "running"]
        rec = {"from": slept_at, "to": now, "minutes": round((now - slept_at) / 60, 1),
               "jobs": [JOB_TITLES.get(j.kind, j.kind) for j in running][:6],
               "awake": power.AWAKE.active, "display": power.AWAKE.display, **power.snapshot()}
        self.sleeps = (self.sleeps + [rec])[-10:]
        if running or rec["awake"]:
            self.slept_while_busy = True
        n = inflight.abort_stale()
        log.info("пробуждение: не работали %.1f мин (запрет сна %s, экран %s, %s%s); оборвано соединений: %d",
                 rec["minutes"], "был" if rec["awake"] else "не был", "держали" if rec["display"] else "не держали",
                 "от сети" if rec.get("ac") else "от батареи" if rec.get("ac") is False else "питание ?",
                 ", Modern Standby" if rec.get("modern_standby") else "", n)

    def resume_tick(self, woke: bool = False) -> list[str]:
        """Запустить ждущие задачи. Возвращает виды запущенных (для проверок)."""
        started: list[str] = []
        if not self.ws or vault.status() == "full" or not self.tasks.load():
            self.power_update()
            return started
        if self.running("digest"):   # идёт сводка — долгие задачи ждут её конца (Maverick ведёт одну сессию)
            self.power_update()
            return started
        if woke:
            log.info("пробуждение: задачи с ошибками повторю сейчас")
            self.tasks.woke()
        due = self.tasks.due()
        if due and vault.status() != "none" and vault.settings().get("require_telegram", True) \
                and not self.tg_authorized():
            due = []   # данные открываются только после входа в Telegram — задачи подождут
        for rec in due:
            if self.running(rec["kind"]):
                continue
            path = tasksmod.KINDS[rec["kind"]][0]
            body = {**(rec.get("body") or {}), "_task": rec["id"],
                    "_resume": {"first": rec.get("first"), "attempt": int(rec.get("attempts") or 0) + 1}}
            try:
                h = self.handler_cls.__new__(self.handler_cls)   # без сокета: тот же разбор, что у запроса окна
                h._api("POST", path, body, {})
                started.append(rec["kind"])
                log.info("задача %s: продолжаю (запуск %d)", rec["kind"], body["_resume"]["attempt"])
            except UserError as e:
                self.tasks.later(rec["id"], str(e), 60 if "занят" in str(e) or "уже" in str(e) else None)
            except vault.Locked:
                break
            except Exception as e:  # noqa: BLE001
                self.tasks.later(rec["id"], _human_error(e))
                log.warning("задача %s: не запустилась: %s", rec["kind"], logs.err_info(e))
        self.power_update()
        return started

    @property
    def keep_display(self) -> bool:
        """Не гасить экран, пока идут задачи: «вкл», «выкл» или «авто» — на ноутбуке с Modern Standby (там сон
        наступает вместе с выключением экрана) и после того, как компьютер уже уснул посреди задач."""
        if self.display_mode in ("on", "off"):
            return self.display_mode == "on"
        return bool(power.snapshot().get("modern_standby")) or self.slept_while_busy

    def power_update(self) -> None:
        """Запрет сна — пока идёт любая задача (сводка, ИИ, ассистент, выгрузка…) или долгая ждёт запуска."""
        on = self.keep_awake and (any(j.status == "running" for j in self.jobs.values())
                                  or (self.tasks.loaded_for is not None and self.tasks.wants_awake()))
        power.AWAKE.set(on, display=on and self.keep_display)

    # ------------------------------------------------------------ сводка важнее всего остального
    def running_view(self, me: "Job | None" = None) -> list[dict]:
        return [{"id": j.id, "kind": j.kind, "title": j.title or JOB_TITLES.get(j.kind, j.kind).capitalize(),
                 "seconds": round(time.time() - j.started), "long": bool(j.task_id or j.kind in LONG),
                 "keep": j.kind in NO_PREEMPT, "last": next((x for x in reversed(j.log) if x.strip()), "")[:200],
                 "info": j.info, "cancelled": j.cancelled}
                for j in list(self.jobs.values()) if j.status == "running" and j is not me]

    def short_running(self) -> int:
        """Идущие задачи, которых нет в списке долгих (сводка, ассистент, ИИ в чате…), — для счётчика «Задачи»."""
        return sum(1 for j in list(self.jobs.values()) if j.status == "running" and not j.task_id
                   and j.kind not in NO_PREEMPT and j.kind not in ("chats", "diag", "report"))

    def stop_job(self, job: "Job", reason: str = "остановлено вами") -> dict:
        """Остановить одну задачу по кнопке. Долгая (из «Задач») — на паузу, с места остановки продолжится по
        «Продолжить». Короткая (сводка, ассистент, ИИ в чате, вопрос) — совсем: окно сразу получает «Остановлено»,
        запросы к модели этой задачи обрываются, открытые ею сессии Maverick закрываются."""
        if job.status != "running":
            return {"stopped": False, "status": job.status}
        if job.kind in NO_PREEMPT:
            raise UserError("эту задачу нельзя прервать (установка, шифрование или загрузка экспорта) — дождитесь конца")
        if job.task_id or job.kind in LONG:
            job.cancel(by_user=True)
            n = inflight.cancel_jobs([job.id], reason)
            return {"stopped": True, "paused": True, "connections": n}
        job.abandon(reason)
        n = inflight.cancel_jobs([job.id], reason)
        log.info("задача %s остановлена пользователем (оборвано запросов: %d)", job.kind, n)

        def release():
            try:
                res = self.release_maverick(job.add, jobs={job.id})
                if res["total"]:
                    log.info("после остановки %s: сессий Maverick закрыто %d из %d", job.kind, res["closed"], res["total"])
            except Exception as e:  # noqa: BLE001
                log.info("сессии Maverick после остановки: %s", logs.err_info(e))
            if job.kind == "asst" and job.info.get("thread"):
                try:
                    from .. import assistant as A
                    st = self.store()
                    try:
                        A.add(st, int(job.info["thread"]), "note", "⏹ Ответ остановлен вами.", {})
                    finally:
                        st.close()
                except Exception as e:  # noqa: BLE001
                    log.info("заметка об остановке не записана: %s", logs.err_info(e))
        threading.Thread(target=release, daemon=True, name="stop-release").start()
        self.power_update()
        return {"stopped": True, "paused": False, "connections": n}

    def preempt(self, me: "Job | None" = None, reason: str = "запущена сводка",
                progress: Callable[[str], None] | None = None, resume: bool = True) -> dict:
        """Остановить всё, что может занимать Maverick и Telegram: короткие задачи — совсем (окно получит
        «Остановлено»), долгие из «Задач» — на время (продолжатся сами, когда закончится сводка); оборвать идущие
        запросы к моделям и закрыть открытые сессии Maverick (ожидающие ответа человека, незаконченные).
        resume=False («Остановить всё» в окне «Задачи») — долгие задачи на паузу: сами не продолжатся."""
        note = progress or (lambda line: None)
        stopped, paused, ids = [], [], []
        if not resume:
            for t in self.tasks.view():
                if t["status"] in ("queued", "retry"):
                    self.tasks.pause(t["id"])
                    paused.append(JOB_TITLES.get(t["kind"], t["kind"]))
        for j in list(self.jobs.values()):
            if j is me or j.status != "running" or j.kind in NO_PREEMPT:
                continue
            ids.append(j.id)
            name = JOB_TITLES.get(j.kind, j.kind)
            if j.task_id or j.kind in LONG:
                j.cancel(by_user=not resume)
                j.add(f"⏸ Приостановлено: {reason} — продолжу, когда она закончится." if resume else
                      f"⏸ На паузе: {reason}. Продолжить — «⏳ Задачи» → «Продолжить».")
                paused.append(name)
            else:
                j.abandon(reason)
                stopped.append(name)
        conns = inflight.cancel_jobs(ids, reason)
        conns += inflight.abort_all(reason, keep=[me.id] if me else [])   # запросы окна без задачи — тоже
        if stopped:
            note("Остановлено: " + ", ".join(sorted(set(stopped))) + ".")
        if paused:
            note(("Приостановлено до конца сводки: " if resume else "На паузе: ") + ", ".join(sorted(set(paused))) + ".")
        if conns:
            note(f"Оборвано запросов к модели: {conns}.")
        sessions = self.release_maverick(note)
        if not (stopped or paused or conns or sessions["total"]):
            note("Других задач и открытых сессий Maverick нет.")
        log.info("перед сводкой: остановлено %d, приостановлено %d, соединений %d, сессий Maverick %d/%d",
                 len(stopped), len(paused), conns, sessions["closed"], sessions["total"])
        return {"stopped": stopped, "paused": paused, "connections": conns, "sessions": sessions}

    def release_maverick(self, note: Callable[[str], None] | None = None, jobs: set[str] | None = None) -> dict:
        """Закрыть открытые сессии Maverick: служебные (запомнены в этом запуске) и беседы ассистента «как на
        портале», которые ждут ответа на вопрос Maverick. jobs — только сессии этих задач (остановка одной задачи)."""
        note = note or (lambda line: None)
        out = {"total": 0, "closed": 0, "items": []}
        sessions = inflight.open_sessions()
        if jobs is not None:
            sessions = {k: v for k, v in sessions.items() if v.get("job") in jobs}
            if not sessions:
                return out
        try:
            cfg = self.cfg()
            name, prof = cfg.llm_profile("maverick")
        except Exception:  # noqa: BLE001 — Maverick не настроен или папка закрыта
            return out
        from ..llm import make_client
        threads: dict[str, int] = {}
        st = None
        try:
            st = self.store()
            from .. import assistant as A
            A.ensure(st)
            # остановка одной задачи (jobs) — беседы, ждущие ответа на вопрос Maverick, не трогаем
            for r in (st.db.execute("SELECT id, meta FROM asst_threads WHERE meta LIKE '%mav_hitl%'")
                      if jobs is None else []):
                m = json.loads(r["meta"] or "{}")
                if m.get("mav_session") and m.get("mav_hitl") is not None:
                    sid = str(m["mav_session"])
                    threads[sid] = r["id"]
                    sessions.setdefault(sid, {"why": "hitl", "owner": "portal", "hitl": m.get("mav_hitl") or {}})
        except Exception as e:  # noqa: BLE001 — таблицы ассистента ещё нет
            log.info("беседы ассистента не просмотрены: %s", logs.err_info(e))
        if not sessions:
            if st is not None:
                st.close()
            return out
        try:
            llm = make_client(name, dict(prof))
        except Exception as e:  # noqa: BLE001
            note(f"Maverick: не получилось подключиться, чтобы закрыть сессии ({str(e)[:120]}).")
            if st is not None:
                st.close()
            return out
        jf = (self.ws / "data" / "maverick-sessions.txt") if self.ws else None

        def journal(line: str) -> None:
            if jf is None:
                return
            try:
                jf.parent.mkdir(parents=True, exist_ok=True)
                old = jf.read_text(encoding="utf-8")[-20000:] if jf.exists() else ""
                jf.write_text(old + f"[{datetime.now():%d.%m %H:%M:%S}] {line}\n", encoding="utf-8")
            except OSError:
                pass
        try:
            for sid, info in sessions.items():
                if not hasattr(llm, "release_session"):
                    break
                out["total"] += 1
                owner = info.get("owner") or "service"
                r = llm.release_session(sid, owner=owner, hitl=info.get("hitl") if info.get("why") == "hitl" else None,
                                        journal=journal)
                out["closed"] += 1 if r["ok"] else 0
                out["items"].append({"owner": owner, "why": info.get("why"), "ok": r["ok"], "how": r["how"]})
                what = "беседа ассистента «как на портале»" if owner == "portal" else "служебный запрос"
                why = {"hitl": "ждала ответа на вопрос Maverick", "busy": "ждала ответа",
                       "processing": "ещё обрабатывалась"}.get(info.get("why") or "", "открыта")
                note(f"Сессия Maverick ({what}, {why}): " + (f"закрыта — {r['how']}." if r["ok"] else
                     "закрыть не удалось — Maverick не принял ни один способ (подробности — data\\maverick-sessions.txt). "
                     "Если сводка ответит «занят», закройте беседу в веб-чате Maverick."))
                tid = threads.get(sid)
                if tid and st is not None and r["ok"]:
                    from .. import assistant as A
                    row = st.db.execute("SELECT meta FROM asst_threads WHERE id=?", (tid,)).fetchone()
                    m = json.loads((row["meta"] if row else None) or "{}")
                    m.pop("mav_hitl", None)
                    A.update(st, tid, meta=m)
                    A.add(st, tid, "note", "Сводка закрыла ожидавший ответа вопрос Maverick (ответ «отмена»). "
                                           "Если действие нужно — напишите заново.", {})
        finally:
            if st is not None:
                st.close()
        return out

    def _adopt_key(self, ws: str, key: bytes) -> None:
        try:
            if vault.adopt(key, ws or None):
                log.info("ключ данных получен от сторожа: задачи продолжатся, окно — под ПИН-кодом")
        except Exception as e:  # noqa: BLE001
            log.warning("ключ от сторожа не подошёл: %s", logs.err_info(e))

    def tasks_api(self, method: str, path: str, b: dict) -> dict | None:
        if path == "/api/tasks":
            self.tasks.load()
            return {"tasks": self.tasks.view(), "keep_awake": self.keep_awake, "awake": power.AWAKE.active,
                    "display_mode": self.display_mode, "display": power.AWAKE.display, "power": power.snapshot(),
                    "sleeps": [dict(x, **{"from": datetime.fromtimestamp(x["from"]).isoformat(timespec="minutes"),
                                          "to": datetime.fromtimestamp(x["to"]).isoformat(timespec="minutes")})
                               for x in self.sleeps[-5:]], "slept_while_busy": self.slept_while_busy,
                    "running": self.running_view(),
                    "power_supported": power.AWAKE.supported, "supervised": self.sup is not None,
                    "autostart": wsmod.autostart_status()}
        if path == "/api/tasks/settings" and method == "POST":
            if "keep_awake" in b:
                self.keep_awake = bool(b["keep_awake"])
                wsmod.save_app_settings({"keep_awake": self.keep_awake})
                self.power_update()
            if b.get("keep_display") in ("auto", "on", "off"):
                self.display_mode = b["keep_display"]
                wsmod.save_app_settings({"keep_display": self.display_mode})
                self.power_update()
            if "autostart" in b:
                ok, msg = wsmod.autostart_set(bool(b["autostart"]))
                if not ok:
                    raise UserError(msg)
            return self.tasks_api("GET", "/api/tasks", {})
        m = re.match(r"^/api/tasks/(\w+)/(pause|resume|remove|now)$", path)
        if m and method == "POST":
            tid, act = m.groups()
            rec = self.tasks.get(tid)
            if not rec:
                raise UserError("задача не найдена")
            if act == "pause":
                job = self.jobs.get(rec.get("job") or "")
                if rec["status"] == "running" and job and job.status == "running":
                    job.cancel(by_user=True)
                else:
                    self.tasks.pause(tid)
            elif act in ("resume", "now"):
                self.tasks.resume(tid)
                self.resume_tick()
            elif act == "remove" and not self.tasks.remove(tid):
                raise UserError("идущую задачу сначала остановите")
            return self.tasks_api("GET", "/api/tasks", {})
        return None

    def stop(self) -> None:
        """Сервер останавливается: идущие задачи — продолжить при следующем запуске."""
        self._stop.set()
        try:
            self.tasks.interrupt_running()
        except Exception:  # noqa: BLE001
            pass
        power.AWAKE.set(False)

    def busy(self) -> bool:
        """Идёт короткая задача (сводка, вопрос, проверка) — вторую такую же не запускаем."""
        return any(j.status == "running" and j.kind not in LONG and j.kind not in PARALLEL for j in self.jobs.values())

    def busy_error(self) -> "UserError":
        """Понятный отказ: что именно сейчас идёт и как это остановить."""
        now = [f"{JOB_TITLES.get(j.kind, j.kind)} ({max(1, round((time.time() - j.started) / 60))} мин)"
               for j in self.jobs.values() if j.status == "running" and j.kind not in LONG and j.kind not in PARALLEL]
        return UserError("дождитесь окончания текущей задачи" + (f": идёт {', '.join(now)}" if now else "") +
                         ". Остановить всё — «⏳ Задачи» → «Остановить всё»; сводка останавливает остальное сама.")

    def running(self, *kinds: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.status == "running" and (not kinds or j.kind in kinds)), None)

    def tg_busy(self) -> Job | None:
        """Фоновая задача, которая сейчас держит подключение к Telegram (выгрузка, скачивание изображений)."""
        return self.running(*BACKGROUND)

    def tg_take(self, timeout: float = 0.5):
        """Захватить Telegram для короткой операции или понятно отказать, если идёт выгрузка."""
        if not self.tg_lock.acquire(timeout=timeout):
            j = self.tg_busy()
            what = {"export": "выгрузка истории", "images": "скачивание изображений", "fetch": "докачка файла"}.get(
                j.kind if j else "", "другая операция с Telegram")
            raise UserError(f"Telegram занят: идёт {what} — дождитесь окончания или остановите её")
        return _Release(self.tg_lock)

    # ------------------------------------------------------------ состояние
    def state(self) -> dict:
        st: dict[str, Any] = {
            "version": __version__, "python": sys.version.split()[0], "platform": sys.platform,
            "workspace": str(self.ws) if self.ws else "", "default_workspace": str(wsmod.default_workspace()),
            "workspace_in_cloud": bool(self.ws and wsmod.in_cloud_folder(self.ws)),
            "steps": {"workspace": bool(self.ws), "llm": False, "telegram": False, "login": False,
                      "profile": False, "chats": False},
            "me": None, "schedule": wsmod.schedule_status(), "busy": self.busy(), "ui": ui_settings(),
        }
        if not self.ws:
            return st
        vs = vault.status()
        st["vault"] = {"status": vs, "enabled": vs != "none"}
        if vs in ("full", "pin"):  # данные закрыты: окно показывает только вход
            st["locked"] = vs
            st["vault"]["pin_len"] = vault.pin_length() if vs == "pin" else 0
            return st
        raw = self.raw()
        ui = raw.get("ui") or {}
        tg = raw.get("telegram") or {}
        st["steps"]["llm"] = bool(ui.get("llm_ok"))
        st["steps"]["telegram"] = bool(str(tg.get("api_id") or "").strip() and str(tg.get("api_hash") or "").strip())
        st["steps"]["profile"] = bool((raw.get("profile") or {}).get("name"))
        st["steps"]["chats"] = bool(ui.get("chats_reviewed"))
        try:  # что уже есть в базе (после загрузки экспорта вход в Telegram для сводки не обязателен)
            cfg = self.cfg()
            dbp = cfg.path(cfg["storage"]["path"])
            if memdb.exists(dbp):
                s = self.store()
                n = s.db.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id), MIN(date), MAX(date) FROM messages").fetchone()
                st["base"] = {"messages": n[0], "chats": n[1], "from": n[2], "to": n[3],
                              "last_import": s.kv_get("last_import"), "last_sync": s.kv_get("last_sync"),
                              "last_export": s.kv_get("last_export"), "media": s.media_stats()}
                s.close()
        except Exception:  # noqa: BLE001
            pass
        session = self.ws / "data" / "telegram.session"
        if session.exists():
            try:
                s = self.store()
                me = s.kv_get("me")
                s.close()
                if me:
                    st["me"] = json.loads(me)
                    logs.add_secrets([st["me"].get("name"), st["me"].get("username")])
                    st["steps"]["login"] = True
            except Exception:  # noqa: BLE001
                pass
        st["llm_label"] = (raw.get("llm") or {}).get("provider", "")
        if vs == "open":
            vset = vault.settings()
            st["vault"].update(require_telegram=bool(vset.get("require_telegram", True)),
                               lock_minutes=vault.lock_minutes(), telegram_ok=self.tg_authorized())
            if st["vault"]["require_telegram"] and not st["vault"]["telegram_ok"]:
                st["locked"] = "telegram"  # пароль верный, но без входа в Telegram данные не показываем
        st["background"] = [{"id": j.id, "kind": j.kind, "seconds": round(time.time() - j.started)}
                            for j in self.jobs.values() if j.status == "running" and j.kind in LONG]
        st["running"] = self.running_view()
        try:
            self.tasks.load()
            tv = self.tasks.view()
            st["tasks"] = {"active": sum(1 for t in tv if t["status"] in ("running", "queued", "retry")),
                           "paused": sum(1 for t in tv if t["status"] == "paused"),
                           "retry": sum(1 for t in tv if t["status"] == "retry")}
        except Exception:  # noqa: BLE001
            pass
        return st

    # ------------------------------------------------------------ защита данных
    def tg_authorized(self) -> bool:
        """Вход в Telegram выполнен: есть сессия, и это та же учётка, под которой включали защиту."""
        if not self.ws or not (self.ws / "data" / "telegram.session").exists():
            return False
        try:
            s = self.store()
            me = json.loads(s.kv_get("me") or "{}")
            s.close()
        except Exception:  # noqa: BLE001
            return False
        if not me.get("id"):
            return False
        owner = vault.settings().get("owner")
        if not owner:
            vault.update_settings(owner=me["id"])  # первая учётка после включения защиты — владелец данных
            return True
        return int(owner) == int(me["id"])

    def voice_settings(self) -> dict:
        from .. import voice
        return voice.settings_of(wsmod.load_app_settings())

    def gate(self, path: str) -> str | None:
        """Почему запрос нельзя выполнить сейчас: full | pin | telegram, или None — можно."""
        if path in OPEN_ALWAYS or path.startswith("/api/vault/"):
            return None
        vs = vault.status()
        if vs in ("full", "pin"):
            return vs
        if path in OPEN_TELEGRAM or path.startswith("/api/jobs/"):
            return None   # доступно и без входа в Telegram — база не нужна (ход задачи окно спрашивает раз в секунду)
        if vs == "open" and vault.settings().get("require_telegram", True) and not self.tg_ok_cached():
            return "telegram"
        return None

    def tg_ok_cached(self, ttl: float = 15.0) -> bool:
        """tg_authorized() читает базу — на каждый запрос окна это лишнее ожидание замка базы. Помним 15 с
        (вход и выход из Telegram сбрасывают)."""
        now = time.time()
        c = getattr(self, "_tg_ok", None)
        if c and c[0] and now - c[1] < ttl and c[2] == str(self.ws):
            return True
        ok = self.tg_authorized()   # «не вошли» не запоминаем: после входа данные должны открыться сразу
        self._tg_ok = (ok, now, str(self.ws))
        return ok

    def vault_api(self, path: str, b: dict) -> dict | None:
        ws = self.need_ws()
        if path == "/api/vault/setup":
            if vault.exists(ws):
                raise UserError("защита уже включена")
            if self.running():
                raise UserError("дождитесь окончания текущих задач (выгрузка, сводка) — потом включите защиту")
            pw, pw2, pin = str(b.get("password") or ""), str(b.get("password2") or ""), str(b.get("pin") or "")
            if pw != pw2:
                raise UserError("пароли не совпадают")
            self.close_login()  # ни одного открытого соединения со старой открытой базой
            try:
                code = vault.create(ws, pw, pin, {"lock_minutes": int(b.get("lock_minutes") or 10),
                                                  "require_telegram": bool(b.get("require_telegram", True))})
            except ValueError as e:
                raise UserError(str(e)) from None
            job = self.start_job("secure", lambda job: self.migrate(job))
            log.info("защита данных включена")
            return {"recovery": code, "job": job.id}
        if path == "/api/vault/unlock":
            try:
                vault.unlock(str(b.get("password") or ""), ws)
            except vault.BadSecret as e:
                time.sleep(1.0)  # медленный перебор
                raise UserError(str(e)) from None
            self._after_unlock()
            return {"status": vault.status()}
        if path == "/api/vault/recover":
            try:
                vault.recover(str(b.get("code") or ""), str(b.get("password") or ""), ws)
            except (vault.BadSecret, ValueError) as e:
                time.sleep(1.0)
                raise UserError(str(e)) from None
            self._after_unlock()
            return {"status": vault.status()}
        if path == "/api/vault/pin":
            try:
                vault.unlock_pin(str(b.get("pin") or ""))
            except vault.BadSecret as e:
                time.sleep(0.5)
                raise UserError(str(e)) from None
            except vault.Locked as e:
                raise UserError(str(e)) from None
            return {"status": vault.status()}
        if path == "/api/vault/lock":
            if b.get("full"):
                self.full_lock()
            else:
                vault.lock_pin()
            return {"status": vault.status()}
        if path == "/api/vault/activity":
            vault.touch()
            return {"status": vault.status()}
        if path == "/api/vault/settings":
            if vault.status() != "open":
                raise UserError("сначала разблокируйте программу")
            if b.get("new_password"):
                try:
                    vault.change_password(str(b.get("password") or ""), str(b["new_password"]))
                except (vault.BadSecret, ValueError) as e:
                    raise UserError(str(e)) from None
            if b.get("new_pin"):
                try:
                    vault.set_pin(str(b["new_pin"]))
                except ValueError as e:
                    raise UserError(str(e)) from None
            kw = {}
            if b.get("lock_minutes") not in (None, ""):
                try:
                    kw["lock_minutes"] = max(1, min(120, int(b["lock_minutes"])))
                except (TypeError, ValueError):
                    raise UserError("минуты — целое число") from None
            if "require_telegram" in b:
                want = bool(b["require_telegram"])
                if not want and vault.settings().get("require_telegram", True) and not self.tg_authorized() \
                        and not vault.check_recovery(str(b.get("code") or "")):
                    raise UserError("без входа в Telegram это условие можно снять только кодом восстановления")
                kw["require_telegram"] = want
            if kw:
                vault.update_settings(**kw)
            return {"settings": {k: v for k, v in vault.settings().items() if k != "owner"}}
        return None

    def _after_unlock(self) -> None:
        log.info("данные открыты")
        if self.ws and any((self.ws / p).is_file() and not sfiles.is_sealed(self.ws / p)
                           for p in ("config.json", "data/telegram.session")):
            self.start_job("secure", lambda job: self.migrate(job))  # остались открытые файлы — дошифровать

    def full_lock(self) -> None:
        for j in self.jobs.values():
            if j.status == "running" and (j.kind in BACKGROUND or j.task_id):
                j.cancel(by_user=False)
        try:  # пока ключ ещё есть: прерванные задачи продолжатся после входа по паролю
            self.tasks.interrupt_running()
        except Exception as e:  # noqa: BLE001
            log.warning("список задач не сохранён: %s", logs.err_info(e))
        self.live.stop()
        self.close_login()
        vault.lock_full()
        log.info("программа заблокирована полностью")

    def migrate(self, job) -> dict:
        """Включили защиту: зашифровать всё, что уже лежит в рабочей папке."""
        from ..secure.migrate import migrate_workspace
        return migrate_workspace(self.cfg(), progress=job.add, cancel=lambda: job.cancelled)

    # ------------------------------------------------------------ настройки для формы
    def config_view(self) -> dict:
        raw = self.raw()
        tg = raw.get("telegram") or {}
        llm = raw.get("llm") or {}
        prof = llm.get("profiles") or {}
        loc, cl, mv, cc = (prof.get(k) or {} for k in ("local", "claude", "maverick", "claude_code"))
        mv_token = mv.get("api_key") or (mv.get("headers") or {}).get("Authorization", "")
        return {
            "profile": raw.get("profile") or {},
            "telegram": {"api_id": str(tg.get("api_id") or ""), "api_hash_set": bool(tg.get("api_hash")),
                         "phone": tg.get("phone") or "", "proxies": tg.get("proxies") or [],
                         "try_direct": bool(tg.get("try_direct")), "web_egress": tg.get("web_egress") or "auto",
                         "vpn": {k: (tg.get("vpn") or {}).get(k) for k in ("enabled", "source", "selected")}},
            "llm": {
                "provider": llm.get("provider") or "local",
                "local": {"base_url": loc.get("base_url") or "http://localhost:1235/v1", "model": loc.get("model") or "auto",
                          "ctx": loc.get("max_context_tokens") or 32768},
                "claude": {"model": cl.get("model") or "claude-sonnet-5-5", "api_key_set": bool(cl.get("api_key"))},
                "maverick": {"style": {"http_json": "custom", "openai": "openai"}.get(mv.get("type"), "agent"),
                             "url": mv.get("url") or mv.get("base_url") or "", "model": mv.get("model") or "",
                             "token_set": bool(mv_token),
                             "body": json.dumps(mv.get("body") or {"query": "{{prompt}}"}, ensure_ascii=False),
                             "response_path": mv.get("response_path") or "answer"},
                "claude_code": {"command": cc.get("command") or "claude -p --output-format text"},
            },
            "digest": raw.get("digest") or {},
            "media": self.media_view(raw),
            "privacy": self.privacy_view(raw),
        }

    @staticmethod
    def privacy_view(raw: dict) -> dict:
        pv = raw.get("privacy") or {}
        people = "\n".join(f"{p.get('name', '')}: {', '.join(p.get('aliases') or [])}"
                           for p in pv.get("people") or [] if isinstance(p, dict) and p.get("name"))
        return {"pseudonymize": str(pv.get("pseudonymize", "external")) != "off",
                "soften": pv.get("soften", True) is not False, "people": people,
                "keep": ", ".join(pv.get("keep") or [])}

    def save_privacy(self, body: dict) -> dict:
        raw = self.raw()
        pv = raw.setdefault("privacy", {})
        if "pseudonymize" in body:
            pv["pseudonymize"] = "external" if body.get("pseudonymize") else "off"
        if "soften" in body:
            pv["soften"] = bool(body.get("soften"))
        if "people" in body:
            people = []
            for line in str(body.get("people") or "").splitlines():
                if ":" not in line:
                    continue
                name, al = line.split(":", 1)
                aliases = [a.strip() for a in re.split(r"[,;]", al) if a.strip()]
                if name.strip() and aliases:
                    people.append({"name": name.strip(), "aliases": aliases})
            pv["people"] = people
        if "keep" in body:
            pv["keep"] = [w.strip() for w in re.split(r"[,;\n]", str(body.get("keep") or "")) if w.strip()]
        self.save(raw)
        return {"privacy": self.privacy_view(raw)}

    def media_view(self, raw: dict) -> dict:
        from ..config import DEFAULTS
        m = {**DEFAULTS["media"], **(raw.get("media") or {})}
        profiles = (raw.get("llm") or {}).get("profiles") or {}
        vp = m.get("vision_provider") or ""
        prof = profiles.get(vp or (raw.get("llm") or {}).get("provider") or "", {})
        mv = profiles.get("maverick") or {}
        return {k: m.get(k) for k in ("download", "light_max_mb", "image_max_mb", "photo_max_px", "vision_provider",
                                      "describe_in_digest", "auto_describe_max", "describe_workers")} | {
            "profiles": [k for k in profiles], "image_mode": bool(prof.get("image_mode")),
            "maverick": mv.get("type") == "maverick", "maverick_image_mode": bool(mv.get("image_mode")),
            "maverick_image_body": json.dumps(mv["image_body"], ensure_ascii=False, indent=1)
            if isinstance(mv.get("image_body"), dict) else ""}

    def save_media(self, body: dict) -> dict:
        raw = self.raw()
        m = raw.setdefault("media", {})
        if "vision_provider" in body:
            vp = str(body.get("vision_provider") or "")
            if vp and vp not in ((raw.get("llm") or {}).get("profiles") or {}):
                raise UserError("эта модель ещё не настроена — сначала сохраните её на шаге «Модель»")
            m["vision_provider"] = vp
        for k, lo, hi in (("light_max_mb", 0, 100), ("image_max_mb", 1, 50), ("auto_describe_max", 0, 50),
                          ("describe_workers", 1, 8), ("photo_max_px", 320, 2560)):
            if body.get(k) not in (None, ""):
                try:
                    m[k] = max(lo, min(hi, float(body[k]) if k.endswith("_mb") else int(body[k])))
                except (TypeError, ValueError):
                    raise UserError(f"{k}: нужно число") from None
        for k in ("download", "describe_in_digest"):
            if k in body:
                m[k] = bool(body[k])
        if "maverick_image_body" in body:  # формат запроса с картинкой для Maverick, заданный вручную
            prof = ((raw.get("llm") or {}).get("profiles") or {}).get("maverick")
            if prof is None:
                raise UserError("Maverick ещё не настроен — сначала сохраните его на шаге «Модель»")
            txt = str(body.get("maverick_image_body") or "").strip()
            if txt:
                try:
                    tmpl = json.loads(txt)
                except ValueError as e:
                    raise UserError(f"формат запроса — не JSON: {e}") from None
                if not isinstance(tmpl, dict) or "{{image_" not in txt:
                    raise UserError("в шаблоне нужен объект JSON с подстановкой картинки: {{image_base64}} "
                                    "или {{image_data_url}}, и текста: {{prompt}}")
                if tmpl != prof.get("image_body"):
                    prof["image_body"] = tmpl
                    prof.pop("image_mode", None)
            elif prof.pop("image_body", None) is not None:
                prof.pop("image_mode", None)
        self.save(raw)
        return {"media": self.media_view(raw)}

    def save_llm(self, body: dict) -> dict:
        raw = self.raw()
        llm = raw.setdefault("llm", {})
        profiles = llm.setdefault("profiles", {})
        kind = body.get("provider")
        f = body.get(kind) or {}
        old = profiles.get(kind) or {}
        if kind == "local":
            profiles["local"] = {"type": "openai", "base_url": (f.get("base_url") or "").strip().rstrip("/"),
                                 "api_key": "lm-studio", "model": f.get("model") or "auto", "prefer": "instruct",
                                 "max_context_tokens": int(f.get("ctx") or 32768), "timeout": 900}
        elif kind == "claude":
            key = (f.get("api_key") or "").strip() or old.get("api_key", "")
            if not key:
                raise UserError("введите API-ключ Claude (console.anthropic.com → API Keys)")
            profiles["claude"] = {"type": "anthropic", "api_key": key, "model": f.get("model") or "claude-sonnet-5-5",
                                  "max_context_tokens": 200000, "external": True}
        elif kind == "maverick":
            token = (f.get("token") or "").strip()
            if not token:
                token = old.get("api_key") or (old.get("headers") or {}).get("Authorization", "").removeprefix("Bearer ")
            url = (f.get("url") or "").strip()
            if not url:
                raise UserError("укажите адрес Maverick (адрес сайта, например https://maverick.company.ru)")
            style = f.get("style") or "agent"
            if style == "agent":
                if not token:
                    raise UserError("введите API-ключ Maverick (sk_…): веб-интерфейс Maverick → «API-ключи» → «Создать ключ»")
                profiles["maverick"] = {"type": "maverick", "url": url.rstrip("/"), "api_key": token,
                                        "max_context_tokens": 32768, "timeout": 600, "external": False}
                if old.get("type") == "maverick" and old.get("body_field"):
                    profiles["maverick"]["body_field"] = old["body_field"]
            elif style == "custom":
                try:
                    tmpl = json.loads(f.get("body") or "{}")
                except ValueError as e:
                    raise UserError(f"шаблон запроса — не JSON: {e}") from None
                profiles["maverick"] = {"type": "http_json", "url": url, "body": tmpl,
                                        "headers": {"Authorization": f"Bearer {token}"} if token else {},
                                        "response_path": f.get("response_path") or "", "model": f.get("model") or "",
                                        "max_context_tokens": 32768, "external": False}
            else:
                profiles["maverick"] = {"type": "openai", "base_url": url.rstrip("/"), "api_key": token,
                                        "model": f.get("model") or "auto", "max_context_tokens": 32768,
                                        "external": False}
        elif kind == "claude_code":
            profiles["claude_code"] = {"type": "command", "command": f.get("command") or "claude -p --output-format text",
                                       "timeout": 900, "external": True}
        else:
            raise UserError("выберите модель")
        new = profiles.get(kind) or {}
        for k in ("image_mode", "vision_model", "image_body", "image_command"):  # найденное раньше — сохраняем
            if old.get(k) and old.get("type") == new.get("type") and k not in new:
                new[k] = old[k]
        llm["provider"] = kind
        raw.setdefault("ui", {})["llm_ok"] = False
        self.save(raw)
        return {"saved": kind}

    def save_telegram(self, body: dict) -> dict:
        from ..sources.tg_proxy import ProxyError, parse_proxy
        raw = self.raw()
        tg = raw.setdefault("telegram", {})
        api_id = str(body.get("api_id") or "").strip()
        if api_id and not api_id.isdigit():
            raise UserError("api_id — это число (my.telegram.org → API development tools)")
        tg["api_id"] = api_id
        if (body.get("api_hash") or "").strip():
            tg["api_hash"] = body["api_hash"].strip()
        tg["phone"] = re.sub(r"[^\d+]", "", body.get("phone") or "")
        lines = [l.strip() for l in re.split(r"[\r\n]+", body.get("proxies") or "") if l.strip()]
        bad, good = [], []
        for l in lines:
            try:
                good.append((l, parse_proxy(l).describe()))
            except ProxyError as e:
                bad.append(f"{l[:50]}… — {e}")
        tg["proxies"] = [l for l, _ in good]
        tg["try_direct"] = bool(body.get("try_direct"))
        eg = (body.get("web_egress") or "auto").strip()
        tg["web_egress"] = eg if eg.lower() in ("auto", "off") or re.match(r"(?i)^(https?://)?([^\s/@]+@)?[\w.-]+(:\d+)?/?$", eg) else "auto"
        self.save(raw)
        return {"proxies": [d for _, d in good], "bad": bad}

    def save_vpn(self, body: dict) -> dict:
        raw = self.raw()
        tg = raw.setdefault("telegram", {})
        vpn = tg.setdefault("vpn", {})
        if "source" in body:
            src = "\n".join(l.strip() for l in str(body.get("source") or "").splitlines() if l.strip())
            if src != (vpn.get("source") or ""):
                vpn["selected"] = "auto"
                try:  # подписка сменилась — старый список серверов не нужен
                    self.cfg().path("data/vpn_servers.json").unlink()
                except OSError:
                    pass
            vpn["source"] = src
        if "enabled" in body:
            vpn["enabled"] = bool(body.get("enabled"))
        if body.get("selected"):
            vpn["selected"] = str(body["selected"])[:40]
        self.save(raw)
        return {"vpn": {k: vpn.get(k) for k in ("enabled", "source", "selected")}}

    def vpn_view(self) -> dict:
        from .. import vpn
        st = vpn.xray.status()
        out: dict[str, Any] = {"xray": st, "running": vpn.xray.RUNNER.running(), "asset": vpn.xray.asset_name(),
                               "releases": vpn.xray.RELEASES_PAGE}
        if self.ws:
            data = vpn.cached(self.cfg())
            out.update(rows=data.get("results") or [], info=data.get("info") or {}, at=data.get("at"),
                       servers=len(data.get("servers") or []))
        return out

    def save_profile(self, body: dict) -> dict:
        raw = self.raw()
        split = lambda s: [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]  # noqa: E731
        comps = []
        for c in body.get("competences") or []:
            if (c.get("name") or "").strip():
                comps.append({"name": c["name"].strip(), "description": (c.get("description") or "").strip(),
                              "keywords": split(c.get("keywords") if isinstance(c.get("keywords"), str)
                                                else ", ".join(c.get("keywords") or []))})
        raw["profile"] = {"name": (body.get("name") or "").strip(), "role": (body.get("role") or "").strip(),
                          "aliases": split(body.get("aliases") if isinstance(body.get("aliases"), str)
                                           else ", ".join(body.get("aliases") or [])),
                          "responsibilities": (body.get("responsibilities") or "").strip(),
                          "ignore_topics": split(body.get("ignore_topics") if isinstance(body.get("ignore_topics"), str)
                                                 else ", ".join(body.get("ignore_topics") or [])),
                          "competences": comps}
        self.save(raw)
        return {"saved": True}

    @staticmethod
    def _chat_rule(c: dict, defaults: dict) -> dict | None:
        """Правило для одного чата из формы. None — всё как по умолчанию, правило не нужно."""
        base = defaults.get(c.get("type"), "normal")
        base_rank = base if base != "ignore" else "low"
        prio = c.get("priority") if c.get("priority") in ("high", "normal", "low") else base_rank
        if c.get("priority") == "ignore":  # старое окно: «не читать»
            prio, c["exclude"] = base_rank, True
        kind = c.get("kind") if c.get("kind") in ("chat", "incident", "alerts", "news") else "chat"
        excl = bool(c.get("exclude"))
        desc = str(c.get("description") or "").strip()[:4000]
        if not desc and kind == "chat" and prio == base_rank and excl == (base == "ignore") and not c.get("explicit"):
            return None
        rule = {"match": str(int(c["id"])), "title": str(c.get("title") or "")[:80], "priority": prio, "kind": kind}
        if excl:
            rule["exclude"] = True
        if desc:
            rule["description"] = desc
        return rule

    def save_chats(self, body: dict) -> dict:
        """Настройки чатов из таблицы (или одного чата — body.only=True): приоритет, режим, исключение, описание."""
        with self.cfg_lock:
            return self._save_chats(body)

    def _save_chats(self, body: dict) -> dict:
        raw = self.raw()
        tg = raw.setdefault("telegram", {})
        defaults = tg.get("defaults") or {"private": "high", "group": "normal", "channel": "low", "bot": "ignore"}
        sent = {str(int(c["id"])): c for c in body.get("chats") or [] if str(c.get("id", "")).lstrip("-").isdigit()}
        rules = tg.get("chats") or []
        by_id = [r for r in rules if re.fullmatch(r"-?\d+", str(r.get("match", "")))]
        named = [r for r in rules if not re.fullmatch(r"-?\d+", str(r.get("match", "")))]
        keep = [r for r in by_id if str(r.get("match")) not in sent]  # чаты, которых нет в форме, — не трогаем
        old = {str(r.get("match")): r for r in by_id}
        mine = []
        for c in sent.values():  # части ИИ и настройки тем, которых нет в форме, сохраняются
            r = self._chat_rule(c, defaults)
            prev = old.get(str(int(c["id"]))) or {}
            extra = {k: prev[k] for k in ("ai_description", "ai_at", "ai_mode", "ai_priority", "topics") if prev.get(k)}
            if r is None and extra:
                r = {"match": str(int(c["id"])), "title": str(c.get("title") or "")[:80]}
            if r is not None:
                r.update(extra)
                mine.append(r)
        tg["chats"] = mine + keep + named
        if not body.get("only"):
            raw.setdefault("ui", {})["chats_reviewed"] = True
        self.save(raw)
        try:
            from ..chatrules import apply_chat_rules
            st = self.store()
            try:
                apply_chat_rules(self.cfg(), st)
            finally:
                st.close()
        except Exception as e:  # noqa: BLE001
            log.warning("правила чатов не применены к базе: %s", logs.err_info(e))
        return {"rules": len(mine) + len(keep)}

    def chats_view(self) -> list[dict]:
        """Чаты из базы (без Telegram) с настройками и счётчиками сообщений и изображений."""
        cfg = self.cfg()
        if not memdb.exists(cfg.path(cfg["storage"]["path"])):
            return []
        st = self.store()
        try:
            rows = st.chat_overview()
        finally:
            st.close()
        for r in rows:
            rule = cfg.chat_rule(r["title"], r["username"], r["id"], r["kind"] or "group")
            r.update(priority=rule["rank"], excluded=rule["excluded"], description=rule["note"], mode=rule["kind"],
                     explicit=rule["explicit"])
        return rows

    # ------------------------------------------------------------ обновление программы
    def update_raw(self) -> dict | None:
        """Настройки обновления (токен GitHub) — из config.json, если папка выбрана и данные открыты."""
        if not self.ws or vault.status() not in ("none", "open"):
            return None
        try:
            return self.raw()
        except Exception:  # noqa: BLE001
            return None

    def update_status(self) -> dict:
        from .. import updater
        raw = self.update_raw()
        s = updater.settings(raw)
        return {"local": updater.local_info(repo=s["repo"]), "last": updater.last_result(), "repo": s["repo"],
                "branch": s["branch"], "token_set": bool(s["token"]), "can_save_token": raw is not None,
                "running": bool(self.running("update"))}

    def save_update_token(self, body: dict) -> dict:
        if self.update_raw() is None:
            raise UserError("сначала выберите рабочую папку и откройте данные — токен хранится в её настройках")
        raw = self.raw()
        up = raw.setdefault("update", {})
        tok = str(body.get("token") or "").strip()
        if tok and not re.fullmatch(r"(github_pat_|ghp_|gho_)?[A-Za-z0-9_]{20,255}", tok):
            raise UserError("это не похоже на токен GitHub (github_pat_… или ghp_…)")
        if tok or body.get("clear"):
            up["token"] = tok
        for k in ("repo", "branch"):
            v = str(body.get(k) or "").strip()
            if v:
                up[k] = v
        self.save(raw)
        logs.add_secrets([tok])
        return self.update_status()

    def restart(self) -> dict:
        from .. import updater
        from ..vpn import xray as _xray
        if self.running("update"):
            raise UserError("дождитесь окончания установки обновления")
        if self.sup is not None:  # сторож не поднимает сервер сам — перезапуск ведёт помощник обновления
            self.sup.send("update")
        updater.schedule_restart(self.port, self.token,
                                 pid=int(os.environ.get("OPSDIGEST_SUPERVISOR_PID") or 0) if self.sup else 0)
        log.info("перезапуск по кнопке")
        _xray.stop_all()
        self.full_lock()
        threading.Thread(target=lambda: (time.sleep(0.4), self.server and self.server.shutdown()), daemon=True).start()
        return {"restarting": True}

    # ------------------------------------------------------------ Telegram
    def tg_source(self):
        from ..sources.telegram import TelegramSource
        return TelegramSource(self.cfg(), self.store(), progress=lambda s: None)

    def close_login(self) -> None:
        if self.login_src is not None:
            try:
                self.run_async(self.login_src.close(), 30)
            except Exception:  # noqa: BLE001
                pass
            self.login_src = None

    def send_code(self, phone: str) -> dict:
        phone = re.sub(r"[^\d+]", "", phone or "")
        if not phone:
            raise UserError("укажите номер телефона в международном формате, например +79991234567")
        raw = self.raw()
        raw.setdefault("telegram", {})["phone"] = phone
        self.save(raw)
        with self.tg_take():
            self.close_login()
            src = self.tg_source()
            self.run_async(src.connect(require_auth=False), 300)
            status = self.run_async(src.send_code(phone), 120)
            if status == "authorized":
                self.run_async(src.close(), 30)
            else:
                self.login_src = src
        return {"status": status, "via": src.proxy.describe() if src.proxy else ""}

    def sign_in(self, code: str, password: str) -> dict:
        with self.tg_take(timeout=5):
            if self.login_src is None:
                raise UserError("сначала нажмите «Получить код»")
            status = self.run_async(self.login_src.sign_in(code=code or None, password=password or None), 120)
            me = None
            if status == "ok":
                me = json.loads(self.login_src.store.kv_get("me") or "{}")
                self.close_login()
        if status == "ok":
            self._tg_ok = None    # условие «только после входа в Telegram» — проверить заново
            self.live.ensure()  # вошли — мессенджер подключается сам
        return {"status": status, "me": me}

    def load_chats(self) -> list[dict]:
        with self.tg_take():
            self.close_login()
            src = self.tg_source()

            async def go():
                await src.connect()
                try:
                    return await src.with_failover(src.list_dialogs)
                finally:
                    await src.close()
            rows = self.run_async(go(), 600)
        from ..storage import Chat
        st = self.store()
        try:  # список чатов — в базу: раздел «Чаты» дальше работает и без Telegram
            for d in rows:
                prev = st.get_chat(d["id"])
                st.upsert_chat(Chat(chat_id=d["id"], title=d["title"], kind=d["kind"], username=d["username"],
                                    priority=d["priority"], mode=d["mode"], note=prev.note if prev else "",
                                    unread=d["unread"] or 0, muted=d["muted"]))
            from ..chatrules import apply_chat_rules
            apply_chat_rules(self.cfg(), st)
        finally:
            st.close()
        return rows


class UserError(Exception):
    """Понятная пользователю ошибка (показывается как есть)."""


class _Release:
    def __init__(self, lock):
        self.lock = lock

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.lock.release()


def export_places(ws: Path) -> list[str]:
    """Где искать экспорт Telegram Desktop без указания пути: рабочая папка и «Загрузки/Telegram Desktop»."""
    out = []
    for base in (ws, Path.home() / "Downloads" / "Telegram Desktop"):
        try:
            if base.is_dir() and any(base.rglob("result.json")):
                out.append(str(base))
        except OSError:
            continue
    return out


def _human_error(e: Exception) -> str:
    name = type(e).__name__
    hints = {
        "ApiIdInvalidError": "неверные api_id / api_hash — проверьте на my.telegram.org",
        "PhoneNumberInvalidError": "неверный номер телефона",
        "PhoneNumberBannedError": "этот номер заблокирован в Telegram",
        "FloodWaitError": "Telegram просит подождать перед повтором",
        "AuthKeyUnregisteredError": "сессия устарела — войдите заново (шаг «Вход»)",
        "TimeoutError": "превышено время ожидания (прокси или модель не отвечают)",
    }
    msg = str(e) or name
    return f"{hints[name]} ({msg})" if name in hints else msg


# ================================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "OpsDigest"

    def log_message(self, fmt, *args):  # без шума в консоли
        pass

    # ---- ответы
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    # ---- защита
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}")

    def _token_ok(self, query: dict) -> bool:
        t = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        if not t:  # картинки в отчёте (относительные ссылки) — по cookie, которую ставит открытие отчёта
            m = re.search(r"(?:^|;\s*)od_t=([\w-]+)", self.headers.get("Cookie") or "")
            t = m.group(1) if m else ""
        return secrets.compare_digest(t, self.app.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # ---- маршруты
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        if not self._host_ok():
            return self._err("bad host", 403)
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path
        if method == "GET" and path in ("/", "/index.html"):
            body = (UI_DIR / "index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                          "script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                                                          "media-src 'self' blob:"})
        if method == "GET" and path.startswith("/static/") and path[8:] in STATIC:
            return self._send(200, (UI_DIR / path[8:]).read_bytes(), STATIC[path[8:]])
        if not self._token_ok(q):
            return self._err("нет доступа (перезапустите OpsDigest)", 403)
        app = self.app
        try:
            why = app.gate(path) if app.ws else None
            if why:
                return self._json({"ok": False, "locked": why, "error": {
                    "full": "данные зашифрованы — введите пароль", "pin": "программа заблокирована — введите ПИН",
                    "telegram": "войдите в Telegram — без этого данные не открываются"}[why]}, 423)
            if path.startswith("/api/vault/") and method == "POST":
                res = app.vault_api(path, self._body())
                if res is None:
                    return self._err("не найдено", 404)
                return self._json({"ok": True, **res})
            if method == "GET" and path.startswith("/reports/"):
                return self._report(path[len("/reports/"):])
            if method == "GET" and path.startswith("/media-file/"):
                return self._media_file(path[len("/media-file/"):])
            if method == "GET" and path.startswith("/media/"):
                return self._media_path(path[len("/media/"):])
            if method == "GET" and path.startswith("/avatar/"):
                return self._avatar(path[len("/avatar/"):])
            if method == "POST" and path == "/api/tg/upload":
                return self._json({"ok": True, **self._upload(q)})
            if method == "GET" and path.startswith("/asst-file/"):
                return self._asst_file(path[len("/asst-file/"):], q)
            if method == "GET" and path.startswith("/ext-media/"):
                return self._ext_media(path[len("/ext-media/"):])
            if method == "POST" and path == "/api/asst/upload":  # вложение в беседу ассистента: тело — сам файл
                return self._json({"ok": True, **self._asst_upload(q)})
            if method == "POST" and path == "/api/voice/recognize":  # голосовой ввод: тело — PCM 16 кГц, 16 бит
                return self._json({"ok": True, **self._voice_recognize(q)})
            body = self._body() if method == "POST" else {}
            res = self._api(method, path, body, q)
            if res is None:
                return self._err("не найдено", 404)
            self._json({"ok": True, **res} if isinstance(res, dict) else {"ok": True, "data": res})
        except UserError as e:
            log.info("%s: отказ: %s", path, e)
            self._err(str(e))
        except LiveError as e:
            log.info("%s: Telegram: %s", path, str(e)[:120])
            self._err(str(e))
        except vault.Locked as e:
            self._json({"ok": False, "locked": "full", "error": str(e)}, 423)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: ошибка %s\n%s", path, logs.err_info(e), logs.frames(e))
            self._err(_human_error(e) + f" [{type(e).__name__}]", 500)
        finally:
            app.last_ping = time.time()

    def _report(self, name: str) -> None:
        name = urllib.parse.unquote(name)
        ws = self.app.need_ws()
        f = ws / "reports" / name
        if not REPORT_RE.match(name) or not f.is_file():
            return self._err("нет такого отчёта", 404)
        ctype = {"html": "text/html", "md": "text/markdown", "json": "application/json"}[name.rsplit(".", 1)[1]]
        self._send(200, sfiles.read_bytes(f), ctype + "; charset=utf-8",
                   {"Set-Cookie": f"od_t={self.app.token}; Path=/; HttpOnly; SameSite=Strict"})

    def _file(self, f: Path, root: Path) -> None:
        try:
            f = f.resolve()
            f.relative_to(root.resolve())
        except (ValueError, OSError):
            return self._err("нет такого файла", 404)
        if not f.is_file():
            return self._err("нет такого файла", 404)
        ext = f.suffix.lower().lstrip(".")
        ctype = CTYPES.get(ext, "application/octet-stream")
        extra = {"Cache-Control": "no-store"}  # расшифрованное не должно оседать в кеше браузера на диске
        if not ctype.startswith(("image/", "text/", "audio/", "video/")):
            extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + urllib.parse.quote(f.name)
        self._send(200, sfiles.read_bytes(f), ctype, extra)

    def _media_file(self, mid: str) -> None:
        if not mid.isdigit():
            return self._err("нет такого файла", 404)
        app = self.app
        st = app.store()
        try:
            row = st.get_media(int(mid))
        finally:
            st.close()
        if not row or not row.get("path"):
            return self._err("файл не скачан", 404)
        return self._file(app.cfg().path(row["path"]), app.need_ws())

    def _asst_file(self, fid: str, q: dict) -> None:
        """Файл беседы ассистента (вложение или файл из ответа Maverick); ?dl=1 — скачать."""
        from .. import assistant as A
        if not fid.isdigit():
            return self._err("нет такого файла", 404)
        app = self.app
        st = app.store()
        try:
            A.ensure(st)
            r = st.db.execute("SELECT name, mime, path FROM asst_files WHERE id=?", (int(fid),)).fetchone()
        finally:
            st.close()
        if not r:
            return self._err("нет такого файла", 404)
        data = sfiles.read_bytes(app.cfg().path(r["path"]))
        extra = {}
        if (q.get("dl") or [""])[0] == "1" or not str(r["mime"] or "").startswith("image/"):
            extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + urllib.parse.quote(r["name"] or "file")
        self._send(200, data, r["mime"] or "application/octet-stream", extra)

    def _ext_media(self, mid: str) -> None:
        """Изображение из ответа плагина (вики, почта, трекер…) — скачано и сохранено программой."""
        from .. import plugins
        if not mid.isdigit():
            return self._err("нет такого файла", 404)
        st = self.app.store()
        try:
            r = plugins.ext_media_file(st, int(mid))
        finally:
            st.close()
        if not r or not r.get("path"):
            return self._err("нет такого файла", 404)
        self._send(200, sfiles.read_bytes(self.app.cfg().path(r["path"])), r.get("mime") or "image/png")

    def _raw_body(self, limit: int) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise UserError("пустой файл")
        if n > limit:
            raise UserError(f"файл больше {limit // 2**20} МБ")
        return self.rfile.read(n)

    def _asst_upload(self, q: dict) -> dict:
        from .. import assistant as A
        data = self._raw_body(64 * 2**20)
        qv = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        tid = int(qv("thread")) if qv("thread").isdigit() else None
        st = self.app.store()
        try:
            f = A.file_save(self.app.cfg(), st, tid, (qv("name") or "файл")[:200], qv("mime") or "", data)
        finally:
            st.close()
        return {"file": f}

    def _voice_recognize(self, q: dict) -> dict:
        from .. import voice
        data = self._raw_body(40 * 2**20)
        rate = int((q.get("rate") or ["16000"])[0] or 16000)
        try:
            return voice.recognize(self.app.voice_settings(), data, rate, glossary=self._voice_glossary())
        except voice.VoiceError as e:
            raise UserError(str(e)) from None

    def _voice_glossary(self):
        """Замены «как слышится → как пишется» из глоссария (синонимы кириллицей у латинских терминов)."""
        from .. import voice
        if not self.app.ws:
            return []
        try:
            st = self.app.store()
        except Exception:  # noqa: BLE001 — нет базы (данные закрыты): без глоссария
            return []
        try:
            return voice.glossary_replacements(st)
        finally:
            st.close()

    def _avatar(self, peer: str) -> None:
        """Аватарка чата/человека: скачанная — сразу; нет или фото сменилось — через мессенджер, если он на связи."""
        from .. import avatars
        if not peer.lstrip("-").isdigit():
            return self._err("нет такой аватарки", 404)
        app = self.app
        cfg = app.cfg()
        pid = int(peer)
        st = app.store()
        try:
            p = avatars.path_of(cfg, st, pid)
            need = avatars.needs_fetch(st, pid)
        finally:
            st.close()
        live = getattr(app, "live", None)
        if need and live is not None and getattr(live, "state", "") == "online":
            try:
                got = live.call(live.avatar(pid), 30)
                p = Path(got) if got else p
            except Exception as e:  # noqa: BLE001 — нет фото, нет прав, нет связи
                log.info("аватарка не получена: %s", type(e).__name__)
        if p is None:
            return self._err("нет аватарки", 404)
        return self._file(p, app.need_ws())

    def _media_path(self, rel: str) -> None:
        from ..media import media_root
        cfg = self.app.cfg()
        root = media_root(cfg)
        return self._file(root / urllib.parse.unquote(rel), root)

    def _api(self, method: str, path: str, b: dict, q: dict):  # noqa: C901 — просто таблица маршрутов
        app = self.app
        if path.startswith("/api/rpa/"):
            from .. import rpa
            ws = app.need_ws()
            try:
                if path == "/api/rpa/explorer" and method == "POST":
                    from ..rpa_explorer import request
                    return request(ws, str(b.get("action", "scan")),
                                   str(b.get("url", "")), str(b.get("channel", "msedge")))
                if path == "/api/rpa/context" and method == "POST":
                    job = app.start_job("rpa_analysis", lambda j: rpa.summarize(
                        ws, str(b.get("run", "")), app.cfg(), j))
                    return {"job": job.id}
                if path == "/api/rpa/templates" and method == "GET":
                    return {"templates": rpa.templates(ws), "fields": rpa.FIELDS, "statuses": rpa.STATUSES}
                if path == "/api/rpa/templates" and method == "POST":
                    rpa.save_template(ws, b.get("name", ""), b.get("items"))
                    return {"saved": True}
                if path == "/api/rpa/run" and method == "POST":
                    items = rpa.queue(b.get("items"))
                    job = app.start_job("rpa", lambda j: rpa.run(ws, items, str(b.get("url", "")),
                                                               str(b.get("channel", "chrome")), j))
                    return {"job": job.id}
            except ValueError as e:
                raise UserError(str(e)) from e
        if path == "/api/ping":
            return {"busy": app.busy(), "locked": vault.status() if vault.status() in ("full", "pin") else None,
                    "boot": app.boot, "supervised": app.sup is not None,
                    "tasks": app.tasks.count_active() if app.tasks.loaded_for is not None else 0,
                    "jobs": app.short_running()}
        if path == "/api/state":
            return app.state()
        if path == "/api/shutdown" and method == "POST":
            from ..vpn import xray as _xray
            if app.sup is not None:
                app.sup.send("quit")   # «Выход»: сторож не поднимает сервер снова
            _xray.stop_all()
            app.full_lock()
            threading.Thread(target=lambda: (time.sleep(0.3), app.server and app.server.shutdown()), daemon=True).start()
            return {"bye": True}
        # --- рабочая папка
        if path == "/api/fs/list":
            try:
                return wsmod.list_dirs((q.get("path") or [""])[0])
            except ValueError as e:
                raise UserError(str(e)) from None
        if path == "/api/fs/mkdir" and method == "POST":
            try:
                return {"path": wsmod.make_dir(b.get("path") or "", b.get("name") or "")}
            except ValueError as e:
                raise UserError(str(e)) from None
        if path == "/api/workspace/set" and method == "POST":
            p = (b.get("path") or "").strip().strip('"')
            if not p:
                raise UserError("укажите папку")
            ws = wsmod.init_workspace(Path(os.path.expandvars(p)))
            app.live.stop()
            app.close_login()
            vault.bind(ws)
            app.ws = ws
            wsmod.save_app_settings({"workspace": str(ws)})
            return app.state()
        if path == "/api/open_folder" and method == "POST":
            if b.get("what") == "logs":
                target = logs.log_dir()
            elif b.get("what") == "media":
                from ..media import media_root
                target = media_root(app.cfg())
                target.mkdir(parents=True, exist_ok=True)
            else:
                target = app.need_ws() / ("reports" if b.get("what") == "reports" else "")
            if hasattr(os, "startfile"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            return {"path": str(target)}
        # --- настройки
        if path == "/api/config":
            return app.config_view()
        if path == "/api/config/llm" and method == "POST":
            return app.save_llm(b)
        if path == "/api/config/telegram" and method == "POST":
            return app.save_telegram(b)
        if path == "/api/config/profile" and method == "POST":
            return app.save_profile(b)
        if path == "/api/config/chats" and method == "POST":
            return app.save_chats(b)
        if path == "/api/config/media" and method == "POST":
            return app.save_media(b)
        if path == "/api/config/privacy" and method == "POST":
            return app.save_privacy(b)
        # --- глоссарий и база знаний (ui/glossary_api.py)
        if path == "/api/ui":  # разделы окна (рубильники) и тема — app.json, без секретов: нужны и до входа
            if method == "POST":
                cur = wsmod.load_app_settings().get("ui") or {}
                if isinstance(b.get("modules"), dict):
                    cur["modules"] = {k: bool(v) for k, v in b["modules"].items() if k in UI_MODULES}
                if b.get("theme") in ("auto", "light", "dark"):
                    cur["theme"] = b["theme"]
                if b.get("tab") in UI_MODULES + ("set",):
                    cur["tab"] = b["tab"]
                wsmod.save_app_settings({"ui": cur})
            return ui_settings()
        if path.startswith("/api/digest/presets") or path.startswith("/api/schedules"):
            from .schedule_api import route as sched_route
            r = sched_route(self, method, path, b, q)
            if r is not None:
                return r
        if path.startswith("/api/plugins") or path == "/api/config/sources":
            from .sources_api import route as sources_route
            r = sources_route(self, method, path, b, q)
            if r is not None:
                return r
        if path == "/api/creds" or path.startswith("/api/creds/"):  # «Учётные данные» (secure/creds.py)
            from .creds_api import route as creds_route
            r = creds_route(self, method, path, b, q)
            if r is not None:
                return r
        if path.startswith("/api/asst/"):
            from .assistant_api import route as asst_route
            r = asst_route(self, method, path, b, q)
            if r is not None:
                return r
        if path.startswith("/api/skill") or path.startswith("/api/feedback"):  # навык: правила, ссылки, отзывы
            from .skill_api import route as skill_route
            r = skill_route(self, method, path, b, q)
            if r is not None:
                return r
        if path.startswith("/api/voice/"):  # голосовой ввод: установка модели и проверка
            from .voice_api import route as voice_route
            r = voice_route(self, method, path, b, q)
            if r is not None:
                return r
        if path.startswith("/api/glossary"):
            from . import glossary_api
            try:
                return glossary_api.route(self, method, path, b, q)
            except ValueError as e:
                raise UserError(str(e)) from None
        # --- чаты из базы: папки, фильтры, темы, люди, шаблоны, ИИ-описания (ui/chats_api.py)
        r = self._chats_api(method, path, b, q)
        if r is not None:
            return r
        if path == "/api/export" and method == "POST":
            return {"job": self._export_job(b).id}
        if path == "/api/chat/images" and method == "POST":
            return {"job": self._images_job(b).id}
        if path == "/api/media/fetch" and method == "POST":
            return {"job": self._fetch_job(b).id}
        if path == "/api/vision/test" and method == "POST":
            return {"job": self._vision_job(b).id}
        if path.startswith("/api/jobs/") and path.endswith("/stop") and method == "POST":
            job = app.jobs.get(path.split("/")[3])
            if not job:
                raise UserError("задача не найдена — возможно, уже закончилась")
            return app.stop_job(job)
        if path.startswith("/api/jobs/") and path.endswith("/cancel") and method == "POST":
            job = app.jobs.get(path.split("/")[3])
            if not job:
                raise UserError("задача не найдена")
            if job.status == "running":
                job.cancel(by_user=True)   # долгая задача — на паузу (продолжить — в окне «Задачи»)
            return {"cancelled": True}
        if path.startswith("/api/tasks"):
            r = app.tasks_api(method, path, b)
            if r is not None:
                return r
        # --- модель
        if path == "/api/llm/models" and method == "POST":
            base = (b.get("base_url") or "").rstrip("/")
            req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {b.get('api_key') or 'x'}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                raise UserError(f"сервер модели не отвечает на {base}: {e}") from None
            ids = [m.get("id") for m in data.get("data") or [] if m.get("id")]
            return {"models": [i for i in ids if not re.search(r"(?i)embed|whisper", i)]}
        if path == "/api/llm/test" and method == "POST":
            return self._llm_test()
        # --- Telegram
        if path == "/api/telegram/proxies" and method == "POST":
            lock = app.tg_take()

            def check(job: Job):
                with lock:
                    app.close_login()
                    src = app.tg_source()
                    res = app.run_async(src.check_proxies(), 900)
                job.add("Проверено: " + ", ".join(s.describe() for s, *_ in res))
                for n in src.notes:
                    job.add(n)
                rows = [{"proxy": s.describe(), "ok": ok, "error": err, "sec": round(sec, 1)} for s, ok, err, sec in res]
                for r in rows:
                    job.add(f"{'✓' if r['ok'] else '✗'} {r['proxy']} — {r['sec']} с {r['error']}")
                return {"rows": rows, "any_ok": any(r["ok"] for r in rows)}
            return {"job": app.start_job("proxies", check).id}
        # --- загрузка экспорта Telegram Desktop
        if path == "/api/import" and method == "POST":
            def imp(job: Job):
                from ..importer import ImportError_, import_path
                paths = [b["path"]] if (b.get("path") or "").strip() else export_places(app.need_ws())
                if not paths:
                    raise UserError("экспорт не найден: положите папку экспорта (с result.json) в рабочую папку "
                                    "или укажите путь к ней")
                store = app.store()
                try:
                    total: dict[str, int] = {}
                    for p in paths:
                        job.add(f"Загружаю экспорт: {Path(p).name}")
                        try:
                            st = import_path(app.cfg(), store, p, job.add)
                        except ImportError_ as e:
                            raise UserError(str(e)) from None
                        for k, v in st.items():
                            total[k] = total.get(k, 0) + v
                    return total
                finally:
                    store.close()
            return {"job": app.start_job("import", imp).id}
        # --- VPN по подписке (Xray)
        if path == "/api/vpn" and method == "GET":
            return app.vpn_view()
        if path == "/api/vpn/save" and method == "POST":
            return app.save_vpn(b)
        if path == "/api/vpn/install" and method == "POST":
            def install(job: Job):
                from ..vpn import VpnError, xray
                try:
                    v = (xray.install_from_zip(b["zip"], job.add) if (b.get("zip") or "").strip()
                         else xray.install_from_github(job.add))
                except VpnError as e:
                    raise UserError(str(e)) from None
                job.add(f"✓ Xray {v} установлен и запускается.")
                return {"version": v}
            return {"job": app.start_job("vpn-install", install).id}
        if path == "/api/vpn/test" and method == "POST":
            app.save_vpn(b)

            def test(job: Job):
                from .. import vpn
                try:
                    return vpn.refresh(app.cfg(), job.add)
                except vpn.VpnError as e:
                    raise UserError(str(e)) from None
            return {"job": app.start_job("vpn-test", test).id}
        if path == "/api/vpn/stop" and method == "POST":
            from ..vpn import xray
            xray.stop_all()
            return {}
        if path == "/api/telegram/send_code" and method == "POST":
            return app.send_code(b.get("phone") or "")
        if path == "/api/telegram/sign_in" and method == "POST":
            return app.sign_in(b.get("code") or "", b.get("password") or "")
        if path == "/api/chats/load" and method == "POST":
            return {"job": app.start_job("chats", lambda job: {"chats": app.load_chats()}).id}
        # --- сводка, вопросы, отчёты
        if path == "/api/digest" and method == "POST":
            return {"job": self._digest_job(b).id}
        if path == "/api/digest/preview" and method == "POST":   # окно «Проверьте перед запуском»
            return self._digest_preview(b)
        if path == "/api/digest/chats" and method == "POST":     # выбор чатов: сколько сообщений за период
            return self._digest_chats(b)
        if path == "/api/demo" and method == "POST":
            if app.busy():
                raise app.busy_error()

            def demo(job: Job):
                from ..demo import load_demo
                from ..digest import DigestBuilder, get_tz
                from ..llm import make_client
                from ..service import write_report
                from ..storage import Store
                cfg = app.cfg()
                st = Store(":memory:")
                since, until = load_demo(cfg, st, get_tz(cfg.digest.get("timezone")))
                job.add("Выдуманная переписка: 6 чатов, 30 сообщений (авария, алерты, разработка, команда).")
                data = DigestBuilder(cfg, st, make_client(*cfg.llm_profile()), progress=job.add).build(since, until, "demo")
                paths = write_report(cfg, data, None)
                job.add("Отчёт готов.")
                ov = data["overview"]
                return {"paths": paths, "report": Path(paths["html"]).name, "headline": ov.get("headline", ""),
                        "top": len(ov.get("top") or []), "chats": data["meta"]["chats"],
                        "messages": data["meta"]["messages"], "errors": data["meta"]["errors"]}
            return {"job": app.start_job("demo", demo).id}
        if path == "/api/ask" and method == "POST":
            question = (b.get("question") or "").strip()
            if not question:
                raise UserError("введите вопрос")
            if app.busy():
                raise app.busy_error()

            def ask(job: Job):
                from ..service import run_ask
                sync = bool(b.get("sync", True))
                if sync and app.tg_busy():
                    sync = False
                    job.add("Идёт выгрузка истории — отвечаю по тому, что уже в базе.")
                if not sync:
                    return run_ask(app.cfg(), question, days=int(b.get("days") or 14), sync=False,
                                   progress=job.add, run_async=lambda c: app.run_async(c))
                with app.tg_lock:
                    app.close_login()
                    return run_ask(app.cfg(), question, days=int(b.get("days") or 14), sync=True,
                                   progress=job.add, run_async=lambda c: app.run_async(c))
            return {"job": app.start_job("ask", ask).id}
        if path == "/api/jobs/stop_all" and method == "POST":   # «Остановить всё» в окне «Задачи»
            lines: list[str] = []
            res = app.preempt(None, "остановлено вами", lines.append, resume=False)
            return {"log": lines, **{k: v for k, v in res.items() if k != "sessions"},
                    "sessions": res["sessions"]["closed"], "sessions_total": res["sessions"]["total"]}
        if path.startswith("/api/jobs/"):
            job = app.jobs.get(path.rsplit("/", 1)[1])
            if not job:
                raise UserError("задача не найдена")
            return job.view(int((q.get("since") or ["0"])[0]))
        if path == "/api/reports":
            return {"reports": self._reports()}
        if path == "/api/reports/export" and method == "POST":
            from .export import export_report
            try:
                out = export_report(app.need_ws(), str(b.get("name") or ""), app.cfg())
            except ValueError as e:
                raise UserError(str(e)) from None
            if b.get("reveal", True) and hasattr(os, "startfile"):
                import subprocess
                subprocess.Popen(["explorer", "/select,", out["html"]])
            return out
        # --- мессенджер и ИИ в чате
        if path.startswith("/api/tg/"):
            return self._tg_api(method, path, b, q)
        if path.startswith("/api/ai/"):
            return self._ai_api(method, path, b, q)
        # --- обновление программы из GitHub
        if path == "/api/update/status":
            return app.update_status()
        if path == "/api/update/check" and method == "POST":
            from .. import updater
            return {"check": updater.check(app.update_raw())}
        if path == "/api/update/zips":
            from .. import updater
            return {"zips": updater.find_zips()}
        if path == "/api/update/token" and method == "POST":
            return app.save_update_token(b)
        if path == "/api/update/install" and method == "POST":
            if app.running("update"):
                raise UserError("обновление уже устанавливается")
            raw = app.update_raw()

            def upd(job: Job):
                from .. import updater
                try:
                    return updater.install(raw, zip_path=(b.get("zip") or "").strip() or None, progress=job.add)
                except updater.UpdateError as e:
                    raise UserError(str(e)) from None
            return {"job": app.start_job("update", upd).id}
        if path == "/api/update/restart" and method == "POST":
            return app.restart()
        if path == "/api/privacy/recent":
            from .. import privacy
            return {"items": privacy.recent()}
        if path == "/api/diag" and method == "POST":
            def diag(job: Job):
                from ..diag import run_checks
                raw = app.raw() if app.ws and vault.status() in ("none", "open") else None
                checks = run_checks(app.ws, raw)
                log.info("самопроверка: ok %d, предупреждений %d, ошибок %d",
                         *(sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail")))
                return {"checks": [{**c, "detail": logs.scrub(c["detail"])} for c in checks]}
            return {"job": app.start_job("diag", diag).id}
        if path == "/api/report" and method == "POST":
            def report(job: Job):
                from .. import report as rep
                raw = app.raw() if app.ws and vault.status() in ("none", "open") else None
                text = rep.build(ws=app.ws, raw=raw)
                p = rep.save(text)
                if b.get("reveal", True):
                    rep.reveal(p)
                return {"path": str(p), "name": p.name, "text": text}
            return {"job": app.start_job("report", report).id}
        if path == "/api/schedule" and method == "POST":
            if b.get("enabled"):
                ok, msg = wsmod.schedule_enable(app.need_ws(), b.get("at") or "08:40", bool(b.get("weekdays_only", True)))
            else:
                ok, msg = wsmod.schedule_disable()
            if not ok:
                raise UserError(msg or "не получилось изменить расписание")
            raw = app.raw()
            raw.setdefault("ui", {})["schedule"] = {"at": b.get("at") or "08:40",
                                                    "weekdays_only": bool(b.get("weekdays_only", True))}
            app.save(raw)
            return {"message": msg, "schedule": wsmod.schedule_status()}
        return None

    # ------------------------------------------------------------ раздел «Чаты»
    def _chats_api(self, method: str, path: str, b: dict, q: dict):  # noqa: C901 — таблица маршрутов
        from . import chats_api as C
        app = self.app
        qv = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
        num = lambda x: int(x) if str(x or "").lstrip("-").isdigit() else None  # noqa: E731
        try:
            if path == "/api/chats/list":
                j = C.chats_view(app)
                j["stats"] = self._media_stats()
                return j
            if path == "/api/chats/manage" and method == "POST":
                import re
                from ..messenger.chat_manage import execute
                action, ids = b.get("action"), b.get("ids")
                if action not in ("leave", "invite") or not isinstance(ids, list) or not 1 <= len(ids) <= 500:
                    raise UserError("Выберите от 1 до 500 чатов")
                if any(isinstance(x, bool) or not str(x).lstrip("-").isdigit() for x in ids):
                    raise UserError("Некорректный номер чата")
                ids = list(dict.fromkeys(int(x) for x in ids))
                username = str(b.get("username") or "").strip().lstrip("@")
                if action == "invite" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
                    raise UserError("Введите ник Telegram: @username")
                if app.live.state != "online":
                    raise UserError("Подключите мессенджер к Telegram")
                if app.running("chat-manage"):
                    raise UserError("Дождитесь завершения операции с чатами")
                return {"job": app.start_job("chat-manage", lambda job:
                    app.live.call(execute(app.live, ids, action, username), 7200)).id}
            if path == "/api/chats/bulk" and method == "POST":
                return C.bulk(app, b)
            if path == "/api/chats/rule" and method == "POST":
                return C.name_rule(app, b)
            if path == "/api/chat/save" and method == "POST":
                return C.save_chat(app, b)
            if path == "/api/chat/topics":
                if method == "POST":
                    return C.save_topics(app, b)
                cid = num(qv("chat"))
                if cid is None:
                    raise UserError("не указан чат")
                return C.topics_view(app, cid, qv("live") == "1")
            if path == "/api/chat/members":
                cid = num(qv("chat"))
                if cid is None:
                    raise UserError("не указан чат")
                return C.members_view(app, cid, qv("live") == "1")
            if path == "/api/people":
                return C.people_view(app, str(qv("q") or ""))
            if path == "/api/people/bulk" and method == "POST":
                return C.bulk_people(app, b)
            if path == "/api/person":
                if method == "POST":
                    return C.save_person(app, b)
                uid = num(qv("id"))
                if uid is None:
                    raise UserError("не указан человек")
                return C.person_view(app, uid, num(qv("chat")))
            if path == "/api/templates":
                return C.save_template(app, b) if method == "POST" else C.templates_view(app, qv("kind"))
            if path == "/api/describe" and method == "POST":
                if app.running("describe"):
                    raise UserError("ИИ уже описывает — дождитесь окончания или остановите")

                def go(job: Job):
                    try:
                        return C.describe_job(app, b, job)
                    except C.ApiError as e:
                        raise UserError(str(e)) from None
                return {"job": app.start_job("describe", go, task=b).id}
        except C.ApiError as e:
            raise UserError(str(e)) from None
        except LiveError as e:
            raise UserError(str(e)) from None
        return None

    # ------------------------------------------------------------ мессенджер
    @staticmethod
    def _int(x, what: str = "чат") -> int:
        try:
            return int(str(x).strip())
        except (TypeError, ValueError):
            raise UserError(f"не указан {what}") from None

    def _tg_api(self, method: str, path: str, b: dict, q: dict):  # noqa: C901 — таблица маршрутов
        app, live = self.app, self.app.live
        qv = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
        if path == "/api/tg/state":
            live.ensure()
            return live.status()
        if path == "/api/tg/connect" and method == "POST":
            live.reconnect()
            return live.status()
        if path == "/api/tg/events":
            live.ensure()
            evs, seq = live.wait(int(qv("since", "0") or 0), min(float(qv("wait", "25") or 25), 30))
            return {"events": evs[-500:], "seq": seq, "state": live.state}
        if path == "/api/tg/dialogs":
            st = app.store()
            try:
                return {"chats": mviews.chat_views(st), "folders": json.loads(st.kv_get("tg_folders") or "[]"),
                        "status": live.status()}
            finally:
                st.close()
        if path == "/api/tg/history":
            chat = self._int(qv("chat"))
            before, around, after, topic = (int(qv(k)) if (qv(k) or "").lstrip("-").isdigit() else None
                                            for k in ("before", "around", "after", "topic"))
            limit = max(10, min(int(qv("limit", "40") or 40), 100))
            use_live = qv("live") == "1" and live.state == "online" and after is None
            more = None
            if use_live:
                _, n = live.call(live.history(chat, before=before, around=around, limit=limit, topic=topic), 90)
                more = n >= limit
            st = app.store()
            try:
                if around:
                    rows = st.page(chat, before=around + 1, limit=limit // 2 + 1, topic=topic) + \
                        st.page(chat, after=around, limit=limit // 2, topic=topic)
                elif after is not None:
                    rows = st.page(chat, after=after, limit=limit, topic=topic)
                else:
                    rows = st.page(chat, before=before, limit=limit, topic=topic)
                if more is None:
                    more = len(rows) >= limit or (live.state == "online" and not st.history_done(chat))
                return {"messages": mviews.msg_views(st, chat, rows), "more": more, "live": use_live,
                        "chat": mviews.one_chat(st, chat)}
            finally:
                st.close()
        if path == "/api/tg/topics":
            from . import chats_api
            return chats_api.topics_view(app, self._int(qv("chat")), qv("live") == "1")
        if path == "/api/tg/pinned":
            chat = self._int(qv("chat"))
            topic = int(qv("topic")) if (qv("topic") or "").isdigit() else None
            ids: list = []
            if live.state == "online":
                try:
                    ids = live.call(live.pinned(chat, topic), 60)
                except Exception as e:  # noqa: BLE001 — закреп необязателен: берём из базы
                    log.info("закреп: %s", type(e).__name__)
                    ids = []
            st = app.store()
            try:
                if not ids:
                    t, ta = st.topic_sql(topic)
                    ids = [r[0] for r in st.db.execute(
                        f"""SELECT msg_id FROM messages WHERE chat_id=? AND meta LIKE '%"pin":1%'{t}
                            ORDER BY msg_id DESC LIMIT 30""", (chat, *ta)).fetchall()]
                views = {v["id"]: v for v in mviews.msg_views(st, chat, st.rows_by_ids(chat, ids))}
                return {"pinned": [views[i] for i in ids if i in views]}
            finally:
                st.close()
        if path == "/api/tg/marks":
            chat = self._int(qv("chat"))
            topic = int(qv("topic")) if (qv("topic") or "").isdigit() else None
            if live.state != "online":
                return {"mentions": [], "reactions": []}
            try:
                return live.call(live.unread_marks(chat, topic), 60)
            except Exception as e:  # noqa: BLE001
                log.info("отметки: %s", type(e).__name__)
                return {"mentions": [], "reactions": []}
        if path == "/api/tg/media":
            chat = self._int(qv("chat"))
            kind = qv("kind") or "media"
            before = int(qv("before")) if (qv("before") or "").isdigit() else None
            ids: list = []
            if live.state == "online":
                try:
                    ids = live.call(live.media_tab(chat, kind, 60, before), 90)
                except Exception as e:  # noqa: BLE001 — вкладки показываем по базе
                    log.info("медиа чата: %s", type(e).__name__)
                    ids = []
            st = app.store()
            try:
                kinds = {"media": ("photo", "image", "video", "gif"), "files": ("document",),
                         "voice": ("voice", "video_note"), "music": ("audio",)}.get(kind)
                if kinds:
                    ids += [r[0] for r in st.db.execute(
                        f"""SELECT msg_id FROM media WHERE chat_id=? AND kind IN ({",".join("?" * len(kinds))})
                            {"AND msg_id<?" if before else ""} ORDER BY msg_id DESC LIMIT 120""",
                        (chat, *kinds, *([before] if before else []))).fetchall()]
                elif kind == "links":
                    ids += [r[0] for r in st.db.execute(
                        f"""SELECT msg_id FROM messages WHERE chat_id=? AND (text LIKE '%http://%' OR text LIKE '%https://%'
                            OR meta LIKE '%["a",%') {"AND msg_id<?" if before else ""} ORDER BY msg_id DESC LIMIT 120""",
                        (chat, *([before] if before else []))).fetchall()]
                ids = sorted(set(ids), reverse=True)[:120]
                views = {v["id"]: v for v in mviews.msg_views(st, chat, st.rows_by_ids(chat, ids))}
                url = re.compile(r"https?://")
                keep = (lambda v: bool(v.get("media"))) if kind != "links" else \
                    (lambda v: bool(url.search(v.get("text") or "")) or any(e[0] == "a" for e in (v.get("m") or {}).get("e") or []))
                return {"items": [views[i] for i in ids if i in views and keep(views[i])]}
            finally:
                st.close()
        if path == "/api/tg/search":
            text = (qv("q") or "").strip()
            chat = int(qv("chat")) if (qv("chat") or "").lstrip("-").isdigit() else None
            frm = int(qv("from")) if (qv("from") or "").lstrip("-").isdigit() else None
            if not text and not (chat and frm):
                raise UserError("введите, что искать")
            pairs: list = []
            if live.state == "online" and frm and chat is not None:
                try:
                    pairs = [(chat, i) for i in live.call(live.search_from(chat, text, frm), 60)]
                except LiveError:
                    pairs = []
            elif live.state == "online":
                try:
                    pairs = live.call(live.search(text, chat), 60)
                except Exception as e:  # noqa: BLE001 — поиск в Telegram не удался: ищем по базе
                    log.info("поиск в Telegram: %s", type(e).__name__)
                    pairs = []
            from ..ask import fts_query
            st = app.store()
            try:
                fq = fts_query(text) or '"' + text.replace('"', "") + '"'
                hits = st.search(fq, limit=60, include_excluded=True, chat_id=chat) if text else []
                if frm:
                    hits = [(m, t) for m, t in hits if m.sender_id == frm]
                    if not text:
                        hits = [(st._msg(r), "") for r in st.db.execute(
                            "SELECT * FROM messages WHERE chat_id=? AND sender_id=? ORDER BY msg_id DESC LIMIT 60",
                            (chat, frm)).fetchall()]
                keys = list(dict.fromkeys([tuple(p) for p in pairs] + [(m.chat_id, m.msg_id) for m, _ in hits]))[:80]
                by: dict[int, list[int]] = {}
                for cid, mid in keys:
                    by.setdefault(cid, []).append(mid)
                found: dict = {}
                for cid, ids in by.items():
                    c = st.get_chat(cid)
                    for v in mviews.msg_views(st, cid, st.rows_by_ids(cid, ids)):
                        v["chat_title"] = c.title if c else str(cid)
                        found[(cid, v["id"])] = v
                res = [found[k] for k in keys if k in found]
                res.sort(key=lambda v: v["date"], reverse=True)
                return {"results": res}
            finally:
                st.close()
        if method != "POST":
            return None
        chat = self._int(b.get("chat")) if path not in ("/api/tg/download", "/api/tg/resolve", "/api/tg/file") else 0
        if path == "/api/tg/send":
            text = str(b.get("text") or "")
            if not text.strip():
                raise UserError("пустое сообщение")
            rt = self._int(b["reply_to"], "ответ") if b.get("reply_to") else None
            topic = self._int(b["topic"], "тема") if b.get("topic") else None
            when = None
            if b.get("schedule"):  # «отправить позже»: местное время окна → UTC
                try:
                    when = datetime.fromisoformat(str(b["schedule"]))
                    if when.tzinfo is None:
                        when = when.astimezone()
                except ValueError:
                    raise UserError("неверное время отправки") from None
                if when.timestamp() < time.time() + 30:
                    raise UserError("время отправки — в будущем (хотя бы через минуту)")
            mid = live.call(live.send_text(chat, text, rt, topic, bool(b.get("silent")), when), 120)
            return {"id": mid, "scheduled": bool(when)}
        if path == "/api/tg/mute":
            topic = self._int(b["topic"], "тема") if b.get("topic") else None
            live.call(live.mute(chat, bool(b.get("on")), topic), 60)
            return {}
        if path == "/api/tg/vote":
            live.call(live.vote(chat, self._int(b.get("id"), "сообщение"), [str(o) for o in b.get("options") or []]), 60)
            return {}
        if path == "/api/tg/read_topic":
            live.call(live.read_topic(chat, self._int(b.get("topic"), "тема"), b.get("max_id")), 60)
            return {}
        if path == "/api/tg/edit":
            text = str(b.get("text") or "")
            if not text.strip():
                raise UserError("пустое сообщение")
            live.call(live.edit(chat, self._int(b.get("id"), "сообщение"), text), 60)
            return {}
        if path == "/api/tg/delete":
            ids = [self._int(i, "сообщение") for i in b.get("ids") or []]
            if not ids:
                raise UserError("не выбраны сообщения")
            live.call(live.delete(chat, ids, bool(b.get("revoke", True))), 60)
            return {}
        if path == "/api/tg/forward":
            ids = [self._int(i, "сообщение") for i in b.get("ids") or []]
            to = self._int(b.get("to"), "чат, куда переслать")
            return {"ids": live.call(live.forward(chat, ids, to), 90)}
        if path == "/api/tg/react":
            live.call(live.react(chat, self._int(b.get("id"), "сообщение"), b.get("emoji") or None), 60)
            return {}
        if path == "/api/tg/read":
            mx = self._int(b["max_id"], "сообщение") if b.get("max_id") else None
            live.call(live.read(chat, mx), 60)
            return {}
        if path == "/api/tg/download":
            mid = self._int(str(b.get("media") or "").lstrip("m"), "файл")
            return {"media": live.call(live.download(mid), 900)}
        if path == "/api/tg/resolve":
            return self._resolve_link(str(b.get("link") or ""))
        if path == "/api/tg/file":
            return self._file_action(self._int(str(b.get("media") or "").lstrip("m"), "файл"), str(b.get("action") or ""))
        return None

    def _resolve_link(self, link: str) -> dict:
        """t.me/c/123/45, t.me/имя/45, t.me/c/123/7/45 (тема), tg://resolve?domain=…&post=…, tg://privatepost → чат."""
        live = self.app.live
        u = urllib.parse.urlparse(link.strip())
        q = urllib.parse.parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        chat = msg = topic = None
        name = None
        if u.scheme == "tg":
            if u.netloc == "privatepost" or (u.path.strip("/") == "privatepost"):
                c = (q.get("channel") or [""])[0]
                chat = -1000000000000 - int(c) if c.isdigit() else None
            else:
                name = (q.get("domain") or [None])[0]
            p = (q.get("post") or [""])[0]
            msg = int(p) if p.isdigit() else None
            t = (q.get("thread") or q.get("topic") or [""])[0]
            topic = int(t) if t.isdigit() else None
        elif parts:
            if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
                chat = -1000000000000 - int(parts[1])
                nums = [int(x) for x in parts[2:] if x.isdigit()]
            elif parts[0] in ("joinchat", "addstickers", "share", "proxy", "socks") or parts[0].startswith("+"):
                return {"external": True}
            else:
                name = parts[0].lstrip("@")
                nums = [int(x) for x in parts[1:] if x.isdigit()]
            if u.scheme != "tg":
                if len(nums) >= 2:
                    topic, msg = nums[0], nums[1]
                elif nums:
                    msg = nums[0]
            t = (q.get("thread") or q.get("topic") or [""])[0]
            if t.isdigit():
                topic = int(t)
            c = (q.get("comment") or [""])[0]
            if c.isdigit():
                return {"external": True}  # комментарии к постам канала — в Telegram
        st = self.app.store()
        try:
            if name and chat is None:
                r = st.db.execute("SELECT chat_id FROM chats WHERE lower(username)=lower(?)", (name,)).fetchone()
                chat = r[0] if r else None
            known = chat is not None and st.get_chat(chat) is not None
        finally:
            st.close()
        if name and chat is None and live.state == "online":
            try:
                r = live.call(live.resolve(name), 30)
                chat, known = r["chat"], r["known"]
            except LiveError:
                pass
        if chat is None or not known:
            return {"external": True, "chat": chat}
        return {"chat": chat, "msg": msg, "topic": topic}

    def _file_action(self, mid: int, action: str) -> dict:
        """Файл из переписки: open — открыть программой Windows, save — копия в «Загрузки\\OpsDigest»,
        folder — показать в папке. В защищённой папке файл расшифровывается в копию (в Загрузки или во временную)."""
        import tempfile
        app = self.app
        st = app.store()
        try:
            row = st.get_media(mid)
        finally:
            st.close()
        if not row or row.get("status") != "ok" or not row.get("path"):
            raise UserError("файл ещё не скачан — сначала скачайте его")
        src = app.cfg().path(row["path"])
        name = re.sub(r'[\\/:*?"<>|]+', "_", row.get("name") or src.name)[:150] or src.name
        data = sfiles.read_bytes(src)
        if action == "save" or action == "folder":
            out = Path.home() / "Downloads" / "OpsDigest"
        else:
            out = Path(tempfile.gettempdir()) / "OpsDigest-open"
        out.mkdir(parents=True, exist_ok=True)
        target = out / name
        if target.exists() and target.read_bytes() != data:
            target = out / f"{target.stem}-{mid}{target.suffix}"
        target.write_bytes(data)
        if hasattr(os, "startfile"):
            if action == "open":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif action == "folder":
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(target)])  # noqa: S603,S607
        return {"path": str(target)}

    def _upload(self, q: dict) -> dict:
        """Отправка файла: тело запроса — сам файл, параметры — в адресе."""
        app = self.app
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise UserError("пустой файл")
        if n > MAX_UPLOAD:
            raise UserError(f"файл больше {MAX_UPLOAD // 2**20} МБ — отправьте его из Telegram")
        data = self.rfile.read(n)
        qv = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        chat = self._int(qv("chat"))
        rt = int(qv("reply")) if qv("reply").isdigit() else None
        topic = int(qv("topic")) if qv("topic").isdigit() else None
        name = (qv("name") or "file")[:200]
        mid = app.live.call(app.live.send_file(chat, data, name, qv("caption")[:1024], rt, qv("doc") == "1", topic,
                                               qv("silent") == "1"), 900)
        return {"id": mid}

    def _ai_api(self, method: str, path: str, b: dict, q: dict):
        from ..llm import LLMError
        from ..messenger import ai
        app = self.app
        kind = path.rsplit("/", 1)[1]
        chat = b.get("chat") if method == "POST" else (q.get("chat") or [None])[0]
        chat = int(chat) if str(chat or "").lstrip("-").isdigit() else None
        if kind == "related":  # поиск по базе, без модели — сразу
            text = str(b.get("text") or "").strip()
            if not text:
                raise UserError("выделите текст или выберите сообщение")
            st = app.store()
            try:
                return ai.related(st, text, chat_id=chat, msg_id=b.get("id"),
                                  other_chats_only=bool(b.get("others", True)))
            finally:
                st.close()
        if method != "POST" or kind not in ("summary", "ask", "draft", "analyze", "maverick", "explain", "everywhere"):
            return None
        if chat is None and kind in ("summary", "ask", "draft", "explain"):
            raise UserError("не выбран чат")
        provider = str(b.get("provider") or "") or None
        topic = int(b["topic"]) if str(b.get("topic") or "").isdigit() else None
        ids = [int(i) for i in b.get("ids") or [] if str(i).isdigit()]
        text = str(b.get("text") or "")
        live = app.live
        sources = b.get("sources") if isinstance(b.get("sources"), dict) else None

        def default_sources(cfg) -> dict:
            from .. import plugins
            return {"telegram": True, **{k: plugins.enabled(cfg, k) for k in plugins.KINDS}}

        def logged(st, title: str, question: str, res: dict) -> dict:
            """Запрос ИИ в чате — в историю ассистента (её видно в разделе «Ассистент»)."""
            from .. import assistant as A
            tid = A.log_exchange(st, "chat", title, question, res.get("text") or "",
                                 {"refs": [r for r in res.get("refs") or [] if isinstance(r, dict)]
                                  or [{"chat": chat, "id": i} for i in res.get("refs") or [] if isinstance(i, int)],
                                  "model": res.get("model")},
                                 {"chat": chat, "ids": ids, "quote": text[:3000], "topic": topic})
            res["thread"] = tid
            return res

        def go(job: Job):
            from ..digest import get_tz
            cfg = app.cfg()
            st = app.store()
            try:
                c0 = st.get_chat(chat) if chat is not None else None
                ctitle = c0.title if c0 else ""
                if kind == "everywhere":  # выделенное → все чаты (включая этот) и плагины Maverick
                    from .. import assistant as A
                    if not text.strip():
                        raise UserError("выделите текст или выберите сообщение")
                    return A.everywhere(cfg, st, text, chat_id=chat, msg_id=ids[0] if ids else None,
                                        sources=sources or default_sources(cfg), progress=job.add)
                if kind == "ask" and (ids or text.strip()) and b.get("everywhere", True) is not False:
                    # вопрос по выделенному: поиск по всем чатам и источникам — это беседа ассистента
                    from .. import assistant as A
                    question = str(b.get("question") or "").strip()
                    if not question:
                        raise UserError("введите вопрос")
                    tid = A.create(st, question[:80], "selection",
                                   {"chat": chat, "ids": ids, "quote": text[:3000], "topic": topic})
                    r = A.answer(cfg, st, ai.make_llm(cfg, provider), tid, question,
                                 opts={"sources": sources or default_sources(cfg)}, progress=job.add)
                    return {"text": r["text"], "refs": r["refs"], "model": r["model"], "thread": tid,
                            "ext": r.get("ext"), "offline": r.get("offline"), "found": None}
                if kind == "summary":
                    tz = get_tz(cfg.digest.get("timezone"))
                    c = mviews.one_chat(st, chat) or {}
                    since, until = ai.parse_period(str(b.get("period") or "day"), b.get("since"), b.get("until"), tz,
                                                   c.get("read_in") or 0, st, chat)
                    if live.state == "online":
                        try:
                            n = live.call(live.fill_period(chat, since), 300)
                            if n:
                                job.add(f"Догрузил из Telegram сообщений: {n}")
                        except LiveError as e:
                            job.add(f"Из Telegram не догрузил ({e}) — сводка по базе.")
                    res = ai.summarize_chat(cfg, st, ai.make_llm(cfg, provider), chat, since, until, job.add, topic=topic)
                    return logged(st, f"Сводка чата «{ctitle}» · {res.get('period') or ''}".strip(" ·"), "", res)
                if kind == "ask":
                    question = str(b.get("question") or "").strip()
                    if not question:
                        raise UserError("введите вопрос")
                    res = ai.ask_chat(cfg, st, ai.make_llm(cfg, provider), chat, question, ids=ids,
                                      quote=text, progress=job.add, topic=topic)
                    return logged(st, f"«{ctitle}»: {question[:70]}", question, res)
                if kind == "draft":
                    return ai.draft_reply(cfg, st, ai.make_llm(cfg, provider), chat, reply_to=ids[0] if ids else None,
                                          hint=str(b.get("hint") or ""), quote=text, progress=job.add, topic=topic)
                if kind == "explain":
                    res = ai.explain(cfg, st, ai.make_llm(cfg, provider), chat, ids=ids, text=text,
                                     use_maverick=b.get("maverick", True) is not False, progress=job.add,
                                     topic=topic)
                    return logged(st, f"Объяснить · «{ctitle}»", text[:300] or "Объясни, что происходит", res)
                if not text.strip():
                    raise UserError("выделите текст или выберите сообщение")
                if kind == "analyze":
                    res = ai.related_analyze(cfg, st, ai.make_llm(cfg, provider), text, chat_id=chat,
                                             msg_id=ids[0] if ids else None, progress=job.add)
                    return logged(st, "Связанное в других чатах", text[:300], res)
                res = ai.maverick_lookup(cfg, st, text, chat_id=chat, question=str(b.get("question") or ""),
                                         progress=job.add)
                return logged(st, "Maverick: почта, ITSM, Confluence", text[:300], res)
            except (ValueError, LLMError) as e:
                raise UserError(str(e)) from None
            finally:
                st.close()
        return {"job": app.start_job("ai", go).id}

    def _llm_test(self) -> dict:
        from ..llm import make_client
        app = self.app
        cfg = app.cfg()
        name, prof = cfg.llm_profile()
        llm = make_client(name, prof)
        llm.retries = 0
        t0 = time.time()
        if hasattr(llm, "check_key"):  # Maverick: сначала проверить ключ — так ошибка понятнее
            try:
                llm.check_key()
            except Exception as e:  # noqa: BLE001
                raise UserError(f"ключ не принят: {e}") from None
        ans = llm.complete("Отвечай одним словом.", "Ответь словом: готов", max_tokens=20)
        log.info("проверка модели: %s (%s) ответила за %.1f с", name, prof.get("type"), time.time() - t0)
        raw = app.raw()
        raw.setdefault("ui", {})["llm_ok"] = True
        found = getattr(llm, "discovered", None)
        if found:  # Maverick: запомнить, в каком поле сервер ждёт текст
            raw["llm"]["profiles"][name]["body_field"] = found
            log.info("Maverick: текст запроса передаётся в поле %s", found)
        app.save(raw)
        note = f" Текст запроса передаётся в поле «{found}»." if found else ""
        return {"answer": ans.strip()[:80] + note, "seconds": round(time.time() - t0, 1), "label": llm.label}

    def _digest_period(self, b: dict, cfg=None) -> dict:
        from ..digest import PeriodError, fmt_period, get_tz, resolve_period
        cfg = cfg or self.app.cfg()
        tz = get_tz(cfg.digest.get("timezone"))
        if str(b.get("period") or "") == "last":   # «С прошлой сводки»: от конца последней сводки до сейчас
            prev = None
            if memdb.exists(cfg.path(cfg["storage"]["path"])):
                st = self.app.store()
                try:
                    r = st.db.execute("SELECT until FROM digests WHERE period NOT IN ('demo', 'source') ORDER BY until DESC LIMIT 1").fetchone()
                    prev = r[0] if r else None
                finally:
                    st.close()
            if not prev:
                raise UserError("прошлой сводки ещё нет — выберите период (например, «Сутки»)")
            since = datetime.fromisoformat(prev).astimezone(tz)
            b = {**b, "period": "custom", "since": since.strftime("%Y-%m-%dT%H:%M:%S"), "until": None}
            try:
                p = resolve_period(b, tz)
            except PeriodError as e:
                raise UserError("с прошлой сводки прошло меньше 5 минут" if "позже" in str(e) else str(e)) from None
            p["kind"], p["label"] = "custom", "время с прошлой сводки"
            p["text"] = fmt_period(p["since"], p["until"], tz)
            return p
        try:
            p = resolve_period(b, tz)
        except PeriodError as e:
            raise UserError(str(e)) from None
        p["text"] = fmt_period(p["since"], p["until"], tz)
        return p

    @staticmethod
    def _chat_ids(b: dict) -> set[int] | None:
        ids = {int(x) for x in (b.get("chat_ids") or []) if str(x).lstrip("-").isdigit()}
        return ids or None

    def _digest_job(self, b: dict) -> Job:
        from ..service import run_digest
        if b.get("mode") == "source":   # «От источника»: очередь ITSM или задачи трекера
            return self._source_job(b)
        app = self.app
        preempt = b.get("preempt", True) is not False
        if not preempt and app.busy():
            raise app.busy_error()
        cfg = app.cfg()
        per = self._digest_period(b, cfg)
        chat_ids = self._chat_ids(b)
        extra = str(b.get("extra_prompt") or "").strip()[:4000]
        if b.get("chat_ids") is not None and not chat_ids:
            raise UserError("не выбран ни один чат — отметьте чаты или выберите «Все чаты»")

        def go(job: Job):
            sync, send = bool(b.get("sync", True)), bool(b.get("send"))
            job.add(f"Период: {per['text']}" + (f" · чатов выбрано: {len(chat_ids)}" if chat_ids else " · все чаты"))
            if preempt:   # всё остальное — стоп: Maverick ведёт одну сессию за раз
                job.add("Освобождаю Maverick и Telegram: останавливаю другие задачи и закрываю открытые сессии…")
                app.preempt(job, "запущена сводка", job.add)
            elif (sync or send) and app.tg_busy():
                sync = send = False
                job.add("Идёт выгрузка истории — она сама забирает новые сообщения; сводка строится по базе, "
                        "без отправки в «Избранное».")
            srcs = b.get("sources") if isinstance(b.get("sources"), dict) else None
            kw = dict(period=per["kind"], since=per["since"], until=per["until"], progress=job.add,
                      run_async=lambda c: app.run_async_job(c, job), sources=srcs, chat_ids=chat_ids, extra=extra,
                      label=per["label"], cancel=lambda: job.cancelled, history=b.get("history", True) is not False)
            got = False
            if sync or send:   # выгрузка после остановки дописывает текущую порцию — подождём её до минуты
                deadline = time.time() + (60 if preempt else 1)
                while not got and not job.cancelled and time.time() < deadline:
                    got = app.tg_lock.acquire(timeout=1)
                if not got and not job.cancelled:
                    sync = send = False
                    job.add("Telegram занят другой операцией — сводка по базе, без новых сообщений и без отправки.")
            if got:
                try:
                    app.close_login()
                    res = run_digest(cfg, send=send, sync=sync, **kw)
                finally:
                    app.tg_lock.release()
            else:
                res = run_digest(cfg, send=False, sync=False, **kw)
            res["report"] = Path(res["paths"]["html"]).name
            self._after_digest(cfg, res)
            return res
        job = app.start_job("digest", go)
        job.title = f"Сводка за {per['label']}" + (f" · чатов: {len(chat_ids)}" if chat_ids else "")
        job.info = {"period": per["text"], "chats": len(chat_ids) if chat_ids else None, "extra": bool(extra)}
        threading.Thread(target=self._resume_after, args=(job,), daemon=True, name="after-digest").start()
        return job

    def _source_args(self, b: dict, cfg) -> tuple[str, dict, dict, str]:
        """Проверка запроса «От источника»: (start, query, look, extra). Ошибки — понятным текстом для окна."""
        from .. import plugins
        from .. import source_digest as sdm
        start = str(b.get("start") or "tracker")
        if start not in sdm.STARTS:
            raise UserError("выберите, с чего начать: ITSM или трекер")
        if not plugins.available(cfg):
            raise UserError("для разбора от источника нужен Maverick с плагинами: «Настройки → Модель» → Maverick")
        if not plugins.enabled(cfg, start):
            raise UserError(f"{plugins.TITLES[start]} выключен в «Настройки → Источники» — включите его")
        q = sdm.norm_query(start, b.get("query") if isinstance(b.get("query"), dict) else {})
        look = sdm.norm_look(b.get("look") if isinstance(b.get("look"), dict) else {})
        return start, q, look, str(b.get("extra_prompt") or "").strip()[:4000]

    def _source_job(self, b: dict) -> Job:
        """Сводка «От источника»: список из ITSM/трекера → связанное → Telegram и почта по номерам → отчёт."""
        from .. import plugins
        from .. import source_digest as sdm
        from ..service import run_source_digest
        app = self.app
        preempt = b.get("preempt", True) is not False
        if not preempt and app.busy():
            raise app.busy_error()
        cfg = app.cfg()
        start, q, look, extra = self._source_args(b, cfg)
        qtext = sdm.query_text(start, q)

        def go(job: Job):
            sync, send = bool(b.get("sync", True)) and look["telegram"], bool(b.get("send"))
            job.add(f"От источника: {plugins.TITLES[start]} — {qtext}")
            if preempt:   # Maverick ведёт одну сессию за раз — остальное стоп
                job.add("Освобождаю Maverick и Telegram: останавливаю другие задачи и закрываю открытые сессии…")
                app.preempt(job, "запущена сводка", job.add)
            elif (sync or send) and app.tg_busy():
                sync = send = False
                job.add("Идёт выгрузка истории — номера ищу по базе, без отправки в «Избранное».")
            kw = dict(start=start, query=q, look=look, progress=job.add, extra=extra,
                      run_async=lambda c: app.run_async_job(c, job), cancel=lambda: job.cancelled)
            got = False
            if sync or send:
                deadline = time.time() + (60 if preempt else 1)
                while not got and not job.cancelled and time.time() < deadline:
                    got = app.tg_lock.acquire(timeout=1)
                if not got and not job.cancelled:
                    sync = send = False
                    job.add("Telegram занят другой операцией — номера ищу по базе, без отправки.")
            try:
                if got:
                    try:
                        app.close_login()
                        res = run_source_digest(cfg, send=send, sync=sync, **kw)
                    finally:
                        app.tg_lock.release()
                else:
                    res = run_source_digest(cfg, send=False, sync=False, **kw)
            except sdm.SourceError as e:
                raise UserError(str(e)) from None
            res["report"] = Path(res["paths"]["html"]).name
            self._after_digest(cfg, res)
            return res
        job = app.start_job("digest", go)
        job.title = f"Разбор очереди: {plugins.SHORT[start]}" + (f" «{q['group'][:60]}»" if q.get("group") else "")
        job.info = {"period": qtext, "chats": None, "extra": bool(extra), "mode": "source"}
        threading.Thread(target=self._resume_after, args=(job,), daemon=True, name="after-digest").start()
        return job

    def _source_preview(self, b: dict) -> dict:
        """«Проверьте перед запуском» для разбора от источника: что разбираем, источники и их подключение, Telegram."""
        from .. import plugins
        from .. import source_digest as sdm
        from ..storage import iso
        app = self.app
        cfg = app.cfg()
        warn: list[dict] = []
        start = str(b.get("start") or "tracker")
        start = start if start in sdm.STARTS else "itsm"
        q = sdm.norm_query(start, b.get("query") if isinstance(b.get("query"), dict) else {})
        look = sdm.norm_look(b.get("look") if isinstance(b.get("look"), dict) else {})
        second = sdm.SECOND[start]
        out: dict[str, Any] = {"mode": "source", "start": start, "start_title": plugins.TITLES[start], "query": q,
                               "query_text": sdm.query_text(start, q), "look": look, "maverick": plugins.available(cfg)}
        stats: dict = {}
        tg_n = 0
        if memdb.exists(cfg.path(cfg["storage"]["path"])):
            st = app.store()
            try:
                stats = plugins.statuses(cfg, st)
                if look["telegram"]:
                    since = iso(datetime.now(timezone.utc) - timedelta(days=look["days"]))
                    tg_n = sum(st.period_counts(since, iso(datetime.now(timezone.utc) + timedelta(minutes=1))).values())
            finally:
                st.close()
        out["tg_messages"] = tg_n
        roles = {start: "откуда берём список", second: "связанные записи"}
        want = [start] + ([second] if look["second"] else []) + [k for k in ("mail", "rooms", "meet") if look.get(k)]
        src: dict[str, dict] = {}
        for k in want:
            s0 = stats.get(k) or {}
            src[k] = {"short": plugins.SHORT[k], "role": roles.get(k, "поиск по номерам"),
                      "state": "disabled" if not plugins.enabled(cfg, k) else (s0.get("state") or "unknown"),
                      "detail": str(s0.get("detail") or "")[:200], "at": s0.get("at")}
        if look["telegram"]:
            src["telegram"] = {"short": "Telegram", "role": "поиск по номерам",
                               "detail": f"по базе на этом компьютере, сообщений за {look['days']} дн.: {tg_n}"}
        out["sources"] = src
        if not out["maverick"]:
            warn.append({"level": "bad", "blocking": True, "text": "Maverick не настроен: «Настройки → Модель» → Maverick."})
        elif src[start]["state"] == "disabled":
            warn.append({"level": "bad", "blocking": True, "text": f"{plugins.TITLES[start]} выключен в «Настройки → Источники»."})
        elif src[start]["state"] in ("offline", "error"):
            warn.append({"level": "bad", "text": f"{plugins.SHORT[start]} не отвечал при последней проверке — переподключите "
                                                 "плагин в чате Maverick и нажмите «Проверить подключение»."})
        elif src[start]["state"] == "unknown":
            warn.append({"level": "warn", "text": f"{plugins.SHORT[start]} ещё не проверялся — нажмите «Проверить подключение»."})
        bad = [v["short"] for k, v in src.items() if k not in (start, "telegram") and v["state"] in ("offline", "error", "disabled")]
        if bad:
            warn.append({"level": "warn", "text": "Без них разбор будет неполным: " + ", ".join(bad) + " (не подключены или выключены)."})
        if not q.get("group") and not q.get("text"):
            warn.append({"level": "warn", "text": "Условия не заданы — Maverick вернёт все подходящие записи, которые вам видны "
                                                  f"(не больше {q['limit']}). Лучше указать группу или проект."})
        if look["telegram"] and not tg_n:
            warn.append({"level": "info", "text": "В базе Telegram за это время сообщений нет — номера искать негде"
                                                  + (" (новые заберутся перед разбором)." if b.get("sync") else ".")})
        if b.get("extra_prompt"):
            out["extra_prompt"] = str(b["extra_prompt"]).strip()[:4000]
        try:
            name, prof = cfg.llm_profile()
            out["model"] = {"name": name, "type": prof.get("type"), "ok": bool((app.raw().get("ui") or {}).get("llm_ok"))}
        except Exception as e:  # noqa: BLE001
            warn.append({"level": "bad", "blocking": True, "text": f"Модель не настроена: {e}"})
        out["sync"], out["send"] = bool(b.get("sync")) and look["telegram"], bool(b.get("send"))
        out["preempt"] = b.get("preempt", True) is not False
        if out["preempt"]:
            out["will_stop"] = [{"title": r["title"], "long": r["long"]} for r in app.running_view() if not r["keep"]]
        elif app.busy():
            warn.append({"level": "bad", "text": str(app.busy_error())})
        out["warnings"] = warn
        return out

    def _digest_chats(self, b: dict) -> dict:
        """Чаты для выбора в «Сводке»: все из базы, сколько сообщений за выбранный период, важность, исключён ли."""
        app = self.app
        cfg = app.cfg()
        per = self._digest_period(b, cfg)
        if not memdb.exists(cfg.path(cfg["storage"]["path"])):
            return {"chats": [], "period": per["text"]}
        from ..storage import iso
        st = app.store()
        try:
            counts = st.period_counts(iso(per["since"]), iso(per["until"]))
            rows = st.db.execute("SELECT chat_id, title, kind, username, top_date, photo FROM chats "
                                 "WHERE source='telegram'").fetchall()
        finally:
            st.close()
        out = []
        for r in rows:
            rule = cfg.chat_rule(r["title"] or "", r["username"], r["chat_id"], r["kind"] or "group")
            out.append({"id": r["chat_id"], "title": r["title"] or str(r["chat_id"]), "kind": r["kind"] or "",
                        "n": counts.get(r["chat_id"], 0), "excluded": rule["excluded"], "priority": rule["rank"],
                        "mode": rule["kind"], "photo": r["photo"] or "", "last": r["top_date"] or ""})
        out.sort(key=lambda c: (-c["n"], c["excluded"], (c["title"] or "").lower()))
        return {"chats": out, "period": per["text"], "with_msgs": sum(1 for c in out if c["n"] and not c["excluded"])}

    def _history_estimate(self, cfg, per: dict, act: list, extra: str) -> dict:
        """Сколько сообщений уже разобрано в прошлых сводках (модель их заново не прочитает) и какая сводка — прошлая."""
        from .. import history
        from ..storage import iso
        st = self.app.store()
        try:
            history.ensure(st)
            covered = chats = 0
            for chat, _n in act[:400]:
                ids = [r[0] for r in st.db.execute(
                    "SELECT msg_id FROM messages WHERE source='telegram' AND chat_id=? AND date>=? AND date<?",
                    (chat.chat_id, iso(per["since"]), iso(per["until"])))]
                ch = history.chain(st, chat.chat_id, ids, extra)
                if ch:
                    chats += 1
                    covered += sum(1 for i in ids if i <= ch[-1]["last_msg"])
            prev = history.prev_digest(st, iso(per["since"]), iso(per["until"]))
        finally:
            st.close()
        from ..digest import fmt_period, get_tz
        tz = get_tz(cfg.digest.get("timezone"))
        return {"covered": covered, "chats": chats,
                "prev": fmt_period(datetime.fromisoformat(prev["since"]), datetime.fromisoformat(prev["until"]), tz)
                if prev else None}

    def _digest_preview(self, b: dict) -> dict:
        """Что именно будет собрано: период, чаты, сообщения, модель, источники и их подключение, что остановится."""
        if b.get("mode") == "source":
            return self._source_preview(b)
        from .. import plugins
        from ..storage import iso
        app = self.app
        cfg = app.cfg()
        per = self._digest_period(b, cfg)
        chat_ids = self._chat_ids(b)
        warn: list[dict] = []
        out: dict[str, Any] = {"period": {"text": per["text"], "label": per["label"], "minutes": per["minutes"],
                                          "since": per["since"].isoformat(timespec="minutes"),
                                          "until": per["until"].isoformat(timespec="minutes")}}
        if b.get("chat_ids") is not None and not chat_ids:
            warn.append({"level": "bad", "text": "Не выбран ни один чат."})
        msgs = chats_n = 0
        names: list[str] = []
        if memdb.exists(cfg.path(cfg["storage"]["path"])):
            st = app.store()
            try:
                if chat_ids:
                    act = [cn for cn in st.active_chats(iso(per["since"]), iso(per["until"]), include_ignored=True)
                           if cn[0].chat_id in chat_ids]
                    titles = {r[0]: r[1] for r in st.db.execute(
                        f"SELECT chat_id, title FROM chats WHERE chat_id IN ({','.join('?' * len(chat_ids))})",
                        list(chat_ids))}
                    names = [titles.get(i) or str(i) for i in chat_ids]
                else:
                    act = st.active_chats(iso(per["since"]), iso(per["until"]))
                chats_n, msgs = len(act), sum(n for _c, n in act)
                last = st.kv_get("last_sync")
            finally:
                st.close()
        else:
            last = None
        out["chats"] = {"all": not chat_ids, "selected": len(chat_ids) if chat_ids else None,
                        "names": sorted(names, key=str.lower)[:40], "with_msgs": chats_n, "messages": msgs}
        out["history"] = {"on": b.get("history", True) is not False}
        if out["history"]["on"] and msgs and memdb.exists(cfg.path(cfg["storage"]["path"])):
            out["history"].update(self._history_estimate(cfg, per, act, str(b.get("extra_prompt") or "")))
        out["last_sync"] = last
        if not msgs:
            warn.append({"level": "warn", "text": "За этот период в " + ("выбранных чатах" if chat_ids else "базе") +
                         " нет сообщений" + (" — новые заберутся из Telegram перед сводкой." if b.get("sync") else ".")})
        if b.get("extra_prompt"):
            out["extra_prompt"] = str(b["extra_prompt"]).strip()[:4000]
        try:
            name, prof = cfg.llm_profile()
            out["model"] = {"name": name, "type": prof.get("type"), "ok": bool((app.raw().get("ui") or {}).get("llm_ok"))}
            if not out["model"]["ok"]:
                warn.append({"level": "warn", "text": "Модель ещё не проверена («Настройки → Модель» → «Сохранить и проверить»)."})
        except Exception as e:  # noqa: BLE001
            warn.append({"level": "bad", "text": f"Модель не настроена: {e}"})
        # источники: выбранные, включённые, подключены ли (по последней проверке)
        want = b.get("sources") if isinstance(b.get("sources"), dict) else {}
        src: dict[str, dict] = {}
        avail = False
        try:
            avail = plugins.available(cfg)
            st = app.store()
            try:
                stats = plugins.statuses(cfg, st)
            finally:
                st.close()
            for k in plugins.KINDS:
                s0 = stats.get(k) or {}
                src[k] = {"title": plugins.TITLES[k], "short": plugins.SHORT[k], "selected": bool(want.get(k)),
                          "enabled": bool(s0.get("enabled")), "state": s0.get("state") or "unknown",
                          "detail": str(s0.get("detail") or "")[:200], "at": s0.get("at")}
        except Exception as e:  # noqa: BLE001
            log.info("источники для подтверждения сводки: %s", logs.err_info(e))
        out["sources"] = src
        out["maverick"] = avail
        chosen = [k for k, v in src.items() if v["selected"]]
        bad = [src[k]["short"] for k in chosen if src[k]["state"] in ("offline", "error")]
        unk = [src[k]["short"] for k in chosen if src[k]["state"] not in ("ok", "offline", "error")]
        if bad:
            warn.append({"level": "bad", "text": "Не подключены при последней проверке: " + ", ".join(bad) +
                         ". Переподключите плагин в чате Maverick и нажмите «Проверить подключение»."})
        if unk:
            warn.append({"level": "warn", "text": "Ещё не проверялись: " + ", ".join(unk) + " — нажмите «Проверить подключение»."})
        if avail and not chosen:
            warn.append({"level": "info", "text": "Плагины не выбраны — сводка только по Telegram, без сверки с почтой, "
                                                  "ITSM и вики."})
        out["sync"], out["send"], out["preempt"] = bool(b.get("sync")), bool(b.get("send")), b.get("preempt", True) is not False
        if out["preempt"]:
            out["will_stop"] = [{"title": r["title"], "long": r["long"]} for r in app.running_view() if not r["keep"]]
        elif app.busy():
            warn.append({"level": "bad", "text": str(app.busy_error())})
        out["warnings"] = warn
        return out

    def _resume_after(self, job: Job) -> None:
        """Сводка закончилась — сразу продолжить приостановленные задачи (не ждать 5 с таймера)."""
        while job.status == "running":
            time.sleep(1)
        try:
            self.app.resume_tick()
        except Exception as e:  # noqa: BLE001
            log.info("задачи после сводки: %s", logs.err_info(e))

    def _after_digest(self, cfg, res: dict) -> None:
        """После сводки: запись в историю ассистента; очередь глоссария — обработать фоном (если включено)."""
        app = self.app
        st = app.store()
        try:
            from .. import assistant as A
            cr = res.get("cross") or {}
            text = (res.get("headline") or "") + (("\n\n**По всем источникам:** " + cr["headline"]) if cr.get("headline") else "")
            ttl = f"Сводка · {res.get('period_text') or ''}".strip(" ·")
            if res.get("mode") == "source":
                ttl = f"Разбор очереди · {(res.get('source') or {}).get('query') or ''}".strip(" ·")
            A.log_exchange(st, "digest", ttl, "",
                           text or "Сводка готова.", {"report": res.get("report"), "top": res.get("top"),
                                                      "chats": res.get("chats"), "cross": cr or None})
        except Exception as e:  # noqa: BLE001
            log.info("сводка не записана в историю ассистента: %s", logs.err_info(e))
        finally:
            st.close()
        gl = cfg.get("glossary") or {}
        if gl.get("auto_process", True) is not False and int(res.get("glossary_pending") or 0) > 0 \
                and not app.running("glossary_auto"):
            try:
                body = {"limit": 30, "min_count": 2, "auto": True}
                self._api("POST", "/api/glossary/process", body, {})
                res["glossary_processing"] = True
            except Exception as e:  # noqa: BLE001 — например, модель не настроена
                log.info("очередь глоссария не запущена: %s", logs.err_info(e))

    def _since(self, b: dict):
        from ..digest import get_tz
        if not b.get("since"):
            return None
        tz = get_tz(self.app.cfg().digest.get("timezone"))
        try:
            return datetime.strptime(str(b["since"])[:10], "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            raise UserError("дата в формате ГГГГ-ММ-ДД") from None

    def _media_stats(self) -> dict:
        app = self.app
        cfg = app.cfg()
        if not memdb.exists(cfg.path(cfg["storage"]["path"])):
            return {}
        st = app.store()
        try:
            out = st.media_stats()
            out["last_export"] = st.kv_get("last_export")
            return out
        finally:
            st.close()

    def _export_job(self, b: dict) -> Job:
        from ..service import run_export
        app = self.app
        if app.running("export"):
            raise UserError("выгрузка уже идёт")
        since = self._since(b)
        chat_ids = {int(x) for x in b.get("chat_ids") or []} or None
        if b.get("light_max_mb") not in (None, ""):
            app.save_media({"light_max_mb": b["light_max_mb"]})
        lock = app.tg_take(timeout=2)

        def go(job: Job):
            with lock:
                app.close_login()
                return run_export(app.cfg(), chat_ids=chat_ids, since=since, with_media=bool(b.get("media", True)),
                                  progress=job.add, run_async=lambda c: app.run_async(c),
                                  cancel=lambda: job.cancelled)
        return app.start_job("export", go, task=b)

    def _images_job(self, b: dict) -> Job:
        from ..service import run_images
        app = self.app
        try:
            chat_id = int(b.get("chat_id"))
        except (TypeError, ValueError):
            raise UserError("не выбран чат") from None
        if app.running("images"):
            raise UserError("изображения уже обрабатываются — дождитесь окончания или остановите")
        since = self._since(b)
        logged = (app.need_ws() / "data" / "telegram.session").exists()

        def go(job: Job):
            got = logged and app.tg_lock.acquire(timeout=1)
            try:
                if logged and not got:
                    job.add("Telegram занят выгрузкой — скачивание пропускаю, читаю уже скачанные изображения.")
                return run_images(app.cfg(), chat_id, since=since, download=bool(got), progress=job.add,
                                  run_async=lambda c: app.run_async(c), cancel=lambda: job.cancelled)
            finally:
                if got:
                    app.tg_lock.release()
        return app.start_job("images", go, task=b)

    def _fetch_job(self, b: dict) -> Job:
        from ..service import run_fetch
        from ..sources.telegram import TelegramError
        app = self.app
        try:
            mid = int(str(b.get("id")).lstrip("m"))
        except (TypeError, ValueError):
            raise UserError("не указан файл") from None
        lock = app.tg_take(timeout=2)

        def go(job: Job):
            with lock:
                app.close_login()
                try:
                    return run_fetch(app.cfg(), mid, progress=job.add, run_async=lambda c: app.run_async(c))
                except TelegramError as e:
                    raise UserError(str(e)) from None
        return app.start_job("fetch", go)

    def _vision_job(self, b: dict) -> Job:
        app = self.app
        if app.busy():
            raise UserError("дождитесь окончания текущей задачи")
        if "provider" in b:
            app.save_media({"vision_provider": b.get("provider") or ""})

        def go(job: Job):
            from ..llm import LLMError
            from ..vision import check, make_vision_llm, save_report
            cfg = app.cfg()
            llm = make_vision_llm(cfg)
            llm.retries = 0
            if b.get("rediscover") and isinstance(llm.profile.get("image_mode"), dict):
                llm.profile.pop("image_mode", None)
            job.add(f"Модель для изображений: {llm.label}. Отправляю картинку с цифрой…")
            had = isinstance(llm.profile.get("image_mode"), dict)
            try:
                try:
                    res = check(llm, progress=job.add)
                except LLMError:
                    if not had:
                        raise
                    res = {"ok": False}
                if had and not res["ok"]:  # сохранённый формат перестал работать — подобрать заново
                    job.add("Сохранённый формат не сработал — подбираю заново…")
                    llm.profile.pop("image_mode", None)
                    res = check(llm, progress=job.add)
            except LLMError as e:
                rp = save_report(cfg, llm)
                raise UserError(str(e).replace("[maverick] запрос к LLM не удался: ", "")
                                + (f"\nЖурнал попыток: {rp}" if rp else "")) from None
            rp = save_report(cfg, llm)
            if rp:
                job.add(f"Журнал подбора формата: {rp}")
            raw = app.raw()
            prof = raw["llm"]["profiles"].get(llm.name)
            if prof is not None:
                for k in ("image_mode", "body_field", "vision_model"):
                    if llm.profile.get(k):
                        prof[k] = llm.profile[k]
                app.save(raw)
            if res["ok"]:
                job.add(f"✓ Модель видит изображения: ответила «{res['answer']}» (на картинке {res['expected']}).")
            else:
                job.add(f"✗ Модель ответила «{res['answer']}», а на картинке {res['expected']} — изображение до неё "
                        "не доходит или модель не умеет читать картинки.")
            return res
        return app.start_job("vision", go)

    def _reports(self) -> list[dict]:
        ws = self.app.ws
        if not ws or not (ws / "reports").exists():
            return []
        out = []
        cache = self.app.__dict__.setdefault("_reports_cache", {})   # расшифровывать 40 отчётов на каждый показ — долго
        for f in sorted((ws / "reports").glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            key = (str(f), f.stat().st_mtime)
            if key in cache:
                out.append(dict(cache[key]))
                continue
            item = {"name": f.name, "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
                    "period": f.stem.rsplit("_", 1)[-1], "headline": "", "md": f.with_suffix(".md").exists()}
            j = f.with_suffix(".json")
            if j.exists() and j.stat().st_size < 3_000_000:
                try:
                    d = json.loads(sfiles.read_text(j))
                    item.update(headline=(d.get("overview") or {}).get("headline", "")[:300],
                                period_text=(d.get("meta") or {}).get("period_text", ""),
                                period_label=(d.get("meta") or {}).get("period_label") or "",
                                top=len((d.get("overview") or {}).get("top") or []))
                    cache[key] = dict(item)
                except (ValueError, vault.Locked):
                    pass
            out.append(item)
        return out


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    app.port = srv.server_address[1]
    app.server = srv
    app.handler_cls = handler
    # Раньше сервер сам завершался через 2 минуты без запросов окна. Окно перестаёт их слать, когда компьютер спит
    # или Windows замораживает свёрнутое окно, — и программа закрывалась посреди выгрузки. Теперь не завершается.
    return srv
