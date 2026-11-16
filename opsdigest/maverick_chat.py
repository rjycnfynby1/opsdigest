"""Ассистент «как в чате на портале Maverick»: прямой разговор с Maverick из беседы ассистента.

Отличия от обычного ответа ассистента (который сам ищет в Telegram и плагинах и собирает ответ):
  * вопрос уходит в Maverick как есть (через обезличивание: имена → коды; в этом режиме карта кодов одна на беседу,
    пока программа открыта, — иначе Maverick в своей истории видел бы одного человека под разными кодами);
  * беседа продолжается в той же сессии Maverick (session_id хранится в беседе) — он помнит, о чём говорили;
  * вложения пользователя загружаются в Maverick (как в веб-версии: /api/v1/attachments/upload → поле запроса),
    файлы из ответа Maverick (file_artifacts) скачиваются и показываются в беседе, картинки — описываются;
  * если Maverick спрашивает или просит подтвердить (hitl_pending) — вопрос показывается с вариантами, ответ
    пользователя уходит следующим сообщением в ту же сессию.
Формат hitl_pending и file_artifacts в документации Maverick не описан: программа берёт известные поля и пишет форму
ответа (только названия полей и типы, без содержимого) в data/maverick-chat.txt — по нему формат уточняется.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any, Callable

from . import logs
from .storage import Store

log = logs.get("maverick_chat")
Progress = Callable[[str], None]

DEFAULT_UPLOAD = {"path": "/api/v1/attachments/upload", "form_field": "file", "as": "key:artifact_id"}
PORTAL_PREAMBLE = ("Сообщение пользователя из программы OpsDigest (рабочий помощник инженера 2-й линии поддержки). "
                   "Отвечай как в обычном чате Maverick: можешь пользоваться подключёнными плагинами, задавать "
                   "уточняющие вопросы и прикладывать файлы.")


def _shape(x: Any, depth: int = 0) -> Any:
    """Форма ответа без содержимого: поля и типы (для журнала — понять формат hitl/артефактов)."""
    if depth > 3:
        return type(x).__name__
    if isinstance(x, dict):
        return {k: _shape(v, depth + 1) for k, v in list(x.items())[:20]}
    if isinstance(x, list):
        return [_shape(x[0], depth + 1)] if x else []
    return type(x).__name__


def _journal(cfg, note: str) -> None:
    try:
        f = cfg.path("data/maverick-chat.txt")
        f.parent.mkdir(parents=True, exist_ok=True)
        old = f.read_text(encoding="utf-8")[-20000:] if f.exists() else ""
        f.write_text(old + f"[{datetime.now():%d.%m %H:%M:%S}] {note}\n", encoding="utf-8")
    except OSError:
        pass


def hitl_view(h: Any) -> dict | None:
    """hitl_pending → {question, options, id} (что известно; форма Maverick не описана)."""
    if not h:
        return None
    if isinstance(h, list):
        h = h[0] if h else None
    if isinstance(h, str):
        return {"question": h[:1000], "options": [], "id": ""}
    if not isinstance(h, dict):
        return None
    q = next((str(h[k]) for k in ("question", "prompt", "message", "text", "title", "description", "reason")
              if isinstance(h.get(k), str) and h[k].strip()), "")
    opts: list[str] = []
    for k in ("options", "choices", "actions", "buttons", "answers", "variants"):
        v = h.get(k)
        if isinstance(v, list):
            for o in v:
                if isinstance(o, str):
                    opts.append(o)
                elif isinstance(o, dict):
                    lbl = next((str(o[x]) for x in ("label", "title", "text", "value", "name") if o.get(x)), "")
                    if lbl:
                        opts.append(lbl)
            break
    if not opts and any(k in h for k in ("approve", "confirm", "tool", "tool_name", "action")):
        opts = ["Да, выполнить", "Нет, отменить"]
    hid = next((str(h[k]) for k in ("id", "hitl_id", "request_id", "pending_id", "interrupt_id", "form_id") if h.get(k)), "")
    form = _form_fields(h)
    tc = h.get("tool_call") if isinstance(h.get("tool_call"), dict) else h.get("function") if isinstance(h.get("function"), dict) else {}
    tool = h.get("tool_name") or (h.get("tool") if isinstance(h.get("tool"), str) else "") or tc.get("name") or \
        (h.get("name") if any(k in h for k in ("arguments", "args", "tool_input")) else "")
    tool = str(tool or "")[:120]
    args = h.get("arguments") or h.get("args") or h.get("tool_input") or tc.get("arguments") or h.get("input")
    if not q and tool:
        q = f"Maverick просит подтвердить действие «{tool}»"
    if args and isinstance(args, (dict, list)):
        q += "\n" + json.dumps(args, ensure_ascii=False)[:600]
    if not opts and not form:
        opts = ["Да, выполнить", "Нет, отменить"]
    return {"question": q or "Maverick ждёт вашего ответа", "options": opts[:8], "id": hid, "tool": tool, "form": form}


FORM_KEYS = ("fields", "questions", "inputs", "form", "schema", "properties", "items", "elements")


def _form_fields(h: dict) -> list[dict]:
    """Форма Maverick (несколько вопросов): [{name, label, type, options, required}] — что удалось распознать."""
    src = None
    for k in FORM_KEYS:
        v = h.get(k)
        if isinstance(v, dict) and k in ("form", "schema"):
            v = v.get("fields") or v.get("questions") or v.get("properties") or v.get("items")
        if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values()):  # JSON Schema properties
            v = [dict(x, name=n) for n, x in v.items()]
        if isinstance(v, list) and v:
            src = v
            break
    out = []
    for i, f in enumerate(src or []):
        if isinstance(f, str):
            out.append({"name": f"q{i + 1}", "label": f[:300], "type": "text", "options": [], "required": False})
            continue
        if not isinstance(f, dict):
            continue
        label = next((str(f[k]) for k in ("label", "question", "title", "text", "prompt", "description", "name")
                      if isinstance(f.get(k), str) and f[k].strip()), f"Вопрос {i + 1}")
        opts = []
        for k in ("options", "choices", "enum", "values", "variants", "answers"):
            v = f.get(k)
            if isinstance(v, list):
                for o in v:
                    if isinstance(o, dict):
                        o = next((o[x] for x in ("label", "title", "text", "value", "name") if o.get(x)), "")
                    if str(o).strip():
                        opts.append(str(o)[:200])
                break
        out.append({"name": str(f.get("name") or f.get("id") or f.get("key") or f"q{i + 1}")[:80], "label": label[:400],
                    "type": "choice" if opts else ("bool" if str(f.get("type")) in ("boolean", "bool") else "text"),
                    "options": opts[:12], "required": bool(f.get("required"))})
    return out[:20]


HITL_KEY = re.compile(r"(?i)hitl|pending|approv|confirm|interrupt|human|form|question|input_request|clarif")


def hitl_find(data: Any) -> dict | None:
    """Где в ответе Maverick вопрос/форма: hitl_pending, другие поля с hitl/approval/form/question в имени, последнее
    сообщение в messages, ожидающие tool_calls. Статус hitl_waiting без распознанной формы — общий вопрос."""
    if not isinstance(data, dict):
        return None
    cands: list[Any] = []
    if data.get("hitl_pending"):
        cands.append(data["hitl_pending"])
    for k, v in data.items():
        if k != "hitl_pending" and v and HITL_KEY.search(k) and isinstance(v, (dict, list, str)) and k not in ("status",):
            cands.append(v)
    msgs = data.get("messages")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and (HITL_KEY.search(str(m.get("type") or m.get("kind") or ""))
                                        or any(HITL_KEY.search(k) for k in m if k not in ("role", "content", "text"))):
                cands.append(m)
                break
    status = str(data.get("status") or "")
    waiting = bool(re.search(r"(?i)hitl|waiting|input|approval|confirm", status))
    if waiting:
        tc = [t for t in data.get("tool_calls") or [] if isinstance(t, dict)
              and not re.search(r"(?i)done|complete|success|finished", str(t.get("status") or ""))]
        if tc:
            cands.append(tc[-1])
    for c in cands:
        v = hitl_view(c)
        if v:
            return v
    if waiting:
        return {"question": "Maverick ждёт вашего ответа или подтверждения, но форму программа не распознала — ответьте "
                            "текстом (или «да» / «нет»); если не поможет — ответьте в веб-чате Maverick.",
                "options": ["Да", "Нет"], "id": "", "tool": "", "form": [], "unknown": True}
    return None


def artifacts_of(data: Any) -> list[dict]:
    """file_artifacts (и похожие поля) → [{id, name, mime, url}]."""
    if not isinstance(data, dict):
        return []
    out = []
    for key in ("file_artifacts", "artifacts", "files", "attachments"):
        v = data.get(key)
        for a in v if isinstance(v, list) else []:
            if isinstance(a, str):
                a = {"id": a}
            if not isinstance(a, dict):
                continue
            aid = next((str(a[k]) for k in ("artifact_id", "id", "file_id", "attachment_id") if a.get(k)), "")
            url = str(a.get("download_url") or a.get("url") or "")
            if not aid and not url:
                continue
            out.append({"id": aid, "url": url, "name": str(a.get("filename") or a.get("name") or a.get("title") or aid or "файл")[:200],
                        "mime": str(a.get("content_type") or a.get("mime_type") or a.get("mime") or "")})
        if out:
            break
    return out[:12]


def converse(cfg, store: Store, tid: int, text: str, files: list[dict], *, meta: dict,
             save_file: Callable[[str, str, bytes], dict], progress: Progress = print,
             answers: dict | None = None) -> dict:
    """Одно сообщение в сессию Maverick. files — вложения пользователя [{name, mime, data}];
    save_file(name, mime, bytes) → запись о файле беседы (для файлов из ответа). Возвращает {text, files, ask, session}."""
    from . import privacy
    from .llm import LLMError, make_client
    from .llm.providers import busy
    name, prof = cfg.llm_profile("maverick")
    prof = copy.deepcopy(prof)
    prof["preamble"] = ""
    llm = make_client(name, prof)
    priv = privacy.attach(llm, cfg, store)
    msg = text
    if priv is not None:
        req = priv.begin(f"asst:{tid}")
        msg = req.preamble() + req.outgoing(text)
        priv.remember(req, "", msg)
    else:
        req = None
    msg = PORTAL_PREAMBLE + "\n\n" + msg
    field = prof.get("body_field") or "message"
    body: dict = {field: msg}
    sid = meta.get("mav_session")
    if sid:
        body["session_id"] = sid
    hitl = meta.get("mav_hitl") if isinstance(meta.get("mav_hitl"), dict) else None
    if hitl:  # это ответ на вопрос/форму Maverick: кроме текста — его номер и ответы по полям
        body.update({"hitl_response": {"id": hitl.get("id") or "", "response": text, "answers": answers or {},
                                       "approved": not re.match(r"(?i)\s*(нет|no|отмен|откл)", text or "")}})
        if hitl.get("id"):
            body["hitl_id"] = hitl["id"]
    if files:
        mode = prof.get("image_mode") if isinstance(prof.get("image_mode"), dict) else {}
        up = mode.get("upload") or DEFAULT_UPLOAD
        att_field = mode.get("field") if mode.get("upload") else "attachments"
        items = []
        for f in files:
            progress(f"Загружаю в Maverick: {f['name']}…")
            try:
                items.append(llm._upload(up, f["name"], f.get("mime") or "application/octet-stream", f["data"], sid))
            except (LLMError, OSError) as e:
                raise LLMError(f"Maverick не принял вложение «{f['name']}»: {str(e)[:200]}") from None
        body[att_field or "attachments"] = items
    progress("Maverick думает…")
    data = None
    from .llm import inflight
    if sid:   # пока ждём ответа, сессия занята — сводка может её остановить
        inflight.note_session(sid, "busy", host=llm.host(), owner="portal")
    try:
        for attempt in range(6):
            try:
                data = llm.http_json("POST", llm.endpoint(), body, llm._headers(), timeout=float(prof.get("timeout") or 600))
            except inflight.Cancelled as e:
                raise LLMError(f"остановлено: {e}") from None
            except LLMError as e:
                if attempt < 5 and (getattr(e, "busy", False) or getattr(e, "status", None) in (502, 503, 504)):
                    progress("Maverick занят (идёт другой запрос) — повторю через 20 с…")
                    inflight.sleep(20)
                    continue
                raise
            if busy(data) and not llm._answer(data) and attempt < 5:
                progress("Maverick занят — повторю через 20 с…")
                inflight.sleep(20)
                continue
            break
    except inflight.Cancelled as e:
        raise LLMError(f"остановлено: {e}") from None
    finally:
        inflight.drop_session(sid)
    if not isinstance(data, dict):
        raise LLMError("Maverick вернул не JSON")
    _journal(cfg, "ответ: " + json.dumps(_shape(data), ensure_ascii=False)[:1500])
    if re.search(r"(?i)hitl|waiting|input|approval", str(data.get("status") or "")) or data.get("hitl_pending"):
        hit = {k: v for k, v in data.items() if HITL_KEY.search(k) or k in ("status", "tool_calls", "error")}
        _journal(cfg, "вопрос Maverick (hitl), как пришёл: " + json.dumps(hit, ensure_ascii=False, default=str)[:6000])
    answer = llm._answer(data) or ""
    if req is not None and answer:
        answer = req.incoming(answer)
    new_sid = data.get("session_id") or sid
    ask = hitl_find(data)
    if ask and req is not None:
        ask["question"] = req.incoming(ask["question"])
        ask["options"] = [req.incoming(o) for o in ask["options"]]
        for f in ask.get("form") or []:
            f["label"] = req.incoming(f["label"])
            f["options"] = [req.incoming(o) for o in f["options"]]
    if ask:
        meta_hitl = {"id": ask.get("id") or "", "tool": ask.get("tool") or "", "form": [f["name"] for f in ask.get("form") or []]}
        ask["pending"] = meta_hitl
        inflight.note_session(new_sid, "hitl", host=llm.host(), hitl=meta_hitl, owner="portal")
    else:
        inflight.drop_session(new_sid)
    got: list[dict] = []
    for a in artifacts_of(data):
        try:
            b, mime = _download(llm, a)
            got.append(save_file(a["name"], a["mime"] or mime, b))
        except Exception as e:  # noqa: BLE001 — файл не скачался: показываем, что он был
            got.append({"name": a["name"], "error": str(e)[:160], "remote": a.get("id") or a.get("url")})
    if not answer and not ask and not got:
        err = data.get("error") or data.get("status") or "пустой ответ"
        raise LLMError(f"Maverick не ответил: {str(err)[:300]}")
    if hitl and hitl.get("id") and ask and (ask.get("id") or "") == hitl["id"]:  # тот же вопрос — ответ не принят
        _journal(cfg, "ответ на вопрос Maverick не принят: тот же hitl " + hitl["id"])
        ask["question"] += ("\n\n⚠ Maverick повторил вопрос — ответ, похоже, нужно дать в его веб-чате (кнопка в "
                            "«Источниках» → «Открыть чат Maverick»).")
    return {"text": answer.strip(), "files": got, "ask": ask, "session": new_sid, "status": str(data.get("status") or "")}


def _download(llm, a: dict) -> tuple[bytes, str]:
    import urllib.parse
    import urllib.request
    host = llm.host()
    url = a.get("url") or ""
    if url and not re.match(r"https?://", url):
        url = host + "/" + url.lstrip("/")
    if not url:
        url = f"{host}/api/v1/artifacts/{urllib.parse.quote(a['id'])}/download"
    headers = llm._headers() if url.startswith(host) else {}
    req = urllib.request.Request(url, headers=headers)
    from .llm import inflight
    with inflight.urlopen(req, timeout=120, context=llm._ssl_context()) as r:  # noqa: S310 — адрес Maverick
        b = r.read(64 * 2**20 + 1)
        if len(b) > 64 * 2**20:
            raise ValueError("файл больше 64 МБ")
        return b, (r.headers.get("Content-Type") or "").split(";")[0]
