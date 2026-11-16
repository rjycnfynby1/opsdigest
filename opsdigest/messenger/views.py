"""Как сообщения и чаты из базы выглядят для окна мессенджера (JSON для index.html / messenger.js)."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterable

from ..storage import Store

TAG_RE = re.compile(r"^\[([^\]]*)\]\s*")


def media_view(x: dict | None) -> dict | None:
    if not x:
        return None
    return {"id": x["id"], "kind": x.get("kind"), "st": x.get("status"), "name": x.get("name"), "size": x.get("size"),
            "w": x.get("width"), "h": x.get("height"), "dur": x.get("duration"), "mime": x.get("mime"),
            "desc": x.get("summary") or ""}


def snippet(text: str, n: int = 140) -> str:
    """Короткий текст для превью: без пометки «[фото] », а если текста нет — сама пометка."""
    text = text or ""
    m = TAG_RE.match(text)
    body = text[m.end():] if m else text
    body = " ".join(body.split())
    if not body and m:
        body = m.group(1)
    return body[:n] + ("…" if len(body) > n else "")


def _meta(r: sqlite3.Row) -> dict:
    try:
        return json.loads(r["meta"]) if r["meta"] else {}
    except (ValueError, IndexError, KeyError):
        return {}


def msg_views(store: Store, chat_id: int, rows: Iterable[sqlite3.Row]) -> list[dict]:
    rows = list(rows)
    ids = [r["msg_id"] for r in rows]
    media = store.media_of(chat_id, ids) if ids else {}
    by_id = {r["msg_id"]: r for r in rows}
    want = {r["reply_to"] for r in rows if r["reply_to"]} - set(by_id)
    if want:
        by_id.update({r["msg_id"]: r for r in store.rows_by_ids(chat_id, want)})
    out = []
    for r in sorted(rows, key=lambda r: r["msg_id"]):
        v: dict = {"id": r["msg_id"], "chat": chat_id, "date": r["date"], "out": bool(r["is_out"]),
                   "sid": r["sender_id"], "from": r["sender"] or "", "text": r["text"] or "",
                   "mention": bool(r["mentioned"])}
        if "topic" in r.keys() and r["topic"]:
            v["topic"] = r["topic"]
        meta = _meta(r)
        if meta:
            x = meta.get("rx_")
            if isinstance(x, dict) and x.get("c"):
                c = store.get_chat(int(x["c"]))
                if c is not None:
                    x["title"] = c.title
                    rr = store.rows_by_ids(int(x["c"]), [x["id"]]) if x.get("id") else []
                    if rr and not x.get("q"):
                        x["q"] = snippet(rr[0]["text"] or "", 120)
                        x.setdefault("from", "Вы" if rr[0]["is_out"] else (rr[0]["sender"] or ""))
            v["m"] = meta
        x = media.get(r["msg_id"])
        if x:
            v["media"] = media_view(x)
        if r["reply_to"]:
            v["reply"] = r["reply_to"]
            rr = by_id.get(r["reply_to"])
            if rr is not None:
                v["rp"] = {"from": "Вы" if rr["is_out"] else (rr["sender"] or ""), "text": snippet(rr["text"] or "", 90)}
        out.append(v)
    return out


def chat_view(r: sqlite3.Row) -> dict:
    keys = r.keys()
    g = lambda k, d=None: r[k] if k in keys and r[k] is not None else d  # noqa: E731
    v = {"id": r["chat_id"], "title": r["title"] or "", "kind": r["kind"] or "", "username": r["username"],
         "unread": g("unread", 0), "muted": bool(g("muted", 0)), "pinned": bool(g("pinned", 0)),
         "archived": bool(g("archived", 0)), "mentions": g("mentions", 0), "read_in": g("read_in", 0),
         "read_out": g("read_out", 0), "contact": bool(g("contact", 0)), "excluded": (g("priority") == "ignore"),
         "prio": g("priority", "normal"), "mode": g("mode", "chat"), "note": g("note", ""),
         "forum": bool(g("forum", 0)), "photo": g("photo", "") or "",
         "date": g("l_date") or g("top_date") or ""}
    if g("l_id"):
        v["last"] = {"id": r["l_id"], "text": snippet(g("l_text", ""), 120), "from": g("l_sender", ""),
                     "out": bool(g("l_out", 0)), "date": g("l_date", "")}
    if v["date"] and g("top_date") and g("top_date") > v["date"]:
        v["date"] = g("top_date")
    return v


def chat_views(store: Store) -> list[dict]:
    out = [chat_view(r) for r in store.chat_list()]
    # закреплённые — сверху, внутри групп — новые первыми
    pinned = sorted((c for c in out if c["pinned"]), key=lambda c: c["date"] or "", reverse=True)
    rest = sorted((c for c in out if not c["pinned"]), key=lambda c: c["date"] or "", reverse=True)
    return pinned + rest


def one_chat(store: Store, chat_id: int) -> dict | None:
    rows = store.chat_list(chat_id=chat_id)
    return chat_view(rows[0]) if rows else None
