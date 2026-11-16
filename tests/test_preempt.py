"""Сводка важнее всего остального: зависшие запросы к модели обрываются, короткие задачи останавливаются, долгие —
приостанавливаются и продолжаются сами после сводки; открытые сессии Maverick закрываются; занятый Maverick —
подождать и повторить; после сна мёртвые соединения обрываются и запрос повторяется сразу."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_chats_api import fill
from test_tasks import _slow_describe, _wait
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна
from test_ui_media import setup_ws

from fake_llm import start_server

from opsdigest.llm import LLMError, inflight
from opsdigest.llm.base import LLMClient


class _Slow(BaseHTTPRequestHandler):
    """mode: hang — не отвечает (как после сна); busy — 409 первые N раз; once_hang — первый запрос висит."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        srv = self.server
        srv.n += 1
        if srv.mode == "hang" or (srv.mode == "once_hang" and srv.n == 1):
            srv.hung.set()
            time.sleep(30)
            return
        if srv.mode == "busy" and srv.n <= srv.busy:
            b = json.dumps({"detail": "Дождитесь окончания текущей сессии"}, ensure_ascii=False).encode()
            self.send_response(409)
        else:
            b = json.dumps({"text": f"ответ {srv.n}"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def mav():
    srv, url = start_server()
    srv.mav_portal = True
    yield srv, url.rsplit("/v1", 1)[0]
    srv.shutdown()


@pytest.fixture
def slow():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    srv.daemon_threads = True
    srv.mode, srv.n, srv.busy, srv.hung = "hang", 0, 0, threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}/q"
    srv.shutdown()


class Client(LLMClient):
    def __init__(self, url):
        super().__init__("t", {"timeout": 25, "retries": 2})
        self.url = url

    def _complete(self, system, user, max_tokens):
        return self.http_json("POST", self.url, {"q": user})["text"]


def _in_job(job, fn):
    out = {}

    def run():
        inflight.bind(job)
        try:
            out["ok"] = fn()
        except Exception as e:  # noqa: BLE001
            out["err"] = e
        finally:
            inflight.bind(None)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def test_cancel_breaks_hanging_request(slow):
    srv, url = slow
    t, out = _in_job("job1", lambda: Client(url).complete("", "привет"))
    assert srv.hung.wait(5)
    _wait(lambda: inflight.open_count("job1") == 1)
    conn = next(c for c, j in inflight._conns.values() if j == "job1")
    assert conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)  # мёртвая связь видна через ~2 мин
    t0 = time.time()
    assert inflight.cancel_jobs(["job1"], "запущена сводка") == 1
    t.join(5)
    assert not t.is_alive() and time.time() - t0 < 3
    e = out["err"]
    assert isinstance(e, LLMError) and getattr(e, "cancelled", False) and "запущена сводка" in str(e)
    assert srv.n == 1   # остановленный запрос не повторяется
    # новые запросы этой задачи — сразу отказ, без сети
    t, out = _in_job("job1", lambda: Client(url).complete("", "ещё"))
    t.join(5)
    assert getattr(out["err"], "cancelled", False) and srv.n == 1
    inflight.forget("job1")


def test_stale_after_sleep_retries_at_once(slow):
    srv, url = slow
    srv.mode = "once_hang"
    t, out = _in_job("job2", lambda: Client(url).complete("", "привет"))
    assert srv.hung.wait(5)
    _wait(lambda: inflight.open_count("job2") == 1)
    assert inflight.abort_stale() >= 1   # проснулись: соединение мёртвое — повторить сейчас
    t.join(10)
    assert out.get("ok") == "ответ 2", out


def test_busy_waits_and_retries(slow, monkeypatch):
    srv, url = slow
    srv.mode, srv.busy = "busy", 4   # больше, чем обычных повторов (2)
    waits = []
    monkeypatch.setattr(inflight, "sleep", lambda s: waits.append(s))
    assert Client(url).complete("", "сводка") == "ответ 5"
    assert len(waits) == 4 and all(w >= 20 for w in waits)


def _hanging_ai_job(app, url):
    """Задача «ИИ в чате», которая повисла на запросе к модели (как после сна)."""
    return app.start_job("ai", lambda job: Client(url).complete("", "вопрос"))


def test_digest_preempts_everything(ui, slow, monkeypatch):  # noqa: F811
    app, call, llm_url, tmp = ui
    srv, url = slow
    setup_ws(call, tmp, llm_url)
    fill(app)
    app._stop.set()   # таймер задач — вручную: продолжение после сводки делает сама сводка
    started, release = [], threading.Event()
    _slow_describe(monkeypatch, started, release)
    desc = call("/api/describe", {"targets": {"chats": [-1001]}})[1]["job"]
    ai = _hanging_ai_job(app, url)
    assert srv.hung.wait(5)
    assert app.busy()
    # без остановки — понятный отказ: что идёт
    code, r = call("/api/digest", {"period": "week", "sync": False, "preempt": False})
    assert code == 400 and "ИИ в чате" in r["error"]
    st = call("/api/state")[1]
    assert {x["kind"] for x in st["running"]} >= {"ai", "describe"}
    # сводка: всё остальное — стоп
    code, r = call("/api/digest", {"period": "week", "sync": False})
    assert code == 200
    j = wait_job(call, r["job"], 30)
    assert j["status"] == "done", j
    log = "\n".join(j["log"])
    assert "Остановлено: ИИ в чате" in log and "Приостановлено до конца сводки: ИИ-описания" in log
    assert "Оборвано запросов к модели: 1" in log
    a = call(f"/api/jobs/{ai.id}")[1]
    assert a["status"] == "error" and "запущена сводка" in a["error"]
    # ИИ-описания продолжились сами после сводки
    _wait(lambda: len(started) == 2)
    assert started[1]["attempt"] == 2 and call(f"/api/jobs/{desc}")[1]["status"] != "running"
    release.set()


def test_stop_all_pauses(ui, slow, monkeypatch):  # noqa: F811
    app, call, llm_url, tmp = ui
    srv, url = slow
    setup_ws(call, tmp, llm_url)
    app._stop.set()
    started, release = [], threading.Event()
    _slow_describe(monkeypatch, started, release)
    call("/api/describe", {"targets": {"chats": [-1001]}})
    ai = _hanging_ai_job(app, url)
    assert srv.hung.wait(5)
    r = call("/api/jobs/stop_all", {})[1]
    assert r["stopped"] == ["ИИ в чате"] and r["paused"] == ["ИИ-описания"]
    _wait(lambda: call("/api/tasks")[1]["tasks"][0]["status"] == "paused")
    assert app.resume_tick() == [] and not app.busy() and ai.status == "error"
    release.set()


def test_release_maverick_sessions(ui, mav):  # noqa: F811
    """Сессия, которая ждёт ответа человека, перед сводкой закрывается: служебная — остановкой (адрес запоминается),
    беседа ассистента «как на портале» — тоже, с пометкой в беседе."""
    from opsdigest import assistant as A
    from opsdigest.llm import make_client
    from opsdigest.llm.providers import MaverickClient
    app, call, llm_url, tmp = ui
    srv, host = mav
    setup_ws(call, tmp, llm_url)
    raw = app.raw()
    raw["llm"]["profiles"]["maverick"] = {"type": "maverick", "url": host, "api_key": "sk_test"}
    app.save(raw)
    srv.mav_cancel = True
    MaverickClient.CANCEL_FOUND.clear()
    MaverickClient.CANCEL_NONE.clear()
    # служебный запрос упёрся в hitl → сессия запомнена и закрывается фоном сразу
    llm = make_client("maverick", app.cfg().llm_profile("maverick")[1])
    with pytest.raises(LLMError, match="hitl"):
        llm.complete("", "нужен hitl_test")
    _wait(lambda: "s1" in getattr(srv, "cancelled", []))
    _wait(lambda: "s1" not in inflight.open_sessions())
    # беседа «как на портале», ждущая ответа на вопрос Maverick
    st = app.store()
    tid = A.create(st, "Портал", "maverick", {"mav_session": "sp9", "mav_hitl": {"id": "h1"}})
    st.close()
    inflight.note_session("s5", "processing", host=host)
    out = app.release_maverick()
    assert out["total"] == 2 and out["closed"] == 2
    assert {"sp9", "s5"} <= set(srv.cancelled)
    st = app.store()
    meta = json.loads(st.db.execute("SELECT meta FROM asst_threads WHERE id=?", (tid,)).fetchone()["meta"])
    notes = [r["text"] for r in st.db.execute("SELECT text FROM asst_msgs WHERE thread_id=? AND role='note'", (tid,))]
    st.close()
    assert "mav_hitl" not in meta and meta["mav_session"] == "sp9" and any("закрыла" in x for x in notes)
    jf = (tmp / "ws" / "data" / "maverick-sessions.txt").read_text(encoding="utf-8")
    assert "закрыта — POST /api/v1/sessions/{sid}/cancel" in jf and "sk_test" not in jf
    assert not inflight.open_sessions()
