"""Обновление OpsDigest из GitHub одной кнопкой: проверить → скачать → проверить → установить → перезапустить.

Откуда берётся новая версия (по порядку):
  1. Папка программы — клон GitHub (в .git адрес нашего репозитория) и установлен git → git pull (--ff-only).
  2. Архив ветки через API GitHub. Репозиторий закрытый, поэтому нужен токен «только чтение» (Fine-grained token,
     Contents: Read-only, только этот репозиторий) — он хранится в config.json рабочей папки (зашифрован защитой).
  3. Архив, скачанный вручную в браузере (GitHub → Code → Download ZIP) — если из программы GitHub недоступен.

Перед заменой архив проверяется: цел, внутри OpsDigest, весь код компилируется. Старые версии заменяемых файлов
кладутся в %LOCALAPPDATA%\\OpsDigest\\update\\backup-…; рабочая папка, config, сессии, local_ai\\tasks не трогаются.
Удаляются только файлы, которые прошлое обновление само поставило и которых в новой версии больше нет.

Перезапуск: окно вызывает /api/update/restart → сервер запускает помощника (python -m opsdigest.updater --restart),
сам закрывается; помощник дожидается выхода, запускает OpsDigest.bat (он доставит новые пакеты из wheels) с тем же
портом и ключом окна — открытое окно само переподключается. Если новая версия не отвечает 3 минуты — помощник
возвращает прежнюю и запускает её. Вручную: python -m opsdigest.updater --rollback.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__, logs

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "rjycnfynby1/mypunchagent"
DEFAULT_BRANCH = "main"
VERSION_FILE = ".opsdigest-version.json"
MANIFEST = ".opsdigest-files.json"
MAX_ZIP = 150 * 2**20
REQUIRED = ("OpsDigest.bat", "opsdigest/__init__.py", "opsdigest/app.py", "opsdigest/ui/server.py", "requirements.txt")
# никогда не трогаем: служебное git/окружения, данные пользователя, задачи воркера
PROTECT_DIRS = (".git", ".venv", "venv", "data", "reports", "media", "local_ai/tasks", "local_ai/agent", ".pytest_cache")
PROTECT_NAMES = {"config.json", "config.toml", ".env", "probe-links.txt", VERSION_FILE, MANIFEST}
PROTECT_SUFFIXES = (".session", ".session-journal", ".pyc", ".pyo", ".odx", ".part")
START_WAIT = 180          # сколько ждать, что новая версия ответит, прежде чем вернуть старую
CHECK_TIMEOUT = 20        # проверка не должна надолго подвешивать окно программы
log = logs.get("update")
Progress = Callable[[str], None]


class UpdateError(RuntimeError):
    """Понятная пользователю причина, почему обновиться не получилось."""


def runtime_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    d = Path(base) / "OpsDigest" / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings(raw: dict | None = None) -> dict:
    up = dict((raw or {}).get("update") or {})
    repo = str(up.get("repo") or DEFAULT_REPO).strip().strip("/")
    repo = repo.removeprefix("https://github.com/").removesuffix(".git")
    return {"repo": repo, "branch": str(up.get("branch") or DEFAULT_BRANCH).strip(),
            "token": str(up.get("token") or os.environ.get("OPSDIGEST_UPDATE_TOKEN") or "").strip()}


def _protected(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("/")
    if any(rel == d or rel.startswith(d + "/") for d in PROTECT_DIRS):
        return True
    if "__pycache__/" in rel or rel.endswith("/__pycache__"):
        return True
    name = rel.rsplit("/", 1)[-1]
    return name in PROTECT_NAMES or name.endswith(PROTECT_SUFFIXES)


# ================================================================ git
def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _git(root: Path, *args: str, timeout: float = 90) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout, env=env,
                          creationflags=_no_window(), encoding="utf-8", errors="replace")


def git_mode(root: Path = ROOT, repo: str = DEFAULT_REPO) -> bool:
    """Папка программы — клон нашего репозитория (не локальные снимки воркера) и git есть."""
    if not (root / ".git").exists() or not shutil.which("git"):
        return False
    try:
        dotgit = root / ".git"
        git_dir = dotgit
        if dotgit.is_file():
            marker = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            if not marker.lower().startswith("gitdir:"):
                return False
            git_dir = (root / marker.split(":", 1)[1].strip()).resolve()
        config = git_dir / "config"
        common = git_dir / "commondir"
        if not config.exists() and common.exists():
            config = (git_dir / common.read_text(encoding="utf-8", errors="replace").strip() / "config").resolve()
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(config, encoding="utf-8")
        url = cfg.get('remote "origin"', "url", fallback="").strip().lower().removesuffix(".git").rstrip("/")
    except (OSError, configparser.Error):
        return False
    return url.endswith(repo.lower())


def _git_head(root: Path, ref: str = "HEAD") -> str | None:
    try:
        r = _git(root, "rev-parse", ref, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _git_log(root: Path, rng: str, n: int = 30) -> list[dict]:
    r = _git(root, "log", f"-n{n}", "--format=%H%x09%cI%x09%s", rng, timeout=30)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append({"sha": parts[0], "date": parts[1], "title": parts[2]})
    return out


# ================================================================ что стоит сейчас
def local_info(root: Path = ROOT, repo: str = DEFAULT_REPO) -> dict:
    info = {"version": __version__, "sha": None, "date": "", "title": "", "method": "unknown", "root": str(root)}
    if git_mode(root, repo):
        sha = _git_head(root)
        if sha:
            info.update(sha=sha, method="git")
            try:
                r = _git(root, "log", "-1", "--format=%cI%x09%s", timeout=15)
                d, _, t = r.stdout.strip().partition("\t")
                info.update(date=d, title=t)
            except (OSError, subprocess.SubprocessError):
                pass
            return info
    try:
        v = json.loads((root / VERSION_FILE).read_text(encoding="utf-8"))
        info.update(sha=v.get("sha"), date=v.get("date") or "", title=v.get("title") or "", method="zip",
                    installed=v.get("installed") or "")
    except (OSError, ValueError):
        pass
    return info


def last_result() -> dict:
    try:
        return json.loads((runtime_dir() / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_last(data: dict) -> None:
    p = runtime_dir() / "last.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ================================================================ GitHub API
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 — переход делаем сами, без ключа
        return None


def _http(url: str, token: str = "", accept: str = "application/vnd.github+json", timeout: float = 30,
          limit: int = 5 * 2**20) -> bytes:
    """GET с ключом GitHub; переадресацию на codeload выполняем сами и уже без ключа."""
    opener = urllib.request.build_opener(_NoRedirect)
    headers = {"Accept": accept, "User-Agent": f"OpsDigest-updater/{__version__}", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for _ in range(4):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as r:
                data = r.read(limit + 1)
                if len(data) > limit:
                    raise UpdateError(f"ответ GitHub больше {limit // 2**20} МБ — что-то не так")
                return data
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                headers.pop("Authorization", None)
                continue
            raise
    raise UpdateError("GitHub слишком много раз переадресовал запрос")


def _api_error(e: Exception, token: str) -> UpdateError:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404 and not token:
            return UpdateError("репозиторий закрытый — нужен токен GitHub «только чтение» (как его получить — в окне "
                               "обновления) или обновите из архива, скачанного в браузере")
        if e.code in (401, 404):
            return UpdateError("GitHub не принял токен: он неверный, просрочен или не даёт доступа к этому репозиторию "
                               "(нужно Contents: Read-only)")
        if e.code == 403:
            return UpdateError("GitHub ограничил запросы (403) — попробуйте позже или задайте токен")
        return UpdateError(f"GitHub ответил ошибкой {e.code}")
    if isinstance(e, UpdateError):
        return e
    return UpdateError(f"GitHub недоступен из программы ({type(e).__name__}: {e}). Скачайте архив в браузере "
                       "(GitHub → Code → Download ZIP) и нажмите «Установить из скачанного архива»")


def _api_commits(repo: str, branch: str, token: str, n: int = 30) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/commits?sha={urllib.parse.quote(branch)}&per_page={n}"
    try:
        data = json.loads(_http(url, token, timeout=CHECK_TIMEOUT).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, UpdateError) as e:
        raise _api_error(e, token) from None
    out = []
    for c in data if isinstance(data, list) else []:
        cm = c.get("commit") or {}
        out.append({"sha": c.get("sha") or "", "date": ((cm.get("committer") or {}).get("date") or ""),
                    "title": (cm.get("message") or "").split("\n", 1)[0]})
    if not out:
        raise UpdateError(f"в ветке {branch} нет коммитов (проверьте имя ветки)")
    return out


def check(raw: dict | None = None, root: Path = ROOT) -> dict:
    """Есть ли новая версия: {"local", "remote", "behind", "changes", "method", "error"}."""
    s = settings(raw)
    local = local_info(root, s["repo"])
    res: dict = {"local": local, "remote": None, "behind": False, "changes": [], "method": "", "error": "",
                 "repo": s["repo"], "branch": s["branch"], "token_set": bool(s["token"])}
    if local["method"] == "git":
        try:
            r = _git(root, "fetch", "--quiet", "origin", s["branch"], timeout=CHECK_TIMEOUT)
            if r.returncode == 0:
                remote = _git_head(root, f"origin/{s['branch']}")
                changes = _git_log(root, f"HEAD..origin/{s['branch']}")
                head = _git_log(root, f"origin/{s['branch']}", n=1) if remote else []
                res.update(method="git", remote=head[0] if head else {"sha": remote, "date": "", "title": ""},
                           changes=changes, behind=bool(changes))
                return res
            res["error"] = "git fetch: " + (r.stderr.strip().splitlines() or ["ошибка"])[-1][:200]
        except (OSError, subprocess.SubprocessError) as e:
            res["error"] = f"git недоступен: {type(e).__name__}"
    try:
        commits = _api_commits(s["repo"], s["branch"], s["token"])
    except UpdateError as e:
        res["error"] = (res["error"] + "; " if res["error"] else "") + str(e)
        res["need_token"] = not s["token"]
        return res
    res.update(method="api", remote=commits[0])
    shas = [c["sha"] for c in commits]
    if local.get("sha") and local["sha"] in shas:
        res["changes"] = commits[:shas.index(local["sha"])]
    elif local.get("sha"):
        res["changes"], res["unknown_base"] = commits[:15], True
    else:
        res["changes"], res["unknown_base"] = commits[:5], True
    res["behind"] = commits[0]["sha"] != local.get("sha")
    return res


# ================================================================ архив: найти, проверить, распаковать
def find_zips() -> list[dict]:
    """Архивы программы в «Загрузках» (скачанные в браузере), новые первыми."""
    out = []
    for base in {Path.home() / "Downloads", Path.home() / "Загрузки"}:
        try:
            files = sorted(base.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:25]
        except OSError:
            continue
        for p in files:
            try:
                with zipfile.ZipFile(p) as zf:
                    _top(zf)
                    sha = _zip_sha(zf)
            except (zipfile.BadZipFile, OSError, UpdateError):
                continue
            st = p.stat()
            out.append({"path": str(p), "name": p.name, "size": st.st_size, "sha": sha,
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")})
    return out


def _top(zf: zipfile.ZipFile) -> str:
    """Общая верхняя папка архива GitHub («mypunchagent-main/») и проверка, что внутри OpsDigest."""
    names = [n for n in zf.namelist() if n and not n.endswith("/")]
    if not names:
        raise UpdateError("архив пустой")
    for n in names:
        if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/") or ":" in n:
            raise UpdateError("в архиве недопустимые пути — не устанавливаю")
    firsts = {n.split("/", 1)[0] for n in names}
    top = (firsts.pop() + "/") if len(firsts) == 1 and all("/" in n for n in names) else ""
    have = set(names)
    miss = [r for r in REQUIRED if top + r not in have]
    if miss:
        raise UpdateError("это не архив OpsDigest: нет " + ", ".join(miss))
    return top


def _zip_sha(zf: zipfile.ZipFile) -> str | None:
    """GitHub пишет номер коммита в комментарий архива."""
    c = (zf.comment or b"").decode("ascii", "ignore").strip()
    return c if len(c) == 40 and all(ch in "0123456789abcdef" for ch in c.lower()) else None


def _stage(zf: zipfile.ZipFile, top: str, dest: Path) -> list[str]:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    rels = []
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(top):
            continue
        rel = info.filename[len(top):]
        if not rel or _protected(rel):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        rels.append(rel)
    # весь код должен компилироваться — иначе после перезапуска окно не откроется
    bad = []
    for rel in rels:
        if rel.endswith(".py"):
            try:
                compile((dest / rel).read_bytes(), rel, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as e:
                bad.append(f"{rel}: {e}")
    if bad:
        raise UpdateError("в новой версии ошибка в коде, не устанавливаю: " + "; ".join(bad[:3]))
    return sorted(rels)


def _same(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _apply(stage: Path, rels: list[str], root: Path, backup: Path, progress: Progress) -> dict:
    """Скопировать новые файлы поверх, старые — в резервную копию; удалить то, что ставили мы и чего больше нет."""
    st = {"changed": [], "added": [], "removed": []}
    try:
        old_manifest = set(json.loads((root / MANIFEST).read_text(encoding="utf-8")).get("files") or [])
    except (OSError, ValueError):
        old_manifest = set()
    new = set(rels)
    for rel in rels:
        src, dst = stage / rel, root / rel
        if dst.exists():
            if _same(src, dst):
                continue
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup / rel)
            st["changed"].append(rel)
        else:
            st["added"].append(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".upd")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    gone = {r for r in old_manifest if r not in new and not _protected(r)}
    if not old_manifest:  # первое обновление этим способом: убираем только устаревшие модули программы
        code = root / "opsdigest"
        if code.is_dir():
            for p in code.rglob("*.py"):
                rel = p.relative_to(root).as_posix()
                if rel not in new and not _protected(rel):
                    gone.add(rel)
    for rel in sorted(gone):
        p = root / rel
        if p.is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, backup / rel)
            p.unlink()
            st["removed"].append(rel)
    for name in (MANIFEST, VERSION_FILE):  # прежние служебные файлы — тоже в резервную копию
        if (root / name).exists():
            shutil.copy2(root / name, backup / name)
    progress(f"Файлов изменено: {len(st['changed'])}, добавлено: {len(st['added'])}, удалено: {len(st['removed'])}")
    return st


def _download(s: dict, sha: str, progress: Progress) -> bytes:
    progress(f"Скачиваю версию {sha[:7]} с GitHub…")
    url = f"https://api.github.com/repos/{s['repo']}/zipball/{sha}"
    try:
        data = _http(url, s["token"], accept="application/vnd.github+json", timeout=120, limit=MAX_ZIP)
    except (urllib.error.URLError, OSError, UpdateError) as e:
        raise _api_error(e, s["token"]) from None
    progress(f"  скачано {len(data) / 2**20:.1f} МБ")
    return data


def install(raw: dict | None = None, *, zip_path: str | None = None, root: Path = ROOT,
            progress: Progress = print) -> dict:
    """Поставить новую версию. zip_path — архив, скачанный вручную; иначе git или API GitHub."""
    s = settings(raw)
    local = local_info(root, s["repo"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    reqs_before = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""

    if not zip_path and local["method"] == "git":
        progress("Папка программы — клон GitHub: обновляю через git…")
        st = _git(root, "status", "--porcelain", "--untracked-files=no", timeout=30)
        if st.stdout.strip():
            raise UpdateError("в папке программы есть изменённые файлы (git status) — git не будет их перезаписывать. "
                              "Сохраните их или обновите из архива")
        r = _git(root, "pull", "--ff-only", "origin", s["branch"], timeout=300)
        if r.returncode != 0:
            raise UpdateError("git pull не прошёл: " + (r.stderr.strip().splitlines() or ["ошибка"])[-1][:300])
        new = _git_head(root) or ""
        if new == local.get("sha"):
            return {"updated": False, "message": "Уже последняя версия.", "sha": new}
        res = {"updated": True, "method": "git", "from": local.get("sha"), "to": new, "at": stamp,
               "restart": True, "changes": _git_log(root, f"{local.get('sha')}..{new}") if local.get("sha") else []}
    else:
        if zip_path:
            p = Path(zip_path.strip().strip('"'))
            if not p.is_file():
                raise UpdateError(f"файл не найден: {p}")
            if p.stat().st_size > MAX_ZIP:
                raise UpdateError("архив слишком большой — это точно OpsDigest?")
            data = p.read_bytes()
            progress(f"Архив: {p.name} ({len(data) / 2**20:.1f} МБ)")
            target = None
        else:
            info = check(raw, root)
            if info.get("error") and not info.get("remote"):
                raise UpdateError(info["error"])
            target = info["remote"]
            if not info["behind"]:
                return {"updated": False, "message": "Уже последняя версия.", "sha": target["sha"]}
            data = _download(s, target["sha"], progress)
        import io
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise UpdateError("архив повреждён (не открывается как ZIP)") from None
        top = _top(zf)
        sha = (target or {}).get("sha") or _zip_sha(zf)
        if sha and sha == local.get("sha"):
            return {"updated": False, "message": "Эта версия уже установлена.", "sha": sha}
        progress("Проверяю архив и код новой версии…")
        stage = runtime_dir() / f"new-{stamp}"
        rels = _stage(zf, top, stage)
        progress(f"  файлов: {len(rels)}, код компилируется")
        backup = runtime_dir() / f"backup-{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        st = _apply(stage, rels, root, backup, progress)
        shutil.rmtree(stage, ignore_errors=True)
        ver = {"sha": sha, "date": (target or {}).get("date", ""), "title": (target or {}).get("title", ""),
               "installed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "method": "zip" if zip_path else "api"}
        (root / VERSION_FILE).write_text(json.dumps(ver, ensure_ascii=False, indent=1), encoding="utf-8")
        (root / MANIFEST).write_text(json.dumps({"files": rels}, ensure_ascii=False), encoding="utf-8")
        _prune_backups()
        res = {"updated": True, "method": ver["method"], "from": local.get("sha"), "to": sha, "at": stamp,
               "backup": str(backup), "added": st["added"], "restart": True,
               "files": len(st["changed"]) + len(st["added"]) + len(st["removed"])}
    reqs_after = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    if reqs_after != reqs_before:
        res["new_packages"] = True
        progress("Изменился список пакетов — новые поставятся из папки wheels при перезапуске.")
    _save_last({**{k: v for k, v in res.items() if k != "changes"}, "started": None})
    log.info("обновление установлено: %s → %s (%s)", (res.get("from") or "?")[:7], (res.get("to") or "?")[:7],
             res.get("method"))
    progress("✓ Новая версия установлена. Нужен перезапуск программы.")
    return res


def _prune_backups(keep: int = 5) -> None:
    olds = sorted(runtime_dir().glob("backup-*"))
    for p in olds[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


def rollback(root: Path = ROOT, progress: Progress = print) -> dict:
    """Вернуть версию до последнего обновления."""
    last = last_result()
    if not last.get("updated"):
        raise UpdateError("нечего откатывать: последнего обновления нет")
    if last.get("method") == "git":
        if not last.get("from"):
            raise UpdateError("неизвестна прежняя версия")
        r = _git(root, "reset", "--hard", last["from"], timeout=60)
        if r.returncode != 0:
            raise UpdateError("git reset не прошёл: " + r.stderr.strip()[-200:])
    else:
        backup = Path(last.get("backup") or "")
        if not backup.is_dir():
            raise UpdateError("резервная копия не найдена")
        for f in backup.rglob("*"):
            if f.is_file():
                rel = f.relative_to(backup)
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, root / rel)
        for rel in last.get("added") or []:
            p = root / rel
            if p.is_file() and not _protected(rel):
                p.unlink()
        if not (backup / VERSION_FILE).exists():
            (root / VERSION_FILE).unlink(missing_ok=True)
        if not (backup / MANIFEST).exists():
            (root / MANIFEST).unlink(missing_ok=True)
    last.update(rolled_back=True, rolled_at=datetime.now().strftime("%d.%m.%Y %H:%M"))
    _save_last(last)
    log.info("откат обновления: вернул %s", (last.get("from") or "?")[:7])
    progress("✓ Прежняя версия возвращена.")
    return last


# ================================================================ перезапуск
def schedule_restart(port: int, token: str, root: Path = ROOT, pid: int = 0) -> None:
    """Запустить помощника, который дождётся выхода этого процесса (под сторожем — выхода сторожа, pid)
    и запустит программу снова."""
    env = {**os.environ, "OPSDIGEST_HELPER_TOKEN": token}
    for k in ("OPSDIGEST_SUPERVISED", "OPSDIGEST_SUPERVISOR_PID"):
        env.pop(k, None)
    args = [sys.executable, "-m", "opsdigest.updater", "--restart", "--pid", str(pid or os.getpid()), "--port", str(port),
            "--root", str(root)]
    kw: dict = {"cwd": str(root), "env": env, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    log.info("перезапуск: помощник запущен")


def _wait_exit(pid: int, timeout: float = 60) -> bool:
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return True
        try:
            return k.WaitForSingleObject(h, int(timeout * 1000)) == 0
        finally:
            k.CloseHandle(h)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.3)
    return False


def _alive(port: int, token: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-Token": token})
        with opener.open(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception:  # noqa: BLE001
        return False


def launch(root: Path, port: int, token: str, window: bool = False) -> None:
    """Запустить программу как OpsDigest.bat (пакеты из wheels + окно), с тем же портом и ключом окна."""
    env = {**os.environ, "OPSDIGEST_PORT": str(port), "OPSDIGEST_TOKEN": token, "PYTHONPATH": str(root)}
    env.pop("OPSDIGEST_HELPER_TOKEN", None)
    if not window:
        env["OPSDIGEST_NO_WINDOW"] = "1"  # открытое окно переподключится само
    else:
        env.pop("OPSDIGEST_NO_WINDOW", None)
    if os.name == "nt" and (root / "OpsDigest.bat").exists():
        subprocess.Popen(["cmd", "/c", str(root / "OpsDigest.bat")], cwd=str(root), env=env,
                         creationflags=0x08000000 | 0x00000200, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    subprocess.call([sys.executable, "-m", "opsdigest.bootstrap"], cwd=str(root), env=env)
    subprocess.Popen([sys.executable, "-m", "opsdigest.app"], cwd=str(root), env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restart_helper(pid: int, port: int, token: str, root: Path = ROOT, wait: float = START_WAIT) -> bool:
    logs.setup("update")
    if not _wait_exit(pid, 60):
        log.warning("перезапуск: прежний процесс не завершился за минуту — запускаю всё равно")
    time.sleep(0.5)
    launch(root, port, token)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if _alive(port, token):
            last = last_result()
            if last:
                last["started"] = True
                _save_last(last)
            log.info("перезапуск: новая версия отвечает")
            return True
        time.sleep(2)
    last = last_result()
    fresh = last.get("updated") and not last.get("rolled_back") and last.get("started") is None
    log.warning("перезапуск: новая версия не ответила за %d с%s", wait, " — возвращаю прежнюю" if fresh else "")
    if fresh:
        try:
            rollback(root, progress=lambda s: None)
            last = last_result()
            last.update(started=False, error="новая версия не запустилась — вернул прежнюю")
            _save_last(last)
        except UpdateError as e:
            log.error("откат не удался: %s", e)
            return False
        launch(root, port, token, window=True)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m opsdigest.updater", description="Обновление OpsDigest из GitHub")
    ap.add_argument("--check", action="store_true", help="проверить, есть ли новая версия")
    ap.add_argument("--install", nargs="?", const="", metavar="ZIP", help="установить (из GitHub или из архива)")
    ap.add_argument("--rollback", action="store_true", help="вернуть версию до последнего обновления")
    ap.add_argument("--token", default="", help="токен GitHub (только чтение)")
    ap.add_argument("--restart", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pid", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    root = Path(a.root)
    raw = {"update": {"token": a.token}} if a.token else None
    try:
        if a.restart:
            token = os.environ.get("OPSDIGEST_HELPER_TOKEN") or ""
            return 0 if restart_helper(a.pid, a.port, token, root) else 1
        if a.rollback:
            rollback(root)
            return 0
        if a.install is not None:
            r = install(raw, zip_path=a.install or None, root=root)
            print(r.get("message") or f"Готово: {(r.get('from') or '?')[:7]} → {(r.get('to') or '?')[:7]}. "
                  "Запустите OpsDigest.bat заново.")
            return 0
        info = check(raw, root)
        loc = info["local"]
        print(f"Установлено: {(loc.get('sha') or 'неизвестно')[:7]} {loc.get('title', '')}")
        if info.get("remote"):
            print(f"В GitHub:    {info['remote']['sha'][:7]} {info['remote'].get('title', '')}")
        print("Есть обновление." if info["behind"] else ("Ошибка: " + info["error"] if info["error"] else "Последняя версия."))
        return 0
    except UpdateError as e:
        print(f"[!] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
