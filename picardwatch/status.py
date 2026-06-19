"""On-demand progress snapshot: albums + tracks moved, remaining, ETA, and whether a
run is currently active. Read-only against the SQLite state, so it can be invoked while
a --watch/--once run is in progress."""

from __future__ import annotations

import threading
from pathlib import Path


def _fmt_eta(minutes) -> str:
    if not minutes or minutes <= 0:
        return "unknown"
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _count_input_dirs(input_root: Path, timeout: float = 4.0):
    """Count immediate subfolders, but give up after `timeout` so a busy network
    drive can't hang the status query. Returns None if it didn't finish in time."""
    result = {"n": None}

    def run():
        try:
            result["n"] = sum(1 for c in input_root.iterdir() if c.is_dir())
        except OSError:
            result["n"] = None

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    return result["n"]


def _remaining_eta(cfg, state):
    counts = state.status_counts()
    input_root = Path(cfg.paths.input)
    total_dirs = _count_input_dirs(input_root) if input_root.exists() else 0
    rate = state.recent_rate_per_min(15)
    if total_dirs is None:        # listing timed out (drive busy) -> remaining unknown
        return None, None, rate, None
    # imported folders are deleted; review/duplicate/failed remain on disk but won't reprocess
    decided_present = counts.get("review", 0) + counts.get("duplicate", 0) + counts.get("failed", 0)
    remaining = max(0, total_dirs - decided_present)
    eta = (remaining / rate) if rate > 0 else None
    return total_dirs, remaining, rate, eta


def is_run_active(cfg) -> bool:
    """True if a --once/--watch run currently holds the single-instance lock."""
    lock_path = Path(cfg.paths.state_db).resolve().parent / "picardwatch.lock"
    if not lock_path.exists():
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        fh = open(lock_path, "r+")
    except OSError:
        return True
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def oneline(cfg, state) -> str:
    c = state.status_counts()
    _, remaining, _, eta = _remaining_eta(cfg, state)
    left = "?" if remaining is None else str(remaining)
    eta_s = "?" if remaining is None else _fmt_eta(eta)
    return (f"{c.get('imported', 0)} albums / {state.imported_track_total()} tracks moved | "
            f"{c.get('review', 0)} review, {c.get('duplicate', 0)} dup | "
            f"~{left} left | ETA {eta_s}")


def render(cfg, state) -> str:
    c = state.status_counts()
    total, remaining, rate, eta = _remaining_eta(cfg, state)
    total_s = "scan skipped (drive busy)" if total is None else f"{total} on disk"
    rem_s = "n/a (drive busy — try when idle)" if remaining is None else f"~{remaining} (estimate)"
    eta_s = "n/a" if remaining is None else _fmt_eta(eta)
    return "\n".join([
        "=== PicardWatch status ===",
        f"  run active        : {'YES' if is_run_active(cfg) else 'no'}",
        f"  albums imported   : {c.get('imported', 0)}   ({state.imported_track_total()} tracks moved)",
        f"  left for review   : {c.get('review', 0)}",
        f"  duplicates        : {c.get('duplicate', 0)}",
        f"  failed            : {c.get('failed', 0)}",
        f"  input folders     : {total_s}",
        f"  still to process  : {rem_s}",
        f"  current pace      : {rate:.1f} albums/min",
        f"  est. time left    : {eta_s}",
        "==========================",
    ])
