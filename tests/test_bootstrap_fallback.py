from types import SimpleNamespace

import pytest

from opsdigest import bootstrap, logs


@pytest.mark.parametrize("offline,online,offline_rc,expected", [
    (False, True, 1, 0), (False, False, 1, 1), (True, False, 0, 0),
    (False, True, 0, 0), (False, False, 0, 1),
])
def test_complete_dependency_install(tmp_path, monkeypatch, offline, online, offline_rc, expected):
    (tmp_path / "existing.whl").touch()
    monkeypatch.setattr(bootstrap, "WHEELS", tmp_path)
    monkeypatch.setattr(bootstrap, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(bootstrap, "_pip_log", lambda: tmp_path / "pip.log")
    monkeypatch.setattr(logs, "setup", lambda *a: SimpleNamespace(info=lambda *a: None, error=lambda *a: None))
    state = {"missing": True}
    calls = []
    monkeypatch.setattr(bootstrap, "missing", lambda: ["tzdata"] if state["missing"] else [])
    def pip(*args):
        calls.append(args)
        if args[0] == "wheel":
            return 0
        success = offline if "--no-index" in args else online
        state["missing"] = not success
        return offline_rc if "--no-index" in args else (0 if success else 1)
    monkeypatch.setattr(bootstrap, "pip", pip)
    assert bootstrap.main() == expected
    assert all("-r" in c for c in calls)
    assert any(c[0] == "install" and "--no-index" not in c for c in calls) == (not offline)
    assert (tmp_path / "stamp").exists() == (expected == 0)
