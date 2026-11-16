"""Журнал и отчёт об ошибке не должны содержать личных и рабочих данных."""
import logging

from opsdigest import logs


def test_scrub_removes_sensitive(monkeypatch):
    monkeypatch.setenv("USERNAME", "nikolay")
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-ABC123")
    logs.add_secrets_from_config({
        "profile": {"name": "Алексей", "aliases": ["Лёша"]},
        "telegram": {"api_id": "1234567", "api_hash": "0123456789abcdef0123456789abcdef", "phone": "+79991234567",
                     "proxies": ["tg://proxy?server=px.corp-example.ru&port=443&secret=dd00"]},
        "llm": {"profiles": {"maverick": {"url": "https://maverick.bank.local/api", "headers": {"Authorization": "Bearer XyZ123token"}}}},
    })
    text = "\n".join([
        r'File "C:\Users\sample-user\AppData\Local\OpsDigest\venv\Lib\site-packages\telethon\client\users.py", line 84',
        r'File "C:\Users\sample-user\Documents\OpsDigest\opsdigest\digest.py", line 312, in build',
        "HTTP 401 от https://maverick.bank.local/api: bad token XyZ123token",
        "via px.corp-example.ru:443 (10.20.30.40:3128) and maverick.bank.local",
        "phone +7 999 123-45-67, @sample_user, user@example.com, Алексей и Лёша",
        "INC0048213 BILL-482 chat -1001234567890 api 1234567 hash 0123456789abcdef0123456789abcdef",
        r"D:\Work\Секретный проект\data\x.txt \\fileserver\share CORP\kpetrov DESKTOP-ABC123",
        "'Очень длинное название чата, которое пришло из данных и не должно попасть в журнал'",
    ])
    out = logs.scrub(text)
    for leak in ("nikolay\\", "maverick.bank.local", "XyZ123token", "corp-example", "10.20.30.40", "999 123", "nikolay_p",
                 "gmail", "Алексей", "Лёша", "INC0048213", "BILL-482", "1001234567890", "1234567", "0123456789abcdef",
                 "Секретный", "fileserver", "kpetrov", "DESKTOP-ABC123", "название чата"):
        assert leak not in out, (leak, out)
    # полезное для разбора остаётся
    for keep in ("site-packages\\telethon\\client\\users.py", "line 84", "opsdigest\\digest.py", "line 312", "HTTP 401"):
        assert keep in out, (keep, out)


def test_logger_writes_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    h = logging.FileHandler(tmp_path / "t.log", encoding="utf-8")
    h.setFormatter(logs.ScrubFormatter("%(message)s"))
    lg = logging.getLogger("opsdigest.testscrub")
    lg.addHandler(h)
    lg.propagate = False
    lg.warning("ошибка при обращении к https://secret.example.com/v1 для +79991112233")
    h.close()
    t = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "<url>" in t and "<phone>" in t and "secret.example" not in t


def test_err_info_has_no_body():
    from opsdigest.llm.base import LLMError
    try:
        try:
            raise LLMError("HTTP 500 от https://x.y/z: тело ответа с перепиской")
        except LLMError as e:
            e.status, e.safe = 500, "HTTP 500"
            raise
    except LLMError as e:
        info = logs.err_info(e)
    assert "status=500" in info and "переписк" not in info and "test_logs.py" not in info


def test_report_build(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    d = logs.log_dir()
    (d / "opsdigest.log").write_text("2026-11-01 ok\nошибка у +79990001122 на host.corp.example\n", encoding="utf-8")
    (d / "launcher.log").write_text("01.11.2026 запуск\n", encoding="utf-8")
    from opsdigest import diag, report
    monkeypatch.setattr(diag, "_https", lambda url, timeout=8: (False, "нет связи"))
    monkeypatch.setattr(diag, "_tcp", lambda h, p, timeout=5: (False, "нет ответа (таймаут)"))
    raw = {"llm": {"provider": "local", "profiles": {"local": {"type": "openai", "base_url": "http://localhost:1235/v1"}}},
           "telegram": {"api_id": "1", "api_hash": "h", "proxies": ["tg://proxy?server=10.1.1.1&port=443&secret=dd00"]}}
    text = report.build(ws=tmp_path, raw=raw)
    assert "Проверка компьютера" in text and "Python" in text and "Прокси 1 (mtproto-dd)" in text
    assert "+7999" not in text and "host.corp.example" not in text and "10.1.1.1" not in text
    assert "opsdigest.log" in text and "launcher.log" in text
    p = report.save(text)
    assert p.exists() and p.name.startswith("OpsDigest_отчёт_")
    p.unlink()
