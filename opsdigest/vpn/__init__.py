"""VPN по подписке (VLESS, Trojan, VMess, Shadowsocks) для связи с Telegram — через ядро Xray.

Настройки — в config.json → telegram.vpn:
    {"enabled": true, "source": "https://… (подписка) или vless://… по строкам", "selected": "auto" | ключ сервера}
Список серверов и итоги проверки — в рабочей папке, data/vpn_servers.json (там ключи серверов — в Git не класть).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .. import logs
from . import xray
from .links import Server, VpnError, load_source

log = logs.get("vpn")

__all__ = ["VpnError", "settings", "refresh", "candidates", "cached", "xray"]


def settings(cfg) -> dict:
    return dict((cfg.telegram or {}).get("vpn") or {})


def _cache_file(cfg) -> Path:
    return cfg.path("data/vpn_servers.json")


def cached(cfg) -> dict:
    from ..secure import files
    try:
        data = json.loads(files.read_text(_cache_file(cfg)))
    except (OSError, ValueError):
        return {}
    for d in data.get("servers") or []:
        _remember_secrets(d.get("outbound") or {})
    return data


def _remember_secrets(ob: dict) -> None:
    st = ob.get("settings") or {}
    for t in (st.get("vnext") or st.get("servers") or []):
        logs.add_secrets([t.get("password")] + [u.get("id") for u in t.get("users") or []])


def _save(cfg, data: dict) -> None:
    from ..secure import files
    files.write_text(_cache_file(cfg), json.dumps(data, ensure_ascii=False, indent=1))


def refresh(cfg, progress=lambda s: None) -> dict:
    """Скачать подписку, запустить Xray со всеми серверами и проверить каждый: отвечает ли через него Telegram."""
    src = settings(cfg).get("source") or ""
    if not src.strip():
        raise VpnError("вставьте ссылку на подписку (https://…) или ссылки на серверы (vless://…)")
    progress("Загружаю подписку…")
    servers, info = load_source(src)
    for e in info.get("errors") or []:
        progress("⚠ " + e)
    if not servers:
        raise VpnError("; ".join(info.get("errors") or []) or "в подписке нет серверов")
    if len(servers) > xray.MAX_SERVERS:
        progress(f"Серверов {len(servers)} — проверю первые {xray.MAX_SERVERS}.")
    progress(f"Серверов: {len(servers)}. Запускаю Xray и проверяю каждый (до 20 секунд)…")
    ports = xray.RUNNER.start(servers)
    rows = xray.rank(asyncio.run(xray.test_servers(servers, ports, progress=progress)))
    info.pop("errors", None)
    _save(cfg, {"at": int(time.time()), "info": info, "servers": [s.to_json() for s in servers], "results": rows})
    ok = sum(1 for r in rows if r["tg_ok"])
    log.info("VPN: серверов %d, Telegram отвечает через %d", len(servers), ok)
    return {"servers": len(servers), "ok": ok, "rows": rows, "info": info}


def candidates(cfg, limit: int = 3) -> list:
    """Варианты подключения к Telegram через VPN (локальные SOCKS-входы Xray), лучшие первыми. Выключен — []."""
    from ..sources.tg_proxy import ProxySpec
    vs = settings(cfg)
    if not vs.get("enabled"):
        return []
    data = cached(cfg)
    servers = [Server.from_json(d) for d in data.get("servers") or []]
    if not servers:
        if not (vs.get("source") or "").strip():
            return []
        servers, _ = load_source(vs["source"])
        _save(cfg, {"at": int(time.time()), "servers": [s.to_json() for s in servers], "results": []})
    if not servers:
        raise VpnError("в подписке VPN нет серверов")
    res = {r["key"]: r for r in data.get("results") or []}
    sel = vs.get("selected") or "auto"

    def order(s: Server):
        r = res.get(s.key)
        manual = 0 if s.key == sel else 1
        state = 0 if r and r.get("tg_ok") else (1 if r is None else 2)
        return manual, state, (r or {}).get("tg_ms") or 10**9

    servers.sort(key=order)
    ports = xray.RUNNER.start(servers)
    picked = [s for s in servers if (res.get(s.key) or {}).get("tg_ok") or s.key == sel] or servers
    return [ProxySpec("socks5", "127.0.0.1", ports[s.key], raw=f"vpn:{s.key}", label=f"VPN «{s.name}»")
            for s in picked[:limit] if s.key in ports]
