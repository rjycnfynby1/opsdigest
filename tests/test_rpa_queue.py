import copy
from types import SimpleNamespace

import pytest

from opsdigest import rpa


def item():
    return {"kind": "incidents", "filters": {"group": "Другая группа",
            "created_after": "2026-11-01T09:30", "created_before": "2026-11-02T18:45"},
            "statuses": ["Выполнен", "Закрыт"]}


def test_filters_roundtrip(tmp_path):
    rpa.save_template(tmp_path, "Рабочая очередь", [item(), {"kind": "problems", "confirm_all": True}])
    saved = rpa.templates(tmp_path)["Рабочая очередь"]
    assert saved[0]["statuses"] == ["Выполнен", "Закрыт"]
    assert saved[0]["filters"]["created_before"] == "02.11.2026 18:45:00"
    assert saved[1]["kind"] == "problems"


def test_problems_are_independent_queue_item(tmp_path):
    problems = {"kind": "problems", "confirm_all": True}
    rpa.save_template(tmp_path, "Только проблемы", [problems])
    saved = rpa.templates(tmp_path)["Только проблемы"]
    assert saved == [rpa.validate(problems)]
    mixed = rpa.queue([problems, item(), problems])
    assert [entry["kind"] for entry in mixed] == ["problems", "incidents", "problems"]
    assert mixed[0]["filters"] == {} and mixed[0]["statuses"] == []


@pytest.mark.parametrize("value", ["2026-02-30", "garbage", "2026-10-01T00:00"])
def test_missing_invalid_reversed_end_rejected(value):
    x = item()
    x["filters"]["created_before"] = value
    with pytest.raises(ValueError):
        rpa.validate(x)


def test_unbounded_rejected_number_allowed():
    x = item()
    del x["filters"]["created_before"]
    assert rpa.validate(x)["filters"]["created_after"] == "01.11.2026 09:30:00"
    x = item()
    del x["filters"]["created_after"]
    with pytest.raises(ValueError):
        rpa.validate(x)
    with pytest.raises(ValueError):
        rpa.validate({"kind": "incidents", "filters": {"group": "Any"}})
    assert rpa.validate({"kind": "incidents", "filters": {"number": "IM123"}})
    with pytest.raises(ValueError):
        rpa.validate({"kind": "problems"})


@pytest.mark.parametrize("lost", ["Создан до", "Статус", "Группа поддержки"])
def test_readback_loss_blocks_search(monkeypatch, lost):
    import example_scenario
    import opsdigest_export
    values = {}
    monkeypatch.setattr(example_scenario, "_fill_search_field", lambda p, k, v: values.update({k: v}))
    monkeypatch.setattr(opsdigest_export, "_select_status_reliable", lambda p, k, v: values.update({k: v}))
    def field(label):
        return SimpleNamespace(locator=SimpleNamespace(evaluate=lambda js: "" if label == lost else values[label]))
    with pytest.raises(ValueError, match="Поиск отменён"):
        rpa.verified_fields(SimpleNamespace(field=field), rpa.validate(item()), "Выполнен")


def test_filename_unique_and_safe():
    x = rpa.validate(item())
    a, b = rpa.filename(x, "Закрыт", ".csv"), rpa.filename(x, "Закрыт", ".csv")
    assert a != b and a.endswith(".csv") and "Другая группа" in a
    assert not any(c in a for c in '<>:"/\\|?*')


def test_rpa_is_main_window_tab():
    from pathlib import Path
    ui = Path(__file__).resolve().parent.parent / "opsdigest" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    modules = (ui / "modules.js").read_text(encoding="utf-8")
    css = (ui / "modules.css").read_text(encoding="utf-8")
    assert 'id="mode-rpa" data-m="rpa"' in html
    assert "{k: 'rpa'" in modules and "mode-rpa" in css


@pytest.mark.parametrize("collapsed", [True, False])
def test_incident_navigation_expands_only_collapsed_menu(collapsed):
    from itsm_rpa import RPAError
    clicks = []
    def click(label, **kwargs):
        clicks.append(label)
        if collapsed and len(clicks) == 1:
            raise RPAError("Меню свёрнуто")
    fields = []
    rpa.navigate_incidents(SimpleNamespace(click=click, field=lambda label, **kw: fields.append(label)))
    assert clicks == (["Поиск инцидентов", "01. Управление инцидентами", "Поиск инцидентов"]
                      if collapsed else ["Поиск инцидентов"])
    assert fields == ["Группа поддержки"]


def test_portal_log_reaches_job():
    from itsm_rpa import Portal
    lines = []
    portal = Portal(None, verbose=False, on_log=lines.append)
    portal._log("Открываю поиск")
    assert len(lines) == 1 and lines[0].endswith("Открываю поиск")


@pytest.mark.parametrize("initial,rebuilt,after,allowed", [
    (["Код", "Статус"], True, [], True),
    (["Статус", "Код"], True, ["Код", "Статус"], True),
    (["Код"], False, [], False),
    (["Код"], True, ["Код"], False),
    ([], True, ["Статус", "Код"], False),
    (["Код", "Код"], True, ["Код", "Код"], False),
])
def test_problem_export_checks_before_download(monkeypatch, initial, rebuilt, after, allowed):
    import example_scenario_fixed as scenario
    from itsm_rpa import RPAError
    events = []
    monkeypatch.setattr(scenario, "_load_reference_fields", lambda: (["Код", "Статус"], {}))
    for name in ("_navigate_to_problems", "_open_view_settings", "_cancel_view_settings"):
        monkeypatch.setattr(scenario, name, lambda p, name=name: events.append(name))
    reads = iter([initial, after])
    monkeypatch.setattr(scenario, "_read_view_summary", lambda p: next(reads))
    def rebuild(p):
        events.append("rebuild")
        return rebuilt
    monkeypatch.setattr(scenario, "rebuild_view_fields", rebuild)
    def export(p, **kwargs):
        events.append("export")
        return "problems.csv"
    monkeypatch.setattr(scenario, "export_to_excel", export)
    portal = SimpleNamespace(_log=lambda text: None)
    if allowed:
        assert scenario.export_problems(portal) == "problems.csv"
        assert events[-1] == "export"
    else:
        with pytest.raises(RPAError, match="Экспорт проблем отменён"):
            scenario.export_problems(portal)
        assert "export" not in events
    assert ("rebuild" in events) == (initial != ["Код", "Статус"])


def test_problem_export_missing_reference_blocks_navigation(monkeypatch):
    import example_scenario_fixed as scenario
    from itsm_rpa import RPAError
    monkeypatch.setattr(scenario, "_load_reference_fields", lambda: ([], {}))
    with pytest.raises(RPAError, match="отсутствует или пуст"):
        scenario.export_problems(SimpleNamespace(_log=lambda text: None))


@pytest.mark.parametrize("actual,expected", [
    (["Код", "Состояние"], True), (["Состояние", "Код"], False),
    (["Код", "Код"], False), (["Код"], False),
    (["Код", "Статус", "Лишнее"], False),
])
def test_reference_requires_order_count_and_explicit_aliases(actual, expected):
    from example_scenario_fixed import _fields_match_reference
    assert _fields_match_reference(actual, ["Код", "Статус"], {"Статус": ["Состояние"]}) == expected


def test_queue_calls_full_problem_export(tmp_path, monkeypatch):
    import itsm_rpa
    import example_scenario
    import example_scenario_fixed as scenario
    from contextlib import nullcontext
    calls = []
    monkeypatch.setattr(itsm_rpa.Portal, "launch", lambda *a, **kw: nullcontext(object()))
    def export(p, downloads_dir):
        calls.append("verified problems")
        path = __import__("pathlib").Path(downloads_dir) / "download.csv"
        path.write_text("Код;Статус", encoding="utf-8")
        return str(path)
    monkeypatch.setattr(scenario, "export_problems", export)
    def unverified(*a, **kw):
        pytest.fail("Очередь не должна обходить сверку полей проблем")
    monkeypatch.setattr(example_scenario, "export_to_excel", unverified)
    job = SimpleNamespace(cancelled=False, add=lambda text: None)
    result = rpa.run(tmp_path, [{"kind": "problems", "confirm_all": True}],
                     "https://portal.invalid", "chrome", job)
    assert calls == ["verified problems"]
    assert len(result["files"]) == 1


@pytest.mark.parametrize("final,expected", [
    (["Код", "Статус"], True), (["Статус", "Код"], False),
    (["Код", "Код"], False), (["Код"], False),
])
def test_rebuild_wipes_adds_and_verifies_order(monkeypatch, final, expected):
    import example_scenario_fixed as scenario
    events = []
    monkeypatch.setattr(scenario, "_load_reference_fields", lambda: (["Код", "Статус"], {}))
    monkeypatch.setattr(scenario.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scenario, "_wipe_all_fields", lambda p: events.append("wipe") or True)
    monkeypatch.setattr(scenario, "_add_all_reference_fields",
                        lambda p, ref, aliases: events.append(("add", ref)) or True)
    monkeypatch.setattr(scenario, "_read_view_fields", lambda p: final)
    portal = SimpleNamespace(_log=lambda text: None, find=lambda *a, **kw: None,
                             click=lambda *a, **kw: None)
    assert scenario.rebuild_view_fields(portal) == expected
    assert events == ["wipe", ("add", ["Код", "Статус"])]


@pytest.mark.parametrize("prefix,suffix", [
    ("Поля...", "Группировать по...Координатор проблемы (по возрастанию); Статус (по возрастанию)"),
    ("Поля…", "Группировать по…Координатор"),
    ("", ""),
])
def test_summary_excludes_adjacent_controls(prefix, suffix):
    from example_scenario_fixed import _parse_view_summary
    actual = _parse_view_summary(prefix + "Код; Заголовок; Ожидаемая дата устранения; Категория" + suffix)
    assert actual == ["Код", "Заголовок", "Ожидаемая дата устранения", "Категория"]


@pytest.mark.parametrize("settings_open", [True, False])
def test_rebuild_opens_fields_before_retrying_settings(monkeypatch, settings_open):
    import example_scenario_fixed as scenario
    from itsm_rpa import RPAError
    calls = []
    monkeypatch.setattr(scenario, "_load_reference_fields", lambda: (["Код"], {}))
    monkeypatch.setattr(scenario.time, "sleep", lambda seconds: None)
    def find(*args, **kwargs):
        raise RPAError("Диалог полей не открыт")
    def fields(p):
        calls.append("fields")
        if not settings_open and len(calls) == 1:
            raise RPAError("Настройки ещё не открыты")
    monkeypatch.setattr(scenario, "_open_fields_tab", fields)
    monkeypatch.setattr(scenario, "_open_view_settings", lambda p: calls.append("settings"))
    monkeypatch.setattr(scenario, "_wipe_all_fields", lambda p: calls.append("wipe") or True)
    monkeypatch.setattr(scenario, "_add_all_reference_fields", lambda *args: True)
    monkeypatch.setattr(scenario, "_read_view_fields", lambda p: ["Код"])
    portal = SimpleNamespace(find=find, _log=lambda text: None, click=lambda *a, **kw: None)
    assert scenario.rebuild_view_fields(portal)
    assert calls == (["fields", "wipe"] if settings_open else ["fields", "settings", "fields", "wipe"])
