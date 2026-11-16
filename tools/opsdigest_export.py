# -*- coding: utf-8 -*-
"""
OpsDigest: пример выгрузки инцидентов из ITSM по группе поддержки и статусам.
Выполняет три поиска в ITSM (поочерёдно по статусам) и объединяет результаты.

Запуск:
  python opsdigest_export.py --url https://itsm.example.com/ --channel chrome --group "Service Desk"
  python opsdigest_export.py --url https://itsm.example.com/ --channel chrome --group "Service Desk" --dry-run
  python opsdigest_export.py --url https://itsm.example.com/ --channel chrome --group "Service Desk" --export
  python opsdigest_export.py --cdp http://localhost:9222 --group "А"

Статусы по умолчанию: В работе, Назначен, В ожидании.
Можно переопределить: --statuses "В работе,Назначен"
"""
from __future__ import annotations

import argparse
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

# ───── КОНФИГУРАЦИЯ ─────

# Статусы для выгрузки (три отдельных поиска)
DEFAULT_STATUSES = ["В работе", "Назначен", "В ожидании"]

# Группа поддержки по умолчанию
DEFAULT_GROUP = "А"

# Выходная папка
OUTPUT_DIR = Path(__file__).parent / "opsdigest_output"

# UI-элементы для поиска по статусу (добавлены к базовому UI)
UI_SEARCH = {
    **UI,
    # Поле статуса в форме поиска (ITSM Service Manager)
    "search_status": "Статус",
    "search_status_alt": "Статус инцидента",
}


# ───── ВЫГРУЗКА ПО СТАТУСУ ─────

def search_by_status(p: Portal, group: str, status: str,
                     date_after: str = "", date_before: str = "") -> list[dict]:
    """Выполняет поиск инцидентов в ITSM по группе и статусу.

    Навигация: 01. Управление инцидентами → Поиск инцидентов
    Заполнение: Группа поддержки + Статус (+ опционально даты)
    Возвращает список строк (dict'ы с колонками таблицы).
    """
    p._log(f"=== ПОИСК: группа=«{group}», статус=«{status}» ===")

    # 1. Навигация в форму поиска
    p.click(UI_SEARCH["nav_section"])
    time.sleep(1)
    p.click(UI_SEARCH["search_menu"])
    time.sleep(3)  # дождаться загрузки страницы поиска

    # 2. Заполнение: Группа поддержки
    p._log(f"  группа = «{group}»")
    _fill_search_field(p, UI_SEARCH["search_group"], group)

    # 3. Заполнение: Статус
    p._log(f"  статус = «{status}»")
    try:
        mode = _fill_search_field(p, UI_SEARCH["search_status"], status)
        p._log(f"    (способ: {mode})")
    except RPAError:
        # Fallback: альтернативное имя поля
        p._log(f"  поле «{UI_SEARCH['search_status']}» не найдено — пробую «{UI_SEARCH['search_status_alt']}»")
        mode = _fill_search_field(p, UI_SEARCH["search_status_alt"], status)
        p._log(f"    (способ: {mode})")

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
    time.sleep(1)
    _trigger_search(p)
    time.sleep(4)  # ожидание результатов

    # 6. Чтение результатов
    rows = _read_search_results(p)

    if rows is None:
        p._log("  !! таблица результатов не найдена")
        errors = _check_validation_errors(p)
        if errors:
            p._log(f"  ошибки: {errors}")
        # Скриншот для отладки
        try:
            shot = OUTPUT_DIR / f"debug_{status.replace(' ', '_')}.png"
            OUTPUT_DIR.mkdir(exist_ok=True)
            p.page.screenshot(path=str(shot))
            p._log(f"  скриншот → {shot}")
        except Exception:
            pass
        return []

    p._log(f"  найдено: {len(rows)} инцидентов")
    return rows


def run_status_exports(p: Portal, group: str, statuses: list[str],
                       date_after: str = "", date_before: str = "") -> dict[str, list[dict]]:
    """Выполняет поочерёдные выгрузки по каждому статусу.

    Возвращает dict: {status: [rows]}
    """
    results: dict[str, list[dict]] = {}

    for i, status in enumerate(statuses):
        if i > 0:
            # Между поисками — возврат к форме (очистка полей)
            p._log(f"\n--- Переход к следующему поиску ({i+1}/{len(statuses)}) ---")
            time.sleep(1)
            # Переходим обратно в форму поиска (очистка предыдущих значений)
            try:
                p.click(UI_SEARCH["nav_section"])
                time.sleep(0.5)
                p.click(UI_SEARCH["search_menu"])
                time.sleep(2)
            except RPAError:
                p._log("  не удалось вернуться к форме — продолжаю")

        rows = search_by_status(p, group, status, date_after, date_before)
        results[status] = rows

    return results


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


# ───── ЭКСПОРТ В EXCEL (опционально) ─────

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
    ap.add_argument("--date-after", default="", help="Создан после (опционально)")
    ap.add_argument("--date-before", default="", help="Создан до (опционально)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Только собрать данные, ничего не менять")
    ap.add_argument("--export", action="store_true",
                    help="Дополнительно сделать экспорт в Excel после каждого поиска")
    ap.add_argument("--no-wait", action="store_true",
                    help="Не ждать Enter после открытия (если уже залогинены)")
    ap.add_argument("--fresh-profile", action="store_true",
                    help="Создать новый профиль")
    ap.add_argument("--output-dir", default="", help="Папка для выходных файлов")
    a = ap.parse_args()

    statuses = [s.strip() for s in a.statuses.split(",") if s.strip()]
    out_dir = Path(a.output_dir) if a.output_dir else OUTPUT_DIR

    p_log(f"=== OPSdigest EXPORT ===")
    p_log(f"Группа: {a.group}")
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

    with p:
        if not a.cdp and not a.no_wait:
            input("Если нужно — войдите в портал в открывшемся окне и нажмите Enter...")

        # Выгрузка по статусам
        results = run_status_exports(p, a.group, statuses, a.date_after, a.date_before)

        # Экспорт в Excel (опционально)
        if a.export:
            export_all_to_excel(p, out_dir=str(out_dir))

        # Объединение
        merged = merge_results(results, a.group)

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
