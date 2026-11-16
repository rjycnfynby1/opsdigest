"""MTProxy FakeTLS (секрет начинается с ee…) для Telethon — на чистом Python, без внешних пакетов.

Приветствие (ClientHello) — точная копия того, что с середины 2026 шлют официальные клиенты Telegram
(tdlib, td/mtproto/TlsInit.cpp, лицензия Boost 1.0): современный Chrome с X25519MLKEM768, ECH и перемешанными
расширениями. Старое приветствие фиксированной длины 517 байт (как в TelethonFakeTLS) с июня 2026 узнаётся DPI
по отпечатку JA4 — соединение рвут сразу после него, хотя сам прокси исправен.

Обёртки потока основаны на TelethonFakeTLS (https://github.com/Ro0tz5/telethon-faketls, MIT, © Ro0tz5), отличия:
не нужен пакет cryptography (ставится без интернета на любой Python), у обёрток есть wait_closed/is_closing,
обрыв или неожиданная запись TLS даёт ConnectionError с понятной причиной, ответ сервера разбирается любой длины.

Как это работает: перед обычным MTProxy-протоколом клиент притворяется браузером, открывающим HTTPS к домену
из секрета (ClientHello с подписью HMAC-SHA256 от секрета и временем), проверяет подпись ответа сервера,
а дальше заворачивает весь поток в записи TLS application data (0x17 0x03 0x03 <длина> <данные>).

Если сервер (mtg, telemt) приветствие НЕ узнал (не тот ключ, время, адрес в чёрном списке), он не рвёт
соединение, а пересылает его настоящему сайту-«маске». Тот отвечает обычным TLS 1.3 — по форме ответ
такой же, но подпись не сходится. explain_response() разбирает ответ для диагностики (без секретов).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import socket
import time

from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

log = logging.getLogger("opsdigest.faketls")

# Поправка часов (точное время − время компьютера), с. Сервер FakeTLS (например, mtg) отклоняет приветствие,
# если время в нём расходится с его часами больше чем на 3–5 с; Telegram Desktop сам подправляет время по
# серверу, а мы — по заголовку Date ответа какого-нибудь HTTPS-сайта (opsdigest/netcheck.py).
CLOCK_OFFSET = 0.0

# Какое приветствие слать: False — как Telegram Desktop/tdlib 2026 (по умолчанию), True — старое 517 байт.
# Включается настройкой telegram.faketls_hello = "old" (только если диагностика показала, что прокси
# принимает старое приветствие, а новое — нет).
HELLO_LEGACY = False
HELLO_NAMES = {False: "новое (как Telegram Desktop 2026)", True: "старое (517 байт)"}

# Поле random ServerHello у HelloRetryRequest (RFC 8446, 4.1.3) — так отвечает настоящий сайт, которому не
# подошли наши ключи; FakeTLS-сервер так не отвечает никогда.
HRR_RANDOM = bytes.fromhex("cf21ad74e59a6111be1d8c021e65b891c2a211167abb8c5e079e09e2c8a8339c")

P25519 = 2 ** 255 - 19

def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def parse_secret(secret: str) -> tuple[bytes, bytes]:
    """Секрет без префикса ee (hex или base64) → (16 байт ключа, домен)."""
    s = secret.strip()
    try:
        raw = bytes.fromhex("ee" + s)
    except ValueError:
        b = re.sub(r"[^a-zA-Z0-9+/_-]+", "", "7" + s)
        raw = base64.urlsafe_b64decode(b.replace("+", "-").replace("/", "_") + "=" * (-len(b) % 4))
    if len(raw) < 18 or raw[0] != 0xEE:
        raise ValueError("секрет FakeTLS должен содержать 16 байт ключа и домен")
    return raw[1:17], raw[17:]


def x25519_like_key() -> bytes:
    """Правдоподобный публичный ключ x25519 — как в tdlib: точка кривой, трижды удвоенная (кратна 8)."""
    p = P25519
    while True:
        k = bytearray(os.urandom(32))
        k[31] &= 127
        x = int.from_bytes(k, "big") % p
        y2 = (((x + 486662) * x + 1) * x) % p
        if pow(y2, (p - 1) // 2, p) != 1:
            continue
        try:
            for _ in range(3):
                den = 4 * ((((x + 486662) * x + 1) * x) % p) % p
                x = ((x * x - 1) ** 2 % p) * pow(den, -1, p) % p
        except ValueError:  # 0 не обращается — практически невозможно, просто берём другое число
            continue
        return x.to_bytes(32, "little")


def mlkem768_like_key() -> bytes:
    """Правдоподобный ключ ML-KEM-768 (1184 байта): 768 коэффициентов < 3329 по 12 бит + 32 случайных байта."""
    out = bytearray()
    for _ in range(384):
        a = int.from_bytes(os.urandom(4), "little") % 3329
        b = int.from_bytes(os.urandom(4), "little") % 3329
        out += bytes((a & 255, (a >> 8) + ((b & 15) << 4), b >> 4))
    return bytes(out) + os.urandom(32)


def grease_values(n: int = 7) -> bytes:
    g = bytearray((c & 0xF0) + 0x0A for c in os.urandom(n))
    for i in range(1, n, 2):
        if g[i] == g[i - 1]:
            g[i] ^= 0x10
    return bytes(g)


class _Hello:
    """Сборщик ClientHello с вложенными «областями» длины (2 байта), как TlsHelloStore в tdlib."""

    def __init__(self):
        self.buf = bytearray()
        self.scopes: list[int] = []

    def add(self, *parts: bytes) -> "_Hello":
        for part in parts:
            self.buf += part
        return self

    def begin(self) -> "_Hello":
        self.scopes.append(len(self.buf))
        self.buf += b"\0\0"
        return self

    def end(self) -> "_Hello":
        start = self.scopes.pop()
        self.buf[start:start + 2] = (len(self.buf) - start - 2).to_bytes(2, "big")
        return self


def build_client_hello(domain: bytes) -> bytes:
    """ClientHello как у официальных клиентов Telegram (tdlib, td/mtproto/TlsInit.cpp, вариант не для Apple):
    современный Chrome — X25519MLKEM768, ECH, ALPS, расширения в случайном порядке. Поле random — нули
    (его заполняет подпись). Старый шаблон фиксированной длины 517 байт DPI с июня 2026 узнаёт и режет."""
    g = grease_values()
    gr = lambda i: bytes((g[i], g[i]))  # noqa: E731
    sni = _Hello().add(b"\x00\x00").begin().begin().add(b"\x00").begin().add(domain).end().end().end().buf
    ech = (_Hello().add(b"\xfe\x0d").begin().add(b"\x00\x00\x01\x00\x01", os.urandom(1), b"\x00\x20",
                                                  x25519_like_key())
           .begin().add(os.urandom(144 + 32 * secrets.randbelow(4))).end().end().buf)
    parts = [
        bytes(sni),
        b"\x00\x05\x00\x05\x01\x00\x00\x00\x00",
        b"\x00\x0a\x00\x0c\x00\x0a" + gr(4) + b"\x11\xec\x00\x1d\x00\x17\x00\x18",
        b"\x00\x0b\x00\x02\x01\x00",
        b"\x00\x0d\x00\x18\x00\x16\x09\x04\x09\x05\x09\x06\x04\x03\x08\x04\x04\x01\x05\x03\x08"
        b"\x05\x05\x01\x08\x06\x06\x01",
        b"\x00\x10\x00\x0e\x00\x0c\x02\x68\x32\x08\x68\x74\x74\x70\x2f\x31\x2e\x31",
        b"\x00\x12\x00\x00",
        b"\x00\x17\x00\x00",
        b"\x00\x1b\x00\x03\x02\x00\x02",
        b"\x00\x23\x00\x00",
        b"\x00\x2b\x00\x07\x06" + gr(6) + b"\x03\x04\x03\x03",
        b"\x00\x2d\x00\x02\x01\x01",
        b"\x00\x33\x04\xef\x04\xed" + gr(4) + b"\x00\x01\x00\x11\xec\x04\xc0" + mlkem768_like_key()
        + x25519_like_key() + b"\x00\x1d\x00\x20" + x25519_like_key(),
        b"\x44\xcd\x00\x05\x00\x03\x02\x68\x32",
        bytes(ech),
        b"\xff\x01\x00\x01\x00",
    ]
    secrets.SystemRandom().shuffle(parts)
    h = _Hello().add(b"\x16\x03\x01").begin().add(b"\x01\x00").begin()
    h.add(b"\x03\x03", b"\x00" * 32, b"\x20", os.urandom(32))
    h.add(b"\x00\x20", gr(0), b"\x13\x01\x13\x02\x13\x03\xc0\x2b\xc0\x2f\xc0\x2c\xc0\x30\xcc\xa9\xcc\xa8"
          b"\xc0\x13\xc0\x14\x00\x9c\x00\x9d\x00\x2f\x00\x35\x01\x00")
    h.begin().add(gr(2), b"\x00\x00", *parts, gr(3), b"\x00\x01\x00")
    pad = 513 - len(h.buf)
    if pad > 0:
        h.add(b"\x00\x15").begin().add(b"\x00" * pad).end()
    return bytes(h.end().end().end().buf)


def build_client_hello_legacy(domain: bytes) -> bytes:
    """Старое приветствие фиксированной длины 517 байт (шаблон TelethonFakeTLS, Chrome ~2020). Только для
    сравнения в диагностике и как запасной вариант по настройке: DPI узнаёт его по отпечатку JA4."""
    sni = (b"\x00\x00" + (len(domain) + 5).to_bytes(2, "big") + (len(domain) + 3).to_bytes(2, "big") + b"\x00"
           + len(domain).to_bytes(2, "big") + domain)
    ext = (b"\x4a\x4a\x00\x00" + sni + b"\x00\x17\x00\x00" + b"\xff\x01\x00\x01\x00"
           + b"\x00\x0a\x00\x0a\x00\x08\xba\xba\x00\x1d\x00\x17\x00\x18" + b"\x00\x0b\x00\x02\x01\x00"
           + b"\x00\x23\x00\x00" + b"\x00\x10\x00\x0e\x00\x0c\x02\x68\x32\x08\x68\x74\x74\x70\x2f\x31\x2e\x31"
           + b"\x00\x05\x00\x05\x01\x00\x00\x00\x00"
           + b"\x00\x0d\x00\x12\x00\x10\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x05\x01\x08\x06\x06\x01"
           + b"\x00\x12\x00\x00"
           + b"\x00\x33\x00\x2b\x00\x29\xba\xba\x00\x01\x00\x00\x1d\x00\x20" + x25519_like_key()
           + b"\x00\x2d\x00\x02\x01\x01" + b"\x00\x2b\x00\x0b\x0a\x9a\x9a\x03\x04\x03\x03\x03\x02\x03\x01"
           + b"\x00\x1b\x00\x03\x02\x00\x02" + b"\x1a\x1a\x00\x01\x00")
    head = (b"\x03\x03" + b"\x00" * 32 + b"\x20" + os.urandom(32) + b"\x00\x20\xfa\xfa\x13\x01\x13\x02\x13\x03"
            b"\xc0\x2b\xc0\x2f\xc0\x2c\xc0\x30\xcc\xa9\xcc\xa8\xc0\x13\xc0\x14\x00\x9c\x00\x9d\x00\x2f\x00\x35"
            b"\x01\x00")
    pad = max(0, 517 - (9 + len(head) + 2 + len(ext) + 4))
    ext += b"\x00\x15" + pad.to_bytes(2, "big") + b"\x00" * pad
    body = head + len(ext).to_bytes(2, "big") + ext
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


class FakeTLSCodec:
    def __init__(self, secret: str, legacy: bool | None = None):
        self.secret, self.domain = parse_secret(secret)
        self.legacy = HELLO_LEGACY if legacy is None else legacy
        self.client_random = b""
        self.session_id = b""

    def client_hello(self, now: float | None = None) -> bytes:
        build = build_client_hello_legacy if self.legacy else build_client_hello
        hello = bytearray(build(self.domain[:182]))
        digest = _hmac(self.secret, bytes(hello))
        ts = (int(now if now is not None else time.time() + CLOCK_OFFSET) & 0xFFFFFFFF).to_bytes(4, "little")
        hello[11:43] = digest[:28] + bytes(ts[i] ^ digest[28 + i] for i in range(4))
        self.client_random = bytes(hello[11:43])
        self.session_id = bytes(hello[44:44 + hello[43]])
        return bytes(hello)

    def verify_server_hello(self, data: bytes) -> bool:
        """data — ответ целиком: запись ServerHello + ChangeCipherSpec + первая запись данных (как в tdlib)."""
        if len(data) < 43 or not data.startswith(b"\x16\x03\x03"):
            return False
        zeroed = data[:11] + b"\x00" * 32 + data[43:]
        return hmac.compare_digest(data[11:43], _hmac(self.secret, self.client_random + zeroed))

    def key_mark(self) -> str:
        """Короткий отпечаток ключа (16 бит SHA-256) — чтобы сравнивать ссылки, не показывая секрет."""
        return hashlib.sha256(self.secret).hexdigest()[:4]


# ============================================================== разбор ответа для диагностики
_GROUPS = {0x001D: "X25519", 0x0017: "P-256", 0x0018: "P-384", 0x11EC: "X25519MLKEM768", 0x6399: "X25519Kyber768"}
_RTYPES = {0x14: "CCS", 0x15: "alert", 0x16: "handshake", 0x17: "данные"}


def tls_records(data: bytes) -> tuple[list[tuple[int, int, int]], int]:
    """Записи TLS подряд: [(тип, смещение, длина тела)], и сколько байт в хвосте не сложилось в запись."""
    out, i = [], 0
    while i + 5 <= len(data) and data[i] in _RTYPES and data[i + 1] == 3:
        n = int.from_bytes(data[i + 3:i + 5], "big")
        if i + 5 + n > len(data):
            break
        out.append((data[i], i, n))
        i += 5 + n
    return out, len(data) - i


def _parse_server_hello(body: bytes) -> dict:
    """Поля ServerHello (тело записи handshake). Пустой словарь — не ServerHello."""
    if len(body) < 4 + 38 or body[0] != 0x02:
        return {}
    p = 4
    info: dict = {"legacy_version": body[p:p + 2].hex(), "random": body[p + 2:p + 34]}
    p += 34
    sid_len = body[p]
    info["session_id"] = body[p + 1:p + 1 + sid_len]
    p += 1 + sid_len
    if p + 3 > len(body):
        return info
    info["cipher"] = body[p:p + 2].hex()
    p += 3
    exts: list[int] = []
    if p + 2 <= len(body):
        end = min(len(body), p + 2 + int.from_bytes(body[p:p + 2], "big"))
        p += 2
        while p + 4 <= end:
            et, ln = int.from_bytes(body[p:p + 2], "big"), int.from_bytes(body[p + 2:p + 4], "big")
            val = body[p + 4:p + 4 + ln]
            exts.append(et)
            if et == 0x002B and len(val) == 2:
                info["version"] = val.hex()
            elif et == 0x0033 and len(val) >= 2:
                info["group"] = int.from_bytes(val[:2], "big")
            p += 4 + ln
    info["extensions"] = exts
    return info


def explain_response(codec: FakeTLSCodec, data: bytes) -> dict:
    """Что именно ответил сервер на приветствие — для диагностики, без секретов и без содержимого.

    Ключи: records [(тип, длина)], tail (байт вне записей), hello (поля ServerHello), hrr, sid_echo,
    signed_by (сколько записей данных подписано; 0 — подпись не сошлась ни для какого числа записей,
    None — нечего проверять), first_bytes (первые 3 байта ответа, если это не TLS)."""
    recs, tail = tls_records(data)
    info: dict = {"records": [(t, n) for t, _, n in recs], "tail": tail, "bytes": len(data), "signed_by": None}
    if not recs:
        info["first_bytes"] = data[:3].hex()
        return info
    if recs[0][0] == 0x15 and recs[0][2] >= 2:          # alert: уровень и код (112 — «неизвестное имя»)
        info["alert"] = data[recs[0][1] + 6]
    if recs[0][0] == 0x16:
        sh = _parse_server_hello(data[5:5 + recs[0][2]])
        info["hello"] = sh
        if sh:
            info["hrr"] = sh.get("random") == HRR_RANDOM
            info["sid_echo"] = sh.get("session_id") == codec.session_id
    # Подпись: как Telegram Desktop (ServerHello + CCS + первая запись данных), затем — по 2, 3, … записям.
    data_ends = [o + 5 + n for t, o, n in recs if t == 0x17]
    if data.startswith(b"\x16\x03\x03") and len(data) >= 43 and data_ends:
        info["signed_by"] = 0
        for k, end in enumerate(data_ends, 1):
            if codec.verify_server_hello(data[:end]):
                info["signed_by"] = k
                break
    return info


def response_shape(info: dict) -> tuple:
    """«Форма» ответа для сравнения двух попыток: тип первых записей и поля ServerHello (без случайных байтов)."""
    sh = info.get("hello") or {}
    return (tuple(t for t, _ in info.get("records", [])[:3]), info.get("alert"), info.get("first_bytes"),
            sh.get("cipher"), sh.get("version"), sh.get("group"), tuple(sh.get("extensions") or ()),
            info.get("hrr"), info.get("sid_echo"))


def describe_response(info: dict) -> list[str]:
    """Человекочитаемые строки по explain_response (без секретов)."""
    lines = []
    recs = info.get("records") or []
    if not info.get("bytes"):
        return ["ответа нет"]
    if not recs:
        fb = info.get("first_bytes", "")
        what = "похоже на HTTP" if fb in ("485454", "3c2144", "3c6874") else "не TLS"
        return [f"ответ {info['bytes']} байт, {what} (начало {fb})"]
    parts = [f"{_RTYPES.get(t, hex(t))} {n} б" for t, n in recs[:8]]
    more = f" и ещё {len(recs) - 8}" if len(recs) > 8 else ""
    tail = f", незаконченный хвост {info['tail']} б" if info.get("tail") else ""
    lines.append(f"ответ: {len(recs)} записей TLS — " + ", ".join(parts) + more + tail)
    if "alert" in info:
        code = info["alert"]
        lines.append(f"сервер прислал отказ TLS (alert {code}" + (" — «неизвестное имя сервера»: прокси не знает домен "
                                                                   "из секрета)" if code == 112 else ")"))
    sh = info.get("hello")
    if sh:
        g = sh.get("group")
        lines.append("ServerHello: " + ", ".join(filter(None, [
            f"версия {'TLS 1.3' if sh.get('version') == '0304' else (sh.get('version') or 'TLS 1.2')}",
            f"шифр {sh['cipher']}" if sh.get("cipher") else "",
            f"группа {_GROUPS.get(g, hex(g))}" if g is not None else "",
            "расширения " + ",".join(f"{e:04x}" for e in sh.get("extensions") or []) if sh.get("extensions") else "",
            "повторяет наш session_id" if info.get("sid_echo") else "session_id НЕ наш",
        ])))
        if info.get("hrr"):
            lines.append("это HelloRetryRequest — так отвечает обычный сайт, FakeTLS-сервер так не делает")
    sb = info.get("signed_by")
    if sb is None:
        lines.append("подпись проверить нельзя: в ответе нет записи данных (17 03 03)")
    elif sb == 1:
        lines.append("подпись сошлась (как проверяет Telegram Desktop)")
    elif sb:
        lines.append(f"подпись сошлась только по {sb} записям данных — сервер подписывает больше, чем проверяет "
                     "Telegram Desktop")
    else:
        n = sum(1 for t, _ in recs if t == 0x17)
        lines.append(f"подпись не сошлась ни по одной из {n} записей данных" if n > 1 else "подпись не сошлась")
    return lines


class FakeTLSReader:
    """Читает TLS-записи и отдаёт их содержимое как сплошной поток."""

    def __init__(self, upstream: asyncio.StreamReader):
        self.upstream = upstream
        self.buf = bytearray()

    async def read_server_hello(self) -> bytes:
        """Запись ServerHello (любой длины) + ChangeCipherSpec + первая запись данных — как в tdlib."""
        head = await self.upstream.readexactly(5)
        if head[:3] != b"\x16\x03\x03":
            raise ConnectionError("FakeTLS: прокси не узнал приветствие — ответил не как FakeTLS-сервер "
                                  "(часы компьютера расходятся с точным временем, неверный секрет или домен)")
        body = await self.upstream.readexactly(int.from_bytes(head[3:5], "big"))
        mark = await self.upstream.readexactly(9 + 2)
        if mark[:9] != b"\x14\x03\x03\x00\x01\x01\x17\x03\x03":
            raise ConnectionError("FakeTLS: прокси не узнал приветствие — ответил обычный сайт "
                                  "(часы компьютера расходятся с точным временем, неверный секрет или домен)")
        tail = await self.upstream.readexactly(int.from_bytes(mark[9:11], "big"))
        return head + body + mark + tail

    async def _record(self) -> bytes:
        while True:
            hdr = await self.upstream.readexactly(5)
            rtype, ver, length = hdr[:1], hdr[1:3], int.from_bytes(hdr[3:5], "big")
            if ver != b"\x03\x03" or rtype not in (b"\x14", b"\x17"):
                raise ConnectionError(f"FakeTLS: неожиданная запись TLS {hdr[:3].hex()} — прокси закрыл соединение?")
            payload = await self.upstream.readexactly(length)
            if rtype == b"\x17":
                return payload

    async def readexactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += await self._record()
        data, self.buf = bytes(self.buf[:n]), self.buf[n:]
        return data

    async def read(self, n: int = -1) -> bytes:
        if not self.buf:
            self.buf += await self._record()
        k = len(self.buf) if n < 0 else min(n, len(self.buf))
        data, self.buf = bytes(self.buf[:k]), self.buf[k:]
        return data

    def at_eof(self) -> bool:
        return not self.buf and self.upstream.at_eof()


class FakeTLSWriter:
    """Заворачивает исходящие данные в записи TLS application data."""
    MAX = 16384 + 24

    def __init__(self, upstream: asyncio.StreamWriter):
        self.upstream = upstream

    def write(self, data: bytes) -> int:
        for i in range(0, len(data), self.MAX):
            chunk = data[i:i + self.MAX]
            self.upstream.write(b"\x17\x03\x03" + len(chunk).to_bytes(2, "big") + chunk)
        return len(data)

    async def drain(self) -> None:
        await self.upstream.drain()

    def close(self) -> None:
        self.upstream.close()

    async def wait_closed(self) -> None:
        await self.upstream.wait_closed()

    def is_closing(self) -> bool:
        return self.upstream.is_closing()

    def get_extra_info(self, name, default=None):
        return self.upstream.get_extra_info(name, default)

    @property
    def transport(self):
        return self.upstream.transport


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """Подключение Telethon через MTProxy с секретом ee… . proxy = (адрес, порт, секрет_без_ee)."""

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        self.fake_tls = FakeTLSCodec(proxy[2])
        host = proxy[0]
        if len(host) > 60:
            host = socket.gethostbyname(host)
        super().__init__(ip, port, dc_id, loggers=loggers, proxy=(host, proxy[1], self.fake_tls.secret.hex()),
                         local_addr=local_addr)

    async def _connect(self, timeout=None, ssl=None):
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host=self._ip, port=self._port), timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"FakeTLS: нет TCP-соединения с прокси за {timeout or 0:.0f} с — порт закрыт в этой "
                                  "сети или этой программе не разрешён выход в интернет") from None
        except OSError as e:
            raise ConnectionError(f"FakeTLS: не удалось подключиться к прокси ({type(e).__name__})") from None
        self._writer.write(self.fake_tls.client_hello())
        await self._writer.drain()
        reader = FakeTLSReader(self._reader)
        try:
            hello = await asyncio.wait_for(reader.read_server_hello(), timeout=timeout)
        except asyncio.IncompleteReadError:
            raise ConnectionError("FakeTLS: прокси закрыл соединение сразу после приветствия (пускает только из своей "
                                  "сети, часы компьютера расходятся с точным временем, неверный секрет или домен)") from None
        except asyncio.TimeoutError:
            raise ConnectionError("FakeTLS: прокси не ответил на приветствие (часы компьютера расходятся с точным временем больше чем на 3 с, неверный секрет или домен)") from None
        if not self.fake_tls.verify_server_hello(hello):
            raise ConnectionError("FakeTLS: прокси не узнал приветствие и переслал соединение обычному сайту — "
                                  "неверный секрет (не та ссылка) или прокси не пускает с этого адреса; подробности — "
                                  "«Проверить связь»")
        log.debug("FakeTLS: рукопожатие выполнено")
        self._reader, self._writer = reader, FakeTLSWriter(self._writer)
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()
