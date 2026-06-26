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

from picardwatch import cleanup, discovery, diskspace, importer, plex, report, status, winutil
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
                    cleanup.cleanup_after_import(str(folder), cfg)  # redundant exact copy -> remove + prune empty parents
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

        if not diskspace.ensure_space(cfg):
            return  # low disk space -> stop requested; this album is re-judged next run
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
    mode.add_argument("--prune-cache", action="store_true", help="cap the MB/AcoustID response cache (grows unbounded) and exit; add --vacuum to reclaim disk")
    mode.add_argument("--tidy-input", action="store_true", help="remove already-decided folders from the input(s): delete duplicates, archive review folders; batch via --limit. Run periodically.")
    mode.add_argument("--retag", action="store_true", help="re-tag + re-organize the existing library to the current standard, then exit")
    mode.add_argument("--enrich", action="store_true", help="write audiophile-enrichment sidecars (audiophile.json) for the library; add --analyze for ffmpeg loudness/DR/waveform")
    mode.add_argument("--start-enrich", action="store_true", help="clear stop/done flags and launch the enrichment worker (Tier-2) in the background; it auto-restarts via the keepalive task")
    mode.add_argument("--stop-enrich", action="store_true", help="ask the enrichment worker to finish its current album, stop, and not auto-restart")
    mode.add_argument("--catalogue", action="store_true", help="write a metadata catalogue (catalogue.json + README.md) of the library to --out")
    mode.add_argument("--supervise", action="store_true", help="keep the watcher alive: (re)start run.py --watch whenever it stops or hangs")
    mode.add_argument("--stop", action="store_true", help="ask a running supervisor/watcher to finish the current album and shut down cleanly")
    ap.add_argument("--dry-run", action="store_true", help="judge + report only; never move files")
    ap.add_argument("--limit", type=int, default=0, help="with --once/--enrich, process at most N folders (0 = all)")
    ap.add_argument("--out", default="", help="repo dir for --catalogue (default: catalogue.repo_dir in config)")
    ap.add_argument("--push", action="store_true", help="with --catalogue: git commit + push collection.json to its repo")
    ap.add_argument("--vacuum", action="store_true", help="with --prune-cache: VACUUM to reclaim freed space (needs the watcher stopped)")
    ap.add_argument("--delete-review", action="store_true", help="with --tidy-input: DELETE review folders instead of archiving them")
    ap.add_argument("--analyze", action="store_true", help="with --enrich: also run ffmpeg loudness/LRA/true-peak/waveform (slow; needs ffmpeg on PATH)")
    ap.add_argument("--force", action="store_true", help="re-judge even if this exact folder was decided before")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    winutil.suppress_child_windows()  # keep fpcalc/taskkill from flashing console windows
    cfg = load_config(args.config)
    if args.stop:
        from picardwatch import control
        control.request_stop(cfg)
        print("Stop requested. The supervisor + watcher will finish the current album and shut down.")
        return
    log_file = ("supervisor.log" if args.supervise else
                "enrich.log" if args.enrich else
                "catalogue.log" if args.catalogue else
                "tidy.log" if args.tidy_input else
                "picardwatch.log")
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

    if args.stop_enrich:
        from picardwatch import enrich
        enrich.request_enrich_stop(cfg)
        print("Enrichment stop requested - it will finish the current album, exit, and not auto-restart.")
        return

    if args.start_enrich:
        import subprocess
        from picardwatch import enrich
        enrich.clear_enrich_flags(cfg)
        proj = Path(__file__).resolve().parent
        pyw = proj / ".venv" / "Scripts" / "pythonw.exe"
        if not pyw.exists():
            pyw = proj / ".venv" / "Scripts" / "python.exe"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen([str(pyw), str(proj / "run.py"), "--enrich", "--analyze"],
                         cwd=str(proj), creationflags=flags)
        print("Enrichment started in the background (Tier-2). It auto-restarts if killed, and stops "
              "itself when the whole library is done. Stop early with:  run.py --stop-enrich")
        return

    if args.enrich:
        # Its own lock (separate from the watcher's) so two enrichers can't overlap and the
        # keepalive can tell it's running. Reads audio + writes sidecars only.
        from picardwatch import enrich
        _elock = _single_instance_lock(cfg, "picardwatch-enrich.lock", "--enrich")  # noqa: F841
        enrich.enrich_library(cfg, analyze=args.analyze, force=args.force, limit=args.limit)
        return

    if args.catalogue:
        # Build the Audio Vault collection.json (vault schema) from the library + audiophile
        # sidecars, write it to the repo root, and (with --push) git commit + push it.
        from picardwatch import catalogue
        cat = getattr(cfg, "catalogue", None)
        repo = args.out or str(getattr(cat, "repo_dir", "") or (Path(cfg.paths.log_dir).resolve().parent / "catalogue"))
        fname = str(getattr(cat, "file", "collection.json"))
        vname = str(getattr(cat, "name", "Audio Vault"))
        coll = catalogue.build_collection(cfg, name=vname, limit=args.limit)
        catalogue.write_collection(repo, coll, fname)
        st = coll["meta"]["stats"]
        print(f"collection.json: {st['artists']} artists, {st['albums']} albums, "
              f"{st['tracks']} tracks, {st['analyzed']} enriched -> {repo}\\{fname}")
        if args.push:
            print("Published." if catalogue.publish(repo, fname) else "Push FAILED (see logs/catalogue.log).")
        return

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
    if args.prune_cache:
        before = state.cache_count()
        n = state.prune_cache()
        print(f"Pruned {n} of {before} cache row(s); {state.cache_count()} kept.")
        if args.vacuum:
            print("VACUUM (reclaiming disk; needs exclusive access)...")
            print("Done." if state.vacuum() else "VACUUM skipped — DB locked (stop the watcher first).")
        return
    if args.tidy_input:
        from picardwatch import cleanup
        _tlock = _single_instance_lock(cfg, "picardwatch-tidy.lock", "--tidy-input")  # noqa: F841
        r = cleanup.tidy_input(cfg, state, limit=(args.limit or 300), delete_review=args.delete_review)
        print(f"tidy-input: deleted {r['deleted']}, archived {r['archived']}, "
              f"pruned {r['pruned']} empties, skipped {r['skipped']}")
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
        roots = [Path(r) for r in discovery.input_roots(cfg)]
        existing = [r for r in roots if r.exists()]
        for missing in (r for r in roots if not r.exists()):
            log.warning("Input folder does not exist (skipping): %s", missing)
        if not existing:
            log.error("No input folders exist (set paths.input / paths.extra_inputs in config.yaml)")
            return
        done = 0
        reached_limit = False
        for root in existing:
            if reached_limit:
                break
            log.info("Scanning %s ...", root)
            for album in discovery.find_albums(root, cfg.judge.audio_extensions):
                try:
                    process(album)
                except Exception:
                    log.exception("Error processing %s (skipping)", album)
                done += 1
                if args.limit and done >= args.limit:
                    log.info("Reached --limit %d; stopping.", args.limit)
                    reached_limit = True
                    break
        for root in existing:                         # tidy leftover empty husks once done
            try:
                cleanup.sweep_empty_dirs(root)
            except Exception:
                log.exception("Cleanup sweep failed for %s", root)
    elif args.watch:
        watch(cfg, process)


if __name__ == "__main__":
    main()
