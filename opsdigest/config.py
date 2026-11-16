"""Загрузка настроек: config.toml + .env + значения по умолчанию.

Окно пишет config.json (стандартная библиотека). config.toml — для ручной настройки (tomllib, 3.11+).
Секреты (api_hash, токены, прокси) лучше держать в .env, а в config.toml писать "${ИМЯ}".
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "profile": {
        "name": "",
        "role": "",
        "aliases": [],
        "responsibilities": "",
        "ignore_topics": [],
        "competences": [],
    },
    "telegram": {
        "api_id": "",
        "api_hash": "",
        "phone": "",
        "session": "data/telegram",
        "proxies": [],          # ссылки на прокси; плюс TG_PROXY из .env и data/proxies.txt
        "try_direct": False,    # пробовать ли без прокси, если ни один не подошёл
        "connect_timeout": 15,
        "proxy_bot": "",        # экспериментально: бот, выдающий прокси (@имя)
        "proxy_bot_command": "/start",
        "first_sync_days": 2,
        "max_messages_per_chat": 400,
        "include_archived": False,
        "skip_muted": False,
        "defaults": {"private": "high", "group": "normal", "channel": "low", "bot": "ignore"},
        "chats": [],
    },
    "llm": {
        "provider": "local",
        "profiles": {
            "local": {
                "type": "openai",
                "base_url": "http://localhost:1235/v1",
                "api_key": "lm-studio",
                "model": "auto",
                "max_context_tokens": 32768,
                "timeout": 900,
            }
        },
    },
    "digest": {
        "timezone": "local",
        "output_dir": "reports",
        "chunk_chars": 0,  # 0 = посчитать из max_context_tokens модели
        "max_output_tokens": 4096,
        "top_actions": 10,
        "max_messages_per_chat": 600,
        "send_to_saved": False,
        # общая картина: сводка Telegram сверяется с почтой, ITSM и вики через плагины Maverick (раздел «Сводка»)
        "sources": {"mail": False, "itsm": False, "wiki": False},
        "cross_private": False,   # личные переписки в сверку с почтой/ITSM не идут (их нет даже в обезличенном виде)
    },
    # плагины корпоративного агента Maverick: подключаются в его чате; программа проверяет, что они отвечают
    "plugins": {
        "check_minutes": 30,      # сколько минут верить последней удачной проверке подключения
        "mail": {"enabled": True, "name": "", "hint": "", "body": {}},
        "itsm": {"enabled": True, "name": "", "hint": "", "body": {}, "cache_hours": 6},
        "wiki": {"enabled": True, "name": "", "hint": "", "body": {}, "url": ""},
    },
    # глоссарий пополняется сам: термины из всего, что разбирает программа, — в очередь, очередь — моделью
    "glossary": {"auto_collect": True, "auto_process": True, "process_batch": 6, "use_wiki": True},
    "entities": {
        "patterns": {
            "incident": r"\b(?:INC|INS|IM|SD|RITM|REQ)[-_]?\d{5,}\b",
            "change": r"\bCHG[-_]?\d{5,}\b",
            "problem": r"\b(?:PRB|PM)[-_]?\d{5,}\b",
            "task": r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b",
        },
        "ignore": [],
    },
    "privacy": {"redact": []},
    "storage": {"path": "data/opsdigest.db"},
    "media": {
        "download": True,          # скачивать изображения и лёгкие файлы вместе с перепиской
        "folder": "media",         # папка в рабочей папке: media/<чат>/<ГГГГ-ММ>/файл + описание рядом (.md)
        "photo_max_px": 1280,      # какой размер фото брать (Telegram хранит несколько; 1280 — читаемые скриншоты)
        "image_max_mb": 10,        # изображения, отправленные файлом (PNG-скриншоты), — до этого размера
        "light_max_mb": 3,         # документы до этого размера скачиваются, крупнее — только ссылка (докачка по запросу)
        "never_download": ["exe", "msi", "dll", "scr", "com", "apk", "iso", "img", "bin", "vhd", "vhdx", "vmdk"],
        "vision_provider": "",     # LLM-профиль для описания изображений; пусто — тот же, что для сводки
        "describe_workers": 2,     # сколько изображений описывать одновременно
        "describe_in_digest": True,  # новые изображения периода обязательно описываются перед сводкой
        "auto_describe_max": 8,    # сколько изображений модель может решить описать при ответе на вопрос
        "fetch_max_mb": 200,       # больше этого по запросу не докачивается
    },
}

PRIORITIES = ("high", "normal", "low", "ignore")
KINDS = ("chat", "incident", "alerts", "news")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv(path: Path) -> None:
    """Минимальный разбор .env: KEY=VALUE, # комментарии. Уже заданные переменные окружения не трогаем."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def _expand(obj: Any) -> Any:
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    return obj


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k == "profiles" and isinstance(v, dict):
            out[k] = copy.deepcopy(v)  # профили LLM целиком из файла пользователя
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict, base_dir: Path):
        self.data = data
        self.base_dir = base_dir

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def profile(self) -> dict:
        return self.data["profile"]

    @property
    def telegram(self) -> dict:
        return self.data["telegram"]

    @property
    def digest(self) -> dict:
        return self.data["digest"]

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p

    def llm_profile(self, name: str | None = None) -> tuple[str, dict]:
        llm = self.data["llm"]
        name = name or llm.get("provider") or "local"
        profiles = llm.get("profiles") or {}
        if name not in profiles:
            raise KeyError(f"LLM-профиль '{name}' не найден в config.toml (есть: {', '.join(profiles) or '—'})")
        return name, profiles[name]

    def _rule_index(self):
        """Правила чатов, разобранные один раз: номер → (позиция, правило); остальные — по порядку."""
        idx = getattr(self, "_ridx", None)
        if idx is None:
            by_id: dict[int, tuple[int, dict]] = {}
            others: list[tuple[int, str, dict]] = []
            for pos, r in enumerate(self.telegram.get("chats") or []):
                ms = str(r.get("match", "")).strip() if isinstance(r, dict) else ""
                if not ms:
                    continue
                if re.fullmatch(r"-?\d+", ms):
                    by_id.setdefault(int(ms), (pos, r))
                else:
                    others.append((pos, ms, r))
            idx = self._ridx = (by_id, others)
        return idx

    def chat_rule(self, title: str, username: str | None, chat_id: int, kind: str) -> dict:
        """Приоритет и режим чата. kind: private|group|channel|bot.

        Правило из [[telegram.chats]] подходит, если match совпал с id, @username, регуляркой (re:...)
        или подстрокой названия (без учёта регистра). Первое подходящее правило побеждает.
        description — описание пользователя, ai_description — часть от ИИ (ниже); note для модели — обе вместе.
        topics — настройки тем форума: {"<номер темы>": {"title", "priority", "description", "ai_description",
        "exclude"}}.
        """
        tg = self.telegram
        base = (tg.get("defaults") or {}).get(kind, "normal")
        rule = {"priority": base, "kind": "chat", "note": "", "explicit": False, "excluded": base == "ignore",
                "rank": base if base != "ignore" else "low", "title": "", "description": "", "ai": "", "ai_at": "",
                "topics": {}, "by": ""}
        t = (title or "").lower()
        u = (username or "").lower().lstrip("@")
        by_id, others = self._rule_index()
        hit_pos, hit = (by_id[chat_id] if chat_id in by_id else (10**9, None))
        for pos, ms, r in others:
            if pos > hit_pos:
                break
            if ms.startswith("@"):
                ok = bool(u) and ms[1:].lower() == u
            elif ms.startswith("re:"):
                try:
                    ok = re.search(ms[3:], title or "", re.I) is not None
                except re.error:
                    ok = False
            else:
                ok = ms.lower() in t
            if ok:
                hit_pos, hit = pos, r
                break
        if hit is not None:
            r = hit
            if r.get("priority") in PRIORITIES:
                rule["priority"] = r["priority"]
            if r.get("kind") in KINDS:
                rule["kind"] = r["kind"]
            # description — описание чата от пользователя (роль, за чем следить, что игнорировать).
            # note — старое поле: в config.toml это подсказка для модели, а окно раньше писало туда название чата.
            ms = str(r.get("match", ""))
            desc = r.get("description")
            if desc is None and r.get("note") and not re.fullmatch(r"-?\d+", ms):
                desc = r.get("note")
            rule["description"] = str(desc or "").strip()
            rule["ai"] = str(r.get("ai_description") or "").strip()
            rule["ai_at"] = str(r.get("ai_at") or "")
            rule["topics"] = r.get("topics") if isinstance(r.get("topics"), dict) else {}
            rule["title"] = str(r.get("title") or "")
            rule["explicit"] = True
            rule["by"] = "id" if re.fullmatch(r"-?\d+", ms) else ms
            rule["excluded"] = bool(r.get("exclude")) or rule["priority"] == "ignore"
            if rule["priority"] != "ignore":
                rule["rank"] = rule["priority"]
            if rule["excluded"]:
                rule["priority"] = "ignore"
        rule["note"] = rule["description"] + (("\n" if rule["description"] else "") +
                                              f"Анализ ИИ: {rule['ai']}" if rule["ai"] else "")
        return rule

    @property
    def media(self) -> dict:
        return self.data.get("media") or DEFAULTS["media"]


def load_config(path: str | Path = "config.toml") -> Config:
    """config.toml (ручная настройка) или config.json (его пишет окно настройки) + .env рядом."""
    path = Path(path).resolve()
    base = path.parent
    load_dotenv(base / ".env")
    user: dict = {}
    if path.exists():
        if path.suffix.lower() == ".json":
            import json

            from .secure import files
            user = json.loads(files.read_text(path, encoding="utf-8-sig") or "{}")
        else:
            try:
                import tomllib  # Python 3.11+
            except ModuleNotFoundError:  # Python 3.10 — config.toml нужен только для ручной настройки
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ModuleNotFoundError:
                    raise SystemExit("config.toml на Python 3.10 требует пакет tomli (pip install tomli); "
                                     "окну OpsDigest он не нужен — там config.json") from None
            with open(path, "rb") as f:
                user = tomllib.load(f)
    data = _expand(_merge(DEFAULTS, user))
    return Config(data, base)
