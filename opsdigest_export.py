# -*- coding: utf-8 -*-
"""
OpsDigest: пример выгрузки инцидентов из ITSM по группе поддержки и статусам.
Выполняет три поиска в ITSM (поочерёдно по статусам) и объединяет результаты.

Логика:
  1. Для каждого статуса: заполняем форму → поиск → проверяем результат
  2. Если найдено (2+ инцидентов) → экспорт в Excel кнопкой
  3. Если не найдено → лог «не найдено», переход к следующему статусу
  4. Если 1 инцидент → ITSM открывает полноэкранную карточку → «Назад» → лог

Запуск:
  python opsdigest_export.py --url https://itsm.example.com/ --channel chrome --group "Service Desk"
  python opsdigest_export.py --url https://itsm.example.com/ --channel chrome --group "Service Desk" --dry-run
  python opsdigest_export.py --url https://itsm.example.com/ --channel chrome --group "Service Desk" --export
  python opsdigest_export.py --cdp http://localhost:9222 --group "А"

Статусы по умолчанию: В работе, Назначен, Ожидание.
Можно переопределить: --statuses "В работе,Назначен"
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path

from itsm_rpa import Portal, RPAError
from example_scenario import (
    UI,
    _fill_search_field,
    _trigger_search,
    _read_search_results,
    _check_validation_errors,
    parse_date_to_itsm,
    export_to_excel,
)

# ───── ЖИВОЕ ОКНО БРАУЗЕРА (скриншоты для интерфейса OpsDigest) ─────

_LIVE_OUT: Path | None = None  # задаётся в main()

def _live_snap(p: Portal) -> None:
    """Скриншот текущей страницы → browser_live.png (для панели в интерфейсе)."""
    if _LIVE_OUT is None:
        return
    try:
        _LIVE_OUT.mkdir(parents=True, exist_ok=True)
        p.page.screenshot(path=str(_LIVE_OUT / "browser_live.png"))
    except Exception:
        pass  # скриншот не критичен для выгрузки


# ───── КОНФИГУРАЦИЯ ─────

# Статусы для выгрузки (три отдельных поиска)
DEFAULT_STATUSES = ["В работе", "Назначен", "Ожидание"]

# Группа поддержки по умолчанию
DEFAULT_GROUP = "Service Desk"

# Выходная папка
OUTPUT_DIR = Path(__file__).parent / "opsdigest_output"

# UI-элементы для поиска по статусу (добавлены к базовому UI)
UI_SEARCH = {
    **UI,
    # Поле статуса в форме поиска (ITSM Service Manager)
    "search_status": "Статус",
    "search_status_alt": "Статус инцидента",
}


# ───── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────

def _detect_no_results(p: Portal) -> bool:
    """Проверяет, показано ли на экране сообщение «инциденты не найдены».

    ITSM при пустом результате показывает текстовое уведомление,
    но поля формы остаются с последними значениями.
    """
    no_result_patterns = [
        "не найдены", "не найдено", "ничего не найдено",
        "нет результатов", "результатов нет",
        "инцидентов не найдено", "записей не найдено",
        "не найдено ни одного", "не найдено ни одной",
        "пусто", "нет данных",
    ]
    try:
        scan = p._scan()
        for c in scan:
            for src, t in c["texts"]:
                tl = t.lower().strip()
                if any(pat in tl for pat in no_result_patterns):
                    p._log(f"  обнаружено сообщение: «{t.strip()}»")
                    return True
    except RPAError:
        pass
    return False


def _detect_single_incident_card(p: Portal) -> bool:
    """Проверяет, открыта ли полноэкранная карточка одного инцидента.

    КРИТИЧЕСКИ ВАЖНО: сначала проверяем, ЕСТЬ ЛИ ТАБЛИЦА СО СТРОКАМИ.
    Если таблица с данными видна — это СПИСОК инцидентов, НЕ карточка.
    Только если таблицы нет (или она пуста) И есть кнопка «Назад» —
    считаем, что открыта карточка одного инцидента.
    """
    # Шаг 1: проверяем, есть ли таблица с данными (список результатов)
    try:
        rows = _read_search_results(p)
        if rows is not None and len(rows) > 0:
            p._log(f"  таблица с {len(rows)} строками видна — это СПИСОК, не карточка")
            return False
    except RPAError:
        pass  # Таблица не найдена — возможно, это карточка

    # Шаг 2: таблицы нет — ищем кнопку «Назад» (признак карточки)
    for back_text in ["Назад", "← Назад", "< Назад", "Вернуться"]:
        try:
            p.click(back_text, timeout=2)
            p._log(f"  обнаружена полноэкранная карточка (клик «{back_text}» выполнен)")
            time.sleep(1.5)
            return True
        except RPAError:
            continue
    return False


def _clear_field(p: Portal, label: str) -> bool:
    """Очищает поле: клик → Ctrl+A → Delete."""
    try:
        p.click(label, timeout=3)
        time.sleep(0.3)
        p.page.keyboard.press("Control+a")
        time.sleep(0.1)
        p.page.keyboard.press("Delete")
        time.sleep(0.3)
        p._log(f"  поле «{label}» очищено")
        return True
    except RPAError:
        p._log(f"  не удалось очистить поле «{label}» (возможно, было пусто)")
        return False


def _parse_export_csv(csv_path: str) -> list[dict]:
    """Читает скачанный файл экспорта ITSM («Экспорт в Excel») и возвращает
    список словарей {заголовок: значение}.

    Файл экспорта содержит ВСЕ инциденты по текущему поиску, тогда как
    таблица на странице ограничена 50 строками. Поэтому для полного списка
    используем именно этот файл, а не строки, прочитанные со страницы.

    Поддерживает разделители табуляция/запятая и кодировки
    UTF-16 (LE/BE, с BOM), UTF-8 (с BOM), CP1251.
    """
    path = Path(csv_path)
    raw = path.read_bytes()

    # Определяем кодировку по BOM, иначе пробуем по очереди
    if raw[:2] == b"\xff\xfe":
        text = raw.decode("utf-16-le")
    elif raw[:2] == b"\xfe\xff":
        text = raw.decode("utf-16-be")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")
    else:
        text = None
        for enc in ("utf-8", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")

    # Убираем BOM (U+FEFF), остающийся при декодировании UTF-16 с BOM,
    # иначе первый заголовок («Номер») будет с невидимым символом в начале.
    if text.startswith("\ufeff"):
        text = text[1:]

    lines = text.splitlines()
    if not lines:
        return []

    # Разделитель: табуляция или запятая (по заголовочной строке)
    header = lines[0]
    delimiter = "\t" if header.count("\t") >= header.count(",") else ","

    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    for r in reader:
        rows.append({
            (k.strip() if isinstance(k, str) else k): (v or "").strip()
            for k, v in r.items() if k is not None
        })
    return rows


# ───── ВЫГРУЗКА ПО СТАТУСУ ─────

def _fill_status_verified(p: Portal, label: str, value: str) -> None:
    """Надёжно заполняет поле статуса с верификацией фактического значения.

    Проблема: ITSM-поле статуса может не очищаться через Ctrl+A+Delete
    (особенно после навигации «Назад»). Используем агрессивную очистку
    и проверяем результат.
    """
    try:
        # Клик по полю для фокуса
        p.click(label, timeout=3)
        time.sleep(0.5)

        # Агрессивная очистка: Ctrl+A → Delete → Backspace ×15
        p.page.keyboard.press("Control+a")
        time.sleep(0.2)
        p.page.keyboard.press("Delete")
        time.sleep(0.2)
        for _ in range(15):
            p.page.keyboard.press("Backspace")
        time.sleep(0.3)
        p._log(f"  поле «{label}» очищено (Ctrl+A + Delete + Backspace×15)")

        # Ввод нового значения
        p.page.keyboard.type(value, delay=50)
        time.sleep(0.3)
        p.page.keyboard.press("Tab")
        time.sleep(0.5)
        p._log(f"  поле «{label}» ← «{value}» (keyboard.type)")

        # Верификация: проверяем .value через JS (HTML-атрибут value не обновляется при вводе)
        verified = False
        try:
            js_check = """(val) => {
                const inputs = document.querySelectorAll('input:not([type=hidden]):not([type=button])');
                for (const inp of inputs) {
                    if (inp.value === val) return true;
                }
                return false;
            }"""
            verified = p.page.evaluate(js_check, value)
        except Exception:
            pass

        if verified:
            p._log(f"  ✓ верифицировано: поле содержит «{value}»")
        else:
            # keyboard.type почти наверняка сработал (реальные клавиши),
            # но пробуем p.fill как fallback с коротким таймаутом
            p._log(f"  ⚠ верификация не прошла (JS не нашёл «{value}»). Пробую p.fill...")
            try:
                p.fill(label, value, timeout=3)
                p._log(f"  ✓ p.fill() выполнен для «{value}»")
            except Exception as e2:
                p._log(f"  ⚠ p.fill не сработал: {e2}. Продолжаю — keyboard.type скорее всего сработал.")

    except RPAError:
        p._log(f"  поле «{label}» не найдено — пробую «Статус инцидента»")
        try:
            p.click(UI_SEARCH["search_status_alt"], timeout=3)
            time.sleep(0.5)
            p.page.keyboard.press("Control+a")
            time.sleep(0.2)
            p.page.keyboard.press("Delete")
            time.sleep(0.2)
            for _ in range(15):
                p.page.keyboard.press("Backspace")
            time.sleep(0.3)
            p.page.keyboard.type(value, delay=50)
            time.sleep(0.3)
            p.page.keyboard.press("Tab")
            time.sleep(0.5)
            p._log(f"  поле «{UI_SEARCH['search_status_alt']}» ← «{value}» (fallback)")
        except RPAError as e:
            p._log(f"  ✗ не удалось заполнить статус (оба варианта): {e}")


def _select_status_reliable(p: Portal, label: str, value: str) -> None:
    """Надёжный выбор статуса в ITSM-комбобоксе.

    Стратегия (по убыванию надёжности):
    1. Нативный <select> → JS select + change event
    2. Кастомный комбобокс (Ext GWT): JS-очистка → клик → JS-поиск точного option → клик
    3. Fallback: p.select() из itsm_rpa
    4. Последний resort: _fill_status_verified (keyboard.type)
    """
    # ── Шаг 1: Нативный <select> через JS ──
    try:
        result = p.page.evaluate("""(args) => {
            const value = args;
            const selects = document.querySelectorAll('select');
            for (const sel of selects) {
                const opts = [...sel.options];
                const exact = opts.find(o => o.text.trim() === value);
                if (exact) {
                    sel.value = exact.value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    sel.dispatchEvent(new Event('input', {bubbles: true}));
                    return 'ok';
                }
            }
            return 'no_match';
        }""", value)
        if result == 'ok':
            p._log(f"  ✓ статус «{value}» установлен (нативный <select>)")
            return
    except Exception:
        pass

    # ── Шаг 2: Кастомный комбобокс — JS-очистка + dropdown + точный клик ──
    p._log(f"  Комбобокс: открываю dropdown для «{value}»...")

    # 2a. Клик по полю для фокуса и открытия dropdown
    try:
        p.click(label, timeout=5)
    except RPAError:
        p._log(f"  ⚠ клик по «{label}» не удался, пробую alt...")
        p.click(UI_SEARCH["search_status_alt"], timeout=5)
    time.sleep(0.8)

    # 2b. JS: очистить поле (Ext GWT combobox — input внутри)
    try:
        p.page.evaluate("""(label) => {
            // Найти input рядом с label
            const labels = document.querySelectorAll('label');
            for (const lbl of labels) {
                if (lbl.textContent.includes(label)) {
                    const forId = lbl.getAttribute('for');
                    let inp = null;
                    if (forId) inp = document.getElementById(forId);
                    if (!inp) {
                        // Ищем input в соседнем div
                        const parent = lbl.parentElement;
                        if (parent) inp = parent.querySelector('input[type=text], input:not([type])');
                    }
                    if (inp && inp.tagName === 'INPUT') {
                        inp.focus();
                        inp.value = '';
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'cleared';
                    }
                }
            }
            // Fallback: ищем все видимые input и очищаем тот, что содержит текущий статус
            const inputs = document.querySelectorAll('input[type=text], input:not([type])');
            for (const inp of inputs) {
                if (inp.value && inp.offsetParent !== null) {
                    inp.focus();
                    inp.value = '';
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    return 'cleared_fallback';
                }
            }
            return 'no_input';
        }""", label)
    except Exception as e:
        p._log(f"  ⚠ JS-очистка: {e}")
    time.sleep(0.5)

    # 2c. Клик ещё раз, чтобы открыть dropdown после очистки
    try:
        p.click(label, timeout=3)
    except RPAError:
        pass
    time.sleep(1.0)

    # 2d. JS: найти точный option в открытом dropdown и кликнуть
    option_clicked = False
    for attempt in range(4):
        try:
            clicked = p.page.evaluate("""(value) => {
                // Ext GWT / custom dropdown: ищем видимые элементы с точным текстом
                const selectors = [
                    'div.x-combo-list-item', 'div.x-list-item',
                    'li[role=option]', 'div[role=option]',
                    'ul[role=listbox] li', 'div[role=listbox] div',
                    'div.x-menu-item', 'div[class*=combo] div[class*=item]',
                    'div[class*=dropdown] div', 'div[class*=list] div[class*=item]',
                    'div.x-selectable div', 'span.x-combo-list-item'
                ];
                let items = [];
                for (const sel of selectors) {
                    items.push(...document.querySelectorAll(sel));
                }
                // Также: все видимые li/div/span с коротким текстом (опции)
                if (items.length === 0) {
                    items = [...document.querySelectorAll('li, div, span')].filter(el => {
                        const t = el.textContent.trim();
                        return t.length > 2 && t.length < 40 && el.offsetParent !== null
                            && el.children.length === 0; // leaf-элемент
                    });
                }
                for (const item of items) {
                    const text = item.textContent.trim();
                    if (text === value && item.offsetParent !== null) {
                        item.click();
                        return 'clicked: ' + text;
                    }
                }
                return 'not_found';
            }""", value)
            if clicked and clicked.startswith('clicked'):
                p._log(f"  ✓ option «{value}» найден и кликнут (попытка {attempt+1})")
                option_clicked = True
                break
            else:
                p._log(f"  ⚠ option не найден (попытка {attempt+1}/4), жду...")
                time.sleep(0.8)
        except Exception as e:
            p._log(f"  ⚠ JS-поиск option: {e}")
            time.sleep(0.8)

    # 2e. Верификация
    time.sleep(1.0)
    try:
        actual = p.read(label, timeout=3)
        if actual and value in actual:
            p._log(f"  ✓ верифицировано: «{actual}»")
            return
        p._log(f"  ⚠ верификация: ожидал «{value}», получил «{actual}»")
    except Exception:
        pass

    # ── Шаг 3: Fallback — p.select() из itsm_rpa ──
    if not option_clicked:
        p._log(f"  Пробую p.select() как fallback...")
        try:
            p.select(label, value)
            time.sleep(0.5)
            actual = p.read(label, timeout=3)
            if actual and value in actual:
                p._log(f"  ✓ p.select() сработал: «{actual}»")
                return
        except RPAError as e:
            p._log(f"  ⚠ p.select(): {e}")

    # ── Шаг 4: Последний resort — _fill_status_verified ──
    p._log(f"  Последний resort: _fill_status_verified...")
    _fill_status_verified(p, label, value)


def search_by_status(p: Portal, group: str, status: str,
                     date_after: str = "", date_before: str = "") -> tuple[list[dict], str]:
    """Выполняет поиск инцидентов в ITSM по группе и статусу.

    Навигация: 01. Управление инцидентами → Поиск инцидентов
    Заполнение: Группа поддержки + Статус (+ опционально даты)

    Возвращает (rows, outcome):
      rows:    список строк таблицы (пустой, если ничего не найдено)
      outcome: "found" | "not_found" | "single_incident" | "error"
    """
    from opsdigest.rpa import validate, verified_fields
    validated = validate({"kind": "incidents", "filters": {"group": group,
                         "created_after": date_after, "created_before": date_before}, "statuses": [status]})
    p._log(f"=== ПОИСК: группа=«{group}», статус=«{status}» ===")
    _live_snap(p)  # кадр: начало поиска по статусу

    # 1. Навигация в форму поиска
    # Проверка: уже ли мы на странице поиска (поле «Группа поддержки» видно)?
    try:
        p.field(UI_SEARCH["search_group"], timeout=2)
        p._log(f"  уже на странице поиска — пропуск навигации по меню")
    except Exception:
        # Не на странице поиска — пытаемся перейти
        # Сначала пробуем «Поиск инцидентов» напрямую:
        # если меню уже раскрыто — клик пройдёт, и мы НЕ сворачиваем его
        try:
            p.click(UI_SEARCH["search_menu"], timeout=3)
            p._log(f"  «Поиск инцидентов» кликнулось напрямую (меню уже раскрыто)")
        except RPAError:
            # «Поиск инцидентов» не видно — нужно раскрыть меню
            p.click(UI_SEARCH["nav_section"])
            time.sleep(2)
            for _attempt in range(3):
                try:
                    p.click(UI_SEARCH["search_menu"], timeout=5)
                    break
                except RPAError:
                    if _attempt < 2:
                        p._log(f"  ⚠ «Поиск инцидентов» не кликнулось (попытка {_attempt+1}/3), жду 3 сек...")
                        time.sleep(3)
                    else:
                        raise
        time.sleep(3)  # дождаться загрузки страницы поиска

    # 2. Заполнение: Группа поддержки
    p._log(f"  группа = «{group}»")
    _fill_search_field(p, UI_SEARCH["search_group"], group)

    # 3. Заполнение: Статус — надёжный выбор из dropdown
    p._log(f"  статус = «{status}»")
    _select_status_reliable(p, UI_SEARCH["search_status"], status)


    # 4. Опционально: даты (если переданы)
    if date_after:
        try:
            _na = parse_date_to_itsm(date_after)
            p._log(f"  создан после = «{_na}»")
            _fill_search_field(p, UI_SEARCH["search_created_after"], _na)
        except ValueError as e:
            p._log(f"  !! дата «{date_after}» не распознана: {e}")

    if date_before:
        try:
            _nb = parse_date_to_itsm(date_before)
            p._log(f"  создан до = «{_nb}»")
            _fill_search_field(p, UI_SEARCH["search_created_before"], _nb)
        except ValueError as e:
            p._log(f"  !! дата «{date_before}» не распознана: {e}")

    # 5. Запуск поиска
    verified_fields(p, validated, status)
    time.sleep(1)
    _trigger_search(p)
    _live_snap(p)  # кадр: поиск запущен, ждём результат

    # Ожидание результатов: опрашиваем до 15 сек, пока таблица не появилась
    rows = None
    _t0 = time.time()
    while time.time() - _t0 < 15:
        time.sleep(2)
        try:
            rows = _read_search_results(p)
            if rows is not None:
                break
        except Exception:
            pass
    if rows is None:
        p._log(f"  ⚠ Таблица результатов не появилась за 15 сек — скриншот и переход дальше")
        try:
            shot = OUTPUT_DIR / f"timeout_{status.replace(' ', '_')}.png"
            OUTPUT_DIR.mkdir(exist_ok=True)
            p.page.screenshot(path=str(shot))
            p._log(f"  скриншот → {shot}")
        except Exception:
            pass

    # 7. Если таблица со строками — инциденты найдены (СПИСОК)
    if rows is not None and len(rows) > 0:
        p._log(f"  ✓ найдено: {len(rows)} инцидентов (список)")
        return rows, "found"

    # 8. Таблицы нет (или пустая) — проверяем сообщение «не найдено»
    if _detect_no_results(p):
        p._log(f"  ⚠ По статусу «{status}» инциденты не найдены. Это нормально, переходим дальше.")
        return [], "not_found"

    # 9. Таблицы нет, «не найдено» нет — возможно, карточка одного инцидента
    if _detect_single_incident_card(p):
        p._log(f"  ⚠ По статусу «{status}» найден только 1 инцидент — открыта карточка. Возврат к списку.")
        return [], "single_incident"

    # 10. Ничего не распознали — отладка
    p._log("  !! не удалось распознать результат поиска")
    errors = _check_validation_errors(p)
    if errors:
        p._log(f"  ошибки валидации: {errors}")
    # Скриншот для отладки
    try:
        shot = OUTPUT_DIR / f"debug_{status.replace(' ', '_')}.png"
        OUTPUT_DIR.mkdir(exist_ok=True)
        p.page.screenshot(path=str(shot))
        p._log(f"  скриншот → {shot}")
    except Exception:
        pass
    if not errors:
        p._log(f"  ⚠ Вероятно, по статусу «{status}» ничего не найдено.")
        return [], "not_found"
    return [], "error"


def run_status_exports(p: Portal, group: str, statuses: list[str],
                       date_after: str = "", date_before: str = "",
                       out_dir: Path | None = None) -> dict[str, list[dict]]:
    """Выполняет поочерёдные выгрузки по каждому статусу.

    Логика для каждого статуса:
      1. Заполнить форму (группа + статус) → поиск
      2. Если найдено (2+) → экспорт в Excel
      3. Если не найдено → лог, переход к следующему
      4. Если 1 (карточка) → «Назад», лог, переход к следующему

    Возвращает dict: {status: [rows]}
    """
    results: dict[str, list[dict]] = {}
    export_dir = str(out_dir) if out_dir else None

    for i, status in enumerate(statuses):
        if i > 0:
            p._log(f"\n--- Переход к следующему поиску ({i + 1}/{len(statuses)}) ---")
            time.sleep(1)
            # Навигация к форме поиска выполняется внутри search_by_status

        rows, outcome = search_by_status(p, group, status, date_after, date_before)
        results[status] = rows

        # Экспорт: только если нашлись инциденты в виде таблицы
        if outcome == "found" and rows:
            p._log(f"  → Экспорт инцидентов по статусу «{status}» ({len(rows)} шт. на странице)")
            try:
                path = export_to_excel(p, downloads_dir=export_dir)
                p._log(f"  ✓ Excel → {path}")
                # ВАЖНО: таблица на странице ограничена 50 строками, а файл
                # экспорта содержит ВСЕ инциденты. Читаем CSV и используем его
                # как источник строк, чтобы не потерять инциденты за пределами 50.
                try:
                    csv_rows = _parse_export_csv(path)
                    if csv_rows:
                        p._log(f"  ✓ из файла экспорта: {len(csv_rows)} инцидентов (полный список)")
                        results[status] = csv_rows
                    else:
                        p._log(f"  ⚠ файл экспорта пуст — оставляю строки со страницы ({len(rows)})")
                except Exception as ce:
                    p._log(f"  ⚠ не удалось прочитать файл экспорта ({ce}) — оставляю строки со страницы")
            except RPAError as e:
                p._log(f"  ✗ Экспорт не удался: {e}")
            # Пауза после экспорта: ITSM обрабатывает скачивание, UI может быть нестабилен
            time.sleep(3)
        elif outcome == "not_found":
            p._log(f"  → Статус «{status}»: ничего не найдено, экспорт не нужен")
        elif outcome == "single_incident":
            p._log(f"  → Статус «{status}»: 1 инцидент (полноэкранная карточка), экспорт пропущен")
        elif outcome == "error":
            p._log(f"  → Статус «{status}»: ошибка при поиске, экспорт пропущен")

    return results


# ───── ВЫГРУЗКА БЕЗ СТАТУСА ─────

def run_no_status_export(p: Portal, group: str,
                         date_after: str = "", date_before: str = "",
                         out_dir: Path | None = None) -> dict[str, list[dict]]:
    """Выполняет поиск инцидентов по группе БЕЗ фильтра по статусу.

    Один поиск: заполняем только группу (+ опционально даты), не трогаем поле статуса.
    Возвращает dict: {"Без фильтра": [rows]}
    """
    from opsdigest.rpa import validate, verified_fields
    validated = validate({"kind": "incidents", "filters": {"group": group,
                         "created_after": date_after, "created_before": date_before}, "statuses": []})
    export_dir = str(out_dir) if out_dir else None
    p._log(f"=== ПОИСК БЕЗ СТАТУСА: группа=«{group}» ===")
    _live_snap(p)

    # 1. Навигация в форму поиска
    try:
        p.field(UI_SEARCH["search_group"], timeout=2)
        p._log(f"  уже на странице поиска")
    except Exception:
        try:
            p.click(UI_SEARCH["search_menu"], timeout=3)
        except RPAError:
            p.click(UI_SEARCH["nav_section"])
            time.sleep(2)
            for _attempt in range(3):
                try:
                    p.click(UI_SEARCH["search_menu"], timeout=5)
                    break
                except RPAError:
                    if _attempt < 2:
                        time.sleep(3)
                    else:
                        raise
        time.sleep(3)

    # 2. Заполнение: Группа поддержки
    p._log(f"  группа = «{group}»")
    _fill_search_field(p, UI_SEARCH["search_group"], group)

    # 3. ОЧИЩАЕМ статус — оставляем поле пустым (все инциденты)
    p._log(f"  статус = (очищено, все инциденты)")
    try:
        p.fill(UI_SEARCH["search_status"], "", timeout=3, commit=True)
    except Exception:
        p._log(f"  (поле статуса не удалось очистить — возможно, уже пустое)")

    # 4. Опционально: даты
    if date_after:
        try:
            _na = parse_date_to_itsm(date_after)
            p._log(f"  создан после = «{_na}»")
            _fill_search_field(p, UI_SEARCH["search_created_after"], _na)
        except ValueError as e:
            p._log(f"  !! дата «{date_after}» не распознана: {e}")
    if date_before:
        try:
            _nb = parse_date_to_itsm(date_before)
            p._log(f"  создан до = «{_nb}»")
            _fill_search_field(p, UI_SEARCH["search_created_before"], _nb)
        except ValueError as e:
            p._log(f"  !! дата «{date_before}» не распознана: {e}")

    # 5. Запуск поиска
    time.sleep(1)
    verified_fields(p, validated, "")
    _trigger_search(p)
    _live_snap(p)

    # Ожидание результатов
    rows = None
    _t0 = time.time()
    while time.time() - _t0 < 15:
        time.sleep(2)
        try:
            rows = _read_search_results(p)
            if rows is not None:
                break
        except Exception:
            pass

    # 6. Обработка результата
    if rows is not None and len(rows) > 0:
        p._log(f"  ✓ найдено: {len(rows)} инцидентов (без фильтра по статусу)")
        # Экспорт в Excel
        if export_dir:
            try:
                path = export_to_excel(p, downloads_dir=export_dir)
                p._log(f"  ✓ Excel → {path}")
                try:
                    csv_rows = _parse_export_csv(path)
                    if csv_rows:
                        p._log(f"  ✓ из файла экспорта: {len(csv_rows)} инцидентов")
                        rows = csv_rows
                except Exception as ce:
                    p._log(f"  ⚠ не удалось прочитать экспорт: {ce}")
            except RPAError as e:
                p._log(f"  ✗ Экспорт не удался: {e}")
            time.sleep(3)
        return {"Без фильтра": rows}

    if _detect_no_results(p):
        p._log(f"  ⚠ По группе «{group}» инциденты не найдены.")
        return {"Без фильтра": []}

    if _detect_single_incident_card(p):
        p._log(f"  ⚠ Найден только 1 инцидент (карточка).")
        return {"Без фильтра": []}

    p._log("  ⚠ Не удалось распознать результат поиска")
    return {"Без фильтра": []}


# ───── ОБЪЕДИНЕНИЕ РЕЗУЛЬТАТОВ ─────

def merge_results(results: dict[str, list[dict]], group: str) -> list[dict]:
    """Объединяет результаты трёх выгрузок в один список.

    Каждая строка получает поле "_status" (из какого статуса) и "_group".
    Дедупликация по номеру инцидента (если один инцидент попал в два статуса —
    оставляем первый).
    """
    merged: list[dict] = []
    seen: set[str] = set()

    for status, rows in results.items():
        for row in rows:
            num = (row.get("Номер") or "").strip()
            if num and num in seen:
                continue  # дубликат
            entry = dict(row)
            entry["_status"] = status
            entry["_group"] = group
            merged.append(entry)
            if num:
                seen.add(num)

    p_log(f"Объединение: {len(merged)} уникальных инцидентов "
          f"(из {sum(len(r) for r in results.values())} строк)")
    return merged


def p_log(msg: str):
    """Логирование вне Portal."""
    print(f"[OPSdigest] {msg}")


# ───── СОХРАНЕНИЕ ─────

def save_output(merged: list[dict], results: dict[str, list[dict]],
                group: str, statuses: list[str], out_dir: Path) -> Path:
    """Сохраняет результаты в JSON.

    Структура:
    {
      "metadata": {group, statuses, timestamp, total},
      "incidents": [merged list],
      "by_status": {status: [rows]}
    }
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"opsdigest_{group}_{ts}.json"

    data = {
        "metadata": {
            "group": group,
            "statuses": statuses,
            "timestamp": datetime.now().isoformat(),
            "total_incidents": len(merged),
            "by_status_count": {s: len(r) for s, r in results.items()},
        },
        "incidents": merged,
        "by_status": results,
    }

    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    p_log(f"Результаты → {out_file}")
    return out_file


# ───── ЭКСПОРТ В EXCEL (опционально, отдельный вызов) ─────

def export_all_to_excel(p: Portal, out_dir: str | None = None) -> str | None:
    """Экспорт текущего результата поиска в Excel (кнопка «Ещё» → «Экспорт в Excel»)."""
    try:
        path = export_to_excel(p, downloads_dir=out_dir)
        p_log(f"Excel → {path}")
        return path
    except RPAError as e:
        p_log(f"Экспорт в Excel не удался: {e}")
        return None


# ───── ИЗВЛЕЧЕНИЕ ЗАДАЧ ИЗ КОММЕНТАРИЕВ ─────

# Паттерны для поиска номеров задач в XTracker в комментариях
TASK_PATTERNS = [
    r'\b([A-Z]{2,10}-\d{1,6})\b',  # PROJ-123
    r'\b(TSK[-_]?\d+)\b',           # TSK-123, TSK123
    r'\b(ЗАДАЧА[- ]?\d+)\b',        # ЗАДАЧА 123
]


def extract_task_refs(text: str) -> list[str]:
    """Извлекает номера задач/проблем из текста комментария."""
    refs = set()
    for pattern in TASK_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            refs.add(match.group(1).upper())
    return sorted(refs)


def extract_all_task_refs(incidents: list[dict]) -> dict[str, list[str]]:
    """Для каждого инцидента извлекает номера задач из полей.

    Возвращает: {incident_number: [task_refs]}
    """
    result: dict[str, list[str]] = {}
    text_fields = ["Комментарий", "Подробное описание", "Техническое решение",
                   "Решение для пользователя", "Краткое описание"]

    for inc in incidents:
        num = (inc.get("Номер") or "").strip()
        if not num:
            continue
        refs = set()
        for field in text_fields:
            text = (inc.get(field) or "")
            refs.update(extract_task_refs(text))
        if refs:
            result[num] = sorted(refs)

    p_log(f"Задачи найдены в комментариях: {len(result)} инцидентов")
    return result


# ───── ГЕНЕРАЦИЯ ОТЧЁТА ─────

def generate_report(merged: list[dict], task_refs: dict[str, list[str]],
                    out_dir: Path) -> Path:
    """Генерирует текстовый отчёт-саммари по выгруженным инцидентам."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = out_dir / f"opsdigest_report_{ts}.txt"

    lines = []
    lines.append("=" * 70)
    lines.append(f"OPSdigest — СВОДКА ПО ИНЦИДЕНТАМ")
    lines.append(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    # По статусам
    status_counts: dict[str, int] = {}
    for inc in merged:
        st = inc.get("_status", "Неизвестно")
        status_counts[st] = status_counts.get(st, 0) + 1

    lines.append(f"ВСЕГО ИНЦИДЕНТОВ: {len(merged)}")
    lines.append("")
    lines.append("По статусам:")
    for st, cnt in sorted(status_counts.items()):
        lines.append(f"  • {st}: {cnt}")
    lines.append("")

    # Инциденты с задачами
    lines.append(f"ИНЦИДЕНТЫ СО СВЯЗАННЫМИ ЗАДАЧАМИ: {len(task_refs)}")
    lines.append("-" * 70)
    for num, refs in sorted(task_refs.items()):
        inc = next((i for i in merged if (i.get("Номер") or "").strip() == num), {})
        desc = (inc.get("Краткое описание") or "")[:80]
        status = inc.get("_status", "?")
        lines.append(f"  {num} [{status}]")
        lines.append(f"    {desc}")
        lines.append(f"    Задачи: {', '.join(refs)}")
        lines.append("")

    # Все инциденты (кратко)
    lines.append("=" * 70)
    lines.append("ПОЛНЫЙ СПИСОК:")
    lines.append("=" * 70)
    for inc in merged:
        num = inc.get("Номер", "?")
        status = inc.get("_status", "?")
        desc = (inc.get("Краткое описание") or "")[:60]
        executor = inc.get("Исполнитель", "")
        lines.append(f"  {num} | {status:15s} | {executor:20s} | {desc}")

    report = "\n".join(lines)
    report_file.write_text(report, encoding="utf-8")
    p_log(f"Отчёт → {report_file}")
    return report_file


# ───── ОБЪЕДИНЕНИЕ ДАТЫ И ВРЕМЕНИ ─────

def _combine_date_time(date_str: str, time_str: str) -> str:
    """Объединяет дату и время в одно значение для ITSM.

    Если time_str пустой — возвращает date_str как есть (parse_date_to_itsm
    добавит 00:00:00 автоматически).
    Если оба заданы — склеивает: '13.11.2026' + '09:30' → '13.11.2026 09:30:00'.
    Если date_str уже содержит время (пробел + цифры) — time_str игнорируется.
    """
    if not date_str:
        if time_str:
            raise ValueError("Время указано без даты")
        return ""
    if not time_str:
        return date_str
    # Если в date_str уже есть время (пробел + цифры) — не дублируем
    if re.search(r"[T\s]+\d{1,2}:\d{2}", date_str):
        raise ValueError("Укажите время либо в дате, либо отдельно")
    # Нормализуем время: '09:30' → '09:30:00'
    t = time_str.strip()
    if t.count(":") == 1:
        t += ":00"
    return f"{date_str.strip()} {t}"


# ───── ПОСТ-ФИЛЬТР ПО ВРЕМЕНИ ─────

def _parse_creation_datetime(row: dict) -> datetime | None:
    """Извлекает дату/время создания из поля «Номер».

    Пример: 'ИНЦ-024201370 01.10.2026 09:45:08' → datetime(2026, 10, 1, 9, 45, 8)
    Берёт ПЕРВОЕ вхождение даты с временем (в некоторых строках две даты).
    """
    num = (row.get("Номер") or "").strip()
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?', num)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h, mi = int(m.group(4)), int(m.group(5))
    sec = int(m.group(6) or 0)
    try:
        return datetime(y, mo, d, h, mi, sec)
    except ValueError:
        return None


def _filter_by_datetime(rows: list[dict], full_after: str, full_before: str) -> list[dict]:
    """Пост-фильтр по дате+времени создания.

    ITSM не поддерживает время в поиске, поэтому фильтруем уже выгруженные данные.

    full_after:  'ДД.ММ.ГГГГ ЧЧ:ММ:СС' — нижняя граница (включительно)
    full_before: 'ДД.ММ.ГГГГ ЧЧ:ММ:СС' — верхняя граница (включительно)
    Пустая граница = без ограничения.
    """
    if not full_after and not full_before:
        return rows

    dt_after = None
    dt_before = None

    if full_after:
        try:
            normalized = parse_date_to_itsm(full_after)
            dt_after = datetime.strptime(normalized, "%d.%m.%Y %H:%M:%S")
        except (ValueError, TypeError):
            pass

    if full_before:
        try:
            normalized = parse_date_to_itsm(full_before)
            dt_before = datetime.strptime(normalized, "%d.%m.%Y %H:%M:%S")
        except (ValueError, TypeError):
            pass

    if not dt_after and not dt_before:
        return rows

    filtered = []
    skipped = 0
    for row in rows:
        dt = _parse_creation_datetime(row)
        if dt is None:
            filtered.append(row)  # не удалось распарсить — оставляем
            continue
        if dt_after and dt < dt_after:
            skipped += 1
            continue
        if dt_before and dt > dt_before:
            skipped += 1
            continue
        filtered.append(row)

    if skipped:
        p_log(f"  ⏰ Пост-фильтр по времени: отфильтровано {skipped} из {len(rows)} "
              f"(окно: {full_after or '…'} → {full_before or '…'})")

    return filtered


# ───── MAIN ─────

def main():
    ap = argparse.ArgumentParser(
        description="OPSdigest: выгрузка инцидентов из ITSM по группе и статусам")
    ap.add_argument("--url", help="URL ITSM-портала")
    ap.add_argument("--cdp", help="CDP endpoint (подключиться к открытому браузеру)")
    ap.add_argument("--channel", default="chrome", help="Браузер: chrome/msedge/none")
    ap.add_argument("--profile", default="rpa_profile", help="Папка профиля")
    ap.add_argument("--group", default=DEFAULT_GROUP,
                    help=f"Группа поддержки (по умолчанию: {DEFAULT_GROUP})")
    ap.add_argument("--statuses", default=",".join(DEFAULT_STATUSES),
                    help=f"Статусы через запятую (по умолчанию: {','.join(DEFAULT_STATUSES)})")
    ap.add_argument("--date-after", default="", help="Создан после (опционально, напр. '13.11.2026')")
    ap.add_argument("--date-before", default="", help="Создан до (опционально, напр. '13.11.2026')")
    ap.add_argument("--time-after", default="", help="Часы:минуты для 'Создан после' (напр. '09:00' или '09:30:00')")
    ap.add_argument("--time-before", default="", help="Часы:минуты для 'Создан до' (напр. '18:30' или '18:30:00')")
    ap.add_argument("--dry-run", action="store_true",
                    help="Только собрать данные, ничего не менять")
    ap.add_argument("--export", action="store_true",
                    help="Дополнительно сделать экспорт в Excel после каждого поиска")
    ap.add_argument("--no-status", action="store_true",
                    help="Поиск без фильтра по статусу (все инциденты по группе)")
    ap.add_argument("--no-wait", action="store_true",
                    help="Не ждать Enter после открытия (если уже залогинены)")
    ap.add_argument("--fresh-profile", action="store_true",
                    help="Создать новый профиль")
    ap.add_argument("--output-dir", default="", help="Папка для выходных файлов")
    a = ap.parse_args()

    statuses = [s.strip() for s in a.statuses.split(",") if s.strip()]
    from opsdigest.rpa import validate
    combined_after = _combine_date_time(a.date_after, a.time_after)
    combined_before = _combine_date_time(a.date_before, a.time_before)
    checked = validate({"kind": "incidents", "filters": {"group": a.group,
                       "created_after": combined_after, "created_before": combined_before},
                       "statuses": [] if a.no_status else statuses})
    a.date_after = checked["filters"].get("created_after", "")
    a.date_before = checked["filters"].get("created_before", "")
    a.time_after = a.time_before = ""
    no_status = a.no_status
    out_dir = Path(a.output_dir) if a.output_dir else OUTPUT_DIR
    global _LIVE_OUT
    _LIVE_OUT = out_dir

    p_log(f"=== OPSdigest EXPORT ===")
    p_log(f"Группа: {a.group}")
    if no_status:
        p_log(f"Режим: без фильтра по статусу (все инциденты по группе)")
    else:
        p_log(f"Статусы: {statuses}")
    p_log(f"Выходная папка: {out_dir}")
    p_log("")

    # Запуск браузера
    profile_dir = a.profile
    if a.fresh_profile and not a.cdp:
        import time as _t
        profile_dir = f"rpa_profile_{_t.strftime('%Y%m%d_%H%M%S')}"
        p_log(f"🆕 Новый профиль: {profile_dir}")

    p = (Portal.connect(a.cdp, url_contains=a.url) if a.cdp
         else Portal.launch(a.url, channel=None if a.channel == "none" else a.channel,
                            profile_dir=profile_dir))

    _live_snap(p)  # первый кадр: страница загрузки

    # Проверка: не улетели ли на страницу логина (сессия протухла)
    try:
        cur_url = p.page.url.lower()
        if any(kw in cur_url for kw in ("sign", "login", "auth", "sso", "cas/", "oauth")):
            p_log(f"⚠ Сессия ITSM протухла — браузер на странице: {p.page.url}")
            p_log("  Войдите в ITSM вручную в открывшемся окне Chrome, затем повторите выгрузку.")
            p_log("  (или запустите export_incidents.bat для ручного входа)")
            sys.exit(2)
    except Exception:
        pass  # если URL ещё не готов — не критично

    with p:
        if not a.cdp and not a.no_wait:
            input("Если нужно — войдите в портал в открывшемся окне и нажмите Enter...")

        # ITSM не поддерживает время в поиске — передаём только дату
        date_after = a.date_after.strip() if a.date_after else ""
        date_before = a.date_before.strip() if a.date_before else ""

        # Выгрузка: по статусам или без фильтра по статусу
        _live_snap(p)  # кадр перед выгрузкой
        if no_status:
            results = run_no_status_export(p, a.group, date_after, date_before,
                                           out_dir=out_dir)
        else:
            results = run_status_exports(p, a.group, statuses, date_after, date_before,
                                         out_dir=out_dir)

        # Объединение
        merged = merge_results(results, a.group)

        # Пост-фильтр по времени (ITSM не поддерживает время в поиске)
        if a.time_after or a.time_before:
            full_after = _combine_date_time(a.date_after, a.time_after)
            full_before = _combine_date_time(a.date_before, a.time_before)
            merged = _filter_by_datetime(merged, full_after, full_before)

        # Извлечение задач из комментариев
        task_refs = extract_all_task_refs(merged)

        # Сохранение
        out_file = save_output(merged, results, a.group, statuses, out_dir)

        # Генерация отчёта
        report_file = generate_report(merged, task_refs, out_dir)

        # Итог
        p_log("")
        p_log(f"=== ГОТОВО ===")
        p_log(f"Инцидентов: {len(merged)}")
        p_log(f"Со ссылками на задачи: {len(task_refs)}")
        p_log(f"JSON: {out_file}")
        p_log(f"Отчёт: {report_file}")

        time.sleep(1)


if __name__ == "__main__":
    main()
