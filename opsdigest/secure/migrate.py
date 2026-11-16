"""Включение защиты в рабочей папке, где уже есть данные: всё, что лежит открытым, — зашифровать.

  config.json, сессия Telegram, кеши (прокси, VPN) — на месте;
  база — переносится в зашифрованный снимок (opsdigest.db → opsdigest.db.odx), открытая удаляется;
  отчёты (reports/*.html|json|md) — на месте;
  вложения — в безликие папки (по именам не видно чатов), файлы-описания .md рядом с картинками удаляются.
Можно прервать и запустить снова: уже зашифрованное пропускается.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import files, memdb, vault


def migrate_workspace(cfg, progress=print, cancel=lambda: False) -> dict:
    vault.key()
    ws = Path(cfg.base_dir)
    st = {"files": 0, "reports": 0, "media": 0, "media_skipped": 0, "removed": 0}
    for rel in ("config.json", "data/telegram.session", "data/vpn_servers.json", "data/proxies.txt"):
        if files.encrypt_in_place(ws / rel):
            st["files"] += 1
    progress(f"Настройки и сессия Telegram: зашифровано файлов {st['files']}.")
    # база: Store сам переносит открытую базу в память, снимок сохраняем сразу
    from ..storage import Store
    store = Store(cfg.path(cfg["storage"]["path"]))
    memdb.flush_all(force=True)
    progress("База переписки зашифрована.")
    moved: dict[str, str] = {}
    st.update(_media(cfg, store, progress, cancel, moved))
    memdb.flush_all(force=True)
    out_dir = cfg.path(cfg.digest.get("output_dir") or "reports")
    if out_dir.is_dir():
        for p in sorted(out_dir.iterdir()):
            if p.suffix.lower() not in (".html", ".json", ".md") or not p.is_file():
                continue
            text = files.read_text(p)
            new = _relink(text, moved, cfg, out_dir) if moved else text
            if new != text or not files.is_sealed(p):
                files.write_text(p, new, encrypt=True)
                st["reports"] += 1
    progress(f"Отчёты: зашифровано {st['reports']}.")
    progress("Готово: файлы в рабочей папке открываются только в OpsDigest." if not cancel() else "Остановлено.")
    return st


def _quoted(cfg, out_dir: Path, rel: str) -> str:
    """Ссылка на файл из отчёта — так же, как её пишет digest._report_images."""
    import urllib.parse
    r = os.path.relpath(cfg.path(rel), out_dir).replace("\\", "/")
    return "/".join(urllib.parse.quote(part) for part in r.split("/"))


def _relink(text: str, moved: dict[str, str], cfg, out_dir: Path) -> str:
    """Старые отчёты ссылаются на старые имена папок вложений — поправить ссылки."""
    for old, new in moved.items():
        try:
            text = text.replace(_quoted(cfg, out_dir, old), _quoted(cfg, out_dir, new))
        except ValueError:
            pass
        text = text.replace(f'"path": "{old}"', f'"path": "{new}"')
    return text


def _media(cfg, store, progress, cancel, moved: dict[str, str]) -> dict:
    from ..media import media_root, rel_path, sidecar_path
    st = {"media": 0, "media_skipped": 0, "removed": 0}
    rows = store.db.execute("SELECT id, chat_id, msg_id, date, kind, name, mime, path FROM media "
                            "WHERE status='ok' AND path IS NOT NULL ORDER BY id").fetchall()
    total = len(rows)
    if total:
        progress(f"Вложения: {total} файлов…")
    for i, r in enumerate(rows, 1):
        if cancel():
            break
        old = cfg.path(r["path"])
        side = sidecar_path(old)
        if not old.is_file():
            st["media_skipped"] += 1
            continue
        new_rel = rel_path(cfg, store, dict(r), "")
        new = cfg.path(new_rel)
        if new_rel == r["path"] and files.is_sealed(old):
            continue
        data = files.read_bytes(old)
        files.write_bytes(new, data, encrypt=True)
        store.db.execute("UPDATE media SET path=? WHERE id=?", (new_rel, r["id"]))
        if new_rel != r["path"]:
            moved[r["path"]] = new_rel
        if new != old:
            try:
                old.unlink()
            except OSError:
                pass
        for extra in (side, old.with_name(old.name + ".md")):
            if extra.exists():
                try:
                    extra.unlink()
                    st["removed"] += 1
                except OSError:
                    pass
        st["media"] += 1
        if i % 200 == 0:
            store.db.commit()
            progress(f"  {i} из {total}")
    store.db.commit()
    root = media_root(cfg)
    av = root / "avatars"   # аватарки чатов и людей (avatars.py) — зашифровать на месте
    if av.is_dir():
        for p in av.iterdir():
            if p.is_file() and files.encrypt_in_place(p):
                st["media"] += 1
    if root.is_dir():  # открытые описания .md и опустевшие папки со старыми именами чатов
        for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            try:
                if p.is_file() and p.suffix.lower() == ".md":
                    p.unlink()
                    st["removed"] += 1
                elif p.is_dir() and not any(p.iterdir()):
                    os.rmdir(p)
            except OSError:
                pass
    progress(f"Вложения: зашифровано {st['media']}" + (f", не найдено на диске {st['media_skipped']}"
                                                         if st["media_skipped"] else ""))
    return st
