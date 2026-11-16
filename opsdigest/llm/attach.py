"""Передача картинки агенту, чей формат файлов неизвестен (Maverick Agent Core API).

Идея: пробовать варианты тела запроса с картинкой-цифрой и смотреть, что ответит сервер.
• 422 от FastAPI/pydantic подробно говорит, что не так: не хватает поля (loc …files.0.content, type=missing),
  лишнее поле (extra_forbidden), ждали строку/UUID/объект/список, допустимые значения (literal_error). По этим
  подсказкам строится следующий вариант — так формат «выясняется» за несколько запросов даже без описания API.
• 200 — смотрим ответ модели: назвала цифру (в том числе словом — «семь») → формат рабочий.
Всё, что происходило, складывается в журнал попыток (без ключей и без содержимого картинки).
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from .base import data_url

DIGIT_WORDS = {
    "0": r"ноль|нуль|zero", "1": r"один|единиц\w*|one", "2": r"два|двойк\w*|two", "3": r"три|тройк\w*|three",
    "4": r"четыре|ч[её]тв[её]рк\w*|four", "5": r"пять|пят[её]рк\w*|five", "6": r"шесть|ш[её]ст[её]рк\w*|six",
    "7": r"семь|сем[её]рк\w*|seven", "8": r"восемь|восьм[её]рк\w*|eight", "9": r"девять|девятк\w*|nine",
}
UPLOAD_GUESSES = ("/api/v1/files", "/api/v1/files/upload", "/api/v1/upload", "/api/v1/attachments",
                  "/api/v1/documents")
OPENAPI_PATHS = ("/openapi.json", "/api/openapi.json", "/api/v1/openapi.json", "/docs/openapi.json",
                 "/api/docs/openapi.json", "/api/v1/docs/openapi.json", "/swagger.json", "/api/swagger.json",
                 "/api/v1/swagger.json", "/v3/api-docs")
_B64_RUN = re.compile(r"(?:data:[\w/+.-]+;base64,)?[A-Za-z0-9+/=]{60,}")


def digits_in(text: str) -> set[str]:
    """Цифры из ответа модели: «7», «Цифра 7.», «семь», «Это семёрка», «seven»."""
    t = (text or "").lower()
    out = set(re.findall(r"\d", t))
    for d, words in DIGIT_WORDS.items():
        if re.search(rf"(?<![а-яёa-z])(?:{words})(?![а-яёa-z])", t):
            out.add(d)
    return out


def short_detail(body: Any, limit: int = 400) -> str:
    """Суть ответа сервера об ошибке — без эха нашего запроса и без base64."""
    if isinstance(body, dict) and isinstance(body.get("detail"), list):
        parts = []
        for e in body["detail"]:
            if not isinstance(e, dict):
                parts.append(str(e)[:120])
                continue
            loc = ".".join(str(x) for x in (e.get("loc") or []))
            ctx = e.get("ctx") or {}
            exp = f", ожидалось {ctx['expected']}" if isinstance(ctx, dict) and ctx.get("expected") else ""
            parts.append(f"{loc}: {e.get('msg', '')} [{e.get('type', '')}{exp}]")
        s = "; ".join(parts)
    elif isinstance(body, dict):
        b = {k: v for k, v in body.items() if k not in ("input",)}
        s = json.dumps(b, ensure_ascii=False)
    else:
        s = str(body or "")
    s = _B64_RUN.sub("…", s)
    return s[:limit] + ("…" if len(s) > limit else "")


# ---------------------------------------------------------------- поля объекта-файла
def guess_role(key: str) -> str:
    k = key.lower()
    if "url" in k:
        return "data_url"
    if re.search(r"mime|content_?type|media_?type|mimetype", k):
        return "mime"
    if "name" in k:
        return "name"
    if re.fullmatch(r"type|kind|file_?type|category", k):
        return "kind"
    if re.search(r"size|length", k):
        return "size"
    if re.search(r"ext(ension)?$", k):
        return "ext"
    if "encoding" in k:
        return "const:base64"
    return "b64"


def item_fields(item: Any) -> dict[str, str] | None:
    """Поля объекта-файла как {ключ: роль}; поддерживается и старая запись {data, name, mime, data_url}."""
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("fields"), dict):
        return dict(item["fields"])
    f: dict[str, str] = {}
    if item.get("data"):
        f[item["data"]] = "data_url" if item.get("data_url") else "b64"
    if item.get("name"):
        f[item["name"]] = "name"
    if item.get("mime"):
        f[item["mime"]] = "mime"
    for k, v in (item.get("const") or {}).items():
        f[k] = f"const:{v}"
    return f


def build_item(fields: dict[str, str], name: str, mime: str, data: bytes) -> dict:
    out: dict[str, Any] = {}
    for key, role in fields.items():
        if role == "b64":
            out[key] = base64.b64encode(data).decode("ascii")
        elif role == "data_url":
            out[key] = data_url(data, mime)
        elif role == "name":
            out[key] = name
        elif role == "mime":
            out[key] = mime
        elif role == "kind":
            out[key] = "image"
        elif role == "size":
            out[key] = len(data)
        elif role == "ext":
            out[key] = name.rsplit(".", 1)[-1]
        elif role.startswith("const:"):
            out[key] = role[6:]
    return out


OBJECT_DEFAULTS = (
    {"filename": "name", "content": "b64", "content_type": "mime"},
    {"name": "name", "content": "b64", "mime_type": "mime"},
    {"filename": "name", "data": "b64", "mime_type": "mime"},
    {"file_name": "name", "file_content": "b64", "file_type": "mime"},
    {"name": "name", "data": "data_url", "type": "mime"},
)


def defaults_for(field: str) -> list[dict]:
    """Типичные варианты для поля с файлами (когда о формате ничего не известно)."""
    out = [{"field": field, "list": True, "item": {"fields": dict(f)}} for f in OBJECT_DEFAULTS]
    out += [{"field": field, "list": True, "item": "data_url"}, {"field": field, "list": True, "item": "base64"}]
    return out


# ---------------------------------------------------------------- подстройка по ответу 422
def adapt(mode: dict, body: Any, uploads: list[str]) -> list[dict]:
    """Новые варианты по ошибке валидации (FastAPI/pydantic v1 и v2). Пустой список — подсказок нет."""
    errs = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(errs, list) or "field" not in mode or mode.get("multipart") or mode.get("template"):
        return []
    f = mode["field"]
    missing: list[str] = []
    forbidden: list[str] = []
    enums: dict[str, str] = {}
    want_list = want_obj = want_str = want_uuid = False
    for e in errs:
        if not isinstance(e, dict):
            continue
        loc = list(e.get("loc") or [])
        if loc and loc[0] == "body":
            loc = loc[1:]
        if not loc or loc[0] != f:
            continue
        rest = loc[1:]
        if rest and isinstance(rest[0], int):
            rest = rest[1:]
        t = str(e.get("type") or "")
        if not rest:
            if "list" in t:
                want_list = True
            elif "uuid" in t:
                want_uuid = True
            elif t in ("string_type", "type_error.str", "int_parsing", "int_type", "type_error.integer"):
                want_str = True
            elif t in ("dict_type", "model_type", "model_attributes_type", "type_error.dict", "dataclass_type"):
                want_obj = True
            continue
        key = rest[0]
        if not isinstance(key, str):
            continue
        if t == "missing" or t.endswith(".missing"):
            missing.append(key)
        elif "extra" in t:
            forbidden.append(key)
        elif t in ("literal_error", "enum", "type_error.enum"):
            exp = str((e.get("ctx") or {}).get("expected") or "")
            vals = re.findall(r"'([^']+)'", exp) or [v.strip() for v in exp.split(",") if v.strip()]
            if vals:
                enums[key] = next((v for v in vals if "image" in v.lower() or "изобр" in v.lower()), vals[0])
    out: list[dict] = []
    if want_list and not mode.get("list", True):
        out.append({**mode, "list": True})
    if want_obj and not isinstance(mode.get("item"), dict):
        out += [{"field": f, "list": mode.get("list", True), "item": {"fields": dict(x)}} for x in OBJECT_DEFAULTS[:3]]
    if want_uuid or want_str:
        out += [{"field": f, "list": mode.get("list", True), "upload": {"path": p, "form_field": "file"}} for p in uploads]
        if want_str and mode.get("item") not in ("data_url", "base64"):
            out += [{"field": f, "list": mode.get("list", True), "item": "data_url"},
                    {"field": f, "list": mode.get("list", True), "item": "base64"}]
    if missing or forbidden or enums:
        fields = item_fields(mode.get("item")) or {}
        for k in forbidden:
            fields.pop(k, None)
        for k in missing:
            fields.setdefault(k, guess_role(k))
        for k, v in enums.items():
            fields[k] = f"const:{v}"
        if fields and not any(r in ("b64", "data_url") for r in fields.values()):
            fields.setdefault("content", "b64")
        out.append({"field": f, "list": mode.get("list", True), "item": {"fields": fields}})
    return out


# ---------------------------------------------------------------- описание API (для журнала)
def _res(sch: Any, comps: dict, depth: int = 0) -> dict:
    while isinstance(sch, dict) and "$ref" in sch and depth < 6:
        sch = comps.get(sch["$ref"].rsplit("/", 1)[-1], {})
        depth += 1
    if isinstance(sch, dict) and (sch.get("anyOf") or sch.get("oneOf")):
        alts = [_res(a, comps, depth + 1) for a in sch.get("anyOf") or sch.get("oneOf")]
        alts = [a for a in alts if a.get("type") != "null"]
        if alts:
            return alts[0]
    return sch if isinstance(sch, dict) else {}


def _type(sch: dict, comps: dict, depth: int = 0) -> str:
    sch = _res(sch, comps)
    if sch.get("type") == "array":
        return f"[{_type(sch.get('items') or {}, comps, depth + 1)}]"
    if (sch.get("type") == "object" or sch.get("properties")) and depth < 2:
        props = sch.get("properties") or {}
        req = set(sch.get("required") or [])
        return "{" + ", ".join(f"{k}{'*' if k in req else ''}: {_type(v, comps, depth + 1)}"
                               for k, v in props.items()) + "}"
    t = sch.get("type") or "?"
    if sch.get("format"):
        t += f"({sch['format']})"
    if sch.get("enum"):
        t += "=" + "|".join(str(x) for x in sch["enum"][:6])
    return t


def schema_summary(spec: dict | None) -> list[str]:
    """Методы, которые принимают тело запроса (POST/PUT), с полями и типами — без значений."""
    if not isinstance(spec, dict):
        return []
    comps = (spec.get("components") or {}).get("schemas") or {}
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method in ("post", "put"):
            op = ops.get(method)
            if not isinstance(op, dict):
                continue
            content = (op.get("requestBody") or {}).get("content") or {}
            if not content:
                continue
            for ctype, c in content.items():
                out.append(f"{method.upper()} {path} [{ctype}]: {_type((c or {}).get('schema') or {}, comps)}")
    return out


def upload_paths(spec: dict | None) -> list[str]:
    """Адреса, куда можно загрузить файл (POST multipart), из описания API; иначе — типичные."""
    found = []
    if isinstance(spec, dict):
        for path, ops in (spec.get("paths") or {}).items():
            op = (ops or {}).get("post") if isinstance(ops, dict) else None
            if isinstance(op, dict) and "multipart/form-data" in ((op.get("requestBody") or {}).get("content") or {}):
                if not str(path).rstrip("/").endswith("/query"):
                    found.append(path)
    return found or list(UPLOAD_GUESSES)
