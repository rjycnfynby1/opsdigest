"""Загрузка экспорта Telegram Desktop (формат «Машиночитаемый JSON», файл result.json) в базу OpsDigest.

• Полный экспорт (Настройки → Продвинутые настройки → Экспорт данных из Telegram) и экспорт одного чата
  (⋮ в чате → Экспорт истории чата) — оба понимаются.
• Файл читается по частям: экспорт за годы весит сотни мегабайт, целиком в память он не грузится.
• Номера чатов и сообщений — те же, что у Telegram API, поэтому повторная загрузка не создаёт дублей,
  а синхронизация через API потом догружает только то, что новее загруженного.
• Служебные сообщения (вход в чат, закрепление…), «Избранное» и чат с кодами входа не загружаются.
• Вложения регистрируются в базе. Если экспорт сделан с фото и файлами, они копируются в папку media/ по тому же
  правилу, что и при выгрузке через API (изображения и лёгкие файлы — да, тяжёлые — только ссылка). Не вошедшие
  в экспорт изображения скачаются через Telegram при следующей синхронизации или «Прочитать изображения».
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import logs
from . import media as mediamod
from .entities import EntityExtractor
from .storage import Chat, Message, Store, iso, parse_iso

log = logs.get("import")
Progress = Callable[[str], None]
MAX_STORED_CHARS = 8000
SKIP_TYPES = {"saved_messages", "replies", "verification_codes"}
KINDS = {"personal_chat": "private", "bot_chat": "bot", "private_group": "group", "private_supergroup": "group",
         "public_supergroup": "group", "private_channel": "channel", "public_channel": "channel"}
MEDIA = {"sticker": "стикер", "voice_message": "голосовое", "video_message": "видеосообщение", "video_file": "видео",
         "animation": "GIF", "audio_file": "аудио"}


class ImportError_(ValueError):
    pass


# ============================================================== поиск файлов экспорта
def find_exports(path: str | Path) -> list[Path]:
    """Файл result.json или папка (ищется в ней и во вложенных: ChatExport_…, DataExport_…)."""
    p = Path(str(path).strip().strip('"'))
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise ImportError_("папка или файл не найдены")
    found = sorted({f for f in p.rglob("result.json") if f.is_file()}, key=lambda f: f.stat().st_mtime)
    if not found:
        raise ImportError_("в папке нет result.json — в Telegram Desktop выберите формат «Машиночитаемый JSON»")
    return found


# ============================================================== потоковое чтение JSON
class _Stream:
    """Последовательное чтение большого JSON: ищем ключи и раскодируем объекты сообщений по одному."""
    CHUNK = 1 << 20

    def __init__(self, f):
        self.f, self.buf, self.pos, self.eof = f, "", 0, False
        self.dec = json.JSONDecoder()

    def _more(self) -> bool:
        if self.eof:
            return False
        data = self.f.read(self.CHUNK)
        if not data:
            self.eof = True
            return False
        if self.pos > self.CHUNK:
            self.buf, self.pos = self.buf[self.pos:], 0
        self.buf += data
        return True

    def find(self, pattern: re.Pattern) -> re.Match | None:
        while True:
            m = pattern.search(self.buf, self.pos)
            if m and m.end() < len(self.buf):
                self.pos = m.end()
                return m
            if not self._more():
                if m:
                    self.pos = m.end()
                return m

    def peek(self) -> str:
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in " \t\r\n,:":
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._more():
                return ""

    def value(self):
        while True:
            try:
                v, end = self.dec.raw_decode(self.buf, self.pos)
                self.pos = end
                return v
            except json.JSONDecodeError:
                if not self._more():
                    raise ImportError_("файл экспорта обрывается — экспорт не закончился или файл повреждён") from None


HEAD_RE = re.compile(r'"(name|type|id)"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+|null)')
MESSAGES_RE = re.compile(r'"messages"\s*:\s*\[')
LIST_RE = re.compile(r'"chats"\s*:\s*\{\s*"about"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"list"\s*:\s*\[|"chats"\s*:\s*\{\s*"list"\s*:\s*\[')
ME_RE = re.compile(r'"personal_information"\s*:\s*\{\s*"user_id"\s*:\s*(\d+)')


def iter_chats(path: Path) -> Iterator[tuple[dict, Iterator[dict]]]:
    """(шапка чата {name,type,id}, поток его сообщений) — для полного экспорта и для экспорта одного чата."""
    with open(path, encoding="utf-8-sig") as f:
        s = _Stream(f)
        while len(s.buf) < 65536 and s._more():
            pass
        head = s.buf[:65536]
        single = bool(MESSAGES_RE.search(head)) and not re.search(r'"(chats|personal_information)"\s*:', head)
        if single:
            yield from _chats_from(s, single=True)
            return
        if not s.find(LIST_RE):
            raise ImportError_("в файле нет списка чатов — это не экспорт Telegram Desktop?")
        yield from _chats_from(s, single=False)


def _chats_from(s: _Stream, single: bool) -> Iterator[tuple[dict, Iterator[dict]]]:
    while True:
        if not single:
            c = s.peek()
            if c != "{":
                return
            s.pos += 1
        start = s.pos
        if not s.find(MESSAGES_RE):
            return
        header = {k: json.loads(v) for k, v in HEAD_RE.findall(s.buf[start:s.pos])}

        def msgs() -> Iterator[dict]:
            while True:
                c = s.peek()
                if c == "]" or not c:
                    s.pos += 1
                    return
                v = s.value()
                if isinstance(v, dict):
                    yield v

        gen = msgs()
        yield header, gen
        for _ in gen:  # если вызывающий не дочитал — пропускаем до конца чата
            pass
        if single:
            return
        # конец объекта чата: пропустить оставшиеся поля (если есть) до «}»
        while True:
            c = s.peek()
            if c == "}":
                s.pos += 1
                break
            if not c:
                return
            s.value()


def read_me(path: Path) -> dict | None:
    """Владелец экспорта (personal_information): {"id", "name"} или None (экспорт одного чата)."""
    with open(path, encoding="utf-8-sig") as f:
        head = f.read(65536)
    m = re.search(r'"personal_information"\s*:\s*', head)
    if not m:
        return None
    try:
        info, _ = json.JSONDecoder().raw_decode(head, m.end())
        uid = int(info["user_id"])
    except (ValueError, KeyError, TypeError):
        return None
    name = " ".join(x for x in (info.get("first_name"), info.get("last_name")) if x)
    return {"id": uid, "name": name}


# ============================================================== преобразование
def marked_id(kind: str, bare: int) -> int:
    """Номер чата как у Telethon: пользователи — как есть, группы — −n, супергруппы и каналы — −100n."""
    if kind == "private_group":
        return -bare
    if kind in ("private_supergroup", "public_supergroup", "private_channel", "public_channel"):
        return -(10**12 + bare)
    return bare


def peer_id(s: str | None) -> int | None:
    m = re.fullmatch(r"(user|channel|chat)(\d+)", s or "")
    if not m:
        return None
    n = int(m.group(2))
    return n if m.group(1) == "user" else (-(10**12 + n) if m.group(1) == "channel" else -n)


def text_of(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(x if isinstance(x, str) else str((x or {}).get("text", "")) for x in v)
    return ""


def convert(chat_id: int, title: str, m: dict, me: int | None) -> Message | None:
    if m.get("type") != "message":
        return None
    text = text_of(m.get("text")).strip()
    tags = []
    if m.get("forwarded_from") is not None or m.get("forwarded_from_id"):
        tags.append("переслано")
    if m.get("photo"):
        tags.append("фото")
    elif m.get("media_type") in MEDIA:
        tags.append(MEDIA[m["media_type"]])
    elif m.get("poll"):
        tags.append("опрос")
    elif m.get("file") or m.get("file_name"):
        tags.append(f"файл: {m['file_name']}" if m.get("file_name") else "файл")
    elif m.get("location_information") or m.get("contact_information"):
        tags.append("вложение")
    if tags:
        text = f"[{', '.join(tags)}] {text}".strip()
    if not text:
        return None
    if len(text) > MAX_STORED_CHARS:
        text = text[:MAX_STORED_CHARS] + " …[обрезано]"
    try:
        date = datetime.fromtimestamp(int(m["date_unixtime"]), tz=timezone.utc)
    except (KeyError, ValueError, TypeError, OSError):
        try:
            date = datetime.fromisoformat(m["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError):
            return None
    sender_id = peer_id(m.get("from_id"))
    return Message(chat_id=chat_id, msg_id=int(m["id"]), date=iso(date), sender=str(m.get("from") or title),
                   text=text, sender_id=sender_id, is_out=bool(me and sender_id == me), mentioned=False,
                   reply_to=m.get("reply_to_message_id"))


MEDIA_KIND = {"sticker": "sticker", "voice_message": "voice", "video_message": "video_note", "video_file": "video",
              "animation": "gif", "audio_file": "audio"}


def media_info(m: dict) -> tuple[dict, str | None] | None:
    """Вложение сообщения экспорта: (метаданные, путь к файлу внутри экспорта или None, если файл не включён)."""
    def rel(v) -> str | None:
        return v if isinstance(v, str) and v and not v.startswith("(") else None
    if m.get("photo"):
        return ({"kind": "photo", "mime": "image/jpeg", "size": m.get("photo_file_size"), "width": m.get("width"),
                 "height": m.get("height"), "name": None}, rel(m.get("photo")))
    if not (m.get("file") or m.get("file_name") or m.get("media_type")):
        return None
    mime = m.get("mime_type")
    kind = MEDIA_KIND.get(m.get("media_type") or "") or ("image" if mime in mediamod.IMAGE_MIMES else "document")
    return ({"kind": kind, "mime": mime, "size": m.get("file_size"), "name": m.get("file_name"),
             "width": m.get("width"), "height": m.get("height"), "duration": m.get("duration_seconds")},
            rel(m.get("file")))


# ============================================================== загрузка
def import_file(cfg, store: Store, path: Path, progress: Progress = print) -> dict:
    owner = read_me(path)
    try:
        known = json.loads(store.kv_get("me") or "{}")
    except ValueError:
        known = {}
    if owner and not known.get("id"):
        store.kv_set("me", json.dumps(owner, ensure_ascii=False))
        logs.add_secrets([owner.get("name")])
    me = (owner or {}).get("id") or known.get("id") or None
    extractor = EntityExtractor(cfg.get("entities") or {})
    policy = mediamod.Policy(cfg)
    stats = {"chats": 0, "messages": 0, "new": 0, "skipped_chats": 0, "files": 0}
    for head, msgs in iter_chats(path):
        kind = head.get("type") or ""
        if kind in SKIP_TYPES or kind not in KINDS or head.get("id") is None:
            stats["skipped_chats"] += 1
            continue
        chat_id = marked_id(kind, int(head["id"]))
        title = str(head.get("name") or "")
        if chat_id == 777000:
            stats["skipped_chats"] += 1
            continue
        k = KINDS[kind]
        prev = store.get_chat(chat_id)
        rule = cfg.chat_rule(title, prev.username if prev else None, chat_id, k)
        store.upsert_chat(Chat(chat_id=chat_id, title=title or (prev.title if prev else ""), kind=k,
                               username=prev.username if prev else None, priority=rule["priority"], mode=rule["kind"],
                               note=rule["note"], unread=prev.unread if prev else 0, muted=prev.muted if prev else False))
        batch: list[Message] = []
        media_rows: list[dict] = []
        n = new = 0
        max_id, min_id, min_date = (prev.max_msg_id, prev.min_msg_id, prev.min_date) if prev else (0, 0, None)

        def save_media() -> None:
            if not media_rows:
                return
            store.add_media(media_rows)
            known = store.media_of(chat_id, [r["msg_id"] for r in media_rows])
            for r in media_rows:
                src, row = r.get("_src"), known.get(r["msg_id"])
                if not src or not row or row["status"] != "pending":
                    continue
                f = path.parent / src
                if not f.is_file():
                    continue
                relp = mediamod.rel_path(cfg, store, row, title)
                target = cfg.path(relp)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    from .secure import files, vault
                    if vault.protected(target):  # защищённая папка: копия файла — сразу зашифрованная
                        data = f.read_bytes()
                        files.write_bytes(target, data, encrypt=True)
                    else:
                        shutil.copyfile(f, target)
                except OSError:
                    continue
                store.set_media_file(row["id"], relp, mediamod.sha256_file(target), size=f.stat().st_size)
                stats["files"] += 1
            media_rows.clear()

        for raw in msgs:
            msg = convert(chat_id, title, raw, me)
            if msg is None:
                continue
            mi = media_info(raw)
            if mi:
                info, src = mi
                media_rows.append({"chat_id": chat_id, "msg_id": msg.msg_id, "date": msg.date, **info,
                                   "status": policy.status(info), "_src": src})
            batch.append(msg)
            max_id = max(max_id, msg.msg_id)
            min_id = msg.msg_id if not min_id else min(min_id, msg.msg_id)
            if not min_date or parse_iso(msg.date) < parse_iso(min_date):
                min_date = msg.date
            if len(batch) >= 2000:
                new += store.add_messages(batch)
                store.add_entities(extractor.rows(batch))
                save_media()
                n += len(batch)
                batch = []
        if batch:
            new += store.add_messages(batch)
            store.add_entities(extractor.rows(batch))
            n += len(batch)
        save_media()
        if n:
            store.set_chat_range(chat_id, max_id, min_id, min_date)
        stats["chats"] += 1
        stats["messages"] += n
        stats["new"] += new
        if n:
            progress(f"  {title or chat_id}: {n} сообщ. (новых {new})")
    return stats


def import_path(cfg, store: Store, path: str | Path, progress: Progress = print) -> dict:
    total = {"files": 0, "chats": 0, "messages": 0, "new": 0, "skipped_chats": 0, "media_files": 0}
    for f in find_exports(path):
        progress(f"Читаю {f.name} ({f.stat().st_size / 2**20:.1f} МБ)…")
        st = import_file(cfg, store, f, progress)
        total["files"] += 1
        total["media_files"] += st["files"]
        for k in ("chats", "messages", "new", "skipped_chats"):
            total[k] += st[k]
    store.kv_set("last_import", iso(datetime.now(timezone.utc)))
    log.info("импорт экспорта: файлов %d, чатов %d, сообщений %d (новых %d)", total["files"], total["chats"],
             total["messages"], total["new"])
    progress(f"Готово: чатов {total['chats']}, сообщений {total['messages']}, из них новых {total['new']}.")
    return total
