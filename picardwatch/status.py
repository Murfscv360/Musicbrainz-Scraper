"""On-demand progress snapshot: albums + tracks moved, remaining, ETA, and whether a
run is currently active. Read-only against the SQLite state, so it can be invoked while
a --watch/--once run is in progress."""

from __future__ import annotations

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


def _remaining_eta(cfg, state):
    counts = state.status_counts()
    input_root = Path(cfg.paths.input)
    total_dirs = 0
    if input_root.exists():
        try:
            total_dirs = sum(1 for c in input_root.iterdir() if c.is_dir())
        except OSError:
            total_dirs = 0
    # imported folders are deleted; review/duplicate/failed remain on disk but won't reprocess
    decided_present = counts.get("review", 0) + counts.get("duplicate", 0) + counts.get("failed", 0)
    remaining = max(0, total_dirs - decided_present)
    rate = state.recent_rate_per_min(15)
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
    return (f"{c.get('imported', 0)} albums / {state.imported_track_total()} tracks moved | "
            f"{c.get('review', 0)} review, {c.get('duplicate', 0)} dup | "
            f"~{remaining} left | ETA {_fmt_eta(eta)}")


def render(cfg, state) -> str:
    c = state.status_counts()
    total, remaining, rate, eta = _remaining_eta(cfg, state)
    return "\n".join([
        "=== PicardWatch status ===",
        f"  run active        : {'YES' if is_run_active(cfg) else 'no'}",
        f"  albums imported   : {c.get('imported', 0)}   ({state.imported_track_total()} tracks moved)",
        f"  left for review   : {c.get('review', 0)}",
        f"  duplicates        : {c.get('duplicate', 0)}",
        f"  failed            : {c.get('failed', 0)}",
        f"  input folders     : {total} on disk",
        f"  still to process  : ~{remaining} (estimate)",
        f"  current pace      : {rate:.1f} albums/min",
        f"  est. time left    : {_fmt_eta(eta)}",
        "==========================",
    ])
