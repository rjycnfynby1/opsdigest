"""Сессия 11: новые источники (X5 Rooms, xTracker, Scriber AI), изображения из плагинов, люди и чаты по вики,
навык (правила, ссылки, отзывы, действия, отмена), ассистент (указания, вложения, «как на портале»), голосовой ввод."""
import copy
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import fake_llm
from fake_llm import start_server
from test_ui import ui, wait_job  # noqa: F401 — фикстура окна

from opsdigest import assistant as A
from opsdigest import cross, plugins, skill, voice
from opsdigest.config import DEFAULTS, Config
from opsdigest.demo import load_demo
from opsdigest.digest import DigestBuilder
from opsdigest.glossary import Glossary, block_for
from opsdigest.llm import make_client
from opsdigest.storage import Store

Q = lambda s: None  # noqa: E731 — тихий progress


@pytest.fixture
def env(tmp_path):
    fake_llm.PLUGINS.update({k: True for k in plugins.KINDS})
    srv, url = start_server()
    srv.mav_portal = True
    host = url.rsplit("/v1", 1)[0]
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "api_key": "x", "model": "fake",
                                         "external": True},
                               "maverick": {"type": "maverick", "url": host, "api_key": "sk_test"}}
    data["llm"]["provider"] = "local"
    data["privacy"] = {"pseudonymize": "all"}
    cfg = Config(data, tmp_path)
    st = Store(":memory:")
    yield srv, cfg, st
    fake_llm.PLUGINS.update({k: True for k in plugins.KINDS})


def mav(srv):
    return [c.get("message") or "" for c in srv.calls if isinstance(c, dict) and "message" in c]


# ---------------------------------------------------------------- новые источники
def test_new_sources(env):
    srv, cfg, st = env
    assert set(plugins.KINDS) >= {"rooms", "tracker", "meet"}
    for k in ("rooms", "tracker", "meet"):
        assert plugins.check(cfg, st, k)["state"] == "ok"
    assert plugins.task_keys(cfg, "см. BILL-482, UTF-8, INC0012345, SHA-256 и OPS-7") == ["BILL-482", "OPS-7"]
    r = plugins.rooms_search(cfg, st, "BILL-482", progress=Q)
    assert r and r[0]["source"] == "rooms" and "Биллинг" in plugins.item_line(r[0])
    t = plugins.tracker_lookup(cfg, st, ["BILL-482", "NOPE-1"], progress=Q)
    by = {x["ref"]: x for x in t}
    assert by["BILL-482"]["found"] and not by["NOPE-1"]["found"]
    n = len(srv.calls)
    plugins.tracker_lookup(cfg, st, ["BILL-482"], progress=Q)
    assert len(srv.calls) == n  # кеш
    now = datetime.now(timezone.utc)
    m = plugins.meet_digest(cfg, st, now - timedelta(days=1), now, progress=Q)
    assert m and "итоговый текст" in plugins.item_line(m[0])
    assert {x["source"] for x in plugins.search(cfg, st, "meet", "BILL-482", progress=Q)} == {"meet"}
    fake_llm.PLUGINS["tracker"] = False
    assert plugins.check(cfg, st, "tracker", force=True)["state"] == "offline"
    with pytest.raises(plugins.PluginOffline):
        plugins.search(cfg, st, "tracker", "что-то", progress=Q)


def test_people_lookup_privacy_exception(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    got = plugins.people_lookup(cfg, st, [{"name": "Алексей Смирнов", "username": "asmirnov"}], progress=Q)
    assert got and got[0]["data"]["post"] == "Ведущий инженер" and got[0]["source"] == "person"
    sent = [m for m in mav(srv) if "карточки этих сотрудников" in m][-1]
    assert "Алексей Смирнов" in sent and "@asmirnov" in sent  # имя для поиска — как есть (исключение)
    # в остальном запросе — обезличивание работает: имя руководителя из ответа не утекает в следующий запрос
    cfg.data["privacy"]["lookup_people"] = False
    assert plugins.people_lookup(cfg, st, [{"name": "Ирина Ковалёва"}], progress=Q) == []
    c = plugins.chats_lookup(cfg, st, [{"title": "Дежурка L2"}], progress=Q)
    assert c and "дежурная смена" in plugins.item_line(c[0])


def test_plugin_images(env):
    srv, cfg, st = env
    items = plugins.wiki_search(cfg, st, "автоплатёж", progress=Q)
    img = items[0]["data"]["images"][0]
    assert img["status"] == "ok" and img["media"] and img["mime"] == "image/png"
    f = plugins.ext_media_file(st, img["media"])
    assert f and cfg.path(f["path"]).is_file()
    assert "схема автоплатежа" in plugins.item_line(items[0]) and img.get("desc")  # описание моделью для картинок
    # артефакты ответа Maverick (file_artifacts) скачиваются с его ключом
    data = {"items": [{"title": "x"}]}
    n = plugins.ext_images(cfg, st, make_client("maverick", cfg.llm_profile("maverick")[1]), "wiki", data,
                           {"file_artifacts": [{"artifact_id": "art_00000000000000aa", "filename": "a.png",
                                                "mime_type": "image/png"}]})
    assert n == 1 and data["items"][0]["images"][0]["status"] == "ok"


def test_cross_with_new_sources(env):
    srv, cfg, st = env
    since, now = load_demo(cfg, st, timezone.utc)
    llm = make_client("local", cfg.llm_profile("local")[1])
    data = DigestBuilder(cfg, st, llm, progress=Q).build(since, now, "day")
    res = cross.CrossBuilder(cfg, st, llm, Q, {"rooms": True, "tracker": True, "meet": True}).build(data, since, now)
    assert res["rooms"] and res["meet"] and res["tracker"]
    assert any(x["ref"] == "OPS-77" for x in res["tracker"])  # «мои задачи за период»
    merged = [c for c in mav(srv) + [c["messages"][-1]["content"] for c in srv.calls if isinstance(c, dict) and "messages" in c]
              if "очищенные данные за период" in c]
    assert merged and "Сообщения X5 Rooms" in merged[-1] and "Встречи (Scriber AI)" in merged[-1]
    from opsdigest.render.html import _cross
    assert "Встречи за период" in _cross(res)


# ---------------------------------------------------------------- навык
def test_rules_scope_and_keys(env):
    srv, cfg, st = env
    skill.rule_save(st, {"text": "INC1234567 — обращение ITSM, чат с INC — инцидент-чат", "scope": ["digest"],
                         "keys": ["INC"]})
    skill.rule_save(st, {"text": "Пиши кратко", "scope": ["all"]})
    skill.rule_save(st, {"text": "Про биллинг спрашивай Смирнова", "scope": ["assistant"], "keys": ["биллинг"]})
    b = block_for(st, "чат INC0012345 упал", scope="digest")
    assert "инцидент-чат" in b and "Пиши кратко" in b and "Смирнова" not in b
    assert "Смирнова" in block_for(st, "что с биллингом?", scope="assistant")
    assert "Правила" not in block_for(st, "текст", scope=None)
    skill.link_save(st, {"title": "Список чатов", "url": "https://wiki.local/chats", "note": "с чего начинать"})
    assert "https://wiki.local/chats" in skill.plugin_context(st, "wiki")
    # ссылки и правила поиска — в вводной запроса к плагину вики
    plugins.wiki_search(cfg, st, "что-то", progress=Q)
    assert "https://wiki.local/chats" in mav(srv)[-1]
    with pytest.raises(ValueError):
        skill.link_save(st, {"url": "ftp://x"})


def test_feedback_to_skill_and_undo(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    raw = copy.deepcopy(cfg.data)
    ctx = skill.Ctx(raw, st, cfg)
    llm = make_client("local", cfg.llm_profile("local")[1])
    fid = skill.feedback_add(st, "report", "путает номер INC и инцидент-чат", rating=-1, excerpt="… INC0012345 …")
    res = skill.feedback_process(ctx, llm, fid, progress=Q)
    assert res["status"] == "pending"  # знания применены, изменение чатов ждёт «Применить»
    states = {p["op"]: p["state"] for p in res["plan"]}
    assert states["rule.add"] == "done" and states["term.upsert"] == "done" and states["chat.set"] == "pending"
    assert any("инцидент-чат" in r["text"] for r in skill.rules(st))
    e = Glossary(st).get("Инцидент-чат")
    assert e and e["status"] == "confirmed"
    t = A.thread(st, res["thread"])
    assert t["kind"] == "feedback" and t["messages"][-1]["data"]["plan"]
    # «Применить» — чаты с «авар» в названии
    chat_plan = next(p for p in res["plan"] if p["op"] == "chat.set")
    assert chat_plan["count"] >= 1
    r = skill.apply(ctx, chat_plan["action"], source="feedback")
    rules = raw["telegram"]["chats"]
    hit = [x for x in rules if x.get("kind") == "incident"]
    assert hit and "Чат аварии." in hit[0]["description"]
    skill.undo(ctx, r["journal"])
    assert not [x for x in raw["telegram"]["chats"] if x.get("kind") == "incident" and "Чат аварии." in (x.get("description") or "")]
    # отмена правила
    j = next(x for x in skill.journal(st) if x["op"] == "rule.add")
    skill.undo(ctx, j["id"])
    assert not any("инцидент-чат" in r["text"] for r in skill.rules(st))
    assert "INC" in skill.skill_md(st) or "Инцидент" in skill.skill_md(st)


def test_feedback_question_flow(env):
    srv, cfg, st = env
    ctx = skill.Ctx(copy.deepcopy(cfg.data), st, cfg)
    llm = make_client("local", cfg.llm_profile("local")[1])
    fid = skill.feedback_add(st, "answer", "непонятно с номерами")
    r = skill.feedback_process(ctx, llm, fid, progress=Q)
    assert r["status"] == "question" and r["options"] == ["номер ITSM", "инцидент-чат"]
    assert skill.feedback_get(st, fid)["status"] == "question"
    r2 = skill.feedback_process(ctx, llm, fid, answer="номер ITSM", progress=Q)
    assert r2["status"] in ("done", "pending")
    msgs = A.thread(st, r["thread"])["messages"]
    assert [m["role"] for m in msgs][-3:] == ["assistant", "user", "assistant"]


def test_assistant_command_and_ask(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    llm = make_client("local", cfg.llm_profile("local")[1])
    tid = A.create(st, "Указания")
    raw = copy.deepcopy(cfg.data)
    r = A.answer(cfg, st, llm, tid, "Поставь всем чатам аварий высокую важность и добавь ссылку на регламент",
                 opts={"raw": raw}, progress=Q)
    states = {p["op"]: p["state"] for p in r["plan"]}
    assert states["link.add"] == "done" and states["chat.set"] == "pending"
    assert skill.links(st)[0]["url"] == "https://wiki.local/incidents"
    r2 = A.answer(cfg, st, llm, tid, "Измени непонятно что", opts={"raw": raw}, progress=Q)
    assert r2["ask"]["options"] == ["все с «авар»", "только этот"]
    text, acts, ask = skill.parse_blocks('Ок.\n```ask {"question": "Какой период?", "options": ["день", "неделя"]}```')
    assert text == "Ок." and ask["question"] == "Какой период?" and not acts


def test_assistant_files_and_portal(env):
    srv, cfg, st = env
    tid = A.create(st, "Портал", "maverick")
    f = A.file_save(cfg, st, tid, "заметка.txt", "text/plain", "Ошибка 502 на входе в ЛК".encode())
    assert f["has_text"] and not f["image"]
    r = A.answer(cfg, st, None, tid, "Что это за ошибка? пришли файл", opts={"maverick": True, "files": [f["id"]]},
                 progress=Q)
    assert "ход 1, вложений 1" in r["text"] and r["files"] and r["files"][0]["image"]
    sess = A.thread(st, tid)["meta"]["mav_session"]
    r2 = A.answer(cfg, st, None, tid, "подтверди создание задачи", opts={"maverick": True}, progress=Q)
    assert "ход 2" in r2["text"] and r2["ask"]["options"] == ["Да", "Нет"]
    assert A.thread(st, tid)["meta"]["mav_session"] == sess
    portal = [c for c in srv.calls if isinstance(c, dict) and c.get("session_id") == sess]
    assert portal  # второй ход — в той же сессии
    # обычный ассистент: текст вложения уходит модели
    tid2 = A.create(st, "Обычная")
    f2 = A.file_save(cfg, st, tid2, "лог.txt", "text/plain", "Traceback: KafkaTimeout в billing-svc".encode())
    A.answer(cfg, st, make_client("local", cfg.llm_profile("local")[1]), tid2, "Вопрос пользователя по логу",
             opts={"files": [f2["id"]], "sources": {"telegram": False}}, progress=Q)
    sent = [c["messages"][-1]["content"] for c in srv.calls if isinstance(c, dict) and "messages" in c][-1]
    assert "KafkaTimeout" in sent


def test_everywhere_selected_sources(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    out = A.everywhere(cfg, st, "BILL-482 двойные списания", sources={"telegram": False, "tracker": True, "meet": True},
                       progress=Q)
    assert out["sources"] == ["tracker", "meet"] and out["tracker"] and out["meet"] and not out["telegram"]
    assert not out["mail"]
    out2 = A.everywhere(cfg, st, "BILL-482", sources={"telegram": True}, progress=Q)
    assert out2["sources"] == ["telegram"]


def test_describe_with_sources(env):
    srv, cfg, st = env
    load_demo(cfg, st, timezone.utc)
    from opsdigest.describe import Describer
    llm = make_client("local", cfg.llm_profile("local")[1])
    saved = {}
    d = Describer(cfg, st, llm, lambda k, t, r: saved.setdefault(t, r), progress=Q, sources=["wiki", "mail"])
    chat = st.chats()[0]
    st.db.execute("UPDATE messages SET sender_id=555 WHERE sender='Алексей Смирнов'")
    uid = (555, "Алексей Смирнов")
    d.run([("chat", chat.chat_id), ("person", uid[0], uid[1])], workers=1)
    reqs = [c["messages"][-1]["content"] for c in srv.calls if isinstance(c, dict) and "messages" in c]
    assert any("Что известно о чате из других источников" in x and "дежурная смена" in x for x in reqs)
    card = saved[("person", uid[0], uid[1])]["card"]
    assert card["post"] == "Ведущий инженер"


# ---------------------------------------------------------------- голосовой ввод
def test_voice_fake_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSDIGEST_VOICE_DIR", str(tmp_path / "voice"))
    s = voice.settings_of({"voice": {"model": "fake", "replace": [["мэверик", "Maverick"]]}})
    pcm = b"\x00\x10" * 16000 * 3  # 3 секунды
    try:
        r = voice.recognize(s, pcm, 16000)
        assert r["text"] == "проверка 3.0 с" and r["seconds"] == 3.0
        assert voice.ENGINE.state()["running"]
        long = b"\x00\x10" * 16000 * 50  # длинная запись режется на куски
        assert voice.recognize(s, long, 16000)["text"].count("проверка") >= 3
    finally:
        voice.ENGINE.stop()
    assert voice.postprocess("спроси мэверик новая строка про инк", s, [("инк", "INC")]) == "спроси Maverick\nпро INC"


def test_voice_model_files_and_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSDIGEST_VOICE_DIR", str(tmp_path / "voice"))
    tree = [{"path": "v3_e2e_ctc.onnx", "size": 900}, {"path": "v3_e2e_ctc.int8.onnx", "size": 300,
                                                        "lfs": {"oid": "a" * 64}},
            {"path": "v3_e2e_ctc_vocab.txt", "size": 5}, {"path": "v3_ctc.onnx", "size": 1}, {"path": "README.md"},
            {"path": "config.json", "size": 2}]
    monkeypatch.setattr(voice, "_json", lambda url: tree)
    s = voice.settings_of({})
    names = [f["path"] for f in voice.model_files(s)]
    assert names == ["v3_e2e_ctc.int8.onnx", "v3_e2e_ctc_vocab.txt", "config.json"]
    z = tmp_path / "Downloads" / "OpsDigest-voice.zip"
    z.parent.mkdir()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("models/gigaam-v3-e2e-ctc/v3_e2e_ctc.int8.onnx", b"x" * 10)
        zf.writestr("models/gigaam-v3-e2e-ctc/v3_e2e_ctc_vocab.txt", b"a\nb")
        zf.writestr("../evil.txt", b"no")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert voice.find_archives() == [z]
    res = voice.import_archive(s, z, progress=Q)
    assert res["models"] == ["gigaam-v3-e2e-ctc"] and voice.model_status(s)["ok"]
    assert not (tmp_path / "evil.txt").exists()


def test_voice_glossary_replacements(env):
    srv, cfg, st = env
    Glossary(st).save({"term": "Maverick", "aliases": ["Мэверик", "maverick agent"]})
    assert voice.glossary_replacements(st) == [("Мэверик", "Maverick")]


# ---------------------------------------------------------------- окно
def test_window_skill_voice_upload(ui, tmp_path, monkeypatch):
    app, call, llm_url, _ = ui
    monkeypatch.setenv("OPSDIGEST_VOICE_DIR", str(tmp_path / "voice"))
    host = llm_url.rsplit("/v1", 1)[0]
    call("/api/workspace/set", {"path": str(tmp_path / "ws")})
    call("/api/config/llm", {"provider": "maverick", "maverick": {"style": "agent", "url": host, "token": "sk_test"}})
    call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "fake", "ctx": 32768}})
    code, r = call("/api/skill/rule", {"text": "Пиши кратко", "scope": ["digest"]})
    assert code == 200 and r["rule"]["scope"] == ["digest"]
    assert call("/api/skill/link", {"title": "Регламент", "url": "https://wiki.local/r"})[0] == 200
    code, sk = call("/api/skill")
    assert code == 200 and sk["counts"]["rules"] == 1 and sk["links"][0]["title"] == "Регламент"
    code, ex = call("/api/skill/export?format=md")
    assert "Пиши кратко" in ex["text"]
    code, r = call("/api/feedback", {"target": "answer", "rating": -1, "text": "непонятно с номерами"})
    j = wait_job(call, r["job"], 20)
    assert j["status"] == "done" and j["result"]["status"] == "question", j
    tid = j["result"]["thread"]
    code, r = call("/api/asst/ask", {"thread": tid, "question": "номер ITSM"})  # ответ — в разбор отзыва
    j = wait_job(call, r["job"], 20)
    assert j["status"] == "done" and j["result"]["status"] in ("done", "pending")
    t = call(f"/api/asst/thread?id={tid}")[1]["thread"]
    mid = t["messages"][-1]["id"]
    code, r = call("/api/skill/apply", {"msg": mid, "cancel": True})
    assert code == 200
    # вложение в беседу: тело — сам файл
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/asst/upload?name=a.txt&mime=text/plain",
                                 data="текст вложения".encode(), method="POST")
    req.add_header("X-Token", app.token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        f = json.loads(resp.read())["file"]
    code, raw = call(f"/asst-file/{f['id']}", raw=True)
    assert code == 200 and raw.decode() == "текст вложения"
    # голосовой ввод: состояние, настройки, распознавание (поддельная модель)
    code, v = call("/api/voice/status")
    assert code == 200 and "runtime" in v and v["models"]
    call("/api/voice/settings", {"model": "fake", "replace": "мэверик → Maverick"})
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/voice/recognize?rate=16000",
                                 data=b"\x00\x10" * 16000, method="POST")
    req.add_header("X-Token", app.token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read())
        assert out["text"] == "проверка 1.0 с"
    finally:
        voice.ENGINE.stop()


def test_voice_github_archive_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSDIGEST_VOICE_DIR", str(tmp_path / "voice"))
    import io

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(voice, "_get_h", lambda url, headers, timeout=60: R(json.dumps([{"assets": [{"name": "x.zip"}]}]).encode()))
    with pytest.raises(voice.VoiceError, match="нет OpsDigest-voice.zip"):
        voice.github_archive({"update": {"token": "t"}}, progress=Q)


# ---------------------------------------------------------------- сессия 11, продолжение: формы Maverick, сессия Telegram
def test_hitl_shapes():
    from opsdigest import maverick_chat as M
    a = M.hitl_find({"status": "hitl_waiting", "response_text": "", "hitl_pending": None,
                     "pending_form": {"id": "f1", "title": "Уточните", "fields": [
                         {"name": "plugin", "label": "Какой плагин?", "options": ["Rooms", "Почта"], "required": True},
                         {"name": "why", "label": "Зачем?"}]}})
    assert a["id"] == "f1" and [f["type"] for f in a["form"]] == ["choice", "text"] and a["form"][0]["required"]
    b = M.hitl_find({"status": "hitl_waiting", "tool_calls": [{"name": "rooms_connect", "arguments": {"x": 1}, "status": "pending"}]})
    assert "rooms_connect" in b["question"] and b["options"] == ["Да, выполнить", "Нет, отменить"]
    c = M.hitl_find({"status": "hitl_waiting"})
    assert c["unknown"] and c["options"] == ["Да", "Нет"]
    d = M.hitl_find({"status": "hitl_waiting", "hitl_pending": {"schema": {"properties": {
        "env": {"title": "Среда", "enum": ["prod", "test"]}, "ok": {"title": "Подтвердить?", "type": "boolean"}}}}})
    assert [f["name"] for f in d["form"]] == ["env", "ok"] and d["form"][1]["type"] == "bool"
    assert M.hitl_find({"status": "completed", "response_text": "ok"}) is None


def test_portal_hitl_form_and_answer(env):
    srv, cfg, st = env
    tid = A.create(st, "Портал", "maverick")
    r = A.answer(cfg, st, None, tid, "покажи форму", opts={"maverick": True}, progress=Q)
    assert r["ask"]["form"] and r["ask"]["form"][0]["options"] == ["Rooms", "Почта"]
    assert A.thread(st, tid)["meta"]["mav_hitl"]["id"] == "form7"
    r2 = A.answer(cfg, st, None, tid, "Какой плагин?: Rooms", opts={"maverick": True, "answers": {"plugin": "Rooms"}},
                  progress=Q)
    sent = [c for c in srv.calls if isinstance(c, dict) and c.get("hitl_id") == "form7"]
    assert sent and sent[-1]["hitl_response"]["answers"] == {"plugin": "Rooms"} and "ход 2" in r2["text"]
    assert "mav_hitl" not in A.thread(st, tid)["meta"]
    log = cfg.path("data/maverick-chat.txt").read_text(encoding="utf-8")
    assert "hitl" in log and "Какой плагин" in log
    # служебный запрос (плагин) на hitl — понятная ошибка, без повторов
    from opsdigest.llm import LLMError
    llm = make_client("maverick", cfg.llm_profile("maverick")[1])
    with pytest.raises(LLMError, match="ждёт подтверждения"):
        llm.complete("", "Это запрос программы OpsDigest, нужен hitl_test")


def test_telegram_revoked_session(tmp_path):
    import asyncio
    from opsdigest.sources import telegram as T
    from opsdigest.sources.tg_proxy import ProxySpec
    data = copy.deepcopy(DEFAULTS)
    data["telegram"].update({"api_id": "1", "api_hash": "x"})
    cfg = Config(data, tmp_path)
    st = Store(tmp_path / "db.sqlite")
    sess = cfg.path("data/telegram.session")
    sess.parent.mkdir(parents=True, exist_ok=True)
    sess.write_bytes(b"dead")

    class AuthKeyDuplicatedError(Exception):
        pass
    tried = []

    class C:
        session = None

        def __init__(self, spec):
            self.spec = spec

        async def connect(self):
            tried.append(self.spec.raw)
            raise AuthKeyDuplicatedError("used under two different IP addresses")

        async def disconnect(self):
            pass

        def is_connected(self):
            return False
    src = T.TelegramSource(cfg, st, progress=lambda s: None)
    specs = [ProxySpec("socks5", "a", 1, raw="vpn:a"), ProxySpec("socks5", "b", 1, raw="vpn:b")]

    async def cands():
        return specs

    async def clock(_):
        return None
    src.all_candidates, src._fix_clock, src._new_client = cands, clock, (lambda spec, probe=False: C(spec))
    with pytest.raises(T.TelegramError, match="отозвал сессию"):
        asyncio.run(src.connect())
    assert tried == ["vpn:a"] and not sess.exists()  # второй сервер не пробовали, мёртвая сессия удалена
    # мессенджер подключается — сводка ждёт его подключения, а не открывает второе
    busy = {"v": True}
    T.LIVE_BUSY = lambda: busy["v"]
    shared_client = type("S", (), {"get_me": None})()

    async def go():
        async def later():
            await asyncio.sleep(1.2)
            T.SHARED = lambda: (shared_client, specs[0])
            busy["v"] = False
        asyncio.ensure_future(later())
        await src.connect(require_auth=False)
    try:
        src2 = T.TelegramSource(cfg, st, progress=lambda s: None)
        src = src2
        asyncio.run(go())
        assert src2.client is shared_client and src2._shared
    finally:
        T.LIVE_BUSY, T.SHARED = None, None
