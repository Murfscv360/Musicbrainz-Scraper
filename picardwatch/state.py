"""SQLite-backed state: per-album decisions (idempotency + re-evaluation) and a
response cache for MusicBrainz / AcoustID lookups.

Why the content signature matters: imperfect albums are LEFT in the input folder
(per your design choice), so without state we'd re-judge them on every scan. The
signature is a hash of (filename, size) pairs — if you later add the missing track,
the signature changes and the album is automatically re-evaluated.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .models import AlbumDecision


class State:
    def __init__(self, db_path: str | Path):
        self.db = sqlite3.connect(str(db_path), timeout=30)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")   # readers (e.g. --status) don't block on writes
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            pass
        self._init()

    def _init(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS albums (
                folder       TEXT PRIMARY KEY,
                signature    TEXT,
                status       TEXT,          -- imported | review | failed | would_import
                release_mbid TEXT,
                artist       TEXT,
                title        TEXT,
                score        REAL,
                coverage     REAL,
                orphans      INTEGER,
                file_count   INTEGER,
                track_count  INTEGER,
                reason       TEXT,
                decided_at   REAL
            );
            CREATE TABLE IF NOT EXISTS cache (
                k  TEXT PRIMARY KEY,
                v  TEXT,
                ts REAL
            );
            """
        )
        self.db.commit()

    # --- album decisions -----------------------------------------------------
    def get(self, folder: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM albums WHERE folder=?", (folder,)).fetchone()
        return dict(row) if row else None

    def record(self, folder: str, signature: str, status: str, d: AlbumDecision) -> None:
        self.db.execute(
            """
            INSERT INTO albums (folder, signature, status, release_mbid, artist, title,
                                score, coverage, orphans, file_count, track_count, reason, decided_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(folder) DO UPDATE SET
                signature=excluded.signature, status=excluded.status,
                release_mbid=excluded.release_mbid, artist=excluded.artist, title=excluded.title,
                score=excluded.score, coverage=excluded.coverage, orphans=excluded.orphans,
                file_count=excluded.file_count, track_count=excluded.track_count,
                reason=excluded.reason, decided_at=excluded.decided_at
            """,
            (folder, signature, status, d.release_mbid, d.artist, d.release_title,
             d.score, d.coverage, d.orphans, d.file_count, d.track_count, d.reason, time.time()),
        )
        self.db.commit()

    def list_reviews(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM albums WHERE status IN ('review','failed','duplicate') ORDER BY decided_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def find_processed_by_signature(self, signature: str, exclude_folder: str):
        """Return (folder, status) of another folder with identical content already
        handled, or None. Used to skip byte-identical duplicate downloads."""
        row = self.db.execute(
            "SELECT folder, status FROM albums WHERE signature=? AND folder<>? "
            "AND status IN ('imported','review','duplicate') ORDER BY decided_at LIMIT 1",
            (signature, exclude_folder),
        ).fetchone()
        return (row["folder"], row["status"]) if row else None

    def release_imported(self, mbid: str, exclude_folder: str) -> bool:
        """Has this MusicBrainz release already been imported from a different folder?"""
        if not mbid:
            return False
        row = self.db.execute(
            "SELECT 1 FROM albums WHERE release_mbid=? AND folder<>? AND status='imported' LIMIT 1",
            (mbid, exclude_folder),
        ).fetchone()
        return row is not None

    # --- progress / status --------------------------------------------------
    def status_counts(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) AS c FROM albums GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def imported_track_total(self) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(file_count), 0) AS t FROM albums WHERE status='imported'"
        ).fetchone()
        return int(row["t"] or 0)

    def recent_rate_per_min(self, minutes: int = 15) -> float:
        """Albums decided per minute over the last `minutes` (current pace, for ETA)."""
        since = time.time() - minutes * 60
        row = self.db.execute("SELECT COUNT(*) AS c FROM albums WHERE decided_at >= ?", (since,)).fetchone()
        return (row["c"] or 0) / float(minutes)

    # --- generic response cache ---------------------------------------------
    def cache_get(self, key: str):
        row = self.db.execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else None

    def cache_set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO cache (k, v, ts) VALUES (?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
            (key, json.dumps(value), time.time()),
        )
        self.db.commit()

    def cache_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0])

    def prune_cache(self, max_rows: int = 50000, keep_days: int = 0) -> int:
        """Cap the MB/AcoustID response cache so it can't grow unbounded (it had reached ~1 GB).
        Keeps the most-recently-written `max_rows` rows (and, if keep_days>0, drops anything older).
        Pruned entries are only lookup accelerators — re-fetched on demand. Returns rows deleted."""
        deleted = 0
        if keep_days > 0:
            cutoff = time.time() - keep_days * 86400
            deleted += self.db.execute("DELETE FROM cache WHERE ts < ?", (cutoff,)).rowcount
        deleted += self.db.execute(
            "DELETE FROM cache WHERE k IN (SELECT k FROM cache ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (max_rows,),
        ).rowcount
        self.db.commit()
        return deleted

    def vacuum(self) -> bool:
        """Reclaim freed space on disk after a prune. Needs EXCLUSIVE access — returns False if the
        DB is locked (i.e. the watcher is running; stop it first)."""
        try:
            self.db.execute("VACUUM")
            return True
        except sqlite3.OperationalError:
            return False

    def mark_tidied(self, folder: str) -> None:
        """Mark an already-decided source as removed from the input: status 'tidied' excludes it
        from future tidy passes and the review report. Import/dedup history stays (by signature)."""
        self.db.execute("UPDATE albums SET status='tidied' WHERE folder=?", (folder,))
        self.db.commit()


def folder_signature(folder: str | Path, audio_exts: list[str]) -> Optional[str]:
    """Return a stable hash of the folder's file (name, size) pairs, or None if the
    folder contains no audio files."""
    folder = Path(folder)
    exts = {e.lower() for e in audio_exts}
    parts: list[str] = []
    has_audio = False
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        parts.append(f"{p.relative_to(folder).as_posix()}:{size}")
        if p.suffix.lower() in exts:
            has_audio = True
    if not has_audio:
        return None
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
