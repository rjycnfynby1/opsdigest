"""Выгрузка всей истории через API на поддельном клиенте: файлы, остановка и продолжение, исключённые чаты,
докачка тяжёлого файла, сводка и вопрос с изображениями."""
import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import fake_llm
from fake_llm import start_server

from opsdigest import media
from opsdigest.config import DEFAULTS, Config
from opsdigest.digest import DigestBuilder
from opsdigest.llm import make_client
from opsdigest.sources.telegram import TelegramSource
from opsdigest.storage import Store

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def jpeg(tag) -> bytes:
    return b"\xff\xd8\xff\xe0" + str(tag).encode() * 30


def tmsg(i, hours_ago, text="", kind=None, name=None, size=None):
    """Сообщение Telethon (минимум полей). kind: photo | video | log | big | voice."""
    m = NS(id=i, date=NOW - timedelta(hours=hours_ago), message=text, out=False, mentioned=False,
           sender=NS(first_name="Иван", last_name=""), sender_id=7, reply_to_msg_id=None, action=None, fwd_from=None,
           media=None, photo=None, document=None, voice=None, video=None, video_note=None, sticker=None, poll=None,
           web_preview=None, gif=None, audio=None, file=None, _data=None)
    if kind == "photo":
        m.photo = NS(sizes=[NS(type="m", w=320, h=240), NS(type="y", w=1280, h=960)])
        m.file = NS(name=None, mime_type="image/jpeg", size=90000, width=1280, height=960, duration=None)
        m._data = jpeg(f"p{i}")
    elif kind in ("video", "voice"):
        setattr(m, kind, NS())
        m.document = NS()
        m.file = NS(name=name, mime_type="video/mp4" if kind == "video" else "audio/ogg", size=size or 80 * 2**20,
                    width=None, height=None, duration=42)
        m._data = b"V" * 1000
    elif kind in ("log", "big"):
        m.document = NS()
        m.file = NS(name=name, mime_type="text/plain", size=size or 300, width=None, height=None, duration=None)
        m._data = "ERROR INC0055555 база недоступна".encode() if kind == "log" else b"Z" * 100
    if kind:
        m.media = NS()
    return m


class FakeTG:
    def __init__(self, chats):
        self.chats = chats  # id -> (dialog, [messages новые->старые])
        self.downloads = []
        self.fail_after = None

    async def iter_dialogs(self, archived=None, limit=None):
        for d, _ in self.chats.values():
            yield d

    async def iter_messages(self, entity, limit=None, min_id=0, offset_id=0, reverse=False):
        msgs = [m for m in self.chats[entity.id][1] if (not min_id or m.id > min_id) and (not offset_id or m.id < offset_id)]
        if reverse:
            msgs = list(reversed(msgs))
        for n, m in enumerate(msgs):
            if limit and n >= limit:
                return
            if self.fail_after is not None:
                self.fail_after -= 1
                if self.fail_after < 0:
                    raise ConnectionError("связь оборвалась")
            yield m

    async def download_media(self, m, file=None, thumb=None):
        self.downloads.append((m.id, thumb))
        if file is bytes:
            return m._data
        with open(file, "wb") as f:
            f.write(m._data)
        return file

    async def get_messages(self, entity, ids=None):
        by = {m.id: m for m in self.chats[entity.id][1]}
        return [by.get(i) for i in ids]

    async def get_input_entity(self, chat_id):
        if chat_id in self.chats:
            return self.chats[chat_id][0].entity
        raise ValueError("нет такого")


def dialog(cid, name, msgs, kind="group"):
    ent = NS(id=cid, username=None, bot=False, is_self=False)
    return NS(id=cid, name=name, entity=ent, is_user=kind == "private", is_group=kind == "group",
              is_channel=kind == "channel", unread_count=0, date=msgs[0].date if msgs else None,
              message=msgs[0] if msgs else None, dialog=NS(notify_settings=NS(mute_until=None)))


def make(tmp_path, chats, rules=None):
    data = copy.deepcopy(DEFAULTS)
    data["telegram"]["chats"] = rules or []
    cfg = Config(data, tmp_path)
    store = Store(tmp_path / "db.sqlite")
    fake = FakeTG({cid: (dialog(cid, name, msgs), msgs) for cid, (name, msgs) in chats.items()})
    src = TelegramSource(cfg, store, progress=lambda s: None)
    src.client = fake
    return src, store, cfg, fake


def history():
    """450 сообщений за ~19 дней: фото каждое 10-е, видео, голосовое, лог, крупный архив."""
    msgs = []
    for i in range(450, 0, -1):
        kind = "photo" if i % 10 == 0 else None
        msgs.append(tmsg(i, 450 - i, f"сообщение {i}", kind))
    special = {445: tmsg(445, 5, "запись созвона", "video", "созвон.mp4"),
               444: tmsg(444, 6, "", "voice", None, 30000),
               443: tmsg(443, 7, "лог приложения", "log", "app.log"),
               442: tmsg(442, 8, "дамп", "big", "dump.log", 50 * 2**20)}
    return [special.get(m.id, m) for m in msgs]


def test_export_full_history_with_files(tmp_path):
    src, store, cfg, fake = make(tmp_path, {-1001: ("Авария X", history()),
                                            -1002: ("Флуд", [tmsg(1, 1, "мем", "photo")]),
                                            555: ("Пётр", [tmsg(2, 1, "привет")])},
                                 rules=[{"match": "-1002", "priority": "low", "exclude": True}])
    st = asyncio.run(src.export_history())
    assert st["excluded"] == 1 and store.messages(-1002, "2000", "3000") == []
    got = store.messages(-1001, "2000", "3000")
    assert len(got) == 450 and store.history_done(-1001) and st["complete"] == 2
    photos = store.media_list(chat_ids=[-1001], kinds=media.IMAGE_KINDS)
    assert len(photos) == 45 and all(p["status"] == "ok" and cfg.path(p["path"]).is_file() for p in photos)
    assert photos[0]["path"].startswith("media/Авария X [-1001]/2026-")
    assert all(t == "y" for _, t in fake.downloads if t)  # размер фото до 1280 px
    by = {r["msg_id"]: r for r in store.media_list(chat_ids=[-1001], kinds=None)}
    assert by[445]["status"] == "ref" and by[445]["name"] == "созвон.mp4"   # тяжёлое — только ссылка
    assert by[444]["status"] == "ref" and by[442]["status"] == "ref"
    assert by[443]["status"] == "ok" and by[443]["described"] and "INC0055555" in by[443]["text"]
    assert [m.msg_id for m, _ in store.search('"inc0055555"')] == [443]   # содержимое лога ищется
    # повторный запуск: ничего не перекачивает и историю не перечитывает
    n = len(fake.downloads)
    st2 = asyncio.run(src.export_history())
    assert st2["messages"] == 0 and len(fake.downloads) == n
    # появились новые сообщения — забираются только они
    new = [tmsg(452, 0, "новое", "photo"), tmsg(451, 0.5, "ещё")]
    fake.chats[-1001][1][:0] = new
    fake.chats[-1001][0].message = new[0]
    st3 = asyncio.run(src.export_history())
    assert st3["messages"] == 2 and len(store.messages(-1001, "2000", "3000")) == 452
    # тяжёлый файл — по запросу
    row = asyncio.run(src.fetch_media(by[445]["id"]))
    assert row["status"] == "ok" and cfg.path(row["path"]).read_bytes() == b"V" * 1000


def test_export_stop_and_resume(tmp_path):
    src, store, cfg, fake = make(tmp_path, {-1001: ("Авария X", history())})
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3   # остановиться после первых порций
    st = asyncio.run(src.export_history(cancel=cancel))
    have = len(store.messages(-1001, "2000", "3000"))
    assert st["cancelled"] and 0 < have < 450 and not store.history_done(-1001)
    # обрыв связи посреди выгрузки — следующий запуск продолжает, без дублей
    fake.fail_after = 150
    try:
        asyncio.run(src.export_history())
    except ConnectionError:
        pass
    fake.fail_after = None
    asyncio.run(src.export_history())
    ids = [m.msg_id for m in store.messages(-1001, "2000", "3000")]
    assert len(ids) == 450 == len(set(ids)) and store.history_done(-1001)
    assert len(store.media_list(chat_ids=[-1001])) == 45


def test_export_since_and_one_chat(tmp_path):
    src, store, cfg, fake = make(tmp_path, {-1001: ("Авария X", history()), -1003: ("Другой", [tmsg(9, 1, "x")])},
                                 rules=[{"match": "-1001", "exclude": True}])
    # исключённый чат выгружается, если его попросили явно; since ограничивает глубину
    st = asyncio.run(src.export_history(chat_ids={-1001}, since=NOW - timedelta(hours=100)))
    got = store.messages(-1001, "2000", "3000")
    assert st["dialogs"] == 1 and 99 <= len(got) <= 101 and not store.history_done(-1001)
    assert store.messages(-1003, "2000", "3000") == []


def test_download_pending_after_import(tmp_path):
    """Сообщения из экспорта Telegram Desktop без фото: фото докачиваются через API по номерам."""
    msgs = [tmsg(3, 1, "скрин", "photo"), tmsg(2, 2, "текст"), tmsg(1, 3, "ещё скрин", "photo")]
    src, store, cfg, fake = make(tmp_path, {-1001: ("Авария X", msgs)})
    store.add_media([{"chat_id": -1001, "msg_id": i, "kind": "photo", "status": "pending",
                      "date": (NOW - timedelta(hours=h)).isoformat()} for i, h in ((3, 1), (1, 3))])
    from opsdigest.storage import Chat
    store.upsert_chat(Chat(-1001, "Авария X", "group"))
    rows = store.media_list(status=("pending",))
    st = asyncio.run(src.download_rows(rows))
    assert st["downloaded"] == 2 and all(r["status"] == "ok" for r in store.media_list())
    fake.chats[-1001][1].pop(0)  # сообщение удалили
    store.add_media([{"chat_id": -1001, "msg_id": 3, "kind": "photo", "status": "pending"}])
    store.set_media_status(store.media_of(-1001, [3])[3]["id"], "pending")
    st = asyncio.run(src.download_rows(store.media_list(status=("pending",))))
    assert st["gone"] == 1


def test_digest_and_ask_with_images(tmp_path):
    srv, url = start_server()
    msgs = [tmsg(5, 1, "смотрите, что на проде", "photo"), tmsg(4, 2, "мем дня", "photo"),
            tmsg(3, 3, "видео аварии", "video", "авария.mp4"), tmsg(2, 4, "привет")]
    src, store, cfg, fake = make(tmp_path, {-1001: ("Авария X", msgs)},
                                 rules=[{"match": "-1001", "priority": "high", "kind": "incident",
                                         "description": "Я — L2. Следить за ошибками оплаты, мемы не важны."}])
    cfg.data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "model": "fake"}}
    asyncio.run(src.export_history())
    fake_llm.IMAGES[hashlib.sha256(jpeg("p5")).hexdigest()] = {
        "kind": "скриншот ошибки", "summary": "Ошибка оплаты PAYMENT_DECLINED на проде",
        "details": "Страница оплаты с красной ошибкой.", "text": "PAYMENT_DECLINED INC0066666", "importance": 5}
    try:
        llm = make_client(*cfg.llm_profile())
        d = DigestBuilder(cfg, store, llm, progress=lambda s: None).build(NOW - timedelta(days=1), NOW + timedelta(hours=1))
        maps = [c["messages"][-1]["content"] for c in srv.calls if isinstance(c["messages"][-1]["content"], str)
                and "=== ЧАТ" in c["messages"][-1]["content"]]
        assert maps and "[фото: Ошибка оплаты PAYMENT_DECLINED на проде; текст: PAYMENT_DECLINED INC0066666]" in maps[0]
        assert "Описание чата от пользователя" in maps[0] and "мемы не важны" in maps[0]
        assert "[видео «авария.mp4», 80 МБ, 42 с — не скачан, файл m" in maps[0]
        assert d["meta"]["images"]["to_describe"] == 2 and not d["meta"]["errors"]  # все новые — описаны
        imgs = d["chats"][0]["images"]
        assert imgs and imgs[0]["src"].startswith("../media/") and imgs[0]["summary"]
        from opsdigest.render import render_html
        assert "<img loading='lazy' src='../media/" in render_html(d)
        assert store.entity_history("INC0066666")
        # вопрос: найдётся по тексту со скриншота; модель просит видео
        orig = fake_llm.respond

        def respond(system, user):
            if user.startswith("Вопрос:"):
                vid = store.media_list(kinds=("video",))[0]["id"]
                return f"Ошибка PAYMENT_DECLINED (#5).\nНУЖНЫ ФАЙЛЫ: m{vid}"
            return orig(system, user)
        fake_llm.respond = respond
        from opsdigest.ask import answer
        r = answer(cfg, store, llm, "что за payment_declined?", days=0, progress=lambda s: None)
        assert r["answer"] == "Ошибка PAYMENT_DECLINED (#5)." and r["need_files"][0]["name"] == "авария.mp4"
        assert r["images"] and r["images"][0]["msg_id"] == 5
    finally:
        fake_llm.respond = orig if "orig" in dir() else fake_llm.respond
        fake_llm.IMAGES.clear()


def test_ask_model_picks_images(tmp_path):
    srv, url = start_server()
    msgs = [tmsg(3, 1, "вот ошибка BILL-482", "photo"), tmsg(2, 2, "что по BILL-482?")]
    src, store, cfg, fake = make(tmp_path, {-1001: ("Биллинг", msgs)})
    cfg.data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "model": "fake"}}
    cfg.data["media"]["describe_in_digest"] = False
    store_rows = asyncio.run(src.export_history())
    assert store_rows["downloaded"] == 1
    from opsdigest.ask import answer
    r = answer(cfg, store, make_client(*cfg.llm_profile()), "что с BILL-482?", days=0, progress=lambda s: None)
    assert r["described"] == 1 and store.media_list(undescribed=True) == []
    triage = [c for c in srv.calls if isinstance(c["messages"][-1]["content"], str)
              and "Выбери изображения" in c["messages"][-1]["content"]]
    assert triage and "вот ошибка BILL-482" in triage[0]["messages"][-1]["content"]
    final = srv.calls[-1]["messages"][-1]["content"]
    assert "[фото: Картинка без подробностей" in final
    assert json.dumps(r, ensure_ascii=False)
