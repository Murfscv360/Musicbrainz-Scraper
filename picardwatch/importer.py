"""Native importer: fetch cover art, move each file into the Plex layout, write tags,
embed art, drop a cover.jpg in the album folder, and (optionally) clean up the source.

This replaces the Picard step: the judge already resolved the exact MusicBrainz release,
so we tag/organize directly — fully automated and unaffected by Picard's GUI/SSL issues.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from . import cleanup, coverart, organizer, tagger

log = logging.getLogger("picardwatch.importer")

_IMG_EXT = {".jpg", ".jpeg", ".png"}
_HEARTBEAT_SEC = 20   # during one large cross-drive copy, log progress at least this often so the
                      # supervisor (which watches log mtime) never mistakes a slow copy for a hang


def _move_file(src: Path, target: Path) -> None:
    """Move src -> target. Fast atomic rename when same-volume; otherwise stream-copy across the
    (slow) network drive in chunks, emitting a heartbeat so a slow-but-progressing copy isn't
    killed by the supervisor's hang detector, then remove the source."""
    try:
        os.replace(src, target)            # same-volume: atomic move (also overwrites target)
        return
    except OSError:
        pass                               # cross-device (e.g. D: -> Y:): chunked copy + heartbeat
    last = time.monotonic()
    with open(src, "rb") as fsrc, open(target, "wb") as fdst:
        while True:
            chunk = fsrc.read(4 * 1024 * 1024)
            if not chunk:
                break
            fdst.write(chunk)
            if time.monotonic() - last >= _HEARTBEAT_SEC:
                log.info("    ...copying %s (%d MB)", target.name, fdst.tell() // (1024 * 1024))
                last = time.monotonic()
    shutil.copystat(str(src), str(target))
    try:
        src.unlink()
    except OSError:
        pass


def import_album(decision, cfg, dry_run: bool = False) -> bool:
    album = decision.album_meta or {}
    plan = decision.file_plan or []
    library = Path(cfg.paths.library)
    if not plan:
        log.error("No file plan for %s (nothing to import)", decision.folder)
        return False

    dest = organizer.album_dir(library, album)
    if dry_run:
        log.info("  [dry-run] -> %s\\  (%d tracks%s)", dest, len(plan),
                 ", compilation" if album.get("is_compilation") else "")
        return True

    importer_cfg = getattr(cfg, "importer", None)
    embed = bool(getattr(importer_cfg, "embed_art", False))
    contact = getattr(cfg.musicbrainz, "contact", "")
    cover = coverart.fetch_front(album.get("mbid", ""), contact) or _source_image(Path(decision.folder))

    dest.mkdir(parents=True, exist_ok=True)
    log.info("  Importing %d tracks -> %s", len(plan), dest)  # marks import start for hang detection
    moved = 0
    for idx, item in enumerate(plan, 1):
        src = Path(item["path"])
        if not src.exists():
            log.warning("Planned file missing: %s", src)
            continue
        target = dest / organizer.track_relpath(item, album, src.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)  # 'Disc N' subfolder for multi-disc albums
        if target.exists():
            target.unlink()
        _move_file(src, target)
        moved += 1
        try:
            tagger.tag_file(str(target), item, album, cover if embed else None)
        except Exception as exc:  # never lose a moved file over a tag error
            log.warning("Tagging failed for %s: %s", target.name, exc)
        # Heartbeat after EVERY track (not every 25): on the slow Y: SMB drive even a small album
        # can exceed the supervisor's hang_timeout, and a per-25 heartbeat never fires for the
        # typical sub-25-track album -> the watcher got killed mid-import and re-imported forever.
        log.info("  ...moved %d/%d tracks", idx, len(plan))

    if cover:
        (dest / coverart.cover_filename(cover)).write_bytes(cover)

    _carry_extras(Path(decision.folder), dest,
                  getattr(importer_cfg, "extra_files", [".log", ".cue"]))

    complete = (moved == len(plan))
    if complete and getattr(importer_cfg, "delete_source", True):
        # delete the leftover source folder AND prune now-empty parents (e.g. an emptied
        # genre folder on the D: library), so no hollow skeleton is left behind.
        pruned = cleanup.cleanup_after_import(decision.folder, cfg)
        if pruned:
            log.info("  Pruned %d empty parent folder(s) on the source drive.", pruned)

    if not cover:
        art_note = "  (no cover art found)"
    elif embed:
        art_note = "  (cover.jpg + art embedded)"
    else:
        art_note = "  (cover.jpg saved)"
    log.info("  Imported %d/%d tracks -> %s%s", moved, len(plan), dest, art_note)
    return complete


def _source_image(folder: Path):
    """Fallback cover: the largest image already in the release folder."""
    imgs = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in _IMG_EXT]
    if not imgs:
        return None
    try:
        return max(imgs, key=lambda p: p.stat().st_size).read_bytes()
    except OSError:
        return None


def _carry_extras(src_folder: Path, dest: Path, exts) -> None:
    wanted = {e.lower() for e in (exts or [])}
    if not wanted:
        return
    for p in src_folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in wanted:
            try:
                shutil.move(str(p), str(dest / p.name))
            except (OSError, shutil.Error):
                pass
