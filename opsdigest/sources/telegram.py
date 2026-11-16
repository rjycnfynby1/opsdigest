"""Telegram через пользовательский клиент (Telethon, протокол MTProto).

Программа входит в аккаунт как ещё одно устройство («OpsDigest» в Настройки → Устройства) и только читает:
список диалогов, сообщения и вложения. Чтение не отмечает сообщения прочитанными. Пишет она только в «Избранное»
(если включена отправка сводки) и, по явной команде proxy-refresh, — боту, выдающему прокси.

Вложения: изображения и лёгкие файлы скачиваются вместе с перепиской (правило — opsdigest/media.py), тяжёлые
остаются ссылкой и докачиваются по запросу (fetch_media). Полная выгрузка истории — export_history: идёт от новых
сообщений к старым порциями, после каждой порции запоминает, докуда дошла, поэтому её можно остановить и продолжить.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import __version__, logs
from .. import media as mediamod
from ..entities import EntityExtractor
from ..storage import Chat, Message, Store, iso, parse_iso
from .tg_proxy import ProxyError, ProxySpec, collect_proxies, find_links, telethon_kwargs

Progress = Callable[[str], None]
# Мессенджер держит одно постоянное подключение (messenger.Live). Пока оно на связи, выгрузка, сводка и вопросы
# пользуются им же, а не открывают второе: SHARED() → (client, proxy) или None.
SHARED: Callable[[], tuple[Any, Any] | None] | None = None
# Мессенджер сейчас подключается (или переподключается): тогда сводка и выгрузка ждут его подключения, а не открывают
# второе — одна сессия с двух адресов одновременно (разные серверы VPN) Telegram считает кражей и отзывает
# (AuthKeyDuplicatedError). LIVE_BUSY() → True, пока мессенджер включён, но ещё не на связи.
LIVE_BUSY: Callable[[], bool] | None = None
SESSION_REVOKED = ("Telegram отозвал сессию программы: один и тот же вход использовался одновременно с двух разных "
                   "адресов (например, мессенджер и сводка подключились через разные серверы VPN, или рабочая папка "
                   "открыта на двух компьютерах). Это защита Telegram, аккаунт в порядке. Старая сессия удалена — "
                   "войдите заново на шаге «Вход» (код придёт в Telegram).")


def _revoked(e: BaseException) -> bool:
    return type(e).__name__ in ("AuthKeyDuplicatedError", "AuthKeyUnregisteredError", "SessionRevokedError")
MAX_STORED_CHARS = 8000
SERVICE_IDS = {777000, 42777}  # служебные уведомления Telegram (коды входа) — не читаем никогда


def is_service(d: Any) -> bool:
    return getattr(d.entity, "is_self", False) or d.id in SERVICE_IDS
log = logs.get("telegram")


class TelegramError(RuntimeError):
    pass


def _media_tag(m: Any) -> str | None:
    if getattr(m, "photo", None):
        return "фото"
    if getattr(m, "voice", None):
        return "голосовое"
    if getattr(m, "video_note", None):
        return "видеосообщение"
    if getattr(m, "video", None):
        return "видео"
    if getattr(m, "sticker", None):
        return "стикер"
    if getattr(m, "poll", None):
        return "опрос"
    if getattr(m, "document", None):
        f = getattr(m, "file", None)
        name = getattr(f, "name", None) if f else None
        return f"файл: {name}" if name else "файл"
    if getattr(m, "media", None) and not getattr(m, "web_preview", None):
        return "вложение"
    return None


def _prefix(m: Any) -> str:
    tags = []
    if getattr(m, "fwd_from", None):
        tags.append("переслано")
    mt = _media_tag(m)
    if mt:
        tags.append(mt)
    return f"[{', '.join(tags)}] " if tags else ""


def message_text(m: Any) -> str:
    body = (getattr(m, "message", None) or "").strip()
    media = getattr(m, "media", None)
    if type(media).__name__ == "MessageMediaPoll":  # текст опроса — в поиск и в сводку
        try:
            p = media.poll
            body = (_tw(p.question) + " — " + " / ".join(_tw(a.text) for a in p.answers or []) +
                    ("\n" + body if body else "")).strip()
        except Exception:  # noqa: BLE001
            pass
    text = (_prefix(m) + body).strip()
    if len(text) > MAX_STORED_CHARS:
        text = text[:MAX_STORED_CHARS] + " …[обрезано]"
    return text


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


ENT = {"MessageEntityBold": "b", "MessageEntityItalic": "i", "MessageEntityUnderline": "u", "MessageEntityStrike": "s",
       "MessageEntityCode": "code", "MessageEntityPre": "pre", "MessageEntityTextUrl": "a", "MessageEntitySpoiler": "sp",
       "MessageEntityBlockquote": "q", "MessageEntityMentionName": "mn", "InputMessageEntityMentionName": "mn"}


def reply_info(m: Any, chat_id: int | None = None) -> tuple[int | None, int | None, dict]:
    """(ответ на номер в этом чате, тема форума, сведения об ответе для окна).

    В форуме у каждого сообщения темы есть «ответ» на начало темы — это не ответ, а принадлежность к теме.
    Ответ на сообщение из другого чата (цитата из другого чата) — не номер в этом чате: его показываем по заголовку."""
    rh = getattr(m, "reply_to", None)
    if rh is None or type(rh).__name__ != "MessageReplyHeader":
        return getattr(m, "reply_to_msg_id", None) if rh is None else None, None, {}
    rid = getattr(rh, "reply_to_msg_id", None)
    top = getattr(rh, "reply_to_top_id", None)
    topic = None
    if getattr(rh, "forum_topic", False):
        if top:
            topic = top
        else:
            topic, rid = rid, None  # сообщение в теме, а не ответ
    info: dict = {}
    q = getattr(rh, "quote_text", None)
    if q:
        info["q"] = str(q)[:400]
    peer = getattr(rh, "reply_to_peer_id", None)
    ext = None
    if peer is not None:
        try:
            from telethon import utils
            ext = utils.get_peer_id(peer)
        except Exception:  # noqa: BLE001
            ext = None
    rf = getattr(rh, "reply_from", None)
    if (ext is not None and ext != chat_id) or (rf is not None and peer is None and rid is None):
        name = getattr(rf, "from_name", None) or getattr(rf, "post_author", None) if rf is not None else None
        info.update({"c": ext, "id": rid})
        if name:
            info["from"] = name
        rm = getattr(rh, "reply_media", None)
        if rm is not None and not q:
            info["media"] = _media_tag(type("M", (), {"photo": getattr(rm, "photo", None),
                                                     "document": getattr(rm, "document", None),
                                                     "media": rm})()) or "вложение"
        return None, topic, {"x": info}
    return rid, topic, info


def message_meta(m: Any, text: str) -> dict | None:
    """То, что нужно окну мессенджера кроме текста: форматирование (смещения — в UTF-16, как в JavaScript),
    пересылка, правка, альбом, реакции, кнопки-ссылки, просмотры. Только то, что есть."""
    meta: dict = {}
    pre = _prefix(m)
    raw = getattr(m, "message", None) or ""
    if pre and text.startswith(pre.rstrip()):
        meta["p"] = _u16(pre) if raw.strip() else _u16(pre.rstrip())
    shift = (_u16(pre) if pre and raw.strip() else 0) - _u16(raw[:len(raw) - len(raw.lstrip())])
    limit = _u16(text)
    ents = []
    for e in getattr(m, "entities", None) or []:
        t = ENT.get(type(e).__name__)
        off, ln = getattr(e, "offset", None), getattr(e, "length", None)
        if not t or not isinstance(off, int) or not isinstance(ln, int):
            continue
        off += shift
        if off < 0 or off >= limit:
            continue
        x = getattr(e, "url", None) if t == "a" else getattr(e, "language", None) if t == "pre" else \
            getattr(e, "user_id", None) if t == "mn" else None
        ents.append([t, off, min(ln, limit - off)] + ([x] if x not in (None, "") else []))
    if ents:
        meta["e"] = ents
    fw = getattr(m, "fwd_from", None)
    if fw:
        name = getattr(fw, "from_name", None) or getattr(fw, "post_author", None)
        try:
            f = getattr(m, "forward", None)
            ent = (getattr(f, "sender", None) or getattr(f, "chat", None)) if f is not None else None
            if ent is not None:
                from telethon import utils
                name = utils.get_display_name(ent) or name
        except Exception:  # noqa: BLE001 — автор пересылки скрыт или не загружен
            pass
        meta["fw"] = name or "скрытый отправитель"
    ed = getattr(m, "edit_date", None)
    if isinstance(ed, datetime) and not getattr(m, "edit_hide", False):
        meta["ed"] = int(ed.timestamp())
    g = getattr(m, "grouped_id", None)
    if g:
        meta["g"] = str(g)
    rx = []
    for rc in getattr(getattr(m, "reactions", None), "results", None) or []:
        r = getattr(rc, "reaction", None)
        emo = getattr(r, "emoticon", None) or ("★" if type(r).__name__ == "ReactionPaid" else "◆")
        rx.append([emo, int(getattr(rc, "count", 0) or 0), getattr(rc, "chosen_order", None) is not None])
    if rx:
        meta["rx"] = rx
    bt = []
    for row in getattr(getattr(m, "reply_markup", None), "rows", None) or []:
        for b in getattr(row, "buttons", None) or []:
            txt = getattr(b, "text", None)
            if txt:
                bt.append([txt, getattr(b, "url", None)])
    if bt:
        meta["bt"] = bt[:12]
    f = getattr(m, "file", None)
    emo = getattr(f, "emoji", None) if f is not None and getattr(m, "sticker", None) else None
    if isinstance(emo, str) and emo:
        meta["emo"] = emo
    v = getattr(m, "views", None)
    if isinstance(v, int) and v:
        meta["v"] = v
    try:
        _rid, _topic, rinfo = reply_info(m, getattr(m, "chat_id", None))
    except Exception:  # noqa: BLE001
        rinfo = {}
    if rinfo.get("x"):
        meta["rx_"] = rinfo["x"]         # ответ на сообщение из другого чата
    elif rinfo.get("q"):
        meta["rq"] = rinfo["q"]          # цитата из сообщения, на которое ответили
    if getattr(m, "pinned", False):
        meta["pin"] = 1
    if getattr(m, "silent", False):
        meta["sil"] = 1
    poll = getattr(getattr(m, "media", None), "poll", None) if type(getattr(m, "media", None)).__name__ == \
        "MessageMediaPoll" else None
    if poll is not None:
        meta["poll"] = poll_view(m.media)
    via = getattr(m, "via_bot_id", None)
    if via:
        meta["via"] = via
    return meta or None


def _tw(x: Any) -> str:
    """TextWithEntities или строка → строка."""
    return x if isinstance(x, str) else (getattr(x, "text", None) or "")


def poll_view(media: Any) -> dict:
    """Опрос для окна: вопрос, варианты с голосами, мой выбор, закрыт ли, викторина."""
    poll, res = media.poll, getattr(media, "results", None)
    votes = {bytes(r.option): r for r in (getattr(res, "results", None) or [])}
    opts = []
    for a in poll.answers or []:
        r = votes.get(bytes(a.option))
        opts.append({"t": _tw(a.text), "o": bytes(a.option).hex(), "n": int(getattr(r, "voters", 0) or 0) if r else 0,
                     "my": bool(getattr(r, "chosen", False)) if r else False,
                     "ok": bool(getattr(r, "correct", False)) if r else False})
    return {"q": _tw(poll.question), "opts": opts, "total": int(getattr(res, "total_voters", 0) or 0),
            "closed": bool(poll.closed), "multi": bool(poll.multiple_choice), "quiz": bool(poll.quiz),
            "public": bool(poll.public_voters)}


def classify(d: Any) -> str:
    ent = d.entity
    if getattr(d, "is_user", False):
        return "bot" if getattr(ent, "bot", False) else "private"
    if getattr(d, "is_group", False):
        return "group"
    if getattr(d, "is_channel", False):
        return "channel"
    return "group"


def entity_username(ent: Any) -> str | None:
    u = getattr(ent, "username", None)
    if u:
        return u
    for x in getattr(ent, "usernames", None) or []:
        if getattr(x, "active", True) and getattr(x, "username", None):
            return x.username
    return None


def is_muted(d: Any) -> bool:
    ns = getattr(getattr(d, "dialog", None), "notify_settings", None)
    mu = getattr(ns, "mute_until", None)
    if not mu:
        return False
    ts = mu.timestamp() if isinstance(mu, datetime) else float(mu)
    return ts > time.time()


class TelegramSource:
    def __init__(self, cfg, store: Store, progress: Progress = print):
        self.cfg = cfg
        self.tg = cfg.telegram
        self.store = store
        self.progress = progress
        self.client = None
        self.proxy: ProxySpec | None = None
        self.me_id: int | None = None
        self.notes: list[str] = []
        self.extractor = EntityExtractor(cfg.get("entities") or {})
        self.policy = mediamod.Policy(cfg)
        self._entities: dict[int, Any] = {}

    # ================================================================ подключение
    def _client_args(self) -> tuple[str, int, str]:
        api_id, api_hash = str(self.tg.get("api_id") or "").strip(), str(self.tg.get("api_hash") or "").strip()
        if not api_id or not api_hash:
            raise TelegramError("не заданы TG_API_ID / TG_API_HASH (получить: my.telegram.org → API development tools)")
        if not api_id.isdigit():
            raise TelegramError(f"TG_API_ID должен быть числом, сейчас: {api_id!r}")
        session = self.cfg.path(self.tg.get("session") or "data/telegram")
        session.parent.mkdir(parents=True, exist_ok=True)
        return str(session), int(api_id), api_hash

    def _egress(self) -> str | None:
        """Выход в интернет для WEB-прокси: как у браузера (auto), напрямую или через указанный HTTP-прокси."""
        from .webproxy import egress_for
        return egress_for(self.tg.get("web_egress"))

    async def _fix_clock(self, specs: list[ProxySpec]) -> None:
        """Перед FakeTLS сверить часы: сервер не пустит, если время расходится больше чем на 3–5 с."""
        if not any(s.mode == "mtproto-faketls" for s in specs):
            return
        try:   # какое приветствие FakeTLS слать: new (как Telegram Desktop 2026, по умолчанию) или old (517 байт)
            from . import faketls
            faketls.HELLO_LEGACY = str(self.tg.get("faketls_hello") or "new").strip().lower() == "old"
        except Exception:  # noqa: BLE001 — нет Telethon: FakeTLS всё равно недоступен
            pass
        from ..netcheck import _clock, apply_clock, clock_line, web_urls
        urls = web_urls(specs)
        for prof in ((self.cfg.get("llm") or {}).get("profiles") or {}).values():
            u = str((prof or {}).get("base_url") or (prof or {}).get("host") or "")
            if u.startswith("http") and "localhost" not in u and "127.0.0.1" not in u:
                urls.append(u.split("/api/")[0].rstrip("/") + "/")
        try:
            offset = await asyncio.to_thread(apply_clock, urls)
        except Exception:  # noqa: BLE001
            return
        self.notes.append(clock_line(offset, _clock.get("src", "")))

    def _new_client(self, spec: ProxySpec, probe: bool = False):
        """probe — для проверки связи: временная пустая сессия, без ключа входа (ключ входа нельзя «светить» с
        разных адресов подряд — Telegram может его отозвать)."""
        from telethon import TelegramClient
        session, api_id, api_hash = self._client_args()
        kw = telethon_kwargs(spec, self._egress() if spec.kind == "web" else None)
        timeout = int(self.tg.get("connect_timeout") or 15)
        from ..secure.tgsession import session_for
        if probe:
            from telethon.sessions import MemorySession
            sess = MemorySession()
        else:
            sess = session_for(session)
        c = TelegramClient(sess, api_id, api_hash, timeout=timeout, connection_retries=1, retry_delay=1,
                           request_retries=3, flood_sleep_threshold=120, device_model="OpsDigest",
                           app_version=__version__, lang_code="ru", system_lang_code="ru", **kw)
        return c

    def proxy_candidates(self) -> list[ProxySpec]:
        return collect_proxies(self.tg, self.cfg.path("data/proxies.txt"), self.store.kv_get("tg_proxy_ok"))

    async def all_candidates(self) -> list[ProxySpec]:
        """VPN по подписке (если включён) — первым, затем прокси из списка."""
        vpn: list[ProxySpec] = []
        if (self.tg.get("vpn") or {}).get("enabled"):
            from .. import vpn as vpnmod
            try:
                vpn = await asyncio.to_thread(vpnmod.candidates, self.cfg)
            except vpnmod.VpnError as e:
                self.notes.append(f"✗ VPN: {e}")
                log.warning("VPN не запустился: %s", logs.scrub(str(e))[:200])
        last = self.store.kv_get("tg_vpn_ok") or ""
        if last:  # один и тот же сервер VPN для всех подключений, пока он работает (один адрес — одна сессия)
            vpn.sort(key=lambda s: s.raw != last)
        rest = [s for s in self.proxy_candidates() if not s.raw.startswith("vpn:")]
        return vpn + rest

    async def check_proxies(self) -> list[tuple[ProxySpec, bool, str, float]]:
        """Попробовать каждый прокси по отдельности (для команды check)."""
        out = []
        self.notes = []
        specs = await self.all_candidates()
        await self._fix_clock(specs)
        for spec in specs:
            t0 = time.monotonic()
            ok, err = False, ""
            client = None
            try:
                client = self._new_client(spec, probe=True)
                await asyncio.wait_for(client.connect(), (self.tg.get("connect_timeout") or 15) + 5)
                ok = client.is_connected()
            except ProxyError as e:
                err = str(e)
            except Exception as e:  # noqa: BLE001 — сетевые ошибки бывают любые
                err = f"{type(e).__name__}: {e}"[:200]
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
            sec = time.monotonic() - t0
            log.info("проверка связи: %s — %s за %.1f с%s", spec.mode, "ok" if ok else "нет", sec,
                     f" ({err.split(':', 1)[0]})" if err else "")
            if not ok:  # по шагам: где именно не получилось
                from ..netcheck import probe
                steps = await probe(spec, self._egress() if spec.kind == "web" else None, after_failure=True)
                for line in steps:
                    log.info("  шаг: %s", line)
                err = (err + "\n" if err else "") + "\n".join("      " + l for l in steps)
            out.append((spec, ok, err, sec))
            if ok and not spec.raw.startswith("vpn:"):
                self.store.kv_set("tg_proxy_ok", spec.raw)
        return out

    async def connect(self, interactive: bool = False, skip: set[str] | None = None,
                      require_auth: bool = True) -> None:
        """Подключиться через первый рабочий вариант. skip — прокси, которые уже отказали в этом запуске.

        require_auth=False — только связь (для входа по шагам из окна: send_code → sign_in)."""
        shared = SHARED() if SHARED is not None else None
        if shared:  # мессенджер на связи — работаем через его подключение
            self.client, self.proxy = shared
            self._shared = True
            if require_auth:
                await self._remember_me()
            return
        if not getattr(self, "is_live", False) and LIVE_BUSY is not None and LIVE_BUSY():  # ждём мессенджер
            self.progress("Мессенджер подключается к Telegram — жду его подключения (до 2 минут)…")
            for _ in range(120):
                await asyncio.sleep(1)
                shared = SHARED() if SHARED is not None else None
                if shared:
                    self.client, self.proxy = shared
                    self._shared = True
                    if require_auth:
                        await self._remember_me()
                    return
                if not LIVE_BUSY():
                    break
            else:
                raise TelegramError("мессенджер не подключился к Telegram за 2 минуты — посмотрите его состояние "
                                    "(вверху мессенджера) или выключите мессенджер и повторите")
        errors = []
        skip = skip or set()
        self._client_args()  # сначала — понятная ошибка, если ключи Telegram не заданы
        candidates = [s for s in await self.all_candidates() if s.raw not in skip]
        if not candidates:
            raise TelegramError("все прокси из списка отказали: " + ", ".join(sorted(skip))[:300])
        await self._fix_clock(candidates)
        for spec in candidates:
            try:
                client = self._new_client(spec)
            except ProxyError as e:
                errors.append(f"{spec.describe()}: {e}")
                continue
            self.progress(f"Подключаюсь к Telegram: {spec.describe()} …")
            try:
                await asyncio.wait_for(client.connect(), (self.tg.get("connect_timeout") or 15) + 5)
                if client.is_connected():
                    log.info("подключение: %s — ok", spec.mode)
                    self.client, self.proxy = client, spec
                    if spec.kind != "direct" and not spec.raw.startswith("vpn:"):
                        self.store.kv_set("tg_proxy_ok", spec.raw)
                    if spec.raw.startswith("vpn:"):
                        self.store.kv_set("tg_vpn_ok", spec.raw)
                    break
            except Exception as e:  # noqa: BLE001
                log.warning("подключение: %s — %s", spec.mode, logs.err_info(e))
                if _revoked(e):  # ключ сессии отозван: другие варианты не помогут, нужен новый вход
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self._drop_session(client)
                    raise TelegramError(SESSION_REVOKED) from None
                errors.append(f"{spec.describe()}: {type(e).__name__}: {e}"[:200])
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        if self.client is None:
            raise TelegramError("не удалось подключиться к Telegram ни через один вариант:\n  " + "\n  ".join(errors)
                                + "\nПодробно по шагам — кнопка «Проверить связь» на шаге Telegram.")
        if not require_auth:
            return
        if not await self.client.is_user_authorized():
            if not interactive:
                raise TelegramError("вход в Telegram не выполнен — пройдите шаг «Вход» в окне настройки")
            phone = str(self.tg.get("phone") or "").strip() or None
            await self.client.start(phone=phone) if phone else await self.client.start()
        await self._remember_me()

    def _drop_session(self, client=None) -> None:
        """Удалить мёртвую сессию (зашифрованную или обычную), чтобы шаг «Вход» начал с чистого листа."""
        log.warning("сессия Telegram отозвана (AuthKeyDuplicated/Unregistered) — удаляю, нужен новый вход")
        try:
            sess = getattr(client, "session", None)
            if sess is not None and hasattr(sess, "delete"):
                sess.delete()
        except Exception:  # noqa: BLE001
            pass
        try:
            session, _a, _h = self._client_args()
            for p in (Path(session + ".session"), Path(session + ".session-journal")):
                p.unlink(missing_ok=True)
        except (OSError, TelegramError):
            pass
        self.store.kv_set("tg_vpn_ok", "")

    async def _remember_me(self) -> dict:
        me = await self.client.get_me()
        name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x)
        info = {"id": me.id, "name": name, "username": getattr(me, "username", None)}
        self.store.kv_set("me", json.dumps(info, ensure_ascii=False))
        self.me_id = me.id
        return info

    # ---- вход по шагам (для окна): код из Telegram, затем при необходимости облачный пароль
    async def is_authorized(self) -> bool:
        return bool(self.client) and await self.client.is_user_authorized()

    async def send_code(self, phone: str) -> str:
        """'authorized' — уже вошли; 'code_sent' — код отправлен в Telegram (или SMS)."""
        if await self.is_authorized():
            await self._remember_me()
            return "authorized"
        r = await self.client.send_code_request(phone)
        self._login = {"phone": phone, "hash": r.phone_code_hash}
        log.info("вход: код запрошен (%s)", type(getattr(r, "type", None)).__name__)
        return "code_sent"

    async def sign_in(self, code: str | None = None, password: str | None = None) -> str:
        """'ok' — вошли; 'password_needed' — включена двухэтапная проверка, нужен облачный пароль."""
        from telethon import errors
        login = getattr(self, "_login", None)
        try:
            if password:
                await self.client.sign_in(password=password)
            else:
                if not login:
                    raise TelegramError("сначала запросите код")
                await self.client.sign_in(phone=login["phone"], code=str(code or "").strip(),
                                          phone_code_hash=login["hash"])
        except errors.SessionPasswordNeededError:
            log.info("вход: нужен облачный пароль")
            return "password_needed"
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
            log.info("вход: %s", type(e).__name__)
            raise TelegramError("код неверный или устарел — запросите новый") from e
        except errors.PasswordHashInvalidError as e:
            raise TelegramError("неверный облачный пароль") from e
        await self._remember_me()
        log.info("вход: выполнен")
        return "ok"

    async def close(self) -> None:
        if getattr(self, "_shared", False):  # чужое (общее) подключение не закрываем
            self.client, self._shared = None, False
            return
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001 — соединение могло уже умереть
                pass
            self.client = None

    async def with_failover(self, op, *args, **kwargs):
        """Выполнить шаг; если связь через текущий прокси оборвалась — переподключиться через следующий и повторить.

        Синхронизация инкрементальная, поэтому повтор безопасен: докачается только то, чего ещё нет в базе.
        """
        failed: set[str] = set()
        while True:
            try:
                return await op(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — сессию отозвали посреди работы
                if _revoked(e):
                    if not getattr(self, "_shared", False):
                        cl = self.client
                        await self.close()
                        self._drop_session(cl)
                    raise TelegramError(SESSION_REVOKED) from None
                if not isinstance(e, (ConnectionError, OSError, asyncio.TimeoutError)):
                    raise
                if getattr(self, "_shared", False):  # общее подключение переподключает мессенджер — ждём его
                    log.warning("обрыв общего подключения: %s — жду переподключения", logs.err_info(e))
                    self.progress(f"Связь оборвалась ({type(e).__name__}); жду переподключения…")
                    for _ in range(120):
                        await asyncio.sleep(1)
                        got = SHARED() if SHARED is not None else None
                        if got and got[0] is not self.client:
                            self.client, self.proxy = got
                            break
                    else:
                        raise TelegramError("связь с Telegram потеряна и не восстановилась за 2 минуты") from e
                    continue
                cur = self.proxy
                log.warning("обрыв связи (%s): %s — переключаюсь", cur.mode if cur else "?", logs.err_info(e))
                failed.add(cur.raw if cur else "direct")
                if cur is not None and self.store.kv_get("tg_proxy_ok") == cur.raw:
                    self.store.kv_set("tg_proxy_ok", "")  # больше не считаем его «последним рабочим»
                self.progress(f"Связь через {cur.describe() if cur else '?'} оборвалась ({type(e).__name__}: {e}); "
                              "переключаюсь на другой прокси…")
                await self.close()
                await self.connect(skip=failed)  # TelegramError, если пробовать больше нечего

    # ================================================================ чтение
    async def list_dialogs(self) -> list[dict]:
        """Все диалоги с назначенными правилами — для настройки config.toml (команда chats)."""
        out = []
        async for d in self.client.iter_dialogs(archived=None if self.tg.get("include_archived") else False):
            if is_service(d):
                continue
            kind = classify(d)
            uname = entity_username(d.entity)
            rule = self.cfg.chat_rule(d.name or "", uname, d.id, kind)
            out.append({"id": d.id, "title": d.name or "", "kind": kind, "username": uname, "unread": d.unread_count,
                        "muted": is_muted(d), "priority": rule["priority"], "mode": rule["kind"],
                        "explicit": rule["explicit"], "last": d.date.isoformat() if d.date else ""})
        return out

    async def _fetch(self, entity, since: datetime, cap: int, *, min_id: int = 0,
                     offset_id: int = 0) -> tuple[list, bool]:
        """Сообщения от новых к старым, до границы since. Возвращает (сообщения, дошли ли до границы)."""
        out = []
        reached = False
        async for m in self.client.iter_messages(entity, limit=cap, min_id=min_id, offset_id=offset_id):
            if m.date < since:
                reached = True
                break
            out.append(m)
        else:
            reached = len(out) < cap  # история кончилась раньше лимита
        return out, reached

    def _convert(self, chat_id: int, chat_title: str, msgs: list) -> list[Message]:
        from telethon import utils
        res = []
        for m in msgs:
            if getattr(m, "action", None):  # служебные: вход в чат, закрепление и т.п.
                continue
            text = message_text(m)
            if not text:
                continue
            s = getattr(m, "sender", None)
            if s is not None:
                name = utils.get_display_name(s) or str(getattr(m, "sender_id", "") or "")
            else:
                name = getattr(m, "post_author", None) or chat_title
            try:
                meta = message_meta(m, text)
            except Exception:  # noqa: BLE001 — оформление не должно мешать сохранить сам текст
                meta = None
            try:
                reply_to, topic, _ = reply_info(m, chat_id)
            except Exception:  # noqa: BLE001
                reply_to, topic = getattr(m, "reply_to_msg_id", None), None
            res.append(Message(chat_id=chat_id, msg_id=m.id, date=iso(m.date), sender=name, text=text,
                               sender_id=getattr(m, "sender_id", None), is_out=bool(getattr(m, "out", False)),
                               mentioned=bool(getattr(m, "mentioned", False)),
                               reply_to=reply_to, meta=meta, topic=topic))
        return res

    async def sync(self, since: datetime, only: set[int] | None = None) -> dict:
        """Забрать новые сообщения во всех нужных чатах (only — только в этих); догрузить историю, если since раньше,
        чем уже есть."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cap = int(self.tg.get("max_messages_per_chat") or 400)
        skip_muted = bool(self.tg.get("skip_muted"))
        stats = {"dialogs": 0, "chats": 0, "messages": 0, "capped": []}
        async for d in self.client.iter_dialogs(archived=None if self.tg.get("include_archived") else False):
            if is_service(d):
                continue
            stats["dialogs"] += 1
            kind = classify(d)
            uname = entity_username(d.entity)
            rule = self.cfg.chat_rule(d.name or "", uname, d.id, kind)
            muted = is_muted(d)
            prio = rule["priority"]
            if skip_muted and muted and not rule["explicit"]:
                prio = "ignore"
            chat = Chat(chat_id=d.id, title=d.name or "", kind=kind, username=uname, priority=prio,
                        mode=rule["kind"], note=rule["note"], unread=d.unread_count or 0, muted=muted)
            self.store.upsert_chat(chat)
            from ..avatars import fetch as fetch_avatar, photo_id
            self.store.set_chat_state(d.id, photo=photo_id(d.entity))
            if getattr(d.entity, "forum", False):
                self.store.set_chat_state(d.id, forum=True)
            if (prio == "ignore" and not (only and d.id in only)) or not d.date or d.date < since:
                continue
            if only is not None and d.id not in only:   # сводка по выбранным чатам — остальные не трогаем
                continue
            try:  # аватарка чата — для отчёта (скачивается, только если её нет или фото сменилось)
                await fetch_avatar(self.client, self.store, self.cfg, d.entity, d.id)
            except Exception as e:  # noqa: BLE001 — без аватарки сводка не хуже
                log.info("аватарка чата не получена: %s", type(e).__name__)
            prev = self.store.get_chat(d.id)
            max_id, min_id, covered = prev.max_msg_id, prev.min_msg_id, prev.min_date
            got = []
            last_id = getattr(getattr(d, "message", None), "id", 0) or 0
            # 1) новое с прошлой синхронизации (или всё до since — при первой)
            if not (max_id and last_id and last_id <= max_id):
                msgs, reached = await self._fetch(d.entity, since, cap, min_id=max_id)
                got += msgs
                if msgs:
                    if not max_id:  # первая синхронизация чата
                        min_id = min(m.id for m in msgs)
                        covered = iso(since) if reached else iso(min(m.date for m in msgs))
                    max_id = max(max_id, max(m.id for m in msgs))
                if not reached and len(msgs) >= cap:
                    stats["capped"].append(d.name)
            # 2) догрузка истории: просят период раньше, чем уже загружено (если история чата не выгружена целиком)
            if prev.max_msg_id and covered and parse_iso(covered) > since and min_id and \
                    not self.store.history_done(d.id):
                older, reached = await self._fetch(d.entity, since, cap, offset_id=min_id)
                got += older
                if older:
                    min_id = min(m.id for m in older)
                covered = iso(since) if reached else (iso(min(m.date for m in older)) if older else covered)
            if got:
                self._entities[d.id] = d.entity
                conv = self._convert(d.id, d.name or "", got)
                stats["messages"] += self.store.add_messages(conv)
                self.store.add_entities(self.extractor.rows(conv))
                ms = await self._save_media(d.id, d.name or "", got)
                stats["media"] = stats.get("media", 0) + ms["downloaded"]
                stats["chats"] += 1
                self.progress(f"  {d.name}: +{len(conv)}" + (f", файлов +{ms['downloaded']}" if ms["downloaded"] else ""))
            self.store.set_chat_range(d.id, max_id, min_id, covered)
        self.store.kv_set("last_sync", iso(datetime.now(timezone.utc)))
        log.info("синхронизация: диалогов %d, чатов с новым %d, сообщений %d, упёрлись в лимит %d",
                 stats["dialogs"], stats["chats"], stats["messages"], len(stats["capped"]))
        return stats

    # ================================================================ вложения
    def _media_rows(self, chat_id: int, msgs: list) -> list[tuple[dict, Any]]:
        out = []
        for m in msgs:
            if getattr(m, "action", None):
                continue
            info = mediamod.tg_info(m)
            if not info:
                continue
            row = {"chat_id": chat_id, "msg_id": m.id, "date": iso(m.date), **info}
            row["status"] = self.policy.status(info)
            out.append((row, m))
        return out

    async def _save_media(self, chat_id: int, title: str, msgs: list, download: bool = True,
                          cancel: Callable[[], bool] | None = None) -> dict:
        """Зарегистрировать вложения сообщений и скачать то, что положено по правилу (изображения, лёгкие файлы)."""
        st = {"media": 0, "downloaded": 0, "failed": 0}
        pairs = self._media_rows(chat_id, msgs)
        if not pairs:
            return st
        self.store.add_media([r for r, _ in pairs])
        st["media"] = len(pairs)
        if not download or not self.policy.download:
            return st
        known = self.store.media_of(chat_id, [r["msg_id"] for r, _ in pairs])
        jobs = [(known[r["msg_id"]], m) for r, m in pairs
                if r["msg_id"] in known and known[r["msg_id"]]["status"] in ("pending", "failed")
                and r["status"] == "pending"]
        sem = asyncio.Semaphore(3)

        async def one(row, m):
            if cancel and cancel():
                return
            async with sem:
                ok = await self._download(row, m, title)
            st["downloaded" if ok else "failed"] += 1
        await asyncio.gather(*(one(r, m) for r, m in jobs))
        return st

    async def _download(self, row: dict, m: Any, title: str, to_file: bool = False) -> bool:
        """Скачать вложение сообщения в папку media/. Ошибка одного файла не останавливает работу."""
        from ..secure import files, vault
        rel = mediamod.rel_path(self.cfg, self.store, row, title)
        target = self.cfg.path(rel)
        secure = vault.protected(target)
        data = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            kw: dict = {}
            if row.get("kind") == "photo":
                t = mediamod.photo_thumb(m, self.policy.photo_max_px)
                if t:
                    kw["thumb"] = t
            if secure:  # защищённая папка: файл не касается диска в открытом виде
                data = await self.client.download_media(m, file=bytes, **kw)
                if not data:
                    raise RuntimeError("Telegram не отдал файл")
                files.write_bytes(target, data, encrypt=True)
            elif to_file:  # большой файл — сразу на диск
                part = target.with_name(target.name + ".part")
                if part.exists():
                    part.unlink()
                res = await self.client.download_media(m, file=str(part), **kw)
                if not res:
                    raise RuntimeError("Telegram не отдал файл")
                Path(res).replace(target)
            else:
                data = await self.client.download_media(m, file=bytes, **kw)
                if not data:
                    raise RuntimeError("Telegram не отдал файл")
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(data)
                tmp.replace(target)
        except Exception as e:  # noqa: BLE001 — сеть, права, удалённое сообщение
            name = type(e).__name__
            gone = name in ("FileReferenceExpiredError", "MessageIdInvalidError") or "не отдал" in str(e)
            self.store.set_media_status(row["id"], "gone" if gone else "failed", f"{name}: {e}"[:200])
            log.info("вложение не скачано: %s", name)
            return False
        if data is not None:
            import hashlib
            sha = hashlib.sha256(data).hexdigest()
            mime = mediamod.sniff_mime(data[:16]) if row.get("kind") in ("photo", "image") else None
            size = len(data)
        else:
            sha = mediamod.sha256_file(target)
            mime = mediamod.sniff_mime(target.open("rb").read(16)) if row.get("kind") in ("photo", "image") else None
            size = target.stat().st_size
        self.store.set_media_file(row["id"], rel, sha, size=size, mime=mime)
        if row.get("kind") not in ("photo", "image") and not self.store.description(sha):
            text = mediamod.extract_text(target, row.get("name"))
            if text:  # содержимое лёгких документов (логи, txt, docx, xlsx) — в поиск и в сводку
                self.store.save_description(sha, {"kind": "содержимое файла", "summary": row.get("name") or "",
                                                  "details": "", "text": text, "model": "текст из файла"})
        return True

    async def _entity(self, chat_id: int):
        if chat_id in self._entities:
            return self._entities[chat_id]
        try:
            ent = await self.client.get_input_entity(chat_id)
        except (ValueError, TypeError, KeyError):
            ent = None
            async for d in self.client.iter_dialogs(archived=None):
                self._entities[d.id] = d.entity
                if d.id == chat_id:
                    ent = d.entity
            if ent is None:
                raise TelegramError("чат не найден среди диалогов этого аккаунта")
        self._entities[chat_id] = ent
        return ent

    async def download_rows(self, rows: list[dict], cancel: Callable[[], bool] | None = None,
                            to_file: bool = False) -> dict:
        """Скачать вложения из базы (status pending/failed/ref): сообщения запрашиваются заново по номерам."""
        st = {"downloaded": 0, "failed": 0, "gone": 0}
        by_chat: dict[int, list[dict]] = {}
        for r in rows:
            by_chat.setdefault(r["chat_id"], []).append(r)
        done = 0
        last = time.monotonic()
        for chat_id, items in by_chat.items():
            if cancel and cancel():
                break
            try:
                ent = await self._entity(chat_id)
            except TelegramError:
                for r in items:
                    self.store.set_media_status(r["id"], "gone", "чат недоступен")
                st["gone"] += len(items)
                continue
            title = (self.store.get_chat(chat_id) or Chat(chat_id, "", "")).title
            for i in range(0, len(items), 100):
                if cancel and cancel():
                    break
                part = items[i:i + 100]
                msgs = await self.client.get_messages(ent, ids=[r["msg_id"] for r in part])
                if not isinstance(msgs, list):
                    msgs = [msgs]
                by_id = {m.id: m for m in msgs if m is not None}
                sem = asyncio.Semaphore(3)

                async def one(r):
                    async with sem:
                        m = by_id.get(r["msg_id"])
                        if m is None or not getattr(m, "media", None):
                            self.store.set_media_status(r["id"], "gone", "сообщение удалено")
                            st["gone"] += 1
                            return
                        ok = await self._download(r, m, title, to_file=to_file)
                        st["downloaded" if ok else "failed"] += 1
                await asyncio.gather(*(one(r) for r in part))
                done += len(part)
                if time.monotonic() - last > 5:
                    last = time.monotonic()
                    self.progress(f"  скачано {st['downloaded']} из {len(rows)}…")
        return st

    async def fetch_media(self, media_id: int) -> dict:
        """Докачать конкретный файл (в т.ч. тяжёлый) по запросу."""
        row = self.store.get_media(media_id)
        if not row:
            raise TelegramError("такого файла нет в базе")
        limit = float(self.cfg.media.get("fetch_max_mb") or 200) * 2**20
        if (row.get("size") or 0) > limit:
            raise TelegramError(f"файл больше {limit / 2**20:.0f} МБ — увеличьте media.fetch_max_mb, если он нужен")
        if row["status"] == "ok" and row.get("path") and self.cfg.path(row["path"]).is_file():
            return row
        st = await self.download_rows([row], to_file=True)
        if not st["downloaded"]:
            row = self.store.get_media(media_id) or row
            raise TelegramError(f"файл не скачан: {row.get('error') or 'Telegram не отдал файл'}")
        return self.store.get_media(media_id) or row

    # ================================================================ полная выгрузка истории
    async def export_history(self, *, chat_ids: set[int] | None = None, since: datetime | None = None,
                             with_media: bool = True, cancel: Callable[[], bool] | None = None) -> dict:
        """Вся история (или с даты) всех чатов, кроме исключённых, — с изображениями и лёгкими файлами.

        chat_ids — только эти чаты (тогда исключённые тоже выгружаются: это явная просьба)."""
        cancel = cancel or (lambda: False)
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stats = {"dialogs": 0, "chats": 0, "messages": 0, "new": 0, "media": 0, "downloaded": 0, "failed": 0,
                 "excluded": 0, "complete": 0, "cancelled": False}
        dialogs = []
        async for d in self.client.iter_dialogs(archived=None):  # «вся история» — и архивные чаты тоже
            if is_service(d) or (chat_ids is not None and d.id not in chat_ids):
                continue
            self._entities[d.id] = d.entity
            dialogs.append(d)
        stats["dialogs"] = len(dialogs)
        self.progress(f"Диалогов для выгрузки: {len(dialogs)}" + (f" (с {since:%d.%m.%Y})" if since else " (вся история)"))
        for i, d in enumerate(dialogs, 1):
            if cancel():
                stats["cancelled"] = True
                break
            kind = classify(d)
            uname = entity_username(d.entity)
            rule = self.cfg.chat_rule(d.name or "", uname, d.id, kind)
            chat = Chat(chat_id=d.id, title=d.name or "", kind=kind, username=uname, priority=rule["priority"],
                        mode=rule["kind"], note=rule["note"], unread=d.unread_count or 0, muted=is_muted(d))
            self.store.upsert_chat(chat)
            if rule["priority"] == "ignore" and chat_ids is None:
                stats["excluded"] += 1
                continue
            await self._export_chat(d, f"[{i}/{len(dialogs)}] {d.name}", since, with_media, cancel, stats)
        if cancel():
            stats["cancelled"] = True
        self.store.kv_set("last_export", iso(datetime.now(timezone.utc)))
        log.info("выгрузка истории: диалогов %d, чатов %d, сообщений %d, файлов %d, остановлено: %s",
                 stats["dialogs"], stats["chats"], stats["messages"], stats["downloaded"], stats["cancelled"])
        return stats

    async def _export_chat(self, d: Any, label: str, since: datetime | None, with_media: bool,
                           cancel: Callable[[], bool], stats: dict) -> None:
        prev = self.store.get_chat(d.id)
        max_id, min_id, covered = prev.max_msg_id, prev.min_msg_id, prev.min_date
        title = d.name or ""
        n_chat = 0
        last_note = time.monotonic()

        async def flush(buf: list) -> None:
            nonlocal n_chat, last_note
            if not buf:
                return
            conv = self._convert(d.id, title, buf)
            stats["new"] += self.store.add_messages(conv)
            self.store.add_entities(self.extractor.rows(conv))
            ms = await self._save_media(d.id, title, buf, download=with_media, cancel=cancel)
            for k in ("media", "downloaded", "failed"):
                stats[k] += ms[k]
            n_chat += len(conv)
            stats["messages"] += len(conv)
            if time.monotonic() - last_note > 6:
                last_note = time.monotonic()
                oldest = min(m.date for m in buf).astimezone()
                self.progress(f"  {label}: {n_chat} сообщ., до {oldest:%d.%m.%Y}"
                              + (f", файлов +{stats['downloaded']}" if stats["downloaded"] else ""))

        last_id = getattr(getattr(d, "message", None), "id", 0) or 0
        # 1) новее уже загруженного — от старых к новым, чтобы после остановки продолжить с того же места
        if max_id and last_id > max_id:
            buf: list = []
            async for m in self.client.iter_messages(d.entity, min_id=max_id, reverse=True):
                buf.append(m)
                if len(buf) >= 200:
                    await flush(buf)
                    max_id = max(max_id, max(x.id for x in buf))
                    self.store.set_chat_range(d.id, max_id, min_id, covered)
                    buf = []
                    if cancel():
                        return
            await flush(buf)
            if buf:
                max_id = max(max_id, max(x.id for x in buf))
                self.store.set_chat_range(d.id, max_id, min_id, covered)
        # 2) старше уже загруженного — от новых к старым, до начала истории или до since
        if self.store.history_done(d.id) or (since and covered and parse_iso(covered) <= since):
            if n_chat:
                stats["chats"] += 1
            self.progress(f"  {label}: история уже выгружена" + (f", новых {n_chat}" if n_chat else ""))
            return
        offset = min_id if max_id else 0
        buf = []
        reached = exhausted = False
        async for m in self.client.iter_messages(d.entity, offset_id=offset):
            if since and m.date < since:
                reached = True
                break
            buf.append(m)
            if len(buf) >= 200:
                await flush(buf)
                max_id = max(max_id, max(x.id for x in buf))
                min_id = min(x.id for x in buf) if not min_id else min(min_id, min(x.id for x in buf))
                covered = iso(min(x.date for x in buf))
                self.store.set_chat_range(d.id, max_id, min_id, covered)
                buf = []
                if cancel():
                    return
        else:
            exhausted = True
        await flush(buf)
        if buf:
            max_id = max(max_id, max(x.id for x in buf))
            min_id = min(x.id for x in buf) if not min_id else min(min_id, min(x.id for x in buf))
            covered = iso(min(x.date for x in buf))
        if reached and since:
            covered = iso(since)
        self.store.set_chat_range(d.id, max_id, min_id, covered)
        if exhausted:
            self.store.set_history_done(d.id)
            stats["complete"] += 1
        if n_chat:
            stats["chats"] += 1
        self.progress(f"  {label}: {n_chat} сообщ." + (" — вся история" if exhausted else "")
                      + (f", файлов скачано {stats['downloaded']} (всего)" if with_media and stats["downloaded"] else ""))

    # ================================================================ запись (только в «Избранное» и боту прокси)
    async def send_to_saved(self, text: str, file: Path | None = None) -> None:
        for part in split_message(text):
            await self.client.send_message("me", part, parse_mode="html", link_preview=False)
        if file:
            import io

            from ..secure import files
            bio = io.BytesIO(files.read_bytes(file))  # отчёт в «Избранное» — расшифрованным (только в ваш Telegram)
            bio.name = Path(file).name
            await self.client.send_file("me", bio, caption="Полный отчёт (откройте в браузере)")

    async def refresh_proxies_from_bot(self, bot: str, command: str, wait: int = 40) -> list[str]:
        """Экспериментально: написать боту команду и собрать ссылки на прокси из ответа (текст, кнопки, ссылки)."""
        ent = await self.client.get_entity(bot)
        last = await self.client.get_messages(ent, limit=1)
        last_id = last[0].id if last else 0
        await self.client.send_message(ent, command)
        links: list[str] = []
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not links:
            await asyncio.sleep(3)
            async for m in self.client.iter_messages(ent, min_id=last_id):
                if getattr(m, "out", False):
                    continue
                texts = [m.message or ""]
                for row in getattr(m, "buttons", None) or []:
                    for b in row:
                        u = getattr(b, "url", None)
                        if u:
                            texts.append(u)
                for e in getattr(m, "entities", None) or []:
                    u = getattr(e, "url", None)
                    if u:
                        texts.append(u)
                links += find_links("\n".join(texts))
        links = list(dict.fromkeys(links))
        if links:
            from ..secure import files
            cache = self.cfg.path("data/proxies.txt")
            old = [l.strip() for l in files.read_text(cache).splitlines()] if cache.exists() else []
            merged = list(dict.fromkeys(links + [l for l in old if l and not l.startswith("#")]))[:20]
            files.write_text(cache, "# прокси от бота " + bot + ", новые сверху\n" + "\n".join(merged) + "\n")
        return links


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Разбить длинный текст на части по границам строк (лимит Telegram — 4096 символов)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts
