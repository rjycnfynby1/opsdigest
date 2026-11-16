"""WEB-прокси (tg://webproxy): разбор ссылок, capability по эталонным векторам и полный обмен через настоящий
Telethon с локальным relay, который ведёт себя как tproxy-server (PROTOCOL.md v1) во всех четырёх режимах канала."""
import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import socket
import threading

import pytest

from opsdigest.sources import webproxy as wp
from opsdigest.sources.tg_proxy import find_links, parse_proxy

HOST = "proxy.example.com"
SECRET = bytes.fromhex("dd000102030405060708090a0b0c0d0e0f")
BIG = 5 * 1024 * 1024 + 123          # больше начального окна 4 МиБ — без кадров WINDOW не дойдёт
UP = 300 * 1024


def test_capability_vectors():
    k = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    assert wp.bridge_capability(HOST, "", k) == "MHLEY5PmW1GWqJkSrlmJpvJUiLhBH_QKy6yKg8a0JPk"
    assert wp.bridge_capability(HOST, "", b"\xdd" + k) == "IpJrt3e7sKtzPyoXy6w-Zj6GGEvsvclN66JzQEfPYLA"
    assert wp.bridge_capability(HOST, "dobry-cola-super-app", k) == "hHz99Xs93EN1j91G9gpNepXwGNNt5YdAFkEVk_LlqdQ"
    assert wp.bridge_capability(HOST, "dobry-cola-super-app", b"\xdd" + k) == "TGUkZaevsavLbHvlNWipnRoYxgzZ51ioWvbxgGT3wHo"


def test_links():
    s = parse_proxy("tg://webproxy?server=Eivoo.Example.com&secret=dd" + "ab" * 16)
    assert (s.kind, s.mode, s.host, s.port, s.secret) == ("web", "web", "eivoo.example.com", 443, b"\xdd" + b"\xab" * 16)
    assert s.describe() == "web eivoo.example.com"
    s = parse_proxy("https://t.me/webproxy?server=proxy.example.com%2Fphcf2vfe7zgbrslg&secret=cIVhlEBk_HMMv6RHNWLY7Fk")
    assert s.host == "proxy.example.com/phcf2vfe7zgbrslg" and s.secret.hex() == "8561944064fc730cbfa4473562d8ec59"
    with pytest.raises(ValueError):
        parse_proxy("tg://webproxy?server=x.example.com&secret=ee" + "ab" * 16 + "6578616d706c652e636f6d")
    with pytest.raises(ValueError):
        parse_proxy("tg://webproxy?server=x.example.com:8443&secret=" + "ab" * 16)
    text = ("бот: tg://webproxy?server=a.example.com&secret=dd" + "11" * 16 + ", и ещё "
            "[tg://proxy?server=b.example.com&port=1443&secret=ee" + "22" * 16 + "62]")
    assert [l.split("?")[0] for l in find_links(text)] == ["tg://webproxy", "tg://proxy"]


def test_pick_proxy_and_egress():
    assert wp._pick_proxy("proxy.corp:3128") == "proxy.corp:3128"
    assert wp._pick_proxy("PROXY p.corp:8080; DIRECT") == "p.corp:8080"
    assert wp._pick_proxy("socks=s:1;https=p:1") == "p:1"
    assert wp.egress_for("off") is None
    assert wp.egress_for("p.corp:3128") == "http://p.corp:3128"


def test_connect_tunnel_and_407():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    seen = []

    def serve():
        for reply in (b"HTTP/1.1 200 Connection established\r\n\r\n",
                      b"HTTP/1.1 407 Proxy Auth\r\nProxy-Authenticate: NTLM\r\nProxy-Authenticate: Negotiate\r\n\r\n"):
            c, _ = srv.accept()
            seen.append(c.recv(4096))
            c.sendall(reply)
            if reply.startswith(b"HTTP/1.1 200"):
                c.sendall(c.recv(10))
            c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    s = wp._tunnel(f"http://user:pa%3Ass@127.0.0.1:{port}", HOST, 443, 5)
    s.sendall(b"ping")
    assert s.recv(10) == b"ping"
    s.close()
    assert seen[0].startswith(b"CONNECT proxy.example.com:443 HTTP/1.1")
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"user:pa:ss") in seen[0]
    with pytest.raises(wp.WebProxyError, match="NEGOTIATE, NTLM"):
        wp._tunnel(f"http://127.0.0.1:{port}", HOST, 443, 5)
    t.join(5)


# ====================================================================== локальный relay (как tproxy-server)
class Relay:
    def __init__(self, mode: str):
        self.mode = mode
        self.cap = wp.bridge_capability(HOST, "", SECRET)
        self.boot, self.token = "B" * 43, "S" * 43
        self.created = self.deleted = False
        self.lanes = collections.defaultdict(lambda: {"seq": 1, "cursor": 0, "q": [], "ev": asyncio.Event()})
        self.ws_out: asyncio.Queue | None = None
        self.stream = None
        self.seen = {"header": 0, "got": 0, "windows": 0, "frames": collections.Counter(), "pong": False}
        self.client_credit = wp.INITIAL_WINDOW
        self.credit_ev = asyncio.Event()

    # ---------- кадры от клиента
    def on_frames(self, data: bytes, lane=None):
        for kind, sid, payload in wp.parse_frames(data):
            self.seen["frames"][kind] += 1
            if lane is not None:
                assert sid == lane
            if kind == wp.OPEN:
                assert sid == 1 and self.stream is None
                self.stream = {"buf": bytearray()}
                asyncio.get_running_loop().create_task(self.backend())
            elif kind == wp.DATA:
                assert len(payload) <= wp.CHUNK
                self.stream["buf"] += payload
                self.seen["got"] += len(payload)
                self.send(wp.frame(wp.WINDOW, 1, len(payload).to_bytes(4, "big")), 1)
            elif kind == wp.WINDOW:
                self.seen["windows"] += 1
                self.client_credit += int.from_bytes(payload, "big")
                self.credit_ev.set()
            elif kind == wp.PONG:
                assert sid == 0 and payload == b"tok"
                self.seen["pong"] = True
            elif kind == wp.CLOSE:
                self.seen["closed"] = True

    def send(self, f: bytes, lane: int):
        if self.mode.startswith("websocket"):
            self.ws_out.put_nowait(f)
        else:
            key = lane if self.mode == "https-lanes" else None
            self.lanes[key]["q"].append(f)
            self.lanes[key]["ev"].set()

    async def backend(self):
        """Как MTProxy за relay: ждёт 64 байта заголовка, отвечает hello + BIG байт, потом bye после UP байт."""
        while self.seen["got"] < 64:
            await asyncio.sleep(0.01)
        self.seen["header"] = 64
        if self.mode == "https-lanes":
            self.send(wp.frame(wp.PING, 0, b"tok"), 0)
        elif self.mode == "https" or self.mode == "websocket":
            self.send(wp.frame(wp.PING, 0, b"tok"), 0)
        payload = b"hello" + bytes(i % 251 for i in range(BIG))
        for i in range(0, len(payload), wp.CHUNK):
            chunk = payload[i:i + wp.CHUNK]
            while self.client_credit < len(chunk):
                self.credit_ev.clear()
                await self.credit_ev.wait()
            self.client_credit -= len(chunk)
            self.send(wp.frame(wp.DATA, 1, chunk), 1)
        while self.seen["got"] < 64 + UP:
            await asyncio.sleep(0.01)
        self.send(wp.frame(wp.DATA, 1, b"bye"), 1)

    # ---------- HTTP
    async def handle(self, r, w):
        try:
            while True:
                line = await r.readuntil(b"\r\n")
                method, target, _ = line.decode().split(" ")
                h = {}
                while (l := await r.readuntil(b"\r\n")) != b"\r\n":
                    k, _, v = l.decode().partition(":")
                    h[k.strip().lower()] = v.strip()
                assert h["host"] == HOST and "cookie" not in h
                body = await r.readexactly(int(h["content-length"])) if "content-length" in h else b""
                if h.get("upgrade", "").lower() == "websocket":
                    await self.websocket(r, w, h)
                    return
                status, rh, rb = await self.route(method, target, h, body)
                head = f"HTTP/1.1 {status} X\r\n" + "".join(f"{k}: {v}\r\n" for k, v in rh.items())
                w.write((head + f"Content-Length: {len(rb)}\r\nDate: Mon, 02 Nov 2026 00:00:00 GMT\r\n\r\n").encode() + rb)
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()

    async def route(self, method, target, h, body):
        if method == "GET" and target == f"/?bridge={self.cap}":
            page = (f'<!doctype html><script nonce="x">(()=>{{\'use strict\';\nconst relayBase='
                    f'{json.dumps("https://" + HOST + "/")},bootstrap={json.dumps(self.boot)},'
                    f'carrierMode={json.dumps(self.mode)};\nconst x=1;}})()</script>')
            return 200, {"Content-Type": "text/html"}, page.encode()
        if method == "GET":
            return 200, {"Content-Type": "text/html"}, b"<html>just a site</html>"
        auth = h.get("authorization", "")
        if target == "/api/v1/session" and method == "POST":
            assert auth == "Bearer " + self.boot and h["content-type"] == "application/octet-stream"
            assert body == wp.frame(wp.HELLO, 0, b"\x01")
            self.created = True
            return 200, {"X-Session-Token": self.token, "X-Carrier-Mode": self.mode, "X-Down-Cursor": "0",
                         "Content-Type": "application/octet-stream"}, wp.frame(wp.WELCOME, 0)
        assert auth == "Bearer " + self.token
        if target == "/api/v1/session" and method == "DELETE":
            self.deleted = True
            return 204, {}, b""
        lane = int(h["x-lane-id"]) if self.mode == "https-lanes" else None
        st = self.lanes[lane]
        if target == "/api/v1/up":
            assert h["content-type"] == "application/octet-stream" and int(h["x-up-seq"]) == st["seq"]
            self.on_frames(body, lane)
            st["seq"] += 1
            return 204, {"X-Up-Ack": h["x-up-seq"]}, b""
        if target == "/api/v1/down":
            assert "content-type" not in h and not body and int(h["x-down-cursor"]) == st["cursor"]
            if not st["q"]:
                st["ev"].clear()
                try:
                    await asyncio.wait_for(st["ev"].wait(), 1)
                except asyncio.TimeoutError:
                    return 204, {"X-Down-Cursor": str(st["cursor"])}, b""
            out = []
            while st["q"] and len(out) < 40:
                out.append(st["q"].pop(0))
            st["cursor"] += 1
            return 200, {"X-Down-Cursor": str(st["cursor"]), "Content-Type": "application/octet-stream"}, b"".join(out)
        return 404, {}, b""

    async def websocket(self, r, w, h):
        lanes = self.mode == "websocket-lanes"
        proto = f"tproxy-lane-v1.{self.token}.1" if lanes else f"tproxy-v1.{self.token}"
        assert h["sec-websocket-protocol"] == proto and h["origin"] == "https://" + HOST
        acc = base64.b64encode(hashlib.sha1((h["sec-websocket-key"] + wp.WS_GUID).encode()).digest()).decode()
        w.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {acc}\r\nSec-WebSocket-Protocol: {proto}\r\n\r\n").encode())
        w.write(b"\x89\x02hi")                  # ping от сервера — клиент должен ответить pong
        await w.drain()
        self.ws_out = asyncio.Queue()

        async def pump():
            while True:
                f = await self.ws_out.get()
                batch = [f]
                while not self.ws_out.empty() and len(batch) < 30:
                    batch.append(self.ws_out.get_nowait())
                data = b"".join(batch)
                n = len(data)
                hd = b"\x82" + (bytes([n]) if n < 126 else (b"\x7e" + n.to_bytes(2, "big") if n < 65536
                                                            else b"\x7f" + n.to_bytes(8, "big")))
                w.write(hd + data)
                await w.drain()

        task = asyncio.get_running_loop().create_task(pump())
        try:
            while True:
                b0, b1 = await r.readexactly(2)
                assert b1 & 0x80, "клиент обязан маскировать кадры"
                n = b1 & 0x7F
                if n == 126:
                    n = int.from_bytes(await r.readexactly(2), "big")
                elif n == 127:
                    n = int.from_bytes(await r.readexactly(8), "big")
                mask = await r.readexactly(4)
                data = wp._mask(await r.readexactly(n), mask)
                op = b0 & 0x0F
                if op == 0xA:
                    self.seen["ws_pong"] = data
                elif op == 0x8:
                    break
                elif op == 0x2:
                    self.on_frames(data, 1 if lanes else None)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            task.cancel()
            w.close()


@pytest.mark.parametrize("mode", wp.CARRIERS)
def test_stream_through_relay_with_real_telethon(mode, monkeypatch):
    pytest.importorskip("telethon.network.connection.tcpmtproxy")
    from opsdigest.sources.tg_proxy import telethon_kwargs
    relay = Relay(mode)

    async def run():
        srv = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]

        async def plain(host, p, egress, timeout):
            assert host == HOST and p == 443 and egress is None
            return await asyncio.open_connection("127.0.0.1", port)

        monkeypatch.setattr(wp, "open_stream", plain)
        spec = parse_proxy(f"tg://webproxy?server={HOST}&secret={SECRET.hex()}")
        kw = telethon_kwargs(spec)
        assert kw["connection"].__name__ == "ConnectionTcpMTProxyWeb"
        loggers = collections.defaultdict(lambda: logging.getLogger("t"))
        conn = kw["connection"]("149.154.167.51", 443, 2, loggers=loggers, proxy=kw["proxy"])
        await asyncio.wait_for(conn._connect(timeout=5), 10)    # без фоновых циклов Telethon — читаем сами
        assert await asyncio.wait_for(conn._reader.readexactly(5), 10) == b"hello"
        got = await asyncio.wait_for(conn._reader.readexactly(BIG), 60)
        assert got == bytes(i % 251 for i in range(BIG))
        conn._writer.write(os.urandom(UP))
        await asyncio.wait_for(conn._writer.drain(), 20)
        assert await asyncio.wait_for(conn._reader.readexactly(3), 10) == b"bye"
        conn._writer.close()
        await asyncio.wait_for(conn._writer.wait_closed(), 10)
        srv.close()

    asyncio.run(run())
    s = relay.seen
    assert relay.created and relay.deleted
    assert s["header"] == 64 and s["got"] == 64 + UP
    assert s["windows"] >= (BIG - wp.INITIAL_WINDOW) // wp.WINDOW_STEP
    assert s["frames"][wp.OPEN] == 1
    if mode in ("https", "https-lanes", "websocket"):
        assert s["pong"]
    if mode.startswith("websocket"):
        assert s.get("ws_pong") == b"hi"


def test_wrong_secret_gets_public_site(monkeypatch):
    relay = Relay("https")

    async def run():
        srv = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(wp, "open_stream", lambda *a: asyncio.open_connection("127.0.0.1", port))
        st = wp.WebProxyStream(HOST, bytes(16), None, 5)
        with pytest.raises(wp.WebProxyError, match="не узнал секрет"):
            await st.open()
        srv.close()

    asyncio.run(run())
    assert not relay.created


def test_telemt_bridge_is_named(monkeypatch):
    relay = Relay("https")

    async def page(method, target, h, body):
        return 200, {"Content-Type": "text/html"}, (b"<script>(()=>{let bootstrap=\"x\";const relayOrigin='https://h'"
                                                    b";const r=globalThis.TelemtBridgeResponse;})()</script>")
    relay.route = page

    async def run():
        srv = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(wp, "open_stream", lambda *a: asyncio.open_connection("127.0.0.1", port))
        with pytest.raises(wp.WebProxyError, match="telemt"):
            await wp.WebProxyStream(HOST, SECRET, None, 5).open()
        srv.close()

    asyncio.run(run())
