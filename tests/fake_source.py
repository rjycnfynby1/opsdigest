"""Поддельные ITSM-очередь, xTracker и почта для сводки «От источника» (подключается в fake_llm)."""
import json
import re

QUEUE = {
    "INC0048190": {"type": "incident", "title": "Двойное списание при автоплатеже", "status": "Ожидание", "priority": "3",
                   "group": "L2-Биллинг", "assignee": "Ольга Петрова", "opened": "28.10.2026", "updated": "03.11.2026",
                   "waiting": True, "waiting_reason": "ожидание релиза", "waiting_for": "разработка",
                   "waiting_task": "BILL-482", "description": "Клиенту дважды списали оплату по автоплатежу",
                   "tasks": ["BILL-482"],
                   "comments": [{"date": "03.11.2026", "author": "Ольга Петрова", "text": "Ждём фикс по BILL-482"}]},
    "INC0048201": {"type": "incident", "title": "Двойное списание, второй клиент", "status": "Ожидание", "priority": "3",
                   "group": "L2-Биллинг", "opened": "30.10.2026", "updated": "31.10.2026", "waiting": True,
                   "waiting_reason": "ожидание информации", "waiting_for": "поддержка",
                   "description": "Разработка просит выписку платежей",
                   "comments": [{"date": "31.10.2026", "author": "Ч7", "text": "По BILL-482 нужна выписка; см. также OPS-12"}]},
    "INC0048207": {"type": "incident", "title": "Не проходит возврат", "status": "Ожидание", "priority": "4",
                   "group": "L2-Биллинг", "opened": "01.11.2026", "updated": "02.11.2026", "waiting": True,
                   "waiting_reason": "ожидание релиза", "waiting_for": "разработка", "waiting_task": "BILL-470",
                   "description": "Возврат зависает в статусе «в обработке»", "tasks": ["BILL-470"],
                   "comments": [{"date": "02.11.2026", "author": "Ч3", "text": "Исправят в BILL-470"}]},
    "INC0048300": {"type": "incident", "title": "Ошибка в отчёте по платежам", "status": "Новый", "priority": "4",
                   "group": "L2-Биллинг", "opened": "08.11.2026", "updated": "08.11.2026", "waiting": False,
                   "description": "Отчёт не строится за октябрь",
                   "comments": [{"date": "08.11.2026", "author": "Ч2", "text": "Взял в работу"}]},
}
TASKS = {"BILL-482": {"title": "Двойные списания", "status": "В работе", "closed": False, "release": "5.3",
                      "release_date": "20.11.2026", "waiting_for": "разработка", "waiting_what": "фикс и выкладка",
                      "assignee": "Алексей Смирнов", "updated": "05.11.2026",
                      "comments": [{"date": "05.11.2026", "author": "Ч1", "text": "Нужны примеры обращений"}],
                      "incidents": ["INC0048190", "INC0048201"]},
         "BILL-470": {"title": "Возвраты зависают", "status": "Закрыта", "closed": True, "release": "5.2",
                      "release_date": "01.11.2026", "updated": "01.11.2026", "incidents": ["INC0048207"]},
         "OPS-77": {"title": "Регламент дежурств", "status": "В работе", "closed": False, "incidents": ["INC0048207"]}}
CALLS: list = []


def plugin(kind: str, user: str):
    if "Это разбор очереди" in user and kind == "itsm":
        a, b = map(int, re.search(r"записи с (\d+) по (\d+)", user).groups())
        CALLS.append(("list", a, b))
        keys = list(QUEUE)
        part = keys[a - 1:b]
        items = []
        for n in part:  # в списке журнал есть только у одной записи — остальные запросятся подробно
            d = {k: v for k, v in QUEUE[n].items() if k != "comments" or n == "INC0048300"}
            items.append(dict(d, number=n, found=True))
        return json.dumps({"total": len(keys), "more": b < len(keys), "items": items}, ensure_ascii=False)
    if "Это разбор очереди" in user and kind == "tracker":
        CALLS.append(("tlist",))
        return json.dumps({"total": 2, "more": False, "items": [
            dict(TASKS["BILL-482"], key="BILL-482", found=True), dict(TASKS["BILL-470"], key="BILL-470", found=True)]},
            ensure_ascii=False)
    if "Подробные карточки ITSM для разбора очереди" in user:
        nums = re.findall(r"[A-Z]{3}\d{7}", user.split("разбора очереди:", 1)[1].split(".", 1)[0])
        CALLS.append(("itsm", nums))
        return json.dumps({"items": [dict(QUEUE[n], number=n, found=True) if n in QUEUE else {"number": n, "found": False}
                                     for n in nums]}, ensure_ascii=False)
    if "Подробно задачи xTracker для разбора очереди" in user:
        keys = re.findall(r"[A-Z]+-\d+", user.split("разбора очереди:", 1)[1].split(". По каждой", 1)[0])
        CALLS.append(("tracker", keys))
        return json.dumps({"items": [dict(TASKS[k], key=k, found=True) if k in TASKS else {"key": k, "found": False}
                                     for k in keys]}, ensure_ascii=False)
    if "где упоминаются эти номера" in user:
        CALLS.append(("mention", kind))
        if kind == "mail":
            return json.dumps({"mail": [{"id": "m1", "subject": "INC0048190: клиент снова пишет", "date": "07.11.2026",
                                         "from": "Ирина Ковалёва", "summary": "Клиент спрашивает про возврат двойного списания",
                                         "numbers": ["INC0048190"], "needs_reply": True}]}, ensure_ascii=False)
        return json.dumps({"messages": [], "meetings": []})
    return None


def model(system: str, user: str):
    if "Ниже — выдержки из рабочих чатов Telegram, где упоминаются номера" in user:
        out = []
        for ref in dict.fromkeys(re.findall(r"=== номер (\S+) ·", user)):
            block = user.split(f"=== номер {ref} ·", 1)[1]
            ids = re.findall(r"^#(\d+) ", block, re.M)
            CALLS.append(("tg", ref))
            out.append({"ref": ref, "summary": f"В чате просили пнуть разработку по {ref}", "latest": "клиент ждёт ответа",
                        "date": "08.11.2026", "waiting": "разработка", "msgs": [int(ids[0])] if ids else []})
        return json.dumps({"refs": out}, ensure_ascii=False)
    if "Ниже — записи из очереди" in user:
        refs = re.findall(r"^=== (\S+) ·", user, re.M)
        CALLS.append(("map", refs, "Указания пользователя" in user or "указания" in user.lower()))
        v = {"INC0048190": ("yes", "dev", "двойные списания при автоплатеже", "high"),
             "INC0048201": ("yes", "support", "двойные списания при автоплатеже", "high"),
             "INC0048207": ("no", "nobody", "возвраты", "medium"),
             "INC0048300": ("doubt", "support", "отчёт по платежам", "low")}
        items = []
        for r in refs:
            if r == "INC0048300" and "НЕ_ОТВЕЧАТЬ" in user:
                continue
            a, e, p, pr = v.get(r, ("doubt", "unclear", "", "medium"))
            items.append({"ref": r, "actual": a, "actual_why": "по журналу", "expects": e, "expects_what": "движения",
                          "problem": p, "summary": f"Суть {r}", "flags": ["клиент ждёт"] if r == "INC0048190" else [],
                          "next": f"проверить {r}", "priority": pr})
        return "```json\n" + json.dumps({"items": items}, ensure_ascii=False) + "\n```"
    if "разобранная очередь" in user:
        CALLS.append(("reduce", "answer" in user))
        res = {"headline": "В очереди 4 записи: две ждут BILL-482, по одной ждут от нас выписку.",
               "groups": [{"title": "Двойные списания", "refs": ["INC0048190", "INC0048201", "INC9999999"],
                           "tasks": ["BILL-482"], "why": "одна причина", "status": "ждут релиз 5.3", "next": "дать выписку"}],
               "actions": [{"title": "Отправить выписку по INC0048201", "why": "ждут от нас", "refs": ["INC0048201"]}],
               "close": [{"ref": "INC0048207", "why": "BILL-470 закрыта"}], "clarify": ["Сроки релиза 5.3 у Ч1"]}
        if '"answer"' in user:
            res["answer"] = "Релиза ждут 2 записи."
        return json.dumps(res, ensure_ascii=False)
    return None
