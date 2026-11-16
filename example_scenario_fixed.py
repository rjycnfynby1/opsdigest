# -*- coding: utf-8 -*-
"""
Пример сценария обработки заявок. Все тексты интерфейса вынесены в словарь UI —
поменяйте их на те, что видите в своём портале (подсмотреть: python itsm_rpa.py scout ...).

Запуск:
  # отдельный профиль Edge (первый раз войдите в портал руками, дальше сессия сохранится)
  python example_scenario.py --url https://itsm.company.ru --channel msedge

  # или подключиться к уже открытому Edge/Chrome, запущенному с --remote-debugging-port=9222
  python example_scenario.py --cdp http://localhost:9222

  # только собрать данные по очереди, ничего не меняя
  python example_scenario.py --url ... --dry-run

  # поиск инцидентов по полям формы
  python example_scenario.py --url https://itsm.example.com/ --channel chrome --search \
      --group "Service Desk" --date-after "01.10.2026 00:00:00"
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from itsm_rpa import Portal, RPAError

# ───── тексты интерфейса: правьте под свой портал ─────
UI = {
    "nav_section": "01. Управление инцидентами",
    "queue_menu": "Очередь инцидентов",
    "queue_tab": "Список инцидентов",
    "grid_columns": ["Номер", "Статус", "Группа поддержки", "Исполнитель", "Краткое описание"],
    "new_status": "Зарегистрирован",

    "btn_accept": "Принять в работу",
    "btn_hold": "Перевести в ожидание",
    "btn_resume": "Вывести из ожидания",
    "btn_task": "Выписать задание",
    "btn_link": "Связать с проблемой",
    "btn_save": "Сохранить",
    "btn_close": "Закрыть",
    "btn_ok": "ОК",

    "f_status": "Статус",
    "card_fields": ["Номер", "Статус", "Приоритет", "Назначено группе", "Краткое описание",
                    "Подробное описание", "Дата регистрации", "Срок решения (SLA)", "Осталось по SLA",
                    "OLA", "Связанная проблема", "Задания"],

    "hold_reason": "Причина ожидания",
    "hold_date": "Дата возобновления",
    "hold_comment": "Комментарий",

    "task_group": "Группа исполнителей",
    "task_text": "Описание задания",
    "btn_task_save": "Сохранить задание",

    "problem_search": "Номер или описание",
    "btn_find": "Найти",
    "btn_link_confirm": "Связать",
    "f_problem": "Связанная проблема",
    "f_tasks": "Задания",

    "st_in_work": "В работе",
    "st_hold": "Ожидание",

    # ── поиск инцидентов ──
    "search_menu": "Поиск инцидентов",
    "search_group": "Группа поддержки",
    "search_created_after": "Создан после",
    "search_created_before": "Создан до",
    "search_btn": "Поиск",
    "search_btn_alt": "Найти",
    "search_grid_columns": ["Номер", "Статус", "Группа поддержки", "Исполнитель", "Краткое описание"],

    # ── экспорт в Excel ──
    "btn_more": "Ещё",
    "btn_export_excel": "Экспорт в Excel",
    "btn_export_excel_alt": "Экспортировать в Excel",

    # ── управление проблемами ──
    "prob_nav_section": "06. Управление проблемами",
    "prob_queue_menu": "Очередь проблем",
    "prob_queue_menu_alt": "Очередь проблем/известных ошибок",
    "prob_view_settings": "Настройка текущего представления",
    "prob_fields_tab": "Поля",
    "prob_btn_cancel": "Отмена",
    "prob_btn_confirm_cancel": "Подтвердить",

    # ── настройка полей представления ──
    "prob_field_input": "Проблема",
    "prob_btn_add_to_list": "Добавить в список",
    "prob_btn_remove_field": "Удалить поле",
    "prob_btn_move_up": "Переместить поле вверх",
    "prob_btn_move_down": "Переместить поле вниз",
    "prob_btn_done": "Готово",
}


# ───── шаги сценария: очередь ─────

def _switch_to_flat_view(p: Portal) -> bool:
    """Пытается сменить 'Представление' на плоский список (без группировки)."""
    try:
        p.click("Представление", timeout=3)
        time.sleep(0.5)
        for opt in ["Список инцидентов", "Все инциденты", "Список", "Плоский список", "Без группировки"]:
            try:
                p.click(opt, timeout=2)
                time.sleep(1.5)
                p._log(f"представление → «{opt}»")
                return True
            except RPAError:
                continue
        return False
    except RPAError:
        return False


def _expand_groups(p: Portal, max_rounds: int = 20):
    """Итеративно раскрывает свёрнутые группы (по одному клику за раунд). Работает во всех iframe."""
    js = r"""() => {
        const rows = document.querySelectorAll('tr, [role="row"]');
        for (const row of rows) {
            const txt = (row.textContent || '').trim();
            if (/Группа поддержки/i.test(txt) && /позиц/i.test(txt)) {
                const btn = row.querySelector(
                    '[class*="expand"], [class*="toggle"], [class*="caret"],'
                    + ' [class*="arrow"], [class*="icon"], [class*="tree"],'
                    + ' [class*="plus"], [class*="chevron"], [class*="disclosure"]'
                );
                (btn || row).click();
                return 1;
            }
        }
        return 0;
    }"""
    for _ in range(max_rounds):
        n = 0
        for fr in p.frames():
            try:
                r = fr.evaluate(js)
                if r:
                    n = r
                    break
            except Exception:
                continue
        if not n:
            break
        time.sleep(1.5)
    else:
        p._log("внимание: не все группы могли раскрыться")


def open_queue(p: Portal) -> list[dict]:
    p.click(UI["nav_section"])   # раскрыть дерево навигации
    p.click(UI["queue_menu"])
    time.sleep(2)  # дождаться загрузки страницы

    # Попробовать сменить представление через клик на dropdown
    try:
        p._log("открываю dropdown Представление...")
        p.click("Инциденты на мою группу поддержки", timeout=5)
        time.sleep(1.5)
        switched = False
        for opt in ["Список инцидентов", "Все инциденты", "Плоский список", "Без группировки", "Список"]:
            try:
                p.click(opt, timeout=2)
                p._log(f"представление → «{opt}»")
                switched = True
                time.sleep(2)
                break
            except RPAError:
                continue
        if not switched:
            p._log("не удалось сменить представление — пробую раскрыть группы")
    except RPAError:
        p._log("не удалось открыть dropdown Представление")

    # Если представление не сменилось — раскрываем свёрнутые группы
    try:
        p.read_grid(UI["grid_columns"], timeout=3)
    except RPAError:
        p._log("таблица пуста — раскрываю группы...")
        _expand_groups(p)
        time.sleep(1)

    return p.read_grid(UI["grid_columns"])


def back_to_queue(p: Portal) -> list[dict]:
    p.click(UI["queue_tab"])
    return p.read_grid(UI["grid_columns"])


def open_ticket(p: Portal, number: str):
    p.click(number, exact=True)
    p.wait_text(UI["btn_close"])          # карточка загрузилась
    p.wait_value("Номер", number)         # и это нужная карточка


def collect_ticket(p: Portal) -> dict:
    return p.read_card(UI["card_fields"])


def take_in_work(p: Portal):
    p.click(UI["btn_accept"])
    p.wait_value(UI["f_status"], UI["st_in_work"])


def put_on_hold(p: Portal, reason: str, date: str = "", comment: str = ""):
    p.click(UI["btn_hold"])
    p.select(UI["hold_reason"], reason)
    if date:
        p.fill(UI["hold_date"], date)
    if comment:
        p.fill(UI["hold_comment"], comment)
    p.click(UI["btn_ok"])
    p.wait_value(UI["f_status"], UI["st_hold"])


def resume(p: Portal):
    p.click(UI["btn_resume"])
    p.wait_value(UI["f_status"], UI["st_in_work"])


def create_task(p: Portal, group: str, text: str) -> str:
    p.click(UI["btn_task"])
    p.select(UI["task_group"], group)
    p.fill(UI["task_text"], text)
    p.click(UI["btn_task_save"])
    return p.wait_value(UI["f_tasks"], "TSK", contains=True)


def link_problem(p: Portal, problem: str):
    p.click(UI["btn_link"])
    p.fill(UI["problem_search"], problem)
    p.click(UI["btn_find"])
    p.click(problem, exact=True)
    p.click(UI["btn_link_confirm"], exact=True)
    p.wait_value(UI["f_problem"], problem)


def close_ticket(p: Portal):
    p.click(UI["btn_close"])


# ───── основной цикл: очередь ─────

def process_queue(p: Portal, dry_run: bool = False, out_file: str = "tickets.json") -> list[dict]:
    rows = open_queue(p)
    new = [r for r in rows if (r.get("Статус") or "").strip() == UI["new_status"]]
    p._log(f"в очереди {len(rows)}, новых: {len(new)}")
    collected = []
    for r in new:
        num = r["Номер"]
        try:
            open_ticket(p, num)
            data = collect_ticket(p)
            if not dry_run:
                take_in_work(p)
                data["Статус"] = UI["st_in_work"]
            collected.append(data)
            close_ticket(p)
            p.read_grid(UI["grid_columns"])    # дождаться возврата к списку
        except RPAError as e:
            p._log(f"!! {num}: {e}")
    Path(out_file).write_text(json.dumps(collected, ensure_ascii=False, indent=2), encoding="utf-8")
    p._log(f"данные по {len(collected)} заявкам → {out_file}")
    return collected


# ───── парсинг произвольных дат ─────

# Русские названия месяцев → номер (по «корню», чтобы ловить все падежи)
_RU_MONTHS = [
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4),
    ("май", 5), ("июн", 6), ("июл", 7), ("август", 8),
    ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12),
]


def _expand_year(y: int) -> int:
    """2-значный год → 4-значный (26 → 2026)."""
    return 2000 + y if y < 100 else y


def parse_date_to_itsm(value: str) -> str:
    """Приводит произвольную дату/время к формату ITSM «ДД.ММ.ГГГГ ЧЧ:ММ:СС».

    Понимает: рус. названия месяцев («5 октября 2026 г. 15:45»), 2-значные годы
    («1.10.26»), разделители . / - и пробел, опциональное время (без времени → 00:00:00)."""
    s = (value or "").strip()
    if not s:
        raise ValueError("пустая дата")

    # 1) Русские месяцы → цифры: «5 октября 2026» → «5 10 2026»
    norm = s
    for stem, num in _RU_MONTHS:
        norm = re.sub(rf"\b{stem}\w*", str(num), norm, flags=re.IGNORECASE)
    # убираем «г.» / «г» / «год» после года
    norm = re.sub(r"\bг\.?(?!\w)", " ", norm, flags=re.IGNORECASE)
    norm = re.sub(r"\bгод\w*", " ", norm, flags=re.IGNORECASE)

    # 2) Д.М.Г [Ч:М[:С]]
    m = re.search(
        r"(\d{1,2})\s*[./\- ]\s*(\d{1,2})\s*[./\- ]\s*(\d{2,4})"
        r"(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        norm)
    if not m:
        raise ValueError(f"не удалось распознать дату: {value!r}")

    d, mo, y = int(m.group(1)), int(m.group(2)), _expand_year(int(m.group(3)))
    h, mi, sec = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)

    try:
        datetime(y, mo, d, h, mi, sec)  # валидация
    except ValueError as e:
        raise ValueError(f"некорректная дата {value!r}: {e}")

    return f"{d:02d}.{mo:02d}.{y:04d} {h:02d}:{mi:02d}:{sec:02d}"


# ───── СЦЕНАРИЙ: ПОИСК ИНЦИДЕНТОВ ─────

def _fill_search_field(p: Portal, label: str, value: str) -> str:
    """Заполняет поле поиска: fill без клика + Tab для фиксации.
    Combo-виджеты (hpsm.widgets.Combo) НЕ кликаем — открывают dropdown-попап.
    Порядок: fill+Tab → fill → select (последний шанс для <select>)."""
    # 1) Прямой fill + Tab (основной способ, без клика по combo)
    try:
        p.fill(label, value, timeout=5, commit=True)
        return "fill+tab"
    except RPAError:
        pass
    # 2) Fill без Tab (на случай если commit не нужен)
    try:
        p.fill(label, value, timeout=5)
        return "fill"
    except RPAError:
        pass
    # 3) Последний шанс: select (для настоящих <select>)
    try:
        p.select(label, value, timeout=3)
        return "select"
    except RPAError:
        raise RPAError(f"Не удалось заполнить поле «{label}» значением «{value}»")


def _trigger_search(p: Portal):
    """Запускает поиск: пробует кнопку, потом Enter."""
    for btn in [UI["search_btn"], UI["search_btn_alt"], "Искать", "Выполнить поиск"]:
        try:
            p.click(btn, timeout=3)
            p._log(f"клик «{btn}»")
            return
        except RPAError:
            continue
    # Fallback: Enter
    p.press("Enter")
    p._log("нажат Enter")


def _read_search_results(p: Portal) -> list[dict] | None:
    """Пытается прочитать результаты поиска из таблицы."""
    try:
        rows = p.read_grid(UI["search_grid_columns"], timeout=8)
        p._log(f"результаты: {len(rows)} строк")
        return rows
    except RPAError:
        return None


def _check_validation_errors(p: Portal) -> list[str]:
    """Ищет на экране всплывающие сообщения об ошибках валидации."""
    error_texts = []
    error_patterns = [
        "некорректно", "неверно", "ошибка", "не найдено", "не существует",
        "обязательно", "требуется", "недопустимо", "не может быть",
        "invalid", "error", "required", "not found", "does not exist",
    ]
    try:
        scan = p._scan()
        for c in scan:
            for src, t in c["texts"]:
                tl = t.lower()
                if any(pat in tl for pat in error_patterns):
                    error_texts.append(t.strip())
    except RPAError:
        pass
    for label in ["Ошибка", "Внимание", "Уведомление", "Сообщение"]:
        try:
            with p._quietly():
                v = p.read(label, timeout=1)
                if v:
                    error_texts.append(f"{label}: {v}")
        except RPAError:
            pass
    return list(dict.fromkeys(error_texts))


def search_incidents(p: Portal, group: str, date_after: str,
                     date_before: str = "", out_file: str = "search_results.json") -> list[dict]:
    """Сценарий: Поиск инцидентов по форме."""
    p._log("=== СЦЕНАРИЙ: ПОИСК ИНЦИДЕНТОВ ===")

    # 0. Нормализуем даты в формат ITSM (ДД.ММ.ГГГГ ЧЧ:ММ:СС)
    try:
        _na = parse_date_to_itsm(date_after)
        if _na != date_after:
            p._log(f"дата «Создан после»: «{date_after}» → «{_na}»")
        date_after = _na
    except ValueError as e:
        p._log(f"!! «Создан после» не распознано: {e}")
    if date_before:
        try:
            _nb = parse_date_to_itsm(date_before)
            if _nb != date_before:
                p._log(f"дата «Создан до»: «{date_before}» → «{_nb}»")
            date_before = _nb
        except ValueError as e:
            p._log(f"!! «Создан до» не распознано: {e}")

    # 1. Навигация
    p.click(UI["nav_section"])
    time.sleep(1)
    p.click(UI["search_menu"])
    time.sleep(3)  # дождаться загрузки страницы поиска

    # 2. Заполнение полей
    p._log(f"заполняю «{UI['search_group']}» = «{group}»")
    mode = _fill_search_field(p, UI["search_group"], group)
    p._log(f"  (способ: {mode})")

    p._log(f"заполняю «{UI['search_created_after']}» = «{date_after}»")
    mode = _fill_search_field(p, UI["search_created_after"], date_after)
    p._log(f"  (способ: {mode})")

    if date_before:
        p._log(f"заполняю «{UI['search_created_before']}» = «{date_before}»")
        try:
            _fill_search_field(p, UI["search_created_before"], date_before)
        except RPAError as e:
            p._log(f"  (не удалось: {e})")

    # 3. Запуск поиска
    time.sleep(1)
    _trigger_search(p)

    # 4. Ожидание результатов
    time.sleep(4)

    # 5. Чтение результатов
    rows = _read_search_results(p)

    # 6. Если таблица не нашлась — проверяем ошибки валидации
    if rows is None:
        p._log("таблица результатов не найдена — проверяю ошибки валидации...")
        errors = _check_validation_errors(p)
        if errors:
            p._log(f"обнаружены сообщения: {errors}")
        else:
            p._log("ошибок валидации не найдено — возможно, страница ещё грузится или формат другой")
            try:
                p._log("--- scout (первые 2000 символов) ---")
                report = p.scout()
                p._log(report[:2000])
            except Exception as e:
                p._log(f"scout не удался: {e}")

    # 7. Сохранение
    result = {
        "query": {"group": group, "date_after": date_after, "date_before": date_before},
        "rows": rows,
        "row_count": len(rows) if rows else 0,
        "validation_errors": _check_validation_errors(p) if rows is None else [],
    }
    Path(out_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    p._log(f"результаты → {out_file}")
    return rows if rows else []


# ───── СЦЕНАРИЙ: ЭКСПОРТ В EXCEL ─────

def _downloads_dir() -> Path:
    """Папка «Загрузки» пользователя (Windows: ~/Downloads, macOS: ~/Downloads, Linux: ~/Загрузки)."""
    home = Path.home()
    for name in ("Downloads", "Загрузки"):
        d = home / name
        if d.exists():
            return d
    d = home / "Downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_to_excel(p: Portal, downloads_dir: str | None = None, timeout: float = 120) -> str:
    """Экспорт результатов поиска в Excel.

    Клик «Ещё» (самая правая кнопка над таблицей) → в открывшемся меню «Экспорт в Excel»
    → ловим скачивание и сохраняем файл в папку «Загрузки». Возвращает путь к файлу."""
    p._log("=== ЭКСПОРТ В EXCEL ===")
    dl_dir = Path(downloads_dir) if downloads_dir else _downloads_dir()
    dl_dir.mkdir(parents=True, exist_ok=True)

    # снимок существующих файлов до экспорта (для поиска нового, если Playwright не поймал)
    before = {f.name for f in dl_dir.glob("*.xls*")}

    # 1. Клик «Ещё» — открывает меню
    p.click(UI["btn_more"], timeout=6)
    p._sleep(0.6)

    # 2. Клик «Экспорт в Excel» с ожиданием скачивания (Playwright ловит его из любого фрейма)
    saved: Path | None = None
    try:
        with p.page.expect_download(timeout=int(timeout * 1000)) as dl_info:
            for btn in (UI["btn_export_excel"], UI["btn_export_excel_alt"], "Экспорт в Excel", "Экспорт"):
                try:
                    p.click(btn, timeout=6)
                    break
                except RPAError:
                    continue
        download = dl_info.value
        name = download.suggested_filename or "export.xlsx"
        target = dl_dir / name
        if target.exists():  # не затираем существующий
            stem, suffix = target.stem, target.suffix
            i = 1
            while target.exists():
                target = dl_dir / f"{stem} ({i}){suffix}"
                i += 1
        download.save_as(str(target))
        saved = target
        p._log(f"скачано: «{name}» → {target}")
    except Exception as e:
        p._log(f"Playwright не поймал скачивание ({type(e).__name__}) — ищу файл в «{dl_dir}»")

    # 3. Fallback: ждём, пока в «Загрузки» не появится новый .xls* (актуально для CDP-режима)
    if saved is None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = {f.name for f in dl_dir.glob("*.xls*")} - before
            if new:
                cands = [f for f in dl_dir.glob("*.xls*") if f.name in new]
                cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                saved = cands[0]
                p._log(f"файл появился в «Загрузки»: {saved.name}")
                break
            p._sleep(1.0)

    if saved is None:
        raise RPAError(f"Не удалось получить файл экспорта в «{dl_dir}» за {timeout} с")

    # 4. Ждём, пока файл допишется (не пустой)
    for _ in range(15):
        if saved.exists() and saved.stat().st_size > 0:
            break
        p._sleep(1.0)
    size = saved.stat().st_size if saved.exists() else 0
    p._log(f"ЭКСПОРТ ГОТОВ: {saved} ({size} байт)")
    return str(saved)


# ───── СЦЕНАРИЙ: ВЫГРУЗКА ПРОБЛЕМ ─────

def _navigate_to_problems(p: Portal):
    """Навигация: Управление проблемы → Очередь проблем / известных ошибок."""
    p._log("=== НАВИГАЦИЯ: УПРАВЛЕНИЕ ПРОБЛЕМАМИ ===")
    # точное имя с номером (как «01. Управление инцидентами»); без номера match падает ниже порога
    for nav in [UI["prob_nav_section"], "Управление проблемами"]:
        try:
            p.click(nav, timeout=8)
            p._log(f"клик «{nav}»")
            break
        except RPAError:
            continue
    time.sleep(1.5)
    for menu in [UI["prob_queue_menu_alt"], UI["prob_queue_menu"], "Очередь известных ошибок"]:
        try:
            p.click(menu, timeout=5)
            p._log(f"открыл «{menu}»")
            break
        except RPAError:
            continue
    time.sleep(3)  # дождаться загрузки списка проблем


def _open_view_settings(p: Portal):
    """Кнопка «Ещё» → «Настройка текущего представления»."""
    p.click(UI["btn_more"], timeout=6)
    p._sleep(0.8)
    p.click(UI["prob_view_settings"], timeout=6)
    p._sleep(1.5)


def _open_fields_tab(p: Portal):
    """Вкладка «Поля» в настройках представления."""
    p.click(UI["prob_fields_tab"], timeout=6)
    p._sleep(1.5)



def _scroll_and_collect_fields(p: Portal, target_frame, exclude: set) -> list[str]:
    """Скроллит контейнер полей и собирает ВСЕ элементы (включая дубликаты внизу).
    ExtJS использует виртуальный скролл — элементы вне видимой области НЕ рендерятся."""
    fields: list[str] = []
    seen: set[str] = set()
    no_new_rounds = 0
    max_rounds = 15

    js_scroll = r"""() => {
        const candidates = document.querySelectorAll('div, ul, table, [class*="scroll"], [class*="list"], [class*="grid"]');
        let best = null, bestScore = 0;
        for (const el of candidates) {
            const cs = getComputedStyle(el);
            if (cs.overflowY !== 'auto' && cs.overflowY !== 'scroll') continue;
            const r = el.getBoundingClientRect();
            if (r.height < 50 || r.width < 50) continue;
            const rows = el.querySelectorAll('tr, li, [role="row"], [role="gridcell"], [role="listitem"]');
            const score = rows.length * 10 + r.height;
            if (score > bestScore) { bestScore = score; best = el; }
        }
        if (!best) { window.scrollTo(0, document.body.scrollHeight); return {atBottom: true}; }
        best.scrollTop = best.scrollHeight;
        return {atBottom: best.scrollTop + best.clientHeight >= best.scrollHeight - 5};
    }"""

    js_collect = r"""(excludeArr) => {
        const ex = new Set(excludeArr);
        const out = [];
        const CT = new Set(['BUTTON','A','SUMMARY','OPTION','LABEL']);
        const CR = new Set(['button','link','tab','menuitem','option','treeitem','row','gridcell','checkbox','radio','switch']);
        const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','IFRAME','FRAME','HTML','HEAD','META','LINK','BR']);
        for (const el of document.querySelectorAll('body, body *')) {
            if (SKIP.has(el.tagName)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) continue;
            const cs = getComputedStyle(el);
            if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
            const role = el.getAttribute('role') || '';
            const isBtn = el.tagName === 'INPUT' && /^(button|submit|reset|image)$/.test(el.type);
            const click = CT.has(el.tagName) || CR.has(role) || typeof el.onclick === 'function' || cs.cursor === 'pointer' || isBtn;
            if (!click) continue;
            let text = '';
            for (const n of el.childNodes) if (n.nodeType === 3) text += n.nodeValue;
            text = text.replace(/\s+/g, ' ').trim();
            if (!text) { const it = (el.innerText||'').replace(/\s+/g,' ').trim(); if (it && it.length<=100) text = it; }
            if (!text) continue;
            if (ex.has(text.toLowerCase())) continue;
            out.push({text: text, y: r.top});
        }
        return out;
    }"""

    for _ in range(max_rounds):
        try:
            items = target_frame.evaluate(js_collect, sorted(exclude))
        except Exception:
            break
        new_items = 0
        for item in items:
            t = item["text"].strip()
            if t and t not in seen:
                seen.add(t)
                fields.append(t)
                new_items += 1
        if new_items > 0:
            no_new_rounds = 0
        else:
            no_new_rounds += 1
        if no_new_rounds >= 2:
            break
        try:
            result = target_frame.evaluate(js_scroll)
            if result and result.get("atBottom"):
                try:
                    items2 = target_frame.evaluate(js_collect, sorted(exclude))
                    for item in items2:
                        t = item["text"].strip()
                        if t and t not in seen:
                            seen.add(t)
                            fields.append(t)
                except Exception:
                    pass
                break
        except Exception:
            break
        time.sleep(0.5)

    return fields


def _read_view_fields(p: Portal) -> list[str]:
    """Читает список полей из окна «Поля» (настройка представления).
    Поля находятся в области «Поля назначения» — кликабельные элементы в фрейме
    list.do между «Поля назначения» и «Удалить поле». Возвращает список имён полей."""
    p._log("--- ЧТЕНИЕ ПОЛЕЙ ПРЕДСТАВЛЕНИЯ ---")
    # дамп структуры для отладки
    try:
        report = p.scout()
        Path("view_fields_scout.txt").write_text(report, encoding="utf-8")
        p._log("scout → view_fields_scout.txt")
    except Exception as e:
        p._log(f"scout не удался: {e}")
    # скриншот окна полей
    try:
        shot = Path("view_fields_screenshot.png")
        p.page.screenshot(path=str(shot))
        p._log(f"скриншот → {shot}")
    except Exception as e:
        p._log(f"скриншот не удался: {e}")

    # НЕ раскрываем таблицу: поля видны в свёрнутом состоянии,
    # а раскрытие скрывает левую панель с доступными полями для добавления
    p._log("читаю поля без раскрытия таблицы")

    # Кнопки/метки, которые НЕ являются полями представления
    exclude = {"добавить в список", "увеличить таблицу", "поля назначения",
               "удалить поле", "переместить поле вверх", "переместить поле вниз",
               "< назад", "далее >", "готово", "отмена", "назад", "далее"}

    fields: list[str] = []
    seen: set[str] = set()
    try:
        all_scan = p._scan()
        # 1) Находим URL фрейма, содержащего «Поля назначения»
        target_url = None
        target_frame = None
        for c in all_scan:
            for src, t in c["texts"]:
                if "поля назначения" in t.lower():
                    try:
                        target_url = c["frame"].url
                        target_frame = c["frame"]
                    except Exception:
                        pass
                    break
            if target_url:
                break

        if target_frame:
            # FIX 1: Используем скролл для сбора ВСЕХ полей (включая дубликаты внизу)
            fields = _scroll_and_collect_fields(p, target_frame, exclude)
        else:
            # Fallback: старый способ через p._scan()
            items: list[tuple[float, str]] = []
            for c in all_scan:
                if not c["click"]:
                    continue
                for src, t in c["texts"]:
                    t = t.strip()
                    if not t or t.lower() in exclude:
                        continue
                    items.append((c["y"], t))
                    break
            items.sort(key=lambda x: x[0])
            for y, t in items:
                if t not in seen:
                    seen.add(t)
                    fields.append(t)
    except RPAError:
        pass

    p._log(f"найдено полей: {len(fields)}")
    for i, f in enumerate(fields, 1):
        p._log(f"  [{i}] {f}")
    return fields


def _parse_view_summary(text: str) -> list[str]:
    """Отделяет список колонок от кнопки «Поля» и настройки группировки."""
    import re
    text = re.sub(r"^\s*Поля\s*(?:\.{3}|…|:)\s*", "", text.strip())
    text = re.split(r"Группировать\s+по\s*(?:\.{3}|…|:)", text, maxsplit=1)[0]
    return [part.strip() for part in text.split(";") if part.strip()]


def _read_view_summary(p: Portal) -> list[str]:
    """Читает список полей со страницы «Настройка текущего представления».
    ЭТОТ список виден справа от кнопки «Поля» (ДО клика на неё) —
    все настроенные поля через точку с запятой.
    Это источник правды для проверки порядка и дублей."""
    p._log("--- ЧТЕНИЕ СПИСКА ПОЛЕЙ (суммарный, справа от «Поля») ---")
    fields: list[str] = []

    # JS: ищем элемент, содержащий текст с ≥5 точками с запятой
    # (наш список из 24 полей имеет ~23 разделителя «;»)
    js_read_summary = r"""() => {
        // Сначала узкие элементы: контейнер всей формы включает соседние настройки.
        const candidates = Array.from(document.querySelectorAll('div, span, label, p, td, a'))
            .filter(el => el.getClientRects().length)
            .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
        for (const el of candidates) {
            // СКИПАЕМ элементы, содержащие <script> — иначе читаем JS-код страницы
            if (el.querySelector('script')) continue;
            if (el.tagName.toLowerCase() === 'script') continue;
            const text = (el.textContent || '').trim();
            if (text.length < 50) continue;
            const parts = text.split(';');
            if (parts.length < 5) continue;
            // Фильтруем: имя поля (2-80 символов), без JS-паттернов
            const valid = parts.map(s => s.trim()).filter(s => {
                if (s.length < 2 || s.length > 80) return false;
                // Отбрасываем JS-код
                if (/^(var |const |let |function |if\s*\(|return |Ext\.|tpz_|\/\/|\/\*|\{|\}|cwc\.|hpsm\.|dvdRegister|tpzOnLoad|enableAll|verify|getLastMessage|arReenable|arField|objFields|cellsInLastRows|threadStyle|async)/.test(s)) return false;
                if (s.includes('=>') || s.includes('querySelector') || s.includes('addEventListener') || s.includes('new Array') || s.includes('new Object')) return false;
                return true;
            });
            if (valid.length >= 5) {
                const tag = el.tagName.toLowerCase();
                if (tag === 'button') continue;
                return valid;
            }
        }
        return null;
    }"""

    for fr in p.frames():
        try:
            result = fr.evaluate(js_read_summary)
            if result and len(result) >= 5:
                fields = _parse_view_summary(';'.join(result))
                break
        except Exception:
            continue

    # Fallback: через p._scan() ищем текст с многими «;»
    if not fields:
        try:
            scan = p._scan()
            for c in scan:
                for src, t in c["texts"]:
                    t = t.strip()
                    if t.count(';') >= 5:
                        parts = _parse_view_summary(t)
                        if len(parts) >= 5:
                            fields = parts
                            break
                if fields:
                    break
        except Exception:
            pass

    p._log(f"суммарных полей: {len(fields)}")
    for i, f in enumerate(fields, 1):
        p._log(f"  [{i}] {f}")
    return fields


def _read_table_headers(p: Portal) -> list[str]:
    """Читает реальные заголовки колонок таблицы (после закрытия диалога настроек).
    В отличие от _read_view_fields (читает список в диалоге), эта функция
    читает фактические колонки грида — то, что попадёт в экспорт.
    ВАЖНО: вызывать ТОЛЬКО после закрытия диалога (таблица должна быть видна)."""
    p._log("--- ЧТЕНИЕ ЗАГОЛОВОК ТАБЛИЦЫ ---")
    headers: list[str] = []
    seen: set[str] = set()

    # Способ 1: JS — ExtJS-грид (div, не th). Пробуем несколько селекторов.
    js_read = r"""() => {
        const out = [];
        // ExtJS 3.x: .x-grid3-hd > .x-grid3-hd-inner > span.x-grid3-hd-text
        // ExtJS 4+:  .x-grid-header-cell > .x-grid-cell-inner
        // Универсальный: любой элемент с role=columnheader
        const selectors = [
            '.x-grid3-hd-text',
            '.x-grid-header-cell .x-grid-cell-inner',
            '[role="columnheader"]',
            'th'
        ];
        let els = [];
        for (const sel of selectors) {
            els = document.querySelectorAll(sel);
            if (els.length >= 3) break;
        }
        if (els.length >= 3) {
            for (const el of els) {
                let text = (el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!text) continue;
                text = text.replace(/Сортируемые\s*$/i, '').trim();
                if (text && text.length > 1) out.push(text);
            }
            return out;
        }
        // Способ 2 внутри JS: ищем элементы, чей текст заканчивается "Сортируемые"
        const all = document.querySelectorAll('div, span, a, td');
        for (const el of all) {
            const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
            if (t.endsWith('Сортируемые') && t.length > 10) {
                const name = t.slice(0, -t.lastIndexOf('Сортируемые')).trim();
                if (name && name.length > 1) out.push(name);
            }
        }
        return out;
    }"""
    for fr in p.frames():
        try:
            result = fr.evaluate(js_read)
            if result and len(result) >= 5:
                for h in result:
                    if h not in seen:
                        seen.add(h)
                        headers.append(h)
                break
        except Exception:
            continue

    # Способ 3: fallback через p._scan() — ищем элементы с "Сортируемые"
    if len(headers) < 5:
        headers.clear()
        seen.clear()
        try:
            scan = p._scan()
            for c in scan:
                for src, t in c["texts"]:
                    t = t.strip()
                    if t.endswith("Сортируемые"):
                        name = t[:-len("Сортируемые")].strip()
                        if name and name not in seen:
                            seen.add(name)
                            headers.append(name)
        except Exception:
            pass

    p._log(f"заголовков таблицы: {len(headers)}")
    for i, h in enumerate(headers, 1):
        p._log(f"  [{i}] {h}")
    return headers


def _collapse_expanded_table(p: Portal):
    """Сворачивает расширенную таблицу.
    ВАЖНО: «Свернуть таблицу» = кнопка СВОРАЧИВАНИЯ (клик → закрыть).
    «Увеличить таблицу» = кнопка РАСКРЫТИЯ — НЕ кликать (откроет magnify-окно)!
    """
    # 1. JS: закрыть magnify-окно через ExtJS API (самый надёжный способ)
    try:
        js_close_magnify = r"""() => {
            if (typeof Ext !== 'undefined') {
                const wins = Ext.ComponentQuery.query('window');
                for (const w of wins) {
                    const el = w.el;
                    if (el && el.dom && el.dom.className && el.dom.className.includes('sm-table-magnify-win')) {
                        w.close(); return true;
                    }
                }
            }
            const mag = document.querySelector('.sm-table-magnify-win');
            if (mag) {
                const win = mag.closest('.x-window');
                if (win) { win.style.display = 'none'; return true; }
                mag.remove(); return true;
            }
            return false;
        }"""
        for fr in p.frames():
            try:
                if fr.evaluate(js_close_magnify):
                    time.sleep(1.0)
                    p._log("свёрнута таблица (JS ExtJS)")
                    return
            except Exception:
                continue
    except Exception:
        pass

    # 2. Клик по кнопке «Свернуть таблицу» (НЕ «Увеличить таблицу»!)
    try:
        p.click("Свернуть таблицу", timeout=3)
        p._log("свёрнута таблица («Свернуть таблицу»)")
        time.sleep(1.0)
        return
    except RPAError:
        pass

    # 3. JS fallback: найти кнопку «Свернуть таблицу» по тексту
    try:
        js_collapse = r"""() => {
            const els = document.querySelectorAll('a, button, span, div');
            for (const el of els) {
                const t = (el.textContent || '').trim();
                if (t === 'Свернуть таблицу') {
                    el.click(); return true;
                }
            }
            return false;
        }"""
        for fr in p.frames():
            try:
                if fr.evaluate(js_collapse):
                    time.sleep(1.0)
                    p._log("свёрнута таблица (JS)")
                    return
            except Exception:
                continue
    except Exception:
        pass
    p._log("таблица уже свёрнута")


def _force_close_dialog(p: Portal):
    """Принудительное закрытие диалога: Escape → X → JS (ExtJS)."""
    try:
        p.page.keyboard.press("Escape")
        time.sleep(1.5)
        p._log("нажат Escape")
        try:
            p.find("Поля назначения", timeout=2)
            return False
        except RPAError:
            p._log("диалог закрыт (Escape)")
            return True
    except Exception:
        pass
    for x_text in ["\u2715", "\u00d7", "Закрыть", "X", "Close"]:
        try:
            p.click(x_text, timeout=2)
            time.sleep(1.0)
            p._log(f"диалог закрыт (кнопка «{x_text}»)")
            return True
        except RPAError:
            continue
    try:
        js_close = r"""() => {
            if (typeof Ext !== 'undefined') {
                const ws = Ext.ComponentQuery.query('window');
                for (const w of ws) { if (w.isVisible && w.isVisible()) { w.close(); return true; } }
            }
            const btns = document.querySelectorAll('button, a, [role="button"]');
            for (const b of btns) {
                const t = (b.textContent||'').trim().toLowerCase();
                if (t==='отмена'||t==='готово'||t==='cancel'||t==='done') { b.click(); return true; }
            }
            return false;
        }"""
        for fr in p.frames():
            try:
                if fr.evaluate(js_close):
                    time.sleep(1.5)
                    p._log("диалог закрыт через JS")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    p._log("!! не удалось закрыть диалог")
    return False


def _cancel_view_settings(p: Portal):
    """Выход из настроек: сворачиваем таблицу → Готово → Отмена → Escape → JS."""
    _collapse_expanded_table(p)
    try:
        p.click(UI["prob_btn_done"], timeout=4)
        p._log("нажата «Готово»")
        p._sleep(1.5)
        return
    except RPAError:
        pass
    try:
        p.click(UI["prob_btn_cancel"], timeout=4)
        p._log("нажата «Отмена»")
        p._sleep(0.8)
        for confirm in [UI["prob_btn_confirm_cancel"], "Да, отменить", "Да", "Подтвердить", "ОК", "OK"]:
            try:
                p.click(confirm, timeout=3)
                break
            except RPAError:
                continue
        p._sleep(1.5)
        return
    except RPAError:
        pass
    p._log("кнопки не кликаются — принудительное закрытие")
    _force_close_dialog(p)
    p._sleep(1.5)


def _fields_match_reference(actual, reference, aliases):
    """Точное количество и порядок; допустимы только явные алиасы эталона."""
    norm = lambda value: value.strip().casefold()
    return bool(reference) and len(actual) == len(reference) and all(
        norm(got) in {norm(ref), *(norm(alias) for alias in aliases.get(ref, []))}
        for got, ref in zip(actual, reference))


def export_problems(p: Portal, downloads_dir: str | None = None, timeout: float = 120) -> str:
    """Сверка → при расхождении сброс/заполнение → повторная сверка → экспорт."""
    p._log("=== СЦЕНАРИЙ: ВЫГРУЗКА ПРОБЛЕМ ===")
    ref_fields, aliases = _load_reference_fields()
    if not ref_fields:
        raise RPAError("Экспорт проблем отменён: reference_fields.json отсутствует или пуст")
    _navigate_to_problems(p)
    _open_view_settings(p)
    current = _read_view_summary(p)
    if not _fields_match_reference(current, ref_fields, aliases):
        p._log("Поля отличаются от эталона или не прочитаны — сбрасываю и заполняю заново")
        if not rebuild_view_fields(p):
            raise RPAError("Экспорт проблем отменён: не удалось настроить поля по эталону")
        _cancel_view_settings(p)
        _navigate_to_problems(p)
        _open_view_settings(p)
        current = _read_view_summary(p)
        if not _fields_match_reference(current, ref_fields, aliases):
            raise RPAError("Экспорт проблем отменён: повторная сверка полей и порядка с эталоном не пройдена")
    p._log(f"Поля сверены с эталоном: {len(current)}, состав и порядок верны")
    _cancel_view_settings(p)
    _navigate_to_problems(p)
    return export_to_excel(p, downloads_dir=downloads_dir, timeout=timeout)


# ───── СВЕРКА ПОЛЕЙ С ЭТАЛОНОМ ─────

def _load_reference_fields() -> tuple[list[str], dict[str, list[str]]]:
    """Загружает эталонный список полей и алиасы из reference_fields.json."""
    ref_path = Path(__file__).parent / "reference_fields.json"
    if not ref_path.exists():
        return [], {}
    data = json.loads(ref_path.read_text(encoding="utf-8"))
    return data.get("fields", []), data.get("aliases", {})


def compare_fields(portal_fields: list[str], reference_fields: list[str],
                   aliases: dict[str, list[str]] | None = None) -> dict:
    """Сравнивает поля портала с эталонным списком.

    Возвращает dict:
      match:   [(ref, portal)] — точное совпадение
      renamed: [(ref, portal)] — переименовано (алиасы или fuzzy)
      missing: [ref]           — в эталоне есть, в портале нет
      extra:   [portal]        — в портале есть, в эталоне нет
    """
    if aliases is None:
        aliases = {}

    portal_lower = {f.lower().strip(): f for f in portal_fields}

    match: list[tuple[str, str]] = []
    renamed: list[tuple[str, str]] = []
    matched_portal: set[str] = set()
    matched_ref: set[str] = set()

    # 1) Точное совпадение (case-insensitive)
    for ref in reference_fields:
        key = ref.lower().strip()
        if key in portal_lower:
            match.append((ref, portal_lower[key]))
            matched_ref.add(key)
            matched_portal.add(portal_lower[key].lower().strip())

    # 2) Алиасы (известные переименования)
    for ref in reference_fields:
        key = ref.lower().strip()
        if key in matched_ref:
            continue
        for alias in aliases.get(ref, []):
            alias_key = alias.lower().strip()
            if alias_key in portal_lower:
                renamed.append((ref, portal_lower[alias_key]))
                matched_ref.add(key)
                matched_portal.add(alias_key)
                break

    # 3) Fuzzy matching (difflib) для оставшихся
    from difflib import SequenceMatcher
    remaining_refs = [r for r in reference_fields if r.lower().strip() not in matched_ref]
    remaining_portal = [p for p in portal_fields if p.lower().strip() not in matched_portal]

    for ref in remaining_refs:
        best_ratio = 0.0
        best_portal = None
        for p in remaining_portal:
            ratio = SequenceMatcher(None, ref.lower(), p.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_portal = p
        if best_ratio >= 0.65:
            renamed.append((ref, best_portal))
            matched_ref.add(ref.lower().strip())
            matched_portal.add(best_portal.lower().strip())

    # 4) Не совпало
    missing = [r for r in reference_fields if r.lower().strip() not in matched_ref]
    extra = [p for p in portal_fields if p.lower().strip() not in matched_portal]

    return {
        "match": match,
        "renamed": renamed,
        "missing": missing,
        "extra": extra,
    }


def run_field_check(p: Portal, portal_fields: list[str]) -> dict:
    """Выполняет сверку полей портала с эталоном, пишет отчёт в файл и лог."""
    p._log("=== СВЕРКА ПОЛЕЙ С ЭТАЛОНОМ ===")
    ref_fields, aliases = _load_reference_fields()
    if not ref_fields:
        p._log("⚠️ reference_fields.json не найден или пуст — сверка пропущена")
        return {}

    result = compare_fields(portal_fields, ref_fields, aliases)

    # Формируем отчёт
    lines = []
    lines.append("=" * 70)
    lines.append(f"ОТЧЁТ СВЕРКИ ПОЛЕЙ — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append(f"Эталон: {len(ref_fields)} полей | Портал: {len(portal_fields)} полей")
    lines.append("")

    lines.append(f"✅ СОВПАДАЕТ: {len(result['match'])}")
    for ref, portal in result["match"]:
        lines.append(f"   • {ref}")
    lines.append("")

    lines.append(f"⚠️ ПЕРЕИМЕНОВАНО: {len(result['renamed'])}")
    for ref, portal in result["renamed"]:
        lines.append(f"   • {ref} → {portal}")
    lines.append("")

    lines.append(f"❌ ОТСУТСТВУЕТ В ПОРТАЛЕ: {len(result['missing'])}")
    for ref in result["missing"]:
        lines.append(f"   • {ref}")
    lines.append("")

    lines.append(f"➕ ЛИШНЕЕ В ПОРТАЛЕ: {len(result['extra'])}")
    for p_field in result["extra"]:
        lines.append(f"   • {p_field}")
    lines.append("")

    # Вердикт
    if not result["missing"] and not result["renamed"]:
        verdict = "✅ ПОЛЯ СОВПАДАЮТ — корректировка не нужна"
    elif result["missing"]:
        verdict = f"❌ НУЖНА КОРРЕКТИРОВКА — не хватает {len(result['missing'])} полей"
    else:
        verdict = f"⚠️ ПЕРЕИМЕНОВАНО {len(result['renamed'])} полей — проверить"
    lines.append(f"ВЕРДИКТ: {verdict}")
    lines.append("=" * 70)

    report = "\n".join(lines)

    # Вывод в лог
    p._log(report)

    # Сохранение в файл
    report_path = Path("fields_check_report.txt")
    report_path.write_text(report, encoding="utf-8")
    p._log(f"отчёт → {report_path}")

    # Сохранение в JSON (для дальнейшей автоматизации)
    json_path = Path("fields_check_report.json")
    json_path.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "reference_count": len(ref_fields),
        "portal_count": len(portal_fields),
        "match": [{"reference": r, "portal": p} for r, p in result["match"]],
        "renamed": [{"reference": r, "portal": p} for r, p in result["renamed"]],
        "missing": result["missing"],
        "extra": result["extra"],
        "verdict": verdict,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    p._log(f"JSON → {json_path}")

    return result


# ───── НАСТРОЙКА ПОЛЕЙ ПРЕДСТАВЛЕНИЯ ─────

# ───── ПЕРЕЗАПУСК ПОЛЕЙ: удалить ВСЕ → заново добавить по эталону ─────
# Простая и надёжная стратегия вместо инкрементального «сравни/добавь/удали»:
#   1) удалить ВСЕ текущие поля (до пустого списка),
#   2) заново добавить поля эталона ПО ПОРЯДКУ,
#   3) строго проверить итог: количество == эталон, дублей нет, все эталонные есть.
# Если итог не совпадает (в т.ч. количество БОЛЬШЕ эталона) — НЕ считаем успехом.


def _js_reload_field_store(p: Portal) -> None:
    """Принудительно перезагружает ExtJS-store грида полей после удаления.

    Лечит «Проблему A» из HANDOFF: после клика «Да» поле визуально исчезает,
    но при перечитывании DOM снова появляется (store не обновился)."""
    js_reload = r"""() => {
        if (typeof Ext !== 'undefined') {
            try {
                const grids = Ext.ComponentQuery.query('gridpanel');
                for (const g of grids) {
                    const store = g.getStore && g.getStore();
                    if (store && store.reload) { store.reload(); }
                }
            } catch (e) {}
        }
        return true;
    }"""
    for fr in p.frames():
        try:
            fr.evaluate(js_reload)
        except Exception:
            continue
    time.sleep(1.5)


def _wipe_all_fields(p: Portal, max_rounds: int = 80) -> bool:
    """Удаляет ВСЕ поля из «Поля назначения» до пустого списка.

    Цикл: читаем текущие поля → если пусто, стоп → удаляем первое →
    ждём + форсируем перезагрузку store → перечитываем. Защита от зацикливания:
    max_rounds и «нет прогресса» (счётчик не уменьшается N раундов подряд)."""
    p._log("=== УДАЛЯЮ ВСЕ ПОЛЯ ИЗ ПРЕДСТАВЛЕНИЯ ===")
    last_count = None
    stuck = 0
    for _ in range(max_rounds):
        fields = _read_view_fields(p)
        if not fields:
            p._log("  ✅ все поля удалены — список пуст")
            return True
        cnt = len(fields)
        if last_count is not None and cnt >= last_count:
            stuck += 1
            if stuck >= 4:
                p._log(f"  ⚠️ нет прогресса удаления (осталось {cnt}) — останавливаюсь")
                return False
        else:
            stuck = 0
        last_count = cnt
        target = fields[0]
        p._log(f"  удаляю «{target}» (сейчас: {cnt})...")
        _remove_field(p, target)
        time.sleep(2)
        _js_reload_field_store(p)
    final = _read_view_fields(p)
    p._log(f"  ⚠️ не удалось удалить все поля: осталось {len(final)}")
    return not final


def _match_field(field_list: list[str], names: list[str]) -> bool:
    """True, если в field_list есть поле, совпадающее с любым из names.

    Совпадение: точное (case-insensitive), по префиксу («Номер» → «Номер
    проблемы») или fuzzy (difflib >= 0.6). Используется для анти-дубля и
    верификации добавления."""
    from difflib import SequenceMatcher
    for name in names:
        n = name.lower().strip()
        if not n:
            continue
        for f in field_list:
            fl = f.lower().strip()
            if not fl:
                continue
            if fl == n:
                return True
            if len(n) >= 3 and len(fl) >= 3 and (fl.startswith(n) or n.startswith(fl)):
                return True
            if SequenceMatcher(None, n, fl).ratio() >= 0.75:
                return True
    return False


def _add_all_reference_fields(p: Portal, ref_fields: list[str],
                              aliases: dict[str, list[str]] | None = None) -> bool:
    """Добавляет ВСЕ поля эталона ПО ПОРЯДКУ в (пустой) список «Поля назначения».

    Для каждого эталонного поля (имя + алиасы):
      1) АНТИ-ДУБЛЬ: если эквивалентное поле уже есть в списке — пропускаем.
      2) Добавляем ПО ОДНОМУ, с верификацией ДО/ПОСЛЕ: читаем список до,
         добавляем, читаем после — убеждаемся, что поле реально появилось.
      3) Если не появилось — повторяем (до 3 попыток).
      4) После добавления проверяем, что не задублировано, и чистим дубли.
    Возвращает True, если добавлены все поля эталона."""
    if aliases is None:
        aliases = {}
    p._log(f"=== ДОБАВЛЯЮ ПОЛЯ ЭТАЛОНА ПО ПОРЯДКУ ({len(ref_fields)}) ===")
    ok = 0
    for ref in ref_fields:
        names_to_try = [ref] + list(aliases.get(ref, []))

        # ── 1) АНТИ-ДУБЛЬ: эквивалентное поле уже есть? ──
        current = _read_view_fields(p) or []
        if _match_field(current, names_to_try):
            p._log(f"  ⏭️ «{ref}» уже есть в списке — пропускаю (анти-дубль)")
            ok += 1
            continue

        # ── 2) Добавляем по одному, с верификацией до/после ──
        added = False
        for attempt in range(1, 4):  # до 3 попыток
            before = _read_view_fields(p) or []
            for name in names_to_try:
                # если поле уже появилось (от предыдущего имени) — стоп
                if _match_field(_read_view_fields(p) or [], names_to_try):
                    added = True
                    break
                _add_field(p, name)
                after = _read_view_fields(p) or []
                # новое поле появилось (совпадает с names и его не было в before)
                if _match_field(after, names_to_try) and not _match_field(before, names_to_try):
                    added = True
                    break
                # или список просто вырос (поле добавилось под другим именем)
                if len(after) > len(before):
                    added = True
                    break
            if added:
                break
            p._log(f"    повтор {attempt}/3: «{ref}» не появилось, пробую снова")
            time.sleep(1.5)

        if added:
            ok += 1
            p._log(f"  ✅ «{ref}» добавлено (верифицировано)")
        else:
            p._log(f"  ❌ НЕ удалось добавить «{ref}» (пробовал: {names_to_try})")

        # ── 4) Проверка на дубль после добавления ──
        after = _read_view_fields(p) or []
        from collections import Counter
        lower_after = [f.lower().strip() for f in after]
        dupes = {k: v for k, v in Counter(lower_after).items() if v > 1}
        if dupes:
            p._log(f"  ⚠️ дубль после добавления: {dupes} — убираю лишнее")
            for dupe_name in list(dupes.keys()):
                for _ in range(10):
                    cur = _read_view_fields(p) or []
                    occ = sum(1 for f in cur if f.lower().strip() == dupe_name)
                    if occ <= 1:
                        break
                    orig = next((f for f in cur if f.lower().strip() == dupe_name), dupe_name)
                    _remove_field(p, orig)
                    time.sleep(1)

        time.sleep(0.5)
    p._log(f"  добавлено {ok}/{len(ref_fields)} полей эталона")
    return ok == len(ref_fields)


def rebuild_view_fields(p: Portal) -> bool:
    """Полный перезапуск полей представления: удалить ВСЕ → добавить по эталону.

    Возвращает True ТОЛЬКО если итог точно совпадает с эталоном:
      - количество полей == количеству эталона (и НЕ больше!),
      - дублей нет,
      - все эталонные поля присутствуют (с учётом алиасов).
    Иначе возвращает False (скрипт не будет выдавать ложный «успех»)."""
    ref_fields, aliases = _load_reference_fields()
    if not ref_fields:
        p._log("⚠️ reference_fields.json не найден или пуст — перезапуск полей пропущен")
        return False

    p._log("=== НАСТРОЙКА ПОЛЕЙ: УДАЛИТЬ ВСЕ → ДОБАВИТЬ ПО ЭТАЛОНУ ===")

    # 0) Убедиться, что открыта вкладка «Поля» в настройках представления
    try:
        p.find("Поля назначения", timeout=3)
    except RPAError:
        p._log("  открываю настройки представления → вкладку «Поля»...")
        try:
            # export_problems уже открыл настройки: сначала пробуем саму вкладку.
            try:
                _open_fields_tab(p)
            except RPAError:
                _open_view_settings(p)
                _open_fields_tab(p)
            time.sleep(1)
        except RPAError as e:
            p._log(f"  !! не удалось открыть настройки полей: {e}")
            return False

    # 1) УДАЛИТЬ ВСЕ текущие поля
    if not _wipe_all_fields(p):
        p._log("  ⚠️ не удалось удалить все поля — пробую продолжить, но итог будет проверен")

    # 2) ДОБАВИТЬ поля эталона по порядку
    _add_all_reference_fields(p, ref_fields, aliases)

    # 3) СТРОГАЯ ВЕРИФИКАЦИЯ итогового состояния
    time.sleep(2)
    final = _read_view_fields(p)
    if not final:
        p._log("  ❌ не удалось прочитать итоговый список полей")
        return False

    from collections import Counter
    lower = [f.lower().strip() for f in final]
    counts = Counter(lower)
    dupes = {k: v for k, v in counts.items() if v > 1}

    # эталонные имена (в нижнем регистре) + их алиасы
    ref_keys: set[str] = set()
    for ref in ref_fields:
        ref_keys.add(ref.lower().strip())
        for a in aliases.get(ref, []):
            ref_keys.add(a.lower().strip())

    final_set = set(lower)
    missing_refs = [r for r in ref_fields
                    if r.lower().strip() not in final_set
                    and not any(a.lower().strip() in final_set for a in aliases.get(r, []))]
    extra = [f for f in dict.fromkeys(lower) if f not in ref_keys]

    p._log(f"  ИТОГ: полей {len(final)} | эталон {len(ref_fields)} | "
           f"дублей {len(dupes)} | не хватает {len(missing_refs)} | лишнее {len(extra)}")
    if dupes:
        p._log(f"  ⚠️ дубли: {dupes}")
    if missing_refs:
        p._log(f"  ⚠️ не хватает: {missing_refs}")
    if extra:
        p._log(f"  ⚠️ лишнее: {extra}")

    # ── ЖЁСТКИЙ КРИТЕРИЙ УСПЕХА ──
    success = (not dupes and not missing_refs and not extra
               and _fields_match_reference(final, ref_fields, aliases))
    if len(final) > len(ref_fields):
        p._log(f"  ❌ количество полей ({len(final)}) БОЛЬШЕ эталона ({len(ref_fields)}) — НЕ успех")
        success = False

    if success:
        p._log(f"✅ ПОЛЯ ТОЧНО СОВПАДАЮТ С ЭТАЛОНОМ: {len(final)} полей, дублей нет")
        try:
            p.click(UI["prob_btn_done"], timeout=6)
            time.sleep(2)
            p._log("  «Готово» нажато")
        except RPAError:
            p._log("  !! не удалось нажать «Готово»")
            _force_close_dialog(p)
            return False
        return True

    p._log(f"  ❌ НЕ удалось точно совпасть с эталоном — НЕ считаю успехом")
    try:
        p.click(UI["prob_btn_done"], timeout=6)
        time.sleep(2)
    except RPAError:
        _force_close_dialog(p)
    return False


def _verify_field_added(p: Portal, field_name: str, extra_names: list[str] | None = None) -> bool:
    """Быстрая проверка: появилось ли поле в списке «Поля назначения».

    Учитывает переименования портала: точное совпадение, префикс
    («Номер» → «Номер проблемы») и fuzzy. extra_names — алиасы для проверки."""
    try:
        current = _read_view_fields(p)
        if not current:
            return False
        return _match_field(current, [field_name] + list(extra_names or []))
    except Exception:
        return False


def _add_field(p: Portal, field_name: str, timeout: float = 15):
    """Добавить одно поле в представление.

    Алгоритм:
    1. Кликнуть на название поля в списке доступных (левая панель).
    2. Кликнуть «Добавить в список».
    3. ВЕРИФИКАЦИЯ: перечитать список и убедиться что поле появилось.
    Fallback: найти input, ввести название, выбрать в дереве, «Добавить в список».
    Если не удалось выбрать ТОЧНО нужное поле — НЕ добавляем (чтобы не добавить не то).
    """
    p._log(f"  + добавляю «{field_name}»...")
    start = time.time()

    # ── Предварительно: закрыть magnify-окно если оно открыто ──
    try:
        js_close_mag = r"""() => {
            const mag = document.querySelector('.sm-table-magnify-win');
            if (mag) {
                const win = mag.closest('.x-window');
                if (win) { win.style.display = 'none'; return true; }
                mag.remove(); return true;
            }
            if (typeof Ext !== 'undefined') {
                const wins = Ext.ComponentQuery.query('window');
                for (const w of wins) {
                    const el = w.el;
                    if (el && el.dom && el.dom.className && el.dom.className.includes('sm-table-magnify-win')) {
                        w.close(); return true;
                    }
                }
            }
            return false;
        }"""
        for fr in p.frames():
            try:
                if fr.evaluate(js_close_mag):
                    time.sleep(0.5)
                    p._log("    закрыл magnify-окно перед добавлением")
                    break
            except Exception:
                continue
    except Exception:
        pass

    # ── Стратегия 1: кликнуть на поле в списке доступных (левая панель) ──
    try:
        p.click(field_name, timeout=5, near="Поля назначения")
        time.sleep(0.5)
        p.click(UI["prob_btn_add_to_list"], timeout=5)
        time.sleep(1.0)
        if _verify_field_added(p, field_name):
            p._log(f"  ✅ «{field_name}» добавлено (клик по списку)")
            return True
        p._log(f"    верификация не прошла (клик по списку), пробую дальше")
    except RPAError:
        pass

    # ── Стратегия 2: кликнуть без near ──
    try:
        p.click(field_name, timeout=5)
        time.sleep(0.5)
        p.click(UI["prob_btn_add_to_list"], timeout=5)
        time.sleep(1.0)
        if _verify_field_added(p, field_name):
            p._log(f"  ✅ «{field_name}» добавлено (клик без near)")
            return True
        p._log(f"    верификация не прошла (клик без near)")
    except RPAError:
        pass

    # ── Стратегия 3 (fallback): найти input, ввести, выбрать в дереве ──
    if time.time() - start < timeout:
        field_clicked = False
        for label in [UI["prob_field_input"], "Проблема", "Название поля", "Добавить поле", "Поле:"]:
            try:
                f = p.field(label, timeout=3)
                f.locator.click(timeout=3000)
                field_clicked = True
                p._log(f"    поле ввода найдено по подписи «{label}»")
                break
            except RPAError:
                continue

        if not field_clicked:
            # JS fallback: найти любой видимый input в фрейме с «Поля назначения»
            try:
                scan = p._scan()
                target_frame = None
                for c in scan:
                    for src, t in c["texts"]:
                        if "поля назначения" in t.lower():
                            target_frame = c["frame"]
                            break
                    if target_frame:
                        break
                if target_frame:
                    js_find_input = r"""() => {
                        const inputs = document.querySelectorAll(
                            'input:not([type=hidden]):not([type=button]):not([disabled]):not([readonly]),'
                            + ' [role=combobox]:not([readonly]), [role=textbox]:not([readonly]), select'
                        );
                        for (const inp of inputs) {
                            const r = inp.getBoundingClientRect();
                            if (r.width > 50 && r.height > 10 && r.top < 400) {
                                inp.setAttribute('data-rpa-pin', 'add_field_input');
                                inp.focus();
                                return true;
                            }
                        }
                        return false;
                    }"""
                    target_frame.evaluate(js_find_input)
                    try:
                        target_frame.locator('[data-rpa-pin="add_field_input"]').click(timeout=3000)
                        field_clicked = True
                        p._log("    поле ввода найдено через JS (fallback)")
                    except Exception:
                        pass
            except Exception as e:
                p._log(f"    fallback не сработал: {e}")

        if field_clicked:
            # Очистить поле (критично: fill('') работает на уровне фрейма, не page)
            time.sleep(0.3)
            try:
                f.locator.fill("")
                time.sleep(0.3)
                p._log("    поле поиска очищено")
            except Exception:
                try:
                    p.page.keyboard.press("Control+a")
                    p.page.keyboard.press("Delete")
                    time.sleep(0.2)
                except Exception:
                    pass
            # Ввести имя поля побуквенно
            try:
                p.page.keyboard.type(field_name, delay=50)
            except Exception:
                pass
            time.sleep(1.5)

            # КРИТИЧНО: НЕ нажимать Enter! Enter выбирает ПЕРВОЕ значение
            # автокомплит-дропдауна по алфавиту (например «Affected CI Names»),
            # а не введённое поле. Вместо Enter — клик по нейтральной области,
            # чтобы закрыть выпадающее меню, и только потом «Добавить в список».
            _dd_closed = False
            js_close_dropdown = r"""() => {
                // 1) Разфокусировать активный элемент — закрывает комбо-дропдаун
                const ae = document.activeElement;
                if (ae && ae.blur) { try { ae.blur(); } catch (e) {} }
                // 2) Клик по нейтральному контейнеру диалога (не дерево, не кнопка, не инпут)
                const cands = document.querySelectorAll('.x-window-body, .x-panel-body, .x-window');
                for (const el of cands) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 50 && r.height > 50) {
                        for (const type of ['mousedown', 'mouseup', 'click']) {
                            el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                        }
                        return 'neutral';
                    }
                }
                return 'blur';
            }"""
            for fr in p.frames():
                try:
                    _res = fr.evaluate(js_close_dropdown)
                    if _res:
                        p._log(f"    дропдаун закрыт ({_res})")
                        _dd_closed = True
                        break
                except Exception:
                    continue
            if not _dd_closed:
                p._log("    ⚠️ JS не закрыл дропдаун — клик мыши по нейтральной области (угол фрейма)")
                try:
                    _fb = f.frame.frame_element().bounding_box()
                    if _fb:
                        p.page.mouse.click(_fb["x"] + 15, _fb["y"] + 15)
                        _dd_closed = True
                except Exception:
                    pass
            time.sleep(1.0)

            # После закрытия дропдауна введённый текст уже в комбо-инпуте.
            # Правильный workflow (проверен вручную): ввести → закрыть дропдаун →
            # сразу «Добавить в список». Нода дерева НЕ нужна — кнопка добавляет
            # то, что написано в поле.
            try:
                p.click(UI["prob_btn_add_to_list"], timeout=5)
                time.sleep(1.5)
                if _verify_field_added(p, field_name):
                    p._log(f"  ✅ «{field_name}» добавлено (ввод → закрыть дропдаун → добавить)")
                    return True
                else:
                    p._log(f"    ⚠️ верификация не прошла — пробую выбрать в дереве")
            except RPAError:
                p._log(f"    ⚠️ не удалось нажать «Добавить в список» напрямую")

            # Fallback: если прямой путь не сработал — попробуем найти и кликнуть
            # ноду в дереве (ExtJS TreePanel, виртуальный рендер → JS-поиск).
            field_selected = False
            time.sleep(2)

            js_find_in_tree = r"""(name) => {
                const nodes = document.querySelectorAll('.x-tree-node-text, .x-tree-node-anchor, .x-tree-node-el');
                for (const node of nodes) {
                    if (node.textContent.trim().toLowerCase() === name.toLowerCase()) {
                        node.click();
                        return 'exact';
                    }
                }
                for (const node of nodes) {
                    if (node.textContent.trim().toLowerCase().includes(name.toLowerCase())) {
                        node.click();
                        return 'partial';
                    }
                }
                const allEls = document.querySelectorAll('.x-tree-node-ct, .x-tree-node');
                for (const el of allEls) {
                    const text = el.querySelector('.x-tree-node-text');
                    if (text && text.textContent.trim().toLowerCase() === name.toLowerCase()) {
                        el.click();
                        return 'node-ct';
                    }
                }
                return false;
            }"""

            for fr in p.frames():
                try:
                    result = fr.evaluate(js_find_in_tree, field_name)
                    if result:
                        p._log(f"    «{field_name}» найдено в дереве (JS: {result})")
                        field_selected = True
                        break
                except Exception:
                    continue

            if not field_selected:
                try:
                    p.click(field_name, timeout=3, near="Поля назначения")
                    time.sleep(0.5)
                    p._log(f"    «{field_name}» выбрано (Playwright fallback)")
                    field_selected = True
                except RPAError:
                    p._log(f"    ⚠️ не удалось выбрать «{field_name}» в дереве — НЕ добавляю (защита от ошибки)")
                    return False

            if field_selected:
                try:
                    p.click(UI["prob_btn_add_to_list"], timeout=5)
                    time.sleep(1.0)
                    if _verify_field_added(p, field_name):
                        p._log(f"  ✅ «{field_name}» добавлено (через дерево)")
                        return True
                    else:
                        p._log(f"    ⚠️ верификация не прошла после добавления через дерево")
                except RPAError:
                    pass

    p._log(f"  !! НЕ УДАЛОСЬ добавить «{field_name}»")
    return False


def _remove_field(p: Portal, field_name: str):
    """Удалить одно поле из представления: кликнуть строку → «Удалить поле»."""
    p._log(f"  - удаляю «{field_name}»...")

    # 1. Кликнуть на строку с этим полем в таблице «Поля назначения»
    try:
        p.click(field_name, timeout=5, near="Поля назначения")
        time.sleep(0.5)
    except RPAError:
        # Fallback: клик без near
        try:
            p.click(field_name, timeout=5)
            time.sleep(0.5)
        except RPAError:
            p._log(f"  !! не удалось кликнуть строку «{field_name}»")
            return False

    # 2. Клик «Удалить поле»
    try:
        p.click(UI["prob_btn_remove_field"], timeout=5)
        time.sleep(2.0)
        # 3. Подтвердить удаление (диалог «Да/Нет»)
        try:
            p.click("Да", timeout=5)
            time.sleep(1.0)
            p._log(f"  ✅ «{field_name}» удалено (подтверждено)")
        except RPAError:
            # Диалог не появился — возможно, поле удалилось без подтверждения
            p._log(f"  ✅ «{field_name}» удалено (без подтверждения)")
        # Очистить поле поиска в дереве (иначе старый текст мешает следующему поиску)
        try:
            time.sleep(1.0)
            js_clear_search = r"""() => {
                const inputs = document.querySelectorAll('input:not([type=hidden]):not([type=button]):not([disabled]):not([readonly])');
                for (const inp of inputs) {
                    const r = inp.getBoundingClientRect();
                    if (r.width > 50 && r.height > 10 && r.top < 400 && inp.value) {
                        inp.value = '';
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }"""
            for fr in p.frames():
                try:
                    if fr.evaluate(js_clear_search):
                        p._log("    поле поиска в дереве очищено")
                        break
                except Exception:
                    continue
        except Exception:
            pass
        return True
    except RPAError:
        p._log(f"  !! не удалось удалить «{field_name}»")
        return False


def _move_field(p: Portal, field_name: str, direction: str, steps: int = 1):
    """Переместить поле вверх или вниз на N позиций.

    direction: 'up' | 'down'
    """
    btn_text = UI["prob_btn_move_up"] if direction == "up" else UI["prob_btn_move_down"]

    for _ in range(steps):
        # 1. Кликнуть на строку поля
        try:
            p.click(field_name, timeout=4, near="Поля назначения")
        except RPAError:
            try:
                p.click(field_name, timeout=4)
            except RPAError:
                p._log(f"  !! не удалось выбрать «{field_name}» для перемещения")
                return False
        time.sleep(0.3)

        # 2. Кликнуть кнопку перемещения
        try:
            p.click(btn_text, timeout=4)
            time.sleep(0.4)
        except RPAError:
            p._log(f"  !! кнопка «{btn_text}» не найдена")
            return False

    p._log(f"  ⇅ «{field_name}» → {direction} ×{steps}")
    return True


def _reorder_fields(p: Portal, current_fields: list[str], target_order: list[str],
                    aliases: dict[str, list[str]] | None = None):
    """Расставить поля в нужном порядке (по target_order).

    Для каждого поля в target_order проверяем его позицию в current_fields.
    Если не на своём месте — перемещаем вверх/вниз.
    """
    if aliases is None:
        aliases = {}

    # Строим маппинг: эталонное имя → имя в портале (с учётом алиасов)
    portal_map: dict[str, str] = {}  # ref_name → portal_name
    for ref in target_order:
        portal_map[ref] = ref  # по умолчанию совпадает

    # Применяем алиасы
    for ref in target_order:
        for alias in aliases.get(ref, []):
            if alias in current_fields:
                portal_map[ref] = alias
                break

    # Проверяем порядок
    current_lower = [f.lower().strip() for f in current_fields]
    target_lower = [portal_map[r].lower().strip() for r in target_order if portal_map[r] in current_fields]

    # Сравниваем последовательности (только поля, которые есть в обоих)
    if current_lower == target_lower:
        p._log("  порядок полей уже корректный")
        return True

    p._log(f"  переставляю поля (текущий: {len(current_lower)}, целевой: {len(target_lower)})...")

    # Простой алгоритм: для каждого целевого поля (слева направо) двигаем на место
    # Работаем с текущим списком, обновляя его после каждого перемещения
    current = list(current_fields)

    for target_idx, ref in enumerate(target_order):
        portal_name = portal_map.get(ref, ref)
        if portal_name not in current:
            continue  # поле ещё не добавлено

        # Текущая позиция
        try:
            current_idx = next(i for i, f in enumerate(current) if f.lower().strip() == portal_name.lower().strip())
        except StopIteration:
            continue

        if current_idx == target_idx:
            continue  # уже на месте

        # Перемещаем
        if current_idx > target_idx:
            steps = current_idx - target_idx
            _move_field(p, portal_name, "up", steps)
        else:
            steps = target_idx - current_idx
            _move_field(p, portal_name, "down", steps)

        # Обновляем локальный список (симулируем перемещение)
        item = current.pop(current_idx)
        current.insert(target_idx, item)

    return True


def _verify_and_fix_table(p: Portal, max_rounds: int = 2) -> bool:
    """Проверяет РЕАЛЬНЫЕ колонки таблицы после «Готово».
    Если есть дубли или неправильный порядок — переоткрывает диалог,
    удаляет дубли, переставляет поля, нажимает «Готово» снова.
    Возвращает True если таблица корректна (или не удалось проверить)."""
    ref_fields, aliases = _load_reference_fields()
    if not ref_fields:
        return True

    for round_num in range(1, max_rounds + 1):
        headers = _read_table_headers(p)
        if not headers:
            p._log("⚠️ не удалось прочитать заголовки таблицы — пропускаю проверку")
            return True

        # Проверяем дубли
        from collections import Counter
        lower_headers = [h.lower().strip() for h in headers]
        counts = Counter(lower_headers)
        dupes = {name: cnt for name, cnt in counts.items() if cnt > 1}

        # Проверяем совпадение с эталоном (без учёта порядка)
        ref_lower = {f.lower().strip() for f in ref_fields}
        header_set = set(lower_headers)
        missing = ref_lower - header_set
        extra = header_set - ref_lower

        if not dupes and not missing and not extra:
            ref_order = [f.lower().strip() for f in ref_fields]
            actual_order = list(dict.fromkeys(lower_headers))  # unique, preserve order
            if ref_order == actual_order:
                p._log(f"✅ Таблица корректна: {len(headers)} колонок, порядок совпадает")
                return True
            p._log(f"  порядок отличается (round {round_num}) — переставляю...")
        else:
            if dupes:
                p._log(f"  ⚠️ дубли: {dict(dupes)}")
            if missing:
                p._log(f"  ⚠️ не хватает: {missing}")
            if extra:
                p._log(f"  ⚠️ лишнее: {extra}")

        # ── Чиним: переоткрываем диалог ──
        p._log(f"  переоткрываю настройки полей (round {round_num})...")
        try:
            _open_view_settings(p)
            _open_fields_tab(p)
            time.sleep(1)
        except RPAError:
            p._log("  !! не удалось переоткрыть диалог — пропускаю фикс")
            return False

        # Удаляем дубли: для каждого дубля кликаем на строку и «Удалить поле»
        if dupes:
            for dupe_name in list(dupes.keys()):
                orig_name = next((h for h in headers if h.lower().strip() == dupe_name), dupe_name)
                dupe_count = dupes[dupe_name]
                for _ in range(dupe_count - 1):
                    p._log(f"  удаляю дубль «{orig_name}»...")
                    _remove_field(p, orig_name)
                    time.sleep(0.5)

        # Переставляем поля в эталонный порядок
        current_fields = _read_view_fields(p)
        if current_fields:
            _reorder_fields(p, current_fields, ref_fields, aliases)

        # Добавляем недостающие
        if missing:
            for m in missing:
                ref_name = next((f for f in ref_fields if f.lower().strip() == m), m)
                _add_field(p, ref_name)

        # Удаляем лишние
        if extra:
            for e in extra:
                portal_name = next((h for h in headers if h.lower().strip() == e), e)
                _remove_field(p, portal_name)

        # Готово
        try:
            p.click(UI["prob_btn_done"], timeout=6)
            time.sleep(2)
            p._log(f"  «Готово» нажато (round {round_num})")
        except RPAError:
            p._log("  !! не удалось нажать «Готово»")
            _force_close_dialog(p)
            return False

    # Финальная проверка
    final_headers = _read_table_headers(p)
    if final_headers:
        from collections import Counter as C2
        final_lower = [h.lower().strip() for h in final_headers]
        final_dupes = {k: v for k, v in C2(final_lower).items() if v > 1}
        if not final_dupes:
            p._log(f"✅ Таблица после фикса: {len(final_headers)} колонок, дублей нет")
            return True
        p._log(f"⚠️ Дубли остались: {final_dupes}")
    return False


def configure_view_fields(p: Portal, max_iterations: int = 3) -> bool:
    """Итеративно настраивает поля представления до совпадения с эталоном.

    Цикл:
    1. Читаем текущие поля
    2. Сравниваем с эталоном
    3. СНАЧАЛА удаляем лишние
    4. Добавляем недостающие (с верификацией)
    5. Повторяем (макс. max_iterations)
    6. При совпадении → «Готово»

    БЕЗ перестановки полей — порядок не критичен, важно наличие.
    """
    p._log("=== НАСТРОЙКА ПОЛЕЙ ПРЕДСТАВЛЕНИЯ ===")
    ref_fields, aliases = _load_reference_fields()
    if not ref_fields:
        p._log("⚠️ reference_fields.json не найден или пуст — настройка пропущена")
        return False

    prev_missing: frozenset[str] = frozenset()
    prev_extra: frozenset[str] = frozenset()

    for iteration in range(1, max_iterations + 1):
        p._log(f"--- Итерация {iteration}/{max_iterations} ---")

        # 1. Читаем текущие поля
        current_fields = _read_view_fields(p)
        if not current_fields:
            p._log("  не удалось прочитать поля — пробую ещё раз...")
            time.sleep(2)
            current_fields = _read_view_fields(p)
            if not current_fields:
                p._log("  !! не удалось прочитать поля — прерываю настройку")
                return False

        # 2. Сравниваем с эталоном
        result = compare_fields(current_fields, ref_fields, aliases)
        p._log(f"  совпадает: {len(result['match'])}, переименовано: {len(result['renamed'])},"
               f" не хватает: {len(result['missing'])}, лишнее: {len(result['extra'])}")

        # No-progress guard
        curr_missing = frozenset(f.lower().strip() for f in result["missing"])
        curr_extra = frozenset(f.lower().strip() for f in result["extra"])
        if iteration > 2 and curr_missing == prev_missing and curr_extra == prev_extra:
            p._log(f"  ⚠️ Нет прогресса (итерация {iteration}): те же {len(curr_missing)} не хватает — стоп")
            break
        prev_missing = curr_missing
        prev_extra = curr_extra

        # 3. Всё совпадает?
        if not result["missing"] and not result["extra"]:
            p._log("✅ ПОЛЯ ПОЛНОСТЬЮ СОВПАДАЮТ С ЭТАЛОНОМ")
            try:
                p.click(UI["prob_btn_done"], timeout=6)
                time.sleep(2)
                p._log("нажата «Готово» — возвращаюсь к списку проблем")
                return True
            except RPAError:
                p._log("!! не удалось нажать «Готово»")
                return False

        # Сворачиваем таблицу чтобы левая панель была видна
        _collapse_expanded_table(p)

        # 4. СНАЧАЛА удаляем лишние поля
        removed = 0
        for field in result["extra"]:
            if _remove_field(p, field):
                removed += 1
        if removed:
            p._log(f"  удалено полей: {removed}")
            time.sleep(1)
            # Верификация: перечитаем и проверим что лишние реально ушли
            verify_del = _read_view_fields(p)
            if verify_del:
                still_extra = [f for f in result["extra"]
                              if any(f.lower().strip() == v.lower().strip() for v in verify_del)]
                if still_extra:
                    p._log(f"  ⚠️ не удалились: {still_extra} — повторяю удаление")
                    for field in still_extra:
                        if _remove_field(p, field):
                            p._log(f"  ✅ повторное удаление «{field}»")
                    time.sleep(1)

        # 5. Добавляем недостающие (с верификацией)
        added = 0
        for field in result["missing"]:
            if _add_field(p, field):
                added += 1
        if added:
            p._log(f"  добавлено полей: {added}")
            time.sleep(1)

        # 6. Быстрая верификация после изменений
        if removed or added:
            time.sleep(1)
            verify_fields = _read_view_fields(p)
            if verify_fields:
                verify_result = compare_fields(verify_fields, ref_fields, aliases)
                p._log(f"  верификация: совпадает {len(verify_result['match'])},"
                       f" не хватает {len(verify_result['missing'])},"
                       f" лишнее {len(verify_result['extra'])}")

    p._log(f"⚠️ Не удалось достичь полного совпадения за {max_iterations} итераций")
    _cancel_view_settings(p)
    return False


# ───── main ─────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--cdp")
    ap.add_argument("--channel", default="msedge")
    ap.add_argument("--profile", default="rpa_profile")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--record-network", action="store_true", help="писать запросы фронта в network_log.jsonl")
    # --- поиск ---
    ap.add_argument("--search", action="store_true", help="режим: поиск инцидентов по форме")
    ap.add_argument("--group", default="Service Desk", help="Группа поддержки для поиска")
    ap.add_argument("--date-after", default="01.10.2026 00:00:00", help="Создан после (произвольный формат, напр. «1.10.26»)")
    ap.add_argument("--date-before", default="", help="Создан до (произвольный формат, напр. «5 октября 2026 г. 15:45»)")
    ap.add_argument("--export", action="store_true", help="после поиска сделать экспорт в Excel (кнопка «Ещё» → «Экспорт в Excel»)")
    ap.add_argument("--problems", action="store_true", help="режим: выгрузка проблем (Управление проблемы → Очередь проблем → чтение полей → экспорт в Excel)")
    ap.add_argument("--downloads-dir", default="", help="папка для скачанного файла (по умолчанию ~/Downloads)")
    ap.add_argument("--no-wait", action="store_true", help="не ждать Enter после открытия (если уже залогинены в профиле)")
    ap.add_argument("--fresh-profile", action="store_true", help="создать новый профиль (если старый сломан / ERR_INVALID_AUTH_CREDENTIALS)")
    a = ap.parse_args()

    profile_dir = a.profile
    if a.fresh_profile and not a.cdp:
        import time as _t
        profile_dir = f"rpa_profile_{_t.strftime('%Y%m%d_%H%M%S')}"
        print(f"🆕 Новый профиль: {profile_dir}")

    p = (Portal.connect(a.cdp, url_contains=a.url) if a.cdp
         else Portal.launch(a.url, channel=None if a.channel == "none" else a.channel, profile_dir=profile_dir))
    with p:
        if a.record_network:
            p.record_network()
        if not a.cdp and not a.no_wait:
            input("Если нужно — войдите в портал в открывшемся окне и нажмите Enter...")

        if a.problems:
            export_problems(p, downloads_dir=a.downloads_dir or None)
        elif a.search:
            search_incidents(p, group=a.group, date_after=a.date_after, date_before=a.date_before)
            if a.export:
                export_to_excel(p, downloads_dir=a.downloads_dir or None)
        else:
            process_queue(p, dry_run=a.dry_run)
        time.sleep(1)


if __name__ == "__main__":
    main()
