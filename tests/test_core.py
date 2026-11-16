import os
from pathlib import Path

import pytest

from opsdigest.config import load_config
from opsdigest.entities import EntityExtractor
from opsdigest.llm.base import extract_json, strip_think
from opsdigest.ask import fts_query
from opsdigest.sources.tg_proxy import ProxyError, collect_proxies, find_links, parse_proxy, telethon_kwargs
from opsdigest.storage import Message, Store


def write_cfg(tmp_path: Path, text: str, env: str = "") -> Path:
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    return tmp_path / "config.toml"


def test_config_env_and_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("T_HASH", raising=False)
    p = write_cfg(tmp_path, '''
[telegram]
api_id = "${T_ID}"
api_hash = "${T_HASH}"
[[telegram.chats]]
match = "Авария"
priority = "high"
kind = "incident"
[[telegram.chats]]
match = "@alerts_prod"
kind = "alerts"
[[telegram.chats]]
match = "re:^Флуд"
priority = "ignore"
''', "T_ID=12345\nT_HASH='abc'\n")
    cfg = load_config(p)
    assert cfg.telegram["api_id"] == "12345" and cfg.telegram["api_hash"] == "abc"
    r = cfg.chat_rule("АВАРИЯ: ЛК недоступен", None, -100, "group")
    assert r["priority"] == "high" and r["kind"] == "incident" and r["explicit"]
    assert cfg.chat_rule("Алерты", "Alerts_Prod", -1, "channel")["kind"] == "alerts"
    assert cfg.chat_rule("Флудилка", None, -2, "group")["priority"] == "ignore"
    d = cfg.chat_rule("Иван", None, 5, "private")
    assert d["priority"] == "high" and not d["explicit"]
    assert cfg.chat_rule("Канал", None, -3, "channel")["priority"] == "low"
    name, prof = cfg.llm_profile()
    assert name == "local" and prof["base_url"].endswith(":1235/v1")


def test_example_config_parses(monkeypatch):
    root = Path(__file__).resolve().parent.parent
    cfg = load_config(root / "tools" / "config.example.toml")
    assert cfg.profile["competences"]
    for name in cfg["llm"]["profiles"]:
        cfg.llm_profile(name)


def test_proxy_links():
    plain = parse_proxy("tg://proxy?server=1.2.3.4&port=443&secret=00112233445566778899aabbccddeeff")
    assert plain.mode == "mtproto" and plain.port == 443
    dd = parse_proxy("https://t.me/proxy?server=p.example.com&port=8443&secret=dd00112233445566778899aabbccddeeff")
    assert dd.mode == "mtproto-dd" and dd.host == "p.example.com"
    ee = parse_proxy("tg://proxy?server=h&port=443&secret=ee00112233445566778899aabbccddeeff676f6f676c652e636f6d")
    assert ee.mode == "mtproto-faketls"
    b64 = parse_proxy("tg://proxy?server=h&port=443&secret=7gEBAQEBAQEBAQEBAQEBAQFsaWIuYXJ2YW5jbG91ZC5jb20")
    assert b64.mode == "mtproto-faketls"
    s = parse_proxy("https://t.me/socks?server=10.0.0.1&port=1080&user=u&pass=p")
    assert (s.kind, s.username, s.password) == ("socks5", "u", "p")
    h = parse_proxy("http://user:p%40ss@proxy.corp:3128")
    assert (h.kind, h.password, h.port) == ("http", "p@ss", 3128)
    with pytest.raises(ProxyError):
        parse_proxy("ftp://x")
    kw = telethon_kwargs(dd)
    assert kw["proxy"] == ("p.example.com", 8443, "dd00112233445566778899aabbccddeeff")
    try:
        import telethon.network.connection.tcpmtproxy  # noqa: F401 — настоящий Telethon
    except ImportError:
        return
    kw = telethon_kwargs(ee)
    assert kw["proxy"] == ("h", 443, "00112233445566778899aabbccddeeff676f6f676c652e636f6d")
    assert kw["connection"].__name__ == "ConnectionTcpMTProxyFakeTLS"


def test_find_and_collect(tmp_path, monkeypatch):
    txt = ("Ваш прокси: tg://proxy?server=a.b&port=443&secret=dd00112233445566778899aabbccddeeff. "
           "Запасной https://t.me/proxy?server=c.d&port=443&secret=00112233445566778899aabbccddeeff")
    links = find_links(txt)
    assert len(links) == 2 and not links[0].endswith(".")
    cache = tmp_path / "proxies.txt"
    cache.write_text("# от бота\n" + links[1] + "\n", encoding="utf-8")
    monkeypatch.setenv("TG_PROXY", links[0])
    specs = collect_proxies({"proxies": ["socks5://10.1.1.1:1080"]}, cache, remembered=links[1])
    assert [s.host for s in specs] == ["c.d", "10.1.1.1", "a.b"]  # запомненный первым, дубли убраны
    specs = collect_proxies({"proxies": [], "try_direct": True}, tmp_path / "none.txt")
    assert specs[-1].kind == "direct"


def test_entities():
    ex = EntityExtractor({"patterns": {"incident": r"\b(?:INC|IM)[-_]?\d{5,}\b", "task": r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b"}})
    found = ex.find("Связано с INC0048213 и BILL-482, кодировка UTF-8, ещё раз inc0048213")
    assert ("incident", "INC0048213") in found and ("task", "BILL-482") in found
    assert not any(v == "UTF-8" for _, v in found)
    assert sum(1 for _, v in found if v == "INC0048213") == 1


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Вот:\n```json\n{"a": [1,2,]}\n```') == {"a": [1, 2]}
    assert extract_json('мысли... {"chats": [{"x": "строка с } скобкой"}]} конец')["chats"][0]["x"].endswith("скобкой")
    assert strip_think("<think>долго думаю</think>\n{\"ok\":true}") == '{"ok":true}'
    with pytest.raises(ValueError):
        extract_json("нет json")


def test_store_fts_and_query(tmp_path):
    st = Store(tmp_path / "t.db")
    st.add_messages([Message(1, 10, "2026-11-01T10:00:00+00:00", "Иван", "Инциденты по биллингу: INC0048190 висит"),
                     Message(1, 11, "2026-11-01T10:05:00+00:00", "Пётр", "обед?")])
    q = fts_query("Что с инцидентами по биллингу?")
    hits = st.search(q)
    assert hits and hits[0][0].msg_id == 10
    assert st.search(fts_query("INC0048190"))[0][0].msg_id == 10
    # повторная вставка того же не дублирует
    assert st.add_messages([Message(1, 10, "2026-11-01T10:00:00+00:00", "Иван", "Инциденты по биллингу: INC0048190 висит")]) == 0
