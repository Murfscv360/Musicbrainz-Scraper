"""Find album folders under an input root.

Handles both layouts PicardWatch sees:
  * flat release folders   -> Input/<Release>/*.flac
  * nested libraries       -> Input/<Genre>/<Artist>/<Album>/*.flac
  * multi-disc albums       -> Album/CD1/*.flac, Album/CD2/*.flac

An "album" is a folder that directly contains audio, OR whose subfolders are all
disc folders (CD1/Disc 2/...) that hold audio. Intermediate folders (genre/artist)
are walked through; album folders are not descended into.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_DISC_RE = re.compile(r"^(cd|disc|disk)\s*\d+", re.IGNORECASE)
_MAX_DEPTH = 8


def find_albums(root, audio_exts):
    exts = {e.lower() for e in audio_exts}
    root = Path(root)
    if not root.exists():
        return
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as sd:
                entries = list(sd)
        except OSError:
            continue
        # DirEntry.is_file()/is_dir() use cached type info from the directory enumeration
        # on Windows (FindNextFile), avoiding a separate stat round-trip per entry over SMB.
        if any(e.is_file() and os.path.splitext(e.name)[1].lower() in exts for e in entries):
            yield folder                       # directly contains audio -> album
            continue
        subdirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
        if (subdirs and all(_DISC_RE.match(d.name) for d in subdirs)
                and any(_dir_has_audio(d.path, exts) for d in subdirs)):
            yield folder                       # multi-disc album (CD1/CD2/...)
            continue
        if depth < _MAX_DEPTH:
            stack.extend((Path(d.path), depth + 1) for d in subdirs)


def _dir_has_audio(path, exts) -> bool:
    try:
        with os.scandir(path) as sd:
            return any(e.is_file() and os.path.splitext(e.name)[1].lower() in exts for e in sd)
    except OSError:
        return False


def input_roots(cfg) -> list:
    """Primary input plus any extra inputs, in order.

    A `PICARDWATCH_ROOTS` env var (semicolon-separated paths) overrides config — used to
    temporarily focus the watcher on a single root (e.g. a one-off D: blitz) without
    editing config.yaml. Clearing the var on the next restart restores normal watching.
    """
    override = os.environ.get("PICARDWATCH_ROOTS")
    if override:
        roots = [r.strip() for r in override.split(";") if r.strip()]
        if roots:
            return roots
    roots = [cfg.paths.input]
    roots += list(getattr(cfg.paths, "extra_inputs", []) or [])
    return roots
