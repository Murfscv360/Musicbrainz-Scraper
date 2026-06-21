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
            entries = list(folder.iterdir())
        except OSError:
            continue
        if any(e.is_file() and e.suffix.lower() in exts for e in entries):
            yield folder                       # directly contains audio -> album
            continue
        subdirs = [e for e in entries if e.is_dir()]
        if (subdirs and all(_DISC_RE.match(d.name) for d in subdirs)
                and any(_dir_has_audio(d, exts) for d in subdirs)):
            yield folder                       # multi-disc album (CD1/CD2/...)
            continue
        if depth < _MAX_DEPTH:
            stack.extend((d, depth + 1) for d in subdirs)


def _dir_has_audio(folder: Path, exts) -> bool:
    try:
        return any(p.is_file() and p.suffix.lower() in exts for p in folder.iterdir())
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
