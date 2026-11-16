"""Чистая копия программы для коллег — без личного: журнала разработки, воркера локального агента, истории git.

    python tools/share_export.py ПАПКА [--repo владелец/репозиторий]

Кладёт в ПАПКУ файлы из git (ls-files), кроме EXCLUDE, и правит то, что ссылается на личное:
  • docs/ARCHITECTURE.md — без раздела «Разработка с локальным агентом», README — без строки про local_ai/;
  • --repo — откуда кнопка «⟳ Обновить» у коллег берёт обновления (по умолчанию — этот репозиторий).
ПАПКА — рабочая копия общего репозитория (git clone …): после экспорта там `git add -A && git commit && git push`.
Файлы, которых больше нет в программе, из ПАПКИ удаляются (кроме .git).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE = ("docs/HANDOFF.md", "local_ai/", "tools/share_export.py")


def files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8")
    return [f for f in out.split("\0") if f and not any(f == x or f.startswith(x) for x in EXCLUDE)]


def patch(rel: str, text: str, repo: str | None) -> str:
    if rel == "docs/ARCHITECTURE.md":
        text = re.sub(r"\n## Разработка с локальным агентом.*", "\n", text, flags=re.S)
    if rel == "README.md":
        text = "\n".join(line for line in text.splitlines() if "local_ai/" not in line) + "\n"
    if rel == "opsdigest/updater.py" and repo:
        text = re.sub(r'^DEFAULT_REPO = ".*"$', f'DEFAULT_REPO = "{repo}"', text, flags=re.M)
    if rel == "tools/voice_pack.py" and repo:
        text = re.sub(r'("--repo", default=)"[^"]*"', rf'\1"{repo}"', text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dest")
    ap.add_argument("--repo", default="", help="владелец/репозиторий для обновлений у коллег")
    a = ap.parse_args(argv)
    dest = Path(a.dest).resolve()
    if dest == ROOT or ROOT in dest.parents:
        print("Папка должна быть вне программы.")
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    keep = set(files())
    for rel in keep:
        src, dst = ROOT / rel, dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in (".py", ".md", ".html", ".js", ".css", ".toml", ".txt", ".bat", ".json", ".ps1"):
            try:
                dst.write_text(patch(rel, src.read_text(encoding="utf-8"), a.repo or None), encoding="utf-8", newline="")
                continue
            except UnicodeDecodeError:
                pass
        shutil.copy2(src, dst)
    removed = 0
    for p in sorted(dest.rglob("*"), reverse=True):   # лишнее от прошлых выгрузок
        rel = p.relative_to(dest).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if p.is_file() and rel not in keep:
            p.unlink()
            removed += 1
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    print(f"Готово: {len(keep)} файлов в {dest}" + (f", удалено старых: {removed}" if removed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
