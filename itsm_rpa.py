# -*- coding: utf-8 -*-
"""
itsm_rpa — автоматизация веб-порталов (ITSM / Service Desk) с вложенными iframe
и динамическими id, которые меняются при каждой загрузке.

Идея: искать элементы так, как их ищет человек — по видимому тексту, по подписи
рядом с полем и по расположению на экране. Никаких id, XPath и классов.

Как ищется элемент (по порядку, пока что-то не сработает):
  1. Текст / подсказка (title) / aria-label / placeholder — во ВСЕХ фреймах
     страницы любой вложенности, включая кросс-доменные. Скрытые фреймы и
     невидимые элементы пропускаются.
  2. Поле ввода — по подписи: ближайшее поле справа от подписи или под ней
     (считается по координатам на экране, разметка не важна).
  3. Нечёткое совпадение: ё/е, регистр, двоеточия, латинские «двойники»
     кириллицы (OK/ОК), мелкие опечатки. Для номеров (IM10021) — только точное.
  4. OCR по скриншоту (Tesseract), если текста нет в DOM: canvas, картинки.

Быстрый старт:
    from itsm_rpa import Portal
    with Portal.launch("https://itsm.company.ru", channel="msedge") as p:
        p.click("Очередь инцидентов")
        rows = p.read_grid(["Номер", "Статус", "Срок решения"])
        p.click(rows[0]["Номер"])
        p.click("Принять в работу")
        p.wait_value("Статус", "В работе")

Разведка (что видит скрипт на текущем экране):
    python itsm_rpa.py scout --url https://itsm.company.ru --channel msedge
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import difflib
import io
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Locator, Page, sync_playwright

# Исправление UnicodeEncodeError: stdout на Windows может быть cp1251
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

__all__ = ["Portal", "RPAError", "norm", "similarity", "ocr_locate", "desktop_click_ocr"]


# ─────────────────────────── нормализация и сравнение текста ───────────────────────────

# латинские буквы, которые выглядят как кириллические (после lower())
_HOMOGLYPHS = str.maketrans("aceopxykmthb", "асеорхукмтнв")


def norm(s: str | None) -> str:
    """Приводит текст к виду для сравнения: регистр, ё→е, пробелы, хвостовые ':' и '*', OK→ОК."""
    s = (s or "").replace("\u00a0", " ").replace("ё", "е").replace("Ё", "Е")
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"[\s:*]+$", "", s)
    return s.translate(_HOMOGLYPHS)


def similarity(target: str, cand: str, fuzzy: float = 0.82) -> float:
    """Оценка 0..1: насколько текст элемента cand совпадает с искомым target."""
    t, c = norm(target), norm(cand)
    if not t or not c:
        return 0.0
    if c == t:
        return 1.0
    i = c.find(t)
    if i >= 0:
        before = c[i - 1] if i > 0 else " "
        after = c[i + len(t)] if i + len(t) < len(c) else " "
        if not before.isalnum() and not after.isalnum() and len(c) <= len(t) * 1.6 + 4:
            return 0.86 if i == 0 else 0.8
    if any(ch.isdigit() for ch in t):  # номера заявок сравниваем только точно
        return 0.0
    r = difflib.SequenceMatcher(None, t, c).ratio()
    return round(r * 0.8, 3) if r >= fuzzy else 0.0


class RPAError(RuntimeError):
    pass


# ─────────────────────────── JavaScript, выполняемый внутри каждого фрейма ───────────────────────────

_JS_COMMON = r"""
const FIELD_SEL = 'input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset]):not([type=image]):not([type=checkbox]):not([type=radio]), textarea, select, [contenteditable=""], [contenteditable="true"], [role=textbox], [role=combobox], [role=searchbox]';
const vis = (el) => {
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  const cs = getComputedStyle(el);
  if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) return null;
  return r;
};
const ownText = (el) => {
  let s = '';
  for (const n of el.childNodes) if (n.nodeType === 3) s += n.nodeValue;
  return s.replace(/\s+/g, ' ').trim();
};
const fieldValue = (el) => {
  if (el.tagName === 'SELECT') return el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : '';
  if ('value' in el && el.tagName !== 'BUTTON' && el.tagName !== 'LI') return el.value;
  return (el.innerText || '').trim();
};
"""

JS_COLLECT = "(scan) => {" + _JS_COMMON + r"""
  const CLICK_TAGS = new Set(['BUTTON', 'A', 'SUMMARY', 'OPTION', 'LABEL']);
  const CLICK_ROLES = new Set(['button', 'link', 'tab', 'menuitem', 'option', 'treeitem', 'row', 'gridcell', 'checkbox', 'radio', 'switch']);
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'IFRAME', 'FRAME', 'HTML', 'HEAD', 'META', 'LINK', 'BR']);
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll('body, body *')) {
    if (SKIP.has(el.tagName)) continue;
    const r = vis(el); if (!r) continue;
    const role = el.getAttribute('role') || '';
    const cs = getComputedStyle(el);
    const isBtnInput = el.tagName === 'INPUT' && /^(button|submit|reset|image)$/.test(el.type);
    const click = CLICK_TAGS.has(el.tagName) || CLICK_ROLES.has(role) || typeof el.onclick === 'function' || cs.cursor === 'pointer' || isBtnInput;
    const isField = el.matches(FIELD_SEL);
    const texts = [];
    const own = ownText(el);
    if (own) texts.push(['own', own]);
    for (const a of ['title', 'aria-label', 'placeholder', 'alt']) {
      const v = el.getAttribute(a); if (v && v.trim()) texts.push([a, v.trim()]);
    }
    if (isBtnInput && el.value) texts.push(['value', el.value]);
    if (!own && click && !isField) {
      const it = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (it && it.length <= 100) texts.push(['inner', it]);
    }
    if (!texts.length && !isField) continue;
    const k = scan + '_' + (i++);
    el.setAttribute('data-rpa', k);
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let top = false;
    if (cx >= 0 && cy >= 0 && cx < innerWidth && cy < innerHeight) {
      const hit = document.elementFromPoint(cx, cy);
      top = !!hit && (hit === el || el.contains(hit) || hit.contains(el));
    }
    out.push({k, texts, tag: el.tagName, role, click, field: isField, top,
              x: r.left, y: r.top, w: r.width, h: r.height,
              value: isField ? fieldValue(el) : null});
  }
  return out;
}"""

# Ближайшее к подписи поле (mode='field') или любое значение (mode='value'): справа в той же строке или ниже.
JS_NEAREST = "([k, mode, maxDist]) => {" + _JS_COMMON + r"""
  const lab = document.querySelector(`[data-rpa="${k}"]`);
  if (!lab) return null;
  const mark = (el, dist) => {
    const nk = k + 'f' + Math.floor(Math.random() * 1e6);
    el.setAttribute('data-rpa-pin', nk);   // отдельный атрибут: следующие сканирования его не затирают
    return {k: nk, dist, value: fieldValue(el), tag: el.tagName, field: el.matches(FIELD_SEL)};
  };
  // 1) явная связь <label for=...> / поле внутри <label>
  if (lab.tagName === 'LABEL' && lab.control && vis(lab.control)) return mark(lab.control, 0);
  const inner = lab.querySelector && lab.querySelector(FIELD_SEL);
  if (inner && vis(inner)) return mark(inner, 0);
  // 2) геометрия
  const L = lab.getBoundingClientRect();
  const lcy = L.top + L.height / 2;
  let pool;
  if (mode === 'field') pool = document.querySelectorAll(FIELD_SEL);
  else pool = [...document.querySelectorAll('body *')].filter(e => e.matches(FIELD_SEL) || (ownText(e) && !['SCRIPT', 'STYLE', 'OPTION'].includes(e.tagName)));
  let best = null, bestD = 1e9;
  for (const f of pool) {
    if (f === lab || lab.contains(f) || f.contains(lab)) continue;
    const r = vis(f); if (!r) continue;
    const fcy = r.top + r.height / 2;
    let d = null;
    const sameRow = (lcy >= r.top - 6 && lcy <= r.bottom + 6) || (fcy >= L.top - 6 && fcy <= L.bottom + 6);
    if (sameRow && r.left >= L.left + L.width * 0.6) {
      d = Math.max(0, r.left - L.right) + Math.abs(r.top - L.top) * 0.5;               // справа
    } else if (r.top >= L.top + L.height * 0.6 && r.left < L.right + 40 && r.right > L.left - 40) {
      d = Math.max(0, r.top - L.bottom) * 1.6 + Math.abs(r.left - L.left) * 0.5 + 20; // снизу
    }
    if (d === null || d > maxDist) continue;
    if (d < bestD) { bestD = d; best = f; }
  }
  return best ? mark(best, bestD) : null;
}"""

# Кнопка-«треугольник» сразу справа от поля (для выпадающих списков).
JS_TRIGGER = "(k) => {" + _JS_COMMON + r"""
  const f = document.querySelector(`[data-rpa-pin="${k}"]`);
  if (!f) return null;
  const F = f.getBoundingClientRect();
  let best = null, area = 1e12;
  for (const el of document.querySelectorAll('body *')) {
    if (el === f || el.contains(f) || f.contains(el)) continue;
    const r = vis(el); if (!r) continue;
    if (r.left < F.right - 6 || r.left > F.right + 30) continue;
    if (r.bottom < F.top + 2 || r.top > F.bottom - 2) continue;
    const a = r.width * r.height;
    if (a < area && r.width < 60) { area = a; best = el; }
  }
  if (!best) return null;
  const nk = k + 't';
  best.setAttribute('data-rpa-pin', nk);
  return nk;
}"""

# Таблица по заголовкам колонок: ячейки раскладываются по колонкам по координатам.
JS_GRID = "([keys, maxRows]) => {" + _JS_COMMON + r"""
  const hs = keys.map(k => document.querySelector(`[data-rpa="${k}"]`));
  if (hs.some(x => !x)) return null;
  const R = hs.map(e => e.getBoundingClientRect());
  const order = R.map((r, i) => i).sort((a, b) => R[a].left - R[b].left);
  const spans = new Array(keys.length);
  order.forEach((ci, j) => {
    const r = R[ci];
    const next = order[j + 1] != null ? R[order[j + 1]].left : r.left + Math.max(r.width * 2.5, 250);
    spans[ci] = [r.left - 6, next - 6];
  });
  const headBottom = Math.max(...R.map(r => r.bottom));
  const items = [];
  for (const el of document.querySelectorAll('body *')) {
    if (hs.includes(el)) continue;
    let t = ownText(el);
    if (!t && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) t = el.value;
    if (!t) continue;
    const r = vis(el); if (!r) continue;
    if (r.top < headBottom - 2) continue;
    const col = spans.findIndex(s => r.left + 2 >= s[0] && r.left + 2 < s[1]);
    if (col < 0) continue;
    items.push({col, y: r.top + r.height / 2, h: r.height, t});
  }
  items.sort((a, b) => a.y - b.y);
  const rows = [];
  for (const it of items) {
    const last = rows[rows.length - 1];
    if (last && Math.abs(it.y - last.y) <= Math.max(4, Math.min(it.h, last.h) * 0.45)) {
      last.cells[it.col] = last.cells[it.col] ? last.cells[it.col] + ' ' + it.t : it.t;
    } else {
      const cells = new Array(keys.length).fill(null); cells[it.col] = it.t;
      rows.push({y: it.y, h: it.h, cells});
    }
  }
  const out = []; let prevY = null, gap = null;
  for (const r of rows) {
    const filled = r.cells.filter(Boolean).length;
    if (filled < Math.ceil(keys.length / 2)) { if (out.length) break; else continue; }   // футер «Всего: N» и т.п.
    if (prevY != null) {
      const g = r.y - prevY;
      if (gap != null && g > gap * 3 + 10) break;
      gap = gap == null ? g : Math.min(gap, g);
    }
    out.push(r.cells); prevY = r.y;
    if (out.length >= maxRows) break;
  }
  return out;
}"""

# Для разведки: каждое видимое поле + ближайшая подпись слева/сверху.
JS_SCOUT_FIELDS = "() => {" + _JS_COMMON + r"""
  const texts = [...document.querySelectorAll('body *')].map(e => [e, ownText(e)]).filter(([e, t]) => t && vis(e));
  const out = [];
  for (const f of document.querySelectorAll(FIELD_SEL)) {
    const r = vis(f); if (!r) continue;
    let best = null, bd = 1e9;
    for (const [e, t] of texts) {
      if (e.contains(f)) continue;
      const L = e.getBoundingClientRect();
      const lcy = L.top + L.height / 2;
      let d = null;
      if (lcy >= r.top - 6 && lcy <= r.bottom + 6 && L.right <= r.left + 8) d = r.left - L.right;
      else if (L.bottom <= r.top + 4 && L.left < r.right && L.right > r.left - 40) d = (r.top - L.bottom) * 1.6 + 20;
      if (d !== null && d >= -8 && d < bd && d < 500) { bd = d; best = t; }
    }
    out.push({label: best, tag: f.tagName.toLowerCase(), type: f.type || f.getAttribute('role') || '',
              value: String(fieldValue(f)).slice(0, 80), readonly: !!(f.readOnly || f.disabled)});
  }
  return out;
}"""


# ─────────────────────────── вспомогательные структуры ───────────────────────────

@dataclass
class Cand:
    """Найденный на экране элемент."""
    frame: Frame
    key: str
    text: str
    src: str
    tag: str
    role: str
    click: bool
    field: bool
    top: bool
    x: float
    y: float
    w: float
    h: float
    value: str | None = None
    score: float = 0.0
    attr: str = "data-rpa"

    @property
    def locator(self) -> Locator:
        return self.frame.locator(f'[{self.attr}="{self.key}"]')

    def __repr__(self):
        return f"<{self.tag.lower()} «{self.text}» score={self.score:.2f} frame={_frame_label(self.frame)}>"


def _frame_label(fr: Frame) -> str:
    try:
        u = fr.url.split("?")[0].rsplit("/", 1)[-1] or fr.url
        return f"{fr.name or '-'}:{u}"
    except PWError:
        return "?"


@dataclass
class OcrHit:
    text: str
    x: float
    y: float
    w: float
    h: float
    score: float

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


# ─────────────────────────── OCR (компьютерное зрение) ───────────────────────────

def _setup_tesseract():
    import pytesseract
    cmd = os.environ.get("TESSERACT_CMD")
    if not cmd and os.name == "nt":
        for c in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
            if os.path.exists(c):
                cmd = c
                break
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract


def _ocr_words(img, lang: str, invert: bool):
    from PIL import ImageOps
    pytesseract = _setup_tesseract()
    g = ImageOps.grayscale(img)
    if invert:
        g = ImageOps.invert(g)
    k = 2  # увеличение в 2 раза заметно улучшает распознавание мелкого интерфейсного шрифта
    big = g.resize((g.width * k, g.height * k))
    d = pytesseract.image_to_data(big, lang=lang, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i, w in enumerate(d["text"]):
        if w and w.strip() and float(d["conf"][i]) > 10:
            words.append((w.strip(), d["left"][i] / k, d["top"][i] / k, d["width"][i] / k, d["height"][i] / k))
    return words


def _group_lines(words):
    lines = []
    for w in sorted(words, key=lambda w: (w[2], w[1])):
        cy = w[2] + w[4] / 2
        for ln in lines:
            last = ln[-1]
            lcy = last[2] + last[4] / 2
            if abs(cy - lcy) < max(w[4], last[4]) * 0.6 and -5 < w[1] - (last[1] + last[3]) < max(w[4], last[4]) * 2.2:
                ln.append(w)
                break
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w[1])
    return lines


def ocr_lines(img, lang: str = "rus+eng", invert: bool = False) -> list[OcrHit]:
    """Весь распознанный текст изображения, построчно (для отладки и чтения данных с экрана)."""
    out = []
    for ln in _group_lines(_ocr_words(img, lang, invert)):
        x0 = min(w[1] for w in ln); y0 = min(w[2] for w in ln)
        x1 = max(w[1] + w[3] for w in ln); y1 = max(w[2] + w[4] for w in ln)
        out.append(OcrHit(" ".join(w[0] for w in ln), x0, y0, x1 - x0, y1 - y0, 1.0))
    return out


def ocr_locate(img, text: str, lang: str = "rus+eng", min_ratio: float = 0.75) -> OcrHit | None:
    """Ищет текст на изображении (PIL.Image). Координаты — в пикселях изображения."""
    target = norm(text)
    has_digits = any(ch.isdigit() for ch in target)
    need = 0.9 if has_digits else min_ratio
    n_words = max(1, len(text.split()))
    best: OcrHit | None = None
    for invert in (False, True):  # второй проход — для светлого текста на тёмном фоне
        for ln in _group_lines(_ocr_words(img, lang, invert)):
            for i in range(len(ln)):
                for j in range(i + 1, min(len(ln), i + n_words + 2) + 1):
                    seg = ln[i:j]
                    s = norm(" ".join(w[0] for w in seg))
                    r = 1.0 if s == target else difflib.SequenceMatcher(None, target, s).ratio()
                    if r >= need and (best is None or r > best.score):
                        x0 = min(w[1] for w in seg); y0 = min(w[2] for w in seg)
                        x1 = max(w[1] + w[3] for w in seg); y1 = max(w[2] + w[4] for w in seg)
                        best = OcrHit(" ".join(w[0] for w in seg), x0, y0, x1 - x0, y1 - y0, r)
        if best and best.score >= 0.95:
            break
    return best


def desktop_click_ocr(text: str, lang: str = "rus+eng") -> OcrHit:
    """Клик по тексту на ВСЁМ рабочем столе (без браузерной автоматизации вообще).
    Нужны: pip install pyautogui pillow pytesseract. На экранах с масштабом ≠100%
    координаты скриншота и мыши могут различаться — тогда включите DPI-awareness."""
    import pyautogui
    from PIL import ImageGrab
    img = ImageGrab.grab()
    hit = ocr_locate(img, text, lang)
    if not hit:
        raise RPAError(f"OCR: текст «{text}» на экране не найден")
    sw, sh = pyautogui.size()
    pyautogui.click(hit.cx * sw / img.width, hit.cy * sh / img.height)
    return hit


# ─────────────────────────── основной класс ───────────────────────────

class Portal:
    """Обёртка над страницей Playwright, которая ищет элементы по тексту и геометрии."""

    def __init__(self, page: Page, *, timeout: float = 15, ocr_fallback: bool = True, ocr_after: float = 3,
                 ocr_lang: str = "rus+eng", verbose: bool = True, errors_dir: str | Path = "rpa_errors",
                 on_log: Callable[[str], None] | None = None):
        self.page = page
        self.timeout = timeout
        self.ocr_fallback = ocr_fallback
        self.ocr_after = ocr_after
        self.ocr_lang = ocr_lang
        self.verbose = verbose
        self.on_log = on_log
        self.errors_dir = Path(errors_dir)
        self._closers: list[Callable] = []
        self._net_file = None
        self._quiet = 0

    # ---------- запуск / подключение ----------

    @classmethod
    def launch(cls, url: str | None = None, *, profile_dir: str | Path = "rpa_profile",
               channel: str | None = "msedge", headless: bool = False,
               executable_path: str | None = None, **kw) -> "Portal":
        """Запускает браузер с ОТДЕЛЬНЫМ постоянным профилем: войдите в портал один раз
        руками — сессия/куки сохранятся в profile_dir для следующих запусков.
        channel: 'msedge' | 'chrome' | None (встроенный Chromium Playwright)."""
        pw = sync_playwright().start()
        opts: dict = {"headless": headless}
        if channel:
            opts["channel"] = channel
        if executable_path:
            opts["executable_path"] = executable_path
        if headless:
            opts["viewport"] = {"width": 1600, "height": 900}
        else:
            opts["no_viewport"] = True
            opts["args"] = ["--start-maximized"]
        ctx = pw.chromium.launch_persistent_context(str(profile_dir), **opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        p = cls(page, **kw)
        p._closers = [ctx.close, pw.stop]
        if url:
            p.goto(url)
        return p

    @classmethod
    def connect(cls, cdp_url: str = "http://localhost:9222", url_contains: str | None = None, **kw) -> "Portal":
        """Подключается к УЖЕ открытому браузеру, где вы залогинены.
        Браузер должен быть запущен с ключом --remote-debugging-port=9222."""
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        pages = ctx.pages
        page = None
        if url_contains:
            page = next((pg for pg in pages if url_contains in pg.url), None)
        page = page or (pages[-1] if pages else ctx.new_page())
        p = cls(page, **kw)
        p._closers = [pw.stop]  # чужой браузер не закрываем
        return p

    def close(self):
        for c in self._closers:
            try:
                c()
            except Exception:
                pass
        if self._net_file:
            self._net_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def goto(self, url: str):
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            if "ERR_INVALID_AUTH_CREDENTIALS" in str(e):
                raise RPAError(
                    f"Сервер отклонил учётные данные из профиля (ERR_INVALID_AUTH_CREDENTIALS).\n"
                    f"  → Удалите папку профиля и войдите заново:\n"
                    f"    rmdir /s /q rpa_profile\n"
                    f"  → Или запустите со свежим профилем:\n"
                    f"    python example_scenario.py --url {url} --channel chrome --fresh-profile"
                ) from e
            raise
        self._log(f"открыт {url}")

    def use_latest_window(self):
        """Переключиться на последнее открытое окно/вкладку (если портал открывает всплывающие окна)."""
        self.page = self.page.context.pages[-1]
        self.page.bring_to_front()

    # ---------- служебное ----------

    def _sleep(self, sec: float):
        # не time.sleep: в синхронном Playwright события (новые iframe и т.п.) обрабатываются
        # только во время вызовов API, поэтому ждём через страницу
        try:
            self.page.wait_for_timeout(sec * 1000)
        except PWError:
            time.sleep(sec)

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(f"[{_dt.datetime.now():%H:%M:%S}] {msg}")
        if self.verbose:
            print(f"[{_dt.datetime.now():%H:%M:%S}] {msg}", flush=True)

    @contextlib.contextmanager
    def _quietly(self):
        """Внутренние проверки (exists, ожидание значения): неудача — штатная, скриншот не нужен."""
        self._quiet += 1
        try:
            yield
        finally:
            self._quiet -= 1

    def _fail(self, msg: str, near_text: str | None = None) -> RPAError:
        if self._quiet:
            return RPAError(msg)
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        shot = self.errors_dir / f"{_dt.datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            self.page.screenshot(path=str(shot))
            msg += f"\n  скриншот: {shot}"
        except PWError:
            pass
        if near_text:
            seen = {}
            for c in self._scan():
                for _, t in c["texts"]:
                    r = difflib.SequenceMatcher(None, norm(near_text), norm(t)).ratio()
                    if r > 0.5:
                        seen[t] = max(r, seen.get(t, 0))
            if seen:
                top = sorted(seen.items(), key=lambda kv: -kv[1])[:6]
                msg += "\n  похожие тексты на экране: " + ", ".join(f"«{t}» ({r:.2f})" for t, r in top)
        return RPAError(msg)

    def _frame_visible(self, fr: Frame, cache: dict) -> bool:
        if fr in cache:
            return cache[fr]
        ok = True
        try:
            if fr.is_detached():
                ok = False
            elif fr.parent_frame is not None:
                el = fr.frame_element()
                box = el.bounding_box()
                ok = bool(box and box["width"] > 1 and box["height"] > 1) and el.is_visible()
                ok = ok and self._frame_visible(fr.parent_frame, cache)
        except PWError:
            ok = False
        cache[fr] = ok
        return ok

    def frames(self, frame_url: str | None = None) -> list[Frame]:
        """Все ВИДИМЫЕ фреймы страницы (любой вложенности)."""
        cache: dict = {}
        out = []
        for fr in self.page.frames:
            if frame_url and frame_url not in fr.url and frame_url != fr.name:
                continue
            if self._frame_visible(fr, cache):
                out.append(fr)
        return out

    def _scan(self, frame_url: str | None = None) -> list[dict]:
        scan_id = uuid.uuid4().hex[:6]
        res = []
        for fr in self.frames(frame_url):
            try:
                cands = fr.evaluate(JS_COLLECT, scan_id)
            except PWError:
                continue  # фрейм перезагрузился во время сканирования
            for c in cands:
                c["frame"] = fr
            res.extend(cands)
        return res

    def _match(self, text: str, *, exact=False, clickable=None, frame_url=None,
               min_score=0.8, scan=None) -> list[Cand]:
        out = []
        for c in (scan if scan is not None else self._scan(frame_url)):
            best, btxt, bsrc = 0.0, "", ""
            for src, t in c["texts"]:
                s = 1.0 if norm(t) == norm(text) else (0.0 if exact else similarity(text, t))
                if src in ("title", "aria-label", "alt", "placeholder", "inner"):
                    s *= 0.98  # видимый текст чуть приоритетнее подсказки
                if s > best:
                    best, btxt, bsrc = s, t, src
            if best >= min_score:
                out.append(Cand(c["frame"], c["k"], btxt, bsrc, c["tag"], c["role"], c["click"], c["field"],
                                c["top"], c["x"], c["y"], c["w"], c["h"], c.get("value"), best))
        out.sort(key=lambda c: (-round(c.score, 2), not c.top,
                                (not c.click) if clickable else False, c.w * c.h))
        return out

    def _deadline(self, timeout):
        return time.time() + (self.timeout if timeout is None else timeout)

    # ---------- поиск ----------

    def find(self, text: str, *, exact: bool = False, timeout: float | None = None, clickable: bool | None = None,
             near: str | None = None, frame_url: str | None = None, min_score: float = 0.8) -> Cand:
        """Находит видимый элемент по тексту/подсказке во всех фреймах. Ждёт появления до timeout.
        near — текст-ориентир: из нескольких одинаковых выбирается ближайший к нему (в том же фрейме)."""
        end = self._deadline(timeout)
        while True:
            c = self._find_once(text, exact, clickable, near, frame_url, min_score)
            if c:
                return c
            if time.time() > end:
                raise self._fail(f"Не найден элемент «{text}»", text)
            self._sleep(0.25)

    def _find_once(self, text, exact=False, clickable=None, near=None, frame_url=None, min_score=0.8) -> Cand | None:
        scan = self._scan(frame_url)
        cands = self._match(text, exact=exact, clickable=clickable, min_score=min_score, scan=scan)
        if cands and near:
            anchors = self._match(near, scan=scan)
            if anchors:
                def dist(c):
                    ds = [((c.x - a.x) ** 2 + (c.y - a.y) ** 2) ** 0.5 for a in anchors if a.frame == c.frame]
                    return min(ds) if ds else 1e9
                cands.sort(key=lambda c: (-round(c.score, 2), dist(c)))
        return cands[0] if cands else None

    def exists(self, text: str, *, timeout: float = 0, exact: bool = False) -> bool:
        try:
            with self._quietly():
                self.find(text, timeout=timeout, exact=exact)
            return True
        except RPAError:
            return False

    def wait_text(self, text: str, timeout: float | None = None, exact: bool = False) -> Cand:
        return self.find(text, timeout=timeout, exact=exact)

    def wait_gone(self, text: str, timeout: float | None = None, exact: bool = True):
        end = self._deadline(timeout)
        while self._match(text, exact=exact):
            if time.time() > end:
                raise self._fail(f"Не исчез «{text}»")
            self._sleep(0.25)

    # ---------- действия ----------

    def click(self, text: str, *, exact: bool = False, timeout: float | None = None, near: str | None = None,
              frame_url: str | None = None, double: bool = False, ocr: bool | None = None) -> Cand | OcrHit:
        """Клик по элементу с текстом/подсказкой text. Если в DOM его нет дольше ocr_after секунд —
        параллельно пробует найти текст OCR по скриншоту (canvas, картинки, нестандартная отрисовка)."""
        end = self._deadline(timeout)
        use_ocr = self.ocr_fallback if ocr is None else ocr
        ocr_at = time.time() + self.ocr_after
        last_err = None
        while True:
            c = self._find_once(text, exact, True, near, frame_url)
            if c:
                try:
                    if double:
                        c.locator.dblclick(timeout=2500)
                    else:
                        c.locator.click(timeout=2500)
                    self._log(f"клик «{text}» → <{c.tag.lower()}> «{c.text}» [{_frame_label(c.frame)}]")
                    return c
                except PWError as e:  # элемент перерисовался/перекрыт — пересканируем
                    last_err = e
            elif use_ocr and time.time() >= ocr_at:
                try:
                    hit = self.ocr_find(text)
                except Exception as e:  # не установлен Tesseract/pytesseract и т.п.
                    self._log(f"OCR недоступен ({type(e).__name__}: {e}) — ищу только в DOM")
                    hit, use_ocr = None, False
                if hit:
                    (self.page.mouse.dblclick if double else self.page.mouse.click)(hit.cx, hit.cy)
                    self._log(f"OCR-клик «{text}» → «{hit.text}» ({hit.score:.2f}) в точке {hit.cx:.0f},{hit.cy:.0f}")
                    return hit
                ocr_at = time.time() + 2
            if time.time() > end:
                why = f": {str(last_err).splitlines()[0]}" if last_err else ""
                raise self._fail(f"Не удалось кликнуть «{text}»{why}", text)
            self._sleep(0.25)

    def field(self, label: str, *, timeout: float | None = None, max_dist: float = 600) -> Cand:
        """Поле ввода по подписи (справа от подписи или под ней), либо по placeholder/aria-label самого поля."""
        end = self._deadline(timeout)
        while True:
            labels = self._match(label)
            for c in labels:
                if c.field:  # подпись — это placeholder/aria-label/title самого поля
                    pin = c.key + "p"
                    try:
                        c.locator.evaluate("(e, k) => e.setAttribute('data-rpa-pin', k)", pin)
                    except PWError:
                        continue
                    c.key, c.attr = pin, "data-rpa-pin"
                    return c
            best = None
            for c in labels[:8]:
                try:
                    r = c.frame.evaluate(JS_NEAREST, [c.key, "field", max_dist])
                except PWError:
                    continue
                if r:
                    rank = r["dist"] + (1 - c.score) * 400
                    if best is None or rank < best[0]:
                        best = (rank, c, r)
            if best:
                _, c, r = best
                return Cand(c.frame, r["k"], label, "near", r["tag"], "", False, True, True,
                            0, 0, 0, 0, r["value"], c.score, attr="data-rpa-pin")
            if time.time() > end:
                raise self._fail(f"Не найдено поле с подписью «{label}»", label)
            self._sleep(0.25)

    def fill(self, label: str, value, *, timeout: float | None = None, commit: bool = False):
        """Ввод значения в поле по подписи. commit=True — нажать Tab после ввода (некоторые формы сохраняют по blur).
        Использует locator.fill() БЕЗ предварительного клика — combo-виджеты (hpsm.widgets.Combo)
        открывают dropdown по onclick, и попап перехватывает pointer events."""
        end = self._deadline(timeout)
        while True:
            f = self.field(label, timeout=max(0.3, end - time.time()))
            try:
                # locator.fill() сам фокусирует и ставит значение (без click → нет dropdown)
                f.locator.fill(str(value), timeout=3000)
                if commit:
                    f.locator.press("Tab")
                self._log(f"поле «{label}» ← «{str(value)[:60]}»")
                return
            except PWError as e:
                # fallback: печать по символам (press_sequentially)
                try:
                    f.locator.press_sequentially(str(value), delay=30, timeout=3000)
                    if commit:
                        f.locator.press("Tab")
                    self._log(f"поле «{label}» ← «{str(value)[:60]}» (sequential)")
                    return
                except PWError:
                    pass
                if time.time() > end:
                    raise self._fail(f"Не удалось заполнить «{label}»: {str(e).splitlines()[0]}")
                self._sleep(0.3)

    def select(self, label: str, option: str, *, timeout: float | None = None):
        """Выбор значения в выпадающем списке по подписи. Работает и с <select>, и с самописными
        комбобоксами (ExtJS и т.п.): открывает список кликом по полю, затем по «треугольнику»,
        затем вводом текста; кликает появившийся пункт и проверяет результат."""
        f = self.field(label, timeout=timeout)
        if f.tag == "SELECT":
            opts = f.locator.evaluate("e => [...e.options].map(o => o.text)")
            best = max(opts, key=lambda o: similarity(option, o), default=None)
            if not best or similarity(option, best) < 0.8:
                raise self._fail(f"В списке «{label}» нет «{option}». Есть: {opts}")
            f.locator.select_option(label=best)
            self._log(f"список «{label}» = «{best}»")
            return
        before = {(id(c.frame), round(c.x), round(c.y)) for c in self._match(option)}
        opened = None
        for attempt in ("field", "trigger", "type"):
            try:
                if attempt == "field":
                    f.locator.click(timeout=2500)
                elif attempt == "trigger":
                    k = f.frame.evaluate(JS_TRIGGER, f.key)
                    if not k:
                        continue
                    f.frame.locator(f'[data-rpa-pin="{k}"]').click(timeout=2500)
                else:
                    f.locator.fill(option, timeout=2500)
                    f.locator.press("ArrowDown")
            except PWError:
                continue
            end = time.time() + 2.0
            while time.time() < end and not opened:
                new = [c for c in self._match(option)
                       if (id(c.frame), round(c.x), round(c.y)) not in before and not c.field]
                if new:
                    opened = new[0]
                self._sleep(0.2)
            if opened:
                break
        if not opened:
            raise self._fail(f"Не удалось открыть список «{label}» / найти пункт «{option}»", option)
        opened.locator.click(timeout=3000)
        v = self.read(label, timeout=3)
        if similarity(option, v or "") < 0.8:
            raise self._fail(f"После выбора в «{label}» значение «{v}», ожидалось «{option}»")
        self._log(f"список «{label}» = «{v}»")

    def press(self, key: str):
        self.page.keyboard.press(key)

    # ---------- чтение ----------

    def read(self, label: str, *, timeout: float | None = None) -> str | None:
        """Значение рядом с подписью: поле ввода, текст справа/снизу или «Подпись: значение» в одном элементе."""
        end = self._deadline(timeout)
        while True:
            scan = self._scan()
            for c in scan:  # «OLA: 2 ч 30 мин» — подпись и значение в одном элементе
                for src, t in c["texts"]:
                    if src == "own" and ":" in t:
                        left, right = t.split(":", 1)
                        if norm(left) == norm(label) and right.strip():
                            return right.strip()
            labels = self._match(label, scan=scan)
            best = None
            for c in labels[:8]:
                if c.field and c.src in ("placeholder", "aria-label", "title"):
                    return c.value
                try:
                    r = c.frame.evaluate(JS_NEAREST, [c.key, "value", 600])
                except PWError:
                    continue
                if r:
                    rank = r["dist"] + (1 - c.score) * 400
                    if best is None or rank < best[0]:
                        best = (rank, r["value"])
            if best:
                return (best[1] or "").strip()
            if time.time() > end:
                raise self._fail(f"Не найдено значение для «{label}»", label)
            self._sleep(0.25)

    def read_card(self, labels: Iterable[str], timeout_each: float = 3) -> dict:
        """Снимает значения сразу по списку подписей. Отсутствующие → None."""
        out = {}
        for lb in labels:
            try:
                with self._quietly():
                    out[lb] = self.read(lb, timeout=timeout_each)
            except RPAError:
                out[lb] = None
        return out

    def wait_value(self, label: str, expected: str, *, timeout: float | None = None, contains: bool = False) -> str:
        end = self._deadline(timeout)
        v = None
        while True:
            try:
                with self._quietly():
                    v = self.read(label, timeout=1)
            except RPAError:
                v = None
            if v is not None and ((norm(expected) in norm(v)) if contains else norm(v) == norm(expected)):
                self._log(f"«{label}» = «{v}»")
                return v
            if time.time() > end:
                raise self._fail(f"«{label}» = «{v}», а ожидалось «{expected}»")
            self._sleep(0.3)

    def read_grid(self, columns: list[str], *, timeout: float | None = None, max_rows: int = 500) -> list[dict]:
        """Читает таблицу по названиям колонок (ячейки раскладываются по координатам заголовков).
        Читаются только видимые строки: при «бесконечной» прокрутке листайте и читайте повторно."""
        end = self._deadline(timeout)
        while True:
            scan = self._scan()
            heads = [self._match(c, scan=scan) for c in columns]
            if all(heads):
                for h0 in heads[0][:5]:
                    keys = [h0.key]
                    for hs in heads[1:]:
                        same = [h for h in hs if h.frame == h0.frame and abs((h.y + h.h / 2) - (h0.y + h0.h / 2)) < 15]
                        if not same:
                            break
                        keys.append(min(same, key=lambda h: -h.score).key)
                    if len(keys) == len(columns):
                        try:
                            rows = h0.frame.evaluate(JS_GRID, [keys, max_rows])
                        except PWError:
                            rows = None
                        if rows:
                            self._log(f"таблица: {len(rows)} строк")
                            return [dict(zip(columns, r)) for r in rows]
            if time.time() > end:
                missing = [c for c, h in zip(columns, heads) if not h]
                raise self._fail(f"Таблица с колонками {columns} не найдена"
                                 + (f" (нет колонок {missing})" if missing else " (или в ней нет строк)"))
            self._sleep(0.3)

    # ---------- OCR ----------

    def _screenshot_img(self):
        from PIL import Image
        img = Image.open(io.BytesIO(self.page.screenshot()))
        css_w = self.page.evaluate("innerWidth")
        return img, img.width / css_w

    def ocr_find(self, text: str) -> OcrHit | None:
        img, scale = self._screenshot_img()
        hit = ocr_locate(img, text, self.ocr_lang)
        if hit:
            hit = OcrHit(hit.text, hit.x / scale, hit.y / scale, hit.w / scale, hit.h / scale, hit.score)
        return hit

    def click_ocr(self, text: str, *, double: bool = False, timeout: float = 5) -> OcrHit:
        """Клик по тексту, распознанному на скриншоте страницы (работает для canvas, картинок, чего угодно)."""
        end = time.time() + timeout
        while True:
            hit = self.ocr_find(text)
            if hit:
                (self.page.mouse.dblclick if double else self.page.mouse.click)(hit.cx, hit.cy)
                self._log(f"OCR-клик «{text}» → «{hit.text}» ({hit.score:.2f}) в точке {hit.cx:.0f},{hit.cy:.0f}")
                return hit
            if time.time() > end:
                raise self._fail(f"OCR: текст «{text}» на скриншоте не найден")
            self._sleep(0.5)

    def ocr_dump(self) -> list[str]:
        """Весь текст, который OCR видит на странице (для отладки)."""
        img, _ = self._screenshot_img()
        return [h.text for h in ocr_lines(img, self.ocr_lang)]

    # ---------- сеть ----------

    def record_network(self, path: str | Path = "network_log.jsonl",
                       types: tuple = ("xhr", "fetch", "document")):
        """Пишет в JSONL все запросы фронта к серверу (метод, URL, тело, ответ).
        По этому журналу видно, какие вызовы API делает портал при «Принять в работу» и т.п. —
        часто их можно повторять напрямую (requests + куки сессии), вообще без кликов."""
        self._net_file = open(path, "a", encoding="utf-8")

        def on_response(resp):
            req = resp.request
            if req.resource_type not in types or self._net_file.closed:
                return
            try:
                body = resp.text()[:20000]
            except Exception:
                body = None
            rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "method": req.method, "url": req.url,
                   "type": req.resource_type, "status": resp.status, "request_body": (req.post_data or "")[:20000],
                   "response_body": body}
            self._net_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._net_file.flush()

        self.page.context.on("response", on_response)
        self._log(f"запись сетевых запросов → {path}")

    # ---------- разведка ----------

    def scout(self, out=None) -> str:
        """Отчёт: дерево фреймов, кликабельные тексты и поля с подписями на текущем экране.
        Из этого отчёта берите тексты для click()/fill()/read()."""
        lines = []
        cache: dict = {}
        for fr in self.page.frames:
            depth, p = 0, fr.parent_frame
            while p:
                depth, p = depth + 1, p.parent_frame
            ind = "    " * depth
            visible = self._frame_visible(fr, cache)
            lines.append(f"{ind}■ фрейм [{fr.name or '-'}] {fr.url[:120]}{'' if visible else '  (скрыт)'}")
            if not visible:
                continue
            try:
                cands = fr.evaluate(JS_COLLECT, "s" + uuid.uuid4().hex[:4])
                fields = fr.evaluate(JS_SCOUT_FIELDS)
            except PWError:
                continue
            clicks = []
            for c in cands:
                if c["click"] and c["texts"]:
                    t = " / ".join(dict.fromkeys(x[1] for x in c["texts"]))  # «↻ / Обновить карточку»
                    if t not in clicks and len(t) < 80:
                        clicks.append(t)
            if clicks:
                lines.append(f"{ind}  кликабельное: " + " | ".join(f"«{t}»" for t in clicks[:80]))
            for f in fields:
                ro = " (только чтение)" if f["readonly"] else ""
                val = re.sub(r"\s+", " ", f["value"])
                lines.append(f"{ind}  поле «{f['label']}» [{f['tag']} {f['type']}]{ro} = «{val}»")
        rep = "\n".join(lines)
        print(rep, file=out or sys.stdout)
        return rep


# ─────────────────────────── CLI: разведка ───────────────────────────

def _cli():
    ap = argparse.ArgumentParser(description="Разведка экрана портала: что видит скрипт.")
    ap.add_argument("cmd", choices=["scout"])
    ap.add_argument("--url")
    ap.add_argument("--cdp", help="подключиться к открытому браузеру, напр. http://localhost:9222")
    ap.add_argument("--channel", default="msedge", help="msedge | chrome | none")
    ap.add_argument("--profile", default="rpa_profile")
    a = ap.parse_args()
    if a.cdp:
        p = Portal.connect(a.cdp, url_contains=a.url)
    else:
        p = Portal.launch(a.url, channel=None if a.channel == "none" else a.channel, profile_dir=a.profile)
    try:
        while True:
            input("\nОткройте нужный экран в браузере и нажмите Enter (Ctrl+C — выход)... ")
            rep = p.scout()
            Path("scout_report.txt").write_text(rep, encoding="utf-8")
            print("\n(отчёт сохранён в scout_report.txt)")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        p.close()


if __name__ == "__main__":
    _cli()
