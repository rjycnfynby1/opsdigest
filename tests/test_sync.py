"""Синхронизация Telegram на поддельном клиенте: первая загрузка, новые сообщения, догрузка истории."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from opsdigest.config import Config, DEFAULTS
from opsdigest.sources.telegram import TelegramSource, classify, message_text, split_message
from opsdigest.storage import Store

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def msg(i, hours_ago, text, **kw):
    return NS(id=i, date=NOW - timedelta(hours=hours_ago), message=text, out=kw.get("out", False),
              mentioned=kw.get("mentioned", False), sender=NS(first_name=kw.get("who", "Иван"), last_name=""),
              sender_id=7, reply_to_msg_id=kw.get("reply"), action=kw.get("action"), fwd_from=None, media=None,
              photo=None, document=None, voice=None, video=None, video_note=None, sticker=None, poll=None,
              web_preview=None, file=None)


class FakeClient:
    def __init__(self, chats):
        self.chats = chats  # id -> (dialog, [messages новые->старые])
        self.calls = []

    async def iter_dialogs(self, archived=None, limit=None):
        for d, _ in self.chats.values():
            yield d

    async def iter_messages(self, entity, limit=None, min_id=0, offset_id=0):
        self.calls.append((entity.id, min_id, offset_id))
        n = 0
        for m in self.chats[entity.id][1]:
            if min_id and m.id <= min_id:
                continue
            if offset_id and m.id >= offset_id:
                continue
            yield m
            n += 1
            if limit and n >= limit:
                return


def dialog(cid, name, msgs, kind="group"):
    ent = NS(id=cid, username=None, bot=False, is_self=False)
    return NS(id=cid, name=name, entity=ent, is_user=kind == "private", is_group=kind == "group",
              is_channel=kind == "channel", unread_count=2, date=msgs[0].date if msgs else None,
              message=msgs[0] if msgs else None, dialog=NS(notify_settings=NS(mute_until=None)))


def make(tmp_path, msgs_by_chat, rules=None):
    import copy
    data = copy.deepcopy(DEFAULTS)
    data["telegram"]["max_messages_per_chat"] = 50
    data["telegram"]["chats"] = rules or []
    cfg = Config(data, tmp_path)
    store = Store(tmp_path / "db.sqlite")
    chats = {cid: (dialog(cid, name, msgs), msgs) for cid, (name, msgs) in msgs_by_chat.items()}
    src = TelegramSource(cfg, store, progress=lambda s: None)
    src.client = FakeClient(chats)
    return src, store, chats


def test_first_sync_incremental_and_backfill(tmp_path):
    msgs = [msg(i, 100 - i, f"сообщение {i} INC00{48000 + i}") for i in range(100, 0, -1)]  # id 100 — новое
    src, store, chats = make(tmp_path, {-1001: ("Авария X", msgs), -1002: ("Флуд", [msg(5, 1, "привет")])},
                             rules=[{"match": "Флуд", "priority": "ignore"}])
    # первая синхронизация: последние 10 часов
    st = asyncio.run(src.sync(NOW - timedelta(hours=10)))
    got = store.messages(-1001, "2000", "3000")
    assert [m.msg_id for m in got] == list(range(90, 101)) and st["messages"] == 11  # граница включительно
    assert store.get_chat(-1002).priority == "ignore" and not store.messages(-1002, "2000", "3000")
    assert store.entities_between("2000", "3000")
    # пришли новые сообщения — забираем только их
    new = [msg(102, 0, "ещё"), msg(101, 0.5, "новое")]
    chats[-1001][1][:0] = new
    chats[-1001][0].message, chats[-1001][0].date = new[0], new[0].date
    asyncio.run(src.sync(NOW - timedelta(hours=10)))
    assert src.client.calls[-1] == (-1001, 100, 0)
    assert len(store.messages(-1001, "2000", "3000")) == 13
    # «после отпуска»: просим 30 часов — догружается история старше уже загруженной
    asyncio.run(src.sync(NOW - timedelta(hours=30)))
    assert src.client.calls[-1] == (-1001, 0, 90)
    ids = [m.msg_id for m in store.messages(-1001, "2000", "3000")]
    assert min(ids) == 70 and len(ids) == 33
    # повтор с тем же периодом не ходит за историей снова
    n_calls = len(src.client.calls)
    asyncio.run(src.sync(NOW - timedelta(hours=30)))
    assert len(src.client.calls) == n_calls


def test_service_and_media(tmp_path):
    m1 = msg(3, 1, "", action=NS())  # служебное — пропускаем
    m2 = msg(2, 1, "смотри")
    m2.photo = NS()
    m3 = msg(1, 1, "", out=True)
    m3.document, m3.file = NS(), NS(name="log.txt")
    src, store, _ = make(tmp_path, {555: ("Пётр", [m1, m2, m3])})
    src.client.chats[555][0].is_user, src.client.chats[555][0].is_group = True, False
    asyncio.run(src.sync(NOW - timedelta(hours=5)))
    got = store.messages(555, "2000", "3000")
    assert [m.text for m in got] == ["[файл: log.txt]", "[фото] смотри"]
    assert got[0].is_out and store.get_chat(555).kind == "private"


def test_helpers():
    assert classify(NS(entity=NS(bot=True), is_user=True)) == "bot"
    assert message_text(msg(1, 0, "x" * 9000)).endswith("[обрезано]")
    parts = split_message("строка\n" * 1500)
    assert all(len(p) <= 4000 for p in parts) and len(parts) == 3


def test_failover_switches_proxy(tmp_path):
    from opsdigest.sources.tg_proxy import parse_proxy
    good_msgs = [msg(2, 1, "после переключения"), msg(1, 2, "первое")]
    src, store, chats = make(tmp_path, {-1001: ("Чат", good_msgs)})

    class Broken(FakeClient):
        async def iter_dialogs(self, archived=None, limit=None):
            raise ConnectionError("proxy died")
            yield  # pragma: no cover

    p1 = parse_proxy("tg://proxy?server=one&port=443&secret=dd00112233445566778899aabbccddeeff")
    p2 = parse_proxy("socks5://two:1080")
    src.client, src.proxy = Broken(chats), p1
    store.kv_set("tg_proxy_ok", p1.raw)
    seen_skips = []

    async def fake_connect(interactive=False, skip=None):
        seen_skips.append(set(skip or ()))
        src.client, src.proxy = FakeClient(chats), p2
        store.kv_set("tg_proxy_ok", p2.raw)

    src.connect = fake_connect
    st = asyncio.run(src.with_failover(src.sync, NOW - timedelta(hours=5)))
    assert st["messages"] == 2 and seen_skips == [{p1.raw}]
    assert store.kv_get("tg_proxy_ok") == p2.raw and src.proxy.host == "two"


def test_service_chat_never_read(tmp_path):
    codes = [msg(2, 1, "Login code: 12345. Do not give this code to anyone")]
    src, store, chats = make(tmp_path, {777000: ("Telegram", codes), -1001: ("Чат", [msg(1, 1, "привет")])})
    st = asyncio.run(src.sync(NOW - timedelta(hours=5)))
    assert store.get_chat(777000) is None and not store.messages(777000, "2000", "3000")
    assert st["dialogs"] == 1 and st["messages"] == 1
