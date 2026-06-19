"""Supervisor: keep `run.py --watch` alive.

Launches the watcher and restarts it whenever it **exits** (crash) or **hangs** (no
watcher-log activity for `hang_timeout`). Single-instance; exponential backoff on rapid
failures. Honours the cooperative stop signal (run.py --stop) — it lets the watcher
finish the current album and then exits without relaunching. Keeps the PC awake while it
runs (the display may still sleep). Logs to its own file so its messages don't mask a hang.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import control, power

log = logging.getLogger("picardwatch.supervisor")


def supervise(cfg) -> None:
    project = Path(__file__).resolve().parent.parent
    runpy = str(project / "run.py")
    watch_log = Path(cfg.paths.log_dir) / "picardwatch.log"
    sup = getattr(cfg, "supervisor", None)
    hang_timeout = float(getattr(sup, "hang_timeout", 1200))
    check_interval = float(getattr(sup, "check_interval", 30))
    restart_delay = float(getattr(sup, "restart_delay", 5))

    control.clear_stop(cfg)   # a fresh supervisor ignores any stale stop flag
    if bool(getattr(sup, "keep_awake", True)):
        power.keep_awake()
    log.info("Supervisor started (pid %s); hang_timeout=%ds, check every %ds.",
             os.getpid(), int(hang_timeout), int(check_interval))

    consec_fast = 0
    try:
        while True:
            log.info("Launching watcher (run.py --watch)...")
            started = time.time()
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen([sys.executable, runpy, "--watch"], cwd=str(project),
                                    creationflags=flags)
            reason = _monitor(proc, cfg, watch_log, started, hang_timeout, check_interval)
            ran = time.time() - started
            log.warning("Watcher stopped (%s) after %ds.", reason, int(ran))

            if control.stop_requested(cfg):
                log.info("Stop requested - supervisor shutting down (not restarting).")
                control.clear_stop(cfg)
                return
            consec_fast = consec_fast + 1 if ran < 60 else 0
            delay = min(restart_delay * (2 ** min(consec_fast, 6)), 300)
            if consec_fast:
                log.warning("Rapid restart #%d; backing off %ds.", consec_fast, int(delay))
            time.sleep(delay)
    finally:
        power.allow_sleep()


def _monitor(proc, cfg, watch_log: Path, started: float, hang_timeout: float, check_interval: float) -> str:
    stop_since = None
    while True:
        time.sleep(check_interval)
        if proc.poll() is not None:
            return f"exited code {proc.returncode}"

        if control.stop_requested(cfg):
            if stop_since is None:
                stop_since = time.time()
                log.info("Stop requested - waiting for the watcher to finish the current album...")
            elif time.time() - stop_since > 150:   # grace period, then force
                log.warning("Watcher didn't exit in time; killing.")
                _kill_tree(proc.pid)
                _wait(proc)
                return "stopped (forced)"
            continue

        try:
            mtime = watch_log.stat().st_mtime
        except OSError:
            mtime = 0.0
        # Only flag a hang once the watcher has written something newer than its launch.
        if mtime > started and (time.time() - mtime) > hang_timeout:
            log.warning("No watcher activity for %ds -> killing (hung).", int(time.time() - mtime))
            _kill_tree(proc.pid)
            _wait(proc)
            return "hung"


def _wait(proc) -> None:
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def _kill_tree(pid: int) -> None:
    """Kill the watcher process and its children (the venv launcher spawns a child)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
