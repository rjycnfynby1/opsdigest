"""Сессия 13: период до минут, выбор чатов, указания для сводки, история сводок (повтор без модели, опора на прошлые
сводки, «что изменилось»), шаблоны и расписание, остановка сводки, лёгкие запросы окна во время сводки."""
import copy
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна
from test_ui_media import setup_ws

import fake_llm
from fake_llm import start_server

from opsdigest import schedule as S
from opsdigest.config import DEFAULTS, Config
from opsdigest.digest import DigestBuilder, PeriodError, resolve_period
from opsdigest.llm import make_client
from opsdigest.render import render_html, render_telegram
from opsdigest.storage import Chat, Message, Store, iso

UTC = timezone.utc


@pytest.fixture
def generic():
    fake_llm.GENERIC, fake_llm.CALLS[:] = True, []
    yield fake_llm.CALLS
    fake_llm.GENERIC = False


# ---------------------------------------------------------------- период
def test_resolve_period():
    now = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
    p = resolve_period({"period": "h3"}, UTC, now)
    assert p["since"] == now - timedelta(hours=3) and p["kind"] == "hours" and p["label"] == "3 ч"
    p = resolve_period({"period": "hours", "hours": 1, "minutes": 30}, UTC, now)
    assert p["minutes"] == 90 and p["label"] == "1 ч 30 мин"
    p = resolve_period({"period": "custom", "since": "2026-11-09T08:15", "until": "2026-11-09T12:45"}, UTC, now)
    assert (p["since"].hour, p["since"].minute, p["until"].hour, p["until"].minute) == (8, 15, 12, 45)
    p = resolve_period({"period": "custom", "since": "2026-11-08", "until": "2026-11-08"}, UTC, now)
    assert p["until"] - p["since"] == timedelta(days=1)   # «по 08.11» — весь день
    p = resolve_period({"period": "day", "since": "2026-11-01"}, UTC, now)   # старое окно: «с даты»
    assert p["kind"] == "custom" and p["since"].day == 1 and p["until"] == now
    assert resolve_period({"period": "week"}, UTC, now)["since"] == now - timedelta(days=7)
    for bad in ({"period": "hours", "hours": 0, "minutes": 2}, {"period": "custom"},
                {"period": "custom", "since": "2026-11-10T00:00"}, {"period": "custom", "since": "вчера"},
                {"period": "года"}):
        with pytest.raises(PeriodError):
            resolve_period(bad, UTC, now)


# ---------------------------------------------------------------- история сводок
def _cfg(tmp_path, url):
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"]["local"]["base_url"] = url
    data["profile"]["name"] = "Алексей"
    return Config(data, tmp_path)


def _msgs(store, chat, start_id, n, t0):
    store.add_messages([Message(chat, start_id + i, iso(t0 + timedelta(minutes=10 * i)), "Пётр", f"сервер {start_id + i} упал",
                                sender_id=5) for i in range(n)])


def test_history_reuse_chain_and_changes(tmp_path, generic):
    srv, url = start_server()
    cfg = _cfg(tmp_path, url)
    store = Store(":memory:")
    for c in (-1001, -1002, -1003):
        store.upsert_chat(Chat(c, f"Поддержка {abs(c)}", "group"))
    t0 = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    for c in (-1001, -1002, -1003):
        _msgs(store, c, 1, 12, t0)                      # 00:00–01:50
    llm = make_client(*cfg.llm_profile())

    def build(until, **kw):
        d = DigestBuilder(cfg, store, llm, progress=lambda s: None, **kw).build(t0 - timedelta(hours=1), until, "custom")
        store.save_digest(d["meta"]["period"], d["meta"]["since"], d["meta"]["until"], "t", d, "x.html")
        return d

    d1 = build(t0 + timedelta(hours=3))
    assert d1["meta"]["history"]["reused"] == 0 and len(generic) == 3 and not any(c["history"] for c in generic)
    # 1) то же самое ещё раз — модель не нужна: всё из прошлой сводки
    generic.clear()
    d2 = build(t0 + timedelta(hours=3, minutes=5))
    assert generic == [] and d2["meta"]["history"]["reused"] == 3
    assert {c["summary"] for c in d2["chats"]} == {c["summary"] for c in d1["chats"]}
    # 2) в одном чате новые сообщения — модель читает только их и прошлую сводку; остальные — из истории
    _msgs(store, -1002, 100, 3, t0 + timedelta(hours=4))
    generic.clear()
    d3 = build(t0 + timedelta(hours=5))
    assert len(generic) == 1 and generic[0]["chat"] == "Поддержка 1002" and generic[0]["history"]
    assert generic[0]["ids"] == [100, 101, 102]          # уже разобранные 1…12 заново не читаются
    assert d3["meta"]["history"]["chained"] == 1 and d3["meta"]["history"]["skipped_msgs"] == 12
    ch = next(c for c in d3["chats"] if c["title"] == "Поддержка 1002")
    assert ch["items"][0]["change"] == "updated" and {m for m in ch["items"][0]["msgs"]} <= {100, 101, 102}
    # 3) «что изменилось с прошлой сводки» — в общей картине и в отчёте
    assert d3["overview"]["changes"] and d3["meta"]["history"]["prev"]
    html = render_html(d3)
    assert "Что изменилось с прошлой сводки" in html and "изменилось" in html
    assert "Изменилось с прошлой сводки" in render_telegram(d3)
    # 4) без опоры на историю — читается всё
    generic.clear()
    build(t0 + timedelta(hours=5, minutes=10), history=False)
    assert len(generic) == 3 and sum(len(c["ids"]) for c in generic) == 39
    # 5) другие указания — прошлые сводки не годятся (могли опустить нужное)
    generic.clear()
    d5 = DigestBuilder(cfg, store, llm, progress=lambda s: None, extra="Только аварии").build(
        t0 - timedelta(hours=1), t0 + timedelta(hours=5, minutes=20), "custom")
    assert len(generic) == 3 and d5["meta"]["extra_prompt"] == "Только аварии"
    assert "Только аварии" in srv.calls[-1]["messages"][-1]["content"] or any(
        "Только аварии" in json.dumps(c) for c in srv.calls[-4:])


def test_only_selected_chats_and_cancel(tmp_path, generic):
    srv, url = start_server()
    cfg = _cfg(tmp_path, url)
    store = Store(":memory:")
    for c in (-1001, -1002):
        store.upsert_chat(Chat(c, f"Поддержка {abs(c)}", "group", priority="ignore" if c == -1002 else "normal"))
        _msgs(store, c, 1, 5, datetime(2026, 11, 9, tzinfo=UTC))
    llm = make_client(*cfg.llm_profile())
    since, until = datetime(2026, 11, 8, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC)
    d = DigestBuilder(cfg, store, llm, progress=lambda s: None).build(since, until, "custom", only={-1002})
    assert [c["title"] for c in d["chats"]] == ["Поддержка 1002"] and d["meta"]["chats_selected"] == 1  # исключённый, но выбран
    from opsdigest.digest import DigestCancelled
    with pytest.raises(DigestCancelled):
        DigestBuilder(cfg, store, llm, progress=lambda s: None, cancel=lambda: True, history=False).build(since, until)


# ---------------------------------------------------------------- расписание
def test_schedule_rules():
    e = S.normalize({"mode": "days", "days": [1, 2, 3, 4, 5], "times": "08:40, 13:00", "name": ""})
    assert e["name"] == "По будням в 08:40 и 13:00" and e["times"] == ["08:40", "13:00"]
    mon = datetime(2026, 11, 9, 8, 40, 20)
    assert S.due(e, mon, None) == datetime(2026, 11, 9, 8, 40)
    assert S.due(e, mon, datetime(2026, 11, 9, 8, 40)) is None              # уже запускали
    assert S.due(e, mon + timedelta(minutes=30), None) is None              # проспали — пропускаем молча
    assert S.due(e, mon, None, created=mon) is None                         # создано позже планового времени
    assert S.next_run(e, datetime(2026, 11, 14, 9, 0)) == datetime(2026, 11, 16, 8, 40)   # суббота → понедельник
    ev = S.normalize({"mode": "every", "days": [1, 2, 3, 4, 5, 6, 7], "every_min": 180, "from": "09:00", "to": "21:00"})
    assert S.describe(ev) == "Каждые 3 ч с 09:00 до 21:00, каждый день"
    assert S.next_run(ev, datetime(2026, 11, 9, 12, 1)) == datetime(2026, 11, 9, 15, 0)
    for bad in ({"days": [], "times": "8:40"}, {"days": [1], "times": "25:00"}, {"mode": "every", "days": [1], "every_min": 10},
                {"mode": "every", "days": [1], "every_min": 60, "from": "20:00", "to": "08:00"}):
        with pytest.raises(S.ScheduleError):
            S.normalize(bad)


def _fill(app, n_chats=40, n_msgs=4000):
    from opsdigest.service import open_store
    st = open_store(app.cfg())
    now = datetime.now(UTC)
    for c in range(n_chats):
        st.upsert_chat(Chat(-1000 - c, f"Чат {c}", "group"))
    st.add_messages([Message(-1000 - (i % n_chats), i + 1, iso(now - timedelta(minutes=(n_msgs - i) // 3)),
                             f"Человек {i % 50}", f"сообщение {i}", sender_id=100 + i % 50) for i in range(n_msgs)])
    st.kv_set("me", json.dumps({"id": 1, "name": "Я"}))
    st.close()


def test_presets_schedules_preview_and_stop(ui, generic):  # noqa: F811
    app, call, llm_url, tmp = ui
    setup_ws(call, tmp, llm_url)
    _fill(app)
    # чаты для выбора: сколько сообщений за период
    code, j = call("/api/digest/chats", {"period": "h3"})
    assert code == 200 and j["with_msgs"] == 40 and j["chats"][0]["n"] > 0
    ids = [c["id"] for c in j["chats"][:2]]
    # проверка перед запуском
    body = {"period": "hours", "hours": 2, "minutes": 30, "chat_ids": ids, "extra_prompt": "Опусти болтовню",
            "sources": {"mail": True}, "sync": False}
    code, pv = call("/api/digest/preview", body)
    assert code == 200 and pv["chats"]["selected"] == 2 and pv["chats"]["messages"] > 0
    assert pv["period"]["minutes"] == 150 and pv["extra_prompt"] == "Опусти болтовню"
    assert call("/api/digest/preview", {**body, "chat_ids": []})[1]["warnings"][0]["level"] == "bad"
    assert call("/api/digest/preview", {"period": "custom", "since": "2026-13-01"})[0] == 400
    # шаблон
    code, pr = call("/api/digest/presets/save", {"name": "Утро", "settings": {**body, "junk": 1}})
    assert code == 200 and pr["presets"][0]["settings"]["chat_ids"] == ids and "junk" not in pr["presets"][0]["settings"]
    pid = pr["id"]
    assert call("/api/digest/presets/save", {"name": "утро", "settings": body})[1]["id"] == pid   # то же имя — замена
    # расписание с этим шаблоном
    code, sc = call("/api/schedules/save", {"schedule": {"name": "Каждое утро", "days": [1, 2, 3, 4, 5, 6, 7],
                                                         "times": "08:40", "preset": pid, "send": False}})
    assert code == 200 and sc["schedules"][0]["preset_name"] == "утро" and sc["schedules"][0]["next"]
    sid = sc["id"]
    assert call("/api/digest/presets/delete", {"id": pid})[0] == 400        # шаблон занят расписанием
    # планировщик: время пришло — сводка по шаблону; повторно в ту же минуту — нет
    rec = next(x for x in app.raw()["digest"]["schedules"] if x["id"] == sid)
    planned = S.last_planned(rec, datetime.now() + timedelta(days=1))
    assert planned > datetime.fromisoformat(rec["created"])
    assert app.schedule_tick(planned + timedelta(seconds=30)) == [sid]
    job = next(j for j in app.jobs.values() if j.kind == "digest")
    assert job.title.startswith("По расписанию: Каждое утро")
    r = wait_job(call, job.id, 30)
    assert r["status"] == "done", r
    assert r["result"]["chats"] == 2   # выбранные в шаблоне чаты
    assert app.schedule_tick(planned + timedelta(seconds=50)) == []
    _w = time.time()
    while time.time() - _w < 5 and call("/api/schedules")[1]["schedules"][0]["state"].get("status") != "done":
        time.sleep(0.2)
    assert call("/api/schedules")[1]["schedules"][0]["state"]["status"] == "done"
    # «с прошлой сводки»
    code, pv = call("/api/digest/preview", {"period": "last"})
    assert code == 200 and pv["period"]["label"]
    # остановка сводки: медленная модель, кнопка «Остановить» — сразу «Остановлено», отчёт не пишется
    reports_before = len(call("/api/reports")[1]["reports"])
    fake_llm.DELAY = 3.0
    try:
        code, j = call("/api/digest", {"period": "h3", "sync": False, "history": False})
        jid = j["job"]
        time.sleep(0.5)
        run = call("/api/tasks")[1]["running"]
        assert any(x["id"] == jid and x["title"].startswith("Сводка за 3 ч") for x in run)   # видна в «Задачах»
        assert call("/api/ping")[1]["jobs"] == 1
        t0 = time.time()
        code, st = call(f"/api/jobs/{jid}/stop", {})
        assert code == 200 and st["stopped"]
        r = wait_job(call, jid, 10)
        assert r["status"] == "error" and r["stopped"] and r["error"].startswith("Остановлено")
        assert time.time() - t0 < 2
        time.sleep(4)   # поток сводки доработал после обрыва — отчёт так и не записан
        assert len(call("/api/reports")[1]["reports"]) == reports_before
    finally:
        fake_llm.DELAY = 0.0
    assert call("/api/ping")[1]["jobs"] == 0


def test_window_queries_use_indexes():
    """Список чатов, люди, описания: запросы по чату или автору без source идут по индексу, а не по всей таблице
    (без индекса список 800 чатов на 300 тыс. сообщений собирался 34 с — всё это время база была занята)."""
    st = Store(":memory:")
    for sql, args in (("SELECT COUNT(DISTINCT sender_id) FROM messages WHERE chat_id=? AND is_out=0", (1,)),
                      ("SELECT * FROM messages WHERE chat_id=? AND msg_id>? ORDER BY msg_id DESC LIMIT 10", (1, 5)),
                      ("SELECT sender, COUNT(*) FROM messages WHERE sender_id=?", (5,))):
        plan = " ".join(str(tuple(r)) for r in st.db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, (sql, plan)
