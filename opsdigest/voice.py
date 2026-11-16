"""Голосовой ввод на локальной модели — целиком внутри OpsDigest, без LM Studio и других серверов.

Модель: GigaAM v3 e2e (SberDevices, лицензия MIT) — лучшая открытая модель для русской речи; варианты e2e сразу
ставят пунктуацию и пишут числа цифрами. Формат ONNX (istupakov/gigaam-v3-onnx на Hugging Face), квантованная int8 —
около 300 МБ на диске и около 1 ГБ памяти; на 4 ядрах i5-1240P фраза в 10 секунд распознаётся примерно за секунду.
Запускается через onnx-asr (чистый Python, препроцессор уже внутри — ONNX) и onnxruntime (CPU).

Где лежит (на этом компьютере, не в рабочей папке): %LOCALAPPDATA%\\OpsDigest\\voice\\
  models\\<модель>\\  — файлы модели;
  wheels\\          — пакеты numpy, onnxruntime, onnx-asr и их зависимости для этого Python (pip ставит их офлайн).
Установка — три пути (окно: «Настройки → Голосовой ввод»):
  1) «Скачать»: Hugging Face и PyPI напрямую или через прокси Windows; можно указать зеркала компании;
  2) архив OpsDigest-voice.zip, собранный на домашнем ПК (tools\\voice_pack.bat) и принесённый любым путём
     (Telegram «Избранное», флешка) — программа сама находит его в «Загрузках» и «Загрузках\\Telegram Desktop»;
  3) папка с файлами модели, указанная вручную (пакеты — из той же папки, если в ней есть .whl).
Распознавание — в отдельном процессе (python -m opsdigest.voice --worker): модель грузится один раз и держится в
памяти voice.idle_minutes минут после последней фразы; сбой onnxruntime не роняет программу. Звук записывает окно
(микрофон браузера Edge), сюда приходит PCM 16 кГц 16 бит моно; на диск звук не пишется.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import logs

log = logs.get("voice")
Progress = Callable[[str], None]

MODELS = {
    "gigaam-v3-e2e-ctc": {"title": "GigaAM v3 e2e CTC — с пунктуацией, быстрая (рекомендуется)", "repo": "istupakov/gigaam-v3-onnx",
                          "prefix": "v3_e2e_ctc", "mb": 310},
    "gigaam-v3-e2e-rnnt": {"title": "GigaAM v3 e2e RNN-T — с пунктуацией, чуть точнее и медленнее", "repo": "istupakov/gigaam-v3-onnx",
                           "prefix": "v3_e2e_rnnt", "mb": 320},
    "gigaam-v3-ctc": {"title": "GigaAM v3 CTC — без пунктуации, самая быстрая", "repo": "istupakov/gigaam-v3-onnx",
                      "prefix": "v3_ctc", "mb": 300},
}
DEFAULT_MODEL = "gigaam-v3-e2e-ctc"
PACKAGES = ["onnx-asr", "onnxruntime", "numpy"]
ARCHIVE_RE = re.compile(r"(?i)^opsdigest[-_ ]voice.*\.zip$")
COMMANDS = [(re.compile(r"(?i)[,.]?\s*\b(?:с\s+)?нов(?:ая|ой)\s+строк[аи]\b[,.]?\s*"), "\n"),
            (re.compile(r"(?i)[,.]?\s*\bнов(?:ый|ого)\s+абзац[а]?\b[,.]?\s*"), "\n\n")]


class VoiceError(RuntimeError):
    """Понятная пользователю причина, почему голосовой ввод сейчас не работает."""


# ---------------------------------------------------------------- места и настройки
def base_dir() -> Path:
    base = os.environ.get("OPSDIGEST_VOICE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache"), "OpsDigest", "voice")
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_of(raw: dict | None) -> dict:
    v = dict((raw or {}).get("voice") or {})
    model = v.get("model") if v.get("model") in MODELS or v.get("model") == "fake" else DEFAULT_MODEL
    return {"enabled": v.get("enabled", True) is not False, "model": model, "path": str(v.get("path") or ""),
            "quantization": "" if v.get("quantization") == "none" else "int8",
            "threads": max(1, min(int(v.get("threads") or 4), 16)), "idle_minutes": max(1, min(int(v.get("idle_minutes") or 10), 240)),
            "hf_mirror": str(v.get("hf_mirror") or "").rstrip("/"), "pypi_index": str(v.get("pypi_index") or ""),
            "replace": [list(x)[:2] for x in v.get("replace") or [] if isinstance(x, (list, tuple)) and len(x) >= 2],
            "commands": v.get("commands", True) is not False, "hotkey": str(v.get("hotkey") or "F9")}


def model_dir(s: dict) -> Path:
    return Path(s["path"]) if s.get("path") else base_dir() / "models" / s["model"]


def wheels_dir() -> Path:
    d = base_dir() / "wheels"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- состояние
_RT_CACHE: dict = {}


def runtime_status(force: bool = False) -> dict:
    """Стоят ли пакеты (проверка в отдельном процессе — сервер не грузит onnxruntime)."""
    if _RT_CACHE and not force and time.time() - _RT_CACHE.get("at", 0) < 300:
        return dict(_RT_CACHE)
    code = ("import json, sys\nout = {}\nfor m in ('numpy', 'onnxruntime', 'onnx_asr'):\n"
            "    try:\n        mod = __import__(m)\n        out[m] = getattr(mod, '__version__', 'есть')\n"
            "    except Exception as e:\n        out[m] = None\n        out[m + '_error'] = type(e).__name__ + ': ' + str(e)[:200]\n"
            "print(json.dumps(out))")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=90,
                           creationflags=_NO_WINDOW)
        info = json.loads((r.stdout or "{}").strip().splitlines()[-1] or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError) as e:
        info = {"error": f"{type(e).__name__}: {e}"[:200]}
    ok = bool(info.get("numpy") and info.get("onnxruntime") and info.get("onnx_asr"))
    _RT_CACHE.clear()
    _RT_CACHE.update({"ok": ok, "packages": info, "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                      "platform": _platform_tag(), "at": time.time()})
    return dict(_RT_CACHE)


def model_status(s: dict) -> dict:
    d = model_dir(s)
    files = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    onnx = [f for f in files if f.endswith(".onnx")]
    size = sum((d / f).stat().st_size for f in files) if files else 0
    ok = bool(onnx) or s["model"] == "fake"
    return {"ok": ok, "dir": str(d), "files": files, "mb": round(size / 2**20), "model": s["model"],
            "title": MODELS.get(s["model"], {}).get("title", s["model"])}


def status(s: dict) -> dict:
    return {"settings": s, "runtime": runtime_status(), "model": model_status(s), "worker": ENGINE.state(),
            "archives": [str(p) for p in find_archives()], "models": {k: v["title"] for k, v in MODELS.items()},
            "dir": str(base_dir())}


# ---------------------------------------------------------------- сеть (прокси Windows, зеркала)
def _opener():
    """urllib сам берёт прокси из настроек Windows (реестр) и переменных HTTPS_PROXY; PAC-скрипты — нет."""
    return urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies()))


def _get(url: str, timeout: float = 60):
    req = urllib.request.Request(url, headers={"User-Agent": "OpsDigest-voice/1.0"})
    return _opener().open(req, timeout=timeout)  # noqa: S310 — Hugging Face / PyPI или зеркало из настроек


def _json(url: str) -> dict | list:
    with _get(url, 60) as r:
        return json.loads(r.read().decode("utf-8"))


def _hf(s: dict) -> str:
    return s.get("hf_mirror") or "https://huggingface.co"


def model_files(s: dict) -> list[dict]:
    """Какие файлы скачать для модели: список репозитория на Hugging Face → файлы с её префиксом (int8, если есть)."""
    m = MODELS[s["model"]]
    base = _hf(s)
    try:
        tree = _json(f"{base}/api/models/{m['repo']}/tree/main")
    except Exception:  # noqa: BLE001 — старые зеркала не знают /tree: берём список из карточки модели
        info = _json(f"{base}/api/models/{m['repo']}")
        tree = [{"path": x.get("rfilename"), "size": None} for x in (info.get("siblings") or []) if isinstance(x, dict)]
    files = [x for x in tree if isinstance(x, dict) and x.get("path") and "/" not in x["path"]]
    pre = m["prefix"]
    mine = [x for x in files if x["path"].startswith(pre + ".") or x["path"].startswith(pre + "_")
            or x["path"].startswith(pre + "-")]
    onnx = [x for x in mine if x["path"].endswith(".onnx") or x["path"].endswith(".onnx.data")]
    want_q = s.get("quantization") == "int8"
    q = [x for x in onnx if "int8" in x["path"]]
    plain = [x for x in onnx if "int8" not in x["path"]]
    chosen = (q if (want_q and q) else plain) or onnx
    other = [x for x in mine if x not in onnx] + [x for x in files if x["path"] in ("config.json",)]
    out = chosen + other
    if not chosen:
        raise VoiceError(f"на {base} в {m['repo']} нет файлов модели {pre}*.onnx")
    return [{"path": x["path"], "size": x.get("size"), "sha256": ((x.get("lfs") or {}).get("oid") or "")} for x in out]


# ---------------------------------------------------------------- ход установки: окно, журнал, проценты
def journal_path() -> Path:
    return logs.log_dir() / "voice-install.log"


def _fmt_s(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60} мин {sec % 60:02d} с" if sec >= 60 else f"{sec} с"


def _mb(n: float) -> str:
    return f"{n / 2**20:.1f} МБ" if n < 10 * 2**20 else f"{n / 2**20:.0f} МБ"


class Track:
    """Ход установки. Строки — в окно (progress) и в журнал %LOCALAPPDATA%\\OpsDigest\\logs\\voice-install.log
    (обезличенный, как остальные журналы); meter(dict) — для полосы в окне: шаг, проценты, скорость, сколько осталось."""

    def __init__(self, progress: Progress = print, meter: Callable[[dict], None] | None = None,
                 cancel: Callable[[], bool] | None = None, steps: int = 1):
        self.progress = progress
        self.meter_cb = meter or (lambda d: None)
        self.cancel = cancel or (lambda: False)
        self.steps = max(1, steps)
        self.n = 0
        self.title = ""
        self.t0 = self.ts = time.monotonic()
        self.state: dict = {}
        self._last_meter = 0.0
        self._speed: list[tuple[float, float]] = []
        try:
            f = journal_path()
            if f.exists() and f.stat().st_size > 2 * 2**20:
                f.replace(f.with_suffix(".old.log"))
            self._f = open(f, "a", encoding="utf-8")  # noqa: SIM115 — открыт на время установки
            self._f.write(f"\n===== {time.strftime('%d.%m.%Y %H:%M:%S')} · Python {sys.version.split()[0]} · "
                          f"{_platform_tag()} =====\n")
        except OSError:
            self._f = None

    def check(self) -> None:
        if self.cancel():
            raise VoiceError("остановлено")

    def line(self, text: str, show: bool = True) -> None:
        if self._f is not None:
            try:
                self._f.write(f"[{time.strftime('%H:%M:%S')} +{int(time.monotonic() - self.t0)}с] {logs.scrub(text)}\n")
                self._f.flush()
            except OSError:
                pass
        if show:
            self.progress(text)

    def step(self, title: str) -> None:
        if self.n:
            self.line(f"  ✓ {self.title}: {_fmt_s(time.monotonic() - self.ts)}", show=False)
        self.n += 1
        self.title, self.ts = title, time.monotonic()
        self.state = {}
        self._speed = []
        self.line(f"Шаг {self.n}/{self.steps}: {title}")
        self.meter(pct=None, detail="", force=True)

    def meter(self, force: bool = False, **kw) -> None:
        self.state.update(kw)
        now = time.monotonic()
        if not force and now - self._last_meter < 0.5:
            return
        self._last_meter = now
        pct = self.state.get("pct")
        overall = ((self.n - 1) + (pct or 0) / 100) / self.steps * 100 if self.n else 0
        self.meter_cb({"step": f"{self.n}/{self.steps}", "title": self.title, "pct": pct, "overall": round(overall, 1),
                       "elapsed": round(now - self.t0), "step_elapsed": round(now - self.ts),
                       **{k: v for k, v in self.state.items() if k != "pct"}})

    def bytes(self, done: float, total: float, what: str = "") -> None:
        """Проценты, скорость (по последним ~10 с) и оставшееся время для потока байтов."""
        now = time.monotonic()
        self._speed.append((now, done))
        while len(self._speed) > 2 and now - self._speed[0][0] > 10:
            self._speed.pop(0)
        t0, d0 = self._speed[0]
        speed = (done - d0) / (now - t0) if now - t0 > 0.5 else 0
        eta = (total - done) / speed if speed > 0 and total else None
        self.meter(pct=round(done / total * 100, 1) if total else None, done=_mb(done), total=_mb(total) if total else "",
                   speed=f"{_mb(speed)}/с" if speed else "", eta=round(eta) if eta is not None else None, detail=what,
                   force=bool(total and done >= total))

    def count(self, done: int, total: int, what: str = "") -> None:
        el = time.monotonic() - self.ts
        eta = el / done * (total - done) if done and total else None
        self.meter(pct=round(done / total * 100, 1) if total else None, done=str(done), total=str(total) if total else "",
                   speed="", eta=round(eta) if eta is not None else None, detail=what)

    def close(self, ok: bool, text: str = "") -> None:
        if self.n:
            self.line(f"  {'✓' if ok else '✗'} {self.title}: {_fmt_s(time.monotonic() - self.ts)}", show=False)
        self.line(f"{'Готово' if ok else 'Не получилось'} за {_fmt_s(time.monotonic() - self.t0)}" + (f": {text}" if text else ""),
                  show=False)
        if ok:
            self.n = self.steps
            self.meter(pct=100, detail="", eta=0, force=True)
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None


def tracked(fn):
    """Установка с журналом: при ошибке — строка «Не получилось…» и путь к журналу в тексте ошибки."""
    def run(*a, track: Track | None = None, **kw):
        own = track is None
        t = track or Track(kw.pop("progress", print), kw.pop("meter", None), kw.pop("cancel", None))
        try:
            res = fn(*a, track=t, **kw)
        except VoiceError as e:
            if own:
                t.close(False, str(e)[:300])
            if "Подробный журнал" in str(e) or str(e) == "остановлено":
                raise
            raise VoiceError(f"{e}\nПодробный журнал установки: {journal_path()}") from None
        except Exception as e:  # noqa: BLE001
            t.line(f"ошибка: {type(e).__name__}: {e}", show=False)
            if own:
                t.close(False, type(e).__name__)
            raise VoiceError(f"{type(e).__name__}: {str(e)[:300]}\nПодробный журнал установки: {journal_path()}") from None
        if own:
            t.close(True)
        return res
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


# ---------------------------------------------------------------- модель
@tracked
def download_model(s: dict, progress: Progress = print, cancel: Callable[[], bool] | None = None, *,
                   track: Track) -> dict:
    t = track
    if s["model"] not in MODELS:
        raise VoiceError("выберите модель")
    m = MODELS[s["model"]]
    if not t.title:
        t.steps = 1
    t.step(f"модель {s['model']} с {_hf(s)}")
    t.meter(detail="получаю список файлов…", force=True)
    try:
        files = model_files(s)
    except VoiceError:
        raise
    except Exception as e:  # noqa: BLE001
        raise VoiceError(f"не удалось получить список файлов модели с {_hf(s)}: {type(e).__name__}: {str(e)[:200]}. "
                         "Если Hugging Face закрыт — укажите зеркало или принесите архив с домашнего ПК") from None
    d = model_dir(s)
    d.mkdir(parents=True, exist_ok=True)
    total = sum(f["size"] or 0 for f in files)
    t.line(f"Модель {s['model']}: файлов {len(files)}" + (f", {_mb(total)}" if total else ""))
    base = 0
    for f in files:
        dest = d / f["path"]
        if dest.exists() and f["size"] and dest.stat().st_size == f["size"]:
            t.line(f"  {f['path']} — уже есть")
            base += f["size"] or 0
            continue
        url = f"{_hf(s)}/{m['repo']}/resolve/main/{urllib.parse.quote(f['path'])}"
        _fetch(url, dest, f["size"], f["sha256"], t.line, t.cancel, track=t, base=base, total=total)
        base += f["size"] or dest.stat().st_size
    return model_status(s)


def _fetch(url: str, dest: Path, size: int | None, sha: str, progress: Progress, cancel: Callable[[], bool],
           track: Track | None = None, base: int = 0, total: int = 0) -> None:
    (track.line if track else progress)(f"  скачиваю {dest.name}" + (f" ({_mb(size)})" if size else "") + "…")
    try:
        r = _get(url, 120)
    except Exception as e:  # noqa: BLE001
        raise VoiceError(f"{dest.name}: не скачался ({type(e).__name__}: {str(e)[:160]})") from None
    with r:
        _stream(r, dest, size, sha, progress, cancel, track=track, base=base, total=total)


def _stream(r, dest: Path, size: int | None, sha: str, progress: Progress, cancel: Callable[[], bool],
            track: Track | None = None, base: int = 0, total: int = 0) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    got = 0
    last = time.monotonic()
    t0 = last
    try:
        with open(tmp, "wb") as out:
            n = size or int(r.headers.get("Content-Length") or 0)
            while True:
                if cancel():
                    raise VoiceError("остановлено")
                b = r.read(1 << 20)
                if not b:
                    break
                out.write(b)
                h.update(b)
                got += len(b)
                if track is not None:
                    track.bytes(base + got, total or (base + n), dest.name)
                if time.monotonic() - last > (5 if track is not None else 2):
                    last = time.monotonic()
                    el = last - t0
                    sp = got / el if el else 0
                    eta = f", осталось ~{_fmt_s((n - got) / sp)}" if n and sp else ""
                    (track.line if track is not None else progress)(
                        f"  {dest.name}: {_mb(got)}" + (f" из {_mb(n)} ({got / n * 100:.0f}%)" if n else "") +
                        (f", {_mb(sp)}/с" if sp else "") + eta)
    except VoiceError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise VoiceError(f"{dest.name}: не скачался ({type(e).__name__}: {str(e)[:160]})") from None
    if size and got != size:
        tmp.unlink(missing_ok=True)
        raise VoiceError(f"{dest.name}: скачано {got} байт из {size} — повторите")
    if sha and len(sha) == 64 and h.hexdigest() != sha:
        tmp.unlink(missing_ok=True)
        raise VoiceError(f"{dest.name}: контрольная сумма не совпала — файл повреждён, повторите")
    os.replace(tmp, dest)
    (track.line if track is not None else progress)(f"  {dest.name} ✓ ({_mb(got)})")


def _platform_tag() -> str:
    import platform
    if sys.platform == "win32":
        return "win_amd64" if platform.machine().lower() in ("amd64", "x86_64") else "win32"
    return f"{sys.platform}_{platform.machine().lower()}"


# ---------------------------------------------------------------- пакеты
def wheel_ok(name: str) -> bool:
    """Подходит ли пакет .whl этому Python и этой системе (теги имени файла: python-abi-платформа)."""
    parts = name[:-4].split("-") if name.endswith(".whl") else []
    if len(parts) < 5:
        return False
    pys, abis, plats = parts[-3].split("."), parts[-2].split("."), parts[-1].split(".")
    major, minor = sys.version_info.major, sys.version_info.minor
    me = f"cp{major}{minor}"

    def py_ok(p: str) -> bool:
        if p in ("py3", f"py{major}{minor}", me, f"py{major}"):
            return True
        m = re.fullmatch(r"cp3(\d+)", p)
        return bool(m and "abi3" in abis and int(m.group(1)) <= minor)
    abi_ok = any(a in ("none", "abi3", me) for a in abis)
    mine = _platform_tag()
    if sys.platform == "win32":
        plat_ok = any(x in ("any", mine) for x in plats)
    else:
        import platform as _pl
        arch = _pl.machine().lower()
        plat_ok = any(x == "any" or (x.startswith(("manylinux", "musllinux", "linux")) and x.endswith(arch))
                      or (sys.platform == "darwin" and x.startswith("macosx")) for x in plats)
    return any(py_ok(p) for p in pys) and abi_ok and plat_ok


def _dist(name: str) -> str:
    return name.split("-", 1)[0].lower().replace("_", "-")


def _pip(args: list[str], t: Track, what: str, total: int = 0, timeout: float = 1800) -> list[str]:
    """pip с живым выводом: каждая строка — в журнал, главные — в окно; «молчит N с» — чтобы было видно, что идёт.
    total — сколько пакетов ожидается (для процентов по строкам Processing/Downloading)."""
    import queue
    env = dict(os.environ, PYTHONUNBUFFERED="1", PIP_NO_INPUT="1", PYTHONIOENCODING="utf-8")
    t.line(f"  pip {' '.join(a for a in args if not a.startswith('--proxy'))[:300]}", show=False)
    p = subprocess.Popen([sys.executable, "-m", "pip", "--disable-pip-version-check", *args], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                         creationflags=_NO_WINDOW)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: ([q.put(ln) for ln in p.stdout], q.put(None)), daemon=True).start()
    out: list[str] = []
    done = 0
    t0 = last_out = time.monotonic()
    warned = False
    while True:
        try:
            ln = q.get(timeout=1)
        except queue.Empty:
            ln = ""
        now = time.monotonic()
        if t.cancel():
            p.kill()
            raise VoiceError("остановлено")
        if now - t0 > timeout:
            p.kill()
            raise VoiceError(f"{what}: pip не закончил за {_fmt_s(timeout)}")
        if ln is None:
            break
        ln = ln.rstrip()
        if not ln:
            silent = now - last_out
            if silent > 10:
                t.meter(detail=f"pip работает… ({_fmt_s(silent)} без новых строк)")
            if silent > 120 and not warned:
                warned = True
                t.line(f"  pip молчит уже {_fmt_s(silent)} — обычно это антивирус проверяет файлы пакетов "
                       "(numpy, sympy — тысячи файлов) или медленная сеть. Подождите или нажмите «Остановить».")
            continue
        last_out = now
        out.append(ln)
        t.line("    " + ln, show=False)
        m = re.match(r"^\s*(Collecting|Downloading|Processing|Saved|File was already downloaded|Obtaining)\s+(.+)$", ln)
        if m:
            if m.group(1) in ("Processing", "Downloading", "File was already downloaded"):
                done += 1
            name = Path(m.group(2).split(" ")[0]).name[:90]
            t.count(done, max(total, done), f"{m.group(1).lower()}: {name}")
            if m.group(1) in ("Downloading", "Collecting"):
                t.line(f"  {ln.strip()[:160]}")
        elif re.match(r"^\s*(Installing collected packages|Successfully installed|Successfully downloaded|ERROR|WARNING)", ln):
            t.line("  " + ln.strip()[:400])
            if ln.strip().startswith("Installing collected"):
                t.meter(pct=95 if total else None, detail="устанавливаю пакеты (распаковка, без компиляции)…", force=True)
    code = p.wait()
    if code != 0:
        tail = "\n".join(x for x in out[-6:] if x.strip())
        raise VoiceError(f"{what}: pip завершился с кодом {code}.\n{logs.scrub(tail)[:700]}")
    return out


@tracked
def install_runtime(s: dict, src: Path | None = None, progress: Progress = print, *, track: Track) -> dict:
    """Пакеты для распознавания: из папки с .whl (архив, домашний ПК) или скачать pip'ом (прокси Windows, зеркало)."""
    t = track
    w = src if src is not None else wheels_dir()
    have = [f for f in w.glob("*.whl") if wheel_ok(f.name)] if w.is_dir() else []
    if not t.title:
        t.steps = 3 if (not have or src is None and not _covers(have)) else 2
    if not have or src is None and not _covers(have):
        t.step("скачиваю пакеты numpy, onnxruntime, onnx-asr (pip)")
        args = ["download", "--only-binary=:all:", "--timeout", "20", "--retries", "2", "--progress-bar", "off",
                "-d", str(wheels_dir()), *PACKAGES]
        if s.get("pypi_index"):
            args += ["--index-url", s["pypi_index"]]
        px = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
        if px:
            args += ["--proxy", px]
        t.line("Скачиваю пакеты" + (" через прокси Windows" if px else "") +
               (f" с зеркала {s['pypi_index']}" if s.get("pypi_index") else " с PyPI") + "…")
        try:
            _pip(args, t, "скачивание пакетов", total=14)
        except VoiceError as e:
            if "остановлено" in str(e):
                raise
            raise VoiceError("pip не скачал пакеты (нет доступа к PyPI?). Принесите архив с домашнего ПК "
                             f"(tools\\voice_pack.bat) или укажите зеркало PyPI компании.\n{e}") from None
        w = wheels_dir()
        have = [f for f in w.glob("*.whl") if wheel_ok(f.name)]
    missing = [p for p in PACKAGES if p not in {_dist(f.name) for f in have}]
    if missing:
        raise VoiceError(f"в {w} нет пакетов {', '.join(missing)} для Python {sys.version_info.major}."
                         f"{sys.version_info.minor} ({_platform_tag()}) — соберите архив заново (tools\\voice_pack.bat)")
    t.step(f"ставлю пакеты из папки — {len(have)} шт., без интернета")
    _pip(["install", "--no-index", "--no-compile", "--progress-bar", "off", "--find-links", str(w), *PACKAGES], t,
         "установка пакетов", total=len({_dist(f.name) for f in have}))
    if w != wheels_dir():  # копия рядом — переустановить после обновления программы без архива
        for f in w.glob("*.whl"):
            if not (wheels_dir() / f.name).exists():
                shutil.copy2(f, wheels_dir() / f.name)
    t.step("проверяю, что onnxruntime загружается")
    t.meter(detail="запуск Python с onnxruntime (первый раз — до минуты: антивирус проверяет библиотеки)…", force=True)
    st = runtime_status(force=True)
    if not st["ok"]:
        raise VoiceError("пакеты поставились, но не загружаются: " + json.dumps(st["packages"], ensure_ascii=False)[:400])
    t.line(f"Пакеты готовы: onnxruntime {st['packages'].get('onnxruntime')}, onnx-asr {st['packages'].get('onnx_asr')}")
    return st


def _covers(files: list[Path]) -> bool:
    names = {_dist(f.name) for f in files}
    return all(p in names for p in PACKAGES)


# ---------------------------------------------------------------- архив с домашнего ПК
def find_archives() -> list[Path]:
    home = Path.home()
    out = []
    for d in (home / "Downloads", home / "Downloads" / "Telegram Desktop", home / "Загрузки", home / "Desktop",
              home / "OneDrive" / "Desktop", base_dir()):
        try:
            out += [p for p in d.iterdir() if p.is_file() and ARCHIVE_RE.match(p.name)]
        except OSError:
            continue
    return sorted(set(out), key=lambda p: p.stat().st_mtime, reverse=True)[:5]


def github_archive(raw: dict, progress: Progress = print, cancel: Callable[[], bool] | None = None,
                   track: Track | None = None) -> Path:
    """OpsDigest-voice.zip из релиза репозитория программы на GitHub (тот же токен, что у «⟳ Обновить»).
    Архив выкладывается туда дома: tools\\voice_pack.bat --github (или вручную на странице Releases)."""
    from .updater import _NoRedirect, settings as up_settings
    up = up_settings(raw)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "OpsDigest-voice/1.0", "X-GitHub-Api-Version": "2022-11-28"}
    if up["token"]:
        headers["Authorization"] = f"Bearer {up['token']}"
    try:
        with _get_h(f"https://api.github.com/repos/{up['repo']}/releases?per_page=20", headers) as r:
            rels = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise VoiceError(f"GitHub недоступен или не принял токен ({type(e).__name__}: {str(e)[:120]}). Токен — в окне "
                         "«⟳ Обновить»; или принесите архив другим путём") from None
    asset = next((a for rel in rels if isinstance(rel, dict) for a in rel.get("assets") or []
                  if ARCHIVE_RE.match(a.get("name") or "")), None)
    if not asset:
        raise VoiceError(f"в релизах {up['repo']} нет OpsDigest-voice.zip — выложите его дома: tools\\voice_pack.bat --github")
    dest = base_dir() / asset["name"]
    if dest.exists() and dest.stat().st_size == asset.get("size"):
        progress(f"Архив уже скачан: {dest}")
        return dest
    progress(f"Скачиваю с GitHub {asset['name']} ({(asset.get('size') or 0) / 2**20:.0f} МБ)…")
    h2 = dict(headers, Accept="application/octet-stream")
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler(urllib.request.getproxies()))
    url = asset["url"]
    for _ in range(4):  # переадресация на хранилище файлов — уже без ключа
        try:
            resp = opener.open(urllib.request.Request(url, headers=h2), timeout=120)  # noqa: S310 — api.github.com
            break
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = e.headers["Location"]
                h2 = {"User-Agent": "OpsDigest-voice/1.0", "Accept": "application/octet-stream"}
                continue
            raise VoiceError(f"GitHub ответил {e.code} на скачивание архива") from None
    else:
        raise VoiceError("GitHub слишком много раз переадресовал запрос")
    with resp:
        _stream(resp, dest, asset.get("size"), "", progress, cancel or (lambda: False), track=track,
                total=int(asset.get("size") or 0))
    return dest


def _get_h(url: str, headers: dict, timeout: float = 60):
    req = urllib.request.Request(url, headers=headers)
    return _opener().open(req, timeout=timeout)  # noqa: S310


@tracked
def import_archive(s: dict, path: Path, progress: Progress = print, *, track: Track) -> dict:
    """OpsDigest-voice.zip: wheels/*.whl и models/<модель>/… → %LOCALAPPDATA%\\OpsDigest\\voice; пакеты — офлайн.
    Сначала — проверка архива (докачан ли, цел ли, есть ли пакеты для этого Python), распаковываются только
    подходящие пакеты; содержимое сверяется с manifest.json архива (контрольные суммы), если он есть."""
    t = track
    t.steps = 5
    t.step(f"проверяю архив {path.name}")
    if not path.is_file():
        raise VoiceError(f"нет файла {path}")
    size1 = path.stat().st_size
    time.sleep(1.0)
    if path.stat().st_size != size1:
        raise VoiceError(f"{path.name} ещё скачивается (размер меняется) — дождитесь конца загрузки в Telegram и повторите")
    t.line(f"Архив: {path} · {_mb(size1)} · от {time.strftime('%d.%m.%Y %H:%M', time.localtime(path.stat().st_mtime))}")
    base = base_dir().resolve()
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise VoiceError(f"{path.name} — не ZIP-архив или скачан не до конца ({_mb(size1)}). Перешлите его заново "
                         "и дождитесь полной загрузки") from None
    with z:
        manifest = {}
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            t.line(f"Архив собран {manifest.get('created', '?')} для Python {', '.join(manifest.get('pythons') or ['?'])}"
                   f" ({manifest.get('platform', '?')}), модель {manifest.get('model', '?')}")
        except KeyError:
            t.line("В архиве нет manifest.json (собран старой версией voice_pack) — контрольные суммы не сверяю")
        except (ValueError, zipfile.BadZipFile):
            t.line("manifest.json в архиве не читается — контрольные суммы не сверяю")
        sums = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        todo, skipped_whl, models = [], [], set()
        all_whl = []
        for info in z.infolist():
            name = info.filename.replace("\\", "/").lstrip("/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or ".." in parts or info.is_dir():
                continue
            if parts[0] not in ("wheels", "models") or (parts[0] == "models" and len(parts) < 3):
                continue
            dest = (base / Path(*parts)).resolve()
            if base not in dest.parents:
                continue
            if parts[0] == "wheels":
                all_whl.append(parts[-1])
                if not wheel_ok(parts[-1]):
                    skipped_whl.append(parts[-1])
                    continue
            else:
                models.add(parts[1])
            todo.append((info, dest, "/".join(parts)))
        mine = {_dist(n) for n in all_whl if wheel_ok(n)}
        missing = [p for p in PACKAGES if p not in mine]
        pyv = f"{sys.version_info.major}.{sys.version_info.minor}"
        t.line(f"Пакетов в архиве: {len(all_whl)}, подходят этому Python {pyv} ({_platform_tag()}): "
               f"{len(all_whl) - len(skipped_whl)}; модели: {', '.join(sorted(models)) or 'нет'}")
        if all_whl and missing:
            vers = sorted({f"{m.group(1)}.{m.group(2)}" for n in all_whl if (m := re.search(r"-cp(\d)(\d+)-", n))},
                          key=lambda v: tuple(int(x) for x in v.split(".")))
            raise VoiceError(f"в архиве нет пакетов {', '.join(missing)} для Python {pyv} ({_platform_tag()})"
                             + (f" — он собран для Python {', '.join(vers)}" if vers else "") +
                             ". Соберите архив заново дома: tools\\voice_pack.bat (по умолчанию — для Python 3.11–3.14)")
        total = sum(i.file_size for i, _d, _n in todo)
        t.step(f"распаковываю {len(todo)} файлов ({_mb(total)})")
        done = 0
        for info, dest, rel in todo:
            t.check()
            if dest.exists() and dest.stat().st_size == info.file_size and not (sums.get(rel) or {}).get("sha256"):
                done += info.file_size
                t.bytes(done, total, f"{dest.name} — уже есть")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            h = hashlib.sha256()
            try:
                with z.open(info) as src, open(tmp, "wb") as out:
                    while True:
                        t.check()
                        b = src.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
                        h.update(b)
                        done += len(b)
                        t.bytes(done, total, dest.name)
            except zipfile.BadZipFile as e:   # CRC не сошёлся — архив повреждён при пересылке
                tmp.unlink(missing_ok=True)
                raise VoiceError(f"архив повреждён: {rel} ({e}). Перешлите его заново") from None
            except VoiceError:
                tmp.unlink(missing_ok=True)
                raise
            want = (sums.get(rel) or {}).get("sha256")
            if want and want != h.hexdigest():
                tmp.unlink(missing_ok=True)
                raise VoiceError(f"архив повреждён: {rel} — контрольная сумма не совпала. Перешлите архив заново")
            os.replace(tmp, dest)
            if info.file_size > 20 * 2**20:
                t.line(f"  {rel} ✓ ({_mb(info.file_size)})")
    t.line("Распаковано" + (", контрольные суммы совпали" if sums else ""))
    if list(wheels_dir().glob("*.whl")):
        rt = install_runtime(s, wheels_dir(), track=t)
    else:
        t.steps = 4
        t.step("пакетов в архиве нет — проверяю, что уже стоит")
        rt = runtime_status(force=True)
    t.step("проверяю модель")
    ms = model_status(dict(s, model=s["model"] if s["model"] in models or not models else sorted(models)[0]))
    t.line(f"Модель: {ms['title']} — {'✓ ' + str(ms['mb']) + ' МБ' if ms['ok'] else 'нет файлов .onnx'}")
    return {"runtime": rt, "models": sorted(models)}


# ---------------------------------------------------------------- процесс распознавания
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


class Engine:
    """Отдельный процесс с загруженной моделью; одна фраза за раз; выгружается после простоя."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.key: tuple | None = None
        self.lock = threading.Lock()
        self.last = 0.0
        self.idle = 600.0
        self.loaded_at = 0.0
        self.error = ""
        threading.Thread(target=self._watch, daemon=True, name="voice-idle").start()

    def state(self) -> dict:
        p = self.proc
        return {"running": bool(p and p.poll() is None), "model": self.key[0] if self.key else "",
                "idle_left": max(0, int(self.idle - (time.time() - self.last))) if p and p.poll() is None else 0,
                "error": self.error}

    def _watch(self) -> None:
        while True:
            time.sleep(15)
            if self.proc and self.proc.poll() is None and time.time() - self.last > self.idle:
                with self.lock:
                    log.info("голос: выгружаю модель после простоя")
                    self.stop()

    def stop(self) -> None:
        p, self.proc, self.key = self.proc, None, None
        if p and p.poll() is None:
            try:
                p.stdin.write(b'{"op": "quit"}\n')
                p.stdin.flush()
                p.wait(5)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                p.kill()

    def _start(self, s: dict) -> None:
        d = model_dir(s)
        if s["model"] != "fake":
            ms = model_status(s)
            if not ms["ok"]:
                raise VoiceError("модель не установлена — «Настройки → Голосовой ввод» → «Скачать» или архив")
        args = [sys.executable, "-m", "opsdigest.voice", "--worker", "--model", s["model"], "--dir", str(d),
                "--threads", str(s["threads"])] + (["--quant", s["quantization"]] if s["quantization"] else [])
        log.info("голос: запускаю распознавание (%s)", s["model"])
        root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=root, env=env, creationflags=_NO_WINDOW)
        self.key = (s["model"], str(d), s["quantization"], s["threads"])
        hello = self._read(timeout=240)
        if not hello.get("ok"):
            err = hello.get("error") or "процесс распознавания не запустился"
            self.stop()
            raise VoiceError(err)
        self.loaded_at = time.time()

    def _read(self, timeout: float) -> dict:
        p = self.proc
        out: dict = {}

        def go():
            nonlocal out
            line = p.stdout.readline() if p and p.stdout else b""
            try:
                out = json.loads(line.decode("utf-8") or "{}")
            except ValueError:
                out = {"ok": False, "error": line.decode("utf-8", "replace")[:300]}
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or not out:
            err = ""
            if p and p.poll() is not None and p.stderr:
                err = p.stderr.read().decode("utf-8", "replace")[-600:]
            self.error = logs.scrub(err.strip().splitlines()[-1] if err.strip() else "нет ответа")[:300]
            p and p.kill()
            self.proc = None
            raise VoiceError(f"распознавание не ответило: {self.error}")
        return out

    def recognize(self, s: dict, pcm: bytes, rate: int) -> dict:
        with self.lock:
            self.idle = s["idle_minutes"] * 60
            key = (s["model"], str(model_dir(s)), s["quantization"], s["threads"])
            if not self.proc or self.proc.poll() is not None or self.key != key:
                self.stop()
                self._start(s)
            self.last = time.time()
            hdr = json.dumps({"op": "recognize", "n": len(pcm), "rate": rate}).encode("utf-8") + b"\n"
            try:
                self.proc.stdin.write(hdr + pcm)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.stop()
                raise VoiceError("процесс распознавания завершился — попробуйте ещё раз") from None
            res = self._read(timeout=120 + len(pcm) / (rate * 2) * 2)
            self.last = time.time()
            if not res.get("ok"):
                raise VoiceError(res.get("error") or "не распознано")
            return res


ENGINE = Engine()


def recognize(s: dict, pcm: bytes, rate: int = 16000, glossary: list[tuple[str, str]] | None = None) -> dict:
    if not s.get("enabled", True):
        raise VoiceError("голосовой ввод выключен в настройках")
    if len(pcm) < rate // 5:  # меньше 0,1 с
        return {"text": "", "seconds": 0}
    t0 = time.monotonic()
    res = ENGINE.recognize(s, pcm, rate)
    text = postprocess(res.get("text") or "", s, glossary or [])
    return {"text": text, "seconds": round(len(pcm) / 2 / rate, 1), "ms": int((time.monotonic() - t0) * 1000),
            "model": s["model"]}


def postprocess(text: str, s: dict, glossary: list[tuple[str, str]]) -> str:
    t = (text or "").strip()
    if s.get("commands", True):
        for rx, rep in COMMANDS:
            t = rx.sub(rep, t)
    for a, b in list(s.get("replace") or []) + list(glossary):
        if a and b:
            t = re.sub(r"(?i)(?<![\wа-яё])" + re.escape(str(a)) + r"(?![\wа-яё])", str(b), t)
    return t.strip(" ")


def glossary_replacements(store) -> list[tuple[str, str]]:
    """Термины глоссария, которые пишутся латиницей, а синонимы у них — кириллицей («Мэверик» → «Maverick»):
    модель пишет английские слова русскими буквами, глоссарий возвращает правильное написание."""
    try:
        from .glossary import Glossary
        out = []
        for e in Glossary(store).all():
            term = e.get("term") or ""
            if not re.search(r"[A-Za-z]", term):
                continue
            for al in e.get("aliases") or []:
                if re.fullmatch(r"[А-Яа-яЁё][А-Яа-яЁё\- ]{1,40}", al or ""):
                    out.append((al, term))
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- сам процесс распознавания
def _segments(x, rate: int, max_s: float = 22.0, min_s: float = 14.0):
    """Длинную запись — на куски до 22 с по самым тихим местам (GigaAM рассчитана на фразы до ~25 с)."""
    import numpy as np
    n = len(x)
    if n <= max_s * rate:
        return [x]
    frame = int(0.03 * rate)
    out, start = [], 0
    while n - start > max_s * rate:
        a, b = start + int(min_s * rate), start + int(max_s * rate)
        win = x[a:b]
        k = len(win) // frame
        if k < 2:
            cut = b
        else:
            e = np.sqrt((win[: k * frame].reshape(k, frame) ** 2).mean(axis=1))
            cut = a + int(e.argmin()) * frame + frame // 2
        out.append(x[start:cut])
        start = cut
    out.append(x[start:])
    return [s for s in out if len(s) > rate // 10]


def worker_main(model: str, directory: str, threads: int, quant: str | None) -> int:
    out = sys.stdout.buffer
    inp = sys.stdin.buffer

    def send(d: dict) -> None:
        out.write(json.dumps(d, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()
    try:
        import numpy as np
        if model == "fake":  # для проверок программы: «распознаёт» длительность
            def rec(x, rate):
                return f"проверка {len(x) / rate:.1f} с"
        else:
            import onnx_asr
            kw: dict = {"quantization": quant} if quant else {}
            try:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = threads
                so.inter_op_num_threads = 1
                kw["sess_options"] = so
            except Exception:  # noqa: BLE001
                pass
            try:
                m = onnx_asr.load_model(model, directory, **kw)
            except TypeError:  # старая onnx-asr без sess_options
                kw.pop("sess_options", None)
                m = onnx_asr.load_model(model, directory, **kw)
            except Exception:  # noqa: BLE001 — нет int8-файлов: пробуем обычные
                if not quant:
                    raise
                kw.pop("quantization", None)
                m = onnx_asr.load_model(model, directory, **kw)

            def rec(x, rate):
                r = m.recognize(x, sample_rate=rate)
                return r if isinstance(r, str) else getattr(r, "text", str(r))
        send({"ok": True, "loaded": model})
    except Exception as e:  # noqa: BLE001
        send({"ok": False, "error": f"модель не загрузилась: {type(e).__name__}: {str(e)[:300]}"})
        return 1
    while True:
        line = inp.readline()
        if not line:
            return 0
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            send({"ok": False, "error": "плохой запрос"})
            continue
        if req.get("op") == "quit":
            return 0
        if req.get("op") == "ping":
            send({"ok": True})
            continue
        n, rate = int(req.get("n") or 0), int(req.get("rate") or 16000)
        buf = inp.read(n) if n else b""
        try:
            x = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            t0 = time.monotonic()
            parts = [rec(seg, rate) for seg in _segments(x, rate)]
            send({"ok": True, "text": " ".join(p.strip() for p in parts if p and p.strip()),
                  "ms": int((time.monotonic() - t0) * 1000), "parts": len(parts)})
        except Exception as e:  # noqa: BLE001
            send({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})


def wav_pcm(data: bytes) -> tuple[bytes, int]:
    """WAV (PCM 16 бит) → (сырые отсчёты моно, частота) — для проверки из командной строки."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise VoiceError("не WAV")
    pos, rate, ch, bits, pcm = 12, 16000, 1, 16, b""
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            _fmt, ch, rate, _br, _al, bits = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if bits != 16:
        raise VoiceError("нужен WAV 16 бит")
    if ch > 1:
        import array
        a = array.array("h", pcm)
        pcm = array.array("h", a[::ch]).tobytes()
    return pcm, rate


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m opsdigest.voice", description="Голосовой ввод OpsDigest")
    ap.add_argument("--worker", action="store_true", help="процесс распознавания (запускает программа)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dir", default="")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--quant", default="")
    ap.add_argument("--download", action="store_true", help="скачать модель")
    ap.add_argument("--install", nargs="?", const="", help="поставить пакеты (из папки или скачать)")
    ap.add_argument("--archive", help="поставить из OpsDigest-voice.zip")
    ap.add_argument("--wav", help="распознать WAV-файл (проверка)")
    a = ap.parse_args(argv)
    if a.worker:
        return worker_main(a.model, a.dir, a.threads, a.quant or None)
    from . import workspace
    s = settings_of(workspace.load_app_settings())
    if a.model:
        s["model"] = a.model
    if a.dir:
        s["path"] = a.dir
    try:
        if a.archive:
            print(import_archive(s, Path(a.archive)))
        if a.install is not None:
            print(install_runtime(s, Path(a.install) if a.install else None))
        if a.download:
            print(download_model(s))
        if a.wav:
            pcm, rate = wav_pcm(Path(a.wav).read_bytes())
            print(recognize(s, pcm, rate)["text"])
            ENGINE.stop()
    except VoiceError as e:
        print("✗", e)
        return 1
    if not (a.archive or a.install is not None or a.download or a.wav):
        print(json.dumps(status(s), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
