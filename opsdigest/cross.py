"""Общая картина за период: сводка Telegram, сверенная с почтой, ITSM, вики, X5 Rooms, трекером xTracker и встречами
Scriber AI (через плагины Maverick).

Два уровня (решение пользователя 08.11):
  1. Telegram — обычная сводка (map/reduce): переписка уходит в модель только обезличенной, в каждом запросе свои коды.
  2. Сверка — из готовой сводки берутся только очищенные пункты: суть, статус, номера, системы, роль участника
     (без текста сообщений, без личных переписок). Только они — и тоже через обезличивание — встречаются в одном
     запросе с почтой, ITSM и вики. Сырая переписка Telegram никогда не идёт в один запрос с другими источниками.

Сверка в обе стороны:
  • Telegram → почта и ITSM: пункты Telegram проверяются в почте (плагин почты), их номера — в ITSM;
  • почта → Telegram: по номерам и ключевым словам писем — поиск по базе Telegram на этом компьютере (без модели);
    в общую картину попадает только, в каком чате это обсуждалось, и сводка этого чата (не сообщения).
Один человек в одном запросе сверки — один код, где бы он ни встретился (в пункте Telegram или в письме), поэтому
модель может сопоставить участников, не узнавая имён.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Callable

from . import logs, plugins, prompts
from .storage import Store, iso

log = logs.get("cross")
Progress = Callable[[str], None]

TASK = """Ниже — очищенные данные за период из нескольких источников пользователя: пункты сводки Telegram (без текста
сообщений), письма (через плагин почты), сообщения X5 Rooms, задачи трекера xTracker, итоги встреч (Scriber AI),
карточки ITSM (инциденты, проблемы, изменения), статьи вики. Имена людей заменены кодами: один код — один человек
во всех источниках.

Собери общую картину: сгруппируй всё, что относится к одному событию, проблеме или задаче (по номерам, системам,
участникам, времени), в «нити». Для каждой нити: что известно по всем источникам вместе, текущий статус (ITSM главнее
слов в чате, если они расходятся — отметь расхождение), что требуется от пользователя. Отдельно — что есть только в
Telegram и чего нет в почте/ITSM (возможно, не заведено или не сообщено), и что есть только в почте, Rooms, трекере или
на встречах и не обсуждалось в Telegram (особенно поручения пользователю со встреч). Ничего не выдумывай: только то,
что есть в данных.

Строго JSON:
{"headline": "2–4 предложения: главное за период по всем источникам",
 "threads": [{"title": "событие/проблема/задача", "importance": 1-5, "status": "статус",
              "sources": ["telegram", "mail", "itsm", "wiki", "rooms", "tracker", "meet"], "tg": ["t1"], "mail": ["m1"],
              "itsm": ["INC…"], "rooms": ["r1"], "tracker": ["ABC-123"], "meet": ["s1"],
              "summary": "что известно по всем источникам", "participants": ["Ч12 — роль"],
              "conflicts": "расхождения между источниками или пусто", "next": "что делать пользователю или пусто",
              "to_me": true}],
 "only_telegram": [{"tg": "t3", "why": "чего не хватает (нет инцидента, нет письма…)"}],
 "only_mail": [{"mail": "m4", "why": "почему стоит обратить внимание"}],
 "only_other": [{"source": "rooms | tracker | meet", "ref": "r2 | ABC-7 | s1", "why": "почему стоит обратить внимание"}],
 "actions": [{"title": "что сделать", "why": "почему", "sources": ["mail", "itsm"]}]}"""


def _personal(c: dict) -> bool:
    return (c.get("kind") or "") == "private"


def points(data: dict, cfg, numbers_rx) -> tuple[list[dict], dict]:
    """Очищенные пункты сводки Telegram для сверки: без текста сообщений и без личных переписок.

    Возвращает (пункты для запроса, служебное: id → {chat_key, chat_id, title, msgs, links})."""
    include_private = bool((cfg.digest or {}).get("cross_private"))
    out, back = [], {}
    n = 0
    chats = sorted(data.get("chats") or [], key=lambda c: -int(c.get("importance") or 1))
    for c in chats:
        if _personal(c) and not include_private:
            continue
        its = c.get("items") or []
        for it in its[:8]:
            n += 1
            pid = f"t{n}"
            text = f"{it.get('title') or ''} {it.get('details') or ''}"
            p = {"id": pid, "chat": c["title"], "type": it.get("type"), "title": (it.get("title") or "")[:200],
                 "details": (it.get("details") or "")[:300], "chat_status": c.get("status") or "",
                 "numbers": plugins.ticket_numbers(cfg, text, pats=numbers_rx)}
            if it.get("owner_name"):
                p["owner"] = it["owner_name"][:80]
            out.append({k: v for k, v in p.items() if v})
            back[pid] = {"chat_key": c["key"], "chat_id": c.get("chat_id"), "chat": c["title"], "msgs": it.get("msgs") or [],
                         "links": it.get("links") or [], "title": it.get("title") or "", "avatar": c.get("avatar")}
        if not its and c.get("summary") and int(c.get("importance") or 1) >= 2:
            n += 1
            pid = f"t{n}"
            out.append({"id": pid, "chat": c["title"], "type": "summary", "title": c["summary"][:300],
                        "numbers": plugins.ticket_numbers(cfg, c["summary"], pats=numbers_rx)})
            back[pid] = {"chat_key": c["key"], "chat_id": c.get("chat_id"), "chat": c["title"], "msgs": [],
                         "links": [{"url": c.get("link")}] if c.get("link") else [], "title": c["summary"][:120]}
        if len(out) >= 70:
            break
    by_key = {c["key"]: c for c in data.get("chats") or []}
    for x in (data.get("overview") or {}).get("incidents") or []:
        keys = [k for k in x.get("chats") or [] if k in by_key]
        work = [k for k in keys if include_private or not _personal(by_key[k])]
        if keys and not work:  # авария только из личных переписок — в сверку не идёт
            continue
        n += 1
        pid = f"t{n}"
        nums = [r for r in x.get("refs") or [] if plugins.ticket_numbers(cfg, str(r), pats=numbers_rx)]
        out.append({k: v for k, v in {"id": pid, "type": "incident", "title": x.get("title"), "status": x.get("status"),
                                      "details": (x.get("details") or "")[:300], "numbers": nums}.items() if v})
        back[pid] = {"chat_key": work[0] if work else None, "chat": ", ".join(by_key[k]["title"] for k in work),
                     "msgs": [], "links": [], "title": x.get("title") or ""}
    return out, back


def tg_mentions(store: Store, mail: list[dict], since: datetime, until: datetime, data: dict, cfg) -> dict:
    """Почта → Telegram: где в Telegram (на этом компьютере) обсуждалось то же, что в письме. Без модели.
    Возвращает {id письма: [{"chat", "chat_id", "msgs", "summary"}]} — только чаты и их сводки, не сообщения."""
    from .messenger.ai import fts, key_terms
    include_private = bool((cfg.digest or {}).get("cross_private"))
    by_chat = {c.get("chat_id"): c for c in data.get("chats") or []}
    s = iso(since - timedelta(days=7))
    out: dict[str, list[dict]] = {}
    for x in mail:
        d = x.get("data") or {}
        text = " ".join(str(d.get(k) or "") for k in ("subject", "summary", "thread")) + " " + " ".join(d.get("numbers") or [])
        terms = key_terms(text, 6)
        q = fts(terms)
        if not q:
            continue
        try:
            hits = store.search(q, since=s, limit=30)
        except Exception:  # noqa: BLE001 — FTS не понял запрос
            continue
        groups: dict[int, dict] = {}
        for m, title in hits:
            c = store.get_chat(m.chat_id)
            if c is not None and c.kind == "private" and not include_private:
                continue
            g = groups.setdefault(m.chat_id, {"chat": title or str(m.chat_id), "chat_id": m.chat_id, "msgs": []})
            if len(g["msgs"]) < 5:
                g["msgs"].append(m.msg_id)
        res = []
        for cid, g in list(groups.items())[:4]:
            dc = by_chat.get(cid)
            g["summary"] = (dc or {}).get("summary") or ""
            g["in_digest"] = dc is not None
            res.append(g)
        if res:
            out[str(d.get("id") or x["ref"])] = res
    return out


def wiki_terms(store: Store, texts: list[str], limit: int = 6) -> list[str]:
    """Термины для вики: кандидаты, которых нет в глоссарии (или есть без определения), — самые частые."""
    from .glossary import Glossary
    from .glossary_auto import candidates
    g = Glossary(store)
    cnt: dict[str, int] = {}
    keep: dict[str, str] = {}
    for t in texts:
        for term, _ctx in candidates(t):
            e = g.get(term)
            if e and e.get("summary"):
                continue
            k = term.lower()
            cnt[k] = cnt.get(k, 0) + 1
            keep.setdefault(k, term)
    return [keep[k] for k, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:limit]]


class CrossBuilder:
    """data (готовая сводка Telegram) → data["cross"]: общая картина по всем включённым источникам."""

    def __init__(self, cfg, store: Store, llm, progress: Progress = print, sources: dict | None = None):
        from . import privacy
        self.cfg, self.store, self.llm, self.progress = cfg, store, llm, progress
        want = dict((cfg.digest or {}).get("sources") or {})
        want.update({k: bool(v) for k, v in (sources or {}).items() if k in plugins.KINDS})
        self.want = {k for k in plugins.KINDS if want.get(k) and plugins.enabled(cfg, k)}
        privacy.attach(llm, cfg, store)
        plugins.ensure(store)

    def build(self, data: dict, since: datetime, until: datetime) -> dict:
        res: dict = {"sources": sorted(self.want), "offline": {}, "errors": [], "mail": [], "itsm": [], "wiki": [],
                     "rooms": [], "tracker": [], "meet": [], "threads": [], "only_tg": [], "only_mail": [],
                     "only_other": [], "actions": [], "headline": "", "tg_points": 0}
        if not self.want:
            return res
        cfg, st = self.cfg, self.store
        rx = plugins.number_patterns(cfg, st)
        pts, back = points(data, cfg, rx)
        res["tg_points"] = len(pts)
        self.progress(f"Общая картина: пунктов Telegram для сверки — {len(pts)} (без текста сообщений и личных переписок); "
                      f"источники: {', '.join(plugins.TITLES[k] for k in sorted(self.want))}")
        for k in sorted(self.want):  # проверка подключения плагинов — до запросов
            stt = plugins.check(cfg, st, k, progress=self.progress)
            if stt.get("state") != "ok":
                res["offline"][k] = {"state": stt.get("state"), "detail": stt.get("detail") or "", "links": stt.get("links") or []}
        live = [k for k in sorted(self.want) if k not in res["offline"]]
        from .llm import LLMError

        def guarded(kind, fn):
            try:
                return fn()
            except plugins.PluginOffline as e:
                res["offline"][kind] = {"state": e.status.get("state", "offline"), "detail": e.status.get("detail", ""),
                                        "links": e.status.get("links") or []}
            except (LLMError, OSError, ValueError) as e:
                res["errors"].append(f"{plugins.TITLES[kind]}: {str(e)[:300]}")
                log.warning("источник %s: %s", kind, logs.err_info(e))
            return None

        matches = []
        if "mail" in live:
            md = guarded("mail", lambda: plugins.mail_digest(cfg, st, since, until, pts, progress=self.progress))
            if md:
                res["mail"] = md["mail"]
                matches = md.get("matches") or []
        rmatches = []
        if "rooms" in live:
            rd = guarded("rooms", lambda: plugins.rooms_digest(cfg, st, since, until, pts, progress=self.progress))
            if rd:
                res["rooms"] = rd["messages"]
                rmatches = rd.get("matches") or []
        if "meet" in live:
            res["meet"] = guarded("meet", lambda: plugins.meet_digest(cfg, st, since, until, progress=self.progress)) or []
        numbers = set()
        for p in pts:
            numbers.update(p.get("numbers") or [])
        for g in data.get("refs") or []:
            if g.get("type") in ("incident", "problem", "change"):
                numbers.add(g["value"])
        for m in res["mail"] + res["rooms"] + res["meet"]:
            numbers.update(str(n).upper() for n in (m["data"].get("numbers") or []))
        texts_all = " ".join([f"{p.get('title', '')} {p.get('details', '')}" for p in pts] +
                             [plugins.item_line(x) for x in res["mail"] + res["rooms"] + res["meet"]] + sorted(numbers))
        if "tracker" in live:
            keys = plugins.task_keys(cfg, texts_all, st)
            numbers -= set(keys)
            items = guarded("tracker", lambda: plugins.tracker_lookup(cfg, st, keys, progress=self.progress)) if keys else []
            mine = guarded("tracker", lambda: plugins.tracker_mine(cfg, st, since, until, progress=self.progress)) or []
            res["tracker"] = [x for x in {x["ref"]: x for x in (items or []) + mine}.values() if x.get("found")]
        if "itsm" in live and numbers:
            items = guarded("itsm", lambda: plugins.itsm_lookup(cfg, st, sorted(numbers), progress=self.progress))
            res["itsm"] = [x for x in items or [] if x.get("found")]
            res["itsm_missing"] = [x["ref"] for x in items or [] if not x.get("found")]
        if "wiki" in live:
            texts = [f"{p.get('title', '')} {p.get('details', '')}" for p in pts] + \
                    [f"{m['data'].get('subject', '')} {m['data'].get('summary', '')}" for m in res["mail"]]
            terms = wiki_terms(st, texts)
            if terms:
                res["wiki"] = guarded("wiki", lambda: plugins.wiki_lookup(cfg, st, terms, progress=self.progress)) or []
        mentions = tg_mentions(st, res["mail"], since, until, data, cfg) if res["mail"] else {}
        res["mail_in_tg"] = mentions
        res["rooms_matches"] = rmatches
        self._glossary(res)
        if not any(res[k] for k in ("mail", "itsm", "wiki", "rooms", "tracker", "meet")):
            res["headline"] = ""
            if res["offline"]:
                self.progress("Общая картина: подключённых источников нет — только Telegram.")
            return res
        self._merge(res, pts, matches, mentions, data)
        self._resolve(res, back)
        return res

    def _glossary(self, res: dict) -> None:
        from .glossary_auto import note_many
        items = [(plugins.item_line(x), x["source"], None)
                 for x in res["mail"] + res["itsm"] + res["wiki"] + res["rooms"] + res["tracker"] + res["meet"]]
        n = note_many(self.cfg, self.store, items)
        if n:
            self.progress(f"Глоссарий: новых кандидатов из почты/ITSM/вики — {n}")

    def _merge(self, res: dict, pts: list[dict], matches: list[dict], mentions: dict, data: dict) -> None:
        from .llm import LLMError
        from .llm.base import extract_json
        mail_lines = []
        for m in res["mail"]:
            line = plugins.item_line(m)
            tg = mentions.get(str(m["data"].get("id") or m["ref"]))
            if tg:
                line += " | в Telegram: " + "; ".join(f"чат «{g['chat']}»" + (f" — {g['summary'][:200]}" if g.get("summary") else "")
                                                    for g in tg)
            mail_lines.append(line)
        parts = [f"Период: {data['meta'].get('period_text', '')}",
                 "Пункты сводки Telegram (JSON):\n" + json.dumps(pts, ensure_ascii=False)[:14000]]
        if matches:
            parts.append("Что плагин почты нашёл по пунктам Telegram:\n" + json.dumps(matches, ensure_ascii=False)[:5000])
        if mail_lines:
            parts.append("Письма:\n" + "\n".join(mail_lines)[:12000])
        if res["itsm"]:
            parts.append("Карточки ITSM:\n" + "\n".join(plugins.item_line(x) for x in res["itsm"])[:10000])
        if res.get("itsm_missing"):
            parts.append("В ITSM не найдены: " + ", ".join(res["itsm_missing"][:30]))
        if res["wiki"]:
            parts.append("Вики:\n" + "\n".join(plugins.item_line(x) for x in res["wiki"])[:5000])
        if res.get("rooms"):
            parts.append("Сообщения X5 Rooms:\n" + "\n".join(plugins.item_line(x) for x in res["rooms"])[:9000])
        if res.get("rooms_matches"):
            parts.append("Что плагин Rooms нашёл по пунктам Telegram:\n" + json.dumps(res["rooms_matches"], ensure_ascii=False)[:4000])
        if res.get("tracker"):
            parts.append("Задачи трекера:\n" + "\n".join(plugins.item_line(x) for x in res["tracker"])[:8000])
        if res.get("meet"):
            parts.append("Встречи (Scriber AI):\n" + "\n".join(plugins.item_line(x) for x in res["meet"])[:9000])
        if res["offline"]:
            parts.append("Недоступны (не подключены): " + ", ".join(plugins.TITLES[k] for k in res["offline"]))
        me = json.loads(self.store.kv_get("me") or "{}").get("name", "")
        system = prompts.system_prompt(self.cfg.profile, me)
        user = "\n\n".join(parts) + "\n\n" + TASK
        try:  # правила и поправки пользователя для сводок (навык) — и для общей картины
            from . import skill
            rb = skill.rules_block(self.store, user[:20000], "digest")
            if rb:
                user = rb + "\n\n" + user
        except Exception as e:  # noqa: BLE001
            log.info("правила навыка не подставлены: %s", logs.err_info(e))
        self.progress("Общая картина: свожу Telegram и другие источники…")
        try:
            raw = self.llm.complete(system, user, max_tokens=int((self.cfg.digest or {}).get("max_output_tokens") or 4096))
            out = extract_json(raw)
        except (LLMError, ValueError, OSError) as e:
            res["errors"].append(f"общая картина: {str(e)[:300]}")
            return
        if not isinstance(out, dict):
            res["errors"].append("общая картина: модель вернула не JSON")
            return
        res["headline"] = str(out.get("headline") or "").strip()
        for t in out.get("threads") or []:
            if isinstance(t, dict) and t.get("title"):
                try:
                    imp = max(1, min(5, int(str(t.get("importance") or 3)[:1])))
                except ValueError:
                    imp = 3
                res["threads"].append({
                    "title": str(t["title"]), "importance": imp, "status": str(t.get("status") or ""),
                    "sources": [s for s in (t.get("sources") or []) if s in ("telegram",) + plugins.KINDS],
                    "tg": [str(x) for x in t.get("tg") or []], "mail": [str(x) for x in t.get("mail") or []],
                    "rooms": [str(x) for x in t.get("rooms") or []], "tracker": [str(x).upper() for x in t.get("tracker") or []],
                    "meet": [str(x) for x in t.get("meet") or []],
                    "itsm": [str(x).upper() for x in t.get("itsm") or []], "summary": str(t.get("summary") or ""),
                    "participants": [str(x) for x in t.get("participants") or []][:8],
                    "conflicts": str(t.get("conflicts") or ""), "next": str(t.get("next") or ""),
                    "to_me": bool(t.get("to_me"))})
        res["threads"].sort(key=lambda t: -t["importance"])
        res["only_tg"] = [x for x in out.get("only_telegram") or [] if isinstance(x, dict) and x.get("tg")][:20]
        res["only_mail"] = [x for x in out.get("only_mail") or [] if isinstance(x, dict) and x.get("mail")][:20]
        res["only_other"] = [x for x in out.get("only_other") or [] if isinstance(x, dict) and x.get("ref")
                             and x.get("source") in ("rooms", "tracker", "meet")][:20]
        res["actions"] = [x for x in out.get("actions") or [] if isinstance(x, dict) and x.get("title")][:15]

    @staticmethod
    def _resolve(res: dict, back: dict) -> None:
        """Номера пунктов (t1, m1, INC…) → ссылки и заголовки для отчёта."""
        mail_by = {str(m["data"].get("id") or m["ref"]): m for m in res["mail"]}
        itsm_by = {x["ref"]: x for x in res["itsm"]}
        for t in res["threads"]:
            t["tg_refs"] = [dict(back[x], id=x) for x in t["tg"] if x in back]
            t["mail_refs"] = [{"id": x, "subject": mail_by[x]["data"].get("subject") or "", "date": mail_by[x]["data"].get("date") or "",
                               "from": mail_by[x]["data"].get("from") or ""} for x in t["mail"] if x in mail_by]
            t["itsm_refs"] = [{"number": x, "title": itsm_by[x]["title"], "status": itsm_by[x]["status"]}
                              for x in t["itsm"] if x in itsm_by]
        other = {"rooms": {str(m["data"].get("id") or m["ref"]): m for m in res.get("rooms") or []},
                 "tracker": {x["ref"]: x for x in res.get("tracker") or []},
                 "meet": {str(m["data"].get("id") or m["ref"]): m for m in res.get("meet") or []}}

        def oref(kind: str, key: str) -> dict | None:
            x = other[kind].get(str(key)) or other[kind].get(str(key).upper())
            if not x:
                return None
            d = x["data"]
            return {"id": key, "title": d.get("title") or d.get("chat") or d.get("summary", "")[:80], "date": d.get("date") or
                    d.get("updated") or "", "status": d.get("status") or "", "url": d.get("url") or "",
                    "from": d.get("from") or ""}
        for t in res["threads"]:
            for kind in ("rooms", "tracker", "meet"):
                t[kind + "_refs"] = [r for r in (oref(kind, k) for k in t.get(kind) or []) if r]
        for x in res.get("only_other") or []:
            x["info"] = oref(x["source"], x["ref"])
        for x in res["only_tg"]:
            x["ref"] = back.get(str(x["tg"]))
        for x in res["only_mail"]:
            m = mail_by.get(str(x["mail"]))
            x["ref"] = {"subject": m["data"].get("subject"), "date": m["data"].get("date"), "from": m["data"].get("from")} if m else None


def apply(cfg, store: Store, llm, data: dict, since: datetime, until: datetime, *, sources: dict | None = None,
          progress: Progress = print) -> None:
    """Добавить общую картину к готовой сводке (если включён хоть один источник). Ошибки — в meta.errors."""
    try:
        cb = CrossBuilder(cfg, store, llm, progress, sources)
        if not cb.want:
            return
        data["cross"] = cb.build(data, since, until)
        for k, v in (data["cross"].get("offline") or {}).items():
            data["meta"].setdefault("errors", []).append(
                f"{plugins.TITLES[k]}: плагин Maverick не подключён — переподключите его в чате Maverick"
                + (f" ({v['detail'][:160]})" if v.get("detail") else ""))
        data["meta"].setdefault("errors", []).extend(data["cross"].get("errors") or [])
    except Exception as e:  # noqa: BLE001 — сверка не должна ломать готовую сводку Telegram
        log.warning("общая картина не собрана: %s", logs.err_info(e))
        data["meta"].setdefault("errors", []).append(f"общая картина не собрана: {str(e)[:200]}")

