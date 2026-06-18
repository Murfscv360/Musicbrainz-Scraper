"""The "perfect album" judge — the heart of PicardWatch.

For one album folder it decides whether the files are a COMPLETE, high-confidence
match to a single MusicBrainz release. Only "perfect" albums are handed to Picard;
everything else is left untouched and logged to the review report.

An album is PERFECT when ALL hold:
  * every release track is matched by a file        (coverage == 1.0, no missing tracks)
  * every audio file matched a track on the release (orphans == 0, no stray files)
  * file_count == release track_count               (right edition)
  * min per-track confidence >= acoustid.min_score  (or a strong metadata fallback)
  * exactly one release was chosen

Matching is primarily by *recording MBID*: AcoustID gives each file a set of
recording MBIDs; the MB release lists a recording MBID per track; the intersection
is a precise, fingerprint-grounded match. Files AcoustID can't identify fall back to
duration + fuzzy-title matching (unless judge.require_full_acoustid is true).
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import acoustid
import mutagen

from .models import AlbumDecision, FileAnalysis, Track
from .musicbrainz import MBClient, album_metadata, release_tracks
from .state import State

log = logging.getLogger("picardwatch.judge")


@dataclass
class _Scored:
    mbid: str
    rel: dict
    tracks: list
    coverage: float
    orphans: int
    matched_files: int
    track_count: int
    min_conf: float
    missing: list
    assignment: dict  # file index -> Track (which release track each file is)

    @property
    def rank(self):
        # Prefer full coverage, then file-count agreement, then fewest orphans.
        return (round(self.coverage, 3), self.track_count == self.matched_files + self.orphans,
                -self.orphans, self.matched_files)


class Judge:
    def __init__(self, cfg, state: State):
        self.cfg = cfg
        self.state = state
        self.mb = MBClient(cfg, state)
        self.exts = {e.lower() for e in cfg.judge.audio_extensions}
        self.min_score = float(cfg.acoustid.min_score)
        self.api_key = cfg.acoustid.api_key

    # ------------------------------------------------------------------ public
    def evaluate(self, folder: str | Path) -> AlbumDecision:
        folder = Path(folder)
        files = self._audio_files(folder)
        if not files:
            return AlbumDecision(folder=str(folder), reason="no audio files")

        analyses = self._analyze_all(files)
        n = len(files)

        # Candidate releases from fingerprint votes (most-supported release first).
        votes: Counter = Counter()
        for a in analyses:
            for rid in a.release_ids:
                votes[rid] += 1
        fp_candidates = [rid for rid, _ in votes.most_common(self.cfg.judge.max_candidates)]

        best = self._best_candidate(fp_candidates, analyses, n, None)

        # Only pay for a tag-based MB search if fingerprints didn't already nail it.
        if best is None or not self._is_perfect(best, n):
            extra = [c for c in self._tag_search_candidates(analyses, n) if c not in fp_candidates]
            best = self._best_candidate(extra, analyses, n, best)

        if best is None:
            return AlbumDecision(
                folder=str(folder), file_count=n,
                reason="no MusicBrainz release candidates from fingerprints or tags",
            )
        return self._finalize(folder, best, analyses)

    # --------------------------------------------------------------- internals
    def _audio_files(self, folder: Path) -> list[Path]:
        return sorted(p for p in folder.rglob("*")
                      if p.is_file() and p.suffix.lower() in self.exts)

    def _analyze_all(self, files: list[Path]) -> list[FileAnalysis]:
        """Fingerprint + AcoustID every file. Cache hits are instant; misses are
        computed concurrently (fpcalc + AcoustID), with all SQLite writes kept on the
        main thread (the connection is not thread-safe)."""
        preps = [self._prep(f) for f in files]
        analyses: list = [None] * len(files)
        misses = []
        for i, (f, (tags, tagdur, ck)) in enumerate(zip(files, preps)):
            cached = self.state.cache_get(ck)
            if cached is not None:
                analyses[i] = self._build(f, tags, cached, tagdur)
            else:
                misses.append((i, f, tags, tagdur, ck))

        if misses:
            workers = max(1, min(int(getattr(self.cfg.judge, "fingerprint_workers", 4)), len(misses)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self._compute, f, tagdur): (i, f, tags, ck)
                        for (i, f, tags, tagdur, ck) in misses}
                for fut in as_completed(futs):
                    i, f, tags, ck = futs[fut]
                    data = fut.result()
                    self.state.cache_set(ck, data)        # main thread only
                    analyses[i] = self._build(f, tags, data)
        return analyses

    def _prep(self, path: Path):
        tags = self._read_tags(path)
        tag_duration = tags.pop("_duration", 0.0)  # internal key; not a FileAnalysis field
        return tags, tag_duration, f"acoustid:{self._file_sig(path)}"

    def _build(self, path, tags, data, tag_duration: float = 0.0) -> FileAnalysis:
        fa = FileAnalysis(path=str(path), duration=data.get("duration") or tag_duration, **tags)
        fa.rec_scores = {k: float(v) for k, v in data["rec_scores"].items()}
        fa.release_ids = data["release_ids"]
        fa.release_meta = data["release_meta"]
        return fa

    def _compute(self, path: Path, tag_duration: float) -> dict:
        """fpcalc + AcoustID lookup. No DB access, so it is safe to run in a thread."""
        duration = tag_duration
        rec_scores: dict = {}
        release_ids: list = []
        release_meta: dict = {}
        try:
            dur, fp = acoustid.fingerprint_file(str(path))
            duration = float(dur)
            resp = acoustid.lookup(self.api_key, fp, dur, meta=["recordings", "releases"])
            rec_scores, release_ids, release_meta = self._parse_acoustid(resp)
        except acoustid.NoBackendError:
            log.error("fpcalc not found on PATH — install Chromaprint (see requirements.txt)")
        except (acoustid.FingerprintGenerationError, acoustid.WebServiceError) as exc:
            log.warning("AcoustID failed for %s: %s", path.name, exc)
        return {"duration": duration, "rec_scores": rec_scores,
                "release_ids": release_ids, "release_meta": release_meta}

    @staticmethod
    def _is_perfect(s: "_Scored", n_files: int) -> bool:
        return s.coverage >= 1.0 and s.orphans == 0 and s.track_count == n_files

    def _best_candidate(self, candidate_ids, analyses, n, best):
        """Score candidate releases in order, stopping at the first perfect match."""
        for rid in candidate_ids:
            if not rid:
                continue
            scored = self._score_release(rid, analyses)
            if scored and (best is None or scored.rank > best.rank):
                best = scored
            if best is not None and self._is_perfect(best, n):
                break
        return best

    @staticmethod
    def _parse_acoustid(resp: dict):
        rec_scores: dict = {}
        release_ids: list = []
        release_meta: dict = {}
        for result in resp.get("results", []):
            score = float(result.get("score", 0.0))
            for rec in result.get("recordings", []):
                rid = rec.get("id")
                if not rid:
                    continue
                rec_scores[rid] = max(rec_scores.get(rid, 0.0), score)
                for rel in rec.get("releases", []):
                    relid = rel.get("id")
                    if not relid:
                        continue
                    if relid not in release_meta:
                        release_ids.append(relid)
                        release_meta[relid] = {
                            "title": rel.get("title"),
                            "track_count": rel.get("track_count"),
                        }
        return rec_scores, release_ids, release_meta

    def _read_tags(self, path: Path) -> dict:
        out = {"title": "", "artist": "", "album": "", "albumartist": "",
               "tracknumber": "", "_duration": 0.0}
        try:
            mf = mutagen.File(str(path), easy=True)
            if mf is not None:
                getf = lambda k: (mf.get(k) or [""])[0]
                out.update(
                    title=getf("title"), artist=getf("artist"),
                    album=getf("album"), albumartist=getf("albumartist"),
                    tracknumber=getf("tracknumber"),
                )
                if mf.info and getattr(mf.info, "length", None):
                    out["_duration"] = float(mf.info.length)
        except Exception as exc:  # mutagen raises many subclasses; tags are best-effort
            log.debug("tag read failed for %s: %s", path.name, exc)
        return out

    def _tag_search_candidates(self, analyses: list[FileAnalysis], n_files: int) -> list[str]:
        album = _majority(a.album for a in analyses)
        artist = _majority(a.albumartist for a in analyses) or _majority(a.artist for a in analyses)
        if not (album and artist):
            return []
        return self.mb.search_releases(artist, album, tracks=n_files, limit=5)

    def _score_release(self, mbid: str, analyses: list[FileAnalysis]) -> Optional[_Scored]:
        rel = self.mb.get_release(mbid)
        if not rel:
            return None
        tracks = release_tracks(rel)
        if not tracks:
            return None

        rec_to_track = {t.recording_id: t for t in tracks if t.recording_id}
        assignment: dict = {}      # file index -> Track
        confidences: list = []

        # Pass 1 — precise match by recording MBID + AcoustID score.
        for i, a in enumerate(analyses):
            best_conf = 0.0
            hit: Optional[Track] = None
            for rid, score in a.rec_scores.items():
                if score >= self.min_score and rid in rec_to_track and score > best_conf:
                    best_conf, hit = score, rec_to_track[rid]
            if hit is not None:
                assignment[i] = hit
                confidences.append(best_conf)

        # Pass 2 — metadata fallback for files AcoustID couldn't place.
        if not self.cfg.judge.require_full_acoustid:
            used = {(t.disc, t.position) for t in assignment.values()}
            remaining = [t for t in tracks if (t.disc, t.position) not in used]
            for i, a in enumerate(analyses):
                if i in assignment:
                    continue
                hit = self._metadata_match(a, remaining)
                if hit is not None:
                    assignment[i] = hit
                    confidences.append(self.min_score)  # borderline-pass
                    remaining.remove(hit)

        matched_positions = {(t.disc, t.position) for t in assignment.values()}
        track_count = len(tracks)
        coverage = len(matched_positions) / track_count if track_count else 0.0
        orphans = len(analyses) - len(assignment)
        missing = [f"{t.disc}-{t.position} {t.title}" for t in tracks
                   if (t.disc, t.position) not in matched_positions]
        min_conf = min(confidences) if confidences else 0.0
        return _Scored(mbid, rel, tracks, coverage, orphans, len(assignment),
                       track_count, min_conf, missing, assignment)

    def _metadata_match(self, a: FileAnalysis, tracks: list[Track]) -> Optional[Track]:
        tol = float(self.cfg.judge.duration_tolerance_sec)
        need = float(self.cfg.judge.title_similarity)
        best: Optional[Track] = None
        best_sim = 0.0
        for t in tracks:
            if t.length_ms and a.duration:
                if abs(a.duration - t.length_ms / 1000.0) > tol:
                    continue
            sim = SequenceMatcher(None, a.title.lower(), (t.title or "").lower()).ratio()
            if sim >= need and sim > best_sim:
                best, best_sim = t, sim
        return best

    def _finalize(self, folder: Path, s: _Scored, analyses: list[FileAnalysis]) -> AlbumDecision:
        n_files = len(analyses)
        perfect = (s.coverage >= 1.0 and s.orphans == 0 and s.track_count == n_files)
        reasons = []
        if s.coverage < 1.0:
            reasons.append(f"missing {len(s.missing)} track(s)")
        if s.orphans:
            reasons.append(f"{s.orphans} unmatched file(s)")
        if s.track_count != n_files:
            reasons.append(f"file_count {n_files} != release tracks {s.track_count}")

        rel = s.rel
        album_meta = album_metadata(rel)   # single source of album-level tags (shared with --retag)
        albumartist = album_meta["albumartist"]
        aa_sort = album_meta["albumartist_sort"]
        aa_mbid = album_meta["albumartist_mbid"]
        file_plan = []
        for i, track in sorted(s.assignment.items(), key=lambda kv: (kv[1].disc, kv[1].position)):
            file_plan.append({
                "path": analyses[i].path,
                "title": track.title,
                "artist": track.artist or albumartist,
                "artist_sort": track.artist_sort or aa_sort,
                "artist_mbid": track.artist_mbid or aa_mbid,
                "isrc": track.isrc,
                "position": track.position,
                "disc": track.disc,
                "recording_id": track.recording_id,
            })

        return AlbumDecision(
            folder=str(folder), perfect=perfect,
            release_mbid=s.mbid, release_title=rel.get("title", ""),
            artist=albumartist, year=album_meta["year"],
            score=round(s.min_conf, 3), coverage=round(s.coverage, 3),
            orphans=s.orphans, file_count=n_files, track_count=s.track_count,
            reason="; ".join(reasons), missing=s.missing,
            file_plan=file_plan, album_meta=album_meta,
        )

    @staticmethod
    def _file_sig(path: Path) -> str:
        st = path.stat()
        return hashlib.sha1(f"{path}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()


def _majority(values) -> str:
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else ""
