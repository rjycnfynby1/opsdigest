"""Мессенджер: постоянное подключение к Telegram для окна OpsDigest.

Одно соединение на программу. События Telegram (новые, изменённые, удалённые сообщения, прочтение, «печатает»)
сразу пишутся в локальную базу и передаются окну длинным опросом (/api/tg/events). Пока мессенджер на связи,
выгрузка истории, сводка и вопросы работают через это же подключение (sources.telegram.SHARED).

В Telegram программа пишет только по явному действию пользователя в окне: отправить, ответить, переслать,
изменить, удалить, реакция, «Прочитано». Открытие чата, выгрузка и разбор моделью ничего не отмечают
прочитанным; ИИ сам ничего не отправляет — черновик попадает в поле ввода.
"""
from __future__ import annotations

import asyncio
import collections
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import logs
from ..secure import vault
from ..storage import Chat, iso
from .views import media_view, msg_views, one_chat

log = logs.get("messenger")
SERVICE = {777000, 42777}


class LiveError(RuntimeError):
    """Понятная пользователю причина (нет связи, нельзя писать в чат…)."""


ERRORS = {
    "ChatWriteForbiddenError": "в этот чат нельзя писать (нет прав или вы не участник)",
    "ChatAdminRequiredError": "для этого нужны права администратора чата",
    "UserIsBlockedError": "собеседник вас заблокировал",
    "YouBlockedUserError": "вы заблокировали этого пользователя — разблокируйте в Telegram",
    "MessageNotModifiedError": "текст не изменился",
    "MessageIdInvalidError": "сообщение не найдено (удалено?)",
    "MessageEditTimeExpiredError": "сообщение уже нельзя изменить — прошло слишком много времени",
    "MessageAuthorRequiredError": "изменить можно только свои сообщения",
    "MessageDeleteForbiddenError": "это сообщение удалить нельзя",
    "MessageTooLongError": "слишком длинное сообщение (до 4096 символов)",
    "MediaCaptionTooLongError": "слишком длинная подпись к файлу",
    "MessageEmptyError": "пустое сообщение",
    "SlowModeWaitError": "в чате включён медленный режим — подождите",
    "FloodWaitError": "Telegram просит подождать перед повтором",
    "ReactionInvalidError": "такую реакцию в этом чате поставить нельзя",
    "ChannelPrivateError": "канал закрыт или вас из него удалили",
    "PeerIdInvalidError": "чат не найден",
    "FilePartsInvalidError": "файл не загрузился — попробуйте ещё раз",
}


def human(e: Exception) -> str:
    name = type(e).__name__
    base = ERRORS.get(name)
    secs = getattr(e, "seconds", None)
    if base and secs:
        base += f" ({secs} с)"
    return base or f"{name}: {e}"[:300]


def entity_kind(ent: Any) -> str:
    t = type(ent).__name__
    if t == "User":
        return "bot" if getattr(ent, "bot", False) else "private"
    if t in ("Chat", "ChatForbidden"):
        return "group"
    if t in ("Channel", "ChannelForbidden"):
        return "group" if getattr(ent, "megagroup", False) else "channel"
    return "group"


class Live:
    def __init__(self, app):
        self.app = app
        self.state = "off"        # off | connecting | online | reconnecting | error | auth
        self.error = ""
        self.via = ""
        self.src = None
        self.client = None
        self.store = None
        self._full: dict[int, Any] = {}   # полные сущности из списка чатов (для аватарок)
        self.me: dict = {}
        self.events: collections.deque = collections.deque(maxlen=3000)
        self.seq = 0
        self.cond = threading.Condition()
        self.fut = None
        self._stop = False
        self._wake: asyncio.Event | None = None
        self._dlq: asyncio.Queue | None = None
        self._workers: list = []
        self._dl_seen: set[int] = set()
        self.typing: dict[int, dict] = {}
        self.since = 0.0
        self.loaded = False        # список чатов получен из Telegram в этом подключении

    # ================================================================ из потоков окна
    def can_start(self) -> str | None:
        ws = self.app.ws
        if not ws:
            return "рабочая папка не выбрана"
        if vault.status() in ("full", "pin"):
            return "программа заблокирована"
        if not (ws / "data" / "telegram.session").exists():
            return "вход в Telegram не выполнен (шаг «Вход»)"
        raw = self.app.raw()
        tg = raw.get("telegram") or {}
        if not str(tg.get("api_id") or "").strip() or not str(tg.get("api_hash") or "").strip():
            return "не заданы ключи Telegram (шаг «Telegram»)"
        return None

    def running(self) -> bool:
        return self.fut is not None and not self.fut.done()

    def ensure(self) -> None:
        if self.running():
            return
        why = self.can_start()
        if why:
            if self.state not in ("auth",):
                self.state, self.error = "off", why
            return
        self._stop = False
        self.state, self.error = "connecting", ""
        self.fut = asyncio.run_coroutine_threadsafe(self._run(), self.app.loop)

    def reconnect(self) -> None:
        if self.running() and self._wake is not None:
            self.app.loop.call_soon_threadsafe(self._wake.set)
            if self.client is not None:
                asyncio.run_coroutine_threadsafe(self._drop(), self.app.loop)
        else:
            if self.state == "auth":
                self.state = "off"
            self.ensure()

    def stop(self, timeout: float = 15) -> None:
        from ..sources import telegram as tgmod
        self._stop = True
        tgmod.SHARED = None
        if self.running():
            if self._wake is not None:
                self.app.loop.call_soon_threadsafe(self._wake.set)
            try:
                asyncio.run_coroutine_threadsafe(self._drop(), self.app.loop).result(timeout)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.fut.result(timeout)
            except Exception:  # noqa: BLE001
                pass
        self.fut = None
        self.store = None
        self._set("off")

    def status(self) -> dict:
        now = time.time()
        typing = {cid: t for cid, t in self.typing.items() if t["until"] > now}
        return {"state": self.state, "error": self.error, "via": self.via, "me": self.me, "seq": self.seq,
                "online": self.state == "online", "loaded": self.loaded, "typing": typing}

    def push(self, ev: dict) -> None:
        with self.cond:
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
            self.cond.notify_all()

    def wait(self, since: int, timeout: float = 25) -> tuple[list[dict], int]:
        with self.cond:
            if since > self.seq:  # программа перезапущена — окно начинает заново
                since = 0
            self.cond.wait_for(lambda: self.seq > since or self._stop, timeout=timeout)
            evs = [e for e in self.events if e["seq"] > since]
            return evs, self.seq

    def call(self, coro, timeout: float = 120):
        """Выполнить действие в Telegram из потока окна (нужна связь)."""
        if self.state != "online" or self.client is None:
            coro.close()
            raise LiveError("нет связи с Telegram" + (f": {self.error}" if self.error else "") +
                            " — переписка доступна из базы, отправка — когда связь появится")
        try:
            return self.app.run_async(coro, timeout)
        except LiveError:
            raise
        except asyncio.TimeoutError:
            raise LiveError("Telegram не ответил вовремя — попробуйте ещё раз") from None
        except Exception as e:  # noqa: BLE001 — ошибки Telegram превращаем в понятные
            from ..sources.telegram import TelegramError
            if isinstance(e, TelegramError):
                raise LiveError(str(e)) from None
            if type(e).__module__.startswith("telethon"):
                log.info("действие в Telegram: %s", type(e).__name__)
                raise LiveError(human(e)) from None
            raise

    # ================================================================ подключение
    def _set(self, state: str, error: str = "") -> None:
        if state != self.state or error != self.error:
            self.state, self.error = state, error
            self.push({"type": "state", "state": state, "error": error, "via": self.via})

    def _shared(self):
        if self.state == "online" and self.client is not None and self.client.is_connected():
            return self.client, self.src.proxy
        return None

    async def _drop(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        from ..sources import telegram as tgmod
        self._wake = asyncio.Event()
        delay = 3
        while not self._stop:
            self._set("connecting" if self.state != "reconnecting" else "reconnecting", self.error)
            try:
                await self._connect()
            except tgmod.TelegramError as e:
                msg = str(e)
                if "вход в Telegram не выполнен" in msg or "отозвал сессию" in msg:
                    self._set("auth", msg)
                    return
                self._set("error", msg.split("\n")[0][:300])
                log.info("мессенджер: нет связи, повтор через %d с", delay)
                await self._sleep(delay)
                delay = min(delay * 2, 120)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("мессенджер: ошибка подключения %s\n%s", logs.err_info(e), logs.frames(e))
                self._set("error", human(e))
                await self._sleep(delay)
                delay = min(delay * 2, 120)
                continue
            delay = 3
            self.since = time.time()
            self._set("online")
            log.info("мессенджер: на связи (%s)", self.src.proxy.mode if self.src.proxy else "?")
            tgmod.SHARED = self._shared
            loader = asyncio.ensure_future(self._after_connect())
            try:
                await self.client.disconnected
            except Exception:  # noqa: BLE001
                pass
            tgmod.SHARED = None
            loader.cancel()
            for w in self._workers:
                w.cancel()
            self._workers = []
            if self._stop:
                break
            log.info("мессенджер: связь оборвалась — переподключаюсь")
            self._set("reconnecting", "связь оборвалась")
            await self._sleep(2)
        self.client = None
        self._set("off")

    async def _sleep(self, sec: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), sec)
        except asyncio.TimeoutError:
            pass

    async def _connect(self) -> None:
        from ..sources import telegram as tgmod
        tgmod.SHARED = None
        if self.store is None:
            self.store = self.app.store()
        src = tgmod.TelegramSource(self.app.cfg(), self.store, progress=lambda s: None)
        src.is_live = True  # своё подключение мессенджера — не ждать самого себя (LIVE_BUSY)
        tgmod.LIVE_BUSY = lambda: self.running() and not self._stop and self.state in ("connecting", "reconnecting")
        await src.connect()
        self.src, self.client = src, src.client
        self.me = json.loads(self.store.kv_get("me") or "{}")
        self.via = src.proxy.describe() if src.proxy else "напрямую"
        self._register(src.client)
        self._dlq = asyncio.Queue()
        self._workers = [asyncio.ensure_future(self._dl_worker()) for _ in range(2)]

    def _register(self, client) -> None:
        from telethon import events
        for fn, ev in ((self._on_new, events.NewMessage()), (self._on_edit, events.MessageEdited()),
                       (self._on_delete, events.MessageDeleted()), (self._on_read, events.MessageRead(inbox=True)),
                       (self._on_read, events.MessageRead(inbox=False)), (self._on_user, events.UserUpdate())):
            client.add_event_handler(self._safe(fn), ev)

    def _safe(self, fn):
        async def handler(ev):
            try:
                await fn(ev)
            except Exception as e:  # noqa: BLE001 — одно событие не должно ронять мессенджер
                log.warning("событие %s: %s\n%s", fn.__name__, logs.err_info(e), logs.frames(e))
        return handler

    async def _after_connect(self) -> None:
        try:
            await self.refresh_dialogs()
            await self.load_folders()
            await self._fill_gaps()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("мессенджер: список чатов не загружен: %s\n%s", logs.err_info(e), logs.frames(e))

    # ================================================================ чаты, папки
    def _rule_chat(self, chat_id: int, title: str, kind: str, uname: str | None, unread: int = 0,
                   muted: bool = False, is_self: bool = False) -> Chat:
        cfg = self.src.cfg
        rule = cfg.chat_rule(title, uname, chat_id, kind)
        prio = rule["priority"]
        if is_self and not rule["explicit"]:
            prio = "ignore"  # «Избранное»: сюда приходят сводки — в сводку и поиск его не берём
        return Chat(chat_id=chat_id, title=title, kind=kind, username=uname, priority=prio, mode=rule["kind"],
                    note=rule["note"], unread=unread, muted=muted)

    async def refresh_dialogs(self) -> int:
        from ..sources.telegram import classify, entity_username, is_muted
        n = 0
        self._tops: list[tuple[Any, int]] = []
        async for d in self.client.iter_dialogs(archived=None):
            if d.id in SERVICE:
                continue
            is_self = bool(getattr(d.entity, "is_self", False))
            title = "Избранное" if is_self else (d.name or "")
            kind = classify(d)
            chat = self._rule_chat(d.id, title, kind, entity_username(d.entity), d.unread_count or 0, is_muted(d),
                                   is_self)
            self.store.upsert_chat(chat)
            dd = getattr(d, "dialog", None)
            from ..avatars import photo_id
            self._full[d.id] = d.entity
            self.store.set_chat_state(
                d.id, forum=bool(getattr(d.entity, "forum", False)), photo=photo_id(d.entity),
                pinned=bool(getattr(d, "pinned", False)), archived=bool(getattr(d, "archived", False)),
                read_in=getattr(dd, "read_inbox_max_id", 0) or 0, read_out=getattr(dd, "read_outbox_max_id", 0) or 0,
                mentions=getattr(dd, "unread_mentions_count", 0) or 0,
                contact=bool(getattr(d.entity, "contact", False)), top_date=iso(d.date) if d.date else None)
            top = getattr(d, "message", None)
            if top is not None:
                self._tops.append((d, top.id))
                if not getattr(top, "action", None):
                    await self._store(d.id, [top], title)
            n += 1
        self.loaded = True
        self.push({"type": "dialogs"})
        log.info("мессенджер: чатов %d", n)
        return n

    async def load_folders(self) -> list[dict]:
        from telethon import utils
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        try:
            res = await self.client(GetDialogFiltersRequest())
        except Exception as e:  # noqa: BLE001
            log.info("папки Telegram не получены: %s", type(e).__name__)
            return []
        out = []
        for f in getattr(res, "filters", res) or []:
            if type(f).__name__ == "DialogFilterDefault":
                continue
            title = getattr(f, "title", "")
            title = title if isinstance(title, str) else getattr(title, "text", "") or ""

            def ids(peers):
                r = []
                for p in peers or []:
                    try:
                        r.append(utils.get_peer_id(p))
                    except Exception:  # noqa: BLE001
                        pass
                return r
            out.append({"id": f.id, "title": title, "emo": getattr(f, "emoticon", None) or "",
                        "inc": ids(getattr(f, "include_peers", None)), "pin": ids(getattr(f, "pinned_peers", None)),
                        "exc": ids(getattr(f, "exclude_peers", None)),
                        "flags": {k: bool(getattr(f, k, False)) for k in (
                            "contacts", "non_contacts", "groups", "broadcasts", "bots", "exclude_muted",
                            "exclude_read", "exclude_archived")}})
        self.store.kv_set("tg_folders", json.dumps(out, ensure_ascii=False))
        self.push({"type": "folders", "folders": out})
        return out

    async def _fill_gaps(self) -> None:
        """После (пере)подключения: в чатах, где в Telegram есть сообщения новее, чем в базе, — догрузить их."""
        have = self.store.max_ids()
        since = datetime.now(timezone.utc) - timedelta(days=7)
        todo = [(d, top) for i, (d, top) in enumerate(getattr(self, "_tops", []))
                if top > have.get(d.id, 0) and (i < 40 or (d.unread_count or 0) > 0)]
        for d, _ in todo[:80]:
            if self._stop:
                return
            low = have.get(d.id, 0)
            try:
                msgs, _ = await self.src._fetch(d.entity, since, 100 if low else 30, min_id=low)
            except (ConnectionError, OSError):
                return
            if msgs:
                ids = await self._store(d.id, msgs)
                self._push_msgs(d.id, ids, "msg", quiet=True)
        if todo:
            self.push({"type": "dialogs"})

    async def _ensure_chat(self, chat_id: int, m: Any) -> Chat:
        c = self.store.get_chat(chat_id)
        if c is not None:
            return c
        from telethon import utils

        from ..sources.telegram import entity_username
        ent = await m.get_chat()
        is_self = bool(getattr(ent, "is_self", False))
        title = "Избранное" if is_self else (utils.get_display_name(ent) or str(chat_id))
        c = self._rule_chat(chat_id, title, entity_kind(ent), entity_username(ent), is_self=is_self)
        self.store.upsert_chat(c)
        return c

    # ================================================================ запись в базу и окно
    async def _store(self, chat_id: int, msgs: list, title: str | None = None, download: bool = True) -> list[int]:
        msgs = [m for m in msgs if m is not None and not getattr(m, "action", None)]
        if not msgs:
            return []
        if title is None:
            c = self.store.get_chat(chat_id)
            title = c.title if c else ""
        for m in msgs:
            if getattr(m, "sender", None) is None and getattr(m, "sender_id", None):
                try:
                    await m.get_sender()
                except Exception:  # noqa: BLE001
                    pass
        conv = self.src._convert(chat_id, title, msgs)
        self.store.add_messages(conv)
        self.store.add_entities(self.src.extractor.rows(conv))
        pairs = self.src._media_rows(chat_id, msgs)
        if pairs:
            self.store.add_media([r for r, _ in pairs])
            known = self.store.media_of(chat_id, [r["msg_id"] for r, _ in pairs])
            for r, m in pairs:
                k = known.get(r["msg_id"])
                if download and k and k["status"] == "pending" and r["status"] == "pending" and self.src.policy.download \
                        and k["id"] not in self._dl_seen and self._dlq is not None:
                    self._dl_seen.add(k["id"])
                    self._dlq.put_nowait((k, m, title))
        return [c.msg_id for c in conv]

    def _push_msgs(self, chat_id: int, ids: list[int], typ: str = "msg", quiet: bool = False) -> None:
        if not ids:
            return
        views = msg_views(self.store, chat_id, self.store.rows_by_ids(chat_id, ids))
        self.push({"type": typ, "chat": chat_id, "msgs": views, "quiet": quiet})

    def _push_chat(self, chat_id: int) -> None:
        v = one_chat(self.store, chat_id)
        if v:
            self.push({"type": "chat", "chat": v})

    async def _dl_worker(self) -> None:
        while True:
            row, m, title = await self._dlq.get()
            try:
                await self.src._download(row, m, title)
                x = self.store.get_media(row["id"])
                self.push({"type": "media", "chat": row["chat_id"], "msg": row["msg_id"], "media": media_view(x)})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("вложение не скачано: %s", type(e).__name__)

    # ================================================================ события Telegram
    async def _on_new(self, ev) -> None:
        cid = ev.chat_id
        if cid in SERVICE:
            return
        m = ev.message
        c = await self._ensure_chat(cid, m)
        ids = await self._store(cid, [m], c.title)
        if not getattr(m, "out", False):
            self.store.bump_unread(cid, bool(getattr(m, "mentioned", False)), iso(m.date))
        else:
            self.store.set_chat_state(cid, top_date=iso(m.date))
        try:
            from ..sources.telegram import reply_info
            _, topic, _ = reply_info(m, cid)
            c2 = self.store.get_chat(cid)
            if topic or (c2 and self.store.db.execute("SELECT forum FROM chats WHERE chat_id=?", (cid,)).fetchone()[0]):
                t = topic or 1
                self.store.db.execute(
                    """UPDATE topics SET top_msg=?, top_date=?, unread=unread+? WHERE chat_id=? AND topic_id=?""",
                    (m.id, iso(m.date), 0 if getattr(m, "out", False) else 1, cid, t))
                self.store.db.commit()
                self.push({"type": "topic", "chat": cid, "topic": t})
        except Exception:  # noqa: BLE001
            pass
        self._push_msgs(cid, ids, "msg")
        self._push_chat(cid)

    async def _on_edit(self, ev) -> None:
        cid = ev.chat_id
        if cid in SERVICE:
            return
        c = await self._ensure_chat(cid, ev.message)
        ids = await self._store(cid, [ev.message], c.title)
        self._push_msgs(cid, ids, "edit")

    async def _on_delete(self, ev) -> None:
        cid = getattr(ev, "chat_id", None)
        ids = list(getattr(ev, "deleted_ids", None) or [])
        gone = self.store.delete_messages(cid, ids)
        self.push({"type": "del", "chat": cid, "ids": ids, "items": [list(x) for x in gone]})
        for c in {c for c, _ in gone}:
            self._push_chat(c)

    async def _on_read(self, ev) -> None:
        cid = ev.chat_id
        if cid in SERVICE or cid is None:
            return
        max_id = int(getattr(ev, "max_id", 0) or 0)
        if getattr(ev, "inbox", False):  # входящие прочитаны на другом устройстве
            unread = await self._server_unread(cid)
            if unread is None:
                unread = self.store.unread_after(cid, max_id)
            self.store.set_chat_state(cid, read_in=max_id, unread=unread, **({"mentions": 0} if not unread else {}))
            self._push_chat(cid)
        else:                            # собеседник прочитал ваши сообщения
            self.store.set_chat_state(cid, read_out=max_id)
            self.push({"type": "read_out", "chat": cid, "max": max_id})

    async def _server_unread(self, chat_id: int) -> int | None:
        try:
            from telethon.tl.functions.messages import GetPeerDialogsRequest
            from telethon.tl.types import InputDialogPeer
            ent = await self.src._entity(chat_id)
            res = await self.client(GetPeerDialogsRequest(peers=[InputDialogPeer(peer=ent)]))
            return int(res.dialogs[0].unread_count)
        except Exception:  # noqa: BLE001
            return None

    async def _on_user(self, ev) -> None:
        if not getattr(ev, "typing", False):
            return
        cid = getattr(ev, "chat_id", None)
        if cid is None or cid in SERVICE:
            return
        who = ""
        try:
            from telethon import utils
            u = await ev.get_user()
            who = utils.get_display_name(u) if u is not None else ""
        except Exception:  # noqa: BLE001
            pass
        self.typing[cid] = {"who": who, "until": time.time() + 6}
        self.push({"type": "typing", "chat": cid, "who": who})

    # ================================================================ действия окна (корутины для call)
    async def history(self, chat_id: int, before: int | None = None, around: int | None = None,
                      limit: int = 40, topic: int | None = None) -> tuple[list[int], int]:
        ent = await self.src._entity(chat_id)
        kw: dict = {}
        if topic and int(topic) != 1:
            kw["reply_to"] = int(topic)  # сообщения темы форума
        if around:
            msgs = await self.client.get_messages(ent, limit=limit, offset_id=around + 1, add_offset=-(limit // 2), **kw)
        else:
            msgs = await self.client.get_messages(ent, limit=limit if not topic or int(topic) != 1 else limit * 3,
                                                  offset_id=before or 0, **kw)
        msgs = list(msgs or [])
        if msgs:
            await self._ensure_chat(chat_id, msgs[0])
        ids = await self._store(chat_id, msgs)
        await self._fill_replies(chat_id, ent, msgs)
        return ids, len(msgs)

    async def _fill_replies(self, chat_id: int, ent: Any, msgs: list) -> None:
        """Сообщения, на которые отвечают, но их нет в базе (старые, из другого чата), — догрузить, чтобы цитата
        показывалась, а не «не загружено»."""
        from ..sources.telegram import reply_info
        want, ext = set(), {}
        for m in msgs:
            try:
                rid, _, info = reply_info(m, chat_id)
            except Exception:  # noqa: BLE001
                continue
            if rid:
                want.add(rid)
            x = info.get("x") or {}
            if x.get("c") and x.get("id"):
                ext.setdefault(int(x["c"]), set()).add(int(x["id"]))
        have = {r["msg_id"] for r in self.store.rows_by_ids(chat_id, want)} if want else set()
        miss = sorted(want - have)[:50]
        if miss:
            try:
                got = [x for x in await self.client.get_messages(ent, ids=miss) or [] if x is not None]
                await self._store(chat_id, got, download=False)
            except Exception as e:  # noqa: BLE001
                log.info("ответы не догружены: %s", type(e).__name__)
        for cid, ids in list(ext.items())[:5]:
            if self.store.get_chat(cid) is None:
                continue  # чужой чат, которого нет в ваших диалогах, — показываем то, что пришло в заголовке
            have = {r["msg_id"] for r in self.store.rows_by_ids(cid, ids)}
            need = sorted(ids - have)[:20]
            if not need:
                continue
            try:
                e2 = await self.src._entity(cid)
                got = [x for x in await self.client.get_messages(e2, ids=need) or [] if x is not None]
                await self._store(cid, got, download=False)
            except Exception as e:  # noqa: BLE001
                log.info("сообщения другого чата не догружены: %s", type(e).__name__)

    async def fill_period(self, chat_id: int, since: datetime, cap: int = 2000) -> int:
        """Перед сводкой чата: догрузить из Telegram то, чего за период нет в базе (новее и старше уже загруженного)."""
        ent = await self.src._entity(chat_id)
        low, high, low_date = self.store.bounds(chat_id)
        got = 0
        new, _ = await self.src._fetch(ent, since, cap, min_id=high)
        got += len(await self._store(chat_id, new))
        if low and low_date and datetime.fromisoformat(low_date) > since and not self.store.history_done(chat_id):
            older, _ = await self.src._fetch(ent, since, cap, offset_id=low)
            got += len(await self._store(chat_id, older))
        return got

    async def search(self, q: str, chat_id: int | None = None, limit: int = 40) -> list[tuple[int, int]]:
        if chat_id is not None:
            ent = await self.src._entity(chat_id)
            msgs = list(await self.client.get_messages(ent, search=q, limit=limit) or [])
            await self._store(chat_id, msgs)
            return [(chat_id, m.id) for m in msgs if m is not None]
        msgs = list(await self.client.get_messages(None, search=q, limit=limit) or [])
        out = []
        by: dict[int, list] = {}
        for m in msgs:
            cid = getattr(m, "chat_id", None)
            if m is None or cid is None or cid in SERVICE:
                continue
            by.setdefault(cid, []).append(m)
        for cid, ms in by.items():
            c = await self._ensure_chat(cid, ms[0])
            await self._store(cid, ms, c.title)
            out += [(cid, m.id) for m in ms]
        return out

    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None, topic: int | None = None,
                        silent: bool = False, schedule: datetime | None = None) -> int:
        from ..sources.telegram import split_message
        ent = await self.src._entity(chat_id)
        last = None
        for i, part in enumerate(split_message(text, 4000)):
            rt = reply_to if i == 0 else None
            if topic and int(topic) != 1:
                last = await self._send_in_topic(ent, part, int(topic), rt, silent, schedule)
            else:
                kw = {k: v for k, v in (("silent", silent or None), ("schedule", schedule)) if v}
                last = await self.client.send_message(ent, part, reply_to=rt, parse_mode="md", link_preview=True, **kw)
            if schedule is None and last is not None:
                await self._store(chat_id, [last])
                self._push_msgs(chat_id, [last.id], "msg")
        if schedule is None:
            await self._read(chat_id, ent)  # ответили — значит, прочитали (как в Telegram)
        log.info("мессенджер: отправлено сообщение" + (" (отложенное)" if schedule else ""))
        return last.id if last is not None else 0

    async def _send_in_topic(self, ent: Any, text: str, topic: int, reply_to: int | None, silent: bool,
                             schedule: datetime | None):
        """Сообщение в тему форума: ответ внутри темы требует номер темы (top_msg_id), Telethon его не ставит."""
        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputReplyToMessage
        if not hasattr(self.client, "_parse_message_text"):  # другой клиент: в тему — без привязки к сообщению
            return await self.client.send_message(ent, text, reply_to=reply_to or topic, parse_mode="md")
        msg, ents = await self.client._parse_message_text(text, "md")
        rt = InputReplyToMessage(reply_to_msg_id=reply_to or topic,
                                 top_msg_id=topic if reply_to and reply_to != topic else None)
        req = SendMessageRequest(peer=ent, message=msg, entities=ents or None, reply_to=rt, silent=silent or None,
                                 schedule_date=schedule)
        res = await self.client(req)
        return self.client._get_response_message(req, res, ent)

    async def send_file(self, chat_id: int, data: bytes, name: str, caption: str = "", reply_to: int | None = None,
                        as_document: bool = False, topic: int | None = None, silent: bool = False) -> int:
        ent = await self.src._entity(chat_id)
        bio = io.BytesIO(data)
        bio.name = name or "file"
        if topic and int(topic) != 1 and not reply_to:
            reply_to = int(topic)  # файл — в тему (ответ на конкретное сообщение темы — без привязки к нему)
        m = await self.client.send_file(ent, bio, caption=caption or None, reply_to=reply_to,
                                        force_document=as_document, parse_mode="md",
                                        **({"silent": True} if silent else {}))
        msgs = m if isinstance(m, list) else [m]
        ids = await self._store(chat_id, msgs, download=False)
        await self._save_local(chat_id, msgs[0], data)
        self._push_msgs(chat_id, ids, "msg")
        await self._read(chat_id, ent)
        log.info("мессенджер: отправлен файл")
        return msgs[0].id

    async def _save_local(self, chat_id: int, m: Any, data: bytes) -> None:
        """Свой отправленный файл не скачиваем обратно — кладём то, что отправили."""
        import hashlib

        from .. import media as mediamod
        from ..secure import files
        row = self.store.media_of(chat_id, [m.id]).get(m.id)
        if not row or row["status"] == "ok":
            return
        c = self.store.get_chat(chat_id)
        rel = mediamod.rel_path(self.src.cfg, self.store, row, c.title if c else "")
        target = self.src.cfg.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        files.write_bytes(target, data, encrypt=vault.protected(target))
        mime = mediamod.sniff_mime(data[:16]) if row.get("kind") in ("photo", "image") else None
        self.store.set_media_file(row["id"], rel, hashlib.sha256(data).hexdigest(), size=len(data), mime=mime)

    async def edit(self, chat_id: int, msg_id: int, text: str) -> None:
        ent = await self.src._entity(chat_id)
        m = await self.client.edit_message(ent, msg_id, text, parse_mode="md")
        await self._store(chat_id, [m])
        self._push_msgs(chat_id, [msg_id], "edit")

    async def delete(self, chat_id: int, ids: list[int], revoke: bool = True) -> None:
        ent = await self.src._entity(chat_id)
        await self.client.delete_messages(ent, ids, revoke=revoke)
        gone = self.store.delete_messages(chat_id, ids)
        self.push({"type": "del", "chat": chat_id, "ids": ids, "items": [list(x) for x in gone]})
        self._push_chat(chat_id)

    async def forward(self, from_chat: int, ids: list[int], to_chat: int) -> list[int]:
        src_ent = await self.src._entity(from_chat)
        dst_ent = await self.src._entity(to_chat)
        res = await self.client.forward_messages(dst_ent, ids, from_peer=src_ent)
        msgs = res if isinstance(res, list) else [res]
        out = await self._store(to_chat, msgs)
        self._push_msgs(to_chat, out, "msg")
        self._push_chat(to_chat)
        return out

    async def react(self, chat_id: int, msg_id: int, emoji: str | None) -> None:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        ent = await self.src._entity(chat_id)
        await self.client(SendReactionRequest(peer=ent, msg_id=msg_id,
                                              reaction=[ReactionEmoji(emoticon=emoji)] if emoji else []))
        m = await self.client.get_messages(ent, ids=msg_id)
        if m is not None:
            await self._store(chat_id, [m])
            self._push_msgs(chat_id, [msg_id], "edit")

    async def read(self, chat_id: int, max_id: int | None = None) -> None:
        ent = await self.src._entity(chat_id)
        await self._read(chat_id, ent, max_id)

    async def _read(self, chat_id: int, ent: Any, max_id: int | None = None) -> None:
        await self.client.send_read_acknowledge(ent, max_id=max_id or 0, clear_mentions=True)
        top = max_id or (self.store.max_ids().get(chat_id) or 0)
        unread = self.store.unread_after(chat_id, max_id) if max_id else 0
        self.store.set_chat_state(chat_id, unread=unread, read_in=top, **({"mentions": 0} if not unread else {}))
        self._push_chat(chat_id)

    async def download(self, media_id: int) -> dict:
        row = self.store.get_media(media_id)
        if not row:
            raise LiveError("такого файла нет в базе")
        limit = float(self.src.cfg.media.get("fetch_max_mb") or 200) * 2**20
        if (row.get("size") or 0) > limit:
            raise LiveError(f"файл больше {limit / 2**20:.0f} МБ — откройте его в Telegram")
        if row["status"] != "ok" or not row.get("path") or not self.src.cfg.path(row["path"]).is_file():
            ent = await self.src._entity(row["chat_id"])
            m = await self.client.get_messages(ent, ids=row["msg_id"])
            if m is None or not getattr(m, "media", None):
                self.store.set_media_status(media_id, "gone", "сообщение удалено")
                raise LiveError("сообщение с файлом удалено")
            c = self.store.get_chat(row["chat_id"])
            protected = vault.protected(self.src.cfg.path("data/x"))
            ok = await self.src._download(row, m, c.title if c else "", to_file=not protected)
            if not ok:
                raise LiveError("файл не скачан: " + ((self.store.get_media(media_id) or {}).get("error") or "ошибка"))
        x = self.store.get_media(media_id)
        v = media_view(x)
        self.push({"type": "media", "chat": x["chat_id"], "msg": x["msg_id"], "media": v})
        return v

    # ================================================================ темы форумов, закреп, упоминания, опросы
    async def load_topics(self, chat_id: int) -> list[dict]:
        """Темы форума из Telegram (до 500) → база. Возвращает строки тем."""
        from telethon.tl.functions.messages import GetForumTopicsRequest
        ent = await self.src._entity(chat_id)
        rows, tops = [], []
        off_date, off_id, off_topic = None, 0, 0
        now = time.time()
        for _ in range(5):
            res = await self.client(GetForumTopicsRequest(peer=ent, offset_date=off_date, offset_id=off_id,
                                                          offset_topic=off_topic, limit=100))
            msgs = {m.id: m for m in getattr(res, "messages", None) or [] if m is not None}
            got = [t for t in getattr(res, "topics", None) or [] if type(t).__name__ == "ForumTopic"]
            for t in got:
                top = msgs.get(t.top_message)
                mu = getattr(getattr(t, "notify_settings", None), "mute_until", None)
                mts = mu.timestamp() if isinstance(mu, datetime) else float(mu or 0)
                rows.append({"id": t.id, "title": t.title, "color": t.icon_color, "closed": bool(t.closed),
                             "pinned": bool(t.pinned), "hidden": bool(getattr(t, "hidden", False)),
                             "top_msg": t.top_message, "top_date": iso(top.date) if top is not None else None,
                             "unread": t.unread_count, "mentions": t.unread_mentions_count,
                             "read_in": t.read_inbox_max_id, "muted": mts > now})
            tops += list(msgs.values())
            if len(got) < 100:
                break
            last = got[-1]
            lm = msgs.get(last.top_message)
            off_date, off_id, off_topic = (lm.date if lm is not None else None), last.top_message, last.id
        self.store.upsert_topics(chat_id, rows)
        self.store.set_chat_state(chat_id, forum=True)
        if tops:
            await self._store(chat_id, tops, download=False)
        self.push({"type": "topics", "chat": chat_id})
        return rows

    async def mute(self, chat_id: int, on: bool, topic: int | None = None) -> None:
        """Уведомления чата или темы: без звука навсегда / включить."""
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyForumTopic, InputNotifyPeer, InputPeerNotifySettings
        from telethon import utils
        ent = await self.src._entity(chat_id)
        try:
            ent = utils.get_input_peer(ent)  # вложенное поле Telethon сам не преобразует
        except TypeError:
            pass
        peer = InputNotifyForumTopic(peer=ent, top_msg_id=int(topic)) if topic else InputNotifyPeer(peer=ent)
        until = 2147483647 if on else 0
        await self.client(UpdateNotifySettingsRequest(peer=peer, settings=InputPeerNotifySettings(mute_until=until)))
        if topic:
            self.store.set_topic_state(chat_id, int(topic), muted=on)
            self.push({"type": "topics", "chat": chat_id})
        else:
            self.store.set_chat_state(chat_id, muted=on)
            self._push_chat(chat_id)

    async def pinned(self, chat_id: int, topic: int | None = None) -> list[int]:
        """Закреплённые сообщения (новые первыми) → база; номера."""
        from telethon.tl.types import InputMessagesFilterPinned
        ent = await self.src._entity(chat_id)
        kw = {"reply_to": int(topic)} if topic and int(topic) != 1 else {}
        msgs = [m for m in await self.client.get_messages(ent, limit=30, filter=InputMessagesFilterPinned, **kw) or []
                if m is not None]
        await self._store(chat_id, msgs, download=False)
        return [m.id for m in msgs]

    async def unread_marks(self, chat_id: int, topic: int | None = None) -> dict:
        """Непрочитанные упоминания и реакции на мои сообщения (номера, старые первыми) — для кнопок «@» и «❤»."""
        from telethon.tl.functions.messages import GetUnreadMentionsRequest, GetUnreadReactionsRequest
        ent = await self.src._entity(chat_id)
        tid = int(topic) if topic and int(topic) != 1 else None
        out = {}
        for key, req in (("mentions", GetUnreadMentionsRequest), ("reactions", GetUnreadReactionsRequest)):
            try:
                kw = {"top_msg_id": tid} if tid else {}
                res = await self.client(req(peer=ent, offset_id=0, add_offset=0, limit=50, max_id=0, min_id=0, **kw))
                msgs = [m for m in getattr(res, "messages", None) or [] if m is not None
                        and type(m).__name__ not in ("MessageEmpty", "MessageService")]
                if msgs:
                    await self._store(chat_id, msgs, download=False)
                out[key] = sorted(m.id for m in msgs)
            except Exception as e:  # noqa: BLE001
                log.info("%s не получены: %s", key, type(e).__name__)
                out[key] = []
        return out

    async def read_topic(self, chat_id: int, topic: int, max_id: int | None = None) -> None:
        """Тема форума прочитана (как «Прочитано» в Telegram) — только по нажатию пользователя."""
        from telethon.tl.functions.messages import ReadDiscussionRequest
        ent = await self.src._entity(chat_id)
        if not max_id:
            t, ta = self.store.topic_sql(topic)
            r = self.store.db.execute(f"SELECT MAX(msg_id) FROM messages WHERE chat_id=?{t}", (chat_id, *ta)).fetchone()
            max_id = int(r[0] or 0)
        if int(topic) == 1:
            await self.client.send_read_acknowledge(ent, max_id=max_id)
        else:
            await self.client(ReadDiscussionRequest(peer=ent, msg_id=int(topic), read_max_id=int(max_id)))
        self.store.set_topic_state(chat_id, int(topic), unread=0, mentions=0, read_in=int(max_id))
        self.push({"type": "topics", "chat": chat_id})

    async def vote(self, chat_id: int, msg_id: int, options: list[str]) -> None:
        from telethon.tl.functions.messages import SendVoteRequest
        ent = await self.src._entity(chat_id)
        await self.client(SendVoteRequest(peer=ent, msg_id=msg_id, options=[bytes.fromhex(o) for o in options]))
        m = await self.client.get_messages(ent, ids=msg_id)
        if m is not None:
            await self._store(chat_id, [m], download=False)
            self._push_msgs(chat_id, [msg_id], "edit")

    async def media_tab(self, chat_id: int, kind: str, limit: int = 60, before: int | None = None) -> list[int]:
        """Вкладки «Медиа / Файлы / Ссылки / Голосовые» — из Telegram (фильтры поиска) → база; номера."""
        from telethon.tl import types as T
        flt = {"media": T.InputMessagesFilterPhotoVideo, "files": T.InputMessagesFilterDocument,
               "links": T.InputMessagesFilterUrl, "voice": T.InputMessagesFilterRoundVoice,
               "music": T.InputMessagesFilterMusic, "gif": T.InputMessagesFilterGif}.get(kind)
        if flt is None:
            return []
        ent = await self.src._entity(chat_id)
        msgs = [m for m in await self.client.get_messages(ent, limit=limit, filter=flt, offset_id=before or 0) or []
                if m is not None]
        await self._store(chat_id, msgs, download=False)
        return [m.id for m in msgs]

    async def search_from(self, chat_id: int, q: str, from_id: int | None, limit: int = 60) -> list[int]:
        ent = await self.src._entity(chat_id)
        kw = {}
        if from_id:
            kw["from_user"] = await self.client.get_input_entity(int(from_id))
        msgs = [m for m in await self.client.get_messages(ent, search=q or None, limit=limit, **kw) or []
                if m is not None]
        await self._store(chat_id, msgs, download=False)
        return [m.id for m in msgs]

    async def members(self, chat_id: int, limit: int = 2000) -> list[dict]:
        """Участники группы из Telegram (в каналах без прав администратора Telegram их не отдаёт)."""
        from telethon import utils
        ent = await self.src._entity(chat_id)
        out = []
        async for u in self.client.iter_participants(ent, limit=limit):
            if getattr(u, "deleted", False):
                continue
            p = getattr(u, "participant", None)
            role = {"ChannelParticipantCreator": "creator", "ChatParticipantCreator": "creator",
                    "ChannelParticipantAdmin": "admin", "ChatParticipantAdmin": "admin"}.get(type(p).__name__)
            if getattr(u, "bot", False):
                role = "bot"
            from ..sources.telegram import entity_username
            out.append({"id": u.id, "name": utils.get_display_name(u) or str(u.id), "username": entity_username(u),
                        "role": role or "member"})
        from .. import people
        people.save_members(self.store, chat_id, out)
        return out

    async def avatar(self, peer_id: int) -> str | None:
        """Аватарка чата или человека: скачать маленькое фото профиля, если его нет или оно сменилось."""
        from .. import avatars
        if not hasattr(self, "_av_sem"):
            self._av_sem = asyncio.Semaphore(2)   # не больше двух загрузок сразу — без лимитов Telegram
        async with self._av_sem:
            return await self._avatar(peer_id, avatars)

    async def _avatar(self, peer_id: int, avatars) -> str | None:
        ent = self._full.get(peer_id)
        if ent is None:
            try:
                ent = await self.client.get_entity(peer_id)
            except (ValueError, TypeError, KeyError):
                avatars._save(self.store, peer_id, "", None, "none")
                return None
            self._full[peer_id] = ent
        p = await avatars.fetch(self.client, self.store, self.src.cfg, ent, peer_id)
        return str(p) if p else None

    async def resolve(self, username: str) -> dict:
        """@имя или ссылка t.me/имя → номер чата (если он есть среди ваших диалогов — его можно открыть)."""
        from telethon import utils
        ent = await self.client.get_entity(username)
        cid = utils.get_peer_id(ent)
        return {"chat": cid, "title": utils.get_display_name(ent) or username, "known": self.store.get_chat(cid) is not None}
