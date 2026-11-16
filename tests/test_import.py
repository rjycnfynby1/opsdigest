"""Загрузка экспорта Telegram Desktop: полный и одного чата, потоковое чтение, без дублей, догрузка через API."""
import json

import pytest

from opsdigest import importer
from opsdigest.config import load_config
from opsdigest.storage import Store

ME = 555


def msg(i, ts, text, frm="Иван", fid="user100", **kw):
    return {"id": i, "type": "message", "date": "2026-11-01T10:00:00", "date_unixtime": str(ts), "from": frm,
            "from_id": fid, "text": text, "text_entities": [], **kw}


def full_export(tmp_path, chats, big=False):
    data = {"about": "Here is the data you requested.", "personal_information": {"user_id": ME, "first_name": "Я"},
            "contacts": {"about": "x", "list": [{"first_name": "Пётр", "phone_number": "+7"}]},
            "chats": {"about": "This page lists all chats from this export.", "list": chats},
            "left_chats": {"about": "x", "list": [{"name": "Ушёл", "type": "public_channel", "id": 9, "messages": []}]}}
    d = tmp_path / "DataExport_2026-11-02"
    d.mkdir()
    (d / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path):
    cfgf = tmp_path / "config.json"
    cfgf.write_text(json.dumps({"workspace": str(tmp_path)}), encoding="utf-8")
    cfg = load_config(cfgf)
    store = Store(tmp_path / "base.sqlite")
    yield cfg, store, tmp_path
    store.close()


def test_full_export_and_ids(env):
    cfg, store, tmp = env
    chats = [
        {"name": "Дежурка L2", "type": "private_supergroup", "id": 1234567890, "messages": [
            msg(10, 1793500000, "Упал INC0012345, кто смотрит?"),
            {"id": 11, "type": "service", "date_unixtime": "1793500001", "action": "pin_message"},
            msg(12, 1793500100, ["Смотрю ", {"type": "bold", "text": "я"}], frm="Я", fid=f"user{ME}", reply_to_message_id=10),
            msg(13, 1793500200, "", photo="(File not included)", forwarded_from="Канал")]},
        {"name": "Пётр", "type": "personal_chat", "id": 100, "messages": [msg(1, 1793400000, "привет")]},
        {"name": "Старая группа", "type": "private_group", "id": 4321, "messages": [msg(5, 1793300000, "текст")]},
        {"type": "saved_messages", "id": ME, "messages": [msg(1, 1793300000, "заметка")]},
        {"name": "Telegram", "type": "verification_codes", "id": 777000, "messages": [msg(1, 1793300000, "Login code: 1")]},
    ]
    d = full_export(tmp, chats)
    st = importer.import_path(cfg, store, d, progress=lambda s: None)
    assert (st["files"], st["chats"], st["messages"], st["new"]) == (1, 3, 5, 5)
    sg = store.get_chat(-1001234567890)
    assert sg.title == "Дежурка L2" and sg.kind == "group" and (sg.max_msg_id, sg.min_msg_id) == (13, 10)
    assert store.get_chat(100).kind == "private" and store.get_chat(-4321) is not None
    assert store.get_chat(ME) is None and store.get_chat(777000) is None
    rows = store.messages(-1001234567890, "2000", "3000")
    assert [m.msg_id for m in rows] == [10, 12, 13]
    assert rows[1].text == "Смотрю я" and rows[1].is_out and rows[1].reply_to == 10
    assert rows[2].text == "[переслано, фото]" and rows[0].sender_id == 100
    assert any(r["value"] == "INC0012345" for r in store.entities_between("2000", "3000"))
    st2 = importer.import_path(cfg, store, d / "result.json", progress=lambda s: None)
    assert st2["messages"] == 5 and st2["new"] == 0          # повторная загрузка — без дублей


def test_single_chat_export_and_streaming(env, monkeypatch):
    cfg, store, tmp = env
    monkeypatch.setattr(importer._Stream, "CHUNK", 257)      # мелкие куски — проверка потокового чтения
    many = [msg(i, 1793000000 + i, f"сообщение {i} " + "х" * (i % 50)) for i in range(1, 800)]
    data = {"name": "Один чат", "type": "public_supergroup", "id": 777, "messages": many}
    f = tmp / "ChatExport_2026" / "result.json"
    f.parent.mkdir()
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    st = importer.import_path(cfg, store, tmp / "ChatExport_2026", progress=lambda s: None)
    assert st["messages"] == 799 and store.get_chat(-1000000000777).max_msg_id == 799


def test_bad_inputs(env):
    cfg, store, tmp = env
    with pytest.raises(importer.ImportError_, match="result.json"):
        importer.find_exports(tmp)
    (tmp / "result.json").write_text('{"chats": {"list": [{"name": "a", "type": "personal_chat", "id": 1, "messages": [{"id": 1,',
                                     encoding="utf-8")
    with pytest.raises(importer.ImportError_, match="обрывается"):
        importer.import_path(cfg, store, tmp / "result.json", progress=lambda s: None)
