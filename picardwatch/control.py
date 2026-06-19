"""Cooperative stop signal.

A small flag file that the supervisor and watcher poll, so they can be shut down
*cleanly* — finish the album in progress, then exit — instead of being force-killed
(which could leave an album half-moved). `run.py --stop` (or stop.ps1) sets the flag.
"""

from __future__ import annotations

from pathlib import Path


def stop_path(cfg) -> Path:
    return Path(cfg.paths.state_db).resolve().parent / "picardwatch.stop"


def request_stop(cfg) -> None:
    stop_path(cfg).write_text("stop", encoding="utf-8")


def stop_requested(cfg) -> bool:
    return stop_path(cfg).exists()


def clear_stop(cfg) -> None:
    try:
        stop_path(cfg).unlink()
    except OSError:
        pass
