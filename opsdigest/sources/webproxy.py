"""WEB-прокси Telegram (ссылки tg://webproxy и https://t.me/webproxy) — на чистом Python.

С лета 2026 Telegram Desktop умеет четвёртый тип прокси — WEB: соединение MTProxy (секрет dd… или обычный)
едет внутри обычного HTTPS/WebSocket к сайту на порту 443. Протокол — «WEB proxy protocol v1» из
https://github.com/telegramdesktop/tproxy-server (PROTOCOL.md). Кратко:

  1. capability = base64url(HMAC-SHA256(секрет, "tdesktop-web-proxy-bridge-v1\\n" + домен)) — без «=»;
  2. GET https://домен/?bridge=<capability> — страница-мост, в ней одноразовый bootstrap-токен и режим канала;
  3. POST /api/v1/session (Bearer bootstrap, тело — кадр HELLO) → X-Session-Token, X-Carrier-Mode, кадр WELCOME;
  4. дальше обмен кадрами  type:u8 | stream:u24 | len:u32 | данные  по одному из каналов:
       https            — POST /api/v1/up (X-Up-Seq) и длинный опрос POST /api/v1/down (X-Down-Cursor);
       https-lanes      — то же, но отдельная «полоса» на поток (X-Lane-ID);
       websocket        — один WebSocket /api/v1/ws, подпротокол tproxy-v1.<токен>;
       websocket-lanes  — WebSocket на поток, подпротокол tproxy-lane-v1.<токен>.<поток>.
  Внутри — обычный поток MTProxy (Telethon сам делает обфускацию), relay его не расшифровывает.

Здесь на каждое подключение Telethon открывается свой сеанс relay с одним потоком (номер 1) — так проще и надёжнее.
Выход в интернет — напрямую или через HTTP-прокси компьютера (CONNECT), как это делает окно Telegram.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.request
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlparse

log = logging.getLogger("opsdigest.webproxy")

OPEN, DATA, CLOSE, WINDOW, PING, PONG, HELLO, WELCOME, BYE = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x10, 0x11, 0x1F
INITIAL_WINDOW = 4 * 1024 * 1024
CHUNK = 64 * 1024
MAX_PAYLOAD = 1024 * 1024
WINDOW_STEP = 256 * 1024
BUF_CAP = 8 * 1024 * 1024  # сколько принятых данных держим, не дожидаясь, пока Telethon их прочитает
BATCH_BYTES = 1024 * 1024
POLL_TIMEOUT = 60          # сервер держит длинный опрос ~25 с
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC65B11"
CARRIERS = ("https", "https-lanes", "websocket", "websocket-lanes")

# последняя замеренная разница часов (сервер − компьютер) по заголовку Date ответа WEB-прокси
last_clock_offset: float | None = None


class WebProxyError(ConnectionError):
    """Ошибка WEB-прокси с понятным пользователю текстом (без секретов)."""


# ============================================================== секрет, адрес, capability
def split_server(server: str) -> tuple[str, str]:
    """'Домен' или 'домен/путь' (в ссылке путь закодирован как %2F) → (домен в нижнем регистре, путь без слэшей)."""
    s = unquote((server or "").strip())
    s = re.sub(r"^https?://", "", s, flags=re.I)
    host, _, path = s.partition("/")
    host = host.strip().rstrip(".").lower()
    if ":" in host:
        raise WebProxyError("в адресе WEB-прокси не должно быть порта — он всегда 443")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise WebProxyError("непонятный адрес WEB-прокси") from None
    if not re.fullmatch(r"[a-z0-9.-]+", host or "") or "." not in host:
        raise WebProxyError("непонятный адрес WEB-прокси")
    return host, path.strip("/")


def decode_secret(text: str) -> bytes:
    """hex (16 байт, или 17 с dd) или base64url; форма с маркером 0x70 (для адреса с путём) — маркер снимается."""
    s = unquote((text or "").strip())
    if re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) % 2 == 0:
        raw = bytes.fromhex(s)
    else:
        try:
            raw = base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_") + "=" * (-len(s) % 4))
        except (ValueError, TypeError):
            raise WebProxyError("не удалось разобрать секрет WEB-прокси") from None
    if len(raw) >= 17 and raw[0] == 0x70:
        raw = raw[1:]
    if len(raw) == 16 or (len(raw) == 17 and raw[0] == 0xDD):
        return raw
    if raw[:1] == b"\xee":
        raise WebProxyError("секрет ee… (FakeTLS) для WEB-прокси не подходит — нужен dd… или обычный")
    raise WebProxyError("секрет WEB-прокси должен быть 16 байт (32 hex-символа), можно с dd в начале")


def bridge_capability(host: str, path: str, secret: bytes) -> str:
    ctx = f"tdesktop-web-proxy-bridge-v1\n{host}" if not path else f"tdesktop-web-proxy-bridge-v2\n{host}\n{path}"
    mac = hmac.new(secret, ctx.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


# ============================================================== кадры
def frame(kind: int, sid: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + sid.to_bytes(3, "big") + len(payload).to_bytes(4, "big") + payload


def parse_frames(data: bytes) -> list[tuple[int, int, bytes]]:
    out, i = [], 0
    while i < len(data):
        if len(data) - i < 8:
            raise WebProxyError("WEB-прокси прислал обрезанный кадр")
        kind, sid, n = data[i], int.from_bytes(data[i + 1:i + 4], "big"), int.from_bytes(data[i + 4:i + 8], "big")
        if n > MAX_PAYLOAD or i + 8 + n > len(data):
            raise WebProxyError("WEB-прокси прислал повреждённый кадр")
        out.append((kind, sid, bytes(data[i + 8:i + 8 + n])))
        i += 8 + n
    return out


# ============================================================== сеть: выход в интернет, TLS, HTTP/1.1
def egress_for(setting: str | None) -> str | None:
    """Через что выходить в интернет: 'auto' — прокси Windows/переменные окружения (как браузер), 'off' — напрямую,
    иначе адрес HTTP-прокси вида http://[логин:пароль@]адрес:порт."""
    s = (setting or "auto").strip()
    if s.lower() in ("off", "none", "direct", "нет", "напрямую"):
        return None
    if s.lower() != "auto":
        return s if "://" in s else "http://" + s
    try:
        p = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        p = {}
    v = p.get("https") or p.get("http") or windows_proxy_for("https://example.com/")
    if not v:
        return None
    return v if "://" in v else "http://" + v


def windows_proxy_for(url: str) -> str | None:
    """Прокси из автонастройки Windows (PAC/WPAD), если она есть. Не Windows или ошибка — None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IEConfig(ctypes.Structure):
            _fields_ = [("fAutoDetect", wintypes.BOOL), ("lpszAutoConfigUrl", wintypes.LPWSTR),
                        ("lpszProxy", wintypes.LPWSTR), ("lpszProxyBypass", wintypes.LPWSTR)]

        class AutoProxyOptions(ctypes.Structure):
            _fields_ = [("dwFlags", wintypes.DWORD), ("dwAutoDetectFlags", wintypes.DWORD),
                        ("lpszAutoConfigUrl", wintypes.LPCWSTR), ("lpvReserved", ctypes.c_void_p),
                        ("dwReserved", wintypes.DWORD), ("fAutoLogonIfChallenged", wintypes.BOOL)]

        class ProxyInfo(ctypes.Structure):
            _fields_ = [("dwAccessType", wintypes.DWORD), ("lpszProxy", wintypes.LPWSTR),
                        ("lpszProxyBypass", wintypes.LPWSTR)]

        winhttp = ctypes.WinDLL("winhttp.dll")
        cfg = IEConfig()
        if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(cfg)):
            return None
        if cfg.lpszProxy:
            return _pick_proxy(cfg.lpszProxy)
        if not (cfg.fAutoDetect or cfg.lpszAutoConfigUrl):
            return None
        winhttp.WinHttpOpen.restype = ctypes.c_void_p
        h = winhttp.WinHttpOpen(UA, 1, None, None, 0)  # WINHTTP_ACCESS_TYPE_NO_PROXY
        if not h:
            return None
        try:
            opt = AutoProxyOptions()
            if cfg.lpszAutoConfigUrl:
                opt.dwFlags, opt.lpszAutoConfigUrl = 0x2, cfg.lpszAutoConfigUrl           # AUTO_CONFIG_URL
            else:
                opt.dwFlags, opt.dwAutoDetectFlags = 0x1, 0x1 | 0x2                       # AUTO_DETECT, DHCP|DNS
            opt.fAutoLogonIfChallenged = True
            info = ProxyInfo()
            winhttp.WinHttpGetProxyForUrl.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                                      ctypes.POINTER(AutoProxyOptions), ctypes.POINTER(ProxyInfo)]
            if winhttp.WinHttpGetProxyForUrl(h, url, ctypes.byref(opt), ctypes.byref(info)) and info.lpszProxy:
                return _pick_proxy(info.lpszProxy)
        finally:
            winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
            winhttp.WinHttpCloseHandle(h)
    except Exception:  # noqa: BLE001 — автонастройка не обязательна
        return None
    return None


def _pick_proxy(value: str) -> str | None:
    """'host:port' | 'https=host:port;http=…' | 'PROXY host:port; DIRECT' → первый подходящий host:port."""
    for part in re.split(r"[;\s]+(?=[A-Za-z]+=|PROXY|DIRECT)|;", value or ""):
        part = part.strip()
        if not part or part.upper() == "DIRECT":
            continue
        part = re.sub(r"^(?:https?=|PROXY\s+)", "", part, flags=re.I)
        if re.match(r"(?:socks|ftp)=", part, re.I):
            continue
        return part.strip()
    return None


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()   # в Windows — сертификаты из хранилища системы (в т.ч. корпоративные)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _tunnel(egress: str, host: str, port: int, timeout: float) -> socket.socket:
    """Блокирующий CONNECT через HTTP-прокси (вызывается в отдельном потоке)."""
    u = urlparse(egress)
    s = socket.create_connection((u.hostname, u.port or 8080), timeout=timeout)
    try:
        auth = ""
        if u.username:
            cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
            auth = "Proxy-Authorization: Basic " + base64.b64encode(cred).decode() + "\r\n"
        s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\nUser-Agent: {UA}\r\n{auth}\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise WebProxyError("прокси для выхода в интернет закрыл соединение")
            head += chunk
            if len(head) > 16384:
                raise WebProxyError("прокси для выхода в интернет прислал странный ответ")
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        m = re.match(r"HTTP/\d\.\d\s+(\d+)", first)
        code = int(m.group(1)) if m else 0
        if code == 407:
            schemes = re.findall(rb"(?im)^proxy-authenticate:\s*(\w+)", head)
            kind = ", ".join(sorted({x.decode().upper() for x in schemes})) or "?"
            raise WebProxyError(f"прокси для выхода в интернет просит авторизацию ({kind}) — укажите его адрес "
                                "с логином и паролем в настройках Telegram или выберите «напрямую»")
        if code != 200:
            raise WebProxyError(f"прокси для выхода в интернет не пустил к WEB-прокси: HTTP {code or '?'}")
        s.settimeout(None)
        return s
    except BaseException:
        s.close()
        raise


async def _open_tls(host: str, port: int, egress: str | None, timeout: float):
    ctx = _ssl_context()
    if egress:
        sock = await asyncio.wait_for(asyncio.to_thread(_tunnel, egress, host, port, timeout), timeout + 2)
        sock.setblocking(False)
        return await asyncio.wait_for(asyncio.open_connection(sock=sock, ssl=ctx, server_hostname=host), timeout)
    return await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout)


# Точка подмены для тестов (там — обычный TCP без TLS)
open_stream = _open_tls


async def _read_head(r: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    line = await r.readuntil(b"\r\n")
    m = re.match(rb"HTTP/\d\.\d\s+(\d+)", line)
    if not m:
        raise WebProxyError("WEB-прокси ответил не по HTTP")
    headers: dict[str, str] = {}
    while True:
        line = await r.readuntil(b"\r\n")
        if line == b"\r\n":
            break
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(m.group(1)), headers


async def _read_body(r: asyncio.StreamReader, status: int, headers: dict[str, str]) -> tuple[bytes, bool]:
    """→ (тело, нужно_закрыть_соединение)."""
    close = headers.get("connection", "").lower() == "close"
    if status in (204, 304) or 100 <= status < 200:
        return b"", close
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = bytearray()
        while True:
            size = int((await r.readuntil(b"\r\n")).split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while (await r.readuntil(b"\r\n")) != b"\r\n":
                    pass
                return bytes(body), close
            body += await r.readexactly(size)
            await r.readexactly(2)
    if "content-length" in headers:
        return await r.readexactly(int(headers["content-length"])), close
    return await r.read(), True


class HttpConn:
    """Одно keep-alive HTTPS-соединение с сайтом WEB-прокси (переподключается само)."""

    def __init__(self, host: str, egress: str | None, timeout: float):
        self.host, self.egress, self.timeout = host, egress, timeout
        self.r: asyncio.StreamReader | None = None
        self.w: asyncio.StreamWriter | None = None

    async def request(self, method: str, path: str, headers: dict[str, str] | None = None,
                      body: bytes | None = None, timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        h = {"Host": self.host, "User-Agent": UA, "Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9",
             "Connection": "keep-alive", **(headers or {})}
        if method != "GET":
            h["Content-Length"] = str(len(body or b""))
        raw = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode()
        raw += body or b""
        for attempt in (0, 1):
            fresh = self.w is None
            if fresh:
                self.r, self.w = await open_stream(self.host, 443, self.egress, self.timeout)
            try:
                self.w.write(raw)
                await self.w.drain()
                status, rh = await asyncio.wait_for(_read_head(self.r), timeout or self.timeout)
                data, close = await asyncio.wait_for(_read_body(self.r, status, rh), timeout or self.timeout)
                _note_clock(rh.get("date"))
                if close:
                    self.close()
                return status, rh, data
            except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ssl.SSLError) as e:
                self.close()
                if fresh or attempt:
                    raise WebProxyError(f"WEB-прокси: обрыв HTTPS-соединения ({type(e).__name__})") from None
            except asyncio.TimeoutError:
                self.close()
                raise
        raise WebProxyError("WEB-прокси: не удалось выполнить запрос")

    def close(self) -> None:
        if self.w is not None:
            try:
                self.w.close()
            except Exception:  # noqa: BLE001
                pass
        self.r = self.w = None


def _note_clock(date: str | None) -> None:
    global last_clock_offset
    if not date:
        return
    try:
        last_clock_offset = parsedate_to_datetime(date).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        pass


class WebSocket:
    """Минимальный клиент WebSocket (RFC 6455): только двоичные сообщения, ping/pong, close."""

    def __init__(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        self.r, self.w = r, w
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, host: str, path: str, protocol: str, egress: str | None, timeout: float) -> "WebSocket":
        r, w = await open_stream(host, 443, egress, timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {UA}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: {protocol}\r\nOrigin: https://{host}\r\nCache-Control: no-cache\r\n\r\n")
        w.write(req.encode())
        await w.drain()
        status, h = await asyncio.wait_for(_read_head(r), timeout)
        if status != 101:
            w.close()
            raise WebProxyError(f"WEB-прокси не открыл WebSocket (HTTP {status})")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if h.get("sec-websocket-accept") != accept or h.get("sec-websocket-protocol") != protocol:
            w.close()
            raise WebProxyError("WEB-прокси ответил на WebSocket не по протоколу")
        return cls(r, w)

    async def send(self, data: bytes, opcode: int = 0x2) -> None:
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        body = _mask(data, mask)
        async with self.lock:
            self.w.write(head + mask + body)
            await self.w.drain()

    async def recv(self) -> bytes:
        msg, first = bytearray(), None
        while True:
            b0, b1 = await self.r.readexactly(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = int.from_bytes(await self.r.readexactly(2), "big")
            elif n == 127:
                n = int.from_bytes(await self.r.readexactly(8), "big")
            if n > 64 * 1024 * 1024:
                raise WebProxyError("WEB-прокси прислал слишком большое сообщение WebSocket")
            mask = await self.r.readexactly(4) if b1 & 0x80 else None
            payload = await self.r.readexactly(n) if n else b""
            if mask:
                payload = _mask(payload, mask)
            if op == 0x9:
                await self.send(payload, 0xA)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise WebProxyError("WEB-прокси закрыл WebSocket")
            if op in (0x1, 0x2):
                first = op
            elif op != 0x0:
                continue
            msg += payload
            if b0 & 0x80:
                if first != 0x2:
                    raise WebProxyError("WEB-прокси прислал текст вместо данных")
                return bytes(msg)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.send((1000).to_bytes(2, "big"), 0x8), 2)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.w.close()
        except Exception:  # noqa: BLE001
            pass


def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


# ============================================================== поток через WEB-прокси
class WebProxyStream:
    """Один поток MTProxy через сеанс relay. reader/writer — для Telethon (readexactly/write/drain/close)."""
    SID = 1

    def __init__(self, server: str, secret: bytes, egress: str | None = None, timeout: float = 15):
        self.host, self.path = split_server(server)
        self.secret, self.egress, self.timeout = secret, egress, timeout
        self.api = "/" + (self.path + "/" if self.path else "")
        self.mode = ""
        self.token = ""
        self.ctl = HttpConn(self.host, egress, timeout)
        self.ws: WebSocket | None = None
        self.buf = bytearray()
        self.data_event = asyncio.Event()
        self.credit = INITIAL_WINDOW           # сколько ещё можно отправить relay (он добавляет кадрами WINDOW)
        self.owed = 0                           # принято, но ещё не возвращено relay кадром WINDOW
        self.pending = bytearray()              # данные Telethon, ещё не упакованные в кадры DATA
        self.out: list[bytes] = []              # служебные кадры потока SID (OPEN, WINDOW, PONG)
        self.out0: list[bytes] = []             # служебные кадры полосы 0 (PONG в режиме https-lanes)
        self.inflight = 0
        self.out_event = asyncio.Event()
        self.sent_event = asyncio.Event()
        self.sent_event.set()
        self.eof = False
        self.error: Exception | None = None
        self.closed = False
        self.tasks: list[asyncio.Task] = []
        self.reader = _Reader(self)
        self.writer = _Writer(self)

    # ---------------------------------------------------------------- открытие
    async def open(self) -> None:
        try:
            await self._open()
        except BaseException:
            await self.aclose()
            raise

    async def _open(self) -> None:
        cap = bridge_capability(self.host, self.path, self.secret)
        try:
            status, _, page = await self.ctl.request("GET", f"{self.api}?bridge={cap}", {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1"})
        except asyncio.TimeoutError:
            raise WebProxyError("WEB-прокси не ответил вовремя (сайт недоступен с этого компьютера?)") from None
        text = page.decode("utf-8", "replace")
        m = re.search(r"relayBase\s*=\s*(\"(?:[^\"\\]|\\.)*\")\s*,\s*bootstrap\s*=\s*(\"(?:[^\"\\]|\\.)*\")"
                      r"\s*,\s*carrierMode\s*=\s*(\"(?:[^\"\\]|\\.)*\")", text)
        if status == 200 and not m and "TelemtBridge" in text:
            raise WebProxyError("WEB-прокси узнал секрет, но работает на сервере telemt — его вид моста программа "
                                "пока не поддерживает; используйте ссылку MTProxy (tg://proxy…), если она есть")
        if status == 200 and not m and ("TelegramWebProxy" in text or "tproxy-android-init" in text):
            raise WebProxyError("WEB-прокси узнал секрет, но мост незнакомого вида — программа его пока не "
                                "поддерживает; используйте ссылку MTProxy (tg://proxy…), если она есть")
        if status != 200 or not m:
            raise WebProxyError("WEB-прокси не узнал секрет (открылся обычный сайт) — проверьте адрес и секрет "
                                f"(HTTP {status})")
        relay, bootstrap, mode = (json.loads(x) for x in m.groups())
        api = urlparse(relay).path or "/"
        self.api = api if api.endswith("/") else api + "/"
        if mode not in CARRIERS:
            raise WebProxyError(f"WEB-прокси выбрал неизвестный режим канала: {mode[:20]}")
        for attempt in range(8):
            status, h, body = await self.ctl.request("POST", self.api + "api/v1/session", {
                "Authorization": "Bearer " + bootstrap, "Content-Type": "application/octet-stream",
                "Origin": f"https://{self.host}"}, frame(HELLO, 0, b"\x01"))
            if status != 503:
                break
            await asyncio.sleep(_retry_after(h))
        if status != 200 or not h.get("x-session-token"):
            raise WebProxyError(f"WEB-прокси не открыл сеанс (HTTP {status})")
        if [f[0] for f in parse_frames(body)] != [WELCOME]:
            raise WebProxyError("WEB-прокси ответил на приветствие не по протоколу")
        self.token, self.mode = h["x-session-token"], h.get("x-carrier-mode") or mode
        if self.mode not in CARRIERS:
            raise WebProxyError("WEB-прокси выбрал неизвестный режим канала")
        log.debug("WEB-прокси: сеанс открыт, канал %s", self.mode)
        self.out.append(frame(OPEN, self.SID))
        loop = asyncio.get_running_loop()
        if self.mode == "websocket":
            self.ws = await WebSocket.connect(self.host, self.api + "api/v1/ws", "tproxy-v1." + self.token,
                                              self.egress, self.timeout)
        elif self.mode == "websocket-lanes":
            self.ws = await WebSocket.connect(self.host, self.api + "api/v1/ws",
                                              f"tproxy-lane-v1.{self.token}.{self.SID}", self.egress, self.timeout)
        if self.ws is not None:
            self.tasks += [loop.create_task(self._ws_recv()), loop.create_task(self._ws_send())]
        else:
            lanes = self.mode == "https-lanes"
            self.tasks.append(loop.create_task(self._http_send(self.SID if lanes else None)))
            self.tasks.append(loop.create_task(self._http_poll(self.SID if lanes else None)))
            if lanes:
                self.tasks.append(loop.create_task(self._http_send(0)))
                self.tasks.append(loop.create_task(self._http_poll(0)))
        self.out_event.set()

    # ---------------------------------------------------------------- приём кадров
    def _dispatch(self, data: bytes, lane: int | None = None) -> None:
        for kind, sid, payload in parse_frames(data):
            if lane is not None and sid != lane and kind != WELCOME:
                raise WebProxyError("WEB-прокси прислал кадр не своей полосы")
            if kind == DATA and sid == self.SID:
                self.buf += payload
                self.owed += len(payload)
                self._grant()
                self.data_event.set()
            elif kind == WINDOW and sid == self.SID and len(payload) == 4:
                self.credit += int.from_bytes(payload, "big")
                self.out_event.set()
            elif kind == CLOSE and sid == self.SID:
                self.eof = True
                self.data_event.set()
            elif kind == PING and sid == 0 and self.mode != "websocket-lanes":
                (self.out0 if self.mode == "https-lanes" else self.out).append(frame(PONG, 0, payload))
                self.out_event.set()
            elif kind == BYE:
                raise WebProxyError("WEB-прокси завершил сеанс")

    async def _ws_recv(self) -> None:
        try:
            while not self.closed:
                self._dispatch(await self.ws.recv(), self.SID if self.mode == "websocket-lanes" else None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(e)

    async def _http_poll(self, lane: int | None) -> None:
        conn = HttpConn(self.host, self.egress, self.timeout)
        cursor = "0"
        try:
            while not self.closed:
                h = {"Authorization": "Bearer " + self.token, "X-Down-Cursor": cursor, "Origin": f"https://{self.host}"}
                if lane is not None:
                    h["X-Lane-ID"] = str(lane)
                try:
                    status, rh, body = await conn.request("POST", self.api + "api/v1/down", h, b"", POLL_TIMEOUT)
                except asyncio.TimeoutError:
                    conn.close()
                    continue
                if status == 503:
                    await asyncio.sleep(_retry_after(rh))
                    continue
                if status == 204:
                    if rh.get("x-lane-closed") == "1":
                        return
                    continue
                if status != 200 or not rh.get("x-down-cursor") or not body:
                    raise WebProxyError(f"WEB-прокси отказал в приёме данных (HTTP {status})")
                self._dispatch(body, lane)
                cursor = rh["x-down-cursor"]
                if rh.get("x-lane-closed") == "1":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(e)
        finally:
            conn.close()

    # ---------------------------------------------------------------- отправка
    def _ready(self, lane0: bool) -> bool:
        if lane0:
            return bool(self.out0)
        return bool(self.out) or (bool(self.pending) and self.credit > 0)

    def _maybe_sent(self) -> None:
        if not self.inflight and not self.out and not self.out0 and not self.pending:
            self.sent_event.set()

    async def _next_batch(self, lane0: bool = False) -> bytes:
        """Следующая пачка кадров: служебные + DATA в пределах кредита relay (не больше BATCH_BYTES)."""
        while not self._ready(lane0):
            if self.closed or self.error is not None:
                raise asyncio.CancelledError
            self._maybe_sent()
            self.out_event.clear()
            await self.out_event.wait()
        queue = self.out0 if lane0 else self.out
        parts, size = [], 0
        while queue and len(parts) < 4096 and size < BATCH_BYTES:
            parts.append(queue.pop(0))
            size += len(parts[-1])
        while not lane0 and self.pending and self.credit > 0 and size < BATCH_BYTES and len(parts) < 4096:
            n = min(CHUNK, len(self.pending), self.credit)
            parts.append(frame(DATA, self.SID, bytes(self.pending[:n])))
            del self.pending[:n]
            self.credit -= n
            size += 8 + n
        self.inflight += 1
        return b"".join(parts)

    async def _ws_send(self) -> None:
        try:
            while not self.closed:
                batch = await self._next_batch()
                try:
                    await self.ws.send(batch)
                finally:
                    self.inflight -= 1
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(e)

    async def _http_send(self, lane: int | None) -> None:
        conn = HttpConn(self.host, self.egress, self.timeout)
        seq = 1
        try:
            while not self.closed:
                batch = await self._next_batch(lane == 0)
                try:
                    await self._post_up(conn, lane, seq, batch)
                finally:
                    self.inflight -= 1
                seq += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(e)
        finally:
            conn.close()

    async def _post_up(self, conn: HttpConn, lane: int | None, seq: int, batch: bytes) -> None:
        h = {"Authorization": "Bearer " + self.token, "Content-Type": "application/octet-stream",
             "X-Up-Seq": str(seq), "Origin": f"https://{self.host}"}
        if lane is not None:
            h["X-Lane-ID"] = str(lane)
        deadline = time.monotonic() + 90
        while True:
            try:
                status, rh, _ = await conn.request("POST", self.api + "api/v1/up", h, batch)
            except (WebProxyError, asyncio.TimeoutError):
                if time.monotonic() > deadline:
                    raise
                conn.close()
                await asyncio.sleep(0.5)
                continue
            if status == 503 and time.monotonic() < deadline:
                await asyncio.sleep(_retry_after(rh))
                continue
            break
        if status != 204 or rh.get("x-up-ack") != str(seq):
            raise WebProxyError(f"WEB-прокси не принял данные (HTTP {status})")

    def send_data(self, data: bytes) -> None:
        self._check()
        self.pending += data
        self.sent_event.clear()
        self.out_event.set()

    def _grant(self) -> None:
        """Вернуть relay кредит за принятые байты (кадр WINDOW), пока наш буфер не переполнен."""
        if self.owed >= WINDOW_STEP and len(self.buf) <= BUF_CAP:
            self.out.append(frame(WINDOW, self.SID, self.owed.to_bytes(4, "big")))
            self.owed = 0
            self.out_event.set()

    # ---------------------------------------------------------------- ошибки и закрытие
    def _fail(self, e: Exception) -> None:
        if self.closed or self.error is not None:
            return
        log.info("WEB-прокси: канал оборвался (%s)", type(e).__name__)
        self.error = e if isinstance(e, WebProxyError) else WebProxyError(f"WEB-прокси: обрыв канала ({type(e).__name__})")
        for ev in (self.data_event, self.out_event, self.sent_event):
            ev.set()

    def _check(self) -> None:
        if self.error is not None:
            raise self.error
        if self.closed:
            raise ConnectionError("WEB-прокси: соединение закрыто")

    async def aclose(self) -> None:
        if self.closed:
            return
        try:
            if self.token and self.error is None and self.ws is not None:
                await asyncio.wait_for(self.ws.send(frame(CLOSE, self.SID)), 2)
        except Exception:  # noqa: BLE001
            pass
        self.closed = True
        for ev in (self.data_event, self.out_event, self.sent_event):
            ev.set()
        for t in self.tasks:
            t.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self.token:
            try:
                await asyncio.wait_for(self.ctl.request("DELETE", self.api + "api/v1/session",
                                                        {"Authorization": "Bearer " + self.token}), 3)
            except Exception:  # noqa: BLE001
                pass
        self.ctl.close()


def _retry_after(h: dict[str, str]) -> float:
    try:
        return min(max(float(h.get("retry-after", "1")), 0.2), 30)
    except ValueError:
        return 1.0


class _Reader:
    def __init__(self, s: WebProxyStream):
        self.s = s

    async def readexactly(self, n: int) -> bytes:
        s = self.s
        while len(s.buf) < n:
            if s.eof or s.closed or s.error is not None:
                if s.error is not None and not s.eof:
                    raise s.error
                raise asyncio.IncompleteReadError(bytes(s.buf), n)
            s.data_event.clear()
            await s.data_event.wait()
        data = bytes(s.buf[:n])
        del s.buf[:n]
        s._grant()
        return data

    async def read(self, n: int = -1) -> bytes:
        s = self.s
        while not s.buf and not (s.eof or s.closed or s.error):
            s.data_event.clear()
            await s.data_event.wait()
        if not s.buf and s.error is not None and not s.eof:
            raise s.error
        k = len(s.buf) if n < 0 else min(n, len(s.buf))
        data = bytes(s.buf[:k])
        del s.buf[:k]
        if k:
            s._grant()
        return data

    def at_eof(self) -> bool:
        return not self.s.buf and (self.s.eof or self.s.closed)


class _Writer:
    def __init__(self, s: WebProxyStream):
        self.s = s

    def write(self, data: bytes) -> None:
        self.s.send_data(bytes(data))

    async def drain(self) -> None:
        self.s._check()
        while not self.s.sent_event.is_set():
            await self.s.sent_event.wait()
        self.s._check()

    def close(self) -> None:
        if not self.s.closed:
            asyncio.get_running_loop().create_task(self.s.aclose())

    async def wait_closed(self) -> None:
        await self.s.aclose()

    def is_closing(self) -> bool:
        return self.s.closed

    def get_extra_info(self, name, default=None):
        return default


# ============================================================== подключение для Telethon
def _make_connection(base):
    class _Conn(base):
        """proxy = (домен[/путь], 443, секрет_hex_с_dd_или_без, выход_в_интернет_или_None)."""

        def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
            self.web_server = proxy[0]
            self.web_secret = bytes.fromhex(proxy[2])
            self.web_egress = proxy[3] if len(proxy) > 3 else None
            host, _ = split_server(proxy[0])
            super().__init__(ip, port, dc_id, loggers=loggers, proxy=(host, 443, proxy[2]), local_addr=local_addr)

        async def _connect(self, timeout=None, ssl=None):
            stream = WebProxyStream(self.web_server, self.web_secret, self.web_egress, timeout or 15)
            try:
                await asyncio.wait_for(stream.open(), (timeout or 15) * 2)
            except asyncio.TimeoutError:
                await stream.aclose()
                raise WebProxyError("WEB-прокси не ответил вовремя") from None
            self._reader, self._writer = stream.reader, stream.writer
            self._codec = self.packet_codec(self)
            self._init_conn()
            await self._writer.drain()

    return _Conn


def connection_class(secret: bytes):
    """Класс подключения Telethon: dd… — с паддингом (randomized intermediate), обычный секрет — intermediate."""
    from telethon.network.connection.tcpmtproxy import (ConnectionTcpMTProxyIntermediate,
                                                        ConnectionTcpMTProxyRandomizedIntermediate)
    global _CONN_DD, _CONN_PLAIN
    if secret[:1] == b"\xdd":
        if _CONN_DD is None:
            _CONN_DD = _make_connection(ConnectionTcpMTProxyRandomizedIntermediate)
            _CONN_DD.__name__ = _CONN_DD.__qualname__ = "ConnectionTcpMTProxyWeb"
        return _CONN_DD
    if _CONN_PLAIN is None:
        _CONN_PLAIN = _make_connection(ConnectionTcpMTProxyIntermediate)
        _CONN_PLAIN.__name__ = _CONN_PLAIN.__qualname__ = "ConnectionTcpMTProxyWebPlain"
    return _CONN_PLAIN


_CONN_DD = None
_CONN_PLAIN = None


def server_param(host: str, path: str) -> str:
    return host + ("/" + path if path else "")


def link(host: str, path: str, secret: bytes) -> str:
    """Ссылка tg://webproxy для этого прокси (для журнала не использовать — в ней секрет)."""
    sec = secret.hex() if not path else base64.urlsafe_b64encode(b"\x70" + secret).rstrip(b"=").decode()
    return f"tg://webproxy?server={quote(server_param(host, path), safe='')}&secret={sec}"
