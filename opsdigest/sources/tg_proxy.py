"""Прокси для Telegram: разбор ссылок и параметры подключения Telethon.

Поддерживаются:
  tg://proxy?server=H&port=P&secret=S  и  https://t.me/proxy?...      — MTProxy
      секрет без префикса или с dd…  — встроенная поддержка Telethon
      секрет с ee… (FakeTLS)         — встроенная реализация (sources/faketls.py), внешние пакеты не нужны
  tg://webproxy?server=H&secret=S  и  https://t.me/webproxy?...       — WEB-прокси (HTTPS/WebSocket, порт 443;
      встроенная реализация в sources/webproxy.py)
  tg://socks?server=H&port=P&user=U&pass=W  и  https://t.me/socks?...  — SOCKS5
  socks5://user:pass@host:port — встроенная реализация (sources/socks5.py)
  socks4://host:port, http://user:pass@host:port — нужен пакет python-socks[asyncio]
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

LINK_RE = re.compile(r"(?:tg://(?:proxy|socks|webproxy)\?[^\s<>\"')\]]+|https?://t\.me/(?:proxy|socks|webproxy)\?[^\s<>\"')\]]+"
                     r"|socks[45]h?://[^\s<>\"')]+)", re.I)


class ProxyError(ValueError):
    pass


@dataclass
class ProxySpec:
    kind: str                # mtproto | web | socks5 | socks4 | http | direct
    host: str = ""
    port: int = 0
    secret: bytes = b""      # для mtproto: сырые байты секрета (с префиксом dd/ee, если был)
    username: str | None = None
    password: str | None = None
    raw: str = ""
    label: str = ""          # имя для человека (например, «VPN «Нидерланды»»)

    @property
    def mode(self) -> str:
        if self.kind != "mtproto":
            return self.kind
        if self.secret[:1] == b"\xee":
            return "mtproto-faketls"
        if self.secret[:1] == b"\xdd":
            return "mtproto-dd"
        return "mtproto"

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.kind == "direct":
            return "без прокси"
        if self.kind == "web":
            return f"web {self.host}"
        return f"{self.mode} {self.host}:{self.port}"


def _decode_secret(s: str) -> bytes:
    s = unquote(s.strip())
    if re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) % 2 == 0:
        return bytes.fromhex(s)
    try:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_") + pad)
    except (binascii.Error, ValueError):
        raise ProxyError(f"не удалось разобрать secret прокси: {s[:12]}…") from None


def parse_proxy(text: str) -> ProxySpec:
    s = (text or "").strip()
    if not s:
        raise ProxyError("пустая строка прокси")
    if s.lower() in ("direct", "none", "без прокси"):
        return ProxySpec("direct", raw=s)
    u = urlparse(s)
    scheme = u.scheme.lower()
    # «+» в ссылках Telegram — это «+», а не пробел (иначе секрет в base64 портится и ключ получается неверным)
    q = {k: v[0] for k, v in parse_qs(u.query.replace("+", "%2B")).items()}
    is_tme = scheme in ("http", "https") and u.netloc.lower() in ("t.me", "telegram.me")
    if (scheme == "tg" and u.netloc == "proxy") or (is_tme and u.path.strip("/") == "proxy"):
        if not {"server", "port", "secret"} <= q.keys():
            raise ProxyError("в ссылке MTProxy нужны server, port и secret")
        return ProxySpec("mtproto", q["server"], int(q["port"]), _decode_secret(q["secret"]), raw=s)
    if (scheme == "tg" and u.netloc == "webproxy") or (is_tme and u.path.strip("/") == "webproxy"):
        from .webproxy import WebProxyError, decode_secret, split_server
        if not {"server", "secret"} <= q.keys():
            raise ProxyError("в ссылке WEB-прокси нужны server и secret")
        try:
            host, path = split_server(q["server"])
            return ProxySpec("web", host + ("/" + path if path else ""), 443, decode_secret(q["secret"]), raw=s)
        except WebProxyError as e:
            raise ProxyError(str(e)) from None
    if (scheme == "tg" and u.netloc == "socks") or (is_tme and u.path.strip("/") == "socks"):
        return ProxySpec("socks5", q.get("server", ""), int(q.get("port", 1080)), username=q.get("user"),
                         password=q.get("pass"), raw=s)
    if scheme in ("socks5", "socks5h", "socks4", "http", "https"):
        if not u.hostname or not u.port:
            raise ProxyError(f"в адресе прокси нужен host:port — {s}")
        kind = {"socks5h": "socks5", "https": "http"}.get(scheme, scheme)
        return ProxySpec(kind, u.hostname, u.port, username=unquote(u.username) if u.username else None,
                         password=unquote(u.password) if u.password else None, raw=s)
    raise ProxyError(f"непонятный формат прокси: {s[:60]}")


def find_links(text: str) -> list[str]:
    """Все ссылки на прокси в произвольном тексте (например, в ответе бота)."""
    return list(dict.fromkeys(m.group(0).rstrip(".,;") for m in LINK_RE.finditer(text or "")))


def collect_proxies(tg_cfg: dict, cache_file: Path, remembered: str | None = None) -> list[ProxySpec]:
    """Список прокси в порядке попыток: последний рабочий, config/.env, кэш от бота, затем direct (если разрешено)."""
    raw: list[str] = []
    if remembered:
        raw.append(remembered)
    items = tg_cfg.get("proxies") or []
    if isinstance(items, str):
        items = [items]
    for item in items:
        raw.extend(p for p in re.split(r"[\s,]+", str(item)) if p)
    env = os.environ.get("TG_PROXY", "")
    raw.extend(p for p in re.split(r"[\s,]+", env) if p)
    if cache_file.exists():
        from ..secure import files
        raw.extend(l.strip() for l in files.read_text(cache_file).splitlines()
                   if l.strip() and not l.startswith("#"))
    specs: list[ProxySpec] = []
    seen: set[str] = set()
    for r in raw:
        if r in seen:
            continue
        seen.add(r)
        try:
            specs.append(parse_proxy(r))
        except ProxyError:
            continue
    if not specs or tg_cfg.get("try_direct"):
        if "direct" not in seen:
            specs.append(ProxySpec("direct", raw="direct"))
    return specs


def telethon_kwargs(spec: ProxySpec, egress: str | None = None) -> dict:
    """Аргументы connection/proxy для TelegramClient. Бросает ProxyError с понятной подсказкой.

    egress — HTTP-прокси для выхода в интернет (нужен только WEB-прокси; None — напрямую)."""
    if spec.kind == "direct":
        return {}
    if spec.kind == "web":
        from .webproxy import connection_class
        return {"connection": connection_class(spec.secret), "proxy": (spec.host, 443, spec.secret.hex(), egress)}
    if spec.kind == "mtproto":
        from telethon import connection  # noqa: WPS433
        if spec.mode == "mtproto-faketls":
            # встроенная поддержка FakeTLS (opsdigest/sources/faketls.py) — без внешних пакетов
            from .faketls import ConnectionTcpMTProxyFakeTLS
            return {"connection": ConnectionTcpMTProxyFakeTLS, "proxy": (spec.host, spec.port, spec.secret[1:].hex())}
        if spec.mode == "mtproto-dd":
            return {"connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
                    "proxy": (spec.host, spec.port, spec.secret.hex())}
        return {"connection": connection.ConnectionTcpMTProxyIntermediate,
                "proxy": (spec.host, spec.port, spec.secret.hex())}
    if spec.kind == "socks5":  # встроенный SOCKS5 — без python-socks (на Python 3.10 ему не хватает async-timeout)
        from .socks5 import ConnectionTcpFullSocks5
        return {"connection": ConnectionTcpFullSocks5,
                "proxy": (spec.host, spec.port, spec.username, spec.password)}
    try:
        import python_socks  # noqa: F401
    except ImportError:
        raise ProxyError("для SOCKS/HTTP-прокси нужен пакет python-socks: pip install \"python-socks[asyncio]\"") from None
    p = {"proxy_type": spec.kind, "addr": spec.host, "port": spec.port, "rdns": True}
    if spec.username:
        p["username"] = spec.username
    if spec.password:
        p["password"] = spec.password
    return {"proxy": p}
