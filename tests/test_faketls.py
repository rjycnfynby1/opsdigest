"""FakeTLS (прокси с секретом ee…): рукопожатие и обмен через настоящий Telethon с локальным «прокси»,
который проверяет ClientHello так же, как серверы MTProxy (mtprotoproxy/mtg)."""
import asyncio
import collections
import hashlib
import hmac
import logging
import os
import time

import pytest

pytest.importorskip("telethon.network.connection.tcpmtproxy")

from opsdigest.sources.faketls import ConnectionTcpMTProxyFakeTLS, FakeTLSCodec, parse_secret  # noqa: E402
from opsdigest.sources.tg_proxy import parse_proxy, telethon_kwargs  # noqa: E402

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
DOMAIN = b"www.google.com"
LINK = "tg://proxy?server=127.0.0.1&port={port}&secret=ee" + KEY.hex() + DOMAIN.hex()


def mac(msg):
    return hmac.new(KEY, msg, hashlib.sha256).digest()


async def fake_proxy(reader, writer, seen):
    hdr = await reader.readexactly(5)
    hello = hdr + await reader.readexactly(int.from_bytes(hdr[3:5], "big"))
    seen["hello_len"] = len(hello)
    digest = hello[11:43]
    calc = mac(hello[:11] + b"\x00" * 32 + hello[43:])
    x = bytes(a ^ b for a, b in zip(digest, calc))
    seen["digest_ok"] = x[:28] == b"\x00" * 28
    seen["time_skew"] = abs(int.from_bytes(x[28:], "little") - int(time.time()))
    seen["sni"] = DOMAIN in hello
    sid = hello[44:76]
    if seen.get("mlkem"):   # сервер выбрал X25519MLKEM768 — ServerHello длиннее 127 байт (как у telemt)
        ext = b"\x00\x33\x04\x64\x11\xec\x04\x60" + os.urandom(1120) + b"\x00\x2b\x00\x02\x03\x04"
    else:
        ext = b"\x00\x33\x00\x24\x00\x1d\x00\x20" + os.urandom(32) + b"\x00\x2b\x00\x02\x03\x04"
    body = b"\x03\x03" + b"\x00" * 32 + b"\x20" + sid + b"\x13\x01\x00" + len(ext).to_bytes(2, "big") + ext
    hs = b"\x02" + len(body).to_bytes(3, "big") + body
    srv_hello = b"\x16\x03\x03" + len(hs).to_bytes(2, "big") + hs
    assert len(srv_hello) == (127 if not seen.get("mlkem") else 127 + 1124 - 36)
    junk = os.urandom(150)
    resp = srv_hello + b"\x14\x03\x03\x00\x01\x01" + b"\x17\x03\x03" + len(junk).to_bytes(2, "big") + junk
    d = mac(digest + resp)
    resp = resp[:11] + d + resp[43:]
    writer.write(resp)
    await writer.drain()
    rec = await reader.readexactly(5)  # первая запись — заголовок obfuscated2 (64 байта) от Telethon
    seen["first_record"] = (rec[:3], int.from_bytes(rec[3:], "big"))
    await reader.readexactly(int.from_bytes(rec[3:], "big"))
    writer.write(b"\x17\x03\x03\x00\x05hello")
    await writer.drain()
    seen["done"] = True
    await asyncio.sleep(0.5)
    writer.close()


def test_secret_parsing():
    key, dom = parse_secret(KEY.hex() + DOMAIN.hex())
    assert key == KEY and dom == DOMAIN
    import base64
    b64 = base64.urlsafe_b64encode(b"\xee" + KEY + DOMAIN).decode().rstrip("=")
    assert parse_secret(b64[1:]) == (KEY, DOMAIN) or parse_secret(b64.lstrip("7")) == (KEY, DOMAIN)
    hello = FakeTLSCodec(KEY.hex() + DOMAIN.hex()).client_hello()
    assert hello[:3] == b"\x16\x03\x01" and int.from_bytes(hello[3:5], "big") == len(hello) - 5


def walk_extensions(hello: bytes) -> dict:
    """Разбор ClientHello так же строго, как серверы MTProxy: все длины должны сойтись."""
    assert hello[5] == 1 and int.from_bytes(hello[6:9], "big") == len(hello) - 9
    i = 9 + 2 + 32
    assert hello[i] == 32
    i += 1 + 32
    n = int.from_bytes(hello[i:i + 2], "big")
    suites = [hello[i + 2 + k:i + 4 + k] for k in range(0, n, 2)]
    assert suites[1] == b"\x13\x01" and (suites[0][0] & 0x0F) == 0x0A
    i += 2 + n
    assert hello[i:i + 2] == b"\x01\x00"
    i += 2
    total = int.from_bytes(hello[i:i + 2], "big")
    i += 2
    assert i + total == len(hello)
    exts = {}
    while i < len(hello):
        typ, ln = int.from_bytes(hello[i:i + 2], "big"), int.from_bytes(hello[i + 2:i + 4], "big")
        exts[typ] = hello[i + 4:i + 4 + ln]
        i += 4 + ln
    assert i == len(hello)
    return exts


def test_client_hello_like_official_clients():
    orders = set()
    for _ in range(5):
        hello = FakeTLSCodec(KEY.hex() + DOMAIN.hex()).client_hello()
        exts = walk_extensions(hello)
        sni = exts[0x0000]
        assert sni[2] == 0 and sni[5:] == DOMAIN and int.from_bytes(sni[3:5], "big") == len(DOMAIN)
        ks = exts[0x0033]
        assert int.from_bytes(ks[:2], "big") == len(ks) - 2 == 1261
        assert b"\x11\xec\x04\xc0" in ks and ks[-36:-32] == b"\x00\x1d\x00\x20"
        for must in (0x0005, 0x000a, 0x000b, 0x000d, 0x0010, 0x0012, 0x0017, 0x001b, 0x0023, 0x002b, 0x002d,
                     0x44CD, 0xFE0D, 0xFF01):
            assert must in exts
        assert len(hello) > 1700
        orders.add(tuple(k for k in exts if (k & 0x0F0F) != 0x0A0A))
    assert len(orders) > 1          # порядок расширений каждый раз свой, как у Chrome


@pytest.mark.parametrize("mlkem", [False, True])
def test_handshake_and_stream_with_real_telethon(mlkem):
    seen = {"mlkem": mlkem}

    async def run():
        srv = await asyncio.start_server(lambda r, w: fake_proxy(r, w, seen), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        spec = parse_proxy(LINK.format(port=port))
        kw = telethon_kwargs(spec)
        assert kw["connection"] is ConnectionTcpMTProxyFakeTLS
        loggers = collections.defaultdict(lambda: logging.getLogger("t"))
        conn = kw["connection"]("149.154.167.51", 443, 2, loggers=loggers, proxy=kw["proxy"])
        await conn._connect(timeout=5)  # без фоновых циклов Telethon — читаем сами
        data = await asyncio.wait_for(conn._reader.readexactly(5), 5)
        conn._writer.close()
        await conn._writer.wait_closed()
        srv.close()
        await srv.wait_closed()
        return data

    data = asyncio.run(run())
    assert seen["hello_len"] > 1700 and seen["digest_ok"] and seen["time_skew"] <= 2 and seen["sni"]
    assert seen["first_record"] == (b"\x17\x03\x03", 64)
    assert data == b"hello"


def test_bad_secret_rejected_by_client():
    async def bad_proxy(reader, writer):
        hdr = await reader.readexactly(5)
        await reader.readexactly(int.from_bytes(hdr[3:5], "big"))
        writer.write(b"\x16\x03\x03" + os.urandom(200))  # ответ без правильной подписи
        await writer.drain()

    async def run():
        srv = await asyncio.start_server(bad_proxy, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        kw = telethon_kwargs(parse_proxy(LINK.format(port=port)))
        loggers = collections.defaultdict(lambda: logging.getLogger("t"))
        conn = kw["connection"]("149.154.167.51", 443, 2, loggers=loggers, proxy=kw["proxy"])
        with pytest.raises(ConnectionError):
            await conn._connect(timeout=5)
        srv.close()

    asyncio.run(run())


# ============================================================== старое приветствие и разбор ответа
def server_response(sid: bytes, client_random: bytes, key: bytes | None, *, data_records=(150,), tickets=(),
                    sign_all: bool = False, random: bytes | None = None, group: bytes = b"\x00\x1d") -> bytes:
    """Ответ «сервера»: ServerHello + CCS + записи данных. key=None — настоящий сайт (подписи нет)."""
    ks = group + (b"\x00\x20" + os.urandom(32) if group == b"\x00\x1d" else b"")
    ext = b"\x00\x33" + len(ks).to_bytes(2, "big") + ks + b"\x00\x2b\x00\x02\x03\x04"
    body = (b"\x03\x03" + (random or b"\x00" * 32) + bytes([len(sid)]) + sid + b"\x13\x01\x00"
            + len(ext).to_bytes(2, "big") + ext)
    hs = b"\x02" + len(body).to_bytes(3, "big") + body
    resp = b"\x16\x03\x03" + len(hs).to_bytes(2, "big") + hs + b"\x14\x03\x03\x00\x01\x01"
    signed_end = None
    for n in data_records:
        resp += b"\x17\x03\x03" + n.to_bytes(2, "big") + os.urandom(n)
        signed_end = signed_end or len(resp)
    for n in tickets:
        resp += b"\x17\x03\x03" + n.to_bytes(2, "big") + os.urandom(n)
    if key is None:
        if random is None:
            resp = resp[:11] + os.urandom(32) + resp[43:]
        return resp
    end = len(resp) if sign_all else signed_end
    d = hmac.new(key, client_random + resp[:end], hashlib.sha256).digest()
    return resp[:11] + d + resp[43:]


def test_legacy_hello_is_517_bytes_and_well_formed():
    from opsdigest.sources.faketls import build_client_hello_legacy
    for dom in (DOMAIN, b"a" * 60):
        hello = build_client_hello_legacy(dom)
        assert len(hello) == (517 if len(dom) < 100 else len(hello))
        exts = walk_extensions(hello)
        assert exts[0x0000][5:] == dom and 0x0015 in exts
        assert b"\x00\x1d\x00\x20" in exts[0x0033]
    codec = FakeTLSCodec(KEY.hex() + DOMAIN.hex(), legacy=True)
    hello = codec.client_hello()
    assert len(hello) == 517 and codec.session_id == hello[44:76] and codec.client_random == hello[11:43]


def test_explain_response_variants():
    from opsdigest.sources.faketls import HRR_RANDOM, describe_response, explain_response, response_shape
    codec = FakeTLSCodec(KEY.hex() + DOMAIN.hex())
    codec.client_hello()
    sid, cr = codec.session_id, codec.client_random

    good = explain_response(codec, server_response(sid, cr, KEY))
    assert good["signed_by"] == 1 and good["sid_echo"] and good["hello"]["group"] == 0x1D
    assert good["hello"]["version"] == "0304" and good["hello"]["extensions"] == [0x33, 0x2B]
    assert "подпись сошлась (как проверяет Telegram Desktop)" in describe_response(good)

    masked = explain_response(codec, server_response(sid, cr, None, data_records=(2900, 280, 53)))
    assert masked["signed_by"] == 0 and [t for t, _ in masked["records"]] == [0x16, 0x14, 0x17, 0x17, 0x17]
    text = "\n".join(describe_response(masked))
    assert "не сошлась ни по одной из 3" in text and "TLS 1.3" in text and "X25519" in text
    assert response_shape(masked) == response_shape(explain_response(codec, server_response(sid, cr, None)))

    more = explain_response(codec, server_response(sid, cr, KEY, tickets=(80,), sign_all=True))
    assert more["signed_by"] == 2 and "только по 2 записям" in "\n".join(describe_response(more))

    hrr = explain_response(codec, server_response(sid, cr, None, data_records=(), random=HRR_RANDOM))
    assert hrr["hrr"] and hrr["signed_by"] is None
    assert any("HelloRetryRequest" in l for l in describe_response(hrr))

    alert = explain_response(codec, b"\x15\x03\x03\x00\x02\x02\x70")
    assert alert["alert"] == 112 and "неизвестное имя" in "\n".join(describe_response(alert))
    http = explain_response(codec, b"HTTP/1.1 400 Bad Request\r\n\r\n")
    assert http["records"] == [] and "HTTP" in describe_response(http)[0]
    for info in (good, masked, more, hrr, alert, http):    # в описании нет ни ключа, ни домена
        joined = "\n".join(describe_response(info))
        assert KEY.hex() not in joined and DOMAIN.decode() not in joined


def test_base64_secret_with_plus_survives_link_parsing():
    import base64
    raw = b"\xee" + bytes.fromhex("0011fbefbe2233445566778899aabbcc") + b"example.com"
    b64 = base64.b64encode(raw).decode().rstrip("=")
    assert "+" in b64
    spec = parse_proxy(f"tg://proxy?server=h&port=443&secret={b64}")
    assert spec.secret == raw
