"""Голосовой ввод в окне: состояние (пакеты, модель, процесс), установка (скачать / из архива / из папки),
настройки (модель, потоки, зеркала, замены слов)."""
from __future__ import annotations

import re
from pathlib import Path

from .. import voice
from .. import workspace as wsmod


def _save(b: dict) -> dict:
    cur = dict(wsmod.load_app_settings().get("voice") or {})
    for k in ("model", "path", "hf_mirror", "pypi_index", "hotkey"):
        if k in b:
            cur[k] = str(b.get(k) or "").strip()[:500]
    if cur.get("model") and cur["model"] not in voice.MODELS and cur["model"] != "fake":
        cur["model"] = voice.DEFAULT_MODEL
    for k in ("hf_mirror", "pypi_index"):
        if cur.get(k) and not re.match(r"https?://", cur[k]):
            raise ValueError("адрес зеркала — с http:// или https://")
    if "quantization" in b:
        cur["quantization"] = "none" if b["quantization"] in ("none", "", None, False) else "int8"
    for k in ("threads", "idle_minutes"):
        if str(b.get(k) or "").isdigit():
            cur[k] = int(b[k])
    for k in ("enabled", "commands"):
        if k in b:
            cur[k] = bool(b[k])
    if "replace" in b:
        rep = b["replace"]
        if isinstance(rep, str):  # строки «как слышится → как писать»
            rep = [re.split(r"\s*(?:→|->|=>|=)\s*", ln.strip(), maxsplit=1) for ln in rep.splitlines() if ln.strip()]
        cur["replace"] = [[str(x[0]).strip(), str(x[1]).strip()] for x in rep or [] if len(x) == 2 and x[0].strip()][:300]
    wsmod.save_app_settings({"voice": cur})
    voice.ENGINE.stop()  # новая модель/потоки — со следующей фразы
    return voice.settings_of({"voice": cur})


def route(h, method: str, path: str, b: dict, q: dict):
    from .server import Job, UserError
    app = h.app
    s = app.voice_settings()
    if path == "/api/voice/status":
        if (q.get("force") or [""])[0] == "1":
            voice.runtime_status(force=True)
        return voice.status(s)
    if method != "POST":
        return None
    try:
        if path == "/api/voice/settings":
            return {"settings": _save(b)}
        if path == "/api/voice/stop":
            voice.ENGINE.stop()
            return {"stopped": True}
    except ValueError as e:
        raise UserError(str(e)) from None
    if path in ("/api/voice/download", "/api/voice/install", "/api/voice/archive"):
        if app.running("voice"):
            raise UserError("установка уже идёт")

        def go(job: Job):
            def meter(d: dict) -> None:
                job.meter = d
            t = voice.Track(job.add, meter, lambda: job.cancelled)
            job.add(f"Журнал установки: {voice.journal_path()}")
            try:
                if path == "/api/voice/archive":
                    p = Path(str(b.get("path") or "")) if b.get("path") else next(iter(voice.find_archives()), None)
                    if p is None and b.get("github"):
                        t.steps = 6
                        t.step("скачиваю архив из релиза GitHub")
                        p = voice.github_archive(app.raw() if app.ws else {}, progress=job.add, cancel=lambda: job.cancelled,
                                                 track=t)
                    if p is None:
                        raise voice.VoiceError("архив OpsDigest-voice.zip не найден в «Загрузках» — укажите путь "
                                               "или скачайте его из релиза на GitHub")
                    res = voice.import_archive(s, p, track=t)
                elif path == "/api/voice/install":
                    src = Path(str(b["path"])) if b.get("path") else None
                    res = voice.install_runtime(s, src, track=t)
                else:
                    need = not voice.runtime_status().get("ok")
                    t.steps = 4 if need else 1
                    if need:
                        voice.install_runtime(s, None, track=t)
                    res = voice.download_model(s, track=t)
                t.close(True)
                job.add("Готово ✓ — нажмите 🎤 и говорите")
                return {"result": res, "status": voice.status(s)}
            except voice.VoiceError as e:
                t.close(False, str(e).splitlines()[0][:300])
                raise UserError(str(e)) from None
        return {"job": app.start_job("voice", go).id}
    return None
