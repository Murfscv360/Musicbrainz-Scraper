"""Plain data structures shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Track:
    """One track on a candidate MusicBrainz release."""
    position: int
    disc: int
    recording_id: str
    title: str
    length_ms: Optional[int]  # may be None if MB has no duration
    artist: str = ""          # track artist (matters for Various-Artists compilations)


@dataclass
class FileAnalysis:
    """What we learned about a single audio file on disk."""
    path: str
    duration: float                       # seconds (fingerprint or tag-derived)
    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    tracknumber: str = ""
    rec_scores: dict = field(default_factory=dict)   # {recording_mbid: best_acoustid_score}
    release_ids: list = field(default_factory=list)  # release MBIDs AcoustID associated with this file
    release_meta: dict = field(default_factory=dict) # {release_mbid: {"title":..., "track_count":...}}


@dataclass
class AlbumDecision:
    """The verdict for one album folder. `perfect` gates the move."""
    folder: str
    perfect: bool = False
    release_mbid: str = ""
    release_title: str = ""
    artist: str = ""
    year: str = ""
    score: float = 0.0          # min per-track confidence on the chosen release
    coverage: float = 0.0       # fraction of release tracks matched by a file
    orphans: int = 0            # audio files that matched no track on the chosen release
    file_count: int = 0
    track_count: int = 0
    reason: str = ""            # human-readable "why not perfect"
    missing: list = field(default_factory=list)  # release track positions with no file
    file_plan: list = field(default_factory=list)  # perfect album: [{path,title,artist,position,disc,recording_id}]
    album_meta: dict = field(default_factory=dict)  # album-level tags (album, albumartist, year, mbid, ...)
