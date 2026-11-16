"""Обезличенный журнал ошибок OpsDigest.

Журнал можно переслать разработчику, поэтому в нём не должно быть ничего рабочего или личного:
- в журнал пишутся только события программы (что делалось, сколько, сколько секунд, какой класс ошибки и где в коде);
  текст переписки, названия чатов, имена, сводки и ответы модели в журнал не попадают вообще;
- всё, что всё-таки пишется, проходит через scrub(): пути с именем пользователя, адреса серверов, ссылки,
  IP, почты, телефоны, @имена, номера инцидентов и задач, длинные числа, ключи и токены заменяются метками;
- дополнительно вычищаются известные «секреты» из настроек (имя, телефон, ключи, адреса модели и прокси).

Файлы: %LOCALAPPDATA%\\OpsDigest\\logs\\opsdigest.log (+ launcher.log от OpsDigest.bat, pip.log от установки пакетов).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

_SECRETS: set[str] = set()
_CONFIGURED = False

MODULE_PREFIXES = ("opsdigest", "telethon", "urllib", "http", "asyncio", "json", "sqlite3", "socket", "ssl",
                   "python_socks", "concurrent", "threading", "subprocess", "os", "sys", "logging", "email",
                   "encodings", "importlib", "TelethonFakeTLS", "tomllib", "tomli", "zoneinfo", "pathlib", "re",
                   "collections", "typing", "errors", "rpcerrorlist", "client", "network", "connection")


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    d = Path(base) / "OpsDigest" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- очистка
def _path_repl(m: re.Match) -> str:
    p = m.group(0)
    norm = p.replace("/", "\\")
    low = norm.lower()
    for marker in ("site-packages\\", "\\lib\\python", "\\opsdigest\\", "\\tests\\"):
        i = low.find(marker)
        if i != -1:
            return norm[i + 1:] if marker.startswith("\\") else norm[i:]
    name = norm.rstrip("\\").rsplit("\\", 1)[-1]
    # имя файла оставляем, только если оно «наше» и безобидное
    if re.fullmatch(r"(?i)(config\.(json|toml)|telegram\.session|opsdigest\.db|[\w.-]+\.(py|log|bat|exe|whl|txt))", name):
        return f"<path>\\{name}"
    return "<path>"


SAFE_HOSTS = {"pypi.org", "python.org", "www.python.org", "files.pythonhosted.org", "my.telegram.org", "t.me",
              "telegram.org", "core.telegram.org", "api.anthropic.com", "console.anthropic.com", "github.com"}


def _host_repl(m: re.Match) -> str:
    s = m.group(0)
    if s.lower() in SAFE_HOSTS:
        return s
    first = s.split(".", 1)[0]
    if first in MODULE_PREFIXES or s.endswith((".py", ".json", ".toml", ".log", ".bat", ".exe", ".db", ".whl",
                                               ".txt", ".html", ".md", ".session", ".dll", ".pyw")):
        return s
    return "<host>"


_PATH_RULES: list[tuple[re.Pattern, object]] = [
    # пути: UNC, диски (сегменты могут содержать пробелы), домашние папки
    (re.compile(r"\\\\[^\s\\'\"]+\\[^\s'\"]*"), "<unc-path>"),
    (re.compile(r"(?i)\b[a-z]:[\\/](?:[^\\/\n'\"<>|*?]*[^\\/\s'\"<>|*?][\\/])*[^\\/\s'\"<>|*?]*"), _path_repl),
    (re.compile(r"(?<![\w.])/(?:home|Users|root|tmp|mnt|var|opt)/(?:[^/\n'\"<>]*[^/\s'\"<>]/)*[^/\s'\"<>]*"), _path_repl),
]

_RULES: list[tuple[re.Pattern, object]] = [
    # ключи и токены
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|api[_-]?hash|secret|password|pass|x-api-key)\b(\s*[:=]\s*|\s+)([^\s,;\"'}]+)"),
     lambda m: f"{m.group(1)}{m.group(2)}<secret>"),
    (re.compile(r"\bsk-ant-[\w-]+"), "<secret>"),
    # ссылки и прокси
    (re.compile(r"(?i)\b(?:tg|https?|socks[45]h?|ftp)://[^\s'\"<>]+"), "<url>"),
    # почта, @имена, телефоны
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"(?<![\w.])@[A-Za-z][\w]{3,}"), "<@user>"),
    (re.compile(r"\+\d[\d\s\-()]{8,}\d"), "<phone>"),
    # DOMAIN\user
    (re.compile(r"\b[A-Z][A-Z0-9-]{1,15}\\[A-Za-z][\w.-]{1,30}\b"), "<account>"),
    # IP
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I), "<ip>"),
    # имена хостов (кроме имён модулей Python и файлов)
    (re.compile(r"(?i)\b(?:[a-z0-9][a-z0-9-]*\.)+[a-z][a-z0-9-]{1,23}\b"), _host_repl),
    # номера инцидентов/задач
    (re.compile(r"(?i)\b(?:INC|IM|SD|RITM|REQ|CHG|PRB)[-_]?\d{4,}\b"), "<id>"),
    (re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b"), "<id>"),
    # длинные hex/base64 — ключи, хэши, секреты прокси
    (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "<secret>"),
    (re.compile(r"\b(?=[A-Za-z0-9_\-+/]*\d)(?=[A-Za-z0-9_\-+/]*[A-Za-z])[A-Za-z0-9_\-+/]{28,}={0,2}"), "<secret>"),
    # длинные числа: id чатов, телефоны, api_id, порты
    (re.compile(r"(?<![\w.])-?\d{5,}\b"), "<n>"),
]

# длинные цитаты — могли прийти из данных (но не пути и не имена модулей)
_QUOTE = re.compile(r"(['\"«])([^'\"»\n]{40,})(['\"»])")


def _quote_repl(m: re.Match) -> str:
    body = m.group(2)
    if "\\" in body or "/" in body or ".py" in body or body.startswith("<"):
        return m.group(0)
    return f"{m.group(1)}<…>{m.group(3)}"


def add_secrets(values) -> None:
    """Запомнить конкретные значения, которых не должно быть в журнале (имя, телефон, адреса, ключи)."""
    for v in values or []:
        if v is None:
            continue
        s = str(v).strip()
        if len(s) >= 3 and s.lower() not in ("auto", "local", "openai", "none", "true", "false", "lm-studio"):
            _SECRETS.add(s)
            if "/" in s or "\\" in s:
                _SECRETS.add(s.replace("/", "\\"))
                _SECRETS.add(s.replace("\\", "/"))


def add_secrets_from_config(raw: dict) -> None:
    """Собрать чувствительные значения из настроек (config.json / config.toml после загрузки)."""
    vals: list = []
    prof = raw.get("profile") or {}
    vals += [prof.get("name"), *(prof.get("aliases") or [])]
    tg = raw.get("telegram") or {}
    vals += [tg.get("api_id"), tg.get("api_hash"), tg.get("phone"), tg.get("proxy_bot")]
    vals.append((raw.get("update") or {}).get("token"))
    for p in tg.get("proxies") or []:
        vals.append(p)
        m = re.search(r"server=([^&\s]+)", str(p)) or re.search(r"@([^:/\s]+)", str(p)) or re.search(r"//([^:/\s]+)", str(p))
        if m:
            vals.append(m.group(1))
    for prof_ in ((raw.get("llm") or {}).get("profiles") or {}).values():
        for k in ("base_url", "url", "api_key", "model"):
            vals.append(prof_.get(k))
        for h in (prof_.get("headers") or {}).values():
            vals.append(h)
        m = re.search(r"//([^:/\s]+)", str(prof_.get("base_url") or prof_.get("url") or ""))
        if m and m.group(1) not in ("localhost", "127.0.0.1"):
            vals.append(m.group(1))
    for c in tg.get("chats") or []:
        vals.append(c.get("note"))
        vals.append(c.get("title"))
        for part in re.split(r"[.;\n]", str(c.get("description") or "")):  # описание чата — по фразам
            if len(part.strip()) >= 6:
                vals.append(part.strip())
        if not str(c.get("match", "")).lstrip("-").isdigit() and not str(c.get("match", "")).startswith("re:"):
            vals.append(c.get("match"))
    add_secrets(vals)


def scrub(text: str, quotes: bool = True) -> str:
    """Убрать из текста личное и секреты. quotes=False — не прятать длинные цитаты (журнал подбора формата Maverick:
    там цитируются ответы модели на проверочную картинку, а не переписка)."""
    if not text:
        return text
    s = str(text)
    for rx, repl in _PATH_RULES:
        s = rx.sub(repl, s)
    for rx, repl in _RULES:
        s = rx.sub(repl, s)
    # конкретные значения из настроек и имена пользователя/компьютера — целыми словами
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret and secret in s:
            s = re.sub(r"(?<![\w])" + re.escape(secret) + r"(?![\w])", "<скрыто>", s)
    for env in ("USERNAME", "COMPUTERNAME", "USERDOMAIN", "USER", "HOSTNAME"):
        v = os.environ.get(env)
        if v and len(v) >= 3 and v.lower() not in ("root", "user", "admin"):
            s = re.sub(r"(?<![\w])" + re.escape(v) + r"(?![\w])", f"<{env.lower()}>", s, flags=re.I)
    return _QUOTE.sub(_quote_repl, s) if quotes else s


class ScrubFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


# ---------------------------------------------------------------- настройка
def setup(component: str = "app", filename: str = "opsdigest.log") -> logging.Logger:
    """Включить запись журнала в файл (один раз на процесс). Сторож пишет в свой файл: два процесса
    не должны переименовывать один и тот же журнал при ротации."""
    global _CONFIGURED
    log = logging.getLogger("opsdigest")
    if _CONFIGURED:
        return log
    _CONFIGURED = True
    log.setLevel(logging.INFO)
    try:
        h = logging.handlers.RotatingFileHandler(log_dir() / filename, maxBytes=1_000_000, backupCount=3,
                                                 encoding="utf-8")
        h.setFormatter(ScrubFormatter("%(asctime)s %(levelname)-5s [" + component + "] %(name)s: %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
    except OSError:
        pass
    log.propagate = False

    def hook(exc_type, exc, tb):
        log.error("необработанная ошибка", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook
    return log


def get(name: str = "") -> logging.Logger:
    return logging.getLogger("opsdigest" + (f".{name}" if name else ""))


def err_info(e: BaseException) -> str:
    """Короткое безопасное описание ошибки: класс, HTTP-код, место в нашем коде. Без тела ответа сервера."""
    parts = [type(e).__name__]
    status = getattr(e, "status", None) or getattr(e, "code", None)
    if isinstance(status, int):
        parts.append(f"status={status}")
    safe = getattr(e, "safe", None)
    if safe:
        parts.append(str(safe))
    tb = e.__traceback__
    where = []
    while tb is not None:
        fn = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/opsdigest/" in fn:
            where.append(f"{fn.rsplit('/opsdigest/', 1)[1]}:{tb.tb_lineno}")
        tb = tb.tb_next
    if where:
        parts.append("at " + " > ".join(where[-4:]))
    return " ".join(parts)


def frames(e: BaseException, limit: int = 6) -> str:
    """Только строки стека (файл, строка, код) — без текста исключения, где могут быть данные."""
    import traceback
    return "".join(traceback.format_tb(e.__traceback__, limit=-limit)).rstrip()


def tail(path: Path, lines: int) -> list[str]:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return data[-lines:]
