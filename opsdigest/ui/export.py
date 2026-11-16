"""Выборочный экспорт отчёта: расшифровать один отчёт в «Загрузки/OpsDigest» (картинки — внутрь HTML).

Экспортированный файл открыт — это осознанный выбор пользователя (переслать, распечатать, приложить к письму).
"""
from __future__ import annotations

import base64
import re
import urllib.parse
from html import unescape
from pathlib import Path

from ..secure import files

REPORT_RE = re.compile(r"^[\w\-.]+\.html$")
MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
        "bmp": "image/bmp"}


def export_dir() -> Path:
    d = Path.home() / "Downloads" / "OpsDigest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_report(ws: Path, name: str, cfg=None) -> dict:
    if not REPORT_RE.match(name or ""):
        raise ValueError("нет такого отчёта")
    out_dir = (cfg.path(cfg.digest.get("output_dir") or "reports") if cfg is not None else ws / "reports")
    src = out_dir / name
    if not src.is_file():
        raise ValueError("нет такого отчёта")
    html = files.read_text(src)

    def load(url: str) -> bytes | None:
        url = unescape(url)
        if url.startswith(("data:", "http:", "https:", "#")):
            return None
        p = (out_dir / urllib.parse.unquote(url)).resolve()
        try:
            p.relative_to(ws.resolve())
            return files.read_bytes(p)
        except (ValueError, OSError):
            return None

    def inline(m: re.Match) -> str:
        data = load(m.group(3))
        if data is None:
            return m.group(0)
        mime = MIME.get(Path(urllib.parse.unquote(unescape(m.group(3)))).suffix.lower().lstrip("."),
                        "application/octet-stream")
        return f'{m.group(1)}{m.group(2)}data:{mime};base64,{base64.b64encode(data).decode("ascii")}{m.group(2)}'
    html = re.sub(r"(<img[^>]*?\ssrc=)(['\"])([^'\"]+)\2", inline, html)
    html = re.sub(r"(<a\s[^>]*?href=)(['\"])(\.\./[^'\"]+)\2", r"\1\2#\2", html)  # ссылки на файлы папки — не нужны
    target = export_dir() / name
    target.write_text(html, encoding="utf-8")
    res = {"html": str(target)}
    md = src.with_suffix(".md")
    if md.is_file():
        (export_dir() / md.name).write_text(files.read_text(md), encoding="utf-8")
        res["md"] = str(export_dir() / md.name)
    return res
