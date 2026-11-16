"""Сессия 14: сводка «От источника» — очередь ITSM → задачи трекера → Telegram и почта по номерам → отчёт."""
import copy
import time
import json
from datetime import datetime, timezone

import pytest
import fake_llm
import fake_source
from fake_llm import start_server

from opsdigest import plugins
from opsdigest import source_digest as SD
from opsdigest.config import DEFAULTS, Config
from opsdigest.demo import load_demo
from opsdigest.llm import make_client
from opsdigest.render import render_html, render_telegram
from opsdigest.render.markdown import render_markdown
from opsdigest.storage import Store

Q = lambda s: None  # noqa: E731


@pytest.fixture
def env(tmp_path):
    fake_llm.PLUGINS.update({k: True for k in plugins.KINDS})
    fake_source.CALLS.clear()
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "api_key": "x", "model": "fake",
                                         "external": True},
                               "maverick": {"type": "maverick", "url": host, "api_key": "sk_test"}}
    data["llm"]["provider"] = "local"
    data["privacy"] = {"pseudonymize": "all"}
    cfg = Config(data, tmp_path)
    st = Store(":memory:")
    load_demo(cfg, st, timezone.utc)
    yield srv, cfg, st
    fake_llm.PLUGINS.update({k: True for k in plugins.KINDS})


def test_parse_date_and_query():
    assert SD.parse_date("05.11.2026").day == 5 and SD.parse_date("2026-11-05T10:00:00+03:00").hour == 10
    assert SD.parse_date("5.11.26 9:30").minute == 30 and SD.parse_date("") is None and SD.parse_date("вчера") is None
    q = SD.norm_query("itsm", {"group": "L2-Биллинг", "types": ["incident", "request", "nope"], "state": "x", "limit": 9999})
    assert q["types"] == ["incident", "request"] and q["state"] == "open" and q["limit"] == 300
    assert "группа «L2-Биллинг»" in SD.query_text("itsm", q)
    lk = SD.norm_look({"days": -5, "mail": False})
    assert lk["days"] == 1 and lk["telegram"] and not lk["mail"] and lk["second"]


def test_itsm_queue_full_chain(env):
    srv, cfg, st = env
    cfg.data["digest"]["source"] = {"page_size": 2}
    llm = make_client("local", cfg.llm_profile("local")[1])
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    data = SD.build(cfg, st, llm, start="itsm", query={"group": "L2-Биллинг", "types": ["incident"]},
                    look={"telegram": True, "mail": True, "days": 3650}, extra="Сколько записей ждут релиза?",
                    progress=Q, now=now)
    src = data["source"]
    # 1. список порциями по 2 — две страницы, все 4 записи
    assert [c for c in fake_source.CALLS if c[0] == "list"] == [("list", 1, 2), ("list", 3, 4)]
    assert [it["ref"] for it in src["items"]] == list(fake_source.QUEUE)
    # 2. подробно — только те, у кого в списке не было журнала (у INC0048300 был)
    det = [n for c in fake_source.CALLS if c[0] == "itsm" for n in c[1]]
    assert set(det) == {"INC0048190", "INC0048201", "INC0048207"}
    # 3. задачи трекера из полей и комментариев
    keys = {k for c in fake_source.CALLS if c[0] == "tracker" for k in c[1]}
    assert keys == {"BILL-482", "BILL-470", "OPS-12"}
    sec = {s["ref"]: s for s in src["secondary"]}
    assert sec["BILL-482"]["used_by"] == ["INC0048190", "INC0048201"] and sec["BILL-470"]["closed"]
    assert not sec["OPS-12"]["found"]
    by = {it["ref"]: it for it in src["items"]}
    # 4. Telegram: номера из демо-переписки найдены, выводы — отдельным запросом без данных ITSM
    assert by["INC0048190"]["tg"]["discussed"] and by["INC0048190"]["tg"]["links"]
    tg_req = [c["messages"][-1]["content"] for c in srv.calls if isinstance(c, dict) and "messages" in c
              and "выдержки из рабочих чатов Telegram" in c["messages"][-1]["content"]]
    assert tg_req and "ожидание релиза" not in tg_req[0] and "Двойное списание при автоплатеже" not in tg_req[0]
    assert "BILL-482" in tg_req[0]
    # 5. почта по номерам
    assert by["INC0048190"]["ext"]["mail"][0]["title"].startswith("INC0048190")
    # 6. разбор моделью и флаги программы
    assert by["INC0048201"]["verdict"]["expects"] == "support"
    assert any("BILL-470 уже закрыта" in f for f in by["INC0048207"]["code_flags"])
    assert any("ждут от поддержки" in f for f in by["INC0048201"]["code_flags"])
    assert any("нет движения" in f for f in by["INC0048201"]["code_flags"])
    assert any("не найдена в трекере" in f for f in by["INC0048201"]["code_flags"])
    st_ = src["stats"]
    assert st_["total"] == 4 and st_["waiting"] == 3 and ["BILL-482", 2] in st_["shared"]
    assert dict(st_["by_expects"])["поддержка (мы)"] == 2
    # 7. общая картина: группы только из настоящих номеров, ответ на указания
    assert src["groups"][0]["refs"] == ["INC0048190", "INC0048201"] and src["answer"] == "Релиза ждут 2 записи."
    assert src["close"][0]["ref"] == "INC0048207" and src["attention"]
    assert ("reduce", True) in fake_source.CALLS
    # обезличивание: имена из демо-переписки не ушли в модель
    sent = " ".join(c["messages"][-1]["content"] for c in srv.calls if isinstance(c, dict) and "messages" in c)
    assert "Ольга Петрова" not in sent
    # отчёты
    html = render_html(data)
    assert "Разбор очереди" in html and "id='r-INC0048190'" in html and "Ждут от нас" in html and "BILL-470" in html
    assert "Релиза ждут 2 записи." in html
    md = render_markdown(data)
    assert "| INC0048207 |" in md and "## Что сделать" in md
    tg = render_telegram(data)
    assert "Разбор очереди" in tg and "INC0048207" in tg and len(tg) < 4000
    json.dumps(data, ensure_ascii=False)


def test_map_missing_item_and_offline_sources(env):
    srv, cfg, st = env
    fake_llm.PLUGINS["mail"] = False
    cfg.data["plugins"] = {"rooms": {"enabled": False}}
    llm = make_client("local", cfg.llm_profile("local")[1])
    data = SD.build(cfg, st, llm, start="itsm", query={"group": "L2-Биллинг", "text": "НЕ_ОТВЕЧАТЬ"},
                    look={"telegram": False, "mail": True, "rooms": True, "second": False}, progress=Q)
    src = data["source"]
    assert src["offline"]["mail"]["state"] == "offline" and src["offline"]["rooms"]["state"] == "disabled"
    assert not [c for c in fake_source.CALLS if c[0] == "tracker"]  # второй источник выключен
    by = {it["ref"]: it for it in src["items"]}
    assert by["INC0048300"]["verdict"].get("no_model") and by["INC0048190"]["verdict"]["actual"] == "yes"
    assert not any(c[0] == "tg" for c in fake_source.CALLS)
    assert "не подключён" in render_html(data)


def test_start_offline_and_tracker_start(env):
    srv, cfg, st = env
    llm = make_client("local", cfg.llm_profile("local")[1])
    fake_llm.PLUGINS["itsm"] = False
    with pytest.raises(SD.SourceError):
        SD.build(cfg, st, llm, start="itsm", query={}, look={}, progress=Q)
    fake_llm.PLUGINS["itsm"] = True
    plugins.check(cfg, st, "itsm", force=True)
    data = SD.build(cfg, st, llm, start="tracker", query={"group": "BILL"}, look={"telegram": True, "mail": False},
                    progress=Q)
    src = data["source"]
    assert [it["ref"] for it in src["items"]] == ["BILL-482", "BILL-470"]
    sec = {s["ref"]: s for s in src["secondary"]}
    assert set(sec) == {"INC0048190", "INC0048201", "INC0048207"} and sec["INC0048207"]["used_by"] == ["BILL-470"]
    b470 = next(it for it in src["items"] if it["ref"] == "BILL-470")
    assert any("инцидент INC0048207 ещё открыт" in f for f in b470["code_flags"])
    assert "Инциденты ITSM" in render_html(data)


# ---------------------------------------------------------------- окно
from test_ui import ui, wait_job  # noqa: E402,F401 — фикстура окна


def _ws(app, call, tmp, llm_url):
    host = llm_url.rsplit("/v1", 1)[0]
    call("/api/workspace/set", {"path": str(tmp / "ws")})
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    code, _ = call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "fake", "ctx": 32768}})
    assert code == 200
    from opsdigest.service import open_store
    st = open_store(app.cfg())
    load_demo(app.cfg(), st, timezone.utc)
    st.close()


def test_window_source_mode(ui):  # noqa: F811
    from datetime import datetime as dt, timedelta
    from opsdigest import schedule as S
    app, call, llm_url, tmp = ui
    fake_llm.PLUGINS.update({k: True for k in plugins.KINDS})
    fake_source.CALLS.clear()
    _ws(app, call, tmp, llm_url)
    body = {"mode": "source", "start": "itsm", "query": {"group": "L2-Биллинг", "types": ["incident"]},
            "look": {"telegram": True, "mail": True, "days": 3650}, "sync": False, "extra_prompt": "Сколько ждут релиза?"}
    # проверка перед запуском
    code, pv = call("/api/digest/preview", body)
    assert code == 200 and pv["mode"] == "source" and pv["query_text"].startswith("инциденты")
    assert set(pv["sources"]) == {"itsm", "tracker", "mail", "telegram"} and pv["tg_messages"] > 0
    assert pv["sources"]["itsm"]["role"] == "откуда берём список"
    assert call("/api/digest/preview", {**body, "query": {}})[1]["warnings"][-1]["text"].startswith("Условия не заданы")
    assert call("/api/digest", {**body, "start": "nope"})[0] == 400
    # разбор
    code, j = call("/api/digest", body)
    assert code == 200
    assert app.jobs[j["job"]].title == "Разбор очереди: ITSM «L2-Биллинг»" and app.jobs[j["job"]].info["mode"] == "source"
    r = wait_job(call, j["job"], 30)
    assert r["status"] == "done", r
    res = r["result"]
    assert res["mode"] == "source" and res["source"]["items"] == 4 and res["source"]["attention"] >= 2
    code, html = call(f"/reports/{res['report']}", raw=True)
    assert code == 200 and "Разбор очереди".encode() in html and "Релиза ждут 2 записи.".encode() in html
    # в списке отчётов и в истории ассистента
    assert any(x.get("name") == res["report"] or res["report"] in json.dumps(x, ensure_ascii=False)
               for x in call("/api/reports")[1]["reports"])
    # «с прошлой сводки» не опирается на разбор очереди
    assert call("/api/digest/preview", {"period": "last"})[0] == 400
    # шаблон и расписание
    code, pr = call("/api/digest/presets/save", {"name": "Очередь L2", "settings": {**body, "junk": 1, "chat_ids": [1]}})
    assert code == 200
    p = next(x for x in pr["presets"] if x["id"] == pr["id"])
    assert p["settings"]["mode"] == "source" and p["settings"]["query"]["group"] == "L2-Биллинг"
    assert "chat_ids" not in p["settings"] and p["text"].startswith("от источника: ITSM")
    code, sc = call("/api/schedules/save", {"schedule": {"name": "Очередь утром", "days": [1, 2, 3, 4, 5, 6, 7],
                                                         "times": "09:10", "preset": pr["id"], "send": False}})
    assert code == 200
    rec = next(x for x in app.raw()["digest"]["schedules"] if x["id"] == sc["id"])
    planned = S.last_planned(rec, dt.now() + timedelta(days=1))
    assert app.schedule_tick(planned + timedelta(seconds=30)) == [sc["id"]]
    job = next(x for x in app.jobs.values() if x.kind == "digest" and x.title.startswith("По расписанию"))
    r = wait_job(call, job.id, 30)
    assert r["status"] == "done" and r["result"]["mode"] == "source", r
    # плагин стартового источника не подключён — понятная ошибка
    fake_llm.PLUGINS["itsm"] = False
    call("/api/plugins/check", {"kind": "itsm"})
    time.sleep(0.5)
    code, j = call("/api/digest", body)
    r = wait_job(call, j["job"], 30)
    assert r["status"] == "error" and "не подключён" in r["error"], r
    fake_llm.PLUGINS["itsm"] = True


def test_group_name_not_pseudonymized(env):
    """Название группы ITSM и служебные слова не кодируются как люди (09.11: «группа» → Ч568, группа → коды)."""
    from opsdigest.storage import Message, iso
    srv, cfg, st = env
    now = datetime.now(timezone.utc)
    st.add_messages([Message(-1001000000003, 5001, iso(now), "Поддержка МП", "смена приняла", sender_id=901),
                     Message(-1001000000003, 5002, iso(now), "Группа мониторинга", "алерт снят", sender_id=902),
                     Message(-1001000000003, 5003, iso(now), "Service Desk", "ок", sender_id=903)])
    llm = make_client("local", cfg.llm_profile("local")[1])
    fake_llm.PLUGIN_CALLS.clear()
    SD.build(cfg, st, llm, start="itsm", query={"group": "Service Desk"}, look={"telegram": False, "mail": False},
             progress=Q)
    sent = next(u for k, u in fake_llm.PLUGIN_CALLS if "Это разбор очереди" in u)
    assert "«Service Desk»" in sent and "группа исполнителей" in sent and "поддержка | разработка" in sent
    assert "Ольга Петрова" not in " ".join(u for _k, u in fake_llm.PLUGIN_CALLS)  # люди по-прежнему кодируются
