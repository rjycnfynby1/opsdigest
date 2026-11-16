"""Сквозные упоминания: номера инцидентов, задач, изменений в тексте.

Это «клей» между источниками: сейчас — между разными чатами Telegram, позже — с ITSM, трекером и почтой
(тот же номер INC… в чате и в консоли ITSM = одна сущность).
"""
from __future__ import annotations

import re
from typing import Iterable

from .storage import Message


class EntityExtractor:
    def __init__(self, cfg_entities: dict):
        self.patterns: list[tuple[str, re.Pattern]] = []
        for etype, pat in (cfg_entities.get("patterns") or {}).items():
            if pat:
                self.patterns.append((etype, re.compile(pat)))
        self.ignore = {s.upper() for s in (cfg_entities.get("ignore") or [])}

    def find(self, text: str) -> list[tuple[str, str]]:
        """[(тип, значение)] без повторов. Более специфичные шаблоны (incident, change) идут раньше task."""
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for etype, rx in self.patterns:
            for m in rx.finditer(text or ""):
                v = m.group(0).upper().replace("_", "-") if etype == "task" else m.group(0).upper()
                if v in seen or v in self.ignore:
                    continue
                # «UTF-8», «SHA-256», «ISO-8601» и т.п. — не задачи
                if etype == "task" and re.fullmatch(r"(UTF|SHA|ISO|TLS|SSL|HTTP|MD|COVID|RFC|IPV|X)-\d+", v):
                    continue
                seen.add(v)
                out.append((etype, v))
        return out

    def rows(self, msgs: Iterable[Message]) -> list[tuple[str, str, str, int, int, str]]:
        rows = []
        for m in msgs:
            for etype, value in self.find(m.text):
                rows.append((etype, value, m.source, m.chat_id, m.msg_id, m.date))
        return rows
