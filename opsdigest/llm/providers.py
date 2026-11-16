"""Конкретные провайдеры LLM."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Callable

from . import inflight
from .base import Image, LLMClient, LLMError, data_url, multipart

VISION_RE = re.compile(r"(?i)[-_/ .]vl\b|vl[-_ ]|vision|llava|pixtral|gemma-?3|gemma-?4|minicpm-?v|internvl|"
                       r"qwen.?[23].?5?.?(vl|omni)|glm-?4.?\d*v|molmo|llama-?3\.2-?\d+b-?vision|mistral-small-3")


def _no_images(name: str, detail: str) -> LLMError:
    err = LLMError(f"[{name}] {detail}")
    err.fatal, err.no_images = True, True  # type: ignore[attr-defined]
    return err


class OpenAIClient(LLMClient):
    """OpenAI-совместимый /chat/completions: LM Studio (Bionic), Ollama, vLLM, LiteLLM, корпоративные шлюзы."""

    def __init__(self, name: str, profile: dict):
        super().__init__(name, profile)
        self.base_url = (profile.get("base_url") or "http://localhost:1235/v1").rstrip("/")
        self._model = profile.get("model") or "auto"
        self.external = bool(profile.get("external", False))

    def _headers(self) -> dict:
        h = dict(self.profile.get("headers") or {})
        key = self.profile.get("api_key")
        if key:
            h.setdefault("Authorization", f"Bearer {key}")
        return h

    @property
    def model(self) -> str:
        if self._model in ("", "auto", None):
            self._model = self._pick_model()
        return self._model

    @property
    def label(self) -> str:
        m = self._model if self._model not in ("", "auto", None) else "auto"
        return f"{self.name} ({m})"

    def _pick_model(self) -> str:
        data = self.http_json("GET", f"{self.base_url}/models", headers=self._headers(), timeout=20)
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        ids = [i for i in ids if not re.search(r"(?i)embed|whisper|tts|[-_/ ]vl[-_ ]", i)]
        if not ids:
            raise LLMError(f"[{self.name}] на {self.base_url} нет загруженных моделей")
        prefer = self.profile.get("prefer")
        if prefer:
            for i in ids:
                if re.search(prefer, i, re.I):
                    return i
        return ids[0]

    def supports_images(self) -> bool:
        return True

    @property
    def vision_model(self) -> str:
        """Модель для изображений: profile.vision_model, иначе при «auto» — загруженная модель с поддержкой картинок."""
        vm = self.profile.get("vision_model") or ""
        if vm and vm != "auto":
            return vm
        if self._model not in ("", "auto", None) and not vm:
            return self._model
        try:
            data = self.http_json("GET", f"{self.base_url}/models", headers=self._headers(), timeout=20)
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        except (LLMError, OSError):
            ids = []
        for i in ids:
            if VISION_RE.search(i):
                self.profile["vision_model"] = i
                return i
        return self.model

    def _complete(self, system: str, user: str, max_tokens: int) -> str:
        return self._chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                          self.model, max_tokens)

    def _complete_images(self, system: str, user: str, max_tokens: int, images: list[Image]) -> str:
        content: list[dict] = [{"type": "text", "text": user}]
        content += [{"type": "image_url", "image_url": {"url": data_url(b, m)}} for b, m in images]
        try:
            return self._chat([{"role": "system", "content": system}, {"role": "user", "content": content}],
                              self.vision_model, max_tokens)
        except LLMError as e:
            if getattr(e, "status", None) in (400, 422) and re.search(r"(?i)image|vision|multimodal|content.*type", str(e)):
                raise _no_images(self.name, f"модель не принимает изображения — загрузите модель с поддержкой "
                                            f"картинок (VL/vision) или выберите другую модель для изображений. {e}") from None
            raise

    def _chat(self, messages: list[dict], model: str, max_tokens: int) -> str:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        body.update(self.profile.get("extra_body") or {})
        data = self.http_json("POST", f"{self.base_url}/chat/completions", body, self._headers())
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"[{self.name}] неожиданный ответ: {str(data)[:300]}") from None
        content = msg.get("content") or ""
        if not content.strip():
            raise LLMError(f"[{self.name}] пустой ответ модели")
        return content


class AnthropicClient(LLMClient):
    """Claude API: POST /v1/messages."""

    def __init__(self, name: str, profile: dict):
        super().__init__(name, profile)
        self.url = profile.get("url") or "https://api.anthropic.com/v1/messages"
        self.external = bool(profile.get("external", True))
        if not profile.get("api_key"):
            raise LLMError(f"[{name}] не задан api_key (ANTHROPIC_API_KEY в .env)")

    @property
    def context_tokens(self) -> int:
        return int(self.profile.get("max_context_tokens", 200000))

    def supports_images(self) -> bool:
        return True

    def _complete_images(self, system: str, user: str, max_tokens: int, images: list[Image]) -> str:
        blocks: list[dict] = []
        for b, m in images:
            if m not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                raise _no_images(self.name, f"формат {m} Claude не читает (только JPEG, PNG, GIF, WebP)")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": m,
                                                       "data": base64.b64encode(b).decode("ascii")}})
        return self._complete(system, user, max_tokens, blocks + [{"type": "text", "text": user}])

    def _complete(self, system: str, user: str, max_tokens: int, content: Any = None) -> str:
        body = {
            "model": self.profile.get("model") or "claude-sonnet-5-5",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content if content is not None else user}],
            "temperature": self.temperature,
        }
        headers = {"x-api-key": self.profile["api_key"], "anthropic-version": "2023-06-01"}
        data = self.http_json("POST", self.url, body, headers)
        parts = [b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"]
        out = "".join(parts)
        if not out.strip():
            raise LLMError(f"[{self.name}] пустой ответ: {str(data)[:300]}")
        return out


def _fill(obj: Any, values: dict[str, Any]) -> Any:
    """Подставить {{system}}, {{user}}, {{prompt}}, {{max_tokens}}, {{model}} в шаблон запроса."""
    if isinstance(obj, str):
        whole = re.fullmatch(r"\{\{(\w+)\}\}", obj)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: _fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    return obj


def dig(data: Any, path: str) -> Any:
    """Взять значение по пути вида 'choices.0.message.content'."""
    cur = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


class HttpJsonClient(LLMClient):
    """Произвольный JSON API по шаблону — для корпоративного агента, если он не OpenAI-совместимый.

    Пример профиля:
      type: http_json
      url: https://maverick.example/api/v1/chat
      headers: {Authorization: "Bearer ${MAVERICK_TOKEN}"}
      body: {query: "{{prompt}}", max_tokens: "{{max_tokens}}"}
      response_path: answer
    """

    IMAGE_KEYS = ("image_base64", "image_data_url", "images_base64", "images_data_url")

    def supports_images(self) -> bool:
        body = json.dumps(self.profile.get("image_body") or self.profile.get("body") or {})
        return any("{{" + k + "}}" in body for k in self.IMAGE_KEYS)

    def _complete_images(self, system: str, user: str, max_tokens: int, images: list[Image]) -> str:
        b, m = images[0]
        extra = {"image_base64": base64.b64encode(b).decode("ascii"), "image_data_url": data_url(b, m),
                 "image_mime": m, "images_base64": [base64.b64encode(x).decode("ascii") for x, _ in images],
                 "images_data_url": [data_url(x, y) for x, y in images]}
        return self._complete(system, user, max_tokens, extra, self.profile.get("image_body"))

    def _complete(self, system: str, user: str, max_tokens: int, extra: dict | None = None,
                  body_tmpl: Any = None) -> str:
        url = self.profile.get("url")
        if not url:
            raise LLMError(f"[{self.name}] не задан url")
        values = {
            "system": system,
            "user": user,
            "prompt": f"{system}\n\n{user}",
            "max_tokens": max_tokens,
            "model": self.profile.get("model", ""),
            **(extra or {}),
        }
        body = _fill(copy.deepcopy(body_tmpl or self.profile.get("body") or {"prompt": "{{prompt}}"}), values)
        data = self.http_json(self.profile.get("method", "POST").upper(), url, body, self.profile.get("headers") or {})
        try:
            out = dig(data, self.profile.get("response_path", ""))
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMError(f"[{self.name}] в ответе нет пути '{self.profile.get('response_path')}': {str(data)[:300]}") from None
        if not isinstance(out, str):
            out = str(out)
        if not out.strip():
            raise LLMError(f"[{self.name}] пустой ответ")
        return out


class CommandClient(LLMClient):
    """Внешняя программа. Промпт подаётся в stdin (или в файл через {{prompt_file}}), ответ берётся из stdout.

    Пример — Claude Code по подписке, без API-ключа:
      type: command
      command: ["claude", "-p", "--output-format", "text"]
    """

    def __init__(self, name: str, profile: dict):
        super().__init__(name, profile)
        self.external = bool(profile.get("external", True))

    @property
    def context_tokens(self) -> int:
        return int(self.profile.get("max_context_tokens", 150000))

    def supports_images(self) -> bool:
        cmd = self.profile.get("image_command") or self.profile.get("command") or ""
        return "{{image_file}}" in (cmd if isinstance(cmd, str) else " ".join(cmd))

    def _complete_images(self, system: str, user: str, max_tokens: int, images: list[Image]) -> str:
        b, m = images[0]
        fd, path = tempfile.mkstemp(suffix="." + (m.split("/")[-1].replace("jpeg", "jpg")), prefix="opsdigest_img_")
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        try:
            cmd = self.profile.get("image_command") or self.profile.get("command")
            cmd = cmd.replace("{{image_file}}", path) if isinstance(cmd, str) else [c.replace("{{image_file}}", path) for c in cmd]
            return self._complete(system, user, max_tokens, cmd)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _complete(self, system: str, user: str, max_tokens: int, cmd: Any = None) -> str:
        cmd = cmd or self.profile.get("command")
        if not cmd:
            raise LLMError(f"[{self.name}] не задана command")
        prompt = f"{system}\n\n{user}"
        tmp = None
        use_file = "{{prompt_file}}" in (cmd if isinstance(cmd, str) else " ".join(cmd))
        if use_file:
            fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="opsdigest_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(prompt)
            cmd = cmd.replace("{{prompt_file}}", tmp) if isinstance(cmd, str) else [c.replace("{{prompt_file}}", tmp) for c in cmd]
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            r = subprocess.run(cmd, input=None if use_file else prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout,
                               shell=isinstance(cmd, str), env=env)
        except FileNotFoundError:
            err = LLMError(f"[{self.name}] программа не найдена: {cmd}")
            err.fatal = True  # type: ignore[attr-defined]
            raise err from None
        except subprocess.TimeoutExpired:
            raise LLMError(f"[{self.name}] таймаут {self.timeout:.0f} с") from None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if r.returncode != 0:
            raise LLMError(f"[{self.name}] код {r.returncode}: {(r.stderr or r.stdout)[:400]}")
        if not (r.stdout or "").strip():
            raise LLMError(f"[{self.name}] пустой вывод")
        return r.stdout


FIELD_CANDIDATES = ("message", "text", "query", "prompt", "content", "input", "question", "user_message")
ANSWER_KEYS = ("response_text", "response", "answer", "text", "content", "result", "output")


def field_from_openapi(spec: dict, path_suffix: str = "/query") -> str | None:
    """Найти в описании API (OpenAPI) имя текстового поля запроса POST …/query."""
    if not isinstance(spec, dict):
        return None
    comps = (spec.get("components") or {}).get("schemas") or {}

    def resolve(sch):
        seen = 0
        while isinstance(sch, dict) and "$ref" in sch and seen < 5:
            sch = comps.get(sch["$ref"].rsplit("/", 1)[-1], {})
            seen += 1
        return sch if isinstance(sch, dict) else {}

    for path, ops in (spec.get("paths") or {}).items():
        if not str(path).rstrip("/").endswith(path_suffix) or not isinstance(ops, dict) or "post" not in ops:
            continue
        content = ((ops["post"].get("requestBody") or {}).get("content") or {})
        sch = resolve((content.get("application/json") or {}).get("schema") or {})
        props = {}
        for part in [sch] + [resolve(x) for x in sch.get("allOf", [])]:
            props.update(part.get("properties") or {})
        for name in FIELD_CANDIDATES:
            if name in props:
                return name
        for name, prop in props.items():
            if resolve(prop).get("type") == "string":
                return name
    return None


class MaverickClient(LLMClient):
    """Maverick Agent Core API: POST {адрес}/api/v1/query, заголовок X-API-Key, текст запроса в JSON,
    ответ — поле response_text. Модель выбирать не нужно: это агент со своей моделью.

    Имя поля с текстом запроса по документации — "message", но на практике может отличаться; если сервер ответит 422,
    клиент сам определит поле (по /openapi.json или пробными запросами) и запомнит его в profile["body_field"].
    """

    PREAMBLE = ("Это служебная задача обработки текста для программы OpsDigest. Не используй инструменты, плагины, "
                "поиск и память — ответь сразу и строго в формате, который запрошен ниже.")

    def __init__(self, name: str, profile: dict):
        super().__init__(name, profile)
        self.external = bool(profile.get("external", False))
        self.discovered: str | None = None
        if not profile.get("api_key"):
            raise LLMError(f"[{name}] не задан API-ключ Maverick (веб-интерфейс Maverick → «API-ключи»)")
        if not (profile.get("url") or profile.get("base_url")):
            raise LLMError(f"[{name}] не задан адрес Maverick")

    @property
    def label(self) -> str:
        return f"{self.name} (Agent Core)"

    def host(self) -> str:
        u = (self.profile.get("url") or self.profile.get("base_url") or "").strip().rstrip("/")
        for tail in ("/api/v1/query", "/api/v1", "/api"):
            if u.endswith(tail):
                return u[: -len(tail)]
        return u

    def endpoint(self) -> str:
        return self.host() + "/api/v1/query"

    def _headers(self) -> dict:
        return {"X-API-Key": str(self.profile["api_key"]).strip()}

    def check_key(self) -> None:
        """Быстрая проверка ключа: GET /api/v1/api-keys → 200 (из документации Maverick)."""
        self.http_json("GET", self.host() + "/api/v1/api-keys", headers=self._headers(), timeout=20)

    def discover_field(self) -> str | None:
        """Какое поле сервер ждёт для текста: сначала описание API, потом пробные короткие запросы."""
        for path in ("/openapi.json", "/api/openapi.json", "/api/v1/openapi.json"):
            try:
                spec = self.http_json("GET", self.host() + path, headers=self._headers(), timeout=20)
            except (LLMError, OSError):
                continue
            f = field_from_openapi(spec)
            if f:
                return f
        for f in FIELD_CANDIDATES:
            try:
                data = self.http_json("POST", self.endpoint(), {f: "Ответь одним словом: готов"}, self._headers(),
                                      timeout=180)
            except LLMError as e:
                if getattr(e, "status", None) == 422:
                    continue
                raise
            if isinstance(data, dict) and (self._answer(data) or data.get("session_id")):
                return f
        return None

    @staticmethod
    def _answer(data: dict) -> str:
        for k in ANSWER_KEYS:
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v
        msgs = data.get("messages")
        if isinstance(msgs, list):
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") in ("assistant", "agent", None):
                    c = m.get("content") or m.get("text")
                    if isinstance(c, str) and c.strip():
                        return c
        return ""

    def _body(self, field: str, message: str) -> dict:
        """Тело запроса: текст + дополнительные поля (plugins.<вид>.body — например, какой плагин включить)."""
        extra = self.profile.get("extra_body")
        return {**(extra if isinstance(extra, dict) else {}), field: message}

    def _post(self, message: str) -> dict:
        field = self.profile.get("body_field") or "message"
        try:
            return self.http_json("POST", self.endpoint(), self._body(field, message), self._headers())
        except LLMError as e:
            if getattr(e, "status", None) != 422 or self.profile.get("body_field_locked"):
                raise
            found = self.discover_field()
            if not found:
                e.safe = f"HTTP 422, поле для текста не найдено (пробовали: {', '.join(FIELD_CANDIDATES)})"  # type: ignore[attr-defined]
                err = LLMError(f"[{self.name}] Maverick не принимает текст ни в одном из полей "
                               f"({', '.join(FIELD_CANDIDATES)}). Нужна актуальная схема запроса /api/v1/query. "
                               f"Исходная ошибка: {e}")
                err.fatal, err.status, err.safe = True, 422, e.safe  # type: ignore[attr-defined]
                raise err from None
            if found == field:
                e.safe = f"HTTP 422 на запрос {len(message)} симв., короткий запрос в поле {field} проходит"  # type: ignore[attr-defined]
                err = LLMError(f"[{self.name}] Maverick принимает короткий текст в поле «{field}», но отклонил этот "
                               f"запрос ({len(message)} символов) — возможно, ограничение на длину или содержимое. "
                               f"Исходная ошибка: {e}")
                err.fatal, err.status, err.safe = True, 422, e.safe  # type: ignore[attr-defined]
                raise err from None
            self.profile["body_field"] = self.discovered = found
            return self.http_json("POST", self.endpoint(), self._body(found, message), self._headers())

    def _complete(self, system: str, user: str, max_tokens: int) -> str:
        pre = self.profile.get("preamble", self.PREAMBLE)
        message = "\n\n".join(x for x in (pre, system, user) if x)
        data = self._post(message)
        self.last_data = data  # весь ответ: file_artifacts (вложения), hitl_pending (вопросы), session_id
        return self._text_of(data)

    # ---------------------------------------------------------------- изображения
    # Как Maverick принимает файлы, в документации не описано, но сервер подсказывает: «Нужно ввести текст или
    # прикрепить хотя бы один файл». Формат определяется сам (discover_images): по описанию API, если сервер его отдаёт,
    # по подсказкам ошибок 422 и пробными запросами с картинкой-цифрой — рабочий тот, где модель назвала цифру.
    # Найденный формат хранится в profile["image_mode"]. Если известен точный формат (например, из запроса веб-версии
    # Maverick в инструментах разработчика браузера), его можно задать шаблоном profile["image_body"] с подстановками
    # {{prompt}}, {{image_base64}}, {{image_data_url}}, {{image_mime}}, {{image_name}}.
    def supports_images(self) -> bool:
        return self.profile.get("image_mode") != "none"

    def _complete_images(self, system: str, user: str, max_tokens: int, images: list[Image]) -> str:
        mode = self.profile.get("image_mode")
        if not isinstance(mode, dict):
            mode = self.discover_images()
        pre = self.profile.get("preamble", self.PREAMBLE)
        message = "\n\n".join(x for x in (pre, system, user) if x)
        return self._text_of(self._post_images(mode, message, images))

    def _post_images(self, mode: dict, message: str, images: list[Image], timeout: float | None = None) -> Any:
        from .attach import build_item, item_fields
        field = self.profile.get("body_field") or "message"
        files = [(f"image{i + 1}.{m.split('/')[-1].replace('jpeg', 'jpg')}", m, b) for i, (b, m) in enumerate(images)]
        if mode.get("template"):
            n, m, b = files[0]
            values = {"prompt": message, "message": message, "image_base64": base64.b64encode(b).decode("ascii"),
                      "image_data_url": data_url(b, m), "image_mime": m, "image_name": n}
            body = _fill(copy.deepcopy(self.profile.get("image_body") or {}), values)
            url = self.host() + (self.profile.get("image_path") or "/api/v1/query")
            return self.http_json("POST", url, body, self._headers(), timeout=timeout)
        if mode.get("multipart"):
            raw = multipart({field: message}, [(mode["field"], n, m, b) for n, m, b in files])
            return self.http_json("POST", self.endpoint(), headers=self._headers(), raw=raw, timeout=timeout)
        items: list[Any] = []
        extra: dict = {}
        if mode.get("upload") and mode["upload"].get("session_field"):  # загрузка требует сессию — заводим новую
            extra["session_id"] = self._new_session()
        for n, m, b in files:
            item = mode.get("item")
            if mode.get("upload"):
                items.append(self._upload(mode["upload"], n, m, b, extra.get("session_id")))
            elif item == "data_url":
                items.append(data_url(b, m))
            elif item == "base64":
                items.append(base64.b64encode(b).decode("ascii"))
            else:
                items.append(build_item(item_fields(item) or {}, n, m, b))
        value: Any = items if mode.get("list", True) else items[0]
        return self.http_json("POST", self.endpoint(), {field: message, mode["field"]: value, **extra},
                              self._headers(), timeout=timeout)

    def _new_session(self) -> str:
        data = self._post("Это служебный запрос. Ответь одним словом: готов")
        sid = data.get("session_id") if isinstance(data, dict) else None
        if not sid:
            raise LLMError(f"[{self.name}] Maverick не вернул session_id")
        return str(sid)

    def _upload_raw(self, up: dict, name: str, mime: str, data: bytes, session: str | None = None) -> Any:
        fields = {up["session_field"]: session} if session and up.get("session_field") else {}
        return self.http_json("POST", self.host() + up["path"], headers=self._headers(), timeout=180,
                              raw=multipart(fields, [(up.get("form_field") or "file", name, mime, data)]))

    @staticmethod
    def upload_ref(res: Any) -> tuple[str | None, Any]:
        """Номер загруженного файла и описывающий его объект из ответа на загрузку (форма ответа заранее неизвестна)."""
        obj = res
        for _ in range(3):
            if isinstance(obj, list) and obj:
                obj = obj[0]
            if isinstance(obj, dict):
                inner = next((obj[k] for k in ("data", "attachment", "artifact", "file", "result", "item",
                                               "attachments", "artifacts", "files") if isinstance(obj.get(k), (dict, list))
                              and obj.get(k)), None)
                if inner is not None and not any(isinstance(obj.get(k), (str, int)) for k in
                                                 ("id", "attachment_id", "artifact_id", "file_id")):
                    obj = inner
                    continue
            break
        if isinstance(obj, (str, int)):
            return str(obj), obj
        if not isinstance(obj, dict):
            return None, obj
        for key in ("attachment_id", "artifact_id", "id", "file_id", "attachmentId", "artifactId", "fileId", "uuid"):
            v = obj.get(key)
            if isinstance(v, (str, int)) and str(v):
                return str(v), obj
        for v in obj.values():
            if isinstance(v, str) and re.fullmatch(r"(art|att|file|upl|doc|attach)_[\w-]{6,}", v):
                return v, obj
        return None, obj

    def _upload(self, up: dict, name: str, mime: str, data: bytes, session: str | None = None) -> Any:
        """Загрузить файл и вернуть то, что кладётся в поле запроса: номер, объект ответа или {ключ: номер}."""
        res = self._upload_raw(up, name, mime, data, session)
        ref, obj = self.upload_ref(res)
        how = up.get("as") or "id"
        if how == "object" and obj is not None:
            return obj
        if how.startswith("key:"):
            v = obj.get(how[4:]) if isinstance(obj, dict) else None
            if v is None:
                raise LLMError(f"[{self.name}] в ответе на загрузку нет поля {how[4:]}")
            return v
        if ref is None:
            raise LLMError(f"[{self.name}] сервер принял файл, но не вернул его номер (поля: "
                           f"{', '.join(list(obj)[:8]) if isinstance(obj, dict) else type(obj).__name__})")
        if how not in ("id", "object"):
            return {how: ref}
        return ref

    def _openapi(self, notes: list[str] | None = None) -> dict | None:
        from .attach import OPENAPI_PATHS
        tried = []
        for path in OPENAPI_PATHS:
            try:
                spec = self.http_json("GET", self.host() + path, headers=self._headers(), timeout=20)
            except LLMError as e:
                tried.append(f"{path} — " + (f"HTTP {e.status}" if getattr(e, "status", None) else "не JSON"))
                continue
            except OSError as e:
                tried.append(f"{path} — {type(e).__name__}")
                continue
            if isinstance(spec, dict) and spec.get("paths"):
                if notes is not None:
                    notes.append(f"описание API: {path}")
                return spec
            tried.append(f"{path} — не OpenAPI")
        for page in ("/docs", "/api/docs", "/api/v1/docs", "/redoc"):  # страница Swagger знает, где описание
            url = self._spec_url_from(page)
            if not url:
                continue
            try:
                spec = self.http_json("GET", url if url.startswith("http") else self.host() + url,
                                      headers=self._headers(), timeout=20)
            except (LLMError, OSError) as e:
                tried.append(f"{page} → {url} — {getattr(e, 'status', None) or type(e).__name__}")
                continue
            if isinstance(spec, dict) and spec.get("paths"):
                if notes is not None:
                    notes.append(f"описание API: {url} (со страницы {page})")
                return spec
        if notes is not None:
            notes.append("описание API не получено: " + "; ".join(tried))
        return None

    def _spec_url_from(self, page: str) -> str | None:
        """Адрес openapi.json со страницы Swagger UI / ReDoc (FastAPI: url: '/api/openapi.json')."""
        import urllib.request
        req = urllib.request.Request(self.host() + page, headers={**self._headers(), "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=self._ssl_context()) as r:
                html = r.read(300_000).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        m = re.search(r"""(?:url|spec-url)\s*[:=]\s*["']([^"']+\.json[^"']*)["']""", html)
        return m.group(1) if m else None

    def image_candidates(self, spec: dict | None) -> list[dict]:
        """Возможные форматы передачи картинки: шаблон пользователя, из описания API, затем — типичные."""
        from .attach import defaults_for, upload_paths
        out: list[dict] = []
        if isinstance(self.profile.get("image_body"), dict):
            out.append({"template": True})
        info = query_schema(spec) if spec else None
        if info:
            props, comps, is_multipart = info
            for name, prop in props.items():
                if not re.search(r"(?i)file|image|attach|media|upload|document", name) or name == "session_id":
                    continue
                prop = _resolve(prop, comps)
                is_list = prop.get("type") == "array"
                it = _resolve(prop.get("items") or {}, comps) if is_list else prop
                if is_multipart or it.get("format") == "binary":
                    out.append({"field": name, "multipart": True})
                    continue
                if it.get("type") == "object" or it.get("properties"):
                    from .attach import guess_role
                    keys = list((it.get("properties") or {}).keys())
                    req = set(it.get("required") or []) or set(keys)
                    fields = {k: guess_role(k) for k in keys if k in req or guess_role(k) in ("b64", "name", "mime")}
                    for k in keys:
                        enum = _resolve((it.get("properties") or {})[k], comps).get("enum")
                        if enum:
                            fields[k] = "const:" + str(next((v for v in enum if "image" in str(v).lower()), enum[0]))
                    out.append({"field": name, "list": is_list, "item": {"fields": fields}})
                    dl = {k: ("data_url" if r == "b64" else r) for k, r in fields.items()}
                    if dl != fields:
                        out.append({"field": name, "list": is_list, "item": {"fields": dl}})
                    continue
                if re.search(r"(?i)ids?$", name) or it.get("format") == "uuid":
                    out += [{"field": name, "list": is_list, "upload": {"path": p, "form_field": "file"}}
                            for p in upload_paths(spec)]
                    continue
                out += [{"field": name, "list": is_list, "item": "data_url"},
                        {"field": name, "list": is_list, "item": "base64"}]
        out += defaults_for("files")
        out.append({"field": "files", "multipart": True})
        out += defaults_for("attachments")[:3] + defaults_for("images")[-2:]
        uniq, seen = [], set()
        for m in out:
            k = json.dumps(m, sort_keys=True)
            if k not in seen:
                seen.add(k)
                uniq.append(m)
        return uniq

    def discover_images(self, progress=None) -> dict:
        """Найти формат, в котором Maverick принимает изображение и модель его видит. Сохраняет в profile.

        Журнал попыток (что отправлялось и что ответил сервер — без ключа и без картинки) — в self.image_report."""
        import random
        import time
        from collections import deque
        from ..media import digit_png
        from .attach import adapt, digits_in, schema_summary, short_detail, upload_paths
        report: list[str] = []
        self.image_report = report

        def note(line: str) -> None:
            report.append(line)
            if progress:
                progress(line)
        if not self.profile.get("body_field"):  # сначала — в каком поле сервер ждёт текст
            f = self.discover_field()
            if f:
                self.profile["body_field"] = self.discovered = f
        note(f"Текст запроса передаётся в поле «{self.profile.get('body_field') or 'message'}».")
        spec = self._openapi(report)
        if progress:
            progress(report[-1])
        summary = schema_summary(spec)
        if summary:
            note("Методы API с телом запроса:")
            for line in summary[:40]:
                note("  " + line)
        uploads = upload_paths(spec)
        ask = "Какая цифра нарисована на прикреплённом изображении? Ответь только одной цифрой."
        if not isinstance(self.profile.get("image_body"), dict):  # как веб-версия: загрузка файла → номер в запросе
            found = self._discover_attachments(note, ask, spec)
            if found:
                self.profile["image_mode"] = self.discovered_image_mode = found
                return found
        queue = deque(self.image_candidates(spec))
        seen: set[str] = set()
        limit = int(self.profile.get("image_probe_limit") or 30)
        tally = {"rejected": 0, "server": 0, "blind": 0, "silent": 0, "noreply": 0}
        tried = net_streak = 0
        while queue and tried < limit:
            mode = queue.popleft()
            key = json.dumps(mode, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            tried += 1
            digit = str(random.randint(2, 9))
            label = describe_mode(mode)
            t0 = time.time()
            try:
                data = self._post_images(mode, ask, [(digit_png(digit), "image/png")], timeout=240)
                net_streak = 0
            except LLMError as e:
                st = getattr(e, "status", None)
                body = getattr(e, "body", None)
                note(f"{tried}. {label} → HTTP {st or '?'}: {short_detail(body if body is not None else str(e))}")
                if st in (401, 403):
                    raise
                tally["server" if (st or 0) >= 500 else "rejected"] += 1
                new = adapt(mode, body, uploads) if st == 422 else []
                for m in reversed(new):
                    queue.appendleft(m)
                if new:
                    note(f"   ↳ по подсказке сервера пробую ещё: {', '.join(describe_mode(m) for m in new)}")
                continue
            except OSError as e:  # таймаут, обрыв — сервер не ответил
                net_streak += 1
                tally["noreply"] += 1
                note(f"{tried}. {label} → нет ответа за {time.time() - t0:.0f} с ({type(e).__name__})")
                if net_streak >= 3:
                    break
                continue
            ans = self._answer(data) if isinstance(data, dict) else ""
            got = digits_in(ans)
            if not ans.strip():
                tally["silent"] += 1
                note(f"{tried}. {label} → ответ без текста: {response_info(data)}")
                continue
            if got == {digit}:
                note(f"{tried}. {label} → ✓ модель назвала цифру {digit}: «{ans.strip()[:60]}»")
                self.profile["image_mode"] = self.discovered_image_mode = mode
                return mode
            note(f"{tried}. {label} → принято, но ответ «{ans.strip()[:80]}» (на картинке {digit})")
            if got:  # что-то увидела, но прочитала неверно — второй шанс с другой цифрой
                d2 = str((int(digit) + 3) % 8 + 2)
                try:
                    a2 = self._answer(self._post_images(mode, ask, [(digit_png(d2), "image/png")], timeout=240))
                except (LLMError, OSError):
                    a2 = ""
                if digits_in(a2) == {d2}:
                    note(f"   ↳ повтор с цифрой {d2}: ✓ «{a2.strip()[:60]}»")
                    self.profile["image_mode"] = self.discovered_image_mode = mode
                    return mode
                note(f"   ↳ повтор с цифрой {d2}: «{a2.strip()[:60]}»")
            tally["blind"] += 1
        parts = []
        for k, text in (("rejected", "отклонил запрос (4xx)"), ("server", "ошибка на сервере (5xx)"),
                        ("blind", "принял, но картинку не увидел"), ("silent", "ответил без текста"),
                        ("noreply", "не ответил")):
            if tally[k]:
                parts.append(f"{text} — {tally[k]}")
        err = LLMError(f"[{self.name}] Maverick не принял изображение ни в одном из {tried} вариантов "
                       f"({'; '.join(parts) or 'нет попыток'}). Описание API: "
                       f"{'есть' if spec else 'сервер его не отдаёт'}. Журнал попыток — в окне выше и в файле "
                       f"data\\maverick-images.txt рабочей папки (в нём нет ключа и переписки) — пришлите его "
                       f"разработчику. Или задайте формат вручную: шаг «Модель» → «Изображения».")
        err.fatal, err.no_images = True, True  # type: ignore[attr-defined]
        err.safe = f"формат изображений не найден, попыток {tried}"  # type: ignore[attr-defined]
        raise err

    ATTACH_UPLOADS = ("/api/v1/attachments/upload", "/api/v1/attachments", "/api/v1/files/upload", "/api/v1/files",
                      "/api/v1/upload")
    ATTACH_FIELDS = ("attachments", "attachment_ids", "files", "file_ids", "artifact_ids", "artifacts", "images",
                     "documents")

    def _discover_attachments(self, note, ask: str, spec: dict | None) -> dict | None:
        """Как в веб-версии Maverick: файл — на /api/v1/attachments/upload, в запросе — ссылка на него.

        1) загрузить картинку (поле формы подбирается по ошибке 422); 2) найти поле запроса для ссылки: запрос без текста
        с этим полем — если поле не то, сервер сразу отвечает 422 «Нужно ввести текст или прикрепить хотя бы один файл»;
        3) настоящий запрос с текстом — модель должна назвать цифру; 4) подтверждение второй картинкой."""
        import random
        from ..media import digit_png
        from .attach import digits_in, short_detail, upload_paths
        digit = str(random.randint(2, 9))
        png = digit_png(digit)
        paths = list(dict.fromkeys(list(self.ATTACH_UPLOADS) + [p for p in upload_paths(spec)]))
        up = res = None
        session = None
        for path in paths:
            form = "file"
            for _ in range(3):
                cand = {"path": path, "form_field": form}
                if session:
                    cand["session_field"] = session[0]
                try:
                    res = self._upload_raw(cand, "image1.png", "image/png", png, session[1] if session else None)
                    up = cand
                    break
                except LLMError as e:
                    st, body = getattr(e, "status", None), getattr(e, "body", None)
                    note(f"загрузка {path} (поле «{form}») → HTTP {st or '?'}: {short_detail(body if body is not None else str(e), 300)}")
                    if st in (401, 403):
                        raise
                    if st != 422:
                        break
                    miss = [str(x.get("loc", [""])[-1]) for x in (body or {}).get("detail", [])
                            if isinstance(body, dict) and isinstance(x, dict) and "missing" in str(x.get("type"))]
                    fmiss = [k for k in miss if re.search(r"(?i)file|upload|attach|image|data", k)]
                    smiss = [k for k in miss if re.search(r"(?i)session|chat|conversation|thread", k)]
                    if fmiss and fmiss[0] != form:
                        form = fmiss[0]
                    elif smiss and not session:
                        session = (smiss[0], self._new_session())
                        note(f"   ↳ загрузка требует «{smiss[0]}» — завожу сессию")
                    else:
                        break
                except OSError as e:
                    note(f"загрузка {path} → нет ответа ({type(e).__name__})")
                    break
            if up:
                break
        if not up:
            note("Загрузить файл отдельно не удалось — пробую передать его внутри запроса.")
            return None
        ref, obj = self.upload_ref(res)
        keys = ", ".join(list(obj)[:12]) if isinstance(obj, dict) else type(obj).__name__
        note(f"загрузка {up['path']} (поле «{up['form_field']}») → принято; номер файла: {ref or 'не найден'}; "
             f"поля ответа: {keys}")
        shapes: list[tuple[str, Any]] = []
        if isinstance(obj, dict):  # все похожие на номер поля ответа: сначала основной номер
            idkeys = [k for k, v in obj.items() if isinstance(v, (str, int)) and str(v) and (
                re.search(r"(?i)(^|_)id$|[a-z]Id$|uuid", k) or re.fullmatch(r"(art|att|file|upl|doc|attach)_[\w-]{6,}", str(v)))]
            idkeys.sort(key=lambda k: str(obj[k]) != str(ref))
            shapes += [(f"key:{k}", obj[k]) for k in idkeys]
            shapes.append(("object", obj))
        elif ref:
            shapes.append(("id", ref))
        if ref:
            shapes += [(k, {k: ref}) for k in ("id", "attachment_id", "artifact_id", "file_id")]
        if not shapes:
            return None
        qf = self.profile.get("body_field") or "message"
        sess = {"session_id": session[1]} if session else {}

        def body_for(field, val, text):
            b = {field: [val], **sess}
            if text:
                b[qf] = text
            return b

        def run(field, how, val, text, d):
            for attempt in (1, 2):
                data = self.http_json("POST", self.endpoint(), body_for(field, val, text), self._headers(), timeout=300)
                ans = self._answer(data) if isinstance(data, dict) else ""
                if not ans.strip() and attempt == 1 and busy(data):
                    note(f"   ↳ Maverick занят или лимит запросов {response_info(data)} — жду 20 с и повторяю")
                    time.sleep(20)
                    continue
                return ans, digits_in(ans) == {d}, data
            return ans, False, data
        recognized: list[tuple[str, str, Any]] = []
        for field in self.ATTACH_FIELDS:
            known = False
            for how, val in shapes:
                try:
                    ans, ok, data = run(field, how, val, "", digit)
                except LLMError as e:
                    st, body = getattr(e, "status", None), getattr(e, "body", None)
                    det = short_detail(body if body is not None else str(e), 300)
                    if st == 422 and re.search(r"(?i)текст.{0,40}файл|text.{0,40}file", det):
                        note(f"поле «{field}»: сервер его не знает")
                        break  # другой формат значения тут не поможет
                    note(f"поле «{field}» = [{how}] → HTTP {st or '?'}: {det}")
                    if st in (401, 403):
                        raise
                    known = known or st not in (404, 405)
                    continue
                except OSError as e:
                    note(f"поле «{field}» = [{how}] → нет ответа ({type(e).__name__})")
                    continue
                known = True
                if ans.strip():
                    note(f"поле «{field}» = [{how}] → сервер принял (без текста), ответ «{ans.strip()[:60]}»")
                    recognized.insert(0, (field, how, val))
                    break
                note(f"поле «{field}» = [{how}] → принято, но без ответа {response_info(data)}")
                recognized.append((field, how, val))
            if known:
                note(f"Поле для файлов — «{field}».")
                break
        else:  # /query не знает ни одного поля для файлов — что скажет асинхронный метод веб-версии (для журнала)
            self._probe_async(note, shapes[0][1])
        for field, how, val in recognized[:6]:
            mode = {"field": field, "list": True, "upload": {**up, "as": how}}
            try:
                ans, ok, data = run(field, how, val, ask, digit)
            except (LLMError, OSError) as e:
                note(f"«{field}» = [{how}] с вопросом → ошибка: {short_detail(getattr(e, 'body', None) or str(e), 300)}")
                continue
            note(f"«{field}» = [{how}] с вопросом → «{ans.strip()[:80]}» (на картинке {digit}) {'' if ans.strip() else response_info(data)}")
            if not ok:
                continue
            d2 = str((int(digit) + 3) % 8 + 2)  # подтверждение: новая загрузка, другая цифра
            try:
                a2 = self._text_of(self._post_images(mode, ask, [(digit_png(d2), "image/png")], timeout=300))
            except (LLMError, OSError) as e:
                note(f"   ↳ подтверждение не прошло: {e}")
                continue
            if digits_in(a2) == {d2}:
                note(f"   ↳ подтверждение с цифрой {d2}: ✓ «{a2.strip()[:60]}»")
                return mode
            note(f"   ↳ подтверждение с цифрой {d2}: «{a2.strip()[:60]}»")
        return None

    def _probe_async(self, note, val: Any) -> None:
        """Только для журнала: какое поле для файлов принимает /api/v1/query/async (им пользуется веб-версия)."""
        from .attach import short_detail
        url = self.host() + "/api/v1/query/async"
        for field in self.ATTACH_FIELDS[:6]:
            try:
                data = self.http_json("POST", url, {field: [val]}, self._headers(), timeout=60)
            except LLMError as e:
                det = short_detail(getattr(e, "body", None) or str(e), 200)
                if getattr(e, "status", None) == 422 and re.search(r"(?i)текст.{0,40}файл|text.{0,40}file", det):
                    continue
                note(f"/query/async, поле «{field}» → HTTP {getattr(e, 'status', '?')}: {det}")
                if getattr(e, "status", None) in (404, 405):
                    return
                continue
            except OSError as e:
                note(f"/query/async → нет ответа ({type(e).__name__})")
                return
            keys = ", ".join(f"{k}={str(v)[:40]}" if isinstance(v, (str, int)) else k
                             for k, v in list(data.items())[:12]) if isinstance(data, dict) else type(data).__name__
            note(f"/query/async принимает поле «{field}»; ответ: {keys}")
            return
        note("/query/async: ни одно поле для файлов не подошло")

    def _text_of(self, data: Any) -> str:
        if not isinstance(data, dict):
            raise LLMError(f"[{self.name}] неожиданный ответ Maverick: {str(data)[:200]}")
        text = self._answer(data)
        sid = str(data.get("session_id") or "") or None
        if not text.strip() and re.search(r"(?i)hitl|waiting|approval", str(data.get("status") or "")):
            from .. import maverick_chat
            ask = maverick_chat.hitl_find(data) or {}
            # сессия ждёт человека и держит Maverick (он ведёт одну сессию за раз) — закрыть её сразу, фоном
            inflight.note_session(sid, "hitl", host=self.host(), hitl={"id": ask.get("id") or ""})
            if sid:
                threading.Thread(target=self.release_session, args=(sid,), daemon=True,
                                 name="maverick-release").start()
            err = LLMError(f"[{self.name}] Maverick ждёт подтверждения или ответа человека (hitl): "
                           f"{(ask.get('question') or '')[:200]} — служебный запрос программы на это ответить не может; "
                           "подтвердите действие в веб-чате Maverick (или разрешите плагину работать без подтверждения) "
                           "либо задайте вопрос в ассистенте в режиме «Maverick как на портале»")
            err.safe, err.hitl, err.fatal = "status=hitl_waiting", True, True  # type: ignore[attr-defined]
            raise err
        if not text.strip():
            status = str(data.get("status") or "")
            err = LLMError(f"[{self.name}] Maverick не вернул текст: {response_info(data)}")
            err.safe = f"status={status or '?'}"  # type: ignore[attr-defined]
            if busy(data):  # занят другим запросом — подождать и повторить (LLMClient.complete)
                err.busy, err.retry_after = True, 20  # type: ignore[attr-defined]
            if sid and re.search(r"(?i)process|running|pending|queued|progress|accepted|started", status):
                inflight.note_session(sid, "processing", host=self.host())  # ещё работает — закроем перед сводкой
            raise err
        inflight.drop_session(sid)
        return text

    # ---------------------------------------------------------------- открытые сессии
    # Как закрыть сессию, в документации Maverick не описано. Пробуем известные по REST-обычаю адреса (остановить
    # сессию/запрос), запоминаем, какой ответил 2xx; если не сработал ни один, а сессия ждёт ответа человека (hitl) —
    # отвечаем в неё «отмена» (действие не выполняется); служебные сессии программы затем удаляем.
    CANCEL_PATHS = (("POST", "/api/v1/sessions/{sid}/cancel", None), ("POST", "/api/v1/sessions/{sid}/stop", None),
                    ("POST", "/api/v1/query/{sid}/cancel", None), ("POST", "/api/v1/query/cancel", {"session_id": "{sid}"}),
                    ("POST", "/api/v1/sessions/{sid}/interrupt", None))
    CANCEL_FOUND: dict[str, tuple] = {}      # адрес Maverick → рабочий способ остановить
    CANCEL_NONE: set[str] = set()            # адреса, где ни один способ не подошёл (до перезапуска не пробуем)

    def release_session(self, sid: str, *, owner: str = "service", hitl: dict | None = None,
                        journal: Callable[[str], None] | None = None) -> dict:
        """Закрыть открытую сессию Maverick. owner="service" — служебная (можно удалить), "portal" — беседа
        пользователя (только остановить и отклонить ожидающий вопрос). → {sid, ok, how, tried}."""
        info = inflight.open_sessions().get(str(sid)) or {}
        hitl = hitl if hitl is not None else info.get("hitl")
        waiting = hitl is not None or info.get("why") == "hitl"
        host = self.host()
        tried: list[str] = []
        note = journal or (lambda line: None)

        def call(method: str, path: str, body: Any) -> int:
            url = host + path.replace("{sid}", urllib.parse.quote(str(sid)))
            b = json.loads(json.dumps(body).replace("{sid}", str(sid))) if body else None
            try:
                self.http_json(method, url, b, self._headers(), timeout=15)
                return 200
            except LLMError as e:
                return int(getattr(e, "status", 0) or 0)
            except (OSError, ValueError):
                return 0
        ok, how = False, ""
        ways = ([self.CANCEL_FOUND[host]] if host in self.CANCEL_FOUND else
                [] if host in self.CANCEL_NONE else list(self.CANCEL_PATHS))
        for method, path, body in ways:
            code = call(method, path, body)
            tried.append(f"{method} {path} → {code or 'нет связи'}")
            if 200 <= code < 300:
                ok, how = True, f"{method} {path}"
                self.CANCEL_FOUND[host] = (method, path, body)
                break
        if not ok and ways and host not in self.CANCEL_FOUND and all(" → 40" in t for t in tried):
            self.CANCEL_NONE.add(host)   # 404/405 на всё — этих адресов у Maverick нет
        if not ok and waiting:   # ждёт ответа человека — отклонить (ничего не выполняется)
            field = self.profile.get("body_field") or "message"
            hid = (hitl or {}).get("id") or ""
            body = {field: "Отмена. Не выполняй это действие и не задавай вопросов — ответ не нужен.",
                    "session_id": sid, "hitl_response": {"id": hid, "response": "Нет, отменить", "answers": {},
                                                        "approved": False}}
            if hid:
                body["hitl_id"] = hid
            try:
                d = self.http_json("POST", self.endpoint(), body, self._headers(), timeout=60)
                st = str(d.get("status") or "") if isinstance(d, dict) else ""
                if re.search(r"(?i)hitl|waiting|approval", st):
                    tried.append(f"ответ «отмена» → всё ещё {st}")
                else:
                    ok, how = True, "ответ «отмена» на вопрос Maverick"
            except (LLMError, OSError) as e:
                tried.append(f"ответ «отмена» → {getattr(e, 'status', None) or type(e).__name__}")
        if owner == "service":   # служебная сессия программы: удалить, если Maverick это умеет
            code = call("DELETE", "/api/v1/sessions/{sid}", None)
            tried.append(f"DELETE /api/v1/sessions/… → {code or 'нет связи'}")
            if 200 <= code < 300:
                ok, how = True, (how + " + удалена" if how else "удалена")
        if ok or owner == "service":
            inflight.drop_session(sid)
        note(f"сессия …{str(sid)[-6:]} ({owner}, {info.get('why') or ('hitl' if waiting else '?')}): "
             f"{'закрыта — ' + how if ok else 'не закрыта'}; попытки: {'; '.join(tried) or '—'}")
        return {"sid": sid, "ok": ok, "how": how, "tried": tried}


def _resolve(sch: Any, comps: dict) -> dict:
    seen = 0
    while isinstance(sch, dict) and "$ref" in sch and seen < 5:
        sch = comps.get(sch["$ref"].rsplit("/", 1)[-1], {})
        seen += 1
    if isinstance(sch, dict) and (sch.get("anyOf") or sch.get("oneOf")):  # Optional[...] в FastAPI
        for alt in sch.get("anyOf") or sch.get("oneOf"):
            alt = _resolve(alt, comps)
            if alt.get("type") != "null":
                return alt
    return sch if isinstance(sch, dict) else {}


def query_schema(spec: dict | None, suffix: str = "/query") -> tuple[dict, dict, bool] | None:
    """Свойства тела POST …/query: (properties, components, multipart?)."""
    if not isinstance(spec, dict):
        return None
    comps = (spec.get("components") or {}).get("schemas") or {}
    for path, ops in (spec.get("paths") or {}).items():
        if not str(path).rstrip("/").endswith(suffix) or not isinstance(ops, dict) or "post" not in ops:
            continue
        content = (ops["post"].get("requestBody") or {}).get("content") or {}
        ctype = "application/json" if "application/json" in content else next(iter(content), "")
        sch = _resolve((content.get(ctype) or {}).get("schema") or {}, comps)
        props: dict = {}
        for part in [sch] + [_resolve(x, comps) for x in sch.get("allOf", [])]:
            props.update(part.get("properties") or {})
        return props, comps, ctype.startswith("multipart/")
    return None


def describe_mode(mode: dict) -> str:
    from .attach import item_fields
    if mode.get("template"):
        return "шаблон из настроек"
    if mode.get("multipart"):
        return f"multipart, поле «{mode['field']}»"
    lst = "[…]" if mode.get("list", True) else ""
    if mode.get("upload"):
        return f"загрузка {mode['upload']['path']} → «{mode['field']}»: {lst or ''}номер файла"
    item = mode.get("item")
    if isinstance(item, dict):
        f = item_fields(item) or {}
        return f"«{mode['field']}»: {lst[:1]}{{" + ", ".join(f"{k}: {r}" for k, r in f.items()) + "}" + lst[-1:]
    return f"«{mode['field']}»: {lst[:1]}{item}{lst[-1:]}"


def busy(data: Any) -> bool:
    """Ответ без текста из-за занятости/лимита — стоит подождать и повторить."""
    if not isinstance(data, dict):
        return False
    return bool(re.search(r"(?i)limit|лимит|too many|rate|занят|busy|overload|перегруж|429|try again|повтор",
                          json.dumps([data.get("error"), data.get("status")], ensure_ascii=False)))


def response_info(data: Any) -> str:
    """Статус и ошибка из ответа Maverick без текста (для журнала): status=failed, error=«…»."""
    if not isinstance(data, dict):
        return f"(ответ — {type(data).__name__})"
    parts = []
    if data.get("status"):
        parts.append(f"status={data['status']}")
    err = data.get("error")
    if err:
        if isinstance(err, dict):
            err = err.get("message") or err.get("detail") or json.dumps(err, ensure_ascii=False)
        parts.append(f"error=«{str(err)[:300]}»")
    for k in ("hitl_pending", "file_artifacts", "tool_calls"):
        v = data.get(k)
        if v:
            parts.append(f"{k}={json.dumps(v, ensure_ascii=False)[:150]}")
    if not parts:
        parts.append("поля: " + ", ".join(list(data)[:12]))
    return "(" + "; ".join(parts) + ")"
