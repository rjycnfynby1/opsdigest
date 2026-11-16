"""Не давать Windows засыпать, пока идут задачи (сводка, ИИ, выгрузка истории, ИИ-описания и т.п.).

Запросы питания (PowerCreateRequest/PowerSetRequest, действуют на весь процесс):
  * SystemRequired + ExecutionRequired — компьютер не уходит в сон по бездействию, процесс не замораживают;
  * DisplayRequired (если включено «не гасить экран») — экран не гаснет. Это нужно на ноутбуках с Modern Standby
    («S0 Low Power Idle»): там от батареи Windows снимает запрос «система нужна» через несколько минут после
    таймаута сна, а сон наступает вместе с выключением экрана — и задачи стоят до пробуждения. Пока экран включён,
    компьютер не спит ни от сети, ни от батареи.
Сон, выбранный явно (крышка ноутбука, кнопка питания, «Пуск → Спящий режим»), Windows выполнит всё равно — тогда
задачи продолжаются после пробуждения.

Ещё: процесс просит Windows не замедлять его (Power Throttling / EcoQoS): программа без видимого окна иначе
считается фоновой и на батарее получает урезанный процессор.
snapshot() — от сети ли компьютер, заряд, есть ли Modern Standby (для окна «Задачи» и журнала).
На других системах — ничего не делает.
"""
from __future__ import annotations

import sys
import threading

from . import logs

log = logs.get("power")

POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
DISPLAY_REQUIRED, SYSTEM_REQUIRED, EXECUTION_REQUIRED = 0, 1, 3
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


class KeepAwake:
    """set(True[, display=True]) — держать систему (и экран), set(False) — отпустить. Повторные вызовы с теми же
    значениями ничего не делают."""

    def __init__(self, reason: str = "OpsDigest: идут задачи (сводка, ИИ, выгрузка истории)"):
        self.reason = reason
        self.active = False
        self.display = False
        self.supported = sys.platform == "win32"
        self._h = None
        self._lock = threading.Lock()

    def set(self, on: bool, display: bool = False) -> bool:
        on = bool(on)
        display = bool(display) and on
        with self._lock:
            if on == self.active and display == self.display:
                return self.active
            if self.supported:
                try:
                    self._win(on, display)
                except Exception as e:  # noqa: BLE001 — не мешаем задачам, если Windows отказала
                    log.warning("запрос питания не удался: %s", logs.err_info(e))
                    return self.active
            changed_on = on != self.active
            self.active, self.display = on, display
            if changed_on or display:
                log.info("сон компьютера %s%s", "запрещён, пока идут задачи" if on else "снова разрешён",
                         " (экран не гаснет)" if display else "")
            return self.active

    def _win(self, on: bool, display: bool) -> None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        if self._h is None:
            class REASON_CONTEXT(ctypes.Structure):  # noqa: N801 — имя из winbase.h
                _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("SimpleReasonString", wintypes.LPWSTR)]
            k.PowerCreateRequest.restype = wintypes.HANDLE
            k.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
            self._ctx = REASON_CONTEXT(POWER_REQUEST_CONTEXT_VERSION, POWER_REQUEST_CONTEXT_SIMPLE_STRING, self.reason)
            h = k.PowerCreateRequest(ctypes.byref(self._ctx))
            if not h or h == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), "PowerCreateRequest")
            self._h = h
        k.PowerSetRequest.argtypes = k.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]

        def req(kind: int, want: bool, was: bool) -> bool:
            if want == was:
                return True
            return bool((k.PowerSetRequest if want else k.PowerClearRequest)(self._h, kind))
        ok = req(SYSTEM_REQUIRED, on, self.active)
        req(EXECUTION_REQUIRED, on, self.active)  # Windows 8+: процесс не замораживают в Modern Standby (если позволит политика)
        ok_d = req(DISPLAY_REQUIRED, display, self.display)
        if not ok or not ok_d:  # старый способ — на случай, если запросы питания запрещены
            k.SetThreadExecutionState(ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0))
                                                       if on else 0))


AWAKE = KeepAwake()
_SNAP: dict = {}


def snapshot() -> dict:
    """{"ac": от сети?, "battery": заряд %, "modern_standby": есть ли Modern Standby} — что удалось узнать."""
    if sys.platform != "win32":
        return {}
    out: dict = {}
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):  # noqa: N801
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]
        sps = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            out["ac"] = {0: False, 1: True}.get(sps.ACLineStatus)
            if sps.BatteryLifePercent <= 100 and not sps.BatteryFlag & 128:
                out["battery"] = int(sps.BatteryLifePercent)
    except Exception:  # noqa: BLE001
        pass
    if "modern_standby" not in _SNAP:
        _SNAP["modern_standby"] = _modern_standby()
    if _SNAP["modern_standby"] is not None:
        out["modern_standby"] = _SNAP["modern_standby"]
    return out


def _modern_standby() -> bool | None:
    """SYSTEM_POWER_CAPABILITIES.AoAc — компьютер засыпает «как телефон» (Modern Standby, S0 Low Power Idle)."""
    try:
        import ctypes
        buf = (ctypes.c_ubyte * 256)()
        rc = ctypes.WinDLL("powrprof").CallNtPowerInformation(4, None, 0, ctypes.byref(buf), 76)  # SystemPowerCapabilities
        if rc != 0:
            return None
        return bool(buf[20])   # AoAc: 21-й BOOLEAN структуры
    except Exception:  # noqa: BLE001
        return None


def no_throttling() -> bool:
    """Попросить Windows не урезать процессор этому процессу (Power Throttling / EcoQoS) и не огрублять таймеры."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):  # noqa: N801
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]
        st = PROCESS_POWER_THROTTLING_STATE(1, 0x1 | 0x4, 0)   # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION → выключить
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        ok = bool(k.SetProcessInformation(k.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st)))
        if not ok:
            log.info("Power Throttling не выключен: код %s", ctypes.get_last_error())
        return ok
    except Exception as e:  # noqa: BLE001
        log.info("Power Throttling не выключен: %s", logs.err_info(e))
        return False
