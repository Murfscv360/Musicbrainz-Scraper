"""Compute Plex-compatible destination paths and sanitize names for Windows/NTFS.

Layout:  <library>/<Album Artist>/<Album> (Year)/[disc-]NN - [Artist - ]Title.ext
Various-Artists comps go under "Various Artists" with the track artist in the filename.
"""

from __future__ import annotations

import re
from pathlib import Path

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    """Make a string safe as a single Windows path component."""
    cleaned = _ILLEGAL.sub("_", name or "").strip()
    cleaned = cleaned.rstrip(". ")  # Windows forbids trailing dot/space
    return cleaned or "Unknown"


def album_dir(library: Path, album: dict) -> Path:
    artist = sanitize(album.get("albumartist") or "Unknown Artist")
    name = sanitize(album.get("album") or "Unknown Album")
    year = (album.get("year") or "").strip()
    if year:
        name += f" ({year})"
    rtype = (album.get("type") or "").strip()      # Album / EP / Single / Compilation / ...
    if rtype:
        name += f" ({rtype})"
    return Path(library) / artist / sanitize(name)


def track_filename(track: dict, album: dict, ext: str) -> str:
    num = f"{int(track['position']):02d}"
    artist_part = f"{sanitize(track['artist'])} - " if album.get("is_compilation") else ""
    return f"{num} - {artist_part}{sanitize(track['title'])}{ext.lower()}"


def track_relpath(track: dict, album: dict, ext: str) -> str:
    """Path of a track within its album folder. Multi-disc releases get a 'Disc N'
    subfolder; single-disc releases keep everything directly in the album folder."""
    fname = track_filename(track, album, ext)
    if int(album.get("total_discs") or 1) > 1:
        return f"Disc {int(track['disc'])}/{fname}"
    return fname
