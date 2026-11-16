"""Общие операции для окна и командной строки: синхронизация, сводка, отчёт, отправка, вопрос,
выгрузка всей истории, чтение изображений чата, докачка файла."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .storage import IMAGE_KINDS, Store, iso

Progress = Callable[[str], None]
RunAsync = Callable[[Awaitable], Any]
Cancel = Callable[[], bool]


def open_store(cfg) -> Store:
    return Store(cfg.path(cfg["storage"]["path"]))


async def tg_sync(cfg, store: Store, since: datetime, progress: Progress, only: set[int] | None = None) -> dict:
    from .sources.telegram import TelegramSource
    src = TelegramSource(cfg, store, progress=progress)
    await src.connect()
    try:
        progress(f"Синхронизация Telegram с {since.astimezone():%d.%m.%Y %H:%M}"
                 + (f" (выбранные чаты: {len(only)})" if only else "") + " …")
        st = await (src.with_failover(src.sync, since, only) if only else src.with_failover(src.sync, since))
        # изображения периода, которых ещё нет в папке (например, после загрузки экспорта без фото)
        rows = store.media_list(since=iso(since), status=("pending", "failed"), kinds=None) if src.policy.download else []
        if only:
            rows = [r for r in rows if r.get("chat_id") in only]
        if rows:
            progress(f"Докачиваю вложения периода: {len(rows)}…")
            dl = await src.with_failover(src.download_rows, rows)
            st["media"] = st.get("media", 0) + dl["downloaded"]
    finally:
        await src.close()
    progress(f"Диалогов: {st['dialogs']}, чатов с новым: {st['chats']}, новых сообщений: {st['messages']}"
             + (f", файлов: {st['media']}" if st.get("media") else ""))
    if st["capped"]:
        progress(f"Упёрлись в лимит сообщений на чат в: {', '.join(st['capped'][:10])}")
    return st


async def tg_send(cfg, store: Store, text: str, file: Path | None, progress: Progress) -> None:
    from .sources.telegram import TelegramSource
    src = TelegramSource(cfg, store, progress=progress)
    await src.connect()
    try:
        await src.with_failover(src.send_to_saved, text, file)
    finally:
        await src.close()


def write_report(cfg, data: dict, store: Store | None) -> dict:
    from .render import render_html
    out_dir = cfg.path(cfg.digest.get("output_dir") or "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base = out_dir / f"{stamp}_{data['meta']['period']}"
    html_path = base.with_suffix(".html")
    from .secure import files  # в защищённой папке отчёты зашифрованы: открываются только в окне OpsDigest
    files.write_text(html_path, render_html(data))
    files.write_text(base.with_suffix(".json"), json.dumps(data, ensure_ascii=False, indent=1))
    paths = {"html": str(html_path), "json": str(base.with_suffix(".json"))}
    try:  # Markdown-версия (для Confluence/почты)
        from .render.markdown import render_markdown
        files.write_text(base.with_suffix(".md"), render_markdown(data))
        paths["md"] = str(base.with_suffix(".md"))
    except ImportError:
        pass
    if store is not None:
        store.save_digest(data["meta"]["period"], data["meta"]["since"], data["meta"]["until"],
                          data["meta"]["provider"], data, str(html_path))
    return paths


def run_digest(cfg, *, period: str = "day", since: datetime | None = None, until: datetime | None = None,
               send: bool = False, sync: bool = True, provider: str | None = None, progress: Progress = print,
               run_async: RunAsync = asyncio.run, sources: dict | None = None, chat_ids: set[int] | None = None,
               extra: str = "", label: str = "", cancel: Callable[[], bool] | None = None,
               history: bool | None = None) -> dict:
    """Синхронизация → сводка → отчёт → (по желанию) отправка в «Избранное». Возвращает пути и итог.
    chat_ids — только эти чаты; extra — указания пользователя для этой сводки; cancel — остановили ли сводку."""
    from .digest import DigestBuilder, DigestCancelled, get_tz, period_range
    from .llm import make_client
    from .render import render_telegram
    tz = get_tz(cfg.digest.get("timezone"))
    stop = cancel or (lambda: False)

    def check() -> None:
        if stop():
            raise DigestCancelled("сводка остановлена")
    store = open_store(cfg)
    try:
        kind = period if period in ("day", "week", "month", "hours") else ("custom" if since else period)
        since, until = period_range(period, tz, since=since, until=until)
        sync_error = send_error = None
        if sync:
            try:
                run_async(tg_sync(cfg, store, since, progress, only=chat_ids or None))
            except Exception as e:  # noqa: BLE001 — без связи с Telegram сводка всё равно строится по базе
                if getattr(e, "cancelled", False) or stop():
                    raise
                sync_error = str(e)[:300] or type(e).__name__
                progress(f"⚠ Telegram не ответил ({sync_error[:160]}) — сводка по тому, что уже есть в базе.")
                send = False
        check()
        llm = make_client(*cfg.llm_profile(provider))
        vision = _vision_llm(cfg, llm, provider)
        data = DigestBuilder(cfg, store, llm, progress=progress, vision_llm=vision, cancel=stop,
                             extra=extra, history=history).build(since, until, kind, only=chat_ids or None, label=label)
        check()
        from .cross import apply as cross_apply  # общая картина: почта, ITSM, вики (если включены)
        cross_apply(cfg, store, llm, data, since, until, sources=sources, progress=progress)
        check()   # остановили во время сверки — отчёт не пишем
        paths = write_report(cfg, data, store)
        progress("Отчёт готов.")
        if send and not data["meta"]["messages"]:   # «с прошлой сводки» без нового — не шуметь в «Избранном»
            progress("Новых сообщений за период нет — в «Избранное» не отправляю.")
            send = False
        if send:
            progress("Отправляю в «Избранное» Telegram…")
            try:
                run_async(tg_send(cfg, store, render_telegram(data), Path(paths["html"]), progress))
                progress("Отправлено.")
            except Exception as e:  # noqa: BLE001 — отчёт уже готов, отправка — по возможности
                if getattr(e, "cancelled", False) or stop():
                    raise
                send_error = str(e)[:300] or type(e).__name__
                progress(f"⚠ В «Избранное» не отправлено: {send_error[:160]}. Отчёт готов и лежит в программе.")
        ov = data["overview"]
        cr = data.get("cross") or {}
        return {"paths": paths, "headline": ov.get("headline", ""), "top": len(ov.get("top") or []),
                "chats": data["meta"]["chats"], "messages": data["meta"]["messages"],
                "errors": data["meta"]["errors"], "period_text": data["meta"].get("period_text", ""),
                "cross": {"sources": cr.get("sources") or [], "threads": len(cr.get("threads") or []),
                          "offline": list((cr.get("offline") or {}).keys()), "headline": cr.get("headline", "")}
                if cr else None,
                "glossary_pending": _pending(store), "sync_error": sync_error, "send_error": send_error}
    finally:
        store.close()


def run_source_digest(cfg, *, start: str = "itsm", query: dict | None = None, look: dict | None = None,
                      send: bool = False, sync: bool = True, provider: str | None = None, progress: Progress = print,
                      run_async: RunAsync = asyncio.run, extra: str = "", cancel: Callable[[], bool] | None = None) -> dict:
    """Сводка «От источника»: очередь ITSM (или задачи трекера) → связанное → Telegram и почта по номерам → отчёт.

    Telegram здесь — дополнительный источник: перед разбором (sync) забираются только новые сообщения за срок поиска
    упоминаний, дальше программа ищет в базе лишь номера из очереди."""
    from . import source_digest as sdm
    from .digest import DigestCancelled
    from .llm import make_client
    from .render import render_telegram
    stop = cancel or (lambda: False)

    def check() -> None:
        if stop():
            raise DigestCancelled("сводка остановлена")
    store = open_store(cfg)
    try:
        lk = sdm.norm_look(look)
        sync_error = send_error = None
        if sync and lk["telegram"]:
            try:
                since = datetime.now(timezone.utc) - timedelta(days=lk["days"])
                run_async(tg_sync(cfg, store, since, progress))
            except Exception as e:  # noqa: BLE001 — без Telegram разбор всё равно строится, по базе
                if getattr(e, "cancelled", False) or stop():
                    raise
                sync_error = str(e)[:300] or type(e).__name__
                progress(f"⚠ Telegram не ответил ({sync_error[:160]}) — номера ищу в том, что уже есть в базе.")
                send = False
        check()
        llm = make_client(*cfg.llm_profile(provider))
        data = sdm.build(cfg, store, llm, start=start, query=query, look=lk, extra=extra, progress=progress, cancel=stop)
        check()
        paths = write_report(cfg, data, store)
        progress("Отчёт готов.")
        if send:
            progress("Отправляю в «Избранное» Telegram…")
            try:
                run_async(tg_send(cfg, store, render_telegram(data), Path(paths["html"]), progress))
                progress("Отправлено.")
            except Exception as e:  # noqa: BLE001 — отчёт уже готов, отправка — по возможности
                if getattr(e, "cancelled", False) or stop():
                    raise
                send_error = str(e)[:300] or type(e).__name__
                progress(f"⚠ В «Избранное» не отправлено: {send_error[:160]}. Отчёт готов и лежит в программе.")
        src = data["source"]
        return {"paths": paths, "headline": src.get("headline", ""), "top": len(src.get("attention") or []),
                "chats": data["meta"]["chats"], "messages": data["meta"]["messages"], "errors": data["meta"]["errors"],
                "period_text": data["meta"]["period_text"], "cross": None, "mode": "source",
                "source": {"start": src["start"], "query": src["query_text"], "items": len(src["items"]),
                           "total": src.get("total"), "truncated": src.get("truncated"),
                           "secondary": sum(1 for s in src["secondary"] if s["found"]), "groups": len(src["groups"]),
                           "attention": len(src["attention"]), "actions": len(src["actions"]),
                           "offline": list(src["offline"].keys()), "in_telegram": src["stats"].get("in_telegram", 0),
                           "in_mail": src["stats"].get("in_mail", 0)},
                "glossary_pending": _pending(store), "sync_error": sync_error, "send_error": send_error}
    finally:
        store.close()


def _pending(store: Store) -> int:
    try:
        from .glossary_auto import counts
        return int(counts(store).get("pending", 0))
    except Exception:  # noqa: BLE001
        return 0


def _vision_llm(cfg, llm, provider: str | None = None):
    """Модель для изображений: media.vision_provider, иначе та же, что для текста."""
    from .llm import make_client
    from .vision import vision_profile
    name = vision_profile(cfg)
    if not name or name == (provider or cfg["llm"].get("provider")):
        return llm
    try:
        return make_client(*cfg.llm_profile(name))
    except Exception:  # noqa: BLE001 — профиль удалён: описываем основной моделью
        return llm


def run_ask(cfg, question: str, *, days: int = 14, sync: bool = True, provider: str | None = None,
            progress: Progress = print, run_async: RunAsync = asyncio.run) -> dict:
    """{"answer", "images", "need_files", "described"}"""
    from .ask import answer
    from .llm import make_client
    store = open_store(cfg)
    try:
        if sync:
            run_async(tg_sync(cfg, store, datetime.now(timezone.utc) - timedelta(days=days), progress))
        llm = make_client(*cfg.llm_profile(provider))
        return answer(cfg, store, llm, question, days=days, progress=progress, vision_llm=_vision_llm(cfg, llm, provider))
    finally:
        store.close()


# ====================================================================== выгрузка всей истории
def run_export(cfg, *, chat_ids: set[int] | None = None, since: datetime | None = None, with_media: bool = True,
               progress: Progress = print, run_async: RunAsync = asyncio.run, cancel: Cancel | None = None) -> dict:
    """Вся история всех (или выбранных) чатов через Telegram API — с изображениями и лёгкими файлами.

    Можно остановить (cancel) и запустить снова — продолжит с того места, где остановилась."""
    from .sources.telegram import TelegramSource
    cancel = cancel or (lambda: False)
    store = open_store(cfg)
    try:
        src = TelegramSource(cfg, store, progress=progress)

        async def go():
            await src.connect()
            try:
                st = await src.with_failover(src.export_history, chat_ids=chat_ids, since=since,
                                             with_media=with_media, cancel=cancel)
                if with_media and src.policy.download and not cancel():
                    # вложения сообщений, загруженных раньше (экспорт Telegram Desktop без фото, старые синхронизации)
                    ids = chat_ids if chat_ids is not None else None
                    rows = store.media_list(chat_ids=ids, since=iso(since) if since else None,
                                            status=("pending",), kinds=None)
                    if rows:
                        progress(f"Докачиваю вложения, которых нет в папке: {len(rows)}…")
                        dl = await src.with_failover(src.download_rows, rows, cancel)
                        st["downloaded"] += dl["downloaded"]
                        st["failed"] += dl["failed"]
                return st
            finally:
                await src.close()
        st = run_async(go())
        ms = store.media_stats()
        st.update(images_total=ms["images"], images_described=ms["described"], refs=ms["refs"])
        progress(("Остановлено. " if st.get("cancelled") else "Готово. ")
                 + f"Чатов с новым: {st['chats']}, сообщений прочитано: {st['messages']} (новых {st['new']}), "
                 f"файлов скачано: {st['downloaded']}" + (f", не скачано: {st['failed']}" if st["failed"] else "")
                 + f". Исключённых чатов пропущено: {st['excluded']}.")
        return st
    finally:
        store.close()


# ====================================================================== изображения чата
def run_images(cfg, chat_id: int, *, since: datetime | None = None, download: bool = True,
               progress: Progress = print, run_async: RunAsync = asyncio.run, cancel: Cancel | None = None,
               provider: str | None = None) -> dict:
    """Прочитать (описать) все изображения чата за всё время или с даты. Нескачанные — сначала скачать."""
    from .sources.telegram import TelegramError, TelegramSource
    from .vision import Describer, VisionUnavailable
    cancel = cancel or (lambda: False)
    store = open_store(cfg)
    try:
        chat = store.get_chat(chat_id)
        title = chat.title if chat else str(chat_id)
        s = iso(since) if since else None
        rows = store.media_list(chat_ids=[chat_id], since=s, kinds=IMAGE_KINDS)
        progress(f"«{title}»: изображений {len(rows)}" + (f" с {since:%d.%m.%Y}" if since else " за всё время"))
        need = [r for r in rows if r["status"] in ("pending", "failed", "ref")]
        res: dict = {"images": len(rows), "downloaded": 0, "download_error": ""}
        if need and download:
            progress(f"Скачиваю из Telegram: {len(need)}…")
            src = TelegramSource(cfg, store, progress=progress)

            async def go():
                await src.connect()
                try:
                    return await src.with_failover(src.download_rows, need, cancel)
                finally:
                    await src.close()
            try:
                dl = run_async(go())
                res["downloaded"] = dl["downloaded"]
            except (TelegramError, OSError) as e:
                res["download_error"] = str(e)[:300]
                progress(f"  ✗ Telegram недоступен — описываю только уже скачанные ({str(e)[:120]})")
        if cancel():
            res["cancelled"] = True
            return res
        todo = store.media_list(chat_ids=[chat_id], since=s, kinds=IMAGE_KINDS, status=("ok",), undescribed=True)
        from .llm import make_client
        llm = make_client(*cfg.llm_profile(provider or None)) if provider else None
        d = Describer(cfg, store, llm, progress=progress, cancel=cancel)
        try:
            d.describe(todo)
        except VisionUnavailable as e:
            res["error"] = str(e)
            progress(f"✗ {e}")
        res.update(d.stats)
        res["cancelled"] = cancel()
        st = store.media_stats(chat_id)
        res.update(total_described=st["described"], total_ok=st["images_ok"])
        progress(d.summary_line() + f". Всего в чате описано {st['described']} из {st['images']}.")
        return res
    finally:
        store.close()


def run_fetch(cfg, media_id: int, *, progress: Progress = print, run_async: RunAsync = asyncio.run,
              describe: bool = True) -> dict:
    """Докачать файл по запросу (в т.ч. тяжёлый); изображение — сразу описать."""
    from .sources.telegram import TelegramSource
    from .vision import Describer, VisionUnavailable
    store = open_store(cfg)
    try:
        src = TelegramSource(cfg, store, progress=progress)

        async def go():
            await src.connect()
            try:
                return await src.with_failover(src.fetch_media, media_id)
            finally:
                await src.close()
        row = run_async(go())
        progress(f"Скачано: {row.get('path')}")
        if describe and row.get("kind") in IMAGE_KINDS and not row.get("described"):
            try:
                Describer(cfg, store, progress=progress).describe([row])
            except VisionUnavailable as e:
                progress(f"✗ {e}")
            row = store.get_media(media_id) or row
        return {k: row.get(k) for k in ("id", "kind", "name", "size", "path", "status", "summary", "text")}
    finally:
        store.close()
