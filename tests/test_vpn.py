"""VPN по подписке: разбор ссылок и подписок, конфиг Xray, запуск (поддельного) Xray и проверка серверов
настоящим Telethon через встроенный SOCKS5."""
import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

from opsdigest.vpn import links

UUID = "11111111-2222-3333-4444-555555555555"
REALITY = (f"vless://{UUID}@nl.example.com:443?type=tcp&security=reality&pbk=PUBKEY&fp=chrome&sni=www.microsoft.com"
           "&sid=ab12&spx=%2F&flow=xtls-rprx-vision&encryption=none#%F0%9F%87%B3%F0%9F%87%B1%20Netherlands")
XHTTP = (f"vless://{UUID}@de.example.com:8443?type=xhttp&security=tls&sni=de.example.com&path=%2Fx&host=cdn.example.com"
         "&mode=auto&alpn=h2,http/1.1&fp=chrome#Germany")
WS = f"trojan://pa55@fi.example.com:443?type=ws&path=%2Fws&host=fi.example.com&sni=fi.example.com#Finland"
VMESS = "vmess://" + base64.b64encode(json.dumps({"v": "2", "ps": "VM", "add": "vm.example.com", "port": "443", "id": UUID,
                                                  "aid": "0", "net": "grpc", "path": "svc", "tls": "tls",
                                                  "sni": "vm.example.com"}).encode()).decode()
SS = "ss://" + base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:sspass").decode().rstrip("=") + "@ss.example.com:8388#SS"


def test_vless_reality_to_xray():
    s = links.parse_link(REALITY)
    assert (s.name, s.protocol, s.host, s.port, s.transport, s.security) == (
        "🇳🇱 Netherlands", "vless", "nl.example.com", 443, "tcp", "reality")
    user = s.outbound["settings"]["vnext"][0]["users"][0]
    assert user == {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}
    r = s.outbound["streamSettings"]["realitySettings"]
    assert r == {"publicKey": "PUBKEY", "fingerprint": "chrome", "serverName": "www.microsoft.com", "shortId": "ab12",
                 "spiderX": "/"}


def test_other_transports():
    x = links.parse_link(XHTTP).outbound["streamSettings"]
    assert x["network"] == "xhttp" and x["xhttpSettings"] == {"path": "/x", "host": "cdn.example.com", "mode": "auto"}
    assert x["tlsSettings"] == {"serverName": "de.example.com", "fingerprint": "chrome", "alpn": ["h2", "http/1.1"]}
    t = links.parse_link(WS)
    assert t.protocol == "trojan" and t.outbound["settings"]["servers"][0]["password"] == "pa55"
    assert t.outbound["streamSettings"]["wsSettings"] == {"path": "/ws", "host": "fi.example.com"}
    v = links.parse_link(VMESS)
    assert v.protocol == "vmess" and v.outbound["streamSettings"]["grpcSettings"] == {"serviceName": "svc"}
    s = links.parse_link(SS)
    assert s.outbound["settings"]["servers"][0] == {"address": "ss.example.com", "port": 8388,
                                                    "method": "chacha20-ietf-poly1305", "password": "sspass"}


def test_subscription_formats_and_errors():
    body = base64.b64encode("\n".join([REALITY, XHTTP, WS, "hysteria2://x@h:1#H"]).encode()).decode()
    servers, errors = links.parse_text(body)
    assert [s.name for s in servers] == ["🇳🇱 Netherlands", "Germany", "Finland"]
    assert errors and "hysteria2" in errors[0]
    xj = [{"remarks": "JSON NL", "outbounds": [links.parse_link(REALITY).outbound | {"tag": "proxy"},
                                                {"protocol": "freedom", "tag": "direct"}]}]
    servers, _ = links.parse_text(json.dumps(xj))
    assert [s.name for s in servers] == ["JSON NL"] and servers[0].host == "nl.example.com"
    with pytest.raises(links.VpnError, match="Happ"):
        links.parse_link("happ://crypt3/AbCdEf")
    _, errs = links.parse_text("proxies:\n  - name: a\n")
    assert "Clash" in errs[0]
    from opsdigest import logs
    assert UUID not in logs.scrub(f"ключ {UUID} и пароль pa55 sspass")


def test_xray_config():
    from opsdigest.vpn.xray import build_config
    a, b = links.parse_link(REALITY), links.parse_link(WS)
    cfg = build_config([a, b], [10001, 10002], Path("x.log"))
    assert [i["port"] for i in cfg["inbounds"]] == [10001, 10002]
    assert all(i["listen"] == "127.0.0.1" and i["protocol"] == "socks" for i in cfg["inbounds"])
    assert cfg["routing"]["rules"][1] == {"type": "field", "inboundTag": ["in1"], "outboundTag": "s1"}
    assert cfg["outbounds"][0]["tag"] == "s0" and cfg["outbounds"][0]["protocol"] == "vless"


def test_xray_run_and_check_servers(tmp_path, monkeypatch):
    pytest.importorskip("telethon.network.mtprotoplainsender")
    import opsdigest.vpn as vpn
    from opsdigest.config import load_config
    from opsdigest.vpn import xray
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("OPSDIGEST_XRAY", str(Path(__file__).with_name("fake_xray.py")))
    good = REALITY
    bad = REALITY.replace("nl.example.com", "bad.example.com").replace("Netherlands", "Bad")
    assert xray.status() == {"installed": True, "version": "26.7.28"}
    cfgfile = tmp_path / "config.json"
    cfgfile.write_text(json.dumps({"telegram": {"vpn": {"enabled": True, "source": bad + "\n" + good}}}), encoding="utf-8")
    cfg = load_config(cfgfile)
    try:
        res = vpn.refresh(cfg)
        assert res["servers"] == 2 and res["ok"] == 1
        assert res["rows"][0]["name"] == "🇳🇱 Netherlands" and res["rows"][0]["tg_ok"]
        assert not res["rows"][1]["tg_ok"] and "SOCKS5" in res["rows"][1]["error"]
        cands = vpn.candidates(cfg)
        assert len(cands) == 1 and cands[0].describe() == "VPN «🇳🇱 Netherlands»" and cands[0].raw.startswith("vpn:")
        assert UUID not in (tmp_path / "local" / "OpsDigest" / "xray" / "xray.out").read_text(errors="replace")
    finally:
        xray.stop_all()
    assert not xray.RUNNER.running()


def test_xray_start_failure_is_explained(tmp_path, monkeypatch):
    from opsdigest.vpn import xray
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("OPSDIGEST_XRAY", str(Path(__file__).with_name("fake_xray.py")))
    monkeypatch.setenv("FAKE_XRAY_FAIL", "1")
    with pytest.raises(links.VpnError, match="Xray не запустился: infra/conf: invalid config"):
        xray.RUNNER.start([links.parse_link(REALITY)])


def test_install_from_zip(tmp_path, monkeypatch):
    import io
    import zipfile
    from opsdigest.vpn import xray
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("OPSDIGEST_XRAY", raising=False)
    monkeypatch.setattr(xray, "version", lambda: "26.7.28")
    monkeypatch.setattr(xray.time, "sleep", lambda s: None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xray.exe" if sys.platform == "win32" else "xray", b"binary")
        z.writestr("geoip.dat", b"x")
    (tmp_path / "x.zip").write_bytes(buf.getvalue())
    assert xray.install_from_zip(tmp_path / "x.zip") == "26.7.28"
    assert xray.exe_path().read_bytes() == b"binary"
    with pytest.raises(links.VpnError, match="не zip"):
        (tmp_path / "bad.zip").write_bytes(b"nope")
        xray.install_from_zip(tmp_path / "bad.zip")


def test_install_from_github_with_checksum(tmp_path, monkeypatch):
    import hashlib
    import io
    import zipfile
    from opsdigest.vpn import xray
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("OPSDIGEST_XRAY", raising=False)
    monkeypatch.setattr(xray, "version", lambda: "26.7.28")
    monkeypatch.setattr(xray.time, "sleep", lambda s: None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xray.exe" if sys.platform == "win32" else "xray", b"bin")
    zipdata = buf.getvalue()
    name = xray.asset_name()
    good = {xray.LATEST_DOWNLOAD + name: zipdata,
            xray.LATEST_DOWNLOAD + name + ".dgst": f"MD5= x\nSHA2-256= {hashlib.sha256(zipdata).hexdigest()}\n".encode()}
    seen = []

    def fake_get(url, timeout=60, accept="*/*"):
        seen.append((url, accept))
        return good[url]
    monkeypatch.setattr(xray, "_get", fake_get)
    assert xray.install_from_github() == "26.7.28"
    good[xray.LATEST_DOWNLOAD + name + ".dgst"] = b"SHA2-256= " + b"0" * 64
    with pytest.raises(links.VpnError, match="контрольная сумма"):
        xray.install_from_github()

    def offline(url, timeout=60, accept="*/*"):
        raise OSError("нет сети")
    monkeypatch.setattr(xray, "_get", offline)
    with pytest.raises(links.VpnError, match="вручную"):
        xray.install_from_github()


# ============================================================== совместимость с Xray 2026
def test_links_are_made_compatible_with_current_xray():
    s = links.parse_link(XHTTP.replace("#", "&allowInsecure=1&vcn=de.example.com#")).outbound["streamSettings"]["tlsSettings"]
    assert "allowInsecure" not in s and s["verifyPeerCertByName"] == "de.example.com"
    t = links.parse_link(WS.replace("#", "&fp=Chrome&insecure=1#")).outbound["streamSettings"]["tlsSettings"]
    assert t["fingerprint"] == "chrome" and "allowInsecure" not in t
    assert "fingerprint" not in links.parse_link(WS.replace("#", "&fp=qwerty#")).outbound["streamSettings"]["tlsSettings"]
    r = links.parse_link(REALITY.replace("fp=chrome", "fp=nope")).outbound["streamSettings"]["realitySettings"]
    assert r["fingerprint"] == "chrome"
    for bad, why in ((XHTTP.replace("type=xhttp", "type=h2"), "удалён"),
                     (XHTTP.replace("type=xhttp", "type=quic"), "удалён"),
                     (REALITY.replace("type=tcp", "type=ws"), "REALITY"),
                     ("ss://" + base64.urlsafe_b64encode(b"rc4-md5:p").decode().rstrip("=") + "@h.example.com:1#X",
                      "шифр")):
        with pytest.raises(links.VpnError, match=why):
            links.parse_link(bad)
    # подписка с одним «устаревшим» сервером: остальные разбираются, ошибка — по строке
    servers, errors = links.parse_text("\n".join([REALITY, XHTTP.replace("type=xhttp", "type=h2")]))
    assert [x.name for x in servers] == ["🇳🇱 Netherlands"] and "удалён" in errors[0]
    # xray-json: allowInsecure и flow у Trojan вычищаются
    ob = links.parse_link(WS).outbound
    ob["streamSettings"]["tlsSettings"]["allowInsecure"] = True
    ob["settings"]["servers"][0]["flow"] = "xtls-rprx-direct"
    got, _ = links.parse_text(json.dumps({"remarks": "J", "outbounds": [ob]}))
    assert "allowInsecure" not in got[0].outbound["streamSettings"]["tlsSettings"]
    assert "flow" not in got[0].outbound["settings"]["servers"][0]


def test_reason_extracts_the_cause():
    from opsdigest.vpn.xray import _reason
    msg = ("A unified platform for anti-censorship. | 2026/11/02 12:15:54 [Info] infra/conf/serial: Reading config | "
           "Failed to start: main: failed to load config files: [OpsDigest\\xray\\run.json] > infra/conf: failed to "
           "build outbound config with tag s0 > infra/conf: Failed to build TLS config. > infra/conf: The feature "
           "\"allowInsecure\" has been removed and migrated to \"pinnedPeerCertSha256\"(pcs).")
    r = _reason(msg)
    assert r.startswith("infra/conf: The feature \"allowInsecure\" has been removed") and "tag s0" in r


def test_one_bad_server_does_not_break_xray(tmp_path, monkeypatch):
    pytest.importorskip("telethon.network.mtprotoplainsender")
    import opsdigest.vpn as vpn
    from opsdigest.config import load_config
    from opsdigest.vpn import xray
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("OPSDIGEST_XRAY", str(Path(__file__).with_name("fake_xray.py")))
    old = links.parse_link(REALITY)                       # как в старом кэше data/vpn_servers.json
    old.outbound["streamSettings"]["tlsSettings"] = {"allowInsecure": True}
    broken = links.parse_link(REALITY.replace("nl.example.com", "broken.example.com").replace("Netherlands", "Broken"))
    try:
        ports = xray.RUNNER.start([broken, old])
        assert list(ports) == [old.key] and broken.key in xray.RUNNER.skipped
        assert "allowInsecure" in xray.RUNNER.skipped[broken.key]
        run = json.loads((tmp_path / "local" / "OpsDigest" / "xray" / "run.json").read_text(encoding="utf-8"))
        assert "allowInsecure" not in json.dumps(run)
    finally:
        xray.stop_all()
    cfgfile = tmp_path / "config.json"
    src = REALITY.replace("nl.example.com", "broken.example.com").replace("Netherlands", "Broken") + "\n" + REALITY
    cfgfile.write_text(json.dumps({"telegram": {"vpn": {"enabled": True, "source": src}}}), encoding="utf-8")
    try:
        res = vpn.refresh(load_config(cfgfile))
        assert res["servers"] == 2 and res["ok"] == 1
        assert res["rows"][1]["name"].endswith("Broken") and res["rows"][1]["error"].startswith("Xray не принял сервер")
    finally:
        xray.stop_all()
    with pytest.raises(links.VpnError, match="ни одного сервера"):
        try:
            xray.RUNNER.start([broken])
        finally:
            xray.stop_all()
