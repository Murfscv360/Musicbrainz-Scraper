#!/usr/bin/env python3
"""PicardWatch entry point.

  python run.py --once             scan every album folder once, then exit
  python run.py --watch            run the watcher daemon (event-driven + rescan)
  python run.py --folder "PATH"    judge/import a single folder, then exit
  add --dry-run                    judge + report only; never invoke Picard / move files

Start with:   python run.py --once --dry-run     (safe; touches nothing)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from picardwatch import importer, plex, report, status
from picardwatch.models import AlbumDecision
from picardwatch.config import load_config
from picardwatch.judge import Judge
from picardwatch.picard_runner import PicardRunner
from picardwatch.state import State, folder_signature
from picardwatch.watcher import watch

log = logging.getLogger("picardwatch")


def make_processor(cfg, state, judge, runner, dry_run, force=False):
    exts = cfg.judge.audio_extensions
    importer_cfg = getattr(cfg, "importer", None)
    dedupe = bool(getattr(importer_cfg, "dedupe", True))
    delete_source = bool(getattr(importer_cfg, "delete_source", True))
    seen = {"n": 0}

    def process(folder) -> None:
        folder = Path(folder)
        sig = folder_signature(folder, exts)
        if sig is None:
            log.debug("No audio in %s - skipping", folder.name)
            return

        prior = state.get(str(folder))
        if not force and prior and prior["signature"] == sig and prior["status"] in ("imported", "review", "duplicate"):
            log.debug("Unchanged since last decision (%s): %s", prior["status"], folder.name)
            return

        if dedupe:
            dup = state.find_processed_by_signature(sig, str(folder))
            if dup:
                orig_folder, orig_status = dup
                log.info("  DUPLICATE (identical to %s [%s]) - skipping",
                         Path(orig_folder).name, orig_status)
                state.record(str(folder), sig, "duplicate", AlbumDecision(
                    folder=str(folder),
                    reason=f"identical content to {Path(orig_folder).name} ({orig_status})"))
                if orig_status == "imported" and delete_source and not dry_run:
                    shutil.rmtree(folder, ignore_errors=True)  # redundant exact copy of a library album
                report.write(cfg.paths.report, state.list_reviews())
                return

        log.info("Judging: %s", folder.name)
        decision = judge.evaluate(folder)
        seen["n"] += 1
        if seen["n"] % 20 == 0:
            log.info("PROGRESS  %s", status.oneline(cfg, state))

        if not decision.perfect:
            log.info("  NOT perfect - %s  (leaving in place)", decision.reason or "no confident match")
            state.record(str(folder), sig, "review", decision)
            report.write(cfg.paths.report, state.list_reviews())
            return

        log.info("  PERFECT -> %s - %s  [%s]",
                 decision.artist, decision.release_title, decision.release_mbid)

        if dedupe and not dry_run and state.release_imported(decision.release_mbid, str(folder)):
            log.info("  DUPLICATE release (already in library) - leaving in place, flagged")
            decision.reason = "duplicate release (already imported)"
            state.record(str(folder), sig, "duplicate", decision)
            report.write(cfg.paths.report, state.list_reviews())
            return

        if dry_run:
            importer.import_album(decision, cfg, dry_run=True)
            state.record(str(folder), sig, "would_import", decision)
            return

        if importer.import_album(decision, cfg, dry_run=False):
            state.record(str(folder), sig, "imported", decision)
            log.info("  Imported OK.")
            if cfg.plex.enabled:
                plex.scan(cfg.plex)
        else:
            decision.reason = "native import incomplete (see log)"
            state.record(str(folder), sig, "failed", decision)
            report.write(cfg.paths.report, state.list_reviews())

    return process


def _single_instance_lock(cfg, name="picardwatch.lock", label="--once/--watch"):
    """Hold an OS file lock so two runs can't clobber each other or the SQLite state.
    The lock auto-releases when the process exits (even on a crash)."""
    lock_path = Path(cfg.paths.state_db).resolve().parent / name
    try:
        import msvcrt
    except ImportError:
        return None  # non-Windows: best-effort, skip
    handle = open(lock_path, "w")
    handle.write(str(os.getpid()))
    handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error("Another PicardWatch %s is already running (lock: %s). Exiting.", label, lock_path)
        sys.exit(1)
    return handle


def main() -> None:
    ap = argparse.ArgumentParser(description="MusicBrainz/Picard-driven Plex music importer")
    ap.add_argument("--config", default="config.yaml")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="scan all album folders once and exit")
    mode.add_argument("--watch", action="store_true", help="run the watcher daemon")
    mode.add_argument("--folder", help="process a single album folder and exit")
    mode.add_argument("--status", action="store_true", help="print a progress snapshot (moved/remaining/ETA) and exit")
    mode.add_argument("--retag", action="store_true", help="re-tag + re-organize the existing library to the current standard, then exit")
    mode.add_argument("--supervise", action="store_true", help="keep the watcher alive: (re)start run.py --watch whenever it stops or hangs")
    ap.add_argument("--dry-run", action="store_true", help="judge + report only; never move files")
    ap.add_argument("--limit", type=int, default=0, help="with --once, process at most N folders (0 = all)")
    ap.add_argument("--force", action="store_true", help="re-judge even if this exact folder was decided before")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_file = "supervisor.log" if args.supervise else "picardwatch.log"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(Path(cfg.paths.log_dir) / log_file, encoding="utf-8"),
        ],
    )
    for _noisy in ("musicbrainzngs", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)  # silence XML-parser / HTTP debug chatter
    if args.dry_run:
        log.info("DRY RUN - no files will be moved.")

    if args.supervise:
        from picardwatch import supervisor
        _slock = _single_instance_lock(cfg, "picardwatch-supervisor.lock", "--supervise")  # noqa: F841
        supervisor.supervise(cfg)
        return

    _lock = None
    if args.once or args.watch or args.retag:
        _lock = _single_instance_lock(cfg)  # keep handle alive for the process lifetime

    state = State(cfg.paths.state_db)
    if args.status:
        print(status.render(cfg, state))
        return
    if args.retag:
        from picardwatch import retag
        retag.retag_library(cfg, state, dry_run=args.dry_run)
        return
    judge = Judge(cfg, state)
    runner = PicardRunner(cfg)
    process = make_processor(cfg, state, judge, runner, args.dry_run, args.force)

    if args.folder:
        process(args.folder)
    elif args.once:
        root = Path(cfg.paths.input)
        if not root.exists():
            log.error("Input folder does not exist: %s  (set paths.input in config.yaml)", root)
            return
        done = 0
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                process(child)
            except Exception:
                log.exception("Error processing %s (skipping)", child.name)
            done += 1
            if args.limit and done >= args.limit:
                log.info("Reached --limit %d; stopping.", args.limit)
                break
    elif args.watch:
        watch(cfg, process)


if __name__ == "__main__":
    main()
