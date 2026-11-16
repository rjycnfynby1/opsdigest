"""Обезличивание и смягчение текста перед отправкой во внешнюю модель (Maverick, Claude и любые не локальные).

Как работает (всё локально, без модели):
  1. имена людей → коды «Ч417», телефоны → «Тел…», почты → «Почта…» — в каждом запросе к модели свои случайные номера;
  2. мат и оскорбления → нейтральные слова с тем же смыслом (soften);
  3. страховка: ещё раз ищем известные фамилии, ники и матерные корни;
  4. в ответе модели коды заменяются обратно на имена (в нужном падеже).

Подключение — одной строкой: privacy.attach(llm, cfg, store). Клиент LLM сам обезличивает каждый запрос
(LLMClient.complete) и восстанавливает ответ. Локальные модели (localhost) не трогаем.
"""
from __future__ import annotations

import collections
import re
import threading
import time
from urllib.parse import urlparse

from .pseudo import Session
from .registry import Registry, build_registry
from .soften import soften

__all__ = ["Session", "Registry", "build_registry", "soften", "applies", "attach", "Privacy", "Request", "recent"]

_RECENT: collections.deque = collections.deque(maxlen=6)   # последние запросы как их видит модель (только в памяти)
_RLOCK = threading.Lock()


def recent() -> list[dict]:
    with _RLOCK:
        return list(_RECENT)


def _local_url(u: str) -> bool:
    try:
        host = (urlparse(u).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


def applies(llm, cfg=None) -> bool:
    """Нужно ли обезличивать запросы этой модели: всё, кроме локальных (LM Studio, Ollama на этом компьютере)."""
    mode = str(((cfg.get("privacy") if cfg is not None and hasattr(cfg, "get") else None) or {}).get(
        "pseudonymize", "external")).lower()
    prof = getattr(llm, "profile", {}) or {}
    if prof.get("privacy") is False or mode in ("off", "false", "0", "no"):
        return False
    if mode in ("all", "always"):
        return True
    t = str(prof.get("type") or "").lower()
    if t == "openai":
        return not _local_url(str(prof.get("base_url") or ""))
    if t == "http_json":
        return not _local_url(str(prof.get("url") or ""))
    return True  # maverick, anthropic, command (Claude Code) — данные уходят с компьютера


_STICKY: dict = {}
_STICKY_LOCK = threading.Lock()


class Request:
    """Один запрос к модели: своя карта кодов со случайными номерами (в следующем запросе — другие)."""

    def __init__(self, owner: "Privacy", session: Session):
        self.owner = owner
        self.session = session

    def outgoing(self, text: str) -> str:
        out = self.session.apply(text)
        if self.owner.do_soften:
            out = soften(out)
        return self.session.guard(out)

    def incoming(self, text: str, json_safe: bool = True) -> str:
        return self.session.restore(text, json_safe=json_safe)

    def preamble(self) -> str:
        """Пояснение модели в начале системной подсказки (своё в каждом запросе)."""
        me = f" {self.session.me_code} — сам пользователь." if self.session.me_code else ""
        mm = self.session.maybe_me_codes()
        if mm:
            me += (" " + ", ".join(f"{c} — «{n}»" for c, n in mm) +
                   ": так в тексте назвали человека только по имени, а так зовут и пользователя, и могут звать его "
                   "тёзок. К пользователю это относится, только если это ясно из контекста.")
        soft = " Грубые слова заменены нейтральными, [резкий отказ] и [грубое слово] — пометки на их месте." \
            if self.owner.do_soften else ""
        return (f"Имена людей в тексте заменены кодами вида Ч123: один код — один человек.{me} Пиши коды как есть: "
                f"не склоняй, не расшифровывай, не придумывай новых.{soft}\n\n")


class Privacy:
    """То, что клиент LLM вызывает на каждом запросе: begin() → outgoing(...) → incoming(...)."""

    def __init__(self, registry: Registry, me_label: str = "", do_soften: bool = True, randomize: bool = True):
        self.reg = registry
        self.me_label = me_label
        self.do_soften = do_soften
        self.randomize = randomize
        self.calls = 0
        self.persons: set[str] = set()   # сколько разных людей обезличено за задание (по ключу, не по коду)
        self.stats = {"requests": 0, "mentions": 0, "contacts": 0, "guard": 0}
        self.session: Session | None = None   # карта последнего запроса (для проверок)
        self._lock = threading.Lock()

    def begin(self, sticky: str | None = None) -> Request:
        """Карта кодов запроса. sticky — ключ беседы (Maverick «как на портале»): та же карта на всю беседу, пока
        программа открыта, — иначе Maverick в своей истории видел бы одного человека под разными кодами."""
        if sticky:
            with _STICKY_LOCK:
                s = _STICKY.get(sticky)
                if s is None:
                    s = _STICKY[sticky] = Session(self.reg, me_label=self.me_label, randomize=self.randomize)
                    while len(_STICKY) > 50:
                        _STICKY.pop(next(iter(_STICKY)))
        else:
            s = Session(self.reg, me_label=self.me_label, randomize=self.randomize)
        self.session = s
        return Request(self, s)

    def remember(self, req: Request, system: str, user: str) -> None:
        s = req.session
        with self._lock:
            self.calls += 1
            self.stats["requests"] += 1
            self.stats["mentions"] += s.stats["people"]
            self.stats["contacts"] += s.stats["contacts"]
            self.stats["guard"] += s.stats["guard"]
            self.persons |= {p.key for p in s.people.values() if not p.is_me}
        with _RLOCK:
            _RECENT.append({"at": time.strftime("%d.%m %H:%M:%S"), "system": system[:6000], "user": user[:60000],
                            "map": len(s.people)})

    def stats_line(self) -> str:
        st = self.stats
        return (f"запросов {st['requests']} (в каждом свои случайные коды), людей {len(self.persons)}, "
                f"упоминаний {st['mentions']}, контактов {st['contacts']}"
                + (f", страховка {st['guard']}" if st["guard"] else ""))


def attach(llm, cfg, store=None, registry: Registry | None = None, me_label: str = ""):
    """Включить обезличивание для клиента LLM (если модель не локальная). Возвращает Privacy или None."""
    if llm is None or not applies(llm, cfg):
        if llm is not None:
            llm.privacy = None
        return None
    existing = getattr(llm, "privacy", None)
    if isinstance(existing, Privacy):
        return existing
    reg = registry or build_registry(cfg, store)
    prof = (cfg.get("profile") if hasattr(cfg, "get") else {}) or {}
    label = me_label or (reg.me.display if reg.me else "") or prof.get("name") or ""
    priv = (cfg.get("privacy") if hasattr(cfg, "get") else {}) or {}
    p = Privacy(reg, me_label=_first_word(label), do_soften=priv.get("soften", True) is not False,
                randomize=priv.get("random_codes", True) is not False)
    llm.privacy = p
    return p


def _first_word(name: str) -> str:
    m = re.match(r"\s*([А-Яа-яЁёA-Za-z\-]+)", name or "")
    return m.group(1) if m else (name or "")
