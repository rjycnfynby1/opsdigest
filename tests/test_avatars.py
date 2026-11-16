"""Аватарки чатов и людей: скачивание фото профиля (только если нет или сменилось), отдача окну, отчёт."""
import asyncio
import copy
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace as NS

from fake_llm import start_server
from test_ui import ui  # noqa: F401 — фикстура окна
from test_ui_media import setup_ws

from opsdigest import avatars
from opsdigest.config import DEFAULTS, Config
from opsdigest.demo import load_demo
from opsdigest.digest import DigestBuilder
from opsdigest.llm import make_client
from opsdigest.messenger.views import chat_views
from opsdigest.render import render_html
from opsdigest.storage import Chat, Store

JPEG = b"\xff\xd8\xff\xe0" + b"avatar" * 30


class FakeTG:
    def __init__(self):
        self.calls = 0

    async def download_profile_photo(self, entity, file=None, download_big=True):
        self.calls += 1
        assert file is bytes and download_big is False
        return JPEG + str(entity.photo.photo_id).encode()


def test_fetch_only_when_needed(tmp_path):
    cfg = Config(copy.deepcopy(DEFAULTS), tmp_path)
    st = Store(":memory:")
    st.upsert_chat(Chat(-100, "Авария ЛК", "group"))
    tg = FakeTG()
    ent = NS(photo=NS(photo_id=111))
    p = asyncio.run(avatars.fetch(tg, st, cfg, ent, -100))
    assert p and p.read_bytes().startswith(b"\xff\xd8") and "a" in p.name and "100" not in p.name
    assert asyncio.run(avatars.fetch(tg, st, cfg, ent, -100)) == p and tg.calls == 1       # уже есть
    ent.photo.photo_id = 222                                                              # фото сменилось
    asyncio.run(avatars.fetch(tg, st, cfg, ent, -100))
    assert tg.calls == 2 and p.read_bytes().endswith(b"222")
    # фото нет — запоминаем, не спрашиваем снова
    assert asyncio.run(avatars.fetch(tg, st, cfg, NS(photo=None), 5)) is None
    assert avatars.row(st, 5)["status"] == "none" and not avatars.needs_fetch(st, 5, "")
    # в списке чатов — номер фото (окно просит аватарку только если оно есть)
    st.set_chat_state(-100, photo="222")
    assert next(c for c in chat_views(st) if c["id"] == -100)["photo"] == "222"
    assert not avatars.needs_fetch(st, -100)
    st.set_chat_state(-100, photo="333")
    assert avatars.needs_fetch(st, -100)


def test_report_has_chat_avatars(tmp_path):
    srv, url = start_server()
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["profiles"]["local"].update(base_url=url, model="fake")
    cfg = Config(data, tmp_path)
    st = Store(":memory:")
    since, until = load_demo(cfg, st, timezone.utc)
    cid = -1001000000001   # «Авария: недоступен личный кабинет»
    asyncio.run(avatars.fetch(FakeTG(), st, cfg, NS(photo=NS(photo_id=7)), cid))
    d = DigestBuilder(cfg, st, make_client(*cfg.llm_profile()), progress=lambda s: None).build(since, until, "day")
    c = next(x for x in d["chats"] if x["chat_id"] == cid)
    assert c["avatar"].startswith("../media/avatars/a") and c["avatar"].endswith(".jpg")
    html = render_html(d)
    assert f"src='{c['avatar']}'" in html and "class='cav" in html
    assert any(t.get("chat_avatar") for t in d["overview"]["top"])


def test_window_serves_avatar(ui):  # noqa: F811
    app, call, llm_url, tmp = ui
    setup_ws(call, tmp, llm_url)
    cfg = app.cfg()
    st = app.store()
    st.upsert_chat(Chat(-7, "Чат", "group"))
    asyncio.run(avatars.fetch(FakeTG(), st, cfg, NS(photo=NS(photo_id=9)), -7))
    st.set_chat_state(-7, photo="9")
    st.close()
    code, body = call("/avatar/-7", raw=True)
    assert code == 200 and body.startswith(b"\xff\xd8")
    assert call("/avatar/-8", raw=True)[0] == 404         # нет и Telegram не на связи — окно покажет буквы
    assert call("/avatar/-7", token=False, raw=True)[0] == 403
    assert Path(cfg.path(avatars.rel_path(cfg, -7))).is_file()
