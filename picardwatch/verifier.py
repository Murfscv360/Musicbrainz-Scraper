"""Confirm Picard actually moved the album out of the input folder.

The most reliable signal that a move succeeded is that the source folder no longer
contains audio files (Picard MOVES matched files; with "delete empty dirs" on it also
prunes the emptied folder). We treat leftover audio as a failure so the album is not
marked imported and can be retried / reviewed.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("picardwatch.verify")


def verify(source_folder: str | Path, audio_exts: list[str]) -> bool:
    source = Path(source_folder)
    exts = {e.lower() for e in audio_exts}
    if not source.exists():
        return True  # Picard pruned the emptied folder — clean success
    leftover = [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    if leftover:
        log.error("Move incomplete: %d audio file(s) still in %s", len(leftover), source)
        return False
    return True
