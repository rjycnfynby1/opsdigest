from unittest.mock import Mock

from opsdigest import app


def test_embedded_launch_uses_shell_not_external_browser(monkeypatch):
    monkeypatch.setenv('OPSDIGEST_EMBEDDED', '1')
    monkeypatch.delenv('OPSDIGEST_NO_WINDOW', raising=False)
    monkeypatch.setattr(app.Path, 'is_file', lambda _: True)
    launch = Mock()
    monkeypatch.setattr(app.subprocess, 'Popen', launch)
    monkeypatch.setattr(app.webbrowser, 'open', lambda _: (_ for _ in ()).throw(AssertionError()))
    app.open_window('http://127.0.0.1:9999/#t=test')
    args = launch.call_args.args[0]
    assert args[0].endswith('OpsDigest.Embedded.exe')
    assert args[1] == 'http://127.0.0.1:9999/#t=test'


def test_missing_shell_does_not_silently_open_external_browser(monkeypatch):
    import pytest
    monkeypatch.setenv('OPSDIGEST_EMBEDDED', '1')
    monkeypatch.delenv('OPSDIGEST_NO_WINDOW', raising=False)
    monkeypatch.setattr(app.Path, 'is_file', lambda _: False)
    with pytest.raises(RuntimeError, match='build.ps1'):
        app.open_window('http://127.0.0.1:9999/')
