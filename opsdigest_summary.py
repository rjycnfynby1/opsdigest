# -*- coding: utf-8 -*-
"""
OPSdigest: постобработка выгруженных инцидентов.

Читает JSON из opsdigest_export.py и генерирует итоговое саммари:
1. Статусы инцидентов по группам
2. Связанные задачи в XTracker (статус, исполнитель, что ожидается)
3. Рекомендации: что можно закрыть, что актуально
4. Версии приложений (если ниже последней → предложить закрыть)

Запуск:
  python opsdigest_summary.py --input opsdigest_output/opsdigest_А_20261110_103000.json
  python opsdigest_summary.py --input opsdigest_output/opsdigest_А_20261110_103000.json --xtracker
  python opsdigest_summary.py --input opsdigest_output/opsdigest_А_20261110_103000.json --email

Примечание: XTracker и почта доступны через Maverick-плагины.
Этот скрипт генерирует структуру запросов и саммари; фактические вызовы
XTracker/Exchange выполняются в Maverick (через xtracker и exchange-mail плагины).
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


# ───── ЗАГРУЗКА ДАННЫХ ─────

def load_incidents(input_file: str) -> dict:
    """Загружает JSON из opsdigest_export.py."""
    path = Path(input_file)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"[Summary] Загружено: {data['metadata']['total_incidents']} инцидентов")
    return data


# ───── АНАЛИЗ СТАТУСОВ ─────

def analyze_status(incidents: list[dict]) -> dict[str, Any]:
    """Анализирует распределение по статусам и приоритетам."""
    by_status: dict[str, list[dict]] = {}
    by_priority: dict[str, int] = {}

    for inc in incidents:
        st = inc.get("_status", "Неизвестно")
        by_status.setdefault(st, []).append(inc)
        pr = (inc.get("Приоритет") or "Не указан").strip()
        by_priority[pr] = by_priority.get(pr, 0) + 1

    return {
        "total": len(incidents),
        "by_status": {k: len(v) for k, v in by_status.items()},
        "by_priority": by_priority,
        "incidents_by_status": by_status,
    }


# ───── ИЗВЛЕЧЕНИЕ ЗАДАЧ ─────

TASK_PATTERNS = [
    r'\b([A-Z]{2,10}-\d{1,6})\b',  # PROJ-123, OPS-456
    r'\b(TSK[-_]?\d+)\b',           # TSK-123
    r'\b(ПРОБЛ[ЕЁ]МА[- ]?\d+)\b',   # ПРОБЛЕМА 123
]


def extract_task_refs_from_text(text: str) -> list[str]:
    """Извлекает номера задач/проблем из текста."""
    refs = set()
    for pattern in TASK_PATTERNS:
        for match in re.finditer(pattern, text or "", re.IGNORECASE):
            refs.add(match.group(1).upper())
    return sorted(refs)


def build_task_lookup(incidents: list[dict]) -> dict[str, dict]:
    """Строит lookup: task_ref → {incidents: [], context: str}.

    Для каждого найденного номера задачи собирает:
    - Какие инциденты на неё ссылаются
    - Контекст (фрагмент текста вокруг номера)
    """
    lookup: dict[str, dict] = {}
    text_fields = ["Комментарий", "Подробное описание", "Техническое решение",
                   "Решение для пользователя", "Краткое описание"]

    for inc in incidents:
        num = (inc.get("Номер") or "").strip()
        for field in text_fields:
            text = (inc.get(field) or "")
            refs = extract_task_refs_from_text(text)
            for ref in refs:
                if ref not in lookup:
                    lookup[ref] = {"incidents": [], "context": []}
                lookup[ref]["incidents"].append(num)
                # Контекст: 50 символов вокруг совпадения
                for match in re.finditer(re.escape(ref), text, re.IGNORECASE):
                    start = max(0, match.start() - 50)
                    end = min(len(text), match.end() + 50)
                    context = text[start:end].strip()
                    if context not in lookup[ref]["context"]:
                        lookup[ref]["context"].append(context)

    print(f"[Summary] Найдено задач/проблем: {len(lookup)}")
    return lookup


# ───── ГЕНЕРАЦИЯ ЗАПРОСОВ К XTRACKER ─────

def generate_xtracker_queries(task_lookup: dict[str, dict]) -> list[dict]:
    """Генерирует список запросов к XTracker для каждого найденного task_ref.

    Формат:
    [
      {
        "issue_key": "PROJ-123",
        "query": "get_issue",
        "purpose": "Получить статус, исполнителя, описание",
        "related_incidents": ["IM10021", "IM10022"]
      },
      ...
    ]
    """
    queries = []
    for ref, info in task_lookup.items():
        # Только те, что выглядят как XTracker issue (PREFIX-NUMBER)
        if re.match(r'^[A-Z]{2,10}-\d{1,6}$', ref):
            queries.append({
                "issue_key": ref,
                "query": "get_issue",
                "purpose": "Статус, исполнитель, описание, что ожидается",
                "related_incidents": info["incidents"],
            })
    return queries


# ───── ГЕНЕРАЦИЯ ЗАПРОСОВ К ПОЧТЕ ─────

def generate_email_queries(incidents: list[dict], task_lookup: dict[str, dict]) -> list[dict]:
    """Генерирует запросы к Exchange для поиска по описаниям и номерам."""
    queries = []

    # По номерам инцидентов
    for inc in incidents[:20]:  # лимит 20 для неспам
        num = (inc.get("Номер") or "").strip()
        if num:
            queries.append({
                "search": num,
                "purpose": f"Письма по инциденту {num}",
                "type": "incident_number",
            })

    # По номерам задач
    for ref in list(task_lookup.keys())[:20]:
        if re.match(r'^[A-Z]{2,10}-\d{1,6}$', ref):
            queries.append({
                "search": ref,
                "purpose": f"Письма по задаче {ref}",
                "type": "task_number",
            })

    # По ключевым словам из описаний
    for inc in incidents[:10]:
        desc = (inc.get("Краткое описание") or "")[:50]
        if desc:
            queries.append({
                "search": desc,
                "purpose": f"Письма по описанию: {desc}",
                "type": "description",
            })

    return queries


# ───── ГЕНЕРАЦИЯ ЗАПРОСОВ К TELEGRAM/ВСТРЕЧАМ ─────

def generate_telegram_meeting_queries(incidents: list[dict],
                                       task_lookup: dict[str, dict]) -> list[dict]:
    """Генерирует запросы для поиска в Телеграме и на встречах (ScribeAI)."""
    queries = []

    # По описаниям проблем (для встреч)
    for inc in incidents[:15]:
        desc = (inc.get("Краткое описание") or "").strip()
        if desc and len(desc) > 10:
            queries.append({
                "source": "meetings",
                "search": desc[:80],
                "purpose": f"Упоминание проблемы в встречах: {desc[:50]}",
                "incident": inc.get("Номер", ""),
            })

    # По номерам (для Телеграма и встреч)
    for inc in incidents[:20]:
        num = (inc.get("Номер") or "").strip()
        if num:
            queries.append({
                "source": "telegram",
                "search": num,
                "purpose": f"Упоминание {num} в Телеграме",
                "incident": num,
            })

    # По номерам задач
    for ref in list(task_lookup.keys())[:15]:
        if re.match(r'^[A-Z]{2,10}-\d{1,6}$', ref):
            queries.append({
                "source": "telegram",
                "search": ref,
                "purpose": f"Упоминание {ref} в Телеграме",
                "incident": ", ".join(task_lookup[ref]["incidents"][:3]),
            })

    return queries


# ───── ГЕНЕРАЦИЯ ИТОГОВОГО САММАРИ ─────

def generate_summary(data: dict, xtracker_data: dict | None = None,
                     email_data: list | None = None,
                     telegram_data: list | None = None) -> str:
    """Генерирует итоговое текстовое саммари.

    xtracker_data: {issue_key: {status, assignee, summary, ...}} (опционально)
    email_data: список найденных писем (опционально)
    telegram_data: список найденных сообщений (опционально)
    """
    incidents = data["incidents"]
    meta = data["metadata"]
    status_analysis = analyze_status(incidents)
    task_lookup = build_task_lookup(incidents)

    lines = []
    lines.append("=" * 70)
    lines.append("OPSdigest — ИТОГОВОЕ САММАРИ")
    lines.append(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Группа: {meta['group']}")
    lines.append("=" * 70)
    lines.append("")

    # 1. Общая картина
    lines.append("1. ОБЩАЯ КАРТИНА")
    lines.append("-" * 70)
    lines.append(f"Всего инцидентов: {status_analysis['total']}")
    lines.append("")
    lines.append("По статусам:")
    for st, cnt in status_analysis["by_status"].items():
        lines.append(f"  • {st}: {cnt}")
    lines.append("")
    if status_analysis["by_priority"]:
        lines.append("По приоритетам:")
        for pr, cnt in sorted(status_analysis["by_priority"].items()):
            lines.append(f"  • {pr}: {cnt}")
        lines.append("")

    # 2. Связанные задачи
    lines.append("2. СВЯЗАННЫЕ ЗАДАЧИ (XTracker)")
    lines.append("-" * 70)
    if task_lookup:
        for ref, info in sorted(task_lookup.items()):
            if not re.match(r'^[A-Z]{2,10}-\d{1,6}$', ref):
                continue
            lines.append(f"  {ref}:")
            lines.append(f"    Связанные инциденты: {', '.join(info['incidents'][:5])}")
            if xtracker_data and ref in xtracker_data:
                xt = xtracker_data[ref]
                lines.append(f"    Статус: {xt.get('status', '?')}")
                lines.append(f"    Исполнитель: {xt.get('assignee', '?')}")
                lines.append(f"    Описание: {xt.get('summary', '?')[:80]}")
            else:
                lines.append(f"    Статус: (запрос к XTracker не выполнен)")
            lines.append("")
    else:
        lines.append("  Связанные задачи не найдены.")
        lines.append("")

    # 3. Рекомендации
    lines.append("3. РЕКОМЕНДАЦИИ")
    lines.append("-" * 70)
    recommendations = generate_recommendations(incidents, task_lookup, xtracker_data)
    for rec in recommendations:
        lines.append(f"  • {rec}")
    lines.append("")

    # 4. Поиск в почте/Телеграме/встречах
    lines.append("4. ПОИСК В СООБЩЕНИЯХ")
    lines.append("-" * 70)
    if email_data:
        lines.append(f"  Найдено писем: {len(email_data)}")
        for email in email_data[:5]:
            lines.append(f"    • {email.get('subject', '?')} ({email.get('date', '?')})")
    else:
        lines.append("  Поиск в почте не выполнен.")
    if telegram_data:
        lines.append(f"  Найдено сообщений: {len(telegram_data)}")
    else:
        lines.append("  Поиск в Телеграме не выполнен.")
    lines.append("")

    # 5. Что можно закрыть
    lines.append("5. КАНДИДАТЫ НА ЗАКРЫТИЕ")
    lines.append("-" * 70)
    close_candidates = find_close_candidates(incidents, task_lookup, xtracker_data)
    if close_candidates:
        for cc in close_candidates:
            lines.append(f"  {cc['number']} [{cc['status']}]: {cc['reason']}")
    else:
        lines.append("  Нет явных кандидатов на закрытие.")
    lines.append("")

    lines.append("=" * 70)
    lines.append("КОНЕЦ САММАРИ")
    lines.append("=" * 70)

    return "\n".join(lines)


def generate_recommendations(incidents: list[dict],
                             task_lookup: dict[str, dict],
                             xtracker_data: dict | None = None) -> list[str]:
    """Генерирует рекомендации на основе данных."""
    recs = []

    for inc in incidents:
        num = (inc.get("Номер") or "").strip()
        status = inc.get("_status", "")

        # Инциденты в ожидании давно
        if status == "Ожидание":
            recs.append(f"{num}: ожидание — проверить, актуально ли ожидание")

        # Проверить связанные задачи
        for field in ["Комментарий", "Подробное описание"]:
            text = (inc.get(field) or "")
            refs = extract_task_refs_from_text(text)
            for ref in refs:
                if xtracker_data and ref in xtracker_data:
                    xt_status = (xtracker_data[ref].get("status") or "").lower()
                    if "закрыт" in xt_status or "done" in xt_status or "closed" in xt_status:
                        recs.append(f"{num}: задача {ref} закрыта — можно закрыть инцидент")
                    elif "в работе" in xt_status or "in progress" in xt_status:
                        recs.append(f"{num}: задача {ref} в работе — ждать")

    return recs


def find_close_candidates(incidents: list[dict],
                          task_lookup: dict[str, dict],
                          xtracker_data: dict | None = None) -> list[dict]:
    """Находит инциденты, которые можно предложить к закрытию."""
    candidates = []

    for inc in incidents:
        num = (inc.get("Номер") or "").strip()
        status = inc.get("_status", "")
        reasons = []

        # Если связанные задачи закрыты
        for field in ["Комментарий", "Подробное описание"]:
            text = (inc.get(field) or "")
            refs = extract_task_refs_from_text(text)
            for ref in refs:
                if xtracker_data and ref in xtracker_data:
                    xt_status = (xtracker_data[ref].get("status") or "").lower()
                    if "закрыт" in xt_status or "done" in xt_status or "closed" in xt_status:
                        reasons.append(f"задача {ref} закрыта")

        # Если статус "Ожидание" и нет активных задач
        if status == "Ожидание" and not task_lookup.get(num):
            reasons.append("ожидание без активных задач")

        if reasons:
            candidates.append({
                "number": num,
                "status": status,
                "reason": "; ".join(reasons),
            })

    return candidates


# ───── MAIN ─────

def main():
    ap = argparse.ArgumentParser(
        description="OPSdigest: постобработка и саммари по выгруженным инцидентам")
    ap.add_argument("--input", required=True,
                    help="JSON файл из opsdigest_export.py")
    ap.add_argument("--xtracker-data", default="",
                    help="JSON с данными XTracker (опционально)")
    ap.add_argument("--email-data", default="",
                    help="JSON с данными почты (опционально)")
    ap.add_argument("--output-dir", default="",
                    help="Папка для выходных файлов")
    a = ap.parse_args()

    # Загрузка
    data = load_incidents(a.input)
    out_dir = Path(a.output_dir) if a.output_dir else Path(a.input).parent

    # Загрузка дополнительных данных (опционально)
    xtracker_data = None
    if a.xtracker_data and Path(a.xtracker_data).exists():
        xtracker_data = json.loads(Path(a.xtracker_data).read_text(encoding="utf-8"))

    email_data = None
    if a.email_data and Path(a.email_data).exists():
        email_data = json.loads(Path(a.email_data).read_text(encoding="utf-8"))

    # Генерация саммари
    summary = generate_summary(data, xtracker_data, email_data)

    # Сохранение
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_file = out_dir / f"opsdigest_summary_{ts}.txt"
    summary_file.write_text(summary, encoding="utf-8")
    print(f"[Summary] Саммари → {summary_file}")

    # Генерация запросов (для Maverick)
    incidents = data["incidents"]
    task_lookup = build_task_lookup(incidents)

    xtracker_queries = generate_xtracker_queries(task_lookup)
    email_queries = generate_email_queries(incidents, task_lookup)
    telegram_queries = generate_telegram_meeting_queries(incidents, task_lookup)

    queries_file = out_dir / f"opsdigest_queries_{ts}.json"
    queries_file.write_text(json.dumps({
        "xtracker": xtracker_queries,
        "email": email_queries,
        "telegram_meetings": telegram_queries,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Summary] Запросы → {queries_file}")

    # Вывод в консоль
    print("\n" + summary)


if __name__ == "__main__":
    main()
