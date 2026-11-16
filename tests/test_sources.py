"""Источники через Maverick (почта, ITSM, вики): проверка подключения, кеш, общая картина в два слоя
(сырая переписка Telegram не встречается с другими источниками), автономный глоссарий, ассистент, окно."""
import copy
import json
from datetime import timezone
from pathlib import Path

import pytest
import fake_llm
from fake_llm import start_server
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна

from opsdigest import assistant as A
from opsdigest import cross, glossary_auto as GA, plugins
from opsdigest.config import DEFAULTS, Config
from opsdigest.demo import load_demo
from opsdigest.digest import DigestBuilder
from opsdigest.glossary import Glossary
from opsdigest.llm import make_client
from opsdigest.storage import Store


@pytest.fixture
def env():
    fake_llm.PLUGINS.update({"mail": True, "itsm": True, "wiki": True})
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "api_key": "x", "model": "fake",
                                         "external": True},
                               "maverick": {"type": "maverick", "url": host, "api_key": "sk_test"}}
    data["llm"]["provider"] = "local"
    data["privacy"] = {"pseudonymize": "all"}   # поддельная модель — на localhost; проверяем обезличивание и для неё
    cfg = Config(data, Path("."))
    st = Store(":memory:")
    yield srv, cfg, st
    fake_llm.PLUGINS.update({"mail": True, "itsm": True, "wiki": True})


def mav_calls(srv):
    return [c.get("message") or "" for c in srv.calls if isinstance(c, dict) and "message" in c]


def test_parse_check():
    assert plugins.parse_check('{"connected": true, "sample": "02.11"}')[0] == "ok"
    st, why = plugins.parse_check('{"connected": false, "reason": "войдите: https://x/y"}')
    assert st == "offline" and "https://x/y" in why
    assert plugins.parse_check("Плагин не подключён, требуется авторизация")[0] == "offline"
    assert plugins.parse_check("")[0] == "offline"
    assert plugins.parse_check("Последнее письмо — 02.11.2026")[0] == "ok"


def test_check_offline_and_cache(env):
    srv, cfg, st = env
    s = plugins.check(cfg, st, "mail")
    assert s["state"] == "ok"
    n = len(srv.calls)
    assert plugins.check(cfg, st, "mail")["state"] == "ok" and len(srv.calls) == n   # помнит удачную проверку
    sent = mav_calls(srv)[-1]
    assert "плагин почты" in sent and "Не используй инструменты" not in sent     # вводная для плагинов, не «без инструментов»
    fake_llm.PLUGINS["mail"] = False
    s = plugins.check(cfg, st, "mail", force=True)
    assert s["state"] == "offline" and s["links"] == ["https://maverick.local/connect/mail"]
    with pytest.raises(plugins.PluginOffline) as e:
        plugins.need(cfg, st, "mail")
    assert "переподключите" in str(e.value)
    assert plugins.statuses(cfg, st)["mail"]["state"] == "offline"
    # ни в одном запросе нет логинов/паролей — программа их не передаёт
    assert not any("парол" in m.lower() for m in mav_calls(srv))


def test_itsm_lookup_cache_and_numbers(env):
    srv, cfg, st = env
    assert plugins.ticket_numbers(cfg, "см. INS0012345, PM0001234 и INC-0099999, задача BILL-482") == \
        ["INS0012345", "INC-0099999", "PM0001234"]
    items = plugins.itsm_lookup(cfg, st, ["CHG0003318", "INC0000001"], progress=lambda s: None)
    by = {x["ref"]: x for x in items}
    assert by["CHG0003318"]["found"] and by["CHG0003318"]["status"] == "Откачено" and not by["INC0000001"]["found"]
    n = len(srv.calls)
    plugins.itsm_lookup(cfg, st, ["CHG0003318", "INC0000001"], progress=lambda s: None)
    assert len(srv.calls) == n   # из кеша, без запроса
    assert plugins.search_items(st, ["auth-svc"])[0]["ref"] == "CHG0003318"


def test_cross_two_layers(env):
    srv, cfg, st = env
    since, until = load_demo(cfg, st, timezone.utc)
    llm = make_client(*cfg.llm_profile())
    data = DigestBuilder(cfg, st, llm, progress=lambda s: None).build(since, until, "day")
    cfg.data["digest"]["sources"] = {"mail": True, "itsm": True, "wiki": True}
    log = []
    cross.apply(cfg, st, llm, data, since, until, progress=log.append)
    cr = data["cross"]
    assert cr["threads"] and cr["threads"][0]["mail_refs"][0]["subject"].startswith("Итоги аварии")
    assert any(x["ref"] == "CHG0003318" for x in cr["itsm"]) and cr["mail"] and not cr["offline"]
    assert cr["mail_in_tg"]  # почта → Telegram: письмо про CHG0003318 найдено в чатах (по базе, без модели)
    # слой 2: в Maverick — только очищенные пункты: ни текста сообщений, ни имён, ни личной переписки
    raw = [m.text for m in st.messages(-1001000000001, "2000", "3000")] + \
          [m.text for m in st.messages(500000001, "2000", "3000")]
    mav = "\n".join(mav_calls(srv))
    assert not any(t[:50] in mav for t in raw if len(t) > 50)
    assert "Ирина Ковалёва" not in mav and "Алексей Смирнов" not in mav
    pts, back = cross.points(data, cfg, plugins.number_patterns(cfg))
    assert pts and not any(p.get("chat", "").startswith("Алексей Смирнов") for p in pts)
    assert not any("Алексей Смирнов" in (b.get("chat") or "") for b in back.values())
    assert all(plugins.ticket_numbers(cfg, n) for p in pts for n in p.get("numbers") or [])  # только номера ITSM
    # сведение — одним запросом к основной модели, тоже без сырой переписки и с кодами вместо имён
    merge = next(c for c in srv.calls if "messages" in c and "очищенные данные за период из нескольких" in c["messages"][-1]["content"])
    body = merge["messages"][-1]["content"]
    assert "Ирина Ковалёва" not in body and not any(t[:50] in body for t in raw if len(t) > 50)
    from opsdigest.render import render_html
    html = render_html(data)
    assert "Общая картина" in html and "Итоги аварии ЛК" in html and "CHG0003318" in html
    # плагин почты «забылся» — предупреждение в отчёте, сводка Telegram цела
    fake_llm.PLUGINS["mail"] = False
    st.kv_set("plugin_status", "{}")
    data2 = DigestBuilder(cfg, st, llm, progress=lambda s: None).build(since, until, "day")
    cross.apply(cfg, st, llm, data2, since, until, progress=log.append)
    assert "mail" in data2["cross"]["offline"] and any("переподключите" in e for e in data2["meta"]["errors"])
    assert data2["chats"] and "плагин Maverick не подключён" in render_html(data2)


def test_glossary_queue_and_process(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    n = GA.note(cfg, st, "После релиза KeyCloak упал SSO, в Grafana видно 502. Иван, глянь INC0012345.", "telegram", "-1:5")
    q = {x["term"]: x for x in GA.queue(st)}
    assert n >= 3 and {"KeyCloak", "SSO", "Grafana"} <= set(q) and "INC0012345" not in q
    GA.note(cfg, st, "KeyCloak снова тормозит", "mail")
    k = next(x for x in GA.queue(st) if x["term"] == "KeyCloak")
    assert k["count"] == 2 and k["sources"] == {"telegram": 1, "mail": 1} and len(k["contexts"]) == 2
    # известное глоссарию — не в очередь
    Glossary(st).save({"term": "Zabbix", "summary": "мониторинг"})
    GA.note(cfg, st, "Zabbix ругается", "telegram")
    assert "Zabbix" not in {x["term"] for x in GA.queue(st)}
    llm = make_client(*cfg.llm_profile())
    res = GA.process(cfg, st, llm, keys=["KeyCloak", "SSO", "Grafana"], progress=lambda s: None)
    assert res["done"] + res["questions"] + res["ignored"] == 3 and res["questions"] == 1 and res["ignored"] == 1
    c = GA.counts(st)
    assert c.get("done") == 1 and c.get("question") == 1 and c.get("ignored") == 1
    e = next(x for x in Glossary(st).all() if x["source"] == "auto")
    assert e["status"] == "draft" and e["examples"] and e["links"][0]["url"].startswith("https://wiki.local/")
    assert any("Что такое" in x["question"] for x in Glossary(st).questions())


def test_assistant_everywhere(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    llm = make_client(*cfg.llm_profile())
    tid = A.create(st, "Новая беседа")
    r = A.answer(cfg, st, llm, tid, "Что с BILL-482 и автоплатежом?",
                 opts={"sources": {"telegram": True, "mail": True, "itsm": True, "wiki": True}}, progress=lambda s: None)
    assert r["text"] and r["found"]["telegram"] > 0 and r["found"]["mail"] == 1 and r["found"]["wiki"] == 1
    t = A.thread(st, tid)
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"] and t["title"].startswith("Что с BILL-482")
    # слой 1: переписка — отдельным запросом, в итоговый запрос и в Maverick идут только выводы
    oa = [c["messages"][-1]["content"] for c in srv.calls if "messages" in c]
    assert any("Выпиши из переписки Telegram" in x for x in oa)
    final = oa[-1]
    assert "Что нашлось в Telegram (выводы по переписке)" in final and "=== чат «" not in final
    assert not any("=== чат «" in m for m in mav_calls(srv))
    # выделенное в мессенджере → беседа «по выделенному», в истории видно
    tid2 = A.create(st, "x", "selection", {"chat": 500000001, "ids": [1019], "quote": "двойные списания"})
    A.answer(cfg, st, llm, tid2, "Было ли такое раньше?", opts={"sources": {"telegram": True}}, progress=lambda s: None)
    kinds = {x["kind"] for x in A.threads(st)}
    assert kinds == {"free", "selection"}
    ev = A.everywhere(cfg, st, "двойные списания BILL-482", chat_id=500000001,
                      sources={"itsm": True, "mail": False, "wiki": False}, progress=lambda s: None)
    assert ev["telegram"]["total"] >= 1 and ev["itsm"][0]["ref"] == "INC0099999"


def test_ui_modules_sources_assistant(ui, tmp_path):  # noqa: F811
    app, call, llm_url, _ = ui
    fake_llm.PLUGINS.update({"mail": True, "itsm": True, "wiki": True})
    host = llm_url.rsplit("/v1", 1)[0]
    # разделы и тема — до выбора папки (app.json)
    code, u = call("/api/ui")
    assert code == 200 and u["modules"] == {"mx": False, "digest": True, "asst": True, "gl": True} and u["theme"] == "auto"
    call("/api/ui", {"modules": {"mx": True, "gl": False}, "theme": "dark"})
    u = call("/api/state")[1]["ui"]
    assert u["modules"]["mx"] and not u["modules"]["gl"] and u["theme"] == "dark"
    call("/api/workspace/set", {"path": str(tmp_path / "ws")})
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "fake", "ctx": 32768}})
    code, p = call("/api/plugins")
    assert code == 200 and p["available"] and p["status"]["mail"]["state"] == "unknown"
    code, r = call("/api/config/sources", {"settings": {"itsm": {"enabled": True, "name": "ITSM Bot",
                                                                 "body": '{"plugins": ["itsm"]}'}},
                                           "digest_sources": {"mail": True, "itsm": True, "wiki": False}})
    assert code == 200
    assert call("/api/config/sources", {"settings": {"mail": {"body": "{не json"}}})[0] == 400
    code, r = call("/api/plugins/check", {})
    j = wait_job(call, r["job"])
    assert j["status"] == "done" and j["result"]["status"]["itsm"]["state"] == "ok"
    p = call("/api/plugins")[1]
    assert p["digest_sources"]["mail"] and p["settings"]["itsm"]["name"] == "ITSM Bot"
    # карточка одного источника сохраняется отдельно (окно сохраняет её само после ввода), остальное не трогается
    code, r = call("/api/config/sources", {"settings": {"mail": {"enabled": True, "name": "Outlook", "hint": "только Входящие",
                                                                 "body": "plugins: outlook, calendar"}}})
    assert code == 200
    p = call("/api/plugins")[1]
    assert p["settings"]["mail"]["name"] == "Outlook" and p["settings"]["mail"]["hint"] == "только Входящие"
    assert json.loads(p["settings"]["mail"]["body"]) == {"plugins": ["outlook", "calendar"]}
    assert p["settings"]["itsm"]["name"] == "ITSM Bot" and p["digest_sources"]["mail"]
    assert app.cfg().get("plugins")["mail"]["body"] == {"plugins": ["outlook", "calendar"]}
    # ассистент: новая беседа, вопрос, история
    code, r = call("/api/asst/new", {"title": "Новая беседа"})
    tid = r["id"]
    code, r = call("/api/asst/ask", {"thread": tid, "question": "Что по CHG0003318?", "sources": {"itsm": True}})
    j = wait_job(call, r["job"], 20)
    assert j["status"] == "done", j
    assert j["result"]["found"]["itsm"] >= 1
    t = call(f"/api/asst/thread?id={tid}")[1]["thread"]
    assert len(t["messages"]) == 2 and t["messages"][1]["data"]["ext"][0]["ref"] == "CHG0003318"
    assert call("/api/asst/threads")[1]["threads"][0]["id"] == tid
    # очередь глоссария
    code, gq = call("/api/glossary/queue")
    assert code == 200 and "items" in gq
    # в ITSM ушло поле из настроек источника
    assert any(c.get("plugins") == ["itsm"] for c in fake_llm_calls(app))


def fake_llm_calls(app):
    import gc
    srvs = [o for o in gc.get_objects() if type(o).__name__ == "HTTPServer" and hasattr(o, "calls")]
    return [c for s in srvs for c in s.calls if isinstance(c, dict)]
