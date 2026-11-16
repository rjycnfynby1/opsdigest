"""Подписки и ссылки VPN (как в Happ, v2rayN, Hiddify) → серверы для ядра Xray.

Понимает:
  • ссылку на подписку https://… — ответ: base64 со списком ссылок, просто список ссылок или JSON-конфиги Xray;
  • ссылки vless://, trojan://, vmess://, ss:// (по одной в строке, можно вперемешку с подпиской);
  • транспорты tcp (в т.ч. http-маскировка), ws, grpc, xhttp/splithttp, httpupgrade, kcp; защита none/tls/reality.
Не понимает: зашифрованные ссылки Happ (happ://crypt…) — их открывает только сам Happ; hysteria2, tuic, wireguard.

Секреты (UUID, пароли) в журнал не пишутся: каждый найденный секрет передаётся в logs.add_secrets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

from .. import logs

log = logs.get("vpn")

SUPPORTED = ("vless", "trojan", "vmess", "shadowsocks")
LINK_RE = re.compile(r"(?:vless|trojan|vmess|ss|hysteria2|hy2|tuic|wireguard|happ)://[^\s<>\"']+", re.I)
USER_AGENTS = ("v2rayN/7.15.4", "Happ/3.12.0", "Hiddify/2.5.7", "Mozilla/5.0")


class VpnError(ValueError):
    pass


# Что принимает текущий Xray (XTLS/Xray-core, 2026): допустимые «отпечатки» TLS и шифры Shadowsocks.
FINGERPRINTS = {
    "chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized", "randomizednoalpn",
    "unsafe", "hellofirefox_120", "hellofirefox_148", "hellochrome_120", "hellochrome_131", "hellochrome_133",
    "helloios_13", "helloios_14", "helloedge_106", "hellosafari_26_3", "hello360_11_0", "helloqq_11_1", "hellogolang",
    "hellorandomized", "hellorandomizedalpn", "hellorandomizednoalpn", "hellofirefox_auto", "hellofirefox_55",
    "hellofirefox_56", "hellofirefox_63", "hellofirefox_65", "hellofirefox_99", "hellofirefox_102", "hellofirefox_105",
    "hellochrome_auto", "hellochrome_58", "hellochrome_62", "hellochrome_70", "hellochrome_72", "hellochrome_83",
    "hellochrome_87", "hellochrome_96", "hellochrome_100", "hellochrome_102", "hellochrome_106_shuffle", "helloios_auto",
    "helloios_11_1", "helloios_12_1", "helloandroid_11_okhttp", "helloedge_85", "helloedge_auto"}
SS_METHODS = {"aes-128-gcm", "aead_aes_128_gcm", "aes-256-gcm", "aead_aes_256_gcm", "chacha20-poly1305",
              "aead_chacha20_poly1305", "chacha20-ietf-poly1305", "xchacha20-poly1305", "aead_xchacha20_poly1305",
              "xchacha20-ietf-poly1305", "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
              "2022-blake3-chacha20-poly1305"}
REALITY_NETS = ("tcp", "raw", "xhttp", "grpc")


def xray_compatible(outbound: dict) -> tuple[dict, str | None]:
    """Привести outbound к тому, что принимает текущий Xray. → (исправленная копия, причина «не подходит» или None).

    Xray с 2026 года не запускается вовсе, если хоть в одном outbound есть удалённые параметры: allowInsecure
    (заменён на pinnedPeerCertSha256 / verifyPeerCertByName), транспорты http(h2)/quic, flow у Trojan; REALITY — только
    с tcp/xhttp/grpc; неизвестный fingerprint — тоже ошибка. Без allowInsecure сервер с самоподписанным сертификатом
    просто не пройдёт проверку связи, а остальные серверы будут работать."""
    ob = json.loads(json.dumps(outbound))
    ss = ob.get("streamSettings") if isinstance(ob.get("streamSettings"), dict) else {}
    net = str(ss.get("network") or "tcp").lower()
    sec = str(ss.get("security") or "none").lower()
    if net in ("http", "h2", "quic"):
        return ob, f"транспорт {net} удалён из Xray"
    if sec == "reality" and net not in REALITY_NETS:
        return ob, f"REALITY в Xray работает только с tcp, xhttp и grpc (здесь {net})"
    for k in ("tlsSettings", "realitySettings"):
        t = ss.get(k)
        if not isinstance(t, dict):
            continue
        t.pop("allowInsecure", None)
        fp = str(t.get("fingerprint") or "").strip().lower()
        if fp in FINGERPRINTS:
            t["fingerprint"] = fp
        elif k == "realitySettings":
            t["fingerprint"] = "chrome"
        else:
            t.pop("fingerprint", None)
    st = ob.get("settings") if isinstance(ob.get("settings"), dict) else {}
    if ob.get("protocol") == "trojan":
        for srv in st.get("servers") or []:
            srv.pop("flow", None)
    if ob.get("protocol") == "shadowsocks":
        for srv in st.get("servers") or []:
            if str(srv.get("method") or "").lower() not in SS_METHODS:
                return ob, f"шифр Shadowsocks {srv.get('method')!r} Xray не поддерживает"
    return ob, None


@dataclass
class Server:
    name: str
    protocol: str            # vless | trojan | vmess | shadowsocks
    host: str
    port: int
    outbound: dict           # готовый outbound Xray (без tag)
    transport: str = "tcp"
    security: str = "none"
    key: str = ""            # стабильный идентификатор (хэш параметров) — для выбора сервера между запусками
    extra: dict = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.name} ({self.protocol}/{self.transport}{'/' + self.security if self.security != 'none' else ''})"

    def to_json(self) -> dict:
        return {"name": self.name, "protocol": self.protocol, "host": self.host, "port": self.port,
                "outbound": self.outbound, "transport": self.transport, "security": self.security, "key": self.key}

    @classmethod
    def from_json(cls, d: dict) -> "Server":
        return cls(d["name"], d["protocol"], d["host"], int(d["port"]), d["outbound"], d.get("transport", "tcp"),
                   d.get("security", "none"), d.get("key", ""))


def _key(outbound: dict) -> str:
    return hashlib.sha256(json.dumps(outbound, sort_keys=True).encode()).hexdigest()[:12]


def _b64(s: str) -> bytes:
    s = re.sub(r"\s+", "", s)
    return base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_") + "=" * (-len(s) % 4))


def _q(params: dict, *names: str, default: str = "") -> str:
    for n in names:
        if params.get(n):
            return unquote(params[n][0])
    return default


# ============================================================== транспорт и защита
def stream_settings(net: str, sec: str, p: dict) -> dict:
    """Общая часть outbound: streamSettings из параметров ссылки (имена как в v2rayN/Xray)."""
    net = (net or "tcp").lower()
    net = {"splithttp": "xhttp", "raw": "tcp"}.get(net, net)
    if net in ("http", "h2", "quic"):
        raise VpnError(f"транспорт {net} удалён из Xray — этот сервер программа использовать не может")
    sec = (sec or "none").lower()
    s: dict = {"network": net, "security": sec}
    host, path = _q(p, "host"), _q(p, "path")
    if net == "ws":
        ws: dict = {"path": path or "/"}
        if host:
            ws["host"] = host
        s["wsSettings"] = ws
    elif net == "grpc":
        g: dict = {"serviceName": _q(p, "serviceName", "path")}
        if _q(p, "mode") == "multi":
            g["multiMode"] = True
        if _q(p, "authority"):
            g["authority"] = _q(p, "authority")
        s["grpcSettings"] = g
    elif net == "xhttp":
        x: dict = {"path": path or "/"}
        if host:
            x["host"] = host
        if _q(p, "mode"):
            x["mode"] = _q(p, "mode")
        extra = _q(p, "extra")
        if extra:
            try:
                x["extra"] = json.loads(extra)
            except ValueError:
                pass
        s["xhttpSettings"] = x
    elif net == "httpupgrade":
        h: dict = {"path": path or "/"}
        if host:
            h["host"] = host
        s["httpupgradeSettings"] = h
    elif net == "kcp":
        k: dict = {}
        if _q(p, "headerType"):
            k["header"] = {"type": _q(p, "headerType")}
        if _q(p, "seed"):
            k["seed"] = _q(p, "seed")
        s["kcpSettings"] = k
    elif net == "http":
        hs: dict = {"path": path or "/"}
        if host:
            hs["host"] = host.split(",")
        s["httpSettings"] = hs
    elif net == "tcp" and _q(p, "headerType") == "http":
        req: dict = {"path": [path or "/"]}
        if host:
            req["headers"] = {"Host": host.split(",")}
        s["tcpSettings"] = {"header": {"type": "http", "request": req}}
    sni, fp, alpn = _q(p, "sni", "peer"), _q(p, "fp"), _q(p, "alpn")
    if sec == "tls":
        t: dict = {}
        if sni:
            t["serverName"] = sni
        if fp:
            t["fingerprint"] = fp
        if alpn:
            t["alpn"] = [a for a in alpn.split(",") if a]
        # allowInsecure в Xray удалён: вместо него — отпечаток сертификата (pcs) или имя для проверки (vcn)
        if _q(p, "pcs"):
            t["pinnedPeerCertSha256"] = _q(p, "pcs")
        if _q(p, "vcn"):
            t["verifyPeerCertByName"] = _q(p, "vcn")
        s["tlsSettings"] = t
    elif sec == "reality":
        pbk = _q(p, "pbk")
        if not pbk:
            raise VpnError("в ссылке Reality нет pbk (публичного ключа)")
        r: dict = {"publicKey": pbk, "fingerprint": fp or "chrome"}
        if sni:
            r["serverName"] = sni
        if _q(p, "sid"):
            r["shortId"] = _q(p, "sid")
        if _q(p, "spx"):
            r["spiderX"] = _q(p, "spx")
        if _q(p, "pqv"):
            r["mldsa65Verify"] = _q(p, "pqv")
        s["realitySettings"] = r
    elif sec not in ("none", ""):
        raise VpnError(f"незнакомая защита соединения: {sec}")
    return s


# ============================================================== ссылки
def parse_link(link: str) -> Server:
    link = link.strip()
    scheme = link.split("://", 1)[0].lower()
    if scheme == "happ":
        raise VpnError("это зашифрованная ссылка Happ (happ://…) — её открывает только сам Happ. Нужна обычная "
                       "ссылка на подписку https://… или ссылки vless://…")
    if scheme in ("hysteria2", "hy2", "tuic", "wireguard"):
        raise VpnError(f"протокол {scheme} программа пока не поддерживает")
    if scheme == "vmess":
        return _vmess(link)
    if scheme == "ss":
        return _ss(link)
    if scheme not in ("vless", "trojan"):
        raise VpnError("это не ссылка на сервер VPN")
    u = urlparse(link)
    p = parse_qs(u.query, keep_blank_values=True)
    name = unquote(u.fragment).split("?serverDescription=")[0].strip() or f"{u.hostname}:{u.port}"
    if not u.hostname or not u.username:
        raise VpnError("в ссылке нет адреса сервера или ключа")
    port = u.port or 443
    secret = unquote(u.username)
    logs.add_secrets([secret])
    net = _q(p, "type", default="tcp")
    sec = _q(p, "security", default="none" if scheme == "vless" else "tls")
    stream = stream_settings(net, sec, p)
    if scheme == "vless":
        user: dict = {"id": secret, "encryption": _q(p, "encryption", default="none")}
        if _q(p, "flow"):
            user["flow"] = _q(p, "flow")
        out = {"protocol": "vless", "settings": {"vnext": [{"address": u.hostname, "port": port, "users": [user]}]}}
    else:
        out = {"protocol": "trojan", "settings": {"servers": [{"address": u.hostname, "port": port, "password": secret}]}}
    out["streamSettings"] = stream
    out = _checked(out)
    return Server(name, out["protocol"], u.hostname, port, out, stream["network"], stream["security"], _key(out))


def _checked(outbound: dict) -> dict:
    ob, why = xray_compatible(outbound)
    if why:
        raise VpnError(why)
    return ob


def _vmess(link: str) -> Server:
    try:
        d = json.loads(_b64(link.split("://", 1)[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise VpnError("не удалось прочитать ссылку vmess://") from None
    host, port, uid = str(d.get("add") or ""), int(d.get("port") or 443), str(d.get("id") or "")
    if not host or not uid:
        raise VpnError("в ссылке vmess нет адреса или ключа")
    logs.add_secrets([uid])
    p = {k: [str(v)] for k, v in d.items() if v not in (None, "")}
    if d.get("type") and d.get("type") != "none":
        p["headerType"] = [str(d["type"])]
    stream = stream_settings(str(d.get("net") or "tcp"), "tls" if str(d.get("tls") or "") == "tls" else
                             str(d.get("tls") or "none") or "none", p)
    out = {"protocol": "vmess", "settings": {"vnext": [{"address": host, "port": port, "users": [
        {"id": uid, "alterId": int(d.get("aid") or 0), "security": str(d.get("scy") or "auto")}]}]},
        "streamSettings": stream}
    out = _checked(out)
    return Server(str(d.get("ps") or f"{host}:{port}"), "vmess", host, port, out, stream["network"], stream["security"],
                  _key(out))


def _ss(link: str) -> Server:
    body = link.split("://", 1)[1]
    body, _, frag = body.partition("#")
    body, _, query = body.partition("?")
    if query and "plugin=" in query:
        raise VpnError("Shadowsocks с плагином программа не поддерживает")
    if "@" in body:
        cred, _, hp = body.rpartition("@")
        try:
            cred = _b64(cred).decode() if ":" not in unquote(cred) else unquote(cred)
        except (ValueError, UnicodeDecodeError):
            cred = unquote(cred)
    else:
        try:
            cred, _, hp = _b64(body).decode().rpartition("@")
        except (ValueError, UnicodeDecodeError):
            raise VpnError("не удалось прочитать ссылку ss://") from None
    method, _, password = cred.partition(":")
    host, _, port = hp.rstrip("/").rpartition(":")
    host = host.strip("[]")
    if not (method and password and host and port.isdigit()):
        raise VpnError("в ссылке ss:// не хватает данных")
    logs.add_secrets([password])
    out = {"protocol": "shadowsocks", "settings": {"servers": [
        {"address": host, "port": int(port), "method": method, "password": password}]},
        "streamSettings": {"network": "tcp", "security": "none"}}
    out = _checked(out)
    return Server(unquote(frag) or f"{host}:{port}", "shadowsocks", host, int(port), out, "tcp", "none", _key(out))


# ============================================================== JSON-конфиги Xray (Remnawave/Marzban «xray-json»)
def _from_xray_json(data) -> list[Server]:
    configs = data if isinstance(data, list) else [data]
    out: list[Server] = []
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        title = str(cfg.get("remarks") or "")
        for ob in cfg.get("outbounds") or []:
            if not isinstance(ob, dict) or ob.get("protocol") not in SUPPORTED:
                continue
            ob = {k: v for k, v in ob.items() if k in ("protocol", "settings", "streamSettings", "mux")}
            ob, why = xray_compatible(ob)
            if why:
                continue
            st = ob.get("settings") or {}
            target = (st.get("vnext") or st.get("servers") or [st])[0] if isinstance(st, dict) else {}
            host, port = str(target.get("address") or ""), int(target.get("port") or 0)
            if not host or not port:
                continue
            for u in target.get("users") or []:
                logs.add_secrets([str(u.get("id") or "")])
            logs.add_secrets([str(target.get("password") or "")])
            ss = ob.get("streamSettings") or {}
            out.append(Server(title or f"{host}:{port}", ob["protocol"], host, port, ob, ss.get("network", "tcp"),
                              ss.get("security", "none"), _key(ob)))
    return out


# ============================================================== подписка целиком
def parse_text(text: str) -> tuple[list[Server], list[str]]:
    """Текст ответа подписки или вставленный пользователем → (серверы, ошибки по строкам)."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return [], []
    if text[:1] in "[{":
        try:
            return _from_xray_json(json.loads(text)), []
        except ValueError:
            pass
    if not LINK_RE.search(text):
        try:
            decoded = _b64(text).decode("utf-8")
            if LINK_RE.search(decoded) or decoded.strip()[:1] in "[{":
                return parse_text(decoded)
        except (ValueError, UnicodeDecodeError):
            pass
        if re.search(r"(?m)^proxies:", text):
            return [], ["подписка в формате Clash — нужен формат v2rayN/Xray (обычно другая ссылка у провайдера)"]
        return [], ["в ответе подписки нет ссылок на серверы"]
    servers, errors = [], []
    seen: set[str] = set()
    for m in LINK_RE.finditer(text):
        try:
            s = parse_link(m.group(0))
        except VpnError as e:
            errors.append(f"{m.group(0).split('://')[0]}: {e}")
            continue
        if s.key not in seen:
            seen.add(s.key)
            servers.append(s)
    return servers, errors


def fetch_subscription(url: str, timeout: float = 20) -> tuple[list[Server], dict]:
    """Скачать подписку (через прокси Windows, как браузер) и разобрать. → (серверы, сведения о подписке)."""
    last = ""
    for ua in USER_AGENTS:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — адрес задал пользователь
                body = r.read(8 * 1024 * 1024).decode("utf-8", "replace")
                headers = {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            last = f"сервер подписки ответил HTTP {e.code}"
            continue
        except Exception as e:  # noqa: BLE001
            raise VpnError(f"не удалось скачать подписку ({type(e).__name__}) — сайт подписки недоступен "
                           "с этого компьютера?") from None
        if body.lstrip().startswith("happ://"):
            last = "провайдер отдаёт только зашифрованную ссылку Happ (happ://crypt…) — её открывает только Happ"
            continue
        servers, errors = parse_text(body)
        if servers:
            return servers, _sub_info(headers) | ({"errors": errors} if errors else {})
        last = errors[0] if errors else "в ответе подписки нет серверов"
    raise VpnError(last or "подписка пустая")


def _sub_info(h: dict) -> dict:
    info: dict = {}
    title = h.get("profile-title", "")
    if title.startswith("base64:"):
        try:
            title = base64.b64decode(title[7:]).decode("utf-8", "replace")
        except ValueError:
            title = ""
    if title:
        info["title"] = title[:80]
    m = dict(re.findall(r"(\w+)=(\d+)", h.get("subscription-userinfo", "")))
    if m:
        used = int(m.get("upload", 0)) + int(m.get("download", 0))
        info["used_gb"] = round(used / 2**30, 1)
        if int(m.get("total", 0)):
            info["total_gb"] = round(int(m["total"]) / 2**30, 1)
        if int(m.get("expire", 0)):
            info["expire"] = int(m["expire"])
    return info


def load_source(source: str) -> tuple[list[Server], dict]:
    """Поле «подписка» из настроек: ссылки https:// на подписки и/или ссылки на серверы, по строкам."""
    servers: list[Server] = []
    info: dict = {"errors": []}
    lines = [l.strip() for l in (source or "").splitlines() if l.strip()]
    links = "\n".join(l for l in lines if not re.match(r"https?://", l, re.I))
    for url in [l for l in lines if re.match(r"https?://", l, re.I)]:
        try:
            got, meta = fetch_subscription(url)
            servers += got
            info["errors"] += meta.pop("errors", [])
            info.update(meta)
        except VpnError as e:
            info["errors"].append(str(e))
    got, errs = parse_text(links)
    servers += got
    info["errors"] += errs
    uniq: dict[str, Server] = {}
    for s in servers:
        uniq.setdefault(s.key, s)
    return list(uniq.values()), info
