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
    "prob_field_input": "Поле",
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

    # 0) ISO-формат YYYY-MM-DD (из HTML <input type="date">)
    m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$", s)
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        h, mi, sec = int(m_iso.group(4) or 0), int(m_iso.group(5) or 0), int(m_iso.group(6) or 0)
        try:
            datetime(y, mo, d, h, mi, sec)
        except ValueError as e:
            raise ValueError(f"некорректная дата {value!r}: {e}")
        return f"{d:02d}.{mo:02d}.{y:04d} {h:02d}:{mi:02d}:{sec:02d}"

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

    # «Увеличить таблицу» открывает модальное окно, которое блокирует комбо-поле «Проблема».
    # Поля уже видны без раскрытия — не кликаем.
    # try:
    #     p.click("Увеличить таблицу", timeout=3)
    #     p._log("раскрыт полный список полей")
    #     time.sleep(1.0)
    # except RPAError:
    #     p._log("не удалось раскрыть таблицу (возможно, уже раскрыта)")

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
        for c in all_scan:
            for src, t in c["texts"]:
                if "поля назначения" in t.lower():
                    try:
                        target_url = c["frame"].url
                    except Exception:
                        pass
                    break
            if target_url:
                break
        # 2) Собираем кликабельные элементы в этом фрейме (поля — кликабельные строки)
        items: list[tuple[float, str]] = []
        for c in all_scan:
            try:
                if target_url and c["frame"].url != target_url:
                    continue
            except Exception:
                continue
            if not c["click"]:
                continue
            for src, t in c["texts"]:
                t = t.strip()
                if not t or t.lower() in exclude:
                    continue
                items.append((c["y"], t))
                break
        # 3) Сортируем по Y (сверху вниз) — порядок полей в представлении
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


def _cancel_view_settings(p: Portal):
    """Выход из настроек представления: Escape (закрыть dropdown) → Отмена → подтверждение."""
    p._log("выхожу из настроек: Отмена")
    # Сначала закрываем любой открытый dropdown/попап
    try:
        p.page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception:
        pass
    p.click(UI["prob_btn_cancel"], timeout=6)
    p._sleep(0.8)
    for confirm in [UI["prob_btn_confirm_cancel"], "Да, отменить", "Да", "Подтвердить", "ОК", "OK"]:
        try:
            p.click(confirm, timeout=3)
            p._log(f"подтверждение: «{confirm}»")
            break
        except RPAError:
            continue
    p._sleep(1.5)


def export_problems(p: Portal, downloads_dir: str | None = None, timeout: float = 120) -> str:
    """Сценарий: выгрузка проблем в Excel.
    1. Навигация: Управление проблемы → Очередь проблем / известных ошибок
    2. Ещё → Настройка представления → Поля → чтение списка полей
    3. Отмена → подтверждение (возврат к списку проблем)
    4. Ещё → Экспорт в Excel → скачивание файла в «Загрузки»"""
    p._log("=== СЦЕНАРИЙ: ВЫГРУЗКА ПРОБЛЕМ ===")
    _navigate_to_problems(p)
    _open_view_settings(p)
    _open_fields_tab(p)
    fields = _read_view_fields(p)
    Path("view_fields.json").write_text(
        json.dumps({"fields": fields, "count": len(fields)}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    p._log(f"список полей → view_fields.json ({len(fields)} полей)")
    # ── СВЕРКА С ЭТАЛОНОМ ──
    check_result = run_field_check(p, fields)

    # ── АВТОМАТИЧЕСКАЯ НАСТРОЙКА ПОЛЕЙ (если есть расхождения) ──
    if check_result and (check_result.get("missing") or check_result.get("extra")):
        p._log("\n>>> Расхождения найдены — запускаю автоматическую настройку полей...")
        configured = configure_view_fields(p, max_iterations=10)
        if configured:
            p._log(">>> Настройка завершена — повторная сверка...")
            # Повторное чтение полей после настройки
            fields_after = _read_view_fields(p)
            check_result = run_field_check(p, fields_after)
        else:
            p._log(">>> Настройка не завершена полностью — продолжаю с текущими полями")
    elif check_result:
        p._log("\n>>> Поля совпадают с эталоном — корректировка не нужна")

    _cancel_view_settings(p)
    # После выхода из настроек портал перенаправляет на «Очередь объектов на мою группу поддержки»
    # (раздел инцидентов). Повторная навигация в раздел проблем перед экспортом.
    p._log("возвращаюсь в «Очередь проблем/известных ошибок» перед экспортом...")
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

def _add_field(p: Portal, field_name: str, timeout: float = 15):
    """Добавить одно поле в представление.

    Алгоритм:
    1. Кликнуть на название поля в списке доступных (левая панель, near=Поля назначения).
    2. Кликнуть «Добавить в список».
    Fallback: найти input/combobox, ввести название, Enter, «Добавить в список».
    """
    p._log(f"  + добавляю «{field_name}»...")
    start = time.time()

    # ── Стратегия 1: кликнуть на поле в списке доступных (левая панель) ──
    try:
        p.click(field_name, timeout=5, near="Поля назначения")
        time.sleep(0.5)
        p.click(UI["prob_btn_add_to_list"], timeout=5)
        time.sleep(1.0)
        p._log(f"  ✅ «{field_name}» добавлено (клик по списку)")
        return True
    except RPAError:
        pass

    # ── Стратегия 2: кликнуть без near (поле может быть в любом месте) ──
    try:
        p.click(field_name, timeout=5)
        time.sleep(0.5)
        p.click(UI["prob_btn_add_to_list"], timeout=5)
        time.sleep(1.0)
        p._log(f"  ✅ «{field_name}» добавлено (клик без near)")
        return True
    except RPAError:
        pass

    # ── Стратегия 3 (fallback): найти input/combobox, ввести, Enter ──
    if time.time() - start < timeout:
        field_clicked = False
        # Поле-комбо в портале называется «Проблема» (ExtJS combobox для выбора поля)
        for label in ["Проблема", UI["prob_field_input"], "Название поля", "Добавить поле", "Поле:"]:
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
                            'input:not([type=hidden]):not([type=button]):not([disabled]),'
                            + ' [role=combobox], [role=textbox], select'
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
            time.sleep(0.5)
            # Закрыть dropdown, если он открылся при клике
            try:
                p.page.keyboard.press("Escape")
                time.sleep(0.3)
            except Exception:
                pass
            # Очистить текущее значение
            try:
                p.page.keyboard.press("Control+a")
                p.page.keyboard.press("Delete")
                time.sleep(0.3)
            except Exception:
                pass
            # Ввести название поля — ExtJS-комбо покажет фильтр
            try:
                p.page.keyboard.type(field_name, delay=50)
            except Exception:
                pass
            time.sleep(1.5)  # дождаться фильтрации dropdown
            # Выбрать из dropdown: Enter выбирает первый вариант
            p.page.keyboard.press("Enter")
            time.sleep(0.8)
            # Закрыть dropdown (Escape), чтобы он не блокировал клики
            try:
                p.page.keyboard.press("Escape")
                time.sleep(0.5)
            except Exception:
                pass
            # Клик «Добавить в список»
            try:
                p.click(UI["prob_btn_add_to_list"], timeout=5)
                time.sleep(1.0)
                p._log(f"  ✅ «{field_name}» добавлено (через комбо «Проблема»)")
                return True
            except RPAError:
                pass

    p._log(f"  !! НЕ УДАЛОСЬ добавить «{field_name}» — пропускаю")
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


def configure_view_fields(p: Portal, max_iterations: int = 3) -> bool:
    """Итеративно настраивает поля представления до совпадения с эталоном.

    Цикл:
    1. Читаем текущие поля
    2. Сравниваем с эталоном
    3. Добавляем недостающие
    4. Удаляем лишние
    5. Переставляем по порядку
    6. Повторяем (макс. max_iterations)
    7. При совпадении → «Готово»

    Возвращает True если удалось достичь совпадения.
    """
    p._log("=== НАСТРОЙКА ПОЛЕЙ ПРЕДСТАВЛЕНИЯ ===")
    ref_fields, aliases = _load_reference_fields()
    if not ref_fields:
        p._log("⚠️ reference_fields.json не найден или пуст — настройка пропущена")
        return False

    prev_missing: frozenset[str] = frozenset()
    prev_extra: frozenset[str] = frozenset()

    _stuck_count = 0
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

        # No-progress guard: если те же поля не хватает — выходим из цикла
        curr_missing = frozenset(f.lower().strip() for f in result["missing"])
        curr_extra = frozenset(f.lower().strip() for f in result["extra"])
        if iteration > 1 and curr_missing == prev_missing and curr_extra == prev_extra:
            p._log(f"  ⚠️ Нет прогресса (итерация {iteration}): те же {len(curr_missing)} полей не хватает — останавливаюсь")
            break
        prev_missing = curr_missing
        prev_extra = curr_extra

        # 3. Проверяем: всё ли совпадает (включая порядок)
        if not result["missing"] and not result["extra"]:
            # Проверяем порядок
            current_lower = [f.lower().strip() for f in current_fields]
            # Строим ожидаемый порядок (с учётом алиасов)
            expected_order = []
            for ref in ref_fields:
                # Ищем соответствующее имя в портале
                found = None
                for pf in current_fields:
                    if pf.lower().strip() == ref.lower().strip():
                        found = pf
                        break
                if found is None:
                    for alias in aliases.get(ref, []):
                        if alias in current_fields:
                            found = alias
                            break
                if found:
                    expected_order.append(found)
            expected_lower = [f.lower().strip() for f in expected_order]

            if current_lower == expected_lower:
                p._log("✅ ПОЛЯ ПОЛНОСТЬЮ СОВПАДАЮТ С ЭТАЛОНОМ (название + порядок)")
                # 7. Нажимаем «Готово» (сначала Escape — закрыть dropdown)
                try:
                    p.page.keyboard.press("Escape")
                    time.sleep(0.5)
                except Exception:
                    pass
                try:
                    p.click(UI["prob_btn_done"], timeout=6)
                    time.sleep(2)
                    p._log("нажата «Готово» — возвращаюсь к списку проблем")
                    return True
                except RPAError:
                    p._log("!! не удалось нажать «Готово»")
                    return False
            else:
                p._log("  порядок не совпадает — переставляю...")
                _reorder_fields(p, current_fields, ref_fields, aliases)
                time.sleep(1)
                continue  # повторная проверка

        # 4. Добавляем недостающие поля
        added = 0
        for field in result["missing"]:
            if _add_field(p, field):
                added += 1
        if added:
            p._log(f"  добавлено полей: {added}")
            time.sleep(1)
            _stuck_count = 0  # сбрасываем счётчик
        else:
            _stuck_count += 1
            if _stuck_count >= 2:
                p._log(f"  ⚠️ 2 итерации без прогресса — прекращаю настройку полей")
                break

        # 5. Удаляем лишние поля
        removed = 0
        for field in result["extra"]:
            if _remove_field(p, field):
                removed += 1
        if removed:
            p._log(f"  удалено полей: {removed}")
            time.sleep(1)

        # 6. Переставляем (если есть совпадающие, но не по порядку)
        if result["match"] or result["renamed"]:
            _reorder_fields(p, _read_view_fields(p), ref_fields, aliases)
            time.sleep(1)

    p._log(f"⚠️ Не удалось достичь полного совпадения за {max_iterations} итераций")
    # Всё равно пробуем нажать «Готово» (частичная настройка лучше, чем ничего)
    try:
        p.click(UI["prob_btn_done"], timeout=6)
        time.sleep(2)
        p._log("нажата «Готово» (частичная настройка)")
    except RPAError:
        p._log("!! не удалось нажать «Готово»")
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
