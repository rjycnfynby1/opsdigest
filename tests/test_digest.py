"""Сводка целиком: демо-переписка → поддельная LLM по HTTP → JSON → HTML и текст для Telegram."""
import copy
from pathlib import Path
import re
from datetime import timezone

from fake_llm import start_server

from opsdigest.ask import answer
from opsdigest.config import DEFAULTS, Config, load_config
from opsdigest.demo import load_demo
from opsdigest.digest import DigestBuilder, Redactor, make_batches, ChatWork, transcript_lines
from opsdigest.llm import make_client
from opsdigest.render import render_html, render_telegram
from opsdigest.storage import Chat, Message, Store


def build(tmp_path, chunk_chars=0):
    srv, url = start_server()
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"]["local"]["base_url"] = url
    data["digest"]["chunk_chars"] = chunk_chars
    data["profile"]["name"] = "Алексей"
    cfg = Config(data, tmp_path)
    store = Store(":memory:")
    since, until = load_demo(cfg, store, timezone.utc)
    llm = make_client(*cfg.llm_profile())
    d = DigestBuilder(cfg, store, llm, progress=lambda s: None).build(since, until, "day")
    return d, srv, cfg, store, llm


def test_digest_end_to_end(tmp_path):
    d, srv, *_ = build(tmp_path)
    ov = d["overview"]
    assert not d["meta"]["errors"], d["meta"]["errors"]
    assert d["meta"]["chats"] == 6 and d["meta"]["messages"] == 30
    assert srv.calls[0]["model"] == "fake-qwen3-instruct"  # auto выбрал не embedding
    assert ov["headline"] and len(ov["top"]) == 7 and not ov.get("fallback")
    first = ov["top"][0]
    assert first["chat_title"].startswith("Алексей") and first["draft"]
    # ссылки на сообщения групп ведут в t.me/c/…, у личного чата ссылок нет
    inc_top = next(t for t in ov["top"] if t["chat_title"].startswith("Авария"))
    assert inc_top["links"][0]["url"] == "https://t.me/c/1000000001/1010"
    assert first["links"][0]["url"] is None
    # инцидент собран из нескольких чатов
    assert any(len(x["chat_titles"]) == 3 for x in ov["incidents"])
    # сквозные номера: INC0048213 — в аварийном чате, INC0048190 — в команде; BILL-482 в двух чатах
    refs = {g["value"]: g for g in d["refs"]}
    assert refs["BILL-482"]["chats"] == 2 and "INC0048213" in refs and "AUTH-1290" in refs
    # чаты отсортированы по важности, болталка в конце
    assert d["chats"][-1]["title"].startswith("Отдел") and d["chats"][0]["importance"] == 5
    html = render_html(d)
    assert html.startswith("<!DOCTYPE html>") and "Нужна реакция" in html and "t.me/c/1000000001/1010" in html
    assert "<script>" in html and html.count("<details class='chat'") == 6
    tg = render_telegram(d)
    assert len(tg) < 4096 and "<code>" in tg and tg.count("<b>") >= 3
    # теги в тексте для Telegram сбалансированы
    for tag in ("b", "i", "code", "a"):
        assert len(re.findall(f"<{tag}[ >]", tg)) == tg.count(f"</{tag}>")


def test_small_chunks_split_and_merge(tmp_path):
    d, srv, *_ = build(tmp_path, chunk_chars=700)
    assert len(srv.calls) > 8  # аварийный чат ушёл частями
    inc = next(c for c in d["chats"] if c["title"].startswith("Авария"))
    assert inc["summary"] and inc["items"]


def test_fallback_when_reduce_fails(tmp_path, monkeypatch):
    import fake_llm
    orig = fake_llm.respond
    monkeypatch.setattr(fake_llm, "respond", lambda s, u: "не json" if "Сводки чатов:" in u else orig(s, u))
    d, *_ = build(tmp_path)
    ov = d["overview"]
    assert ov.get("fallback") and ov["top"] and ov["top"][0]["priority"] == "high"
    assert any("общая сводка" in e for e in d["meta"]["errors"])


def test_ask(tmp_path):
    d, srv, cfg, store, llm = build(tmp_path)
    out = answer(cfg, store, llm, "что с BILL-482 и двойными списаниями?", days=3, progress=lambda s: None)
    assert "BILL-482" in out["answer"] and out["need_files"] == [] and out["images"] == []
    ctx = srv.calls[-1]["messages"][-1]["content"]
    assert "#1019" in ctx and "Алексей" in ctx


def test_redaction_and_transcript():
    r = Redactor(load_config(Path(__file__).resolve().parent.parent / "tools" / "config.example.toml")["privacy"]["redact"])
    assert r("звоните +7 (999) 123-45-67, пишите a.b@corp.ru") == "звоните [телефон], пишите [email]"
    m = [Message(1, 5, "2026-11-01T10:00:00+00:00", "Иван", "строка1\nстрока2", mentioned=True, reply_to=4),
         Message(1, 6, "2026-11-01T10:01:00+00:00", "Я", "ок", is_out=True)]
    lines = transcript_lines(m, timezone.utc)
    assert lines[0] == "— 01.11.2026 —" and "↩#4 @me" in lines[1] and "⏎" in lines[1] and lines[2].endswith("Я: ок")


def test_batches():
    chat = Chat(1, "Большой", "group")
    w = ChatWork("c1", chat, [], [f"#{i} [10:00] А: " + "х" * 90 for i in range(100)])
    small = [ChatWork(f"c{i}", Chat(i, f"Мелкий {i}", "group"), [], ["#1 [10:00] Б: привет"]) for i in range(2, 12)]
    bs = make_batches([w] + small, budget=3000)
    big = [b for b in bs if b.keys == ["c1"]]
    assert len(big) >= 3 and all(b.chars <= 3000 for b in big) and "часть 1/" in big[0].blocks[0]
    rest = [b for b in bs if "c1" not in b.keys]
    assert sum(len(b.keys) for b in rest) == 10 and all(len(b.keys) <= 8 for b in rest)
