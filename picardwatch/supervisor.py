"""Supervisor: keep `run.py --watch` alive.

Launches the watcher and restarts it whenever it **exits** (crash) or **hangs** (no
watcher-log activity for `hang_timeout`). Single-instance; exponential backoff on rapid
failures. The supervisor logs to its own file (`supervisor.log`) so its messages don't
update the watcher's log and mask a real hang.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("picardwatch.supervisor")


def supervise(cfg) -> None:
    project = Path(__file__).resolve().parent.parent
    runpy = str(project / "run.py")
    watch_log = Path(cfg.paths.log_dir) / "picardwatch.log"
    sup = getattr(cfg, "supervisor", None)
    hang_timeout = float(getattr(sup, "hang_timeout", 1200))
    check_interval = float(getattr(sup, "check_interval", 30))
    restart_delay = float(getattr(sup, "restart_delay", 5))

    log.info("Supervisor started (pid %s); hang_timeout=%ds, check every %ds.",
             os.getpid(), int(hang_timeout), int(check_interval))
    consec_fast = 0
    while True:
        log.info("Launching watcher (run.py --watch)...")
        started = time.time()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, runpy, "--watch"], cwd=str(project),
                                 creationflags=flags)
        reason = _monitor(proc, watch_log, started, hang_timeout, check_interval)
        ran = time.time() - started
        log.warning("Watcher stopped (%s) after %ds.", reason, int(ran))

        consec_fast = consec_fast + 1 if ran < 60 else 0
        delay = min(restart_delay * (2 ** min(consec_fast, 6)), 300)
        if consec_fast:
            log.warning("Rapid restart #%d; backing off %ds.", consec_fast, int(delay))
        time.sleep(delay)


def _monitor(proc, watch_log: Path, started: float, hang_timeout: float, check_interval: float) -> str:
    while True:
        time.sleep(check_interval)
        if proc.poll() is not None:
            return f"exited code {proc.returncode}"
        try:
            mtime = watch_log.stat().st_mtime
        except OSError:
            mtime = 0.0
        # Only flag a hang once the watcher has written something newer than its launch
        # (so a stale log from a previous run doesn't trigger an instant false restart).
        if mtime > started and (time.time() - mtime) > hang_timeout:
            log.warning("No watcher activity for %ds -> killing (hung).", int(time.time() - mtime))
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except Exception:
                pass
            return "hung"


def _kill_tree(pid: int) -> None:
    """Kill the watcher process and its children (the venv launcher spawns a child)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
