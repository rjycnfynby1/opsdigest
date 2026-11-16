# -*- coding: utf-8 -*-
"""Применяет 3 фикса к example_scenario.py → example_scenario_fixed.py"""
import re
from pathlib import Path

src = Path(__file__).parent / "example_scenario.py"
dst = Path(__file__).parent / "example_scenario_fixed.py"

lines = src.read_text(encoding="utf-8").split("\n")
print(f"Оригинал: {len(lines)} строк")

# ============================================================
# FIX 1: Вставить _scroll_and_collect_fields перед _read_view_fields
# и заменить логику сбора полей на скроллинг
# ============================================================

scroll_func = '''
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
            text = text.replace(/\\s+/g, ' ').trim();
            if (!text) { const it = (el.innerText||'').replace(/\\s+/g,' ').trim(); if (it && it.length<=100) text = it; }
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

'''

new_read_fields_body = '''    # Кнопки/метки, которые НЕ являются полями представления
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
'''

# Найти строку def _read_view_fields
rvf_idx = None
for i, line in enumerate(lines):
    if 'def _read_view_fields' in line:
        rvf_idx = i
        break
assert rvf_idx is not None, "Не найден _read_view_fields"

# Строки 633-677 (1-indexed) = 632-676 (0-indexed) — тело сбора полей
# Начинается с "    # Кнопки/метки" и заканчивается перед "    p._log(f\"найдено полей"
body_start = None
body_end = None
for i in range(rvf_idx, len(lines)):
    if 'Кнопки/метки, которые НЕ являются полями' in lines[i]:
        body_start = i
        break
for i in range(body_start, len(lines)):
    if 'p._log(f"найдено полей' in lines[i]:
        body_end = i
        break
assert body_start is not None and body_end is not None, f"Не найдено тело: start={body_start} end={body_end}"

# Вставляем scroll_func перед _read_view_fields, заменяем тело
new_lines = lines[:rvf_idx]
new_lines.append(scroll_func)
new_lines.append(lines[rvf_idx])  # def _read_view_fields...
new_lines.extend(lines[rvf_idx+1:body_start])  # docstring, scout, screenshot, expand
new_lines.extend(new_read_fields_body.split('\n'))
new_lines.extend(lines[body_end:])

print(f"FIX 1: вставлен _scroll_and_collect_fields, заменён сбор полей (строки {body_start+1}-{body_end})")

# ============================================================
# FIX 2: Заменить _cancel_view_settings на robust-версию
# ============================================================

cancel_start = None
cancel_end = None
for i, line in enumerate(new_lines):
    if 'def _cancel_view_settings' in line:
        cancel_start = i
        break
assert cancel_start is not None, "Не найден _cancel_view_settings"

for i in range(cancel_start + 1, len(new_lines)):
    if new_lines[i].startswith('def ') or new_lines[i].startswith('class '):
        cancel_end = i
        break
if cancel_end is None:
    cancel_end = len(new_lines)

new_cancel = '''def _collapse_expanded_table(p: Portal):
    """Сворачивает расширенную таблицу (toggle «Увеличить таблицу»)."""
    try:
        p.click("Увеличить таблицу", timeout=3)
        p._log("свёрнута расширенная таблица")
        time.sleep(1.0)
    except RPAError:
        try:
            p.click("Свернуть таблицу", timeout=3)
            p._log("свёрнута таблица (alt)")
            time.sleep(1.0)
        except RPAError:
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
    for x_text in ["\\u2715", "\\u00d7", "Закрыть", "X", "Close"]:
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

'''

new_lines = new_lines[:cancel_start] + new_cancel.split('\n') + new_lines[cancel_end:]
print(f"FIX 2: _cancel_view_settings заменён (добавлены _collapse_expanded_table, _force_close_dialog)")

# ============================================================
# FIX 3: Сворачивание таблицы перед добавлением полей
# ============================================================
for i, line in enumerate(new_lines):
    if '# 4. Добавляем недостающие поля' in line:
        new_lines.insert(i, '        # FIX 3: сворачиваем таблицу чтобы левая панель была видна')
        new_lines.insert(i + 1, '        _collapse_expanded_table(p)')
        print(f"FIX 3: добавлен _collapse_expanded_table перед добавлением полей (строка {i+1})")
        break

# Сохранить
result = '\n'.join(new_lines)
dst.write_text(result, encoding="utf-8")
print(f"\n✅ Готово: {dst.name} ({len(result)} байт, {len(new_lines)} строк)")
print(f"   Путь: {dst}")
