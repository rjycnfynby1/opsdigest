"""Вложения переписки: что скачивать, куда класть, как подписывать.

Правило по умолчанию (настройки — раздел media в config.json):
• фото и изображения, отправленные файлом (до image_max_mb), — скачиваются: их потом читает модель;
• лёгкие документы (до light_max_mb: логи, txt, csv, json, docx, xlsx, pdf…) — скачиваются, текст из простых форматов
  попадает в поиск;
• тяжёлое (видео, голосовые, аудио, GIF, крупные файлы) — не скачивается: в базе остаётся ссылка с именем, размером
  и типом, файл можно докачать по запросу (кнопка в окне, команда fetch или решение модели при ответе на вопрос).

Файлы лежат в рабочей папке: media/<Название чата [id]>/<ГГГГ-ММ>/<дата_время>_m<номер>[_имя].<расширение>.
Описание изображения — рядом, в файле с тем же именем и расширением .md (а также в базе по SHA-256 содержимого).
"""
from __future__ import annotations

import hashlib
import re
import struct
import zipfile
import zlib
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any

from .storage import IMAGE_KINDS, Store, parse_iso

IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff", "image/heic")
VISION_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")  # что принимают модели
KIND_RU = {"photo": "фото", "image": "изображение", "document": "файл", "video": "видео", "gif": "GIF",
           "voice": "голосовое", "audio": "аудио", "video_note": "видеосообщение", "sticker": "стикер",
           "other": "вложение"}
TEXT_EXT = {"txt", "log", "csv", "tsv", "json", "xml", "yaml", "yml", "md", "ini", "conf", "cfg", "properties",
            "sql", "sh", "ps1", "py", "html", "htm", "eml", "err", "out", "trace"}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


# ============================================================== правило скачивания
class Policy:
    def __init__(self, cfg):
        m = cfg.media if hasattr(cfg, "media") else (cfg.get("media") or {})
        self.download = bool(m.get("download", True))
        self.photo_max_px = int(m.get("photo_max_px") or 1280)
        self.image_max = float(m.get("image_max_mb") or 10) * 2**20
        self.light_max = float(m.get("light_max_mb") or 3) * 2**20
        self.never = {str(x).lower().lstrip(".") for x in m.get("never_download") or []}

    def status(self, info: dict) -> str:
        """'pending' — скачать вместе с перепиской, 'ref' — только ссылка (докачка по запросу)."""
        if not self.download:
            return "ref"
        kind, size = info.get("kind"), info.get("size")
        if kind == "photo":
            return "pending"
        if kind == "image":
            return "pending" if (size or 0) <= self.image_max else "ref"
        if kind == "document":
            if ext_of(info.get("name"), info.get("mime")) in self.never or size is None:
                return "ref"
            return "pending" if size <= self.light_max else "ref"
        return "ref"


def ext_of(name: str | None, mime: str | None = None) -> str:
    if name and "." in name:
        return name.rsplit(".", 1)[1].lower()[:8]
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp",
            "application/pdf": "pdf", "text/plain": "txt", "video/mp4": "mp4", "audio/ogg": "ogg"}.get(mime or "", "bin")


# ============================================================== вложение из сообщения Telethon
def tg_info(m: Any) -> dict | None:
    """Метаданные вложения сообщения Telethon (без скачивания) или None, если вложения-файла нет."""
    f = getattr(m, "file", None)
    g = lambda o, k: getattr(o, k, None) if o is not None else None  # noqa: E731
    info: dict = {"name": g(f, "name"), "mime": g(f, "mime_type"), "size": g(f, "size"), "width": g(f, "width"),
                  "height": g(f, "height"), "duration": g(f, "duration")}
    if getattr(m, "photo", None):
        info.update(kind="photo", mime="image/jpeg")
    elif getattr(m, "sticker", None):
        info["kind"] = "sticker"
    elif getattr(m, "voice", None):
        info["kind"] = "voice"
    elif getattr(m, "video_note", None):
        info["kind"] = "video_note"
    elif getattr(m, "gif", None):
        info["kind"] = "gif"
    elif getattr(m, "video", None):
        info["kind"] = "video"
    elif getattr(m, "audio", None):
        info["kind"] = "audio"
    elif getattr(m, "document", None):
        mime = info.get("mime") or ""
        info["kind"] = "image" if mime in IMAGE_MIMES else "document"
    else:
        return None
    for k in ("size", "width", "height", "duration"):
        v = info.get(k)
        info[k] = int(v) if isinstance(v, (int, float)) else None
    if not isinstance(info.get("name"), str):
        info["name"] = None
    if not isinstance(info.get("mime"), str):
        info["mime"] = None
    return info


def photo_thumb(m: Any, max_px: int) -> str | None:
    """Тип размера фото (буква PhotoSize.type) — самый крупный, у которого сторона не больше max_px."""
    sizes = getattr(getattr(m, "photo", None), "sizes", None) or []
    best, best_side, smallest, small_side = None, 0, None, 10**9
    for s in sizes:
        w, h, t = getattr(s, "w", None), getattr(s, "h", None), getattr(s, "type", None)
        if not (w and h and t) or type(s).__name__ in ("PhotoStrippedSize", "PhotoPathSize", "PhotoCachedSize"):
            continue
        side = max(w, h)
        if side <= max_px and side > best_side:
            best, best_side = t, side
        if side < small_side:
            smallest, small_side = t, side
    return best or smallest


# ============================================================== папки и имена
def safe_name(s: str, limit: int = 60) -> str:
    s = _BAD.sub("_", unescape(str(s or ""))).strip().strip(".")
    s = re.sub(r"\s+", " ", s)[:limit].rstrip(" .")
    if not s or s.lower().split(".")[0] in _RESERVED:
        s = "_" + s
    return s


def media_root(cfg) -> Path:
    return cfg.path((cfg.media.get("folder") if hasattr(cfg, "media") else None) or "media")


def chat_folder(cfg, store: Store, chat_id: int, title: str) -> str:
    """Имя папки чата (запоминается: при переименовании чата старые файлы не теряются).

    В защищённой папке имя безликое (c + 10 знаков): по папкам не видно, какие это чаты."""
    from .secure import vault
    key = f"media_dir:{chat_id}"
    name = store.kv_get(key)
    if name and (not vault.protected(cfg.base_dir) or re.fullmatch(r"c[0-9a-f]{10}", name)):
        return name
    if vault.protected(cfg.base_dir):
        import hmac
        name = "c" + hmac.new(vault.key(), f"chat:{chat_id}".encode(), hashlib.sha256).hexdigest()[:10]
        store.kv_set(key, name)
        return name
    name = f"{safe_name(title or 'чат', 40)} [{chat_id}]"
    store.kv_set(key, name)
    return name


def rel_path(cfg, store: Store, row: dict, chat_title: str) -> str:
    """Путь файла относительно рабочей папки (через «/»)."""
    try:
        dt = parse_iso(row["date"]) if row.get("date") else datetime.now()
    except ValueError:
        dt = datetime.now()
    folder = (cfg.media.get("folder") if hasattr(cfg, "media") else None) or "media"
    ext = "jpg" if row.get("kind") == "photo" else ext_of(row.get("name"), row.get("mime"))
    base = f"{dt:%Y-%m-%d_%H-%M-%S}_m{row['msg_id']}"
    from .secure import vault
    if row.get("kind") != "photo" and row.get("name") and not vault.protected(cfg.base_dir):
        stem = row["name"].rsplit(".", 1)[0] if "." in row["name"] else row["name"]
        base += "_" + safe_name(stem, 50)
    return f"{folder}/{chat_folder(cfg, store, row['chat_id'], chat_title)}/{dt:%Y-%m}/{base}.{ext}"


def sha256_file(path: Path) -> str:
    """SHA-256 содержимого (у зашифрованного файла — расшифрованного)."""
    from .secure import files
    if files.is_sealed(path):
        return hashlib.sha256(files.read_bytes(path)).hexdigest()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sniff_mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit, k in (("ГБ", 2**30), ("МБ", 2**20), ("КБ", 2**10)):
        if n >= k:
            return f"{n / k:.1f} {unit}".replace(".0 ", " ")
    return f"{n} Б"


# ============================================================== как вложение выглядит в тексте для модели
def annotate(x: dict, *, full: bool = False, limit: int = 700) -> str:
    """Короткая пометка о вложении для переписки, которую читает модель.

    Изображение с описанием → «[фото: что на нём; текст: …]», без описания → «[фото, не описано, файл m12]»,
    тяжёлый файл → «[видео «x.mp4», 120 МБ — не скачан, файл m12]»."""
    kind = KIND_RU.get(x.get("kind") or "", "вложение")
    ref = f"файл m{x['id']}"
    name = f" «{x['name']}»" if x.get("name") and x.get("kind") not in ("photo",) else ""
    if x.get("described"):
        if x.get("kind") in IMAGE_KINDS:
            parts = [x.get("summary") or ""]
            if full and x.get("details"):
                parts.append(x["details"])
            if x.get("text"):
                parts.append("текст: " + str(x["text"]).replace("\n", " ⏎ "))
            body = "; ".join(p for p in parts if p)
            if len(body) > limit:
                body = body[:limit] + " …"
            return f"[{kind}{name}: {body}]"
        body = str(x.get("text") or x.get("summary") or "").replace("\r", " ").replace("\n", " ⏎ ")
        if len(body) > limit:
            body = body[:limit] + " …"
        return f"[{kind}{name}, {human_size(x.get('size'))}; содержимое: {body}]"
    if x.get("status") == "ok":
        if x.get("kind") in IMAGE_KINDS:
            st = "не описано" if x.get("desc_state") != "unsupported" else "формат не читается моделью"
            return f"[{kind}{name}, {st}, {ref}]"
        return f"[{kind}{name}, {human_size(x.get('size'))}, скачан, {ref}]"
    if x.get("kind") == "sticker":
        return "[стикер]"
    dur = f", {x['duration']} с" if x.get("duration") else ""
    return f"[{kind}{name}, {human_size(x.get('size'))}{dur} — не скачан, {ref}]"


# ============================================================== текст из лёгких документов (для поиска)
def extract_text(path: Path, name: str | None = None, max_chars: int = 20000) -> str | None:
    """Текст простых документов: txt/log/csv/json/xml…, docx, xlsx, pptx. Остальное — None."""
    ext = ext_of(name or path.name)
    from .secure import files
    try:
        if ext in TEXT_EXT:
            raw = files.read_bytes(path)[: max_chars * 4]
            for enc in ("utf-8-sig", "cp1251", "latin-1"):
                try:
                    txt = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if ext in ("html", "htm"):
                txt = re.sub(r"(?is)<(script|style).*?</\1>|<[^>]+>", " ", txt)
            return _squeeze(unescape(txt))[:max_chars] or None
        if ext in ("docx", "xlsx", "pptx"):
            with zipfile.ZipFile(files.open_read(path)) as z:
                names = z.namelist()
                if ext == "docx":
                    parts = [n for n in names if re.fullmatch(r"word/(document|header\d*|footer\d*)\.xml", n)]
                elif ext == "pptx":
                    parts = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
                else:
                    parts = [n for n in names if n == "xl/sharedStrings.xml"] + sorted(
                        n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
                out = []
                for n in parts:
                    xml = z.read(n).decode("utf-8", errors="replace")
                    xml = re.sub(r"</w:p>|</a:p>|</row>|</si>", "\n", xml)
                    out.append(unescape(re.sub(r"<[^>]+>", " ", xml)))
                    if sum(len(x) for x in out) > max_chars:
                        break
                return _squeeze("\n".join(out))[:max_chars] or None
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return None
    return None


def _squeeze(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


# ============================================================== файл-описание рядом с изображением
def sidecar_path(img: Path) -> Path:
    return img.with_suffix(img.suffix + ".md")


def write_sidecar(img: Path, *, desc: dict, sha: str, chat: str, date_local: str, sender: str, msg_id: int,
                  caption: str) -> None:
    from .secure import vault
    if vault.protected(img):  # описание и так в зашифрованной базе — открытых файлов рядом не оставляем
        return
    lines = [f"# {desc.get('summary') or 'Изображение'}", "",
             f"- Чат: {chat}", f"- Сообщение: #{msg_id}, {date_local}, {sender}",
             f"- Файл: {img.name}", f"- Тип: {desc.get('kind') or '—'}",
             f"- Важность: {desc.get('importance') or '—'}/5",
             f"- Описано: {desc.get('created_at', '')[:16].replace('T', ' ')} UTC, модель {desc.get('model') or '—'}",
             f"- sha256: {sha}", ""]
    if caption:
        lines += ["## Текст сообщения", "", caption.strip(), ""]
    lines += ["## Описание", "", (desc.get("details") or desc.get("summary") or "").strip(), ""]
    if desc.get("text"):
        lines += ["## Текст на изображении", "", str(desc["text"]).strip(), ""]
    img.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path(img).write_text("\n".join(lines), encoding="utf-8")


def read_sidecar(img: Path, sha: str) -> dict | None:
    """Описание из файла рядом с изображением (если база пересоздана) — только если совпадает sha256."""
    p = sidecar_path(img)
    from .secure import files
    try:
        t = files.read_text(p).replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if f"- sha256: {sha}" not in t:
        return None
    sec = dict(re.findall(r"(?ms)^## (.+?)\n\n(.*?)(?=^## |\Z)", t))
    head = re.search(r"^# (.+)$", t, re.M)
    kind = re.search(r"^- Тип: (.+)$", t, re.M)
    imp = re.search(r"^- Важность: (\d)", t, re.M)
    model = re.search(r"модель (.+)$", t, re.M)
    return {"summary": head.group(1).strip() if head else "", "details": sec.get("Описание", "").strip(),
            "text": sec.get("Текст на изображении", "").strip(), "kind": kind.group(1).strip() if kind else "",
            "importance": int(imp.group(1)) if imp else None, "model": model.group(1).strip() if model else ""}


# ============================================================== проверочная картинка (видит ли модель изображения)
_FONT = {  # 5×7
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def digit_png(digit: str, scale: int = 14) -> bytes:
    """PNG с одной крупной цифрой (чёрная на белом) — без сторонних библиотек."""
    glyph = _FONT[str(digit)]
    pad = 2 * scale
    w, h = 5 * scale + 2 * pad, 7 * scale + 2 * pad
    rows = []
    for y in range(h):
        gy = (y - pad) // scale
        row = bytearray([0])
        for x in range(w):
            gx = (x - pad) // scale
            on = 0 <= gy < 7 and 0 <= gx < 5 and glyph[gy][gx] == "1"
            row += b"\x00\x00\x00" if on else b"\xff\xff\xff"
        rows.append(bytes(row))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
