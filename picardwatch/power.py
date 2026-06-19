"""Keep Windows awake while the importer runs — but let the screen turn off.

Uses SetThreadExecutionState with ES_SYSTEM_REQUIRED (stay awake) and deliberately
WITHOUT ES_DISPLAY_REQUIRED, so the monitor is still allowed to blank / power-save.
The request lasts until allow_sleep() or the process exits. Windows-only; no-op elsewhere.
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("picardwatch.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# ES_DISPLAY_REQUIRED is intentionally omitted -> the display may still sleep.


def keep_awake() -> bool:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Keeping the PC awake (display may still sleep).")
        return True
    except (AttributeError, OSError):
        return False  # not Windows / unavailable


def allow_sleep() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except (AttributeError, OSError):
        pass
