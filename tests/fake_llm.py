"""Поддельная LLM для тестов: отвечает заранее написанными сводками по демо-чатам.

Запускается как OpenAI-совместимый сервер (/v1/models, /v1/chat/completions) — так проверяется весь путь
через HTTP, как с LM Studio. Ответы ниже — образец того, что ожидается от хорошей модели.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from opsdigest.media import digit_png

# Изображения, которые «видит» поддельная модель: sha256 байтов -> описание (заполняют тесты)
IMAGES: dict[str, dict] = {}

MAP = {
    "Авария: недоступен личный кабинет": {
        "summary": "P1-авария: клиенты не могли войти в личный кабинет (502 на вебе и в приложении). Причина — утечка "
                   "соединений к БД сессий после релиза auth-svc 4.12 (CHG0003318); через ~30 минут откатили до 4.11, "
                   "вход восстановлен. Разработка завела AUTH-1290, повторная выкатка 4.12 отложена.",
        "status": "resolved", "importance": 5,
        "items": [
            {"type": "reply", "priority": "high", "title": "Список крупных партнёров с обращениями по аварии",
             "details": "Ирина Ковалёва просила дважды — для отдельного уведомления партнёров; ответа в чате нет.",
             "competence": "Интеграции с партнёрами", "msgs": [1010, 1015],
             "draft": "Ирина, собираю список партнёров с обращениями за время аварии — пришлю сюда в течение часа. "
                      "Уведомление для них подготовлю вместе с итоговым текстом."},
            {"type": "action", "priority": "high", "title": "Итоговый текст для клиентов о восстановлении",
             "details": "В начале аварии вы обещали шаблон ответа клиентам; менеджер смены напомнила про итоговый текст.",
             "competence": "Личный кабинет / авторизация", "msgs": [1007, 1015],
             "draft": "Уважаемые клиенты! Вчера вечером около получаса был недоступен вход в личный кабинет и "
                      "мобильное приложение. Причина устранена, сервис работает штатно. Приносим извинения за неудобства."},
            {"type": "action", "priority": "medium", "title": "Примеры обращений с временем ошибки для AUTH-1290",
             "details": "Дмитрий Орлов просит у L2 примеры, чтобы сопоставить с логами.", "competence": None,
             "msgs": [1016],
             "draft": "Дмитрий, примеры обращений с временем ошибки: (номер — время). Если нужно больше — выгружу по группе L2."},
            {"type": "decision", "priority": "low", "title": "Откат auth-svc до 4.11; 4.12 — только после AUTH-1290",
             "details": "", "competence": None, "msgs": [1011, 1016], "draft": None},
        ]},
    "Мониторинг PROD": {
        "summary": "Вечером — всплеск 502 на lk-web-01/03, задержки auth-svc и заполнение пула db-sessions до 98%; "
                   "через ~35 минут алерты закрылись (совпадает с откатом). Позже db-sessions снова растёт: 85% → 87%, "
                   "сообщения о восстановлении нет.",
        "status": "active", "importance": 4,
        "items": [
            {"type": "incident", "priority": "high", "title": "db-sessions снова заполняется: 85% → 87%, алерт не закрыт",
             "details": "Похоже на ту же утечку соединений, хотя auth-svc откатили. Стоит сообщить в аварийный чат.",
             "competence": "Личный кабинет / авторизация", "msgs": [1017, 1018],
             "draft": "Дмитрий, по мониторингу db-sessions снова растёт: 85% → 87% соединений, алерт не закрыт. "
                      "Это та же утечка или другой сервис? Посмотрите, пожалуйста."},
            {"type": "fyi", "priority": "low", "title": "Алерты 502 и задержек auth-svc закрылись после отката",
             "details": "", "competence": None, "msgs": [1012, 1013, 1014], "draft": None},
        ]},
    "Алексей Смирнов": {
        "summary": "Алексей чинит BILL-482 (двойные списания при автоплатеже) и просит 2–3 свежих обращения с данными "
                   "платежей, чтобы воспроизвести; фикс планирует завтра утром. Ответа пока нет.",
        "status": "active", "importance": 5,
        "items": [
            {"type": "reply", "priority": "high", "title": "Прислать обращения по двойным списаниям (BILL-482)",
             "details": "Нужны сегодня. В передаче смены есть подходящие: INC0048190, INC0048201, INC0048207.",
             "competence": "Биллинг и платежи", "msgs": [1019, 1020],
             "draft": "Алексей, привет! По BILL-482 свежие обращения: INC0048190, INC0048201, INC0048207 — во всех "
                      "двойное списание при автоплатеже. Данные платежей выгружу и пришлю в течение часа. По INC0048190 "
                      "клиент ждёт с пятницы — если получится, начни с него."},
        ]},
    "L2 поддержка": {
        "summary": "Ольга передала смену: на L2-Биллинг 14 открытых обращений, три — про двойные списания. По "
                   "INC0048190 клиент ждёт с пятницы, разработка молчит — просят вас продвинуть BILL-482. Не "
                   "подтверждено субботнее дежурство (по графику — вы). После аварии ЛК — 60+ повторных обращений.",
        "status": "active", "importance": 4,
        "items": [
            {"type": "reply", "priority": "high", "title": "Подтвердить дежурство в субботу", "details": "",
             "competence": None, "msgs": [1023, 1024], "draft": "Да, в субботу дежурю я, внесу себя в график."},
            {"type": "follow_up", "priority": "high", "title": "INC0048190: клиент ждёт с пятницы, BILL-482 без движения",
             "details": "Ольга просит продвинуть задачу у разработки.", "competence": "Биллинг и платежи",
             "msgs": [1022],
             "draft": "Ольга, написал Алексею по BILL-482 и передал три обращения, фикс он планирует завтра утром. "
                      "Клиенту по INC0048190 сегодня отвечу промежуточным статусом."},
            {"type": "action", "priority": "medium", "title": "Шаблон для закрытия 60+ повторных обращений после аварии",
             "details": "Сергей Волков спрашивает текст для массового закрытия.", "competence": None, "msgs": [1025],
             "draft": "Шаблон: «Сбой входа в личный кабинет устранён, сервис работает штатно. Если проблема повторится — "
                      "ответьте на это сообщение». Закрываем с привязкой к INC0048213."},
        ]},
    "Релизы платформы": {
        "summary": "Сегодня в 22:00 — релиз payment-gateway 2.8 (CHG0003321): новые лимиты автоплатежей и изменения "
                   "API уведомлений партнёрам, возможны кратковременные 503. Повторная выкатка auth-svc 4.12 — после "
                   "исправления AUTH-1290.",
        "status": "info", "importance": 3,
        "items": [
            {"type": "fyi", "priority": "medium", "title": "Вечером релиз payment-gateway 2.8: автоплатежи и уведомления партнёрам",
             "details": "Пересекается с BILL-482: возможен рост обращений по автоплатежам и вопросы партнёров.",
             "competence": "Биллинг и платежи", "msgs": [1026], "draft": None},
        ]},
    "Отдел: болталка": {"summary": "День рождения у Андрея и обед — рабочего ничего.", "status": "info",
                        "importance": 1, "items": []},
}


def _reduce(keys: dict[str, str]) -> dict:
    k = lambda prefix: next((v for t, v in keys.items() if t.startswith(prefix)), "c0")  # noqa: E731
    inc, mon, dev, team = k("Авария"), k("Мониторинг"), k("Алексей"), k("L2")
    return {
        "headline": "Вечером была P1-авария входа в личный кабинет (INC0048213) — устранена откатом auth-svc, но "
                    "db-sessions снова заполняется. От вас ждут: обращения для BILL-482 Алексею, список партнёров и "
                    "итоговый текст для клиентов по аварии, подтверждение субботнего дежурства.",
        "top": [
            {"chat": dev, "title": "Отправить Алексею обращения по двойным списаниям (BILL-482)",
             "why": "Фикс завтра утром, без примеров не воспроизвести; клиент по INC0048190 ждёт с пятницы.",
             "priority": "high", "msgs": [1019, 1020], "draft": MAP["Алексей Смирнов"]["items"][0]["draft"]},
            {"chat": inc, "title": "Список крупных партнёров с обращениями по аварии",
             "why": "Менеджер смены просила дважды, ответа нет.", "priority": "high", "msgs": [1010, 1015],
             "draft": MAP["Авария: недоступен личный кабинет"]["items"][0]["draft"]},
            {"chat": inc, "title": "Итоговый текст для клиентов о восстановлении",
             "why": "Вы обещали шаблон в начале аварии; есть напоминание.", "priority": "high", "msgs": [1007, 1015],
             "draft": MAP["Авария: недоступен личный кабинет"]["items"][1]["draft"]},
            {"chat": mon, "title": "Сообщить о повторном росте соединений db-sessions",
             "why": "85% → 87%, алерт не закрыт — возможен повтор аварии.", "priority": "high", "msgs": [1017, 1018],
             "draft": MAP["Мониторинг PROD"]["items"][0]["draft"]},
            {"chat": team, "title": "Подтвердить дежурство в субботу", "why": "В графике пусто, коллеги спрашивают.",
             "priority": "medium", "msgs": [1023, 1024], "draft": "Да, в субботу дежурю я, внесу себя в график."},
            {"chat": team, "title": "Шаблон для закрытия повторных обращений после аварии",
             "why": "60+ обращений на группе ждут закрытия.", "priority": "medium", "msgs": [1025],
             "draft": MAP["L2 поддержка"]["items"][2]["draft"]},
            {"chat": inc, "title": "Примеры обращений с временем ошибки для AUTH-1290",
             "why": "Нужны разработке, чтобы найти утечку в логах.", "priority": "medium", "msgs": [1016], "draft": None},
        ],
        "incidents": [
            {"title": "Недоступность входа в личный кабинет (P1)", "status": "локализована", "chats": [inc, mon, team],
             "details": "Утечка соединений к db-sessions после auth-svc 4.12; откат до 4.11, исправление — AUTH-1290. "
                        "60+ повторных обращений; db-sessions снова растёт (87%).",
             "refs": ["INC0048213", "CHG0003318", "AUTH-1290"]},
            {"title": "Двойные списания при автоплатеже", "status": "идёт", "chats": [dev, team],
             "details": "Три обращения на L2-Биллинг, фикс BILL-482 — завтра утром; вечером релиз payment-gateway 2.8 "
                        "меняет лимиты автоплатежей.", "refs": ["BILL-482", "INC0048190", "INC0048201", "INC0048207"]},
        ],
        "competences": [
            {"name": "Биллинг и платежи", "points": ["BILL-482: разработке нужны примеры, фикс завтра утром",
                                                     "Сегодня 22:00 релиз payment-gateway 2.8 — новые лимиты автоплатежей"]},
            {"name": "Личный кабинет / авторизация", "points": ["Авария INC0048213 устранена откатом; 4.12 — после AUTH-1290",
                                                                "60+ повторных обращений ждут закрытия шаблоном"]},
            {"name": "Интеграции с партнёрами", "points": ["Нужен список партнёров для уведомления по аварии",
                                                           "payment-gateway 2.8 меняет API уведомлений партнёрам"]},
        ],
        "clarify": ["У Дмитрия Орлова: новый рост db-sessions — это auth-svc или другой сервис?",
                    "У релиз-менеджера: не отложить ли payment-gateway 2.8 до исправления BILL-482?"],
        "fyi": ["Повторная выкатка auth-svc 4.12 заблокирована до AUTH-1290."],
    }


def see_digit(img: bytes) -> str | None:
    from opsdigest.media import digit_png
    return next((d for d in "0123456789" if digit_png(d) == img), None)


def respond_image(text: str, images: list[bytes]) -> str:
    """Ответ «модели, которая видит картинки»: цифру с проверочной картинки, описание — из IMAGES."""
    if "Какая цифра" in text:
        d = see_digit(images[0]) if images else None
        return d or "не вижу изображения"
    if "Опиши изображение" in text:
        if not images:
            return "Изображение не приложено."
        known = IMAGES.get(hashlib.sha256(images[0]).hexdigest())
        return json.dumps(known or {"kind": "фото", "summary": "Картинка без подробностей", "details": "Нет деталей.",
                                    "text": "", "importance": 2}, ensure_ascii=False)
    return "?"


def _b64(u: str) -> bytes:
    return base64.b64decode(u.split(",", 1)[1] if u.startswith("data:") else u)


# плагины Maverick, которые «подключены» в поддельном Maverick (тесты меняют)
PLUGINS = {"mail": True, "itsm": True, "wiki": True, "rooms": True, "tracker": True, "meet": True}
PLUGIN_CALLS: list = []
ITSM_DB = {"CHG0003318": {"type": "change", "title": "Релиз auth-svc 4.12", "status": "Откачено", "group": "Команда ЛК",
                          "assignee": "Дмитрий Орлов", "description": "Выкатка 4.12, откат до 4.11 после аварии"},
           "INC0012345": {"type": "incident", "title": "Не входит в ЛК", "status": "В работе", "group": "L2",
                          "description": "502 при входе", "related": ["PRB0001234"]}}


def plugin_reply(user: str) -> str | None:
    if "Это запрос программы OpsDigest" not in user:
        return None
    kind = ("mail" if "плагин почты" in user else "itsm" if "плагин ITSM" in user else "wiki" if "плагин Confluence" in user
            else "rooms" if "плагин мессенджера X5 Rooms" in user else "tracker" if "плагин трекера задач" in user
            else "meet" if "плагин Scriber AI" in user else "any")
    PLUGIN_CALLS.append((kind, user))
    if "Служебная проверка подключения" in user:
        if PLUGINS.get(kind, True):
            return json.dumps({"connected": True, "sample": "ok"})
        return json.dumps({"connected": False, "reason": "Требуется повторный вход: https://maverick.local/connect/" + kind},
                          ensure_ascii=False)
    if not PLUGINS.get(kind, True):
        return "Плагин не подключён. Требуется авторизация."
    import fake_source
    fs = fake_source.plugin(kind, user)
    if fs is not None:
        return fs
    if "карточки этих сотрудников" in user:
        names = user.split("что о них известно: ", 1)[1].split(".\n", 1)[0].split("; ")
        out = []
        for n in names:
            nm = n.split(" (Telegram", 1)[0].strip()
            out.append({"name": nm, "found": "Неизвестный" not in nm, "post": "Ведущий инженер", "dept": "Платформа L3",
                        "competences": ["Kafka", "биллинг"], "manager": "Ч99", "url": "https://wiki.local/people/x",
                        "contacts": {"email": ["x@corp.local"], "phone": ["+7 900 000-00-00"], "other": []}})
        return json.dumps({"people": out}, ensure_ascii=False)
    if "упоминания этих чатов Telegram" in user:
        titles = re.findall(r"«([^»]+)»", user.split("дежурства, списки чатов): ", 1)[1].split("\nСтрого", 1)[0])
        return json.dumps({"chats": [{"chat": t, "found": True, "purpose": f"Чат «{t}» — дежурная смена L2, эскалации",
                                      "owner": "руководитель смены", "url": "https://wiki.local/chats"} for t in titles]},
                          ensure_ascii=False)
    if "употребления этих терминов" in user:
        terms = user.split("сокращений: ", 1)[1].split(". Для каждого", 1)[0].split("; ")
        return json.dumps({"items": [{"term": t, "found": True, "meaning": f"{t} — как в письмах",
                                      "examples": [f"по {t} письмо"]} for t in terms]}, ensure_ascii=False)
    if "в X5 Rooms за период" in user:
        tg = re.findall(r'"id": "(t\d+)"', user)
        return json.dumps({"messages": [{"id": "r1", "chat": "Дежурка L2", "date": "02.11.2026 10:00", "from": "Ч5",
                                         "summary": "Просят подтвердить откат по CHG0003318", "numbers": ["CHG0003318", "BILL-482"],
                                         "mention": True, "needs_reply": True, "importance": 4}],
                           "matches": [{"tg": tg[0] if tg else "t1", "found": True, "rooms": ["r1"], "detail": "то же"}]},
                          ensure_ascii=False)
    if "Найди в X5 Rooms сообщения по теме" in user:
        return json.dumps({"messages": [{"id": "r7", "chat": "Биллинг", "date": "03.11.2026", "from": "Ч3",
                                         "summary": "Обсуждали BILL-482", "numbers": ["BILL-482"]}]}, ensure_ascii=False)
    if "Найди в xTracker эти задачи" in user:
        keys = re.findall(r"[A-Z][A-Z0-9]+-\d+", user.split("основные поля: ", 1)[1].split(".\n", 1)[0])
        return json.dumps({"items": [{"key": k, "found": k == "BILL-482", "title": "Двойные списания", "status": "В работе",
                                      "assignee": "Алексей Смирнов", "description": "фикс завтра"} for k in keys]},
                          ensure_ascii=False)
    if "Найди в xTracker задачи пользователя" in user or "Найди в xTracker задачи и баги" in user:
        return json.dumps({"items": [{"key": "OPS-77", "found": True, "title": "Регламент дежурств", "status": "Ревью",
                                      "assignee": "Я", "updated": "02.11.2026"}]}, ensure_ascii=False)
    if "встречи пользователя за период" in user:
        return json.dumps({"meetings": [{"id": "s1", "title": "Разбор аварии ЛК", "date": "02.11.2026 15:00",
                                         "participants": ["Ч1", "Ч2"], "summary": "Решили откатить auth-svc",
                                         "decisions": ["откат до 4.11"], "actions": [{"who": "пользователь",
                                         "what": "подготовить итоговый текст", "due": "03.11"}],
                                         "numbers": ["CHG0003318"], "to_me": "подготовить итоговый текст"}]},
                          ensure_ascii=False)
    if "Найди в расшифровках встреч" in user:
        return json.dumps({"meetings": [{"id": "s4", "title": "Планёрка", "date": "01.11.2026", "summary": "про BILL-482"}]},
                          ensure_ascii=False)
    if "Найди в ITSM карточки этих записей" in user:
        nums = re.findall(r"[A-Z]{2,4}\d{5,}", user.split("основные поля:", 1)[1].split("\n", 1)[0])
        return json.dumps({"items": [dict(ITSM_DB[n], number=n, found=True) if n in ITSM_DB else {"number": n, "found": False}
                                     for n in nums]}, ensure_ascii=False)
    if "Найди в ITSM инциденты и проблемы, похожие" in user:
        return json.dumps({"items": [{"number": "INC0099999", "found": True, "title": "Двойные списания при автоплатеже",
                                      "status": "Решён", "resolution": "исправлено в BILL-470"}]}, ensure_ascii=False)
    if "Посмотри письма пользователя за период" in user:
        tg = re.findall(r'"id": "(t\d+)"', user)
        return json.dumps({"mail": [{"id": "m1", "subject": "Итоги аварии ЛК и CHG0003318", "date": "02.11.2026 09:10",
                                     "from": "Ирина Ковалёва", "summary": "Просит список партнёров и итоговый текст",
                                     "numbers": ["CHG0003318"], "needs_reply": True, "importance": 5},
                                    {"id": "m2", "subject": "Плановые работы на выходных", "date": "02.11.2026 12:00",
                                     "from": "Служба эксплуатации", "summary": "Работы на сети в субботу",
                                     "numbers": [], "needs_reply": False, "importance": 2}],
                           "matches": [{"tg": tg[0] if tg else "t1", "found": True, "mail": ["m1"],
                                        "detail": "письмо об итогах аварии"}]}, ensure_ascii=False)
    if "Найди письма пользователя по теме" in user:
        return json.dumps({"mail": [{"id": "m9", "subject": "BILL-482: примеры обращений", "date": "03.11.2026",
                                     "from": "Алексей Смирнов", "summary": "Просит примеры обращений с платежами",
                                     "numbers": [], "needs_reply": True}]}, ensure_ascii=False)
    if "Найди в Confluence статьи по этим терминам" in user:
        terms = user.split("самую подходящую статью): ", 1)[1].split("\n", 1)[0].split("; ")
        return json.dumps({"items": [{"term": t, "found": i == 0, "title": f"{t} — что это", "url": f"https://wiki.local/{t}",
                                      "summary": f"Статья про {t}"} for i, t in enumerate(terms)]}, ensure_ascii=False)
    if "Найди в Confluence статьи по теме" in user:
        return json.dumps({"items": [{"term": "автоплатёж", "found": True, "title": "Автоплатёж: как устроен",
                                      "url": "https://wiki.local/autopay", "summary": "Списания по расписанию",
                                      "images": [{"name": "схема.png", "about": "схема автоплатежа",
                                                  "url": "data:image/png;base64," + base64.b64encode(digit_png("5")).decode()}]}]},
                          ensure_ascii=False)
    return "Нашёл в почте и ITSM: по BILL-482 писем нет."


def skill_reply(system: str, user: str) -> str | None:
    both = system + "\n" + user
    if "Ты обучаешь программу OpsDigest" in both:
        fb = both.split("Отзыв пользователя:", 1)[1].split("\n", 1)[0] if "Отзыв пользователя:" in both else ""
        if "непонятно" in fb and "Пользователь ответил:" not in both:
            return json.dumps({"understood": False, "reply": "Не уверен, о чём речь.", "question": "Это про номер ITSM или про чат?",
                               "options": ["номер ITSM", "инцидент-чат"], "actions": []}, ensure_ascii=False)
        acts = [{"op": "rule.add", "text": "Номер вида INC1234567 — обращение в ITSM, а чат с «INC» в названии — "
                 "инцидент-чат; не путать.", "scope": ["digest", "assistant"], "keys": ["INC", "инцидент"]},
                {"op": "term.upsert", "term": "Инцидент-чат", "aliases": ["инц-чат"], "kind": "entity",
                 "summary": "Чат малой аварии", "senses": [{"meaning": "чат, созданный дежурным по росту ошибок",
                                                           "how_to_tell": "в названии INC и дата"}]},
                {"op": "chat.set", "where": {"title": ["авар"]}, "set": {"mode": "incident", "append": "Чат аварии."}}]
        return json.dumps({"understood": True, "reply": "Понял: номер ITSM и инцидент-чат — разные вещи, запомнил.",
                           "question": "", "options": [], "actions": acts}, ensure_ascii=False)
    if "выполняешь указания" in both and "Указание пользователя:" in both:
        cmd = both.split("Указание пользователя:", 1)[1]
        if "непонятно" in cmd:
            return 'Уточните.\n```ask {"question": "Какие чаты?", "options": ["все с «авар»", "только этот"]}```'
        acts = [{"op": "chat.set", "where": {"title": ["авар"]}, "set": {"priority": "high", "description": "Чат аварии"}},
                {"op": "link.add", "title": "Регламент инцидентов", "url": "https://wiki.local/incidents", "note": "с чего начинать"},
                {"op": "person.set", "where": {"name": ["Смирнов"]}, "set": {"post": "Разработчик биллинга"}}]
        return "Поставлю чатам аварий высокую важность и добавлю ссылку.\n```actions\n" + json.dumps(acts, ensure_ascii=False) + "\n```"
    return None


def respond(system: str, user: str) -> str:
    pr = plugin_reply(user)
    if pr is not None:
        return pr
    sr = skill_reply(system, user)
    if sr is not None:
        return sr
    import fake_source
    fs = fake_source.model(system, user)
    if fs is not None:
        return fs
    if "Кандидаты в термины" in user:
        names = re.findall(r"^### (.+?) \(встречался", user, re.M)
        out = []
        for i, t in enumerate(names):
            if i % 3 == 0:
                out.append({"term": t, "is_term": True, "clear": True, "kind": "service", "summary": f"{t}: сервис",
                            "senses": [{"meaning": f"система {t}"}], "examples": [f"упал {t}"],
                            "links": [{"title": t, "url": f"https://wiki.local/{t}"}]})
            elif i % 3 == 1:
                out.append({"term": t, "is_term": True, "clear": False, "summary": "",
                            "question": f"Что такое {t}?", "options": ["система", "команда"]})
            else:
                out.append({"term": t, "is_term": False})
        return json.dumps({"terms": out}, ensure_ascii=False)
    if "очищенные данные за период из нескольких источников" in user:
        tg = re.findall(r'"id": "(t\d+)"', user)
        return json.dumps({"headline": "Авария ЛК закрыта откатом; в почте ждут итоговый текст, CHG0003318 откачен.",
                           "threads": [{"title": "Авария ЛК и релиз auth-svc 4.12", "importance": 5, "status": "решена",
                                        "sources": ["telegram", "mail", "itsm"], "tg": tg[:2], "mail": ["m1"],
                                        "itsm": ["CHG0003318"], "summary": "Откат до 4.11; ждут итоговый текст клиентам",
                                        "participants": ["Ч1 — менеджер смены"], "conflicts": "",
                                        "next": "отправить итоговый текст", "to_me": True}],
                           "only_telegram": [{"tg": tg[-1] if tg else "t1", "why": "нет инцидента в ITSM"}],
                           "only_mail": [{"mail": "m2", "why": "плановые работы в субботу"}],
                           "actions": [{"title": "Ответить на письмо об итогах аварии", "why": "просят список партнёров",
                                        "sources": ["mail", "telegram"]}]}, ensure_ascii=False)
    if "Выпиши из переписки Telegram ниже всё, что относится к вопросу" in user:
        ids = re.findall(r"^#(\d+) ", user, re.M)
        return f"- Про BILL-482 говорили в чате (#{ids[0] if ids else 1})."
    if "Ты ведёшь глоссарий" in system:
        if "Предложи статью глоссария" in user:
            return json.dumps({"term": "ЛК", "kind": "app", "aliases": ["личный кабинет"], "summary": "Личный кабинет клиента",
                               "senses": [{"meaning": "веб и приложение клиента", "how_to_tell": "вход, 502"}],
                               "questions": []}, ensure_ascii=False)
        return json.dumps({"terms": [
            {"term": "auth-svc", "kind": "service", "aliases": ["auth"], "summary": "Сервис авторизации ЛК",
             "senses": [{"meaning": "бэкенд входа в личный кабинет", "how_to_tell": "релизы 4.11/4.12, вход"}],
             "known": "", "msgs": [1], "confidence": 0.9},
            {"term": "инц", "kind": "abbr", "summary": "сокращение", "senses": [{"meaning": "обращение в ITSM"}],
             "known": "Инцидент", "msgs": [2], "confidence": 0.8},
            {"term": "мусор", "confidence": 0.1}],
            "questions": [{"term": "auth-svc", "question": "Какая команда отвечает за auth-svc?",
                           "options": ["Команда ЛК", "Платформа"], "msgs": [1]}]}, ensure_ascii=False)
    if "Ты аккуратно редактируешь глоссарий" in system:
        art = json.loads(user.split("Статья глоссария (JSON): ", 1)[1].split("\n\nВопрос", 1)[0] or "{}")
        ans = user.split("Его ответ: ", 1)[1].split("\n", 1)[0]
        art["owner"] = ans
        return json.dumps(art, ensure_ascii=False)
    if "Выбери изображения" in user:  # отбор картинок — «все, что предложены»
        return json.dumps({"describe": sorted(set(re.findall(r"— (m\d+) ", user)))})
    if "Какая цифра" in user or "Опиши изображение" in user:  # картинку не приложили
        return respond_image(user, [])
    if "Сводки чатов:" in user:
        body = user.split("Сводки чатов:\n", 1)[1].split("\n\nНомера", 1)[0]
        keys = {c["title"]: c["chat"] for c in json.loads(body)}
        out = _reduce(keys)
        if GENERIC and "Прошлая сводка (" in user:   # есть прошлая сводка — что изменилось
            k0 = next(iter(keys.values()), "")
            out["changes"] = [{"kind": "updated", "title": "Статус задачи изменился", "details": "было: открыта → стало: в работе",
                               "chat": k0}]
        return json.dumps(out, ensure_ascii=False)
    if "Ответь словом" in user:
        return "готов"
    # ИИ-описания чатов, тем и людей
    if "Опиши чат." in user:
        if "Название похоже на чат аварии" in user:
            return json.dumps({"type": "авария", "summary": "Разбор недоступности ЛК.", "topics": [],
                               "incident": {"what": "недоступен личный кабинет", "status": "закрыта",
                                            "outcome": "откат auth-svc до 4.11", "started": "01.11.2026",
                                            "ended": "02.11.2026"}, "mode": "incident", "priority": "low"},
                              ensure_ascii=False)
        return json.dumps({"type": "команда", "summary": "Рабочий чат команды L2.", "topics": ["дежурства", "биллинг"],
                           "user_role": "эскалация по биллингу", "use": "следить за вопросами к вам",
                           "incident": None, "mode": "chat", "priority": "normal"}, ensure_ascii=False)
    if "Опиши тему." in user:
        return json.dumps({"summary": "Тема про релизы.", "use": "следить за датами", "priority": "high"},
                          ensure_ascii=False)
    if "Опиши этого человека" in user:
        return json.dumps({"summary": "Разработчик биллинга.", "knows": ["платежи", "BILL"], "asks": "примеры обращений",
                           "answers": "про списания", "with_user": None}, ensure_ascii=False)
    if '"role"' in user and "Его сообщения в этом чате" in user:
        return json.dumps({"role": "чинит баги биллинга в этом чате"}, ensure_ascii=False)
    # ИИ в чате мессенджера
    if "Сделай сводку переписки чата" in user:
        ids = re.findall(r"^#(\d+) ", user, re.M)
        return f"### Главное\nОбсуждали сбой BILL-482 (#{ids[0]}).\n### Нужна ваша реакция\n- Ответить про примеры (#{ids[-1]})."
    if "Напиши черновик сообщения" in user:
        return "Черновик: «Пришлю примеры обращений сегодня до 15:00.»"
    if "где ещё это обсуждалось" in user:
        ids = re.findall(r"^#(\d+) ", user, re.M)
        return f"Это же обсуждали в другом чате: #{ids[0]}."
    if "Вопрос пользователя:" in user:
        return "Речь о сбое BILL-482, подробности в #2."
    if "Вопрос:" in user:
        return "По найденным сообщениям: BILL-482 чинит Алексей Смирнов, фикс завтра утром (#1019, #1020)."
    chats = []
    for key, title in re.findall(r"=== ЧАТ (c\d+) \| «(.+?)»", user):
        for prefix, data in MAP.items():
            if title.startswith(prefix):
                chats.append({"chat": key, **data})
        if GENERIC and not any(c["chat"] == key for c in chats):   # стенд и тесты истории: любой чат
            block = user.split(f"=== ЧАТ {key} |", 1)[1].split("=== ЧАТ ", 1)[0]
            ids = [int(x) for x in re.findall(r"^#(\d+) ", block, re.M)]
            hist = "ПРОШЛЫЕ СВОДКИ ПО ЭТОМУ ЧАТУ" in block
            CALLS.append({"chat": title, "ids": ids, "history": hist})
            chats.append({"chat": key, "summary": f"Обсуждали {title}: сообщений {len(ids)}.", "status": "active",
                          "importance": 3, "items": [{"type": "action", "priority": "medium",
                                                      "title": f"Проверить {title}", "details": "", "msgs": ids[-2:],
                                                      "to_me": "no", "owner": "nobody",
                                                      **({"change": "updated"} if hist else {})}]})
    # как настоящая модель — иногда в ```json-блоке и с хвостом текста
    return "Вот сводка:\n```json\n" + json.dumps({"chats": chats}, ensure_ascii=False) + "\n```\nГотово."


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихо
        pass

    def _send(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _deny(self):
        b = json.dumps({"detail": "Invalid API key"}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_code(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/docs" and getattr(self.server, "mav_docs", False):
            b = b"<html><script>const ui = SwaggerUIBundle({url: '/internal/spec.json', dom_id: '#swagger-ui'})</script></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if self.path == "/internal/spec.json" and getattr(self.server, "mav_docs", False):
            return self._send({"openapi": "3.1.0", "paths": {"/api/v1/query": {"post": {"requestBody": {"content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Q"}}}}}},
                "/api/v1/files": {"post": {"requestBody": {"content": {"multipart/form-data": {"schema": {
                    "properties": {"file": {"type": "string", "format": "binary"}}, "required": ["file"]}}}}}}},
                "components": {"schemas": {"Q": {"properties": {"message": {"type": "string"}, "files": {
                    "anyOf": [{"type": "array", "items": {"$ref": "#/components/schemas/F"}}, {"type": "null"}]}}},
                    "F": {"properties": {"file_name": {"type": "string"}, "file_data": {"type": "string"},
                                         "mime": {"type": "string"}}, "required": ["file_name", "file_data", "mime"]}}}})
        if self.path.endswith("/openapi.json"):
            if not getattr(self.server, "mav_openapi", False) or self.path != "/openapi.json":
                return self._send_code(404, {"detail": "Not Found"})
            f = getattr(self.server, "mav_field", "message")
            return self._send({"openapi": "3.1.0", "paths": {"/api/v1/query": {"post": {"requestBody": {"content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/QueryRequest"}}}}}}},
                "components": {"schemas": {"QueryRequest": {"properties": {
                    "session_id": {"type": "string"}, f: {"type": "string"}, "files": {"type": "array"}}}}}})
        if "/api/v1/artifacts/" in self.path and self.path.endswith("/download"):
            if self.headers.get("X-API-Key") != "sk_test":
                return self._deny()
            b = digit_png("7")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if self.path.endswith("/api/v1/api-keys"):  # Maverick: проверка ключа
            if self.headers.get("X-API-Key") != "sk_test":
                return self._deny()
            return self._send({"items": []})
        self._send({"data": [{"id": "text-embedding-nomic"}, {"id": "fake-qwen3-instruct"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        if (getattr(self.server, "delay", 0) or DELAY) and self.path.endswith("/chat/completions"):  # медленная модель
            import time
            time.sleep(getattr(self.server, "delay", 0) or DELAY)
        accept = getattr(self.server, "mav_images", None)  # какой формат картинок «понимает» этот Maverick
        if self.path.endswith("/api/v1/attachments/upload") and (accept == "attach" or getattr(self.server, "mav_portal", False)):
            if self.headers.get("X-API-Key") != "sk_test":
                return self._deny()
            if b'name="file"' not in raw:
                return self._send_code(422, {"detail": [{"type": "missing", "loc": ["body", "file"],
                                                         "msg": "Field required"}]})
            body = raw.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            store = self.server.__dict__.setdefault("uploaded", {})
            aid = f"art_{len(store) + 1:016x}"
            store[aid] = body
            return self._send({"artifact_id": aid, "filename": "image1.png", "mime_type": "image/png",
                               "size": len(body)})
        if self.path.endswith("/api/v1/files") and accept == "upload":  # загрузка файла → номер
            if self.headers.get("X-API-Key") != "sk_test":
                return self._deny()
            body = raw.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            store = self.server.__dict__.setdefault("uploaded", {})
            fid = f"f{len(store) + 1}"
            store[fid] = body
            return self._send({"data": {"id": fid, "name": "image1.png"}})
        if "/api/v1/sessions/" in self.path and self.path.endswith("/cancel") and getattr(self.server, "mav_cancel", False):
            self.server.__dict__.setdefault("cancelled", []).append(self.path.split("/")[-2])
            return self._send({"status": "cancelled"})
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError:  # multipart и прочее, чего этот сервер не понимает
            return self._send_code(422, {"detail": [{"type": "model_attributes_type", "loc": ["body"],
                                                     "msg": "Input should be a valid dictionary"}]})
        if self.path.endswith("/api/v1/query"):  # Maverick Agent Core
            if self.headers.get("X-API-Key") != "sk_test":
                return self._deny()
            f = getattr(self.server, "mav_field", "message")
            if "Сообщение пользователя из программы OpsDigest" in str(req.get(f) or ""):  # «как на портале»
                self.server.calls.append(req)  # type: ignore[attr-defined]
                sess = self.server.__dict__.setdefault("sessions", {})
                sid = req.get("session_id") or f"sp{len(sess) + 1}"
                sess[sid] = sess.get(sid, 0) + 1
                text = str(req[f])
                out = {"session_id": sid, "status": "completed",
                       "response_text": f"Ответ портала (ход {sess[sid]}, вложений {len(req.get('attachments') or [])})"}
                if "покажи форму" in text:
                    out.update(status="hitl_waiting", response_text="", hitl_pending=None, pending_form={
                        "id": "form7", "title": "Уточните запрос", "fields": [
                            {"name": "plugin", "label": "Какой плагин?", "options": ["Rooms", "Почта"], "required": True}]})
                    return self._send(out)
                if "подтверди" in text:
                    out["hitl_pending"] = {"id": "h1", "question": "Создать задачу в трекере?", "options": ["Да", "Нет"]}
                if "файл" in text:
                    out["file_artifacts"] = [{"artifact_id": "art_00000000000000aa", "filename": "report.png",
                                              "mime_type": "image/png"}]
                return self._send(out)
            imgs: list[bytes] = []
            words = False
            if accept == "files_obj" and isinstance(req.get("files"), list):
                imgs = [_b64(x["content"]) for x in req["files"] if isinstance(x, dict) and x.get("content")]
            if accept == "strict" and req.get("files") is not None:  # как pydantic: подробный 422
                errs = []
                for i, x in enumerate(req["files"] if isinstance(req["files"], list) else []):
                    if not isinstance(x, dict):
                        errs.append({"type": "model_attributes_type", "loc": ["body", "files", i],
                                     "msg": "Input should be a valid dictionary", "input": x})
                        continue
                    for k in ("file_name", "file_data", "mime"):
                        if k not in x:
                            errs.append({"type": "missing", "loc": ["body", "files", i, k], "msg": "Field required"})
                    for k in x:
                        if k not in ("file_name", "file_data", "mime"):
                            errs.append({"type": "extra_forbidden", "loc": ["body", "files", i, k],
                                         "msg": "Extra inputs are not permitted", "input": x[k]})
                if errs:
                    return self._send_code(422, {"detail": errs})
                imgs, words = [_b64(x["file_data"]) for x in req["files"]], True
            if accept == "upload" and req.get("files") is not None:
                bad = [i for i, x in enumerate(req["files"]) if not isinstance(x, str)]
                if bad:
                    return self._send_code(422, {"detail": [{"type": "string_type", "loc": ["body", "files", bad[0]],
                                                             "msg": "Input should be a valid string"}]})
                up = self.server.__dict__.get("uploaded", {})
                if any(x not in up for x in req["files"]):
                    return self._send_code(404, {"detail": "File not found"})
                imgs = [up[x] for x in req["files"]]
            if accept == "attach" and req.get("attachments") is not None:
                up = self.server.__dict__.get("uploaded", {})
                vals = req["attachments"]
                if not isinstance(vals, list) or any(not isinstance(x, str) for x in vals):
                    return self._send_code(422, {"detail": [{"type": "string_type", "loc": ["body", "attachments", 0],
                                                             "msg": "Input should be a valid string"}]})
                if any(x not in up for x in vals):
                    return self._send({"session_id": "s1", "status": "failed", "response_text": "",
                                       "error": "Attachment not found", "file_artifacts": []})
                imgs = [up[x] for x in vals]
            if imgs:
                self.server.calls.append(req)  # type: ignore[attr-defined]
                out = respond_image(str(req.get(f) or ""), imgs)
                if words and out.isdigit():  # «модель» отвечает словом
                    out = "На картинке цифра " + ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь",
                                                  "восемь", "девять"][int(out)] + "."
                return self._send({"session_id": "s1", "status": "completed", "response_text": out})
            if not str(req.get(f) or "").strip():  # как настоящий Maverick, если поле не то
                return self._send_code(422, {"detail": [{"type": "value_error", "loc": ["body"],
                                                         "msg": "Value error, Нужно ввести текст или прикрепить хотя бы один файл",
                                                         "input": req}]})
            self.server.calls.append(req)  # type: ignore[attr-defined]
            if "нужен hitl_test" in str(req[f]):
                return self._send({"session_id": "s1", "status": "hitl_waiting", "response_text": "",
                                   "hitl_pending": {"tool_name": "mail_search", "arguments": {"q": "x"}}})
            return self._send({"session_id": "s1", "status": "completed", "response_text": respond("", req[f]),
                               "messages": []})
        if not self.path.endswith("/chat/completions") or "/api/v1/" in self.path:
            b = b'{"detail":"Not Found"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        msgs = req["messages"]
        content = msgs[-1]["content"]
        if isinstance(content, list):  # с изображениями (OpenAI-формат)
            if getattr(self.server, "blind", False):
                b = b'{"error":"Model does not support images"}'
                self.send_response(400)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            text = "".join(x.get("text", "") for x in content if x.get("type") == "text")
            imgs = [_b64(x["image_url"]["url"]) for x in content if x.get("type") == "image_url"]
            out = respond_image(text, imgs)
        else:
            out = respond(msgs[0]["content"], content)
        self.server.calls.append(req)  # type: ignore[attr-defined]
        self._send({"choices": [{"message": {"role": "assistant", "content": out}}]})


DELAY = 0.0       # медленная модель для всех поддельных серверов (тесты остановки)
GENERIC = False   # отвечать на map и для чатов, которых нет в MAP (стенд, тесты истории сводок)
CALLS: list[dict] = []   # какие чаты и сообщения модель читала (тесты истории)


def start_server() -> tuple[HTTPServer, str]:
    """Поддельный сервер: OpenAI-совместимый (…/v1/chat/completions) и Maverick (…/api/v1/query, ключ sk_test)."""
    srv = HTTPServer(("127.0.0.1", 0), _H)
    srv.calls = []  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"
