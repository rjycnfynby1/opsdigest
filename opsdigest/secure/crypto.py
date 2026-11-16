"""Шифрование на стандартной библиотеке Python (без пакетов, одинаково на Windows и в тестах).

Формат блока: b"ODX1" + nonce (24 байта) + шифротекст + тег (32 байта).
  • поток ключа — SHAKE-256(метка ‖ ключ шифрования ‖ nonce ‖ номер куска) — XOF семейства SHA-3 как потоковый шифр;
  • целостность — HMAC-SHA-256(ключ подписи, заголовок ‖ nonce ‖ доп. данные ‖ шифротекст) (encrypt-then-MAC);
  • ключи шифрования и подписи выводятся из ключа данных HMAC-ом с разными метками.
Ключ из пароля — scrypt (n=2^15, r=8, p=1), если в Python его нет — PBKDF2-SHA-256, 600 000 итераций.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

MAGIC = b"ODX1"
NONCE = 24
TAG = 32
CHUNK = 1 << 20


class CryptoError(Exception):
    """Неверный ключ/пароль или файл повреждён."""


def _subkeys(key: bytes) -> tuple[bytes, bytes]:
    enc = hmac.new(key, b"opsdigest/v1/enc", hashlib.sha256).digest()
    mac = hmac.new(key, b"opsdigest/v1/mac", hashlib.sha256).digest()
    return enc, mac


def _xor_stream(enc: bytes, nonce: bytes, data: bytes) -> bytes:
    out = []
    for i in range(0, len(data), CHUNK):
        part = data[i:i + CHUNK]
        ks = hashlib.shake_256(b"OD-KS1" + enc + nonce + (i // CHUNK).to_bytes(8, "little")).digest(len(part))
        out.append((int.from_bytes(part, "little") ^ int.from_bytes(ks, "little")).to_bytes(len(part), "little"))
    return b"".join(out)


def _tag(mac: bytes, nonce: bytes, aad: bytes, ct: bytes) -> bytes:
    h = hmac.new(mac, MAGIC + nonce + len(aad).to_bytes(4, "little") + aad, hashlib.sha256)
    h.update(ct)
    return h.digest()


def seal(key: bytes, data: bytes, aad: bytes = b"") -> bytes:
    enc, mac = _subkeys(key)
    nonce = os.urandom(NONCE)
    ct = _xor_stream(enc, nonce, data)
    return MAGIC + nonce + ct + _tag(mac, nonce, aad, ct)


def open_(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if not is_sealed(blob) or len(blob) < len(MAGIC) + NONCE + TAG:
        raise CryptoError("это не зашифрованный файл OpsDigest")
    enc, mac = _subkeys(key)
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE]
    ct, tag = blob[len(MAGIC) + NONCE:-TAG], blob[-TAG:]
    if not hmac.compare_digest(tag, _tag(mac, nonce, aad, ct)):
        raise CryptoError("неверный ключ или файл повреждён")
    return _xor_stream(enc, nonce, ct)


def is_sealed(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


# ---------------------------------------------------------------- ключи из пароля
def kdf_params() -> dict:
    if hasattr(hashlib, "scrypt"):
        try:
            hashlib.scrypt(b"x", salt=b"y" * 16, n=2 ** 4, r=8, p=1, dklen=32)
            return {"kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1}
        except (ValueError, OSError):
            pass
    return {"kdf": "pbkdf2", "iter": 600_000}


def derive(password: str, salt: bytes, params: dict) -> bytes:
    pw = password.encode("utf-8")
    if params.get("kdf") == "scrypt":
        return hashlib.scrypt(pw, salt=salt, n=int(params["n"]), r=int(params["r"]), p=int(params["p"]),
                              maxmem=256 * 1024 * 1024, dklen=32)
    return hashlib.pbkdf2_hmac("sha256", pw, salt, int(params.get("iter") or 600_000), dklen=32)


def new_key() -> bytes:
    return secrets.token_bytes(32)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def recovery_code() -> str:
    """Код восстановления: 24 знака (буквы и цифры без похожих), группами по 4."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(24))
    return "-".join(raw[i:i + 4] for i in range(0, 24, 4))


def norm_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum()).replace("0", "O")
