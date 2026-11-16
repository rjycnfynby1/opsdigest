"""Командная строка: python -m opsdigest <команда>.

  check          проверить прокси → Telegram → LLM (начните с неё)
  login          первый вход в Telegram (код из приложения)
  chats          список чатов и назначенные им приоритеты (для настройки config.toml)
  sync           забрать новые сообщения в локальную базу
  digest         сводка за день/неделю/месяц/с даты → HTML-отчёт (+ в «Избранное» с --send)
  ask            вопрос по переписке («что решили по INC0012345?»)
  export         выгрузить всю историю всех чатов (или --chat, --since) с изображениями и лёгкими файлами
  images         прочитать (описать) изображения чата: --chat ID [--since ДАТА]
  fetch          докачать файл-ссылку по номеру (m123)
  vision-check   проверить, видит ли модель изображения (для Maverick — найти формат передачи)
  demo           сводка по встроенным примерам без Telegram (проверить модель и вид отчёта)
  proxy-refresh  (эксперимент) попросить прокси у корпоративного бота
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import load_config
from .storage import Store, iso


def _out(s: str = "") -> None:
    print(s, flush=True)


def _setup_console() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _parse_dt(s: str | None, tz) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise SystemExit(f"Не понял дату: {s} (пример: 2026-10-20 или 20.10.2026 18:00)")


def _llm(cfg, name: str | None):
    from .llm import make_client
    pname, profile = cfg.llm_profile(name)
    return make_client(pname, profile)


def _store(cfg) -> Store:
    return Store(cfg.path(cfg["storage"]["path"]))


# ============================================================================ команды
def cmd_check(cfg, args) -> int:
    from .sources.telegram import TelegramError, TelegramSource
    ok_all = True
    _out(f"OpsDigest {__version__} · папка: {cfg.base_dir}")
    if not args.no_telegram:
        _out("\n[1] Прокси и связь с Telegram")
        store = _store(cfg)
        src = TelegramSource(cfg, store, progress=_out)

        async def run():
            try:
                import telethon  # noqa: F401
            except ImportError:
                _out("  ✗ не установлен пакет telethon — запустите OpsDigest.bat")
                return False
            try:
                src._client_args()
            except TelegramError as e:
                _out(f"  ✗ {e}")
                return False
            specs = src.proxy_candidates()
            _out("  Варианты подключения: " + ", ".join(s.describe() for s in specs))
            try:
                res = await src.check_proxies()
            except TelegramError as e:
                _out(f"  ✗ {e}")
                return False
            good = None
            for spec, ok, err, sec in res:
                _out(f"  {'✓' if ok else '✗'} {spec.describe():40} {sec:5.1f} с  {err}")
                if ok and good is None:
                    good = spec
            if not good:
                _out("  Ни один вариант не подключился. Проверьте прокси в .env (TG_PROXY) или config.toml.")
                return False
            _out("\n[2] Авторизация")
            try:
                await src.connect(interactive=False)
                me = json.loads(store.kv_get("me") or "{}")
                _out(f"  ✓ вход выполнен: {me.get('name')} (@{me.get('username')})")
                n = 0
                async for _ in src.client.iter_dialogs(limit=50):
                    n += 1
                _out(f"  ✓ чтение диалогов работает (первые {n})")
                return True
            except TelegramError as e:
                _out(f"  ✗ {e}")
                return False
            finally:
                await src.close()

        ok_all &= asyncio.run(run())
    if not args.no_llm:
        _out("\n[3] LLM")
        try:
            llm = _llm(cfg, args.provider)
            t0 = datetime.now()
            ans = llm.complete("Отвечай одним словом.", "Ответь словом: готов", max_tokens=20)
            _out(f"  ✓ {llm.label}: «{ans.strip()[:60]}» за {(datetime.now() - t0).total_seconds():.1f} с")
        except Exception as e:  # noqa: BLE001
            _out(f"  ✗ {e}")
            ok_all = False
    _out("\nИтог: " + ("всё работает" if ok_all else "есть проблемы — см. выше"))
    return 0 if ok_all else 1


def cmd_login(cfg, args) -> int:
    from .sources.telegram import TelegramSource
    store = _store(cfg)
    src = TelegramSource(cfg, store, progress=_out)

    async def run():
        await src.connect(interactive=True)
        me = json.loads(store.kv_get("me") or "{}")
        _out(f"Готово: вошли как {me.get('name')} (@{me.get('username')}). Сессия: {cfg.telegram.get('session')}.session")
        _out("Эта сессия видна в Telegram: Настройки → Устройства (OpsDigest). Файл сессии никому не передавайте.")
        await src.close()

    asyncio.run(run())
    return 0


def cmd_chats(cfg, args) -> int:
    from .sources.telegram import TelegramSource
    store = _store(cfg)
    src = TelegramSource(cfg, store, progress=_out)

    async def run():
        await src.connect()
        try:
            return await src.with_failover(src.list_dialogs)
        finally:
            await src.close()

    rows = asyncio.run(run())
    path = cfg.path("data/chats.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("id;title;kind;username;priority;mode;muted;unread;last\n")
        for r in rows:
            f.write(";".join(str(r[k]).replace(";", ",") for k in
                             ("id", "title", "kind", "username", "priority", "mode", "muted", "unread", "last")) + "\n")
    for r in rows[: args.limit]:
        mark = "*" if r["explicit"] else " "
        _out(f"{mark}{r['priority']:7} {r['mode']:8} {r['kind']:8} {r['title'][:50]:50} {r['id']}")
    _out(f"\nВсего: {len(rows)}. Полный список: {path} (* — задано правилом в config.toml)")
    return 0


async def _sync(cfg, store, since: datetime) -> dict:
    from .service import tg_sync
    return await tg_sync(cfg, store, since, _out)


def cmd_sync(cfg, args) -> int:
    from .digest import get_tz
    tz = get_tz(cfg.digest.get("timezone"))
    since = _parse_dt(args.since, tz) or (datetime.now(tz) - timedelta(days=args.days or cfg.telegram["first_sync_days"]))
    asyncio.run(_sync(cfg, _store(cfg), since))
    return 0


def _write_report(cfg, data: dict, store: Store | None, open_it: bool) -> Path:
    from .service import write_report
    html_path = Path(write_report(cfg, data, store)["html"])
    _out(f"\nОтчёт: {html_path}")
    if open_it:
        _open(html_path)
    return html_path


def _open(path: Path) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.as_uri())
    except Exception:  # noqa: BLE001
        pass


def cmd_digest(cfg, args) -> int:
    from .digest import get_tz
    from .service import run_digest
    tz = get_tz(cfg.digest.get("timezone"))
    res = run_digest(cfg, period=args.period, since=_parse_dt(args.since, tz), until=_parse_dt(args.until, tz),
                     send=bool(args.send or cfg.digest.get("send_to_saved")), sync=not args.no_sync,
                     provider=args.provider, progress=_out)
    _out(f"\nОтчёт: {res['paths']['html']}")
    _out(f"Главное: {res['headline'][:400]}")
    if args.open:
        _open(Path(res["paths"]["html"]))
    return 0


def cmd_source(cfg, args) -> int:
    """Сводка «От источника»: очередь ITSM группы (или задачи трекера) → связанное → Telegram и почта по номерам."""
    from .service import run_source_digest
    query = {"group": args.group or "", "state": args.state, "text": args.text or "", "limit": args.limit,
             "types": [t.strip() for t in (args.types or "incident").split(",") if t.strip()]}
    look = {"telegram": not args.no_telegram, "mail": not args.no_mail, "rooms": args.rooms, "meet": args.meet,
            "second": not args.no_second, "days": args.days}
    res = run_source_digest(cfg, start=args.start, query=query, look=look, extra=args.extra or "",
                            send=bool(args.send), sync=not args.no_sync, provider=args.provider, progress=_out)
    s = res["source"]
    _out(f"\nОтчёт: {res['paths']['html']}")
    _out(f"Записей: {s['items']}, связанных: {s['secondary']}, групп: {s['groups']}, требуют внимания: {s['attention']}")
    _out(f"Главное: {res['headline'][:400]}")
    if args.open:
        _open(Path(res["paths"]["html"]))
    return 0


def cmd_ask(cfg, args) -> int:
    from .service import run_ask
    r = run_ask(cfg, " ".join(args.question), days=args.days, sync=not args.no_sync, provider=args.provider,
                progress=_out)
    _out("\n" + r["answer"])
    for x in r["images"]:
        _out(f"  изображение m{x['id']} ({x['chat']}, #{x['msg_id']}): {x['summary']}")
    for f in r["need_files"]:
        _out(f"  модели нужен файл m{f['id']}: {f['name']}, {f['size']} — python -m opsdigest fetch m{f['id']}")
    return 0


def cmd_export(cfg, args) -> int:
    from .digest import get_tz
    from .service import run_export
    tz = get_tz(cfg.digest.get("timezone"))
    st = run_export(cfg, chat_ids={int(x) for x in args.chat} if args.chat else None, since=_parse_dt(args.since, tz),
                    with_media=not args.no_media, progress=_out)
    _out(f"Изображений в базе: {st['images_total']}, описано: {st['images_described']}, файлов-ссылок: {st['refs']}")
    return 0


def cmd_images(cfg, args) -> int:
    from .digest import get_tz
    from .service import run_images
    tz = get_tz(cfg.digest.get("timezone"))
    run_images(cfg, int(args.chat), since=_parse_dt(args.since, tz), download=not args.no_download,
               provider=args.provider, progress=_out)
    return 0


def cmd_fetch(cfg, args) -> int:
    from .service import run_fetch
    r = run_fetch(cfg, int(str(args.id).lstrip("mM")), progress=_out)
    _out(f"Файл: {cfg.path(r['path'])}")
    if r.get("summary"):
        _out(f"Описание: {r['summary']}")
    return 0


def cmd_vision_check(cfg, args) -> int:
    from .vision import check, make_vision_llm, remember_discovery, save_report
    llm = make_vision_llm(cfg, args.provider)
    llm.retries = 0
    if args.rediscover:
        llm.profile.pop("image_mode", None)
    _out(f"Модель для изображений: {llm.label}")
    try:
        r = check(llm, progress=_out)
    finally:
        rp = save_report(cfg, llm)
        if rp:
            _out(f"Журнал подбора формата: {rp}")
    remember_discovery(cfg, llm)
    _out(("✓ видит изображения" if r["ok"] else "✗ не видит изображение") +
         f": ответ «{r['answer']}», на картинке {r['expected']} ({r['seconds']} с)")
    if r.get("mode"):
        _out(f"Формат передачи: {json.dumps(r['mode'], ensure_ascii=False)}")
    return 0 if r["ok"] else 1


def cmd_demo(cfg, args) -> int:
    from .demo import load_demo
    if args.offline:
        sample = Path(__file__).parent / "sample_digest.json"
        data = json.loads(sample.read_text(encoding="utf-8"))
        _write_report(cfg, data, None, args.open)
        return 0
    from .digest import DigestBuilder, get_tz
    tz = get_tz(cfg.digest.get("timezone"))
    store = Store(":memory:")
    since, until = load_demo(cfg, store, tz)
    llm = _llm(cfg, args.provider)
    data = DigestBuilder(cfg, store, llm, progress=_out).build(since, until, "demo")
    _write_report(cfg, data, None, args.open)
    return 0


def cmd_import(cfg, args) -> int:
    from .importer import import_path
    from .service import open_store
    store = open_store(cfg)
    try:
        import_path(cfg, store, args.path, progress=_out)
    finally:
        store.close()
    return 0


def cmd_vpn(cfg, args) -> int:
    from . import vpn
    if args.selftest:  # запустить Xray с пустышкой-сервером: проверка, что ядро работает на этом компьютере
        import base64
        import os
        import uuid
        pbk = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        s = vpn.links.parse_link(f"vless://{uuid.uuid4()}@127.0.0.1:9?type=tcp&security=reality&pbk={pbk}"
                                 "&sni=www.microsoft.com&sid=ab&fp=chrome#selftest")
        try:
            vpn.xray.RUNNER.start([s])
            _out(f"Xray {vpn.xray.version()} запущен, локальный порт открыт.")
        finally:
            vpn.xray.stop_all()
        return 0
    if args.install is not None:
        v = vpn.xray.install_from_zip(args.install, _out) if args.install else vpn.xray.install_from_github(_out)
        _out(f"Xray {v} установлен.")
        return 0
    try:
        res = vpn.refresh(cfg, _out)
    finally:
        vpn.xray.stop_all()
    _out(f"Telegram отвечает через {res['ok']} из {res['servers']} серверов.")
    return 0 if res["ok"] else 1


def cmd_proxy_refresh(cfg, args) -> int:
    from .sources.telegram import TelegramSource
    bot = args.bot or cfg.telegram.get("proxy_bot")
    if not bot:
        raise SystemExit("Укажите бота: --bot @имя или telegram.proxy_bot в config.toml")
    store = _store(cfg)
    src = TelegramSource(cfg, store, progress=_out)

    async def run():
        await src.connect()
        try:
            return await src.refresh_proxies_from_bot(bot, args.command or cfg.telegram.get("proxy_bot_command") or "/start")
        finally:
            await src.close()

    links = asyncio.run(run())
    if links:
        _out(f"Получено прокси: {len(links)} → data/proxies.txt (будут пробоваться автоматически)")
        for l in links:
            _out(f"  {l[:80]}")
    else:
        _out("Бот не прислал ссылок на прокси (возможно, нужно нажать кнопку — пока не поддерживается).")
    return 0


def _resolve_config(arg: str | None) -> Path:
    """--config → ./config.toml → config.json рабочей папки, выбранной в окне OpsDigest."""
    if arg:
        return Path(arg)
    if Path("config.toml").exists():
        return Path("config.toml")
    from .workspace import config_path, load_app_settings
    ws = load_app_settings().get("workspace")
    if ws and config_path(ws).exists():
        return config_path(ws)
    return Path("config.toml")


# ============================================================================ разбор аргументов
def _unlock_cli(cfg_path, cmd: str) -> int | None:
    """Защищённая рабочая папка: спросить пароль (данные и настройки зашифрованы)."""
    from pathlib import Path as _P

    from .secure import vault
    if not cfg_path:
        return None
    ws = _P(cfg_path).resolve().parent
    if not vault.exists(ws):
        return None
    vault.bind(ws)
    if not sys.stdin or not sys.stdin.isatty():
        _out("Данные OpsDigest зашифрованы: команду нужно запускать в окне консоли (спросит пароль) "
             "или пользоваться окном OpsDigest. Сводка по расписанию при включённой защите не запускается.")
        return 3
    import getpass
    try:
        vault.unlock(getpass.getpass("Пароль OpsDigest: "), ws)
    except vault.BadSecret as e:
        _out(f"Ошибка: {e}")
        return 3
    if vault.settings().get("require_telegram", True) and cmd not in ("login", "check", "vpn") \
            and not (ws / "data" / "telegram.session").exists():
        _out("Сначала войдите в Telegram (команда login или окно OpsDigest) — без входа данные не открываются.")
        return 3
    return None


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    p = argparse.ArgumentParser(prog="opsdigest", description="Сводки по чатам Telegram для инженера поддержки")
    p.add_argument("--config", help="config.json рабочей папки или config.toml; по умолчанию — папка из окна OpsDigest")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="проверить прокси, Telegram и LLM")
    s.add_argument("--provider")
    s.add_argument("--no-telegram", action="store_true")
    s.add_argument("--no-llm", action="store_true")

    sub.add_parser("login", help="вход в Telegram")

    s = sub.add_parser("chats", help="список чатов и их приоритетов")
    s.add_argument("--limit", type=int, default=60)

    s = sub.add_parser("sync", help="забрать сообщения")
    s.add_argument("--days", type=float)
    s.add_argument("--since")

    s = sub.add_parser("digest", help="сводка")
    s.add_argument("--period", choices=["day", "week", "month"], default="day")
    s.add_argument("--since", help="с даты: 2026-10-20 или '20.10.2026 18:00' (после отпуска)")
    s.add_argument("--until")
    s.add_argument("--provider", help="LLM-профиль из config.toml")
    s.add_argument("--no-sync", action="store_true", help="не ходить в Telegram, только база")
    s.add_argument("--send", action="store_true", help="отправить короткую сводку и отчёт в «Избранное»")
    s.add_argument("--open", action="store_true", help="открыть отчёт в браузере")

    s = sub.add_parser("source", help="сводка «От источника»: очередь ITSM группы или задачи трекера")
    s.add_argument("--start", choices=["itsm", "tracker"], default="itsm", help="с чего начать")
    s.add_argument("--group", help="группа исполнителей ITSM (или проект/доска/фильтр трекера)")
    s.add_argument("--state", choices=["open", "waiting", "work", "new"], default="open")
    s.add_argument("--types", help="типы записей ITSM через запятую: incident,request,problem,change")
    s.add_argument("--text", help="ещё условия своими словами")
    s.add_argument("--limit", type=int, default=60, help="не больше N записей")
    s.add_argument("--days", type=int, default=30, help="упоминания в Telegram и почте — за N дней")
    s.add_argument("--no-second", action="store_true", help="не открывать связанные задачи/инциденты")
    s.add_argument("--no-telegram", action="store_true", help="не искать номера в Telegram")
    s.add_argument("--no-mail", action="store_true", help="не искать номера в почте")
    s.add_argument("--rooms", action="store_true", help="искать номера в X5 Rooms")
    s.add_argument("--meet", action="store_true", help="искать номера во встречах Scriber AI")
    s.add_argument("--extra", help="вопрос и указания к разбору")
    s.add_argument("--provider", help="LLM-профиль")
    s.add_argument("--no-sync", action="store_true", help="не забирать новые сообщения из Telegram")
    s.add_argument("--send", action="store_true", help="отправить коротко и отчёт в «Избранное»")
    s.add_argument("--open", action="store_true", help="открыть отчёт в браузере")

    s = sub.add_parser("ask", help="вопрос по переписке")
    s.add_argument("question", nargs="+")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--provider")
    s.add_argument("--no-sync", action="store_true")

    s = sub.add_parser("demo", help="сводка по встроенным примерам без Telegram")
    s.add_argument("--provider")
    s.add_argument("--offline", action="store_true", help="без LLM: показать готовый пример отчёта")
    s.add_argument("--open", action="store_true")

    s = sub.add_parser("import", help="загрузить экспорт Telegram Desktop (папка или result.json)")
    s.add_argument("path")

    s = sub.add_parser("vpn", help="VPN по подписке: проверить серверы (или --install — поставить Xray)")
    s.add_argument("--install", nargs="?", const="", default=None, help="без значения — скачать с GitHub; путь — из zip")
    s.add_argument("--selftest", action="store_true", help="проверить, что Xray запускается на этом компьютере")

    s = sub.add_parser("export", help="выгрузить историю чатов с изображениями и лёгкими файлами")
    s.add_argument("--since", help="с даты (по умолчанию — вся история)")
    s.add_argument("--chat", action="append", help="номер чата (можно несколько раз); по умолчанию — все, кроме исключённых")
    s.add_argument("--no-media", action="store_true", help="без скачивания файлов (только ссылки)")

    s = sub.add_parser("images", help="прочитать (описать) изображения чата")
    s.add_argument("--chat", required=True, help="номер чата (см. команду chats)")
    s.add_argument("--since", help="с даты; по умолчанию — за всё время")
    s.add_argument("--no-download", action="store_true", help="не ходить в Telegram за нескачанными")
    s.add_argument("--provider", help="LLM-профиль для изображений")

    s = sub.add_parser("fetch", help="докачать файл по номеру (m123)")
    s.add_argument("id")

    s = sub.add_parser("vision-check", help="проверить, видит ли модель изображения")
    s.add_argument("--provider")
    s.add_argument("--rediscover", action="store_true", help="Maverick: подобрать формат картинок заново")

    s = sub.add_parser("proxy-refresh", help="(эксперимент) получить прокси у бота")
    s.add_argument("--bot")
    s.add_argument("--command")

    args = p.parse_args(argv)
    from . import logs
    log = logs.setup("cli")
    cfg_path = _resolve_config(args.config)
    rc = _unlock_cli(cfg_path, args.cmd)
    if rc is not None:
        return rc
    cfg = load_config(cfg_path)
    logs.add_secrets_from_config(cfg.data)
    log.info("команда: %s", args.cmd)
    cmds = {"check": cmd_check, "login": cmd_login, "chats": cmd_chats, "sync": cmd_sync, "digest": cmd_digest, "source": cmd_source,
            "ask": cmd_ask, "demo": cmd_demo, "import": cmd_import, "vpn": cmd_vpn, "proxy-refresh": cmd_proxy_refresh,
            "export": cmd_export, "images": cmd_images, "fetch": cmd_fetch, "vision-check": cmd_vision_check}
    try:
        return cmds[args.cmd](cfg, args)
    except KeyboardInterrupt:
        _out("\nОстановлено.")
        return 130
    except Exception as e:  # noqa: BLE001
        from .llm import LLMError
        from .sources.telegram import TelegramError
        log.warning("команда %s: ошибка %s\n%s", args.cmd, logs.err_info(e), logs.frames(e))
        if isinstance(e, (LLMError, TelegramError, KeyError)) or e.__class__.__name__ in ("ProxyError", "VpnError",
                                                                                            "ImportError_"):
            _out(f"\nОшибка: {e}")
            return 2
        if isinstance(e, ModuleNotFoundError) and "telethon" in str(e):
            _out("\nОшибка: не установлен пакет telethon — запустите OpsDigest.bat")
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
