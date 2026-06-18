"""One-off: bring albums already in the library up to the current standard — rich
metadata + `Artist/Album (Year) (Type)/` layout + multi-disc `Disc N/` subfolders.

For each album folder it reads the MusicBrainz album id from the files' existing tags,
re-fetches the (richly-included) release, remaps each file to its track, then rewrites
tags and moves the file into the correct location. Albums whose id can't be read or
mapped are left untouched.

Run with the watcher STOPPED (so MusicBrainz isn't queried at 2x the rate limit).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import mutagen

from . import coverart, organizer, tagger
from .musicbrainz import MBClient, album_metadata, release_tracks

log = logging.getLogger("picardwatch.retag")

_IMG = {".jpg", ".jpeg", ".png"}


def retag_library(cfg, state, dry_run: bool = False) -> None:
    mb = MBClient(cfg, state)
    library = Path(cfg.paths.library)
    exts = {e.lower() for e in cfg.judge.audio_extensions}
    embed = bool(getattr(getattr(cfg, "importer", None), "embed_art", False))
    if not library.exists():
        log.error("Library folder does not exist: %s", library)
        return

    updated = skipped = 0
    for artist_dir in sorted(p for p in library.iterdir() if p.is_dir()):
        for album_dir in sorted(p for p in artist_dir.iterdir() if p.is_dir()):
            files = sorted(p for p in album_dir.rglob("*")
                           if p.is_file() and p.suffix.lower() in exts)
            if not files:
                continue
            if _retag_album(mb, cfg, library, album_dir, files, embed, dry_run):
                updated += 1
            else:
                skipped += 1
    verb = "would update" if dry_run else "updated"
    log.info("Re-tag complete: %d %s, %d skipped", updated, verb, skipped)


def _retag_album(mb, cfg, library, album_dir, files, embed, dry_run) -> bool:
    mbid = _tag(files[0], "musicbrainz_albumid")
    if not mbid:
        log.info("  skip (no MB album id): %s", album_dir.name)
        return False
    rel = mb.get_release(mbid)
    if not rel:
        log.info("  skip (release lookup failed): %s", album_dir.name)
        return False

    album = album_metadata(rel)
    tracks = release_tracks(rel)
    by_rec = {t.recording_id: t for t in tracks if t.recording_id}
    by_pos = {(t.disc, t.position): t for t in tracks}

    plan = []
    for f in files:
        track = by_rec.get(_tag(f, "musicbrainz_trackid"))
        if track is None:
            try:
                disc = int((_tag(f, "discnumber") or "1").split("/")[0])
                pos = int((_tag(f, "tracknumber") or "0").split("/")[0])
            except ValueError:
                disc, pos = 1, 0
            track = by_pos.get((disc, pos))
        if track is None:
            log.info("  skip album (can't map %s): %s", f.name, album_dir.name)
            return False
        plan.append((f, track))

    new_dir = organizer.album_dir(library, album)
    if dry_run:
        change = "" if new_dir.resolve() == album_dir.resolve() else f" -> {new_dir.relative_to(library)}"
        log.info("  %s%s  (%d tracks, +rich tags)", album_dir.relative_to(library), change, len(plan))
        return True

    cover = _existing_cover(album_dir) or coverart.fetch_front(
        album.get("mbid", ""), getattr(cfg.musicbrainz, "contact", ""))
    new_dir.mkdir(parents=True, exist_ok=True)
    for f, track in plan:
        item = {
            "title": track.title,
            "artist": track.artist or album["albumartist"],
            "artist_sort": track.artist_sort or album["albumartist_sort"],
            "artist_mbid": track.artist_mbid or album["albumartist_mbid"],
            "isrc": track.isrc,
            "position": track.position,
            "disc": track.disc,
            "recording_id": track.recording_id,
        }
        target = new_dir / organizer.track_relpath(item, album, f.suffix)
        if target.resolve() != f.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
        try:
            tagger.tag_file(str(target), item, album, cover if embed else None)
        except Exception as exc:
            log.warning("  tag failed %s: %s", target.name, exc)

    if cover:
        (new_dir / coverart.cover_filename(cover)).write_bytes(cover)
    if new_dir.resolve() != album_dir.resolve():
        _cleanup(album_dir)
    log.info("  retagged: %s", new_dir.relative_to(library))
    return True


def _tag(path: Path, key: str) -> str:
    try:
        mf = mutagen.File(str(path), easy=True)
    except Exception:
        return ""
    if mf is None:
        return ""
    val = mf.get(key)
    if not val:
        return ""
    return val[0] if isinstance(val, list) else str(val)


def _existing_cover(album_dir: Path):
    imgs = [p for p in album_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMG]
    if not imgs:
        return None
    try:
        return max(imgs, key=lambda p: p.stat().st_size).read_bytes()
    except OSError:
        return None


def _cleanup(album_dir: Path) -> None:
    """Remove the now-emptied old album folder (and its artist folder if empty)."""
    try:
        shutil.rmtree(album_dir, ignore_errors=True)
        parent = album_dir.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
