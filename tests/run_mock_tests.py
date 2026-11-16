# -*- coding: utf-8 -*-
"""
Прогон библиотеки на тестовом стенде mock_portal/ (вложенные iframe, случайные id).
  python tests/run_mock_tests.py            # 5 прогонов в Edge, без окна
  python tests/run_mock_tests.py --show     # с окном браузера
  python tests/run_mock_tests.py --channel chrome   # в Chrome вместо Edge
"""
import argparse
import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import example_scenario as S  # noqa: E402
from itsm_rpa import Portal  # noqa: E402

PORT = 8799


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # имитация API бэкенда
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "echo": body.decode("utf-8")}).encode())


def serve():
    h = functools.partial(_Quiet, directory=str(ROOT / "mock_portal"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def check(cond, msg):
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        raise AssertionError(msg)


def ocr_available() -> bool:
    try:
        from itsm_rpa import _setup_tesseract
        t = _setup_tesseract()
        langs = t.get_languages(config="")
        if "rus" not in langs:
            print("! Tesseract найден, но без русского языка — OCR-проверка пропущена")
            return False
        return True
    except Exception as e:
        print(f"! Tesseract/pytesseract недоступен ({type(e).__name__}) — OCR-проверка пропущена")
        return False


def one_run(run_no: int, show: bool, ocr: bool, net_log: Path, channel) -> dict:
    prof = tempfile.mkdtemp(prefix="rpa_prof_")
    p = Portal.launch(f"http://127.0.0.1:{PORT}/index.html", channel=channel, headless=not show,
                      profile_dir=prof, verbose=False, timeout=15, ocr_fallback=ocr)
    ids = {}
    try:
        if run_no == 1:
            p.record_network(net_log)
        t0 = time.time()

        # 1. очередь
        rows = S.open_queue(p)
        check(len(rows) == 4 and rows[0]["Номер"] == "IM10021", f"очередь прочитана: {[r['Номер'] for r in rows]}")
        check(rows[1]["Статус"] == "Зарегистрирован" and rows[1]["Срок решения"] == "16.10.2026 12:47",
              "колонки разложены верно")

        # 2. открываем IM10022, уходим в список (карточка остаётся скрытой в DOM), открываем IM10021
        S.open_ticket(p, "IM10022")
        S.back_to_queue(p)
        S.open_ticket(p, "IM10021")
        ids["accept_btn_id"] = p.find("Принять в работу").locator.evaluate(
            "e => e.closest('table') ? e.closest('table').id : e.id")
        hidden = [f for f in p.page.frames if "IM10022" in f.url]
        check(len(hidden) == 1 and hidden[0] not in p.frames(), "скрытая карточка IM10022 есть в DOM, но игнорируется")

        # 3. сбор данных карточки
        card = S.collect_ticket(p)
        check(card["Номер"] == "IM10021", "Номер")
        check(card["Статус"] == "Зарегистрирован", "Статус (из input readonly)")
        check(card["Приоритет"] == "Средний", "Приоритет (из комбобокса)")
        check(card["Подробное описание"].startswith("Пользователь Иванов") and "809" in card["Подробное описание"],
              "Подробное описание (textarea, подпись НАД полем)")
        check(card["Срок решения (SLA)"] == "16.10.2026 17:12", "SLA")
        check(card["Осталось по SLA"] == "05:24", "Осталось по SLA")
        check(card["OLA"] == "2 ч 30 мин", "OLA («подпись: значение» в одном элементе)")

        # 4. действия
        S.take_in_work(p)
        S.put_on_hold(p, "Ожидание поставщика", "20.10.2026", "Ждём ответ провайдера")
        check(p.read("Причина ожидания") == "Ожидание поставщика", "в ожидании, причина выбрана (диалог в верхнем документе)")
        S.resume(p)
        task = S.create_task(p, "2ЛП Сеть", "Проверить туннель VPN на шлюзе")
        check(task.startswith("TSK"), f"задание создано из формы во фрейме 4-го уровня: {task}")
        S.link_problem(p, "PM1002")

        # 5. приоритет через комбобокс + сохранить
        p.select("Приоритет", "Высокий")
        p.fill("Краткое описание", "Не работает VPN (массово)")
        p.click("Сохранить", exact=True)
        p.wait_value("Приоритет", "Высокий")

        # 6. кнопка-иконка без текста (только подсказка title)
        p.click("Обновить карточку")
        p.wait_value("Статус", "В работе")

        # 7. canvas-кнопка — текста нет в DOM, срабатывает OCR
        if ocr:
            check(not p.exists("Эскалировать"), "текста «Эскалировать» в DOM нет")
            p.click("Эскалировать")
            p.wait_value("Эскалация", "Да")

        # 8. закрыть карточку, вернуться к ранее открытой (скрытой) IM10022 и прочитать короткое «OLA: 1 ч»
        S.close_ticket(p)
        S.open_ticket(p, "IM10022")
        check(p.read("OLA") == "1 ч" and p.read("Осталось по SLA") == "01:02", "IM10022: OLA и остаток SLA")

        db = p.page.evaluate("() => window.__db")
        inc = next(i for i in db["incidents"] if i["num"] == "IM10021")
        check(inc["status"] == "В работе" and inc["problem"] == "PM1002" and inc["prio"] == "Высокий"
              and inc["title"] == "Не работает VPN (массово)" and len(inc["tasks"]) == 1
              and (inc["escalated"] or not ocr),
              f"итог в «базе» портала: {json.dumps({k: inc[k] for k in ('status', 'prio', 'problem', 'tasks', 'escalated')}, ensure_ascii=False)}")
        ids["seconds"] = round(time.time() - t0, 1)
        ids["log"] = db["log"]
        return ids
    finally:
        p.close()
        shutil.rmtree(prof, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--channel", default="msedge", help="msedge | chrome | none (встроенный Chromium Playwright)")
    a = ap.parse_args()
    channel = None if a.channel == "none" else a.channel
    use_ocr = (not a.no_ocr) and ocr_available()
    srv = serve()
    net_log = ROOT / "tests" / "network_log_test.jsonl"
    net_log.unlink(missing_ok=True)
    results = []
    try:
        for i in range(1, a.runs + 1):
            print(f"\n=== прогон {i} ===")
            r = one_run(i, a.show, use_ocr, net_log, channel)
            print(f"  id кнопки «Принять в работу»: {r['accept_btn_id']}   время: {r['seconds']} c")
            results.append(r)
    finally:
        srv.shutdown()
    ids = [r["accept_btn_id"] for r in results]
    print(f"\nВсе {len(results)} прогонов успешны. id кнопки каждый раз разный: {len(set(ids)) == len(ids)} ({', '.join(ids)})")
    print("Действия в портале (прогон 1):", results[0]["log"])
    recs = [json.loads(x) for x in net_log.read_text(encoding="utf-8").splitlines()]
    api = [x for x in recs if x["url"].endswith("/api/action")]
    print(f"Журнал сети: {len(recs)} запросов, из них к API: {len(api)}; пример: "
          f"{api[0]['method']} {api[0]['url']} body={api[0]['request_body']}" if api else "Журнал сети пуст")


if __name__ == "__main__":
    main()
