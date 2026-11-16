"""Пошаговая проверка связи: понятные строки вместо «TimeoutError»; поправка часов для FakeTLS."""
import asyncio
import socket

import pytest

from opsdigest import netcheck
from opsdigest.sources.tg_proxy import parse_proxy

pytest.importorskip("telethon.network.connection.tcpmtproxy")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_closed_port_is_explained():
    spec = parse_proxy(f"tg://proxy?server=127.0.0.1&port={free_port()}&secret=ee" + "11" * 16 + "676f6f676c652e636f6d")
    lines = asyncio.run(netcheck.probe(spec, timeout=2))
    assert lines and lines[-1].startswith("✗")
    assert "отклонил" in lines[-1] or "нет TCP-соединения" in lines[-1]   # Windows вместо отказа ждёт до тайм-аута


def test_faketls_steps_and_clock(monkeypatch):
    from test_faketls import LINK, fake_proxy
    from opsdigest.sources import faketls
    seen = {}

    async def run(offset):
        monkeypatch.setattr(faketls, "CLOCK_OFFSET", offset)
        srv = await asyncio.start_server(lambda r, w: fake_proxy(r, w, seen), "127.0.0.1", 0)
        spec = parse_proxy(LINK.format(port=srv.sockets[0].getsockname()[1]))
        lines = await netcheck.probe(spec, timeout=3)
        srv.close()
        return lines

    lines = asyncio.run(run(0.0))
    assert lines[0].startswith("✓ TCP") and lines[1] == "✓ FakeTLS: прокси узнал приветствие"
    assert seen["time_skew"] <= 2
    asyncio.run(run(-120.0))               # поправка часов уходит в приветствие
    assert 118 <= seen["time_skew"] <= 122


def test_clock_line():
    assert netcheck.clock_line(None).startswith("•")
    assert netcheck.clock_line(0.4).startswith("✓")
    assert "отстают на 42 с" in netcheck.clock_line(42.2)


# ============================================================== подробная проверка FakeTLS
def telemt_like(server_key: bytes, on_reject: str = "mask", accept_legacy_only: bool = False, log=None):
    """Эмулятор telemt/mtg: верный ключ — ответ FakeTLS с подписью; иначе — как настоящий сайт-маска
    (одинаковая «форма» ответа без подписи) или закрыть соединение."""
    import hashlib
    import hmac
    import os
    from test_faketls import server_response

    async def handler(reader, writer):
        try:
            hdr = await reader.readexactly(5)
            hello = hdr + await reader.readexactly(int.from_bytes(hdr[3:5], "big"))
        except asyncio.IncompleteReadError:
            writer.close()
            return
        calc = hmac.new(server_key, hello[:11] + b"\x00" * 32 + hello[43:], hashlib.sha256).digest()
        good = hello[11:39] == calc[:28] and (not accept_legacy_only or len(hello) == 517)
        if log is not None:
            log.append((len(hello), good))
        sid, cr = hello[44:76], hello[11:43]
        if good:
            writer.write(server_response(sid, cr, server_key))
        elif on_reject == "mask":
            writer.write(server_response(sid, cr, None, data_records=(2900, 280, 53)))
        else:
            writer.close()
            return
        await writer.drain()
        try:
            await asyncio.wait_for(reader.read(100), 3)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        writer.close()
    return handler


def run_probe(monkeypatch, handler, link_key: bytes, **kw):
    from test_faketls import DOMAIN
    monkeypatch.setattr(netcheck, "PAUSE", 0.05)

    async def run():
        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        spec = parse_proxy(f"tg://proxy?server=127.0.0.1&port={port}&secret=ee" + link_key.hex() + DOMAIN.hex())
        lines = await netcheck.probe(spec, timeout=3, **kw)
        srv.close()
        return lines
    return asyncio.run(run())


def test_wrong_key_is_recognised_as_mask(monkeypatch):
    from test_faketls import KEY
    log = []
    lines = run_probe(monkeypatch, telemt_like(KEY, log=log), bytes(16))
    text = "\n".join(lines)
    assert lines[1].startswith("✗ FakeTLS: ответ не прошёл проверку подписи")
    assert "подпись не сошлась ни по одной из 3" in text and "повторяет наш session_id" in text
    assert "контроль, заведомо неверный ключ: TLS-ответ без нашей подписи" in text
    assert "ответ такой же, как на основное приветствие" in text
    assert "не та ссылка, что работает в Telegram Desktop" in text and "«Поделиться»" in text
    assert [n > 1000 for n, _ in log] == [True, True, False]     # основное, контроль, старое приветствие
    assert KEY.hex() not in text and bytes(16).hex() not in text  # секретов в выводе нет


def test_right_key_still_short(monkeypatch):
    from test_faketls import KEY
    lines = run_probe(monkeypatch, telemt_like(KEY), KEY)
    assert lines[1] == "✓ FakeTLS: прокси узнал приветствие" and len(lines) == 2


def test_closed_for_any_key_blames_network(monkeypatch):
    from test_faketls import KEY
    lines = run_probe(monkeypatch, telemt_like(KEY, on_reject="close"), KEY[::-1])
    text = "\n".join(lines)
    assert lines[1].startswith("✗ FakeTLS: прокси закрыл соединение")
    assert "контроль, заведомо неверный ключ: соединение закрыто сразу" in text
    assert "дело не в ключе" in text


def test_only_legacy_hello_accepted_suggests_old(monkeypatch):
    from test_faketls import KEY
    lines = run_probe(monkeypatch, telemt_like(KEY, accept_legacy_only=True), KEY)
    text = "\n".join(lines)
    assert "старое (517 байт) приветствие с этим ключом: прокси узнал приветствие ✓" in text
    assert 'telegram.faketls_hello = "old"' in text
    lines = run_probe(monkeypatch, telemt_like(KEY, accept_legacy_only=True), KEY, legacy=True)
    assert lines[1] == "✓ FakeTLS: прокси узнал приветствие"


def test_hello_mode_from_config(monkeypatch, tmp_path):
    from opsdigest.sources import faketls
    monkeypatch.setattr(faketls, "HELLO_LEGACY", False)
    assert faketls.FakeTLSCodec("11" * 16 + "6161").legacy is False
    monkeypatch.setattr(faketls, "HELLO_LEGACY", True)
    assert faketls.FakeTLSCodec("11" * 16 + "6161").legacy is True
    assert len(faketls.FakeTLSCodec("11" * 16 + "6161").client_hello()) == 517
