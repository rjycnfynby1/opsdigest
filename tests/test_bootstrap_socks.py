from opsdigest import bootstrap


def test_socks_bootstrap_uses_local_wheel_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'WHEELS', tmp_path)
    monkeypatch.setattr(bootstrap, '_pip_log', lambda: tmp_path / 'pip.log')
    (tmp_path / 'PySocks-1.7.1-py3-none-any.whl').touch()
    calls = []
    def run(cmd, stdout, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            stdout.write('Missing dependencies for SOCKS support\n')
            stdout.flush()
            return 1
        return 0
    monkeypatch.setattr(bootstrap.subprocess, 'call', run)
    assert bootstrap.pip('install', '-r', 'requirements.txt') == 0
    assert len(calls) == 3
    assert '--no-index' in calls[1] and '--no-deps' in calls[1]
    assert calls[0] == calls[2]


def test_unrelated_failure_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, '_pip_log', lambda: tmp_path / 'pip.log')
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, 'call', lambda *a, **kw: calls.append(a) or 1)
    assert bootstrap.pip('install', 'example') == 1
    assert len(calls) == 1
