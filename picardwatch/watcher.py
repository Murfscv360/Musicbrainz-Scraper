"""Watch the input folder(s) and hand stable album folders to a callback.

Event-driven (watchdog wakes us immediately on filesystem changes) with a periodic
full rescan as a backstop. A folder is processed only once it is STABLE:
  * contains no in-progress download markers (.part, .crdownload, ...), and
  * nothing has been written to it for `stability_seconds`.
This avoids grabbing a half-finished copy/torrent — the classic watch-folder bug.

Multiple input roots are supported (primary `paths.input` plus `paths.extra_inputs`),
and each root may be flat (Release/*.flac) or a nested library (Genre/Artist/Album/...);
`discovery.find_albums` figures out which folders are actual albums.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import cleanup, control, discovery, power

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
    roots = [Path(r) for r in discovery.input_roots(cfg)]
    existing = [r for r in roots if r.exists()]
    for missing in (r for r in roots if not r.exists()):
        log.warning("Input folder does not exist (skipping): %s", missing)
    if not existing:
        raise SystemExit("No input folders exist: " + ", ".join(str(r) for r in roots))

    wake = threading.Event()

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            wake.set()

    observer = Observer()
    for r in existing:
        observer.schedule(_Handler(), str(r), recursive=True)
    observer.start()
    if bool(getattr(getattr(cfg, "supervisor", None), "keep_awake", True)):
        power.keep_awake()
    log.info("Watching %d folder(s): %s  (stop with: run.py --stop  or  stop.ps1)",
             len(existing), "; ".join(str(r) for r in existing))

    # One-shot tidy of leftover empty husks in nested libraries — run in a daemon thread
    # so it never blocks the main scan loop (D: can have thousands of folders).
    extras = {str(Path(r).resolve()) for r in (getattr(cfg.paths, "extra_inputs", []) or [])}
    def _startup_sweep():
        for r in existing:
            try:
                if str(r.resolve()) in extras:
                    cleanup.sweep_empty_dirs(r)
            except Exception:
                log.exception("Startup cleanup sweep failed for %s", r)
    threading.Thread(target=_startup_sweep, daemon=True, name="startup-sweep").start()

    try:
        while True:
            if control.stop_requested(cfg):
                log.info("Stop requested - shutting down watcher.")
                break
            for r in existing:
                if control.stop_requested(cfg):
                    break
                _scan(r, cfg, handler)
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


def _scan(root: Path, cfg, handler: Callable[[Path], None]) -> None:
    log.info("Scanning %s ...", root)
    n = 0
    try:
        for album in discovery.find_albums(root, cfg.judge.audio_extensions):
            if control.stop_requested(cfg):
                log.info("Stop requested - halting scan.")
                return
            n += 1
            if n % 200 == 0:
                log.info("  ...%d album folders scanned in %s", n, root)  # heartbeat
            try:
                stable = is_stable(album, cfg)
            except OSError as exc:                    # a single flaky-drive stat must not abort
                log.warning("Filesystem error on %s: %s (skipping)", album, exc)
                continue
            if not stable:
                log.debug("Not stable yet: %s", album.name)
                continue
            try:
                handler(album)
            except Exception:
                log.exception("Error processing %s", album)
    except OSError as exc:
        # an SMB/network error mid-walk must NOT crash the watcher (it was exiting code 1 and
        # crash-looping on the slow Y: drive) — log and retry on the next cycle.
        log.warning("Scan of %s interrupted by a filesystem error: %s (retrying next cycle)", root, exc)
        return
    log.info("  Scan of %s complete (%d album folders).", root, n)
