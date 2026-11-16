"""Тест для задачи локального агента: Markdown-версия сводки (opsdigest/render/markdown.py).
Пока модуля нет — тест пропускается."""
import json
from pathlib import Path

import pytest

md_mod = pytest.importorskip("opsdigest.render.markdown")

SAMPLE = Path(__file__).resolve().parent.parent / "opsdigest" / "sample_digest.json"


def test_markdown_sections():
    d = json.loads(SAMPLE.read_text(encoding="utf-8"))
    md = md_mod.render_markdown(d)
    assert md.startswith("# Сводка за день")
    for h in ("## Нужна реакция", "## Аварии и инциденты", "## По чатам"):
        assert h in md
    first = d["overview"]["top"][0]
    assert f"1. **{first['title']}**" in md
    assert "> " + first["draft"][:30] in md
    assert "[#1010](https://t.me/c/1000000001/1010)" in md
    for c in d["chats"]:
        assert f"### {c['title']}" in md
    assert "[Ответить]" in md
    assert f"_{d['chats'][0]['count']} сообщ." in md  # число сообщений берётся из поля count
    assert "<b>" not in md and "<div" not in md
