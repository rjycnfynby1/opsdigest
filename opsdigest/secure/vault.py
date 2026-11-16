"""Хранилище ключа рабочей папки: пароль, код восстановления, ПИН, блокировка.

data/vault.json (открытый файл без секретов):
  ключ данных (случайный) «обёрнут» ключом из пароля и отдельно — ключом из кода восстановления;
  ПИН и настройки (минуты до блокировки, нужен ли вход в Telegram, владелец) — зашифрованы ключом данных.

Состояния процесса:
  locked "full"  — ключа в памяти нет: нужен пароль (запуск программы, «Выход», 5 неверных ПИН);
  locked "pin"   — ключ в памяти, окно закрыто экраном ПИН (бездействие N минут, кнопка «Заблокировать»);
                   фоновые задачи (выгрузка, синхронизация) продолжают работать;
  открыто        — ключ в памяти, окно доступно.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import atomic, crypto

VAULT = "data/vault.json"
MAX_PIN_TRIES = 5
DEFAULT_MINUTES = 10


class Locked(Exception):
    """Данные зашифрованы, а ключа в памяти нет (нужен пароль)."""


class BadSecret(Exception):
    """Неверный пароль, код восстановления или ПИН."""


class _State:
    def __init__(self):
        self.ws: Path | None = None
        self.key: bytes | None = None
        self.settings: dict = {}
        self.pin: dict = {}
        self.pin_locked = False
        self.pin_tries = 0
        self.last_activity = time.time()
        self.lock = threading.RLock()
        self.on_full_lock: list = []   # сохранить базу и т.п. перед тем, как забыть ключ
        self.on_open: list = []        # ключ появился (пароль, код восстановления, новая защита) — fn(ws, key)
        self.lock_epoch = 0            # растёт при каждой блокировке (ПИН или полной) — «открытые» окна доступа гаснут


S = _State()


def vault_path(ws: str | Path) -> Path:
    return Path(ws) / VAULT


def exists(ws: str | Path | None = None) -> bool:
    w = ws or S.ws
    return bool(w) and vault_path(w).is_file()


def bind(ws: str | Path | None) -> None:
    """Текущая рабочая папка процесса. Смена папки — полная блокировка."""
    new = Path(ws).resolve() if ws else None
    with S.lock:
        if S.ws is not None and new != S.ws:
            lock_full()
        S.ws = new


def bound() -> Path | None:
    return S.ws


def protected(path: str | Path | None = None) -> bool:
    """Включено ли шифрование для текущей рабочей папки (и лежит ли путь в ней)."""
    if S.ws is None or not exists(S.ws):
        return False
    if path is None:
        return True
    try:
        Path(path).resolve().relative_to(S.ws)
        return True
    except (ValueError, OSError):
        return False


def has_key() -> bool:
    return S.key is not None


def key() -> bytes:
    if S.key is None:
        raise Locked("данные зашифрованы — введите пароль OpsDigest")
    return S.key


def _read(ws: Path) -> dict:
    return json.loads(atomic.read_bytes(vault_path(ws)).decode("utf-8"))


def _write(ws: Path, data: dict) -> None:
    atomic.write_bytes(vault_path(ws), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def _wrap(secret: str, dek: bytes) -> dict:
    params = crypto.kdf_params()
    salt = os.urandom(16)
    kek = crypto.derive(secret, salt, params)
    return {"params": params, "salt": crypto.b64(salt), "key": crypto.b64(crypto.seal(kek, dek, b"vault"))}


def _unwrap(secret: str, rec: dict) -> bytes:
    kek = crypto.derive(secret, crypto.unb64(rec["salt"]), rec["params"])
    try:
        return crypto.open_(kek, crypto.unb64(rec["key"]), b"vault")
    except crypto.CryptoError:
        raise BadSecret("неверный пароль") from None


def _pin_record(pin: str) -> dict:
    salt = os.urandom(16)
    params = {"kdf": "pbkdf2", "iter": 200_000}
    return {"salt": crypto.b64(salt), "hash": crypto.b64(crypto.derive(pin, salt, params)), "len": len(pin)}


def _seal_json(dek: bytes, obj: dict) -> str:
    return crypto.b64(crypto.seal(dek, json.dumps(obj, ensure_ascii=False).encode("utf-8"), b"vault-json"))


def _open_json(dek: bytes, s: str) -> dict:
    return json.loads(crypto.open_(dek, crypto.unb64(s), b"vault-json").decode("utf-8"))


def check_password_rules(password: str) -> str | None:
    if len(password or "") < 8:
        return "пароль — не короче 8 символов"
    return None


def check_pin_rules(pin: str) -> str | None:
    if not (pin or "").isdigit() or not 4 <= len(pin) <= 6:
        return "ПИН — от 4 до 6 цифр"
    return None


# ---------------------------------------------------------------- создание и вход
def create(ws: str | Path, password: str, pin: str, settings: dict | None = None) -> str:
    """Создать хранилище: ключ данных, пароль, код восстановления, ПИН. Возвращает код восстановления."""
    ws = Path(ws).resolve()
    if exists(ws):
        raise ValueError("защита уже включена")
    err = check_password_rules(password) or check_pin_rules(pin)
    if err:
        raise ValueError(err)
    dek = crypto.new_key()
    code = crypto.recovery_code()
    st = {"lock_minutes": DEFAULT_MINUTES, "require_telegram": True, "owner": None, **(settings or {})}
    data = {"v": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "password": _wrap(password, dek),
            "recovery": _wrap(crypto.norm_code(code), dek), "pin": _seal_json(dek, _pin_record(pin)),
            "settings": _seal_json(dek, st), "check": _seal_json(dek, {"ok": True})}
    _write(ws, data)
    with S.lock:
        S.ws = ws
        _opened(dek, data)
    return code


def _opened(dek: bytes, data: dict, pin_locked: bool = False) -> None:
    S.key = dek
    S.pin = _open_json(dek, data["pin"])
    S.settings = _open_json(dek, data["settings"])
    S.pin_locked = pin_locked
    S.pin_tries = 0
    S.last_activity = time.time()
    for fn in list(S.on_open):
        try:
            fn(S.ws, dek)
        except Exception:  # noqa: BLE001
            pass


def adopt(dek: bytes, ws: str | Path | None = None) -> bool:
    """Ключ данных, который сохранил сторож, пока сервер перезапускался после сбоя: данные открыты для фоновых
    задач, окно — под ПИН-кодом. False — ключ не от этой папки."""
    ws = Path(ws).resolve() if ws else S.ws
    if ws is None or not exists(ws) or (S.ws is not None and ws != S.ws):
        return False
    data = _read(ws)
    try:
        if not _open_json(dek, data["check"]).get("ok"):
            return False
    except (crypto.CryptoError, KeyError, ValueError):
        return False
    with S.lock:
        if S.key is not None:
            return True
        S.ws = ws
        _opened(dek, data, pin_locked=True)
    return True


def unlock(password: str, ws: str | Path | None = None) -> None:
    ws = Path(ws).resolve() if ws else S.ws
    if ws is None or not exists(ws):
        raise ValueError("защита не включена")
    data = _read(ws)
    dek = _unwrap(password, data["password"])
    with S.lock:
        S.ws = ws
        _opened(dek, data)


def recover(code: str, new_password: str, ws: str | Path | None = None) -> None:
    """Вход по коду восстановления с новым паролем."""
    ws = Path(ws).resolve() if ws else S.ws
    err = check_password_rules(new_password)
    if err:
        raise ValueError(err)
    data = _read(ws)
    try:
        dek = _unwrap(crypto.norm_code(code), data["recovery"])
    except BadSecret:
        raise BadSecret("неверный код восстановления") from None
    data["password"] = _wrap(new_password, dek)
    _write(ws, data)
    with S.lock:
        S.ws = ws
        _opened(dek, data)


def check_recovery(code: str) -> bool:
    """Верен ли код восстановления (ничего не меняет)."""
    try:
        return _unwrap(crypto.norm_code(code), _read(S.ws)["recovery"]) == S.key
    except (BadSecret, KeyError, ValueError, OSError):
        return False


def change_password(old: str, new: str) -> None:
    data = _read(S.ws)
    dek = _unwrap(old, data["password"])
    err = check_password_rules(new)
    if err:
        raise ValueError(err)
    data["password"] = _wrap(new, dek)
    _write(S.ws, data)


def set_pin(pin: str) -> None:
    err = check_pin_rules(pin)
    if err:
        raise ValueError(err)
    data = _read(S.ws)
    rec = _pin_record(pin)
    data["pin"] = _seal_json(key(), rec)
    _write(S.ws, data)
    S.pin = rec


def update_settings(**kw) -> dict:
    data = _read(S.ws)
    st = {**S.settings, **{k: v for k, v in kw.items() if v is not None}}
    data["settings"] = _seal_json(key(), st)
    _write(S.ws, data)
    S.settings = st
    return st


def settings() -> dict:
    return dict(S.settings)


# ---------------------------------------------------------------- блокировка
def lock_minutes() -> float:
    try:
        return max(1.0, min(120.0, float(S.settings.get("lock_minutes") or DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        return float(DEFAULT_MINUTES)


def touch() -> None:
    """Пользователь что-то делает в окне — откладываем блокировку."""
    if not state_pin_locked():
        S.last_activity = time.time()


def state_pin_locked() -> bool:
    if S.key is None:
        return False
    if not S.pin_locked and time.time() - S.last_activity > lock_minutes() * 60:
        S.pin_locked = True
        S.lock_epoch += 1
    return S.pin_locked


def lock_pin() -> None:
    if S.key is not None:
        S.pin_locked = True
        S.lock_epoch += 1


def check_password(password: str) -> None:
    """Повторный ввод пароля (например, чтобы показать сохранённый пароль из «Учётных данных»). Неверный — BadSecret."""
    if S.key is None:
        raise Locked("нужен пароль")
    if _unwrap(password or "", _read(S.ws)["password"]) != S.key:
        raise BadSecret("неверный пароль")


def unlock_pin(pin: str) -> None:
    if S.key is None:
        raise Locked("нужен пароль")
    rec = S.pin
    ok = bool(rec) and crypto.hmac.compare_digest(
        crypto.derive(pin or "", crypto.unb64(rec["salt"]), {"kdf": "pbkdf2", "iter": 200_000}),
        crypto.unb64(rec["hash"]))
    if not ok:
        S.pin_tries += 1
        left = MAX_PIN_TRIES - S.pin_tries
        if left <= 0:
            lock_full()
            raise BadSecret("ПИН неверный 5 раз — программа заблокирована полностью, нужен пароль")
        raise BadSecret(f"неверный ПИН, осталось попыток: {left}")
    S.pin_locked = False
    S.pin_tries = 0
    S.last_activity = time.time()


def lock_full() -> None:
    """Сохранить всё и забыть ключ."""
    with S.lock:
        if S.key is None:
            return
        for fn in list(S.on_full_lock):
            try:
                fn()
            except Exception:  # noqa: BLE001 — блокировка важнее
                pass
        S.key = None
        S.pin = {}
        S.settings = {}
        S.pin_locked = False
        S.pin_tries = 0
        S.lock_epoch += 1


def status() -> str:
    """none (защита не включена) | full | pin | open"""
    if S.ws is None or not exists(S.ws):
        return "none"
    if S.key is None:
        return "full"
    if state_pin_locked():
        return "pin"
    return "open"


def pin_length() -> int:
    return int((S.pin or {}).get("len") or 4)
