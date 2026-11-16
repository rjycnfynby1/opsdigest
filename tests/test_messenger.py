"""Мессенджер на поддельном клиенте Telegram: подключение, список чатов и папки, история (из базы и из Telegram),
отправка/ответ/файл/правка/удаление/пересылка/реакция/«Прочитано», события (новое, правка, удаление, прочтение,
«печатает»), поиск, общее подключение для других задач, ИИ в чате (сводка, черновик, связанное)."""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("telethon.events")

from fake_llm import start_server  # noqa: E402
from test_export import NOW, jpeg, tmsg  # noqa: E402

from opsdigest.sources import telegram as tgmod  # noqa: E402
from opsdigest.sources.telegram import TelegramSource  # noqa: E402

ME = 1
CH = -1000000001001


def dialog(cid, name, msgs, kind="group", unread=0, pinned=False, is_self=False):
    ent = NS(id=cid, username=None, bot=False, is_self=is_self, contact=False)
    return NS(id=cid, name=name, entity=ent, is_user=kind == "private", is_group=kind == "group",
              is_channel=kind == "channel", unread_count=unread, date=msgs[0].date if msgs else None,
              message=msgs[0] if msgs else None, pinned=pinned, archived=False,
              dialog=NS(notify_settings=NS(mute_until=None), read_inbox_max_id=0, read_outbox_max_id=0,
                        unread_mentions_count=0))


class FakeClient:
    def __init__(self, chats):
        self.chats = chats            # id -> (dialog, [сообщения новые → старые])
        self.handlers = []
        self.read, self.requests, self.deleted, self.downloads = [], [], [], []
        self.connected = True
        self._disc = None

    # --- подключение
    @property
    def disconnected(self):
        if self._disc is None:
            self._disc = asyncio.get_running_loop().create_future()
        return self._disc

    async def disconnect(self):
        self.connected = False
        if self._disc is not None and not self._disc.done():
            self._disc.set_result(None)

    def is_connected(self):
        return self.connected

    async def get_me(self):
        return NS(id=ME, first_name="Я", last_name="Тестов", username=None)

    def add_event_handler(self, cb, ev):
        self.handlers.append((type(ev).__name__, getattr(ev, "inbox", None), cb))

    def handler(self, name, inbox=None):
        return next(cb for n, ib, cb in self.handlers if n == name and (inbox is None or ib == inbox))

    # --- чтение
    async def iter_dialogs(self, archived=None, limit=None):
        for d, _ in self.chats.values():
            yield d

    async def iter_messages(self, entity, limit=None, min_id=0, offset_id=0, reverse=False):
        msgs = [m for m in self.chats[entity.id][1] if (not min_id or m.id > min_id) and (not offset_id or m.id < offset_id)]
        for n, m in enumerate(reversed(msgs) if reverse else msgs):
            if limit and n >= limit:
                return
            yield m

    async def get_messages(self, entity, limit=None, offset_id=0, add_offset=0, ids=None, search=None, filter=None,
                           reply_to=None, from_user=None):
        if filter is not None or reply_to is not None or from_user is not None:
            from telethon.tl import types as T
            msgs = list(self.chats[entity.id][1])
            if reply_to is not None:
                msgs = [m for m in msgs if getattr(getattr(m, "reply_to", None), "reply_to_top_id", None) == reply_to
                        or getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None) == reply_to]
            if filter is T.InputMessagesFilterPinned:
                msgs = [m for m in msgs if getattr(m, "pinned", False)]
            elif filter is not None:
                msgs = [m for m in msgs if m.media is not None]
            if from_user is not None:
                msgs = [m for m in msgs if m.sender_id == from_user.id]
            if search:
                msgs = [m for m in msgs if search.lower() in (m.message or "").lower()]
            if offset_id:
                msgs = [m for m in msgs if m.id < offset_id]
            return msgs[:limit] if limit else msgs
        if entity is None:
            msgs = []
            for cid, (_, ms) in self.chats.items():
                for m in ms:
                    m.chat_id = cid
                    msgs.append(m)
        else:
            msgs = list(self.chats[entity.id][1])
        if ids is not None:
            by = {m.id: m for m in msgs}
            return [by.get(i) for i in ids] if isinstance(ids, list) else by.get(ids)
        if search:
            msgs = [m for m in msgs if search.lower() in (m.message or "").lower()]
        if offset_id:
            i = next((k for k, m in enumerate(msgs) if m.id < offset_id), len(msgs))
            msgs = msgs[max(0, i + add_offset):]
        return msgs[:limit] if limit else msgs

    async def get_input_entity(self, chat_id):
        if chat_id in self.chats:
            return self.chats[chat_id][0].entity
        if isinstance(chat_id, int) and chat_id > 0:  # пользователь (фильтр «от кого»)
            return NS(id=chat_id)
        raise ValueError("нет такого")

    async def download_media(self, m, file=None, thumb=None):
        self.downloads.append(m.id)
        return m._data

    # --- запись
    def _new(self, chat_id, text, **kw):
        msgs = self.chats[chat_id][1]
        nid = max([m.id for ms in (c[1] for c in self.chats.values()) for m in ms] or [0]) + 1
        m = tmsg(nid, 0, text, kw.pop("kind", None))
        m.out, m.sender, m.sender_id, m.chat_id = True, NS(first_name="Я", last_name="Тестов"), ME, chat_id
        for k, v in kw.items():
            setattr(m, k, v)
        msgs.insert(0, m)
        return m

    async def send_message(self, ent, text, reply_to=None, parse_mode=None, link_preview=True):
        return self._new(ent.id, text.replace("**", ""), reply_to_msg_id=reply_to)

    async def send_file(self, ent, bio, caption=None, reply_to=None, force_document=False, parse_mode=None):
        m = self._new(ent.id, caption or "", kind="photo", reply_to_msg_id=reply_to)
        m._data = bio.getvalue()
        return m

    async def edit_message(self, ent, msg_id, text, parse_mode=None):
        m = next(m for m in self.chats[ent.id][1] if m.id == msg_id)
        m.message, m.edit_date = text, NOW
        return m

    async def delete_messages(self, ent, ids, revoke=True):
        self.deleted.append((ent.id, list(ids), revoke))
        self.chats[ent.id] = (self.chats[ent.id][0], [m for m in self.chats[ent.id][1] if m.id not in ids])

    async def forward_messages(self, dst, ids, from_peer=None):
        src = {m.id: m for m in self.chats[from_peer.id][1]}
        return [self._new(dst.id, src[i].message, fwd_from=NS(from_name="Иван")) for i in ids]

    async def send_read_acknowledge(self, ent, max_id=0, clear_mentions=False):
        self.read.append((ent.id, max_id))

    async def __call__(self, req):
        from telethon.tl import types as T
        self.requests.append(type(req).__name__)
        name = type(req).__name__
        if name == "GetDialogFiltersRequest":
            return NS(filters=[T.DialogFilterDefault(),
                               T.DialogFilter(id=2, title=T.TextWithEntities(text="Работа", entities=[]),
                                              pinned_peers=[], include_peers=[T.InputPeerChannel(1001, 0)],
                                              exclude_peers=[], groups=True, emoticon="💼")])
        if name == "GetPeerDialogsRequest":
            return NS(dialogs=[NS(unread_count=0)])
        if name == "GetForumTopicsRequest":
            msgs = {m.id: m for m in self.chats[req.peer.id][1]}
            return NS(topics=[ForumTopic(id=t, title=title, icon_color=0, top_message=top, closed=False, pinned=t == 2001,
                                         hidden=False, unread_count=2 if t == 2001 else 0, unread_mentions_count=0,
                                         read_inbox_max_id=0, notify_settings=NS(mute_until=None))
                              for t, title, top in getattr(self, "forum_topics", [])],
                      messages=[msgs[top] for _, _, top in getattr(self, "forum_topics", []) if top in msgs])
        if name in ("GetUnreadMentionsRequest", "GetUnreadReactionsRequest"):
            return NS(messages=[m for m in self.chats[req.peer.id][1] if getattr(m, "mentioned", False)]
                      if name == "GetUnreadMentionsRequest" else [])
        if name in ("SendVoteRequest", "UpdateNotifySettingsRequest", "ReadDiscussionRequest"):
            return True
        return None


class ForumTopic(NS):
    pass


def history(n=60):
    msgs = [tmsg(i, 100 - i, f"сообщение {i} про BILL-482" if i % 10 == 0 else f"сообщение {i}") for i in range(n, 0, -1)]
    msgs[3] = tmsg(57, 43, "скриншот ошибки", "photo")
    return msgs


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from opsdigest.ui.server import App, make_server
    group = history()
    pm = [tmsg(1001, 1, "Пришлёшь примеры по BILL-482?"), tmsg(1000, 2, "Привет")]
    saved = [tmsg(900, 5, "заметка себе")]
    fake = FakeClient({CH: (dialog(CH, "Авария BILL", group, unread=3, pinned=True), group),
                       555: (dialog(555, "Пётр", pm, kind="private", unread=1), pm),
                       ME: (dialog(ME, "Я", saved, kind="private", is_self=True), saved)})
    orig_connect = TelegramSource.connect

    async def fake_connect(self, interactive=False, skip=None, require_auth=True):
        self.client = fake
        self.proxy = NS(describe=lambda: "напрямую", mode="direct", raw="direct", kind="direct")
        self.store.kv_set("me", json.dumps({"id": ME, "name": "Я Тестов"}))
    monkeypatch.setattr(TelegramSource, "connect", fake_connect)
    app = App(idle_exit=False)
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm_srv, llm_url = start_server()

    def call(path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data,
                                     method="GET" if data is None else "POST")
        req.add_header("X-Token", app.token)
        req.add_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    ws = tmp_path / "ws"
    assert call("/api/workspace/set", {"path": str(ws)})["ok"]
    call("/api/config/llm", {"provider": "local", "local": {"base_url": llm_url, "model": "auto", "ctx": 32768}})
    call("/api/config/telegram", {"api_id": "123", "api_hash": "abc", "phone": "+7999"})
    (ws / "data").mkdir(exist_ok=True)
    (ws / "data" / "telegram.session").write_bytes(b"x")
    yield app, call, fake, orig_connect, llm_srv
    app.live.stop()
    srv.shutdown()
    srv.server_close()


def wait(pred, sec=8):
    end = time.time() + sec
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("не дождался")


def events(call, since=0):
    j = call(f"/api/tg/events?since={since}&wait=0")
    return j["events"], j["seq"]


def test_messenger_end_to_end(env):
    app, call, fake, orig_connect, llm = env
    st = call("/api/tg/state")
    assert st["ok"] and st["state"] in ("connecting", "online")
    wait(lambda: call("/api/tg/state")["loaded"])
    d = call("/api/tg/dialogs")
    chats = {c["id"]: c for c in d["chats"]}
    assert d["chats"][0]["id"] == CH and chats[CH]["pinned"]          # закреплённый — сверху
    assert chats[555]["unread"] == 1 and chats[555]["last"]["text"].startswith("Пришлёшь")
    assert chats[ME]["title"] == "Избранное" and chats[ME]["excluded"]            # в сводку не берём
    assert d["folders"][0]["title"] == "Работа" and d["folders"][0]["inc"] == [CH]

    # история: сначала из базы (после подключения — только последнее), затем из Telegram
    h = call(f"/api/tg/history?chat={CH}&live=1&limit=20")
    assert h["live"] and len(h["messages"]) == 20 and h["more"]
    assert h["messages"][-1]["id"] == 60 and h["messages"][0]["id"] == 41
    photo = next(m for m in h["messages"] if m["id"] == 57)
    assert photo["media"]["kind"] == "photo" and photo["text"].startswith("[фото]")
    wait(lambda: 57 in fake.downloads)                                               # фото скачивается само
    wait(lambda: any(e["type"] == "media" and e["msg"] == 57 for e in events(call)[0]))
    older = call(f"/api/tg/history?chat={CH}&live=1&limit=20&before=41")
    assert [m["id"] for m in older["messages"]][-1] == 40
    around = call(f"/api/tg/history?chat={CH}&around=30&limit=10")
    assert 30 in [m["id"] for m in around["messages"]]
    assert not fake.read                                                              # чтение ничего не отметило

    # отправка ответа: сообщение в базе и в окне, чат прочитан
    _, seq = events(call)
    r = call("/api/tg/send", {"chat": 555, "text": "**Да**, пришлю", "reply_to": 1001})
    assert r["ok"]
    last = call("/api/tg/history?chat=555")["messages"][-1]
    assert last["out"] and last["text"] == "Да, пришлю" and last["rp"]["text"].startswith("Пришлёшь")
    assert fake.read and fake.read[-1][0] == 555
    assert {c["id"]: c for c in call("/api/tg/dialogs")["chats"]}[555]["unread"] == 0
    evs, seq = events(call, seq)
    assert any(e["type"] == "msg" and e["chat"] == 555 for e in evs)

    # входящее сообщение: в базе, в окне, +1 непрочитанное
    incoming = tmsg(2000, 0, "Новое сообщение от Петра")
    incoming.chat_id = 555
    app.run_async(fake.handler("NewMessage")(NS(chat_id=555, message=incoming)))
    evs, seq = events(call, seq)
    assert any(e["type"] == "msg" and e["msgs"][0]["text"] == "Новое сообщение от Петра" for e in evs)
    assert {c["id"]: c for c in call("/api/tg/dialogs")["chats"]}[555]["unread"] == 1
    # правка, «печатает», прочтение на другом устройстве, удаление
    incoming.message, incoming.edit_date = "Исправленный текст", NOW
    app.run_async(fake.handler("MessageEdited")(NS(chat_id=555, message=incoming)))
    app.run_async(fake.handler("UserUpdate")(NS(typing=True, chat_id=555,
                                                get_user=lambda: asyncio.sleep(0, NS(first_name="Пётр")))))
    app.run_async(fake.handler("MessageRead", True)(NS(chat_id=555, max_id=2000, inbox=True)))
    app.run_async(fake.handler("MessageDeleted")(NS(chat_id=None, deleted_ids=[2000])))
    evs, seq = events(call, seq)
    kinds = [e["type"] for e in evs]
    assert "edit" in kinds and "typing" in kinds and "chat" in kinds and "del" in kinds
    ed = next(e for e in evs if e["type"] == "edit")["msgs"][0]
    assert ed["text"] == "Исправленный текст" and ed["m"]["ed"]
    assert next(e for e in evs if e["type"] == "del")["items"] == [[555, 2000]]
    assert 2000 not in [m["id"] for m in call("/api/tg/history?chat=555")["messages"]]
    assert {c["id"]: c for c in call("/api/tg/dialogs")["chats"]}[555]["unread"] == 0

    # файл (скриншот из буфера): отправлен, сохранён локально без повторного скачивания
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    r = call("/api/tg/upload?chat=555&name=shot.png&caption=%D1%81%D0%BC.", raw=png)
    assert r["ok"]
    m = call("/api/tg/history?chat=555")["messages"][-1]
    assert m["media"]["st"] == "ok" and m["id"] not in fake.downloads

    # правка, реакция, пересылка, удаление своего, «Прочитано» вручную
    assert call("/api/tg/edit", {"chat": 555, "id": r["id"], "text": "см. скрин"})["ok"]
    assert call("/api/tg/react", {"chat": CH, "id": 60, "emoji": "👍"})["ok"]
    assert "SendReactionRequest" in fake.requests
    fw = call("/api/tg/forward", {"chat": CH, "ids": [50], "to": 555})
    assert fw["ok"] and call("/api/tg/history?chat=555")["messages"][-1]["m"]["fw"] == "Иван"
    assert call("/api/tg/delete", {"chat": 555, "ids": fw["ids"]})["ok"] and fake.deleted[-1][1] == fw["ids"]
    assert call("/api/tg/read", {"chat": CH})["ok"] and fake.read[-1] == (CH, 0)
    assert "пустое" in call("/api/tg/send", {"chat": 555, "text": "  "})["error"]

    # поиск: в чате (Telegram + база) и по всем чатам
    res = call(f"/api/tg/search?q=BILL-482&chat={CH}")["results"]
    assert res and all(v["chat"] == CH for v in res)
    allres = call("/api/tg/search?q=BILL-482")["results"]
    assert {v["chat"] for v in allres} >= {CH, 555}

    # пока мессенджер на связи, другие задачи берут его подключение
    src2 = TelegramSource(app.cfg(), app.store(), progress=lambda s: None)
    app.run_async(orig_connect(src2))
    assert src2.client is fake and src2._shared
    app.run_async(src2.close())
    assert fake.connected

    # ИИ в чате
    rel = call("/api/ai/related", {"text": "что с BILL-482?", "chat": 555})
    assert rel["groups"] and rel["groups"][0]["chat"] == CH and "bill-482" in rel["terms"]
    job = call("/api/ai/summary", {"chat": CH, "period": "week"})["job"]
    j = wait(lambda: (lambda x: x if x["status"] != "running" else None)(call(f"/api/jobs/{job}")))
    assert j["status"] == "done", j
    assert "### Главное" in j["result"]["text"] and j["result"]["refs"]
    job = call("/api/ai/draft", {"chat": 555, "ids": [1001]})["job"]
    j = wait(lambda: (lambda x: x if x["status"] != "running" else None)(call(f"/api/jobs/{job}")))
    assert j["result"]["text"] == "Пришлю примеры обращений сегодня до 15:00."
    job = call("/api/ai/analyze", {"chat": 555, "text": "BILL-482"})["job"]
    j = wait(lambda: (lambda x: x if x["status"] != "running" else None)(call(f"/api/jobs/{job}")))
    assert j["status"] == "done" and j["result"]["refs"] and j["result"]["refs"][0]["chat"] == CH
    job = call("/api/ai/maverick", {"chat": 555, "text": "BILL-482"})["job"]
    j = wait(lambda: (lambda x: x if x["status"] != "running" else None)(call(f"/api/jobs/{job}")))
    assert j["status"] == "error" and "Maverick не настроен" in j["error"]


def test_offline_and_errors(env):
    app, call, fake, _, _ = env
    # нет связи: переписка из базы, отправка — понятный отказ
    r = call("/api/tg/send", {"chat": 555, "text": "привет"})
    assert not r["ok"] and "нет связи" in r["error"]
    h = call("/api/tg/history?chat=555&live=1")
    assert h["ok"] and h["live"] is False
    # связь появилась и пропала — мессенджер переподключается сам
    call("/api/tg/state")
    wait(lambda: call("/api/tg/state")["state"] == "online")
    app.run_async(fake.disconnect())
    fake.connected = True
    fake._disc = None
    wait(lambda: call("/api/tg/state")["state"] == "online" and fake._disc is not None, 10)
    # ошибки Telegram — по-русски
    from opsdigest.messenger.live import human

    class ChatWriteForbiddenError(Exception):
        pass
    assert "нельзя писать" in human(ChatWriteForbiddenError())


def test_meta_and_views(tmp_path):
    """Форматирование, пересылка, реакции и кнопки сохраняются и доходят до окна."""
    from telethon.tl import types as T

    from opsdigest.config import DEFAULTS, Config
    from opsdigest.messenger.views import chat_views, msg_views
    from opsdigest.storage import Chat, Store
    store = Store(tmp_path / "db.sqlite")
    src = TelegramSource(Config(json.loads(json.dumps(DEFAULTS)), tmp_path), store, progress=lambda s: None)
    m = tmsg(5, 1, "  Сервер **упал**, см. ссылку")
    m.entities = [T.MessageEntityBold(offset=9, length=8), T.MessageEntityTextUrl(offset=23, length=6, url="https://w")]
    m.fwd_from = NS(from_name="Мониторинг")
    m.reactions = NS(results=[NS(reaction=T.ReactionEmoji(emoticon="👍"), count=2, chosen_order=0)])
    m.reply_markup = NS(rows=[NS(buttons=[NS(text="Открыть", url="https://x")])])
    store.upsert_chat(Chat(chat_id=7, title="Алерты", kind="group"))
    store.add_messages(src._convert(7, "Алерты", [m]))
    v = msg_views(store, 7, store.page(7))[0]
    t = v["text"]
    assert t.startswith("[переслано] Сервер")
    u = t.encode("utf-16-le")
    b = v["m"]["e"][0]
    assert b[0] == "b" and u[b[1] * 2:(b[1] + b[2]) * 2].decode("utf-16-le") == "**упал**"
    assert v["m"]["fw"] == "Мониторинг" and v["m"]["rx"] == [["👍", 2, True]] and v["m"]["bt"] == [["Открыть", "https://x"]]
    assert chat_views(store)[0]["last"]["text"].startswith("Сервер")


def test_forum_topics_pins_polls_links_files(env):
    """Темы форума (список, история темы, отправка в тему, прочитано, без звука), закреп, упоминания, опрос,
    вкладки медиа, ссылки t.me → чат, ответ из другого чата и цитата, файл — «Сохранить в Загрузки»."""
    from telethon.tl import types as T
    app, call, fake, orig_connect, llm = env
    F = -1000000002002
    tm = []
    for i, (topic, text) in enumerate([(2001, "релиз ЛК 4.12"), (2001, "откатили"), (2002, "хотфикс"), (None, "общая")]):
        m = tmsg(2010 + i, 10 - i, text)
        if topic:
            m.reply_to = T.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic)
        tm.insert(0, m)
    reply_in_topic = tmsg(2020, 1, "ответ внутри темы")
    reply_in_topic.reply_to = T.MessageReplyHeader(forum_topic=True, reply_to_msg_id=2010, reply_to_top_id=2001)
    tm.insert(0, reply_in_topic)
    fd = dialog(F, "Релизы", tm)
    fd.entity.forum = True
    fake.chats[F] = (fd, tm)
    fake.forum_topics = [(2001, "Релизы ЛК", 2020), (2002, "Хотфиксы", 2012)]
    group = fake.chats[CH][1]
    group[0].pinned = True
    group[1].mentioned = True
    ext = tmsg(70, 0.2, "про тот же сбой")
    ext.reply_to = T.MessageReplyHeader(reply_to_msg_id=1001, reply_to_peer_id=T.PeerUser(555), quote_text="Пришлёшь примеры?")
    poll = tmsg(71, 0.1, "")
    poll.media = T.MessageMediaPoll(poll=T.Poll(id=1, question=T.TextWithEntities("Когда релиз?", []),
                                                answers=[T.PollAnswer(T.TextWithEntities("Сегодня", []), b"\x00"),
                                                         T.PollAnswer(T.TextWithEntities("Завтра", []), b"\x01")], hash=0),
                                    results=T.PollResults(results=[T.PollAnswerVoters(option=b"\x00", voters=3)],
                                                          total_voters=3))
    poll.poll = poll.media.poll
    group[:0] = [poll, ext]
    call("/api/tg/connect", {})
    wait(lambda: call("/api/tg/state")["state"] == "online")
    wait(lambda: any(c["id"] == F for c in call("/api/tg/dialogs")["chats"]))
    assert next(c for c in call("/api/tg/dialogs")["chats"] if c["id"] == F)["forum"]
    # темы
    t = {x["topic_id"]: x for x in call(f"/api/tg/topics?chat={F}&live=1")["topics"]}
    assert t[2001]["title"] == "Релизы ЛК" and t[2001]["unread"] == 2 and t[2002]["title"] == "Хотфиксы"
    h = call(f"/api/tg/history?chat={F}&topic=2001&live=1&limit=20")
    ids = [v["id"] for v in h["messages"]]
    assert 2010 in ids and 2011 in ids and 2012 not in ids and 2013 not in ids
    r = next(v for v in h["messages"] if v["id"] == 2020)
    assert r["reply"] == 2010 and r["topic"] == 2001 and r["rp"]["text"] == "релиз ЛК 4.12"
    assert "reply" not in next(v for v in h["messages"] if v["id"] == 2011)  # «ответ» на начало темы — это тема
    gen = [v["id"] for v in call(f"/api/tg/history?chat={F}&topic=1&live=1&limit=20")["messages"]]
    assert gen == [2013]
    assert call("/api/tg/send", {"chat": F, "text": "в тему", "topic": 2001})["ok"]
    assert call("/api/tg/mute", {"chat": F, "topic": 2002, "on": True})["ok"]
    assert call("/api/tg/read_topic", {"chat": F, "topic": 2001})["ok"]
    t = {x["topic_id"]: x for x in call(f"/api/tg/topics?chat={F}")["topics"]}
    assert t[2002]["muted"] and t[2001]["unread"] == 0
    # закреп и упоминания
    p = call(f"/api/tg/pinned?chat={CH}")["pinned"]
    assert [v["id"] for v in p] == [group[2].id]
    assert call(f"/api/tg/marks?chat={CH}")["mentions"] == [group[3].id]
    # опрос и ответ из другого чата
    h = {v["id"]: v for v in call(f"/api/tg/history?chat={CH}&live=1&limit=10")["messages"]}
    pv = h[71]["m"]["poll"]
    assert pv["q"] == "Когда релиз?" and pv["opts"][0]["n"] == 3 and pv["total"] == 3 and "Когда релиз?" in h[71]["text"]
    assert call("/api/tg/vote", {"chat": CH, "id": 71, "options": [pv["opts"][1]["o"]]})["ok"]
    x = h[70]["m"]["rx_"]
    assert x["c"] == 555 and x["id"] == 1001 and x["title"] == "Пётр" and x["q"] == "Пришлёшь примеры?" and "reply" not in h[70]
    # вкладки: медиа — только с вложениями
    med = call(f"/api/tg/media?chat={CH}&kind=media")["items"]
    assert med and all(v.get("media") for v in med)
    # ссылки Telegram → чат и сообщение (и тема)
    assert call("/api/tg/resolve", {"link": "https://t.me/c/1001/30"}) == {"ok": True, "chat": CH, "msg": 30, "topic": None}
    assert call("/api/tg/resolve", {"link": "https://t.me/c/2002/2001/2011"})["topic"] == 2001
    assert call("/api/tg/resolve", {"link": "https://t.me/+AbCdEf"})["external"]
    # файл из переписки — копия в «Загрузки/OpsDigest»
    import os
    os.environ["HOME"] = str(app.ws.parent / "home")
    st = app.store()
    mid = next(x["id"] for x in st.media_list(chat_ids=[CH], kinds=None) if x["status"] == "ok")
    st.close()
    r = call("/api/tg/file", {"media": mid, "action": "save"})
    assert r["ok"] and os.path.getsize(r["path"]) > 0 and "Downloads" in r["path"]
