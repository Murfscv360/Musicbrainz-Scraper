"""Disk-space guard: don't fill the drive while importing.

Before each album is moved/written, check free space on the library (destination) and
input drives. If it drops below `space.min_free_gb`, either PAUSE imports until space
frees up (default, auto-resumes) or STOP cleanly — configurable via `space.on_low`.
Pausing logs a heartbeat so the supervisor doesn't mistake it for a hang.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from . import control

log = logging.getLogger("picardwatch.diskspace")


def free_gb(path) -> float:
    """Free space (GiB) on the volume holding `path`; +inf if it can't be determined."""
    p = Path(path)
    # walk up to the first existing parent (the path itself may not exist yet)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free / (1024 ** 3)
    except OSError:
        return float("inf")


def _low(cfg):
    sp = getattr(cfg, "space", None)
    min_free = float(getattr(sp, "min_free_gb", 10))
    seen = set()
    offenders = []
    for label, target in (("library", cfg.paths.library), ("input", cfg.paths.input)):
        drive = str(Path(target).resolve().drive or target).lower()
        if drive in seen:           # input + library on the same volume -> check once
            continue
        seen.add(drive)
        g = free_gb(target)
        if g < min_free:
            offenders.append(f"{label} {target} = {g:.1f} GB free")
    return offenders, min_free


def ensure_space(cfg) -> bool:
    """Return True if it's safe to import now. If space is low: STOP (request shutdown,
    return False) or PAUSE here until space frees up / a stop is requested."""
    sp = getattr(cfg, "space", None)
    on_low = str(getattr(sp, "on_low", "pause")).lower()
    offenders, min_free = _low(cfg)
    if not offenders:
        return True

    if on_low == "stop":
        log.error("LOW DISK SPACE (%s; need >= %.0f GB) - stopping cleanly.",
                  "; ".join(offenders), min_free)
        control.request_stop(cfg)
        return False

    log.warning("LOW DISK SPACE (%s; need >= %.0f GB) - PAUSING imports until space frees up.",
                "; ".join(offenders), min_free)
    while True:
        if control.stop_requested(cfg):
            return False
        time.sleep(60)
        offenders, _ = _low(cfg)
        if not offenders:
            log.info("Disk space recovered - resuming imports.")
            return True
        log.warning("Still low on disk space (%s) - waiting...", "; ".join(offenders))
