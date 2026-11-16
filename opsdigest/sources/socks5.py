"""SOCKS5 для Telethon без внешних пакетов (python-socks на Python 3.10 требует async-timeout, которого нет
в комплекте). Используется для VPN по подписке (локальный вход Xray) и для ссылок socks5://."""
from __future__ import annotations

import asyncio
import ipaddress

from telethon.network.connection import ConnectionTcpFull

REPLIES = {1: "общая ошибка", 2: "запрещено правилами", 3: "сеть недоступна", 4: "узел недоступен",
           5: "соединение отклонено", 6: "истекло время", 7: "команда не поддерживается", 8: "тип адреса не поддерживается"}


async def socks5_open(proxy_host: str, proxy_port: int, host: str, port: int, timeout: float | None,
                      username: str | None = None, password: str | None = None):
    """Открыть TCP через SOCKS5 → (reader, writer) уже до цели."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(proxy_host, proxy_port), timeout)
    except asyncio.TimeoutError:
        raise ConnectionError("SOCKS5: прокси не ответил вовремя") from None
    except OSError as e:
        raise ConnectionError(f"SOCKS5: нет соединения с прокси ({type(e).__name__})") from None
    try:
        methods = b"\x00\x02" if username else b"\x00"
        w.write(b"\x05" + bytes([len(methods)]) + methods)
        await w.drain()
        ver, method = await asyncio.wait_for(r.readexactly(2), timeout)
        if ver != 5 or method == 0xFF:
            raise ConnectionError("SOCKS5: прокси не принял способ входа")
        if method == 2:
            u, p = (username or "").encode(), (password or "").encode()
            w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            await w.drain()
            if (await asyncio.wait_for(r.readexactly(2), timeout))[1] != 0:
                raise ConnectionError("SOCKS5: неверный логин или пароль")
        try:
            ip = ipaddress.ip_address(host)
            addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        except ValueError:
            h = host.encode("idna")
            addr = b"\x03" + bytes([len(h)]) + h
        w.write(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        await w.drain()
        head = await asyncio.wait_for(r.readexactly(4), timeout)
        if head[1] != 0:
            raise ConnectionError(f"SOCKS5: {REPLIES.get(head[1], 'отказ ' + str(head[1]))}")
        n = {1: 4, 4: 16}.get(head[3])
        if n is None:
            n = (await r.readexactly(1))[0]
        await r.readexactly(n + 2)
        return r, w
    except BaseException:
        w.close()
        raise


class ConnectionTcpFullSocks5(ConnectionTcpFull):
    """Обычное подключение Telethon (TcpFull), но через SOCKS5. proxy = (адрес, порт[, логин, пароль])."""

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        self._socks = tuple(proxy or ())
        super().__init__(ip, port, dc_id, loggers=loggers, proxy=None, local_addr=local_addr)

    async def _connect(self, timeout=None, ssl=None):
        host, port = self._socks[0], int(self._socks[1])
        user = self._socks[2] if len(self._socks) > 2 else None
        pwd = self._socks[3] if len(self._socks) > 3 else None
        self._reader, self._writer = await socks5_open(host, port, self._ip, self._port, timeout, user, pwd)
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()
