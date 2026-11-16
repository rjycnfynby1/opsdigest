"""Описание изображений моделью: кеш по SHA-256, файл-описание рядом с картинкой, поиск по описаниям.

Кто вызывает:
• сводка — все новые изображения периода описываются обязательно (до map/reduce);
• вопрос по переписке — модель сама выбирает, какие неописанные изображения из найденного стоит прочитать;
• раздел «Чаты» — «Прочитать изображения чата» за всё время или с даты;
• команда `python -m opsdigest images`.

Одинаковая картинка (то же содержимое) описывается один раз: описание хранится в базе (image_desc) по SHA-256
и в файле <картинка>.md рядом с ней — даже если базу пересоздать, повторно модель не вызывается.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import logs, prompts
from .entities import EntityExtractor
from .llm import LLMClient, LLMError, inflight, make_client
from .llm.base import extract_json
from .media import VISION_MIMES, digit_png, read_sidecar, sha256_file, sniff_mime, write_sidecar
from .storage import Message, Store, iso, parse_iso

log = logs.get("vision")
Progress = Callable[[str], None]
MAX_IMAGE_BYTES = 8 * 2**20


class VisionUnavailable(RuntimeError):
    """Модель не принимает изображения — описывать дальше бессмысленно."""


def vision_profile(cfg) -> str | None:
    return (cfg.media.get("vision_provider") or "").strip() or None


def make_vision_llm(cfg, provider: str | None = None) -> LLMClient:
    return make_client(*cfg.llm_profile(provider or vision_profile(cfg)))


def remember_discovery(cfg, llm: LLMClient) -> None:
    """Maverick нашёл формат передачи картинок — сохранить в config.json рабочей папки, чтобы не искать снова."""
    mode = getattr(llm, "discovered_image_mode", None)
    field = getattr(llm, "discovered", None)
    if not mode and not field:
        return
    p = Path(cfg.base_dir) / "config.json"
    if not p.exists():
        return
    try:
        from . import workspace as wsmod
        raw = wsmod.read_config(cfg.base_dir)
        prof = ((raw.get("llm") or {}).get("profiles") or {}).get(llm.name)
        if prof is None:
            return
        if mode:
            prof["image_mode"] = mode
        if field:
            prof["body_field"] = field
        wsmod.write_config(cfg.base_dir, raw)
        log.info("Maverick: формат изображений сохранён")
    except Exception as e:  # noqa: BLE001 — не мешаем основной работе
        log.warning("не удалось сохранить формат изображений: %s", logs.err_info(e))


def save_report(cfg, llm: LLMClient) -> Path | None:
    """Журнал подбора формата картинок для Maverick → data/maverick-images.txt (без ключа, картинок и переписки)."""
    lines = getattr(llm, "image_report", None)
    if not lines:
        return None
    from . import __version__
    p = cfg.path("data/maverick-images.txt")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        head = [f"OpsDigest {__version__} · подбор формата изображений для Maverick · "
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')}", ""]
        p.write_text("\n".join(head + [logs.scrub(x, quotes=False) for x in lines]) + "\n", encoding="utf-8")
        return p
    except OSError:
        return None


def check(llm: LLMClient, progress: Progress | None = None) -> dict:
    """Видит ли модель изображения: картинка со случайной цифрой — модель должна её назвать."""
    t0 = time.time()
    mode = None
    if hasattr(llm, "discover_images") and not isinstance(llm.profile.get("image_mode"), dict):
        mode = llm.discover_images(progress)
    digit = str(random.randint(2, 9))
    ans = llm.complete("Отвечай одной цифрой.", "Какая цифра нарисована на изображении? Ответь только цифрой.",
                       max_tokens=20, images=[(digit_png(digit), "image/png")])
    from .llm.attach import digits_in
    ok = digits_in(ans) == {digit}  # «7», «цифра семь», «семёрка»
    return {"ok": ok, "answer": ans.strip()[:80], "expected": digit, "mode": mode or llm.profile.get("image_mode"),
            "seconds": round(time.time() - t0, 1), "label": llm.label}


def _read(path: Path) -> bytes:
    from .secure import files
    return files.read_bytes(path)


def parse_description(raw: str) -> dict:
    try:
        d = extract_json(raw)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        text = (raw or "").strip()
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0][:200]
        return {"kind": "", "summary": first, "details": text[:1500], "text": "", "importance": 3}
    imp = re.search(r"\d", str(d.get("importance") or "3"))
    txt = d.get("text")
    if isinstance(txt, list):
        txt = "\n".join(str(x) for x in txt)
    return {"kind": str(d.get("kind") or "").strip()[:60], "summary": str(d.get("summary") or "").strip()[:400],
            "details": str(d.get("details") or "").strip()[:2500], "text": str(txt or "").strip()[:3000],
            "importance": max(1, min(5, int(imp.group(0)))) if imp else 3}


class Describer:
    def __init__(self, cfg, store: Store, llm: LLMClient | None = None, progress: Progress = print,
                 cancel: Callable[[], bool] | None = None):
        self.cfg = cfg
        self.store = store
        self._llm = llm
        self.progress = progress
        self.cancel = cancel or (lambda: False)
        self.workers = max(1, int(cfg.media.get("describe_workers") or 2))
        from .digest import get_tz
        self.tz = get_tz(cfg.digest.get("timezone"))
        self.extractor = EntityExtractor(cfg.get("entities") or {})
        self.stats = {"total": 0, "described": 0, "cached": 0, "failed": 0, "unsupported": 0, "missing": 0}
        self.fatal: str | None = None
        self.errors: list[str] = []

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = make_vision_llm(self.cfg)
        if not hasattr(self, "_priv"):
            from . import privacy  # подписи, соседние сообщения и название чата — обезличенно; картинка — как есть
            self._priv = privacy.attach(self._llm, self.cfg, self.store)
        return self._llm

    # ------------------------------------------------------------ подготовка
    def _prepare(self, row: dict) -> tuple[str, Path, str] | None:
        """(sha, файл, mime) для вызова модели или None, если описание не нужно/невозможно (уже учтено в stats)."""
        if row.get("status") != "ok" or not row.get("path"):
            self.stats["missing"] += 1
            return None
        path = self.cfg.path(row["path"])
        if not path.is_file():
            self.store.set_media_status(row["id"], "pending", "файл удалён из папки — будет скачан заново")
            self.stats["missing"] += 1
            return None
        sha = row.get("sha256") or sha256_file(path)
        if not row.get("sha256"):
            self.store.set_media_file(row["id"], row["path"], sha)
        if self.store.description(sha):
            self.stats["cached"] += 1
            return None
        side = read_sidecar(path, sha)
        if side:
            side["created_at"] = iso(datetime.now(timezone.utc))
            self.store.save_description(sha, side)
            self.stats["cached"] += 1
            return None
        from .secure import files
        head = files.head(path, 16)
        size = files.size(path)
        mime = sniff_mime(head) or row.get("mime") or ""
        if mime not in VISION_MIMES or size > MAX_IMAGE_BYTES:
            self.store.set_desc_state(row["id"], "unsupported",
                                      f"формат {mime or '?'} не читается моделью" if mime not in VISION_MIMES
                                      else "изображение больше 8 МБ")
            self.stats["unsupported"] += 1
            return None
        return sha, path, mime

    def _context(self, row: dict) -> tuple[str, list[str], Message | None]:
        msgs = self.store.context(row["chat_id"], row["msg_id"], before=3, after=2)
        me = next((m for m in msgs if m.msg_id == row["msg_id"]), None)
        around = []
        for m in msgs:
            if m.msg_id == row["msg_id"]:
                continue
            dt = parse_iso(m.date).astimezone(self.tz)
            around.append(f"[{dt:%d.%m %H:%M}] {'Я' if m.is_out else m.sender}: {m.text[:300]}")
        caption = re.sub(r"^\[[^\]]*\]\s*", "", me.text if me else "").strip()
        return caption, around, me

    def _build(self, row: dict) -> dict:
        """Текст запроса и подписи для файла-описания (в основном потоке: база — не из рабочих потоков)."""
        caption, around, me = self._context(row)
        chat = self.store.get_chat(row["chat_id"])
        when = parse_iso(row["date"]).astimezone(self.tz).strftime("%d.%m.%Y %H:%M") if row.get("date") else "—"
        sender = ("Я" if me.is_out else me.sender) if me else "?"
        title = chat.title if chat else str(row["chat_id"])
        user = prompts.image_prompt(title, chat.note if chat else "", when, sender, caption, around)
        from .glossary import block_for  # термины пользователя — чтобы называть сервисы и сущности его словами
        user = prompts.with_glossary(block_for(self.store, caption + "\n" + "\n".join(around), 1500, scope="vision"), user)
        return {"user": user, "_caption": caption, "_when": when, "_sender": sender, "_chat": title}

    def _call(self, meta: dict, path: Path, mime: str) -> dict:
        raw = self.llm.complete(prompts.IMAGE_SYSTEM, meta["user"], max_tokens=1200,
                                images=[(_read(path), mime)])
        d = parse_description(raw)
        d.update(model=self.llm.label, created_at=iso(datetime.now(timezone.utc)),
                 **{k: v for k, v in meta.items() if k.startswith("_")})
        return d

    def _save(self, row: dict, sha: str, d: dict) -> None:
        self.store.save_description(sha, d)
        path = self.cfg.path(row["path"])
        try:
            write_sidecar(path, desc=d, sha=sha, chat=d["_chat"], date_local=d["_when"], sender=d["_sender"],
                          msg_id=row["msg_id"], caption=d["_caption"])
        except OSError as e:
            log.warning("файл описания не записан: %s", logs.err_info(e))
        text = " ".join(x for x in (d.get("summary"), d.get("details"), d.get("text")) if x)
        if text:  # номера INC/задач со скриншотов — в сквозные упоминания
            pseudo = Message(chat_id=row["chat_id"], msg_id=row["msg_id"], date=row.get("date") or "", sender="",
                             text=text)
            self.store.add_entities(self.extractor.rows([pseudo]))

    # ------------------------------------------------------------ основное
    def describe(self, rows: list[dict], label: str = "") -> dict:
        """Описать изображения (уже описанные и одинаковые по содержимому берутся из кеша)."""
        todo: list[tuple[dict, str, Path, str]] = []
        seen: set[str] = set()
        self.stats["total"] += len(rows)
        for row in rows:
            if self.cancel():
                break
            prep = self._prepare(row)
            if prep is None:
                continue
            sha, path, mime = prep
            if sha in seen:  # такая же картинка уже в очереди
                self.stats["cached"] += 1
                continue
            seen.add(sha)
            todo.append((row, sha, path, mime))
        if not todo:
            return self.stats
        self.progress(f"Описываю изображения{label}: {len(todo)} (ещё {self.stats['cached']} — из готовых описаний)…")
        t0, last = time.time(), time.time()
        done = 0

        def report(force: bool = False) -> None:
            nonlocal last
            if force or time.time() - last > 8:
                last = time.time()
                rate = done / max(1.0, time.time() - t0) * 60
                self.progress(f"  описано {done}/{len(todo)}" + (f" (≈{rate:.0f} в минуту)" if done > 2 else "")
                              + (f", ошибок {self.stats['failed']}" if self.stats["failed"] else ""))

        def handle(row, sha, fut_or_exc) -> None:
            nonlocal done
            done += 1
            if isinstance(fut_or_exc, dict):
                self._save(row, sha, fut_or_exc)
                self.stats["described"] += 1
                return
            e = fut_or_exc
            if getattr(e, "cancelled", False):   # задачу остановили (например, перед сводкой) — не ошибка картинки
                return
            self.stats["failed"] += 1
            self.store.set_desc_state(row["id"], "failed", str(e)[:200])
            msg = str(e)
            if len(self.errors) < 5:
                self.errors.append(msg[:300])
            log.warning("изображение не описано: %s", getattr(e, "safe", None) or type(e).__name__)
            if getattr(e, "no_images", False):
                self.fatal = msg

        # первое — отдельно: Maverick при этом определяет формат передачи картинок, модель «прогревается»
        first, rest = todo[0], todo[1:]
        try:
            handle(first[0], first[1], self._call(self._build(first[0]), first[2], first[3]))
        except (LLMError, OSError) as e:
            handle(first[0], first[1], e)
        remember_discovery(self.cfg, self.llm)
        save_report(self.cfg, self.llm)
        if self.fatal:
            raise VisionUnavailable(self.fatal)
        streak = 0
        with cf.ThreadPoolExecutor(max_workers=self.workers) as pool:
            it = iter(rest)
            running: dict[cf.Future, tuple] = {}
            while True:
                while len(running) < self.workers * 2 and not self.cancel() and not self.fatal:
                    nxt = next(it, None)
                    if nxt is None:
                        break
                    running[pool.submit(inflight.carry(self._call), self._build(nxt[0]), nxt[2], nxt[3])] = nxt
                if not running:
                    break
                fin, _ = cf.wait(list(running), return_when=cf.FIRST_COMPLETED)
                for f in fin:
                    row, sha, _, _ = running.pop(f)
                    try:
                        res = f.result()
                        streak = 0
                    except (LLMError, OSError, ValueError) as e:
                        res = e
                        streak += 1
                    handle(row, sha, res)
                    if streak >= 5 and not self.fatal:
                        self.fatal = f"5 ошибок подряд, описание остановлено: {self.errors[-1] if self.errors else ''}"
                report()
        report(force=True)
        if self.fatal:
            raise VisionUnavailable(self.fatal)
        return self.stats

    def summary_line(self) -> str:
        s = self.stats
        parts = [f"описано {s['described']}"]
        if s["cached"]:
            parts.append(f"готовых описаний {s['cached']}")
        if s["failed"]:
            parts.append(f"ошибок {s['failed']}")
        if s["unsupported"]:
            parts.append(f"неподдерживаемый формат {s['unsupported']}")
        if s["missing"]:
            parts.append(f"не скачано {s['missing']}")
        return "Изображения: " + ", ".join(parts)


def pick_important(llm: LLMClient, question: str, store: Store, rows: list[dict], limit: int, tz,
                   prompt_fn=None) -> list[dict]:
    """Модель решает, какие неописанные изображения стоит прочитать (по тексту вокруг них)."""
    if not rows or limit <= 0:
        return []
    lines = []
    for r in rows[:40]:
        ctx = store.context(r["chat_id"], r["msg_id"], before=2, after=1)
        lines.append(f"— m{r['id']} в чате «{r.get('chat_title') or r['chat_id']}»:")
        for m in ctx:
            dt = parse_iso(m.date).astimezone(tz)
            mark = "  ▶ " if m.msg_id == r["msg_id"] else "    "
            lines.append(f"{mark}[{dt:%d.%m %H:%M}] {'Я' if m.is_out else m.sender}: {m.text[:250]}")
    fn = prompt_fn or prompts.triage_prompt
    try:
        raw = llm.complete("Ты отбираешь изображения для чтения. Отвечай только JSON.", fn(question, "\n".join(lines), limit),
                           max_tokens=300)
        data = extract_json(raw)
    except (LLMError, ValueError) as e:
        log.info("отбор изображений не удался: %s", getattr(e, "safe", None) or type(e).__name__)
        return rows[:limit]
    ids = data.get("describe") if isinstance(data, dict) else data
    want = {int(x) for x in re.findall(r"\d+", json.dumps(ids))}
    return [r for r in rows if r["id"] in want][:limit]
