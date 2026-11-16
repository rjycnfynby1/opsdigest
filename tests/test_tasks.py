"""Долгие задачи доводятся до конца: список в рабочей папке, пауза и продолжение, повтор после ошибки,
продолжение после перезапуска программы (с пропуском уже сделанного), запрет сна, пока они идут."""
import json
import threading
import time

from test_chats_api import fill
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна
from test_ui_media import setup_ws

from opsdigest import power
from opsdigest import tasks as T


def book(tmp_path):
    f = tmp_path / "tasks.json"
    return T.TaskBook(lambda: f, lambda p: p.read_text(encoding="utf-8"),
                      lambda p, t: p.write_text(t, encoding="utf-8")), f


def test_taskbook(tmp_path):
    b, f = book(tmp_path)
    r = b.begin("export", {"media": True, "_task": None}, "j1")
    assert r["status"] == "running" and r["attempts"] == 1 and "_task" not in r["body"]
    assert r["title"] == "Выгрузка истории — все чаты"
    # ошибка → повтор с паузой; «Повторить» → в очередь
    r = b.finish(r["id"], ok=False, error="нет связи")
    assert r["status"] == "retry" and r["next_at"] > time.time() + 50 and not b.due()
    b.woke()  # проснулся компьютер — повтор почти сразу
    assert b.get(r["id"])["next_at"] < time.time() + 20
    assert b.due(time.time() + 30)[0]["id"] == r["id"]
    # пауза пользователем и прерывание программой
    r = b.begin("export", {"media": True, "_task": r["id"]}, "j2")
    assert r["attempts"] == 2
    assert b.finish(r["id"], ok=False, cancelled="user")["status"] == "paused" and not b.due() and not b.active()
    b.resume(r["id"])
    r = b.begin("export", {"media": True, "_task": r["id"]}, "j3")
    assert b.finish(r["id"], ok=False, cancelled="system")["status"] == "queued" and b.active()
    # та же задача, запущенная из окна заново, заменяет стоящую в очереди
    r2 = b.begin("export", {"media": True}, "j4")
    assert r2["id"] != r["id"] and len(b.items) == 1
    # сбой процесса: «идёт» в файле → после перезапуска «ждёт запуска»
    b2, _ = book(tmp_path)
    assert b2.load() and b2.get(r2["id"])["status"] == "queued" and b2.due()[0]["id"] == r2["id"]
    assert b2.finish(r2["id"], ok=True, result={"chats": 3, "messages": 10, "rows": [1]})["status"] == "done"
    assert b2.get(r2["id"])["result"] == {"chats": 3, "messages": 10} and not b2.active()
    assert b2.remove(r2["id"]) and not b2.items
    # испорченный файл не роняет
    f.write_text("{oops", encoding="utf-8")
    b3, _ = book(tmp_path)
    assert b3.load() and b3.items == {}
    assert T.backoff(1) == 60 and T.backoff(99) == 1800
    # сон: держим, пока идёт или ждёт; после долгой череды ошибок — отпускаем (повторы продолжаются)
    r = b3.begin("describe", {"targets": {"chats": [1]}}, "j9")
    assert b3.wants_awake()
    for _ in range(T.AWAKE_RETRIES):
        b3.finish(r["id"], ok=False, error="нет связи")
        assert b3.wants_awake()
        b3.begin("describe", {"_task": r["id"]}, "j10")
    b3.finish(r["id"], ok=False, error="нет связи")
    assert not b3.wants_awake() and b3.active()


def test_power_noop_outside_windows():
    k = power.KeepAwake()
    assert k.set(True) is True and k.set(True) is True and k.set(False) is False


def _slow_describe(monkeypatch, started, release):
    """Подмена ИИ-описаний: идёт, пока не отпустят или не остановят."""
    from opsdigest.ui import chats_api as C

    def fake(app, body, job):
        started.append(body.get("_resume"))
        while not job.cancelled and not release.is_set():
            time.sleep(0.05)
        return {"done": 0 if job.cancelled else 1, "total": 1}
    monkeypatch.setattr(C, "describe_job", fake)


def _wait(pred, sec=10):
    end = time.time() + sec
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("не дождался")


def test_pause_resume_and_interrupt(ui, monkeypatch):  # noqa: F811
    app, call, llm_url, tmp = ui
    setup_ws(call, tmp, llm_url)
    app._stop.set()  # фоновый запуск — вручную (resume_tick), чтобы тест был предсказуемым
    started, release = [], threading.Event()
    _slow_describe(monkeypatch, started, release)
    job = call("/api/describe", {"targets": {"chats": [-1001]}})[1]["job"]
    tl = call("/api/tasks")[1]
    t = tl["tasks"][0]
    assert t["status"] == "running" and t["job"] == job and t["kind"] == "describe" and t["title"] == "ИИ-описания — 1 чатов"
    assert power.AWAKE.active  # пока идёт — сон запрещён (вне Windows — только отметка)
    assert call("/api/state")[1]["tasks"]["active"] == 1
    # «Остановить» → пауза; сама не продолжается
    call(f"/api/jobs/{job}/cancel", {})
    _wait(lambda: call("/api/tasks")[1]["tasks"][0]["status"] == "paused")
    assert app.resume_tick() == [] and not power.AWAKE.active
    # «Продолжить» → запускается снова с отметкой продолжения
    r = call(f"/api/tasks/{t['id']}/resume", {})[1]
    assert r["tasks"][0]["status"] == "running"
    _wait(lambda: len(started) == 2)
    assert started[1]["attempt"] == 2
    # «Выход»/полная блокировка → прервана, но не на паузе: продолжится сама
    app.full_lock()
    _wait(lambda: app.tasks.get(t["id"])["status"] == "queued" and not app.running("describe"))
    assert app.resume_tick() == ["describe"]
    _wait(lambda: len(started) == 3)
    assert started[2]["attempt"] == 3
    # убрать идущую нельзя; выполненную — можно
    assert call(f"/api/tasks/{t['id']}/remove", {})[0] == 400
    release.set()
    _wait(lambda: app.tasks.get(t["id"])["status"] == "done")
    assert call(f"/api/tasks/{t['id']}/remove", {})[1]["tasks"] == []
    assert not power.AWAKE.active


def test_retry_after_error(ui, monkeypatch):  # noqa: F811
    app, call, llm_url, tmp = ui
    setup_ws(call, tmp, llm_url)
    app._stop.set()
    from opsdigest.ui import chats_api as C
    calls = []

    def flaky(app_, body, job):
        calls.append(body.get("_resume"))
        if len(calls) == 1:
            raise C.ApiError("модель недоступна: таймаут")
        return {"done": 1, "total": 1}
    monkeypatch.setattr(C, "describe_job", flaky)
    j = wait_job(call, call("/api/describe", {"targets": {"chats": [-1001]}})[1]["job"])
    assert j["status"] == "error" and any("Повторю автоматически" in x for x in j["log"])
    t = call("/api/tasks")[1]["tasks"][0]
    assert t["status"] == "retry" and 50 <= t["retry_in"] <= 60 and "таймаут" in t["error"]
    assert app.resume_tick() == []           # пауза ещё не прошла
    call(f"/api/tasks/{t['id']}/now", {})     # «Повторить сейчас»
    _wait(lambda: app.tasks.get(t["id"])["status"] == "done")
    assert calls[1]["attempt"] == 2


def test_resume_after_restart_skips_done(ui, monkeypatch):  # noqa: F811
    """Программа упала посреди «описать заново»: новый процесс продолжает и не повторяет уже описанное."""
    app, call, llm_url, tmp = ui
    ws = setup_ws(call, tmp, llm_url)
    fill(app)
    app._stop.set()
    j = wait_job(call, call("/api/describe", {"targets": {"chats": [-1001]}})[1]["job"], 30)
    assert j["status"] == "done" and j["result"]["done"] == 1
    tid = call("/api/tasks")[1]["tasks"][0]["id"]
    call(f"/api/tasks/{tid}/remove", {})
    rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
    at = rows[-1001]["ai_at"]
    # как будто «описать заново» обоих начали до этого описания и процесс упал на середине
    from datetime import datetime, timedelta
    first = (datetime.fromisoformat(at) - timedelta(seconds=5)).isoformat()
    f = ws / "data" / "tasks.json"
    f.write_text(json.dumps({"v": 1, "tasks": [{
        "id": "abc12", "kind": "describe", "title": "ИИ-описания", "status": "running", "job": "old",
        "body": {"targets": {"chats": [-1001, -1002]}, "overwrite": True}, "created": first, "first": first,
        "attempts": 1}]}), encoding="utf-8")
    from opsdigest.ui.server import App, make_server
    app2 = App(resume=False)   # новый процесс сервера после сбоя
    srv2 = make_server(app2, 0)
    try:
        assert app2.tasks.load() and app2.tasks.get("abc12")["status"] == "queued"
        assert app2.resume_tick() == ["describe"]
        _wait(lambda: app2.tasks.get("abc12")["status"] == "done", 30)
        rec = app2.tasks.get("abc12")
        assert rec["attempts"] == 2 and rec["result"]["total"] == 1 and rec["result"]["done"] == 1, rec
        rows = {c["id"]: c for c in call("/api/chats/list")[1]["chats"]}
        assert rows[-1001]["ai_at"] == at and rows[-1002]["ai"]   # первый не переписан, второй описан
    finally:
        srv2.server_close()
