"""Проверка связи с Telegram по шагам: имя → TCP → рукопожатие прокси → ответ Telegram; и точное время.

Нужна, чтобы вместо «тайм-аут» было видно, на каком шаге и почему не получилось.
Запуск для отладки (результат — без секретов):
    python -m opsdigest.netcheck                       — прокси из настроек рабочей папки
    python -m opsdigest.netcheck --links файл --telegram --out отчёт.txt
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from .sources.tg_proxy import ProxyError, ProxySpec, parse_proxy, telethon_kwargs

log = logging.getLogger("opsdigest.netcheck")

DC_TEST = ("149.154.167.51", 443, 2)    # DC2 — для проверки «Telegram отвечает»
TIME_URLS = ("http://www.msftconnecttest.com/connecttest.txt", "https://www.microsoft.com/",
             "https://www.google.com/generate_204", "https://ya.ru/")
_clock: dict = {}


# ============================================================== точное время
def clock_offset(extra_urls: list[str] | None = None, timeout: float = 4) -> tuple[float | None, str]:
    """(точное время − время компьютера, с; откуда взято) по заголовку Date. Не удалось — (None, '')."""
    for url in [*(extra_urls or []), *TIME_URLS]:
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — адреса из своего списка/настроек
                date = r.headers.get("Date")
        except urllib.error.HTTPError as e:
            date = e.headers.get("Date") if e.headers else None
        except Exception:  # noqa: BLE001
            continue
        t1 = time.time()
        if not date:
            continue
        try:
            server = parsedate_to_datetime(date).timestamp() + 0.5   # Date округлён вниз до секунды
        except (TypeError, ValueError, OverflowError):
            continue
        return server - (t0 + t1) / 2, urlparse(url).hostname or "?"
    return None, ""


def apply_clock(extra_urls: list[str] | None = None, *, force: bool = False) -> float | None:
    """Замерить расхождение часов (раз в 10 минут) и передать поправку FakeTLS, если она больше 1,5 с."""
    if not force and _clock and time.monotonic() - _clock["at"] < 600:
        return _clock["offset"]
    offset, src = clock_offset(extra_urls)
    _clock.update(at=time.monotonic(), offset=offset, src=src)
    try:
        from .sources import faketls
    except Exception:  # noqa: BLE001 — нет Telethon: поправлять нечего
        return offset
    if offset is not None and abs(offset) >= 1.5:
        faketls.CLOCK_OFFSET = offset
        log.warning("часы компьютера расходятся с точным временем на %.0f с — поправка для FakeTLS включена", offset)
    elif offset is not None:
        faketls.CLOCK_OFFSET = 0.0
    return offset


def clock_line(offset: float | None, src: str = "") -> str:
    if offset is None:
        return "• Точное время узнать не удалось (нет доступа к сайтам для сверки) — поправка часов не применяется."
    if abs(offset) < 1.5:
        return f"✓ Часы компьютера точные (расхождение {offset:+.1f} с)."
    side = "отстают" if offset > 0 else "спешат"
    return (f"⚠ Часы компьютера {side} на {abs(offset):.0f} с. Прокси FakeTLS (ee…) такое не пропускают — "
            f"программа подправит время сама; лучше включить синхронизацию времени в Windows.")


# ============================================================== шаги проверки
async def _tcp(host: str, port: int, timeout: float) -> tuple[bool, str, tuple | None]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout)
    except (socket.gaierror, asyncio.TimeoutError, OSError):
        return False, "✗ имя сервера не найдено (DNS) — сеть не знает этот адрес", None
    t0 = time.monotonic()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return False, (f"✗ нет TCP-соединения за {timeout:.0f} с — порт {port} закрыт в этой сети "
                       "или этой программе не разрешён выход в интернет"), None
    except ConnectionRefusedError:
        return False, f"✗ сервер отклонил соединение на порт {port}", None
    except OSError as e:
        return False, f"✗ не удалось подключиться ({type(e).__name__})", None
    return True, f"✓ TCP-соединение есть ({time.monotonic() - t0:.1f} с, адресов: {len(infos)})", (r, w)


PAUSE = 1.2   # между попытками к одному FakeTLS-прокси: telemt после 4 неудач подряд ~1 с не пускает этот адрес
SHARE_HINT = ("в Telegram Desktop: Настройки → Продвинутые настройки → Тип соединения → «⋮» у этого прокси → "
              "«Поделиться» (ссылка скопируется) — и вставьте её в программу заново")


def _hello_complete(buf: bytes) -> bool:
    """Ответ на приветствие уже можно разбирать: есть запись данных, отказ TLS или это вообще не TLS."""
    from .sources.faketls import tls_records
    if len(buf) >= 3 and (buf[0] not in (0x14, 0x15, 0x16, 0x17) or buf[1] != 3):
        return True
    recs, _ = tls_records(buf)
    return any(t == 0x17 for t, _, _ in recs) or bool(recs and recs[0][0] == 0x15)


async def _collect(r: asyncio.StreamReader, timeout: float, settle: float = 0.6,
                   limit: int = 65536) -> tuple[bytes, bool]:
    """Всё, что сервер прислал в ответ на приветствие: до тайм-аута, закрытия или ещё settle с после того,
    как ответ сложился (чтобы увидеть, сколько записей шлёт сервер). → (байты, соединение закрыто)."""
    loop = asyncio.get_running_loop()
    buf, eof = bytearray(), False
    deadline, settle_at = loop.time() + timeout, None
    while len(buf) < limit:
        end = deadline if settle_at is None else min(deadline, settle_at)
        left = end - loop.time()
        if left <= 0:
            break
        try:
            chunk = await asyncio.wait_for(r.read(limit - len(buf)), left)
        except asyncio.TimeoutError:
            break
        except (ConnectionError, OSError):
            eof = True
            break
        if not chunk:
            eof = True
            break
        buf += chunk
        if settle_at is None and _hello_complete(buf):
            settle_at = loop.time() + settle
    return bytes(buf), eof


async def _faketls_try(host: str, port: int, secret_hex: str, timeout: float, *, legacy: bool | None = None,
                       conn: tuple | None = None) -> dict:
    """Одна попытка рукопожатия FakeTLS. secret_hex — секрет без ee. → {status, info, codec, secs, line?}.

    status: ok | signed_more | bad_sig | closed | silent | alert | not_tls | partial | tcp."""
    from .sources.faketls import FakeTLSCodec, explain_response
    if conn is None:
        ok, line, conn = await _tcp(host, port, timeout)
        if not ok:
            return {"status": "tcp", "line": line, "info": {}, "codec": None, "secs": 0.0}
    r, w = conn
    codec = FakeTLSCodec(secret_hex, legacy=legacy)
    t0 = time.monotonic()
    try:
        w.write(codec.client_hello())
        await w.drain()
        data, eof = await _collect(r, timeout)
    except (ConnectionError, OSError):
        data, eof = b"", True
    finally:
        w.close()
    info = explain_response(codec, data)
    recs = info.get("records") or []
    if not data:
        status = "closed" if eof else "silent"
    elif not recs:
        status = "not_tls"
    elif recs[0][0] == 0x15:
        status = "alert"
    elif info.get("signed_by") == 1:
        status = "ok"
    elif info.get("signed_by"):
        status = "signed_more"
    elif info.get("signed_by") == 0:
        status = "bad_sig"
    else:
        status = "partial"
    return {"status": status, "info": info, "codec": codec, "secs": time.monotonic() - t0, "eof": eof}


_MAIN_LINES = {
    "ok": "✓ FakeTLS: прокси узнал приветствие",
    "signed_more": "✗ FakeTLS: прокси узнал приветствие, но подписал ответ иначе, чем проверяет программа — "
                   "нужна правка программы",
    "bad_sig": "✗ FakeTLS: ответ не прошёл проверку подписи — прокси не узнал приветствие и переслал соединение "
               "обычному сайту (неверный секрет, не та ссылка, или прокси не пускает с этого адреса)",
    "closed": "✗ FakeTLS: прокси закрыл соединение сразу после приветствия — не принял его. Причины: прокси "
              "пускает только из определённой сети (например, корпоративной), неверный секрет; или соединение "
              "рвёт провайдер",
    "silent": "✗ FakeTLS: прокси молчит в ответ на приветствие — так бывает, когда он его не узнал (часы компьютера, "
              "неверный секрет или домен) или ответ режет сеть",
    "alert": "✗ FakeTLS: прокси отказал на уровне TLS — не узнал домен из секрета или приветствие",
    "not_tls": "✗ FakeTLS: прокси ответил не как FakeTLS-сервер",
    "partial": "✗ FakeTLS: прокси ответил не до конца — не как FakeTLS-сервер",
}
_SHORT = {
    "ok": "прокси узнал приветствие ✓", "signed_more": "узнал, но подпись по нескольким записям",
    "bad_sig": "TLS-ответ без нашей подписи (так отвечает обычный сайт)", "closed": "соединение закрыто сразу",
    "silent": "молчание", "alert": "отказ TLS", "not_tls": "ответ не TLS", "partial": "неполный TLS-ответ",
    "tcp": "нет TCP-соединения",
}


def _attempt_lines(a: dict, host: str) -> list[str]:
    from .sources.faketls import HELLO_NAMES, describe_response
    c = a["codec"]
    try:
        dom = c.domain.decode("idna") if c else ""
    except UnicodeError:
        dom = c.domain.decode("latin-1")
    lines = [f"приветствие: {HELLO_NAMES[c.legacy]}, ключ …{c.key_mark()}, домен из секрета "
             + ("совпадает с адресом сервера" if dom.lower() == host.lower() else f"другой ({len(dom)} символов)")]
    if not a["info"].get("bytes"):
        lines.append(f"сервер закрыл соединение через {a['secs']:.1f} с, ничего не прислав" if a.get("eof")
                     else f"за {a['secs']:.0f} с не пришло ни байта")
        return lines
    lines += describe_response(a["info"])
    lines.append(f"ответ собран за {a['secs']:.1f} с" + (", затем сервер закрыл соединение" if a.get("eof") else ""))
    return lines


def _verdict(main: dict, ctrl: dict | None, alt: dict | None) -> list[str]:
    from .sources.faketls import HELLO_NAMES, response_shape
    ms = main["status"]
    if ms == "signed_more":
        return ["⇒ Нужна правка программы: прокси подписывает больше записей, чем она проверяет. Пришлите эти строки."]
    if alt and alt["status"] == "ok":
        name = HELLO_NAMES[alt["codec"].legacy]
        opt = "old" if alt["codec"].legacy else "new"
        return [f"⇒ Прокси принимает {name} приветствие, а основное — нет. В настройках Telegram выберите "
                f"приветствие «{opt}» (telegram.faketls_hello = \"{opt}\") и пришлите эти строки."]
    if not ctrl or ctrl["status"] == "tcp":
        return []
    same = ctrl["status"] == ms and response_shape(ctrl["info"]) == response_shape(main["info"])
    if ms == "bad_sig" and same:
        return ["⇒ На ключ из ссылки прокси отвечает так же, как на заведомо неверный: этот ключ он не знает и "
                "отдаёт соединение обычному сайту. Скорее всего, в программе не та ссылка, что работает в Telegram "
                "Desktop. Скопируйте её " + SHARE_HINT + "."]
    if ms in ("closed", "silent") and ctrl["status"] == ms:
        return ["⇒ Прокси одинаково не отвечает и на верный, и на неверный ключ — дело не в ключе: он не пускает "
                "с этого адреса (сети), или соединение рвёт провайдер."]
    if same:
        return ["⇒ На ключ из ссылки прокси отвечает так же, как на заведомо неверный — этот ключ он не знает. "
                "Скопируйте ссылку " + SHARE_HINT + "."]
    return ["⇒ На ключ из ссылки прокси отвечает иначе, чем на неверный: ключ он, видимо, знает, а не нравится "
            "что-то другое. Пришлите эти строки разработчику."]


async def _faketls(spec: ProxySpec, conn: tuple, timeout: float, *, detail: bool = True,
                   legacy: bool | None = None) -> tuple[bool, list[str]]:
    """Рукопожатие FakeTLS по шагам. При неудаче (detail=True) — подробности ответа и две контрольные попытки:
    с заведомо неверным ключом и с другим вариантом приветствия. Всё — без секретов."""
    secret_hex = spec.secret[1:].hex()
    main = await _faketls_try(spec.host, spec.port, secret_hex, timeout, legacy=legacy, conn=conn)
    lines = [_MAIN_LINES[main["status"]]]
    if main["status"] == "ok" or not detail:
        return main["status"] == "ok", lines
    lines += ["  · " + l for l in _attempt_lines(main, spec.host)]
    ctrl = alt = None
    if main["status"] != "signed_more":
        await asyncio.sleep(PAUSE)
        wrong = os.urandom(16).hex() + main["codec"].domain.hex()
        ctrl = await _faketls_try(spec.host, spec.port, wrong, timeout, legacy=main["codec"].legacy)
        lines.append("  · контроль, заведомо неверный ключ: " + _SHORT[ctrl["status"]] + (
            "" if ctrl["status"] == "tcp" else ", ответ такой же, как на основное приветствие"
            if _shape_eq(ctrl, main) else ", ответ другой" + _brief(ctrl)))
        await asyncio.sleep(PAUSE)
        alt = await _faketls_try(spec.host, spec.port, secret_hex, timeout, legacy=not main["codec"].legacy)
        from .sources.faketls import HELLO_NAMES
        lines.append(f"  · {HELLO_NAMES[not main['codec'].legacy]} приветствие с этим ключом: "
                     + _SHORT[alt["status"]] + ("" if alt["status"] == "ok" or _shape_eq(alt, main)
                                                else _brief(alt)))
    lines += ["  " + l for l in _verdict(main, ctrl, alt)]
    return False, lines


def _shape_eq(a: dict, b: dict) -> bool:
    from .sources.faketls import response_shape
    return a["status"] == b["status"] and response_shape(a["info"]) == response_shape(b["info"])


def _brief(a: dict) -> str:
    """Коротко о форме ответа: записи и поля ServerHello."""
    from .sources.faketls import describe_response
    if a["status"] in ("tcp", "closed", "silent", "ok"):
        return ""
    return " (" + "; ".join(describe_response(a["info"])[:2]) + ")"


async def _telegram(spec: ProxySpec, egress: str | None, timeout: float) -> tuple[bool, str]:
    """Обмен первым сообщением MTProto (req_pq) через этот вариант — без ключей API и без входа."""
    from telethon.network.mtprotoplainsender import MTProtoPlainSender
    from telethon.tl.functions import ReqPqMultiRequest
    from telethon.network.connection import ConnectionTcpFull

    class _Loggers(dict):
        def __missing__(self, key):
            return logging.getLogger(key)

    kw = telethon_kwargs(spec, egress)
    cls = kw.get("connection", ConnectionTcpFull)
    conn = cls(DC_TEST[0], DC_TEST[1], DC_TEST[2], loggers=_Loggers(), proxy=kw.get("proxy"))
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(conn.connect(timeout=timeout), timeout * 2 + 5)
        sender = MTProtoPlainSender(conn, loggers=_Loggers())
        nonce = int.from_bytes(os.urandom(16), "big", signed=True)
        res = await asyncio.wait_for(sender.send(ReqPqMultiRequest(nonce)), timeout + 5)
        if getattr(res, "nonce", None) != nonce:
            return False, "✗ Telegram ответил странно (не тот nonce)"
        return True, f"✓ Telegram ответил через этот вариант ({time.monotonic() - t0:.1f} с)"
    except asyncio.TimeoutError:
        return False, "✗ Telegram не ответил вовремя через этот вариант"
    except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
        return False, "✗ Telegram: " + (str(e) if str(e) else type(e).__name__)
    finally:
        try:
            await asyncio.wait_for(conn.disconnect(), 5)
        except Exception:  # noqa: BLE001
            pass


async def probe(spec: ProxySpec, egress: str | None = None, timeout: float = 8,
                telegram: bool = False, *, detail: bool = True, legacy: bool | None = None,
                after_failure: bool = False) -> list[str]:
    """Шаги проверки одного варианта подключения — список строк для человека (без секретов).

    detail — при неудаче FakeTLS разобрать ответ и сделать контрольные попытки; legacy — какое приветствие
    FakeTLS слать (None — как в настройках); after_failure — перед проверкой только что была неудачная
    попытка к этому же прокси (выдержать паузу, иначе telemt отклонит и эту)."""
    lines: list[str] = []
    if after_failure and spec.mode == "mtproto-faketls":
        await asyncio.sleep(PAUSE)
    try:
        if spec.kind == "web":
            from .sources.webproxy import WebProxyError, WebProxyStream, split_server
            host, _ = split_server(spec.host)
            lines.append(f"• выход в интернет: {'через прокси компьютера' if egress else 'напрямую'}")
            if not egress:
                ok, line, conn = await _tcp(host, 443, timeout)
                lines.append(line)
                if conn:
                    conn[1].close()
                if not ok:
                    return lines
            stream = WebProxyStream(spec.host, spec.secret, egress, timeout)
            try:
                await asyncio.wait_for(stream.open(), timeout * 3)
                lines.append(f"✓ WEB-прокси узнал секрет и открыл сеанс (канал {stream.mode})")
            except asyncio.TimeoutError:
                lines.append("✗ WEB-прокси не ответил вовремя")
                return lines
            except WebProxyError as e:
                lines.append("✗ " + str(e))
                return lines
            finally:
                await stream.aclose()
        elif spec.kind == "direct":
            ok, line, conn = await _tcp(DC_TEST[0], DC_TEST[1], timeout)
            lines.append(line.replace("✓ TCP-соединение есть", "✓ TCP-соединение с Telegram есть")
                         .replace("✗ нет TCP-соединения", "✗ нет TCP-соединения с Telegram"))
            if conn:
                conn[1].close()
            if not ok:
                return lines
        elif spec.kind == "mtproto":
            ok, line, conn = await _tcp(spec.host, spec.port, timeout)
            lines.append(line)
            if not ok:
                return lines
            try:
                if spec.mode == "mtproto-faketls":
                    ok, more = await _faketls(spec, conn, timeout, detail=detail, legacy=legacy)
                    lines.extend(more)
                    if not ok:
                        return lines
                    if telegram:
                        await asyncio.sleep(PAUSE)
            finally:
                conn[1].close()
        else:
            ok, line, conn = await _tcp(spec.host, spec.port, timeout)
            lines.append(line)
            if conn:
                conn[1].close()
            if not ok:
                return lines
        if telegram:
            ok, line = await _telegram(spec, egress, timeout)
            lines.append(line)
    except ProxyError as e:
        lines.append("✗ " + str(e))
    except Exception as e:  # noqa: BLE001 — проверка не должна падать
        log.warning("проверка %s: %s", spec.mode, type(e).__name__)
        lines.append(f"✗ ошибка проверки: {type(e).__name__}")
    return lines


def web_urls(specs: list[ProxySpec]) -> list[str]:
    from .sources.webproxy import split_server
    out = []
    for s in specs:
        if s.kind == "web":
            try:
                out.append(f"https://{split_server(s.host)[0]}/")
            except Exception:  # noqa: BLE001
                pass
    return out


# ============================================================== запуск из командной строки
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m opsdigest.netcheck")
    ap.add_argument("--links", help="файл со ссылками на прокси (по одной в строке); иначе — из настроек")
    ap.add_argument("--telegram", action="store_true", help="проверить ещё и ответ Telegram через каждый вариант")
    ap.add_argument("--direct", action="store_true", help="проверить и подключение без прокси")
    ap.add_argument("--egress", default="auto", help="выход в интернет для WEB-прокси: auto | off | http://адрес:порт")
    ap.add_argument("--out", help="записать результат в файл")
    ap.add_argument("--old-hello", action="store_true", help="FakeTLS: основное приветствие — старое (517 байт)")
    ap.add_argument("--quick", action="store_true", help="FakeTLS: без разбора ответа и контрольных попыток")
    a = ap.parse_args(argv)
    if a.links:
        raw = [l.strip() for l in Path(a.links).read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.startswith("#")]
    else:
        from .workspace import load_app_settings, read_config
        ws = (load_app_settings() or {}).get("workspace")
        raw = list(((read_config(Path(ws)) if ws else {}) .get("telegram") or {}).get("proxies") or [])
    specs: list[ProxySpec] = []
    out: list[str] = []
    for r in raw:
        try:
            specs.append(parse_proxy(r))
        except ProxyError as e:
            out.append(f"✗ ссылка не разобрана: {e}")
    if a.direct:
        specs.append(ProxySpec("direct", raw="direct"))
    from .sources.webproxy import egress_for
    egress = egress_for(a.egress)
    offset = apply_clock(web_urls(specs), force=True)
    out.append(clock_line(offset, _clock.get("src", "")))

    async def run() -> None:
        for s in specs:
            out.append(f"\n[{s.describe()}]")
            out.extend(await probe(s, egress, telegram=a.telegram, detail=not a.quick,
                                   legacy=True if a.old_hello else None))
    asyncio.run(run())
    text = "\n".join(out) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
