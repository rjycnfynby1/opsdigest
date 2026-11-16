"""Общая часть клиентов LLM: HTTP через стандартную библиотеку, повторы, разбор JSON из ответа."""
from __future__ import annotations

import base64
import http.client
import json
import re
import secrets
import ssl
import urllib.error
import urllib.request
from typing import Any

from . import inflight

BUSY_WAITS = 8   # столько раз подождать, если сервер модели занят (Maverick: идёт другой запрос), ~5 минут


class LLMError(RuntimeError):
    pass


HTTP_HINTS = {
    401: "ключ неверный, отозван или вставлен не целиком",
    403: "нет прав — обратитесь к администратору сервиса",
    404: "адрес API не найден — проверьте адрес",
    405: "этот адрес не принимает такие запросы — проверьте адрес и тип API",
    413: "запрос слишком большой — уменьшите размер порции (digest.chunk_chars)",
    422: "сервер не принял формат запроса",
    429: "превышен лимит запросов — подождите или поднимите лимит ключа",
    500: "ошибка на сервере модели — повторите позже",
    502: "сервер модели временно недоступен — повторите позже",
    503: "сервер модели временно недоступен — повторите позже",
    504: "сервер модели не успел ответить — повторите позже",
}


Image = tuple  # (байты, mime) — изображение для модели
# сервер занят другим запросом (Maverick ведёт одну сессию за раз): подождать и повторить
BUSY_RE = re.compile(r"(?i)занят|busy|дожд|wait for|in progress|already (running|processing)|сесси[яи] (уже )?(иде|активн|занят)"
                     r"|another (request|session)|concurren|one session|одн[ау] сесси|try again later|повторите позже")


class LLMClient:
    """complete(system, user[, images]) -> текст ответа. images — список (байты, mime) для моделей, читающих картинки."""

    def __init__(self, name: str, profile: dict):
        self.name = name
        self.profile = profile
        self.timeout = float(profile.get("timeout", 600))
        self.retries = int(profile.get("retries", 2))
        self.temperature = float(profile.get("temperature", 0.2))
        # external: данные уходят за пределы компьютера/компании -> применяем privacy.redact
        self.external = bool(profile.get("external", False))
        self.privacy = None   # privacy.Privacy — обезличивание запросов и восстановление ответов (privacy.attach)
        self.calls = 0
        self.chars_in = 0
        self.chars_out = 0

    @property
    def context_tokens(self) -> int:
        return int(self.profile.get("max_context_tokens", 32768))

    @property
    def label(self) -> str:
        model = self.profile.get("model") or ""
        return f"{self.name}" + (f" ({model})" if model and model != "auto" else "")

    def supports_images(self) -> bool:
        """Можно ли вообще передать этой модели изображение (видит ли она его — проверяет vision.check)."""
        return False

    def complete(self, system: str, user: str, max_tokens: int = 4096, images: list[Image] | None = None) -> str:
        last: Exception | None = None
        if images and not self.supports_images():
            err = LLMError(f"[{self.name}] этот тип подключения не умеет передавать изображения модели")
            err.fatal, err.no_images = True, True  # type: ignore[attr-defined]
            raise err
        priv = self.privacy
        req = None
        if priv is not None:  # имена → коды (свои в каждом запросе), мат → нейтральные слова; видно в окне «Защита»
            req = priv.begin(getattr(self, "privacy_sticky", None))
            allow = [a for a in (getattr(self, "privacy_allow", None) or []) if a and len(a) >= 2]
            marks: dict[str, str] = {}
            if allow:  # исключение (поиск карточки сотрудника по имени): эти строки уходят как есть
                system, user, marks = protect(system, user, allow)
            system, user = req.outgoing(system), req.outgoing(user)
            if marks:
                system, user = unprotect(system, marks), unprotect(user, marks)
            system = req.preamble() + system  # пояснение — после обезличивания: в нём коды этого запроса
            priv.remember(req, system, user)
        attempt = busy_waits = 0
        while True:
            try:
                inflight.check()
                out = self._complete_images(system, user, max_tokens, images) if images else \
                    self._complete(system, user, max_tokens)
                out = strip_think(out)
                if req is not None:
                    out = req.incoming(out)
                self.calls += 1
                self.chars_in += len(system) + len(user)
                self.chars_out += len(out)
                return out
            except inflight.Cancelled as e:
                err = LLMError(f"[{self.name}] запрос остановлен: {e}")
                err.fatal, err.cancelled, err.safe = True, True, "остановлено"  # type: ignore[attr-defined]
                raise err from None
            except LLMError as e:
                last = e
                if getattr(e, "fatal", False):
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
                last = e
            try:
                if getattr(last, "busy", False) and busy_waits < BUSY_WAITS:
                    # Maverick занят (другой запрос ещё обрабатывается) — ждём дольше, это не ошибка запроса
                    busy_waits += 1
                    wait = min(int(getattr(last, "retry_after", None) or 20) * (1 + busy_waits // 3), 90)
                    from .. import logs
                    logs.get("llm").info("модель занята — жду %d с (%d из %d)", wait, busy_waits, BUSY_WAITS)
                    inflight.sleep(wait)
                    continue
                if attempt >= self.retries:
                    break
                wait = getattr(last, "retry_after", None)
                inflight.sleep(min(int(wait), 90) if wait else 3 * (attempt + 1))
                attempt += 1
            except inflight.Cancelled as e:
                err = LLMError(f"[{self.name}] запрос остановлен: {e}")
                err.fatal, err.cancelled, err.safe = True, True, "остановлено"  # type: ignore[attr-defined]
                raise err from None
        from .. import logs
        err = LLMError(f"[{self.name}] запрос к LLM не удался: {last}")
        err.status = getattr(last, "status", None)  # type: ignore[attr-defined]
        err.fatal = bool(getattr(last, "fatal", False))  # type: ignore[attr-defined]
        err.no_images = bool(getattr(last, "no_images", False))  # type: ignore[attr-defined]
        err.safe = f"{type(self).__name__}: {getattr(last, 'safe', None) or type(last).__name__}"  # type: ignore[attr-defined]
        err.busy = bool(getattr(last, "busy", False))  # type: ignore[attr-defined]
        logs.get("llm").warning("модель не ответила: %s, попыток %d", err.safe, attempt + 1 + busy_waits)
        raise err

    def _complete(self, system: str, user: str, max_tokens: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def _complete_images(self, system: str, user: str, max_tokens: int, images: list[Image]) -> str:  # pragma: no cover
        raise NotImplementedError

    # ---------------------------------------------------------------- http
    def _ssl_context(self) -> ssl.SSLContext | None:
        ca = self.profile.get("ca_bundle")
        if self.profile.get("verify_ssl", True) is False:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if ca:
            return ssl.create_default_context(cafile=ca)
        return None

    def http_json(self, method: str, url: str, body: Any = None, headers: dict | None = None,
                  timeout: float | None = None, raw: tuple[bytes, str] | None = None) -> Any:
        """JSON-запрос. raw=(байты, Content-Type) — отправить тело как есть (например, multipart с файлом)."""
        data = None
        h = {"Accept": "application/json"}
        if raw is not None:
            data, h["Content-Type"] = raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h["Content-Type"] = "application/json; charset=utf-8"
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with inflight.urlopen(req, timeout=timeout or self.timeout, context=self._ssl_context()) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            full = e.read(400_000).decode("utf-8", errors="replace")
            detail = full[:300]
            hint = HTTP_HINTS.get(e.code, "")
            err = LLMError(f"HTTP {e.code}{f' — {hint}' if hint else ''}. Адрес: {url}. Ответ сервера: {detail}")
            try:  # тело ответа целиком — по нему подбирается формат запроса (ошибки 422 от FastAPI)
                err.body = json.loads(full)  # type: ignore[attr-defined]
            except ValueError:
                err.body = full  # type: ignore[attr-defined]
            err.fatal = e.code in (400, 401, 403, 404, 405, 413, 422)  # type: ignore[attr-defined]
            if e.code == 429:
                try:
                    err.retry_after = int(e.headers.get("Retry-After") or 20)  # type: ignore[attr-defined]
                except ValueError:
                    err.retry_after = 20  # type: ignore[attr-defined]
            if e.code in (409, 423, 429) or (e.code in (400, 503) and BUSY_RE.search(detail)):
                err.busy, err.fatal = True, False  # type: ignore[attr-defined]
                err.retry_after = getattr(err, "retry_after", None) or 20  # type: ignore[attr-defined]
            err.status = e.code  # type: ignore[attr-defined]
            err.safe = f"HTTP {e.code}"  # для журнала — без тела ответа (там могут быть данные)
            raise err from None
        except Exception:
            why, retry = inflight.aborted()
            if why:   # соединение оборвали снаружи: остановка задачи или пробуждение после сна
                if retry:
                    raise ConnectionError(f"соединение оборвано: {why}") from None
                raise inflight.Cancelled(why) from None
            raise
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise LLMError(f"Ответ {url} не JSON: {raw[:300]}") from None


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    """Тело multipart/form-data: обычные поля и файлы (поле, имя файла, mime, байты)."""
    b = "----opsdigest" + secrets.token_hex(12)
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    for field, name, mime, data in files:
        out += (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode("utf-8") + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_think(text: str) -> str:
    """Убрать рассуждения reasoning-моделей (<think>…</think>)."""
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # открывающий тег мог быть в шаблоне чата
        text = text.split("</think>", 1)[1]
    return text.strip()


def extract_json(text: str) -> Any:
    """Достать JSON из ответа модели: целиком, из ```json-блока или первый сбалансированный {...}/[...]."""
    t = (text or "").strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            t = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        chunk = t[start:i + 1]
                        try:
                            return json.loads(chunk)
                        except json.JSONDecodeError:
                            try:
                                return json.loads(_relax(chunk))
                            except json.JSONDecodeError:
                                break
            start = t.find(opener, start + 1)
    raise ValueError("в ответе модели нет корректного JSON")


def _relax(s: str) -> str:
    """Типичные огрехи локальных моделей: висячие запятые."""
    return re.sub(r",\s*([}\]])", r"\1", s)


def protect(system: str, user: str, allow: list[str]) -> tuple[str, str, dict[str, str]]:
    """Строки из allow → метки, которые обезличивание не трогает (потом unprotect возвращает их на место)."""
    marks: dict[str, str] = {}
    for i, a in enumerate(sorted(set(allow), key=len, reverse=True)):
        m = f"QQ{i}XKEEPQQ"
        rx = re.compile(re.escape(a), re.I)
        if rx.search(system) or rx.search(user):
            marks[m] = a
            system, user = rx.sub(m, system), rx.sub(m, user)
    return system, user, marks


def unprotect(text: str, marks: dict[str, str]) -> str:
    for m, a in marks.items():
        text = text.replace(m, a)
    return text
