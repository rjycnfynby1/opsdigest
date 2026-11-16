"""Установка голосового ввода: видно, что идёт (шаги, проценты, скорость, сколько осталось, журнал), архив
проверяется до распаковки (докачан ли, цел ли, есть ли пакеты для этого Python), распаковываются только подходящие
пакеты, содержимое сверяется с manifest.json."""
import hashlib
import json
import sys
import zipfile

import pytest

from opsdigest import voice

PY = f"cp{sys.version_info.major}{sys.version_info.minor}"


@pytest.fixture
def vdir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSDIGEST_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(voice.time, "sleep", lambda s: None)   # проверка «архив ещё качается» — без ожидания
    return tmp_path


def _zip(path, files: dict, manifest: bool = True, corrupt: str = ""):
    with zipfile.ZipFile(path, "w") as z:
        if manifest:
            z.writestr("manifest.json", json.dumps({"created": "09.11.2026 01:00", "pythons": ["3.12"], "platform": "win_amd64",
                                                    "model": "gigaam-v3-e2e-ctc",
                                                    "files": {k: {"size": len(v), "sha256": hashlib.sha256(v).hexdigest()}
                                                              for k, v in files.items()}}))
        for k, v in files.items():
            z.writestr(k, v, zipfile.ZIP_STORED)
    if corrupt:   # испортить байты внутри файла — как при неполной пересылке
        raw = bytearray(path.read_bytes())
        i = raw.find(files[corrupt])
        raw[i + 3] ^= 0xFF
        path.write_bytes(bytes(raw))


def test_wheel_tags():
    plat = voice._platform_tag()
    assert voice.wheel_ok("onnx_asr-0.7.0-py3-none-any.whl")
    assert voice.wheel_ok("sympy-1.14.0-py3-none-any.whl")
    assert not voice.wheel_ok("numpy-2.3.4-cp39-cp39-win_amd64.whl")
    assert voice.wheel_ok("protobuf-6.33.0-cp39-abi3-any.whl")    # abi3 — для этой и более новых версий
    assert not voice.wheel_ok("protobuf-6.33.0-cp399-abi3-any.whl")
    if sys.platform == "win32":
        assert voice.wheel_ok(f"numpy-2.3.4-{PY}-{PY}-{plat}.whl")
    assert not voice.wheel_ok("README.md")


def test_archive_progress_and_journal(vdir):
    model = b"m" * (3 * 2**20 + 5)
    z = vdir / "OpsDigest-voice.zip"
    _zip(z, {"models/gigaam-v3-e2e-ctc/v3_e2e_ctc.int8.onnx": model, "models/gigaam-v3-e2e-ctc/v3_e2e_ctc_vocab.txt": b"a\nb"})
    lines, meters = [], []
    s = voice.settings_of({})
    res = voice.import_archive(s, z, progress=lines.append, meter=meters.append)
    assert res["models"] == ["gigaam-v3-e2e-ctc"] and voice.model_status(s)["ok"]
    text = "\n".join(lines)
    assert "Шаг 1/5: проверяю архив" in text and "Архив собран 09.11.2026 01:00" in text
    assert "Шаг 2/5: распаковываю 2 файлов" in text and "контрольные суммы совпали" in text
    pcts = [m["pct"] for m in meters if m.get("step") == "2/5" and m.get("pct") is not None]
    assert pcts and pcts[-1] == 100.0
    assert meters[-1]["overall"] == 100 and all("elapsed" in m for m in meters)
    log = voice.journal_path().read_text(encoding="utf-8")
    assert "Шаг 2/5" in log and "Готово за" in log and str(vdir) not in log   # пути обезличены
    # второй раз — то, что уже распаковано, не переписывается заново (размер и сумма те же)
    lines.clear()
    voice.import_archive(s, z, progress=lines.append)
    assert voice.model_status(s)["ok"]


def test_archive_wrong_python(vdir):
    z = vdir / "OpsDigest-voice.zip"
    _zip(z, {"wheels/numpy-2.3.4-cp39-cp39-win_amd64.whl": b"x", "wheels/onnxruntime-1.23.0-cp39-cp39-win_amd64.whl": b"y",
             "wheels/onnx_asr-0.7.0-py3-none-any.whl": b"z"})
    with pytest.raises(voice.VoiceError) as e:
        voice.import_archive(voice.settings_of({}), z, progress=lambda x: None)
    msg = str(e.value)
    assert "нет пакетов" in msg and "numpy" in msg and "onnxruntime" in msg and "собран для Python 3.9" in msg and "voice_pack.bat" in msg
    assert "Подробный журнал установки" in msg
    assert not (vdir / "voice" / "wheels" / "numpy-2.3.4-cp39-cp39-win_amd64.whl").exists()   # не распаковывали


def test_archive_corrupt_and_truncated(vdir):
    z = vdir / "OpsDigest-voice.zip"
    data = b"0123456789" * 1000
    _zip(z, {"models/gigaam-v3-e2e-ctc/v3_e2e_ctc.int8.onnx": data}, corrupt="models/gigaam-v3-e2e-ctc/v3_e2e_ctc.int8.onnx")
    with pytest.raises(voice.VoiceError, match="архив повреждён"):
        voice.import_archive(voice.settings_of({}), z, progress=lambda x: None)
    assert not list((vdir / "voice").rglob("*.part"))
    z.write_bytes(z.read_bytes()[:500])   # скачан не до конца
    with pytest.raises(voice.VoiceError, match="скачан не до конца"):
        voice.import_archive(voice.settings_of({}), z, progress=lambda x: None)


def test_archive_cancel(vdir):
    z = vdir / "OpsDigest-voice.zip"
    _zip(z, {"models/gigaam-v3-e2e-ctc/v3_e2e_ctc.int8.onnx": b"m" * 2**21})
    with pytest.raises(voice.VoiceError, match="^остановлено$"):
        voice.import_archive(voice.settings_of({}), z, progress=lambda x: None, cancel=lambda: True)


def test_pip_live_output(vdir):
    """pip — с живым выводом: строки в журнал, ошибка — с хвостом вывода."""
    lines = []
    t = voice.Track(lines.append, steps=1)
    t.step("проверка pip")
    out = voice._pip(["--version"], t, "проверка")
    assert any("pip" in x for x in out)
    with pytest.raises(voice.VoiceError, match="pip завершился с кодом"):
        voice._pip(["install", "--no-index", "--find-links", str(vdir), "nonexistent-package-xyz"], t, "установка")
    t.close(False)
    assert "nonexistent-package-xyz" in voice.journal_path().read_text(encoding="utf-8")
