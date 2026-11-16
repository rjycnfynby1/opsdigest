"""Собрать OpsDigest-voice.zip на компьютере с интернетом (домашний ПК) — для голосового ввода на рабочем ноутбуке.

В архиве: wheels/ — пакеты numpy, onnxruntime, onnx-asr и их зависимости для Python рабочего ноутбука (по умолчанию
сразу для 3.11–3.14, Windows x64 — какой бы Python ни стоял), models/<модель>/ — файлы модели GigaAM v3 с Hugging Face. Архив кладётся в «Загрузки»; перешлите
его себе в «Избранное» Telegram (или на флешке) — на ноутбуке Telegram Desktop сохранит его в «Загрузки\\Telegram
Desktop», а OpsDigest найдёт сам: «Настройки → Голосовой ввод → Установить из архива».

Запуск: tools\\voice_pack.bat  (или python tools\\voice_pack.py --python 3.14 --model gigaam-v3-e2e-ctc)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    from opsdigest import voice
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--python", default="3.11,3.12,3.13,3.14",
                    help="версии Python рабочего ноутбука через запятую (по умолчанию все 3.11–3.14: подойдёт любая)")
    ap.add_argument("--platform", default="win_amd64")
    ap.add_argument("--model", default=voice.DEFAULT_MODEL, choices=sorted(voice.MODELS))
    ap.add_argument("--no-int8", action="store_true", help="полная модель вместо квантованной (вдвое больше)")
    ap.add_argument("--out", default=str(Path.home() / "Downloads" / "OpsDigest-voice.zip"))
    ap.add_argument("--hf-mirror", default="", help="зеркало Hugging Face, если huggingface.co недоступен")
    ap.add_argument("--github", action="store_true", help="выложить архив в релиз voice-pack репозитория (нужен gh)")
    ap.add_argument("--repo", default="rjycnfynby1/mypunchagent")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="opsdigest-voice-"))
    try:
        wheels = tmp / "wheels"
        got = []
        for v in [x.strip() for x in a.python.split(",") if x.strip()]:
            ver = v.replace(".", "")
            print(f"1/3 Пакеты для Python {v} ({a.platform})…", flush=True)
            cmd = [sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
                   "--platform", a.platform, "--python-version", ver, "--implementation", "cp",
                   "--abi", f"cp{ver}", "--abi", "abi3", "--abi", "none", "-d", str(wheels), *voice.PACKAGES]
            if subprocess.run(cmd).returncode == 0:
                got.append(v)
            else:
                print(f"  ⚠ для Python {v} пакеты не скачались — пропускаю", flush=True)
        if not got:
            print("✗ pip не скачал пакеты — проверьте интернет", flush=True)
            return 1
        print(f"  пакеты есть для Python {', '.join(got)}", flush=True)
        print(f"2/3 Модель {a.model} с Hugging Face…", flush=True)
        s = voice.settings_of({"voice": {"model": a.model, "quantization": "none" if a.no_int8 else "int8",
                                         "hf_mirror": a.hf_mirror}})
        mdir = tmp / "models" / a.model
        mdir.mkdir(parents=True)
        for f in voice.model_files(s):
            url = f"{voice._hf(s)}/{voice.MODELS[a.model]['repo']}/resolve/main/{f['path']}"
            voice._fetch(url, mdir / f["path"], f["size"], f["sha256"], print, lambda: False)
        print(f"3/3 Архив {a.out}…", flush=True)
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in tmp.rglob("*") if p.is_file())
        # manifest.json: для каких Python собран и контрольные суммы — по ним ноутбук проверит, что архив дошёл целым
        manifest = {"created": time.strftime("%d.%m.%Y %H:%M"), "pythons": got, "platform": a.platform, "model": a.model,
                    "files": {p.relative_to(tmp).as_posix(): {"size": p.stat().st_size, "sha256": _sha(p)} for p in files}}
        per = {v: sorted({w.name.split("-", 1)[0] for w in wheels.glob("*.whl")
                          if f"cp{v.replace('.', '')}" in w.name or "-py3-none-any" in w.name or "abi3" in w.name}) for v in got}
        for v, names in per.items():
            print(f"  Python {v}: пакетов {len(names)} ({', '.join(names)[:200]})", flush=True)
        total = sum(p.stat().st_size for p in files)
        done = 0
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            for p in files:
                z.write(p, p.relative_to(tmp).as_posix(),
                        zipfile.ZIP_STORED if p.suffix in (".onnx", ".data", ".whl") else zipfile.ZIP_DEFLATED)
                done += p.stat().st_size
                print(f"  {done / total * 100:3.0f}%  {p.name}", flush=True)
        print("  проверяю архив…", flush=True)
        with zipfile.ZipFile(out) as z:
            bad = z.testzip()
        if bad:
            print(f"✗ Архив записался с ошибкой ({bad}) — запустите ещё раз", flush=True)
            return 1
        print(f"✓ Готово: {out} ({out.stat().st_size / 2**20:.0f} МБ, Python {', '.join(got)}). Перешлите его на рабочий "
              "ноутбук (Telegram «Избранное» или флешка) — дождитесь, пока Telegram скачает файл целиком, — и нажмите "
              "в OpsDigest «Установить из архива».", flush=True)
        if a.github:
            if not shutil.which("gh"):
                print("  gh (GitHub CLI) не найден — выложите архив вручную: GitHub → Releases → новый релиз voice-pack → "
                      "прикрепите файл.", flush=True)
                return 0
            tag = "voice-pack"
            if subprocess.run(["gh", "release", "view", tag, "--repo", a.repo], capture_output=True).returncode != 0:
                subprocess.run(["gh", "release", "create", tag, "--repo", a.repo, "--title", "Голосовой ввод OpsDigest",
                                "--notes", "Пакеты и модель GigaAM v3 для голосового ввода (tools/voice_pack.py)"], check=False)
            r = subprocess.run(["gh", "release", "upload", tag, str(out), "--repo", a.repo, "--clobber"])
            print("✓ Выложено в релиз voice-pack — на ноутбуке: «Голосовой ввод» → «⟳ с GitHub»." if r.returncode == 0
                  else "✗ Не выложилось — проверьте gh auth status", flush=True)
        return 0
    except voice.VoiceError as e:
        print("✗", e, flush=True)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
