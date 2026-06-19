"""Watch the input folder and hand stable album folders to a callback.

Event-driven (watchdog wakes us immediately on filesystem changes) with a periodic
full rescan as a backstop. A folder is processed only once it is STABLE:
  * contains no in-progress download markers (.part, .crdownload, ...), and
  * nothing has been written to it for `stability_seconds`.
This avoids grabbing a half-finished copy/torrent — the classic watch-folder bug.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import control, power

log = logging.getLogger("picardwatch.watcher")


def is_stable(folder: Path, cfg) -> bool:
    ignore = {s.lower() for s in cfg.watcher.ignore_suffixes}
    newest = 0.0
    found = False
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in ignore:
            return False  # an in-progress download marker is present
        found = True
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    if not found:
        return False
    return (time.time() - newest) >= cfg.watcher.stability_seconds


def watch(cfg, handler: Callable[[Path], None]) -> None:
    input_root = Path(cfg.paths.input)
    if not input_root.exists():
        raise SystemExit(f"Input folder does not exist: {input_root}")

    wake = threading.Event()

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            wake.set()

    observer = Observer()
    observer.schedule(_Handler(), str(input_root), recursive=True)
    observer.start()
    if bool(getattr(getattr(cfg, "supervisor", None), "keep_awake", True)):
        power.keep_awake()
    log.info("Watching %s (stop with: run.py --stop  or  stop.ps1)", input_root)

    try:
        while True:
            if control.stop_requested(cfg):
                log.info("Stop requested - shutting down watcher.")
                break
            _scan(input_root, cfg, handler)
            if control.stop_requested(cfg):
                log.info("Stop requested - shutting down watcher.")
                break
            # Wait for filesystem events or the rescan interval, waking every ~15s to
            # re-check the stop flag so shutdown stays responsive even while idle.
            waited = 0.0
            interval = float(cfg.watcher.rescan_interval_sec)
            while waited < interval and not control.stop_requested(cfg):
                if wake.wait(timeout=min(15.0, interval - waited)):
                    wake.clear()
                    time.sleep(cfg.watcher.poll_interval_sec)
                    break
                waited += 15.0
    except KeyboardInterrupt:
        log.info("Stopping watcher.")
    finally:
        observer.stop()
        observer.join()
        power.allow_sleep()


def _scan(input_root: Path, cfg, handler: Callable[[Path], None]) -> None:
    children = [c for c in sorted(input_root.iterdir()) if c.is_dir()]
    log.info("Scanning %d folders in %s ...", len(children), input_root)
    for i, child in enumerate(children, 1):
        if control.stop_requested(cfg):
            log.info("Stop requested - halting scan.")
            return
        if i % 200 == 0:
            log.info("  ...scanned %d/%d folders", i, len(children))  # heartbeat during the long sweep
        if not is_stable(child, cfg):
            log.debug("Not stable yet: %s", child.name)
            continue
        try:
            handler(child)
        except Exception:
            log.exception("Error processing %s", child)
