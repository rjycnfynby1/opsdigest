"""Вложения и изображения: правило скачивания, база, поиск по описаниям, описание моделью, Maverick с картинками."""
import copy
import hashlib
import json
import struct
import zipfile
import zlib
from types import SimpleNamespace as NS

import pytest
import fake_llm
from fake_llm import start_server

from opsdigest import media
from opsdigest.config import DEFAULTS, Config
from opsdigest.llm import LLMError, make_client
from opsdigest.storage import Chat, Message, Store
from opsdigest.vision import Describer, VisionUnavailable, check, parse_description


def cfg_for(tmp_path, **media_over):
    data = copy.deepcopy(DEFAULTS)
    data["media"].update(media_over)
    return Config(data, tmp_path)


def jpeg(tag: str) -> bytes:
    """Псевдо-JPEG: сигнатура + уникальный хвост (для модели важны только байты)."""
    return b"\xff\xd8\xff\xe0" + tag.encode() * 20


def put_image(cfg, store, chat_id, msg_id, data: bytes, date="2026-11-01T10:00:00+00:00", title="Чат"):
    store.add_media([{"chat_id": chat_id, "msg_id": msg_id, "date": date, "kind": "photo", "mime": "image/jpeg",
                      "status": "pending"}])
    row = store.media_of(chat_id, [msg_id])[msg_id]
    rel = media.rel_path(cfg, store, row, title)
    f = cfg.path(rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    store.set_media_file(row["id"], rel, hashlib.sha256(data).hexdigest(), size=len(data))
    return store.get_media(row["id"])


# ---------------------------------------------------------------- правило и метаданные
def test_policy_and_tg_info(tmp_path):
    pol = media.Policy(cfg_for(tmp_path, light_max_mb=1))
    assert pol.status({"kind": "photo"}) == "pending"
    assert pol.status({"kind": "image", "size": 5 * 2**20}) == "pending"
    assert pol.status({"kind": "image", "size": 50 * 2**20}) == "ref"
    assert pol.status({"kind": "document", "name": "app.log", "size": 200_000}) == "pending"
    assert pol.status({"kind": "document", "name": "dump.zip", "size": 2 * 2**20}) == "ref"
    assert pol.status({"kind": "document", "name": "setup.exe", "size": 10}) == "ref"
    assert pol.status({"kind": "video", "size": 10}) == "ref" and pol.status({"kind": "voice", "size": 10}) == "ref"
    assert media.Policy(cfg_for(tmp_path, download=False)).status({"kind": "photo"}) == "ref"

    sizes = [NS(type="s", w=90, h=67), NS(type="m", w=320, h=240), NS(type="x", w=800, h=600),
             NS(type="y", w=1280, h=960), NS(type="w", w=2560, h=1920)]
    m = NS(photo=NS(sizes=sizes), file=NS(name=None, mime_type="image/jpeg", size=500000, width=2560, height=1920,
                                          duration=None))
    assert media.tg_info(m)["kind"] == "photo" and media.photo_thumb(m, 1280) == "y" and media.photo_thumb(m, 100) == "s"
    doc = NS(photo=None, sticker=None, voice=None, video_note=None, gif=None, video=None, audio=None, document=NS(),
             file=NS(name="Скрин.png", mime_type="image/png", size=1000, width=10, height=10, duration=None))
    assert media.tg_info(doc)["kind"] == "image"
    doc.file.mime_type, doc.file.name = "text/plain", "app.log"
    assert media.tg_info(doc)["kind"] == "document"
    vid = NS(photo=None, sticker=None, voice=None, video_note=None, gif=None, video=NS(), document=NS(),
             file=NS(name="x.mp4", mime_type="video/mp4", size=10**8, width=1, height=1, duration=61))
    assert media.tg_info(vid)["kind"] == "video" and media.tg_info(NS(file=None)) is None


def test_names_and_text(tmp_path):
    assert media.safe_name('a<b>:c"/d|e?*') == "a_b__c__d_e__" and media.safe_name("CON") == "_CON"
    cfg = cfg_for(tmp_path)
    store = Store(":memory:")
    rel = media.rel_path(cfg, store, {"chat_id": -1001, "msg_id": 7, "date": "2026-11-01T10:00:00+00:00",
                                      "kind": "document", "name": "отчёт: итог.xlsx"}, "Дежурка / L2")
    assert rel == "media/Дежурка _ L2 [-1001]/2026-11/2026-11-01_10-00-00_m7_отчёт_ итог.xlsx"
    # название чата поменялось — папка та же
    assert media.chat_folder(cfg, store, -1001, "Новое имя") == "Дежурка _ L2 [-1001]"
    # текст лёгких документов
    (tmp_path / "a.log").write_text("ERROR INC0012345 timeout\n\n\nok", encoding="utf-8")
    assert media.extract_text(tmp_path / "a.log") == "ERROR INC0012345 timeout\nok"
    with zipfile.ZipFile(tmp_path / "d.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>Регламент &amp; CHG0001234</w:t></w:p></w:document>")
    assert "Регламент & CHG0001234" in media.extract_text(tmp_path / "d.docx")
    assert media.extract_text(tmp_path / "a.bin") is None
    # проверочная картинка — настоящий PNG
    png = media.digit_png("7")
    w, h = struct.unpack(">II", png[16:24])
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) == h * (1 + 3 * w) and media.sniff_mime(png) == "image/png"


def test_annotate():
    base = {"id": 12, "kind": "photo", "status": "ok", "described": True, "summary": "Ошибка 502 на lk-web",
            "details": "Подробно", "text": "HTTP 502\nINC0011111", "size": 1000}
    assert media.annotate(base) == "[фото: Ошибка 502 на lk-web; текст: HTTP 502 ⏎ INC0011111]"
    assert "Подробно" in media.annotate(base, full=True)
    assert media.annotate({**base, "described": False}) == "[фото, не описано, файл m12]"
    assert media.annotate({"id": 3, "kind": "video", "status": "ref", "name": "запись.mp4", "size": 120 * 2**20,
                           "duration": 95, "described": False}) == "[видео «запись.mp4», 120 МБ, 95 с — не скачан, файл m3]"
    doc = {"id": 4, "kind": "document", "status": "ok", "name": "app.log", "size": 2048, "described": True,
           "text": "line1\nline2", "summary": "app.log"}
    assert media.annotate(doc) == "[файл «app.log», 2 КБ; содержимое: line1 ⏎ line2]"


# ---------------------------------------------------------------- база и поиск
def test_store_media_and_search(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    store.upsert_chat(Chat(-1001, "Авария", "group"))
    store.upsert_chat(Chat(-1002, "Флуд", "group", priority="ignore"))
    store.add_messages([Message(-1001, 1, "2026-11-01T10:00:00+00:00", "Иван", "[фото] смотри"),
                        Message(-1001, 2, "2026-11-01T10:05:00+00:00", "Пётр", "[видео] запись"),
                        Message(-1002, 3, "2026-11-01T10:06:00+00:00", "Вася", "[фото] мем")])
    a = put_image(cfg, store, -1001, 1, jpeg("a"))
    put_image(cfg, store, -1002, 3, jpeg("a"))  # та же картинка в исключённом чате
    store.add_media([{"chat_id": -1001, "msg_id": 2, "kind": "video", "status": "ref", "name": "запись.mp4",
                      "size": 10**8, "date": "2026-11-01T10:05:00+00:00"}])
    assert [r["msg_id"] for r in store.media_list(undescribed=True)] == [1]  # исключённый чат не в работе
    assert len(store.media_list(undescribed=True, include_excluded=True)) == 2
    store.save_description(a["sha256"], {"kind": "скриншот", "summary": "Ошибка оплаты", "details": "Окно с ошибкой",
                                         "text": "PAYMENT_DECLINED INC0099999", "importance": 4, "model": "m"})
    assert store.media_list(undescribed=True, include_excluded=True) == []  # одна картинка — одно описание
    hits = store.search('"payment_declined"')
    assert [(m.chat_id, m.msg_id) for m, _ in hits] == [(-1001, 1)]  # нашлось по тексту на картинке
    assert [m.msg_id for m, _ in store.search('"запись"')] == [2]  # по имени файла (и тексту)
    ov = {c["id"]: c for c in store.chat_overview()}
    assert ov[-1001]["images"] == 1 and ov[-1001]["images_described"] == 1 and ov[-1001]["refs"] == 1
    st = store.media_stats()
    assert st["images"] == 2 and st["described"] == 2 and st["refs"] == 1
    # повторная регистрация не сбрасывает скачанное
    store.add_media([{"chat_id": -1001, "msg_id": 1, "kind": "photo", "status": "pending"}])
    assert store.get_media(a["id"])["status"] == "ok"
    # старая база без новых столбцов — открывается
    store.close()
    Store(tmp_path / "db.sqlite").close()


# ---------------------------------------------------------------- описание моделью
@pytest.fixture()
def vision_env(tmp_path):
    srv, url = start_server()
    cfg = cfg_for(tmp_path, describe_workers=3)
    cfg.data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "model": "fake-vl"}}
    store = Store(tmp_path / "db.sqlite")
    store.upsert_chat(Chat(-1001, "Авария ЛК", "group", note="Я — L2, слежу за ошибками оплаты"))
    yield srv, cfg, store
    fake_llm.IMAGES.clear()


def test_describer(vision_env):
    srv, cfg, store = vision_env
    imgs = {}
    for i in range(1, 7):
        store.add_messages([Message(-1001, i, f"2026-11-01T10:0{i}:00+00:00", "Иван", f"[фото] скрин {i}")])
        data = jpeg("same" if i in (5, 6) else f"img{i}")
        imgs[i] = put_image(cfg, store, -1001, i, data, date=f"2026-11-01T10:0{i}:00+00:00", title="Авария ЛК")
        fake_llm.IMAGES[hashlib.sha256(data).hexdigest()] = {
            "kind": "скриншот ошибки", "summary": f"Ошибка №{i}", "details": "Окно оплаты с ошибкой.",
            "text": f"ERR-{i} INC00{77700 + i}", "importance": 4}
    store.add_media([{"chat_id": -1001, "msg_id": 9, "kind": "photo", "status": "pending"}])  # не скачано
    rows = store.media_list(chat_ids=[-1001], kinds=media.IMAGE_KINDS)
    d = Describer(cfg, store, progress=lambda s: None)
    st = d.describe(rows)
    assert st["described"] == 5 and st["cached"] == 1 and st["missing"] == 1 and not st["failed"]
    vision_calls = [c for c in srv.calls if isinstance(c["messages"][-1]["content"], list)]
    assert len(vision_calls) == 5  # одинаковые картинки (5 и 6) — один запрос
    prompt = vision_calls[0]["messages"][-1]["content"][0]["text"]
    assert "Авария ЛК" in prompt and "слежу за ошибками оплаты" in prompt and "скрин 1" in prompt
    five, six = store.get_media(imgs[5]["id"]), store.get_media(imgs[6]["id"])
    assert six["described"] and six["summary"] == five["summary"] and five["sha256"] == six["sha256"]
    side = media.sidecar_path(cfg.path(imgs[1]["path"]))
    assert side.exists() and "ERR-1 INC0077701" in side.read_text(encoding="utf-8")
    assert store.entity_history("INC0077701")  # номер со скриншота — в сквозных упоминаниях
    # повторный запуск — ничего не описывает; новая база с теми же файлами — берёт описания из .md
    n = len(srv.calls)
    assert Describer(cfg, store, progress=lambda s: None).describe(rows)["described"] == 0 and len(srv.calls) == n
    store2 = Store(cfg.path("db2.sqlite"))
    store2.upsert_chat(Chat(-1001, "Авария ЛК", "group"))
    store2.add_messages([Message(-1001, 1, "2026-11-01T10:01:00+00:00", "Иван", "[фото] скрин 1")])
    store2.add_media([{"chat_id": -1001, "msg_id": 1, "kind": "photo", "status": "ok", "path": imgs[1]["path"],
                       "sha256": imgs[1]["sha256"], "date": imgs[1]["date"]}])
    st2 = Describer(cfg, store2, progress=lambda s: None).describe(store2.media_list(kinds=media.IMAGE_KINDS))
    assert st2["cached"] == 1 and len(srv.calls) == n and store2.media_list()[0]["text"].startswith("ERR-1")


def test_vision_unavailable_and_unsupported(vision_env, tmp_path):
    srv, cfg, store = vision_env
    store.add_messages([Message(-1001, 1, "2026-11-01T10:00:00+00:00", "Иван", "[фото]"),
                        Message(-1001, 2, "2026-11-01T10:01:00+00:00", "Иван", "[файл: a.bmp]")])
    put_image(cfg, store, -1001, 1, jpeg("x"))
    put_image(cfg, store, -1001, 2, b"BM" + b"0" * 100)  # BMP — модели его не читают
    srv.blind = True  # модель без поддержки картинок
    with pytest.raises(VisionUnavailable) as e:
        Describer(cfg, store, progress=lambda s: None).describe(store.media_list())
    assert "не принимает изображения" in str(e.value)
    left = store.media_list(undescribed=True)
    assert [r["msg_id"] for r in left] == [1] and left[0]["desc_state"] == "failed"
    # тип подключения без картинок вообще
    cmd = make_client("cc", {"type": "command", "command": "echo"})
    assert not cmd.supports_images()
    with pytest.raises(LLMError):
        cmd.complete("s", "u", images=[(b"x", "image/png")])
    assert parse_description("просто текст. И ещё")["summary"] == "просто текст."


def test_vision_check_openai(vision_env):
    srv, cfg, store = vision_env
    llm = make_client(*cfg.llm_profile())
    r = check(llm)
    assert r["ok"] and r["answer"] == r["expected"]
    srv.blind = True
    with pytest.raises(LLMError):
        check(make_client(*cfg.llm_profile()))


def test_maverick_image_discovery(tmp_path):
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images = "files_obj"  # этот «Maverick» понимает только files: [{content, filename, content_type}]
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test", "body_field": "message"})
    r = check(llm)
    assert r["ok"] and llm.profile["image_mode"]["field"] == "files"
    assert llm.profile["image_mode"]["item"]["fields"]["content"] == "b64"
    # найденный формат используется сразу, без перебора
    n = len(srv.calls)
    data = jpeg("mv")
    fake_llm.IMAGES[hashlib.sha256(data).hexdigest()] = {"kind": "график", "summary": "Рост ошибок",
                                                       "details": "", "text": "", "importance": 3}
    out = llm.complete("s", "Опиши изображение.", images=[(data, "image/jpeg")])
    assert json.loads(out)["summary"] == "Рост ошибок" and len(srv.calls) == n + 1
    # сервер, который картинки не видит ни в каком виде, — понятная ошибка
    srv.mav_images = None
    blind = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test", "body_field": "message",
                                     "image_probe_limit": 4})
    blind.retries = 0
    with pytest.raises(LLMError) as e:
        check(blind)
    assert "не принял изображение" in str(e.value) and getattr(e.value, "no_images", False)
    fake_llm.IMAGES.clear()


def test_maverick_learns_format_from_422_and_words(tmp_path):
    """Описания API нет; сервер подсказывает 422 (не хватает полей, лишние поля), модель отвечает словом."""
    from opsdigest.llm.attach import adapt, digits_in, short_detail
    assert digits_in("На картинке цифра семь.") == {"7"} and digits_in("Это семёрка") == {"7"}
    assert digits_in("7") == {"7"} and digits_in("не вижу изображения") == set()
    body = {"detail": [{"type": "missing", "loc": ["body", "files", 0, "file_data"], "msg": "Field required"},
                       {"type": "extra_forbidden", "loc": ["body", "files", 0, "content"], "input": "QUFB" * 100}]}
    assert "QUFB" not in short_detail(body) and "files.0.file_data" in short_detail(body)
    new = adapt({"field": "files", "list": True, "item": {"fields": {"content": "b64", "filename": "name"}}}, body, [])
    assert new == [{"field": "files", "list": True, "item": {"fields": {"filename": "name", "file_data": "b64"}}}]
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images = "strict"
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test", "body_field": "message"})
    r = check(llm)
    assert r["ok"], llm.image_report
    assert llm.profile["image_mode"]["item"]["fields"] == {"file_name": "name", "file_data": "b64", "mime": "mime"}
    rep = "\n".join(llm.image_report)
    assert "описание API не получено" in rep and "HTTP 422" in rep and "по подсказке сервера" in rep and "✓" in rep
    assert len([x for x in llm.image_report if x[:1].isdigit()]) <= 6  # нашлось за несколько попыток


def test_maverick_upload_then_id(tmp_path):
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images = "upload"
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test", "body_field": "message"})
    r = check(llm)
    assert r["ok"], llm.image_report
    assert llm.profile["image_mode"]["upload"]["path"] == "/api/v1/files"


def test_maverick_template_and_report(tmp_path):
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images = "files_obj"
    tmpl = {"message": "{{prompt}}", "files": [{"content": "{{image_base64}}", "filename": "{{image_name}}"}]}
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test", "body_field": "message",
                                   "image_body": tmpl})
    assert check(llm)["ok"] and llm.profile["image_mode"] == {"template": True}
    assert srv.calls[-1]["files"][0]["filename"] == "image1.png"
    # отчёт о подборе пишется в рабочую папку
    from opsdigest.vision import save_report
    cfg = cfg_for(tmp_path)
    p = save_report(cfg, llm)
    assert p and p.name == "maverick-images.txt" and "шаблон из настроек" in p.read_text(encoding="utf-8")


def test_maverick_spec_from_docs_page(tmp_path):
    """openapi.json по нестандартному адресу — находится по ссылке со страницы /docs; формат — с первой попытки."""
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images, srv.mav_docs = "strict", True
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test", "body_field": "message"})
    assert check(llm)["ok"], llm.image_report
    rep = "\n".join(llm.image_report)
    assert "/internal/spec.json (со страницы /docs)" in rep and "POST /api/v1/files [multipart/form-data]" in rep
    assert "1. «files»: [{file_name: name, file_data: b64, mime: mime}] → ✓" in rep


def test_maverick_attachments_like_web(tmp_path):
    """Как веб-версия Maverick: /api/v1/attachments/upload → art_…, в запросе «attachments»: ["art_…"].

    Поле подбирается запросом без текста: незнакомое поле → 422 «Нужно ввести текст или прикрепить хотя бы один файл»."""
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images, srv.mav_field = "attach", "query"
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test"})
    r = check(llm)
    assert r["ok"], llm.image_report
    mode = llm.profile["image_mode"]
    assert mode["field"] == "attachments" and mode["upload"]["path"] == "/api/v1/attachments/upload"
    assert mode["upload"]["as"] == "key:artifact_id" and llm.profile["body_field"] == "query"
    rep = "\n".join(llm.image_report)
    assert "номер файла: art_" in rep and "поле «attachment_ids»" not in rep  # «attachments» — первое и верное
    # рабочий запрос: загрузка + ссылка, текст в поле query
    last = srv.calls[-1]
    assert last["attachments"][0].startswith("art_") and "query" in last
    # ответ без текста теперь виден целиком: статус и ошибка
    from opsdigest.llm.providers import response_info
    assert response_info({"status": "failed", "response_text": "", "error": "Attachment not found"}) == \
        "(status=failed; error=«Attachment not found»)"


def test_maverick_attachment_busy_retry(tmp_path, monkeypatch):
    """Ответ без текста «лимит запросов» — пауза и повтор, а не «формат не подошёл»."""
    srv, url = start_server()
    host = url.rsplit("/v1", 1)[0]
    srv.mav_images, srv.mav_field = "attach", "query"
    import fake_llm as fl
    orig = fl.respond_image
    state = {"n": 0}

    def flaky(text, images):
        state["n"] += 1
        return "" if state["n"] == 1 else orig(text, images)
    monkeypatch.setattr(fl, "respond_image", flaky)
    real = fl._H._send

    def send(self, obj):  # пустой ответ — как у Maverick при лимите
        if isinstance(obj, dict) and obj.get("response_text") == "":
            obj = {**obj, "status": "failed", "error": "Rate limit exceeded, try again later"}
        return real(self, obj)
    monkeypatch.setattr(fl._H, "_send", send)
    import opsdigest.llm.providers as pr
    monkeypatch.setattr(pr.time, "sleep", lambda s: None)
    llm = make_client("maverick", {"type": "maverick", "url": host, "api_key": "sk_test"})
    assert check(llm)["ok"], llm.image_report
    assert any("лимит запросов" in x and "Rate limit" in x for x in llm.image_report)
