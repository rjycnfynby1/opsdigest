"""Обновление из GitHub: проверка версии, установка из архива и через API, защита файлов пользователя, откат,
перезапуск с возвратом прежней версии, кнопки окна."""
import io
import json
import zipfile

import pytest

from opsdigest import updater

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


def make_root(tmp_path):
    root = tmp_path / "MyAgent"
    files = {
        "OpsDigest.bat": "@echo old\r\n",
        "requirements.txt": "telethon>=1.36,<2\n",
        "opsdigest/__init__.py": "__version__ = '0.1'\n",
        "opsdigest/app.py": "print('old app')\n",
        "opsdigest/ui/server.py": "X = 1\n",
        "opsdigest/old_module.py": "OLD = True\n",
        "data/opsdigest.db": "база пользователя",
        "local_ai/tasks/1.task.md": "задача воркера",
        "config.toml": "секреты",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def github_zip(files: dict, sha=SHA_NEW, top="mypunchagent-main/") -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for rel, text in files.items():
            zf.writestr(top + rel, text)
        zf.comment = sha.encode()
    return bio.getvalue()


NEW_FILES = {
    "OpsDigest.bat": "@echo new\r\n",
    "requirements.txt": "telethon>=1.36,<2\npyaes>=1.6\n",
    "opsdigest/__init__.py": "__version__ = '0.3'\n",
    "opsdigest/app.py": "print('new app')\n",
    "opsdigest/ui/server.py": "X = 1\n",
    "opsdigest/updater.py": "Y = 2\n",
    "data/evil.txt": "архив не должен трогать данные",
    "config.toml": "архив не должен трогать настройки",
}


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)  # без git: архивный способ


def test_install_from_zip_and_rollback(tmp_path):
    root = make_root(tmp_path)
    z = tmp_path / "mypunchagent-main.zip"
    z.write_bytes(github_zip(NEW_FILES))
    lines = []
    r = updater.install(None, zip_path=str(z), root=root, progress=lines.append)
    assert r["updated"] and r["to"] == SHA_NEW and r["restart"] and r["new_packages"]
    assert (root / "opsdigest/app.py").read_text() == "print('new app')\n"
    assert (root / "opsdigest/updater.py").exists()
    assert not (root / "opsdigest/old_module.py").exists()           # устаревший модуль убран
    assert (root / "data/opsdigest.db").read_text(encoding="utf-8") == "база пользователя"
    assert not (root / "data/evil.txt").exists()
    assert (root / "config.toml").read_text(encoding="utf-8") == "секреты"
    assert (root / "local_ai/tasks/1.task.md").exists()
    assert updater.local_info(root)["sha"] == SHA_NEW
    backup = updater.Path(r["backup"])
    assert (backup / "opsdigest/app.py").read_text() == "print('old app')\n"
    assert (backup / "opsdigest/old_module.py").exists()
    assert any("Нужен перезапуск" in l for l in lines)
    # та же версия второй раз — не ставится
    assert updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)["updated"] is False
    # откат
    updater.rollback(root, progress=lambda s: None)
    assert (root / "opsdigest/app.py").read_text() == "print('old app')\n"
    assert (root / "opsdigest/old_module.py").exists() and not (root / "opsdigest/updater.py").exists()
    assert updater.local_info(root)["sha"] is None and updater.last_result()["rolled_back"]


def test_second_update_removes_only_own_files(tmp_path):
    root = make_root(tmp_path)
    z = tmp_path / "a.zip"
    z.write_bytes(github_zip(NEW_FILES))
    updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    (root / "мои заметки.txt").write_text("личное", encoding="utf-8")
    newer = {k: v for k, v in NEW_FILES.items() if k != "opsdigest/updater.py"}
    newer["opsdigest/app.py"] = "print('newer')\n"
    z.write_bytes(github_zip(newer, sha="c" * 40))
    r = updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    assert r["updated"] and not (root / "opsdigest/updater.py").exists()
    assert (root / "мои заметки.txt").exists()


def test_bad_archives_change_nothing(tmp_path):
    root = make_root(tmp_path)
    before = (root / "opsdigest/app.py").read_text()
    broken = dict(NEW_FILES, **{"opsdigest/app.py": "def oops(:\n"})
    z = tmp_path / "b.zip"
    z.write_bytes(github_zip(broken))
    with pytest.raises(updater.UpdateError, match="ошибка в коде"):
        updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    z.write_bytes(github_zip({"README.md": "x"}))
    with pytest.raises(updater.UpdateError, match="не архив OpsDigest"):
        updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for rel, text in NEW_FILES.items():
            zf.writestr("top/" + rel, text)
        zf.writestr("top/../../evil.py", "x")
    z.write_bytes(bio.getvalue())
    with pytest.raises(updater.UpdateError, match="недопустимые пути"):
        updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    assert (root / "opsdigest/app.py").read_text() == before


def fake_github(monkeypatch, commits, zipdata=None, need_token=False):
    calls = []

    def http(url, token="", accept="", timeout=30, limit=0):
        calls.append((url, token))
        if need_token and not token:
            raise updater.urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "/zipball/" in url:
            return zipdata
        return json.dumps([{"sha": s, "commit": {"message": t + "\n\nподробно", "committer": {"date": "2026-11-06T10:00:00Z"}}}
                           for s, t in commits]).encode()
    monkeypatch.setattr(updater, "_http", http)
    return calls


def test_check_and_install_via_api(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    (root / updater.VERSION_FILE).write_text(json.dumps({"sha": SHA_OLD}), encoding="utf-8")
    calls = fake_github(monkeypatch, [(SHA_NEW, "Мессенджер"), ("d" * 40, "Кнопка обновления"), (SHA_OLD, "старое")],
                        github_zip(NEW_FILES, sha=SHA_NEW), need_token=True)
    info = updater.check(None, root)
    assert not info["behind"] and info["need_token"] and "токен" in info["error"]
    raw = {"update": {"token": "github_pat_" + "x" * 30}}
    info = updater.check(raw, root)
    assert info["behind"] and [c["title"] for c in info["changes"]] == ["Мессенджер", "Кнопка обновления"]
    r = updater.install(raw, root=root, progress=lambda s: None)
    assert r["updated"] and r["method"] == "api" and r["to"] == SHA_NEW
    assert any("/zipball/" + SHA_NEW in u and t for u, t in calls)
    assert (root / "opsdigest/app.py").read_text() == "print('new app')\n"
    info = updater.check(raw, root)
    assert not info["behind"] and info["changes"] == []


def test_git_mode_reads_origin_without_starting_git(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    (root / ".git").mkdir()
    (root / ".git/config").write_text(
        '[remote "origin"]\nurl = https://github.com/rjycnfynby1/mypunchagent.git\n', encoding="utf-8")
    monkeypatch.setattr(updater.shutil, "which", lambda name: "git.exe")
    monkeypatch.setattr(updater, "_git", lambda *a, **k: pytest.fail("git must not be started"))
    assert updater.git_mode(root)


def test_restart_helper_rolls_back_when_new_version_silent(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    z = tmp_path / "a.zip"
    z.write_bytes(github_zip(NEW_FILES))
    updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    launches = []
    monkeypatch.setattr(updater, "_wait_exit", lambda pid, timeout=60: True)
    monkeypatch.setattr(updater, "launch", lambda r, port, token, window=False: launches.append(window))
    monkeypatch.setattr(updater, "_alive", lambda port, token: False)
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    assert updater.restart_helper(1, 5000, "t", root, wait=0) is False
    assert launches == [False, True]                    # новая не ответила → прежняя, с окном
    assert (root / "opsdigest/app.py").read_text() == "print('old app')\n"
    last = updater.last_result()
    assert last["rolled_back"] and last["started"] is False and "не запустилась" in last["error"]
    # удачный перезапуск — отметка started
    updater.install(None, zip_path=str(z), root=root, progress=lambda s: None)
    monkeypatch.setattr(updater, "_alive", lambda port, token: True)
    launches.clear()
    assert updater.restart_helper(1, 5000, "t", root, wait=5) is True
    assert launches == [False] and updater.last_result()["started"] is True


def test_find_zips(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "mypunchagent-main.zip").write_bytes(github_zip(NEW_FILES))
    (tmp_path / "Downloads" / "photos.zip").write_bytes(github_zip({"a.jpg": "x"}))
    found = updater.find_zips()
    assert [f["name"] for f in found] == ["mypunchagent-main.zip"] and found[0]["sha"] == SHA_NEW


def test_window_update_buttons(tmp_path, monkeypatch):
    import threading
    import urllib.request
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from opsdigest.ui import server
    restarted = []
    monkeypatch.setattr(updater, "schedule_restart", lambda port, token, root=None, pid=0: restarted.append(port))
    app = server.App(idle_exit=False)
    srv = server.make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", headers={"X-Token": app.token,
                                     "Content-Type": "application/json"},
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    st = call("/api/update/status")
    assert st["ok"] and st["repo"] == updater.DEFAULT_REPO and st["can_save_token"] is False
    assert "рабочую папку" in call("/api/update/token", {"token": "github_pat_" + "x" * 30})["error"]
    assert call("/api/update/restart", {})["restarting"] and restarted == [app.port]
    srv.server_close()
