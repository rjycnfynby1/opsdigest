"""Maverick Agent Core и выбор папки внутри окна."""
import copy
from pathlib import Path
import json
import urllib.parse
from datetime import timezone

import pytest
from fake_llm import start_server
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна

from opsdigest.config import DEFAULTS, Config
from opsdigest.demo import load_demo
from opsdigest.digest import DigestBuilder
from opsdigest.llm import LLMError, make_client
from opsdigest.storage import Store


def test_maverick_provider_digest():
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    llm = make_client("maverick", {"type": "maverick", "url": host + "/api/v1", "api_key": "sk_test"})
    assert llm.endpoint() == host + "/api/v1/query"
    llm.check_key()
    assert llm.complete("Отвечай одним словом.", "Ответь словом: готов") == "готов"
    sent = srv.calls[-1]["message"]
    assert sent.startswith("Это служебная задача") and "Ответь словом" in sent
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"] = {"maverick": {"type": "maverick", "url": host, "api_key": "sk_test"}}
    data["llm"]["provider"] = "maverick"
    cfg = Config(data, Path("."))
    store = Store(":memory:")
    since, until = load_demo(cfg, store, timezone.utc)
    d = DigestBuilder(cfg, store, make_client(*cfg.llm_profile()), progress=lambda s: None).build(since, until, "day")
    assert not d["meta"]["errors"] and len(d["overview"]["top"]) == 7


def test_maverick_errors_are_clear():
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    bad = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_wrong"})
    bad.retries = 0
    with pytest.raises(LLMError) as e:
        bad.complete("s", "u")
    assert "401" in str(e.value) and "ключ" in str(e.value)
    # OpenAI-совместимый режим на Maverick даёт 404 — с подсказкой про адрес
    oa = make_client("m", {"type": "openai", "base_url": host + "/api/v1", "api_key": "x", "model": "m"})
    oa.retries = 0
    with pytest.raises(LLMError) as e:
        oa.complete("s", "u")
    assert "404" in str(e.value) and "адрес" in str(e.value)
    with pytest.raises(LLMError):
        make_client("maverick", {"type": "maverick", "url": host})


def test_ui_maverick_and_folders(ui, tmp_path):  # noqa: F811
    app, call, llm_url, _ = ui
    host = llm_url.rsplit("/v1", 1)[0]
    call("/api/workspace/set", {"path": str(tmp_path / "ws")})
    code, e = call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": ""}})
    assert code == 400 and "sk_" in e["error"]
    assert call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host + "/api/v1", "token": "sk_wrong"}})[0] == 200
    code, e = call("/api/llm/test", {})
    assert code == 400 and "ключ не принят" in e["error"]
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    code, t = call("/api/llm/test", {})
    assert code == 200 and t["answer"] == "готов"
    cv = call("/api/config")[1]["llm"]["maverick"]
    assert cv["style"] == "agent" and cv["token_set"] and "sk_test" not in json.dumps(cv)
    # ключ сохраняется, если поле оставили пустым
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": ""}})
    assert call("/api/llm/test", {})[0] == 200
    # выбор папки
    code, r = call("/api/fs/list?path=")
    assert code == 200 and r["places"] and r["path"] == ""
    code, r = call("/api/fs/list?path=" + urllib.parse.quote(str(tmp_path)))
    assert code == 200 and any(e["name"] == "ws" for e in r["entries"]) and r["parent"]
    code, r = call("/api/fs/mkdir", {"path": str(tmp_path), "name": "Новая папка"})
    assert code == 200 and (tmp_path / "Новая папка").is_dir()
    assert call("/api/fs/mkdir", {"path": str(tmp_path), "name": "a/b"})[0] == 400
    assert call("/api/fs/list?path=" + urllib.parse.quote(str(tmp_path / "нет")))[0] == 400


def test_ui_demo_digest_without_telegram(ui, tmp_path):  # noqa: F811
    app, call, llm_url, _ = ui
    host = llm_url.rsplit("/v1", 1)[0]
    call("/api/workspace/set", {"path": str(tmp_path / "ws")})
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    code, r = call("/api/demo", {})
    j = wait_job(call, r["job"])
    assert j["status"] == "done", j
    assert j["result"]["top"] == 7 and j["result"]["report"].endswith("_demo.html")
    assert call("/api/reports")[1]["reports"][0]["period"] == "demo"


@pytest.mark.parametrize("openapi", [True, False])
def test_maverick_discovers_text_field(openapi):
    from opsdigest.llm.providers import field_from_openapi
    srv, url = start_server()
    srv.mav_field, srv.mav_openapi = "text", openapi
    host = url.rsplit("/v1", 1)[0]
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test"})
    llm.retries = 0
    assert llm.complete("Отвечай одним словом.", "Ответь словом: готов") == "готов"
    assert llm.discovered == "text" and llm.profile["body_field"] == "text"
    assert srv.calls[-1]["text"].startswith("Это служебная задача")
    # дальше сразу правильное поле, без повторного поиска
    n = len(srv.calls)
    llm.complete("s", "Ответь словом: готов")
    assert len(srv.calls) == n + 1
    assert field_from_openapi({"paths": {"/api/v1/query": {"post": {"requestBody": {"content": {"application/json": {
        "schema": {"properties": {"prompt": {"type": "string"}}}}}}}}}}) == "prompt"


def test_ui_saves_discovered_field(ui, tmp_path):  # noqa: F811
    app, call, llm_url, _ = ui
    import fake_llm
    srv, url = fake_llm.start_server()
    srv.mav_field = "query"
    host = url.rsplit("/v1", 1)[0]
    call("/api/workspace/set", {"path": str(tmp_path / "ws")})
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    code, t = call("/api/llm/test", {})
    assert code == 200 and "«query»" in t["answer"]
    cfg = json.loads((tmp_path / "ws" / "config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["profiles"]["maverick"]["body_field"] == "query"
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": ""}})
    cfg = json.loads((tmp_path / "ws" / "config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["profiles"]["maverick"]["body_field"] == "query"
