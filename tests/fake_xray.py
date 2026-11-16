"""Поддельное ядро Xray для тестов: `version` и `run -c config.json`.

На каждый вход SOCKS5 из конфига поднимает сервер. Если адрес сервера в соответствующем outbound начинается
с «bad», отвечает отказом SOCKS; иначе сам изображает Telegram: на req_pq_multi отвечает ResPQ (TcpFull)."""
import asyncio
import json
import os
import struct
import sys
import zlib


def tcpfull(seq: int, data: bytes) -> bytes:
    head = struct.pack("<ii", len(data) + 12, seq)
    return head + data + struct.pack("<I", zlib.crc32(head + data))


async def fake_telegram(r, w):
    from telethon.extensions import BinaryReader
    from telethon.tl.types import ResPQ
    n = struct.unpack("<i", await r.readexactly(4))[0]
    rest = await r.readexactly(n - 4)
    body = rest[4:-4][20:]
    req = BinaryReader(body).tgread_object()
    res = bytes(ResPQ(req.nonce, 7, b"\x17\xed\x48\x94\x1a\x08\xf9\x81", [123]))
    w.write(tcpfull(0, struct.pack("<qqi", 0, 1 << 62, len(res)) + res))
    await w.drain()
    await asyncio.sleep(1)
    w.close()


def handler(bad: bool):
    async def h(r, w):
        try:
            ver, nm = await r.readexactly(2)
            assert ver == 5
            await r.readexactly(nm)
            w.write(b"\x05\x00")
            await w.drain()
            hdr = await r.readexactly(4)
            atyp = hdr[3]
            n = {1: 4, 4: 16}.get(atyp) or (await r.readexactly(1))[0]
            await r.readexactly(n + 2)
            if bad:
                w.write(b"\x05\x05\x00\x01" + bytes(6))
                await w.drain()
                w.close()
                return
            w.write(b"\x05\x00\x00\x01" + bytes(6))
            await w.drain()
            await fake_telegram(r, w)
        except (asyncio.IncompleteReadError, ConnectionError, AssertionError):
            w.close()
    return h


async def run(cfg_path: str) -> None:
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    outs = {o["tag"]: o for o in cfg["outbounds"]}
    route = {r["inboundTag"][0]: r["outboundTag"] for r in cfg["routing"]["rules"]}
    servers = []
    for ib in cfg["inbounds"]:
        ob = outs[route[ib["tag"]]]
        st = ob["settings"]
        addr = (st.get("vnext") or st.get("servers"))[0]["address"]
        servers.append(await asyncio.start_server(handler(addr.startswith("bad")), "127.0.0.1", ib["port"]))
    open(cfg["log"]["error"], "a").write("started\n")
    await asyncio.Event().wait()


if __name__ == "__main__":
    if sys.argv[1:2] == ["version"]:
        print("Xray 26.7.28 (Xray, Penetrates Everything.) fake")
        sys.exit(0)
    if sys.argv[1:4] == ["run", "-test", "-c"]:
        # как настоящий Xray 2026: allowInsecure удалён — конфиг с ним не загружается, ошибка называет тег
        cfg = json.load(open(sys.argv[4], encoding="utf-8"))
        for ob in cfg["outbounds"]:
            tls = (ob.get("streamSettings") or {}).get("tlsSettings") or {}
            st = ob.get("settings") or {}
            addr = ((st.get("vnext") or st.get("servers") or [{}])[0]).get("address", "")
            if "allowInsecure" in tls or addr.startswith("broken"):
                print("Xray 26.7.28 (Xray, Penetrates Everything.) fake")
                print(f"Failed to start: main: failed to load config files: [{sys.argv[4]}] > infra/conf: failed to "
                      f"build outbound config with tag {ob['tag']} > infra/conf: Failed to build TLS config. > "
                      "infra/conf: The feature \"allowInsecure\" has been removed and migrated to "
                      "\"pinnedPeerCertSha256\"(pcs) and \"verifyPeerCertByName\"(vcn).")
                sys.exit(23)
        print("Configuration OK.")
        sys.exit(0)
    if sys.argv[1:3] == ["run", "-c"]:
        if os.environ.get("FAKE_XRAY_FAIL"):
            print("Failed to start: infra/conf: invalid config")
            sys.exit(23)
        asyncio.run(run(sys.argv[3]))
