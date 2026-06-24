"""Emit the Audio Vault `collection.json` (vault schema) from the PicardWatch library.

A Python port of the Audio Vault scanner (its `scanner/scan.js` + `lib.js`): walk the
library, read each album's audio-stream header + tags via Mutagen, keep only lossless
material, and tier by resolution. Measured loudness / LRA / true-peak / DR and the
per-track waveform are MERGED from PicardWatch's `audiophile.json` sidecars (its resilient
enrichment) — so nothing is re-decoded here. Curated `scanner/annotations.json` overlays
are merged on top. Output is written to the repo root and (with publish) committed + pushed.

**Lean read:** album audio quality is homogeneous across an album's tracks, so we read only
ONE representative file per album (not every track) — cutting reads ~12× over a slow network
drive. Per-track length is the only field this drops (shown as "—"); everything audiophile-
critical (bit depth / sample rate / format / loudness / DR / waveform) stays exact. A
path+mtime+size cache makes re-runs instant.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import enrich

log = logging.getLogger("picardwatch.catalogue")

# ── lib.js parity ───────────────────────────────────────────────────────────────
AUDIO_EXT = {
    "flac": "FLAC", "alac": "ALAC", "m4a": "ALAC", "wav": "WAV", "aif": "AIFF",
    "aiff": "AIFF", "dsf": "DSD", "dff": "DSD", "mp3": "MP3", "aac": "AAC",
    "ogg": "OGG", "oga": "OGG", "opus": "OPUS", "wv": "WavPack", "ape": "APE",
}
LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "DSD", "WavPack", "APE"}
PALETTE = ["#5fb6d6", "#c98a3a", "#8a6fd1", "#6fd1a0", "#d16f8a", "#6f9cd1", "#d1a36f"]


def _slug(s: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in str(s).lower())
    return "-".join(filter(None, out.split("-")))


def _decade(y) -> str:
    return f"{(int(y) // 10) * 10}s"


def _clock(sec) -> str:
    if not sec:
        return "—"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def _shade(hexc: str, pct: int) -> str:
    h = str(hexc).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    t = 0 if pct < 0 else 255
    p = abs(pct) / 100
    return "#" + "".join(f"{round((t - v) * p + v):02x}" for v in (r, g, b))


def _resolution(fmt: str, bit_depth: int, sample_rate: int, bitrate: int) -> str:
    if bit_depth and sample_rate:
        khz = f"{sample_rate / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{bit_depth}-bit / {khz} kHz" + (f" · {fmt}" if fmt else "")
    if bitrate:
        return (f"{fmt} · " if fmt else "") + f"{bitrate} kbps"
    return ("Lossless · " if fmt in LOSSLESS else "Lossy · ") + (fmt or "")


def _fmt_of(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return AUDIO_EXT.get(ext, ext.upper())


def _first(v):
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


# ── one representative file read (Mutagen: header + tags) ──────────────────────
def _parse_file(path: Path, cache: dict):
    """Cache entry {'m','s','d'} for one file via a single Mutagen open (or None).
    A path+mtime+size cache hit skips the network read entirely."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = cache.get(key)
    if hit and hit.get("m") == st.st_mtime and hit.get("s") == st.st_size:
        return hit
    try:
        mf = mutagen.File(str(path))
    except Exception:
        mf = None
    if mf is None:
        return None
    info = getattr(mf, "info", None)
    if isinstance(mf, (FLAC, OggVorbis, OggOpus)):
        tg = {str(k).lower(): _first(v) for k, v in (getattr(mf, "tags", None) or {}).items()}
    else:
        tg = enrich._tagmap(path)
    length = float(getattr(info, "length", 0) or 0)
    bitrate = int(getattr(info, "bitrate", 0) or 0)
    if not bitrate and length:
        bitrate = int(st.st_size * 8 / length)
    year = str(tg.get("date") or tg.get("year") or "")[:4]
    d = {
        "albumartist": tg.get("albumartist"), "artist": tg.get("artist"), "album": tg.get("album"),
        "year": int(year) if year.isdigit() else None, "genre": tg.get("genre"),
        "duration": length,
        "bitDepth": int(getattr(info, "bits_per_sample", 0) or 0),
        "sampleRate": int(getattr(info, "sample_rate", 0) or 0),
        "bitrate": round(bitrate / 1000) if bitrate else 0,
        "channels": int(getattr(info, "channels", 0) or 0),
        "size": st.st_size,
        **enrich.tag_fields_map(tg),   # bpm/key/label/catalog/isrc/mb*/RG/DR/totalTracks/compilation
    }
    return {"m": st.st_mtime, "s": st.st_size, "d": d}


def _cache_path(cfg) -> Path:
    return Path(cfg.paths.state_db).resolve().parent / ".catalogue-cache.json"


def _load_cache(cfg) -> dict:
    try:
        return json.loads(_cache_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cfg, c: dict) -> None:
    try:
        _cache_path(cfg).write_text(json.dumps(c), encoding="utf-8")
    except Exception:
        pass


# ── stall resilience: time out a flaky-network op instead of hanging the whole build ──
_OP_TIMEOUT = int(os.environ.get("PICARDWATCH_CAT_TIMEOUT", "45"))   # seconds for one directory
                   # listing / file read before it's "stalled" (generous so a slow-but-working op
                   # isn't dropped). Raise via PICARDWATCH_CAT_TIMEOUT on a slow library drive;
                   # heavy Y: readers (enrich, or the watcher scanning a Y: input) should be paused
                   # or pointed elsewhere — they saturate SMB enough that even a listing times out.


def _with_timeout(fn, timeout=_OP_TIMEOUT):
    """Run fn() in a daemon thread; return its result, or None if it stalls past `timeout`
    (the stalled thread is abandoned — fine for a slow/flaky SMB drive)."""
    box: dict = {}

    def work():
        try:
            box["r"] = fn()
        except Exception:
            box["r"] = None

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("r")


def _read_json(p: Path) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(p: Path, data: dict) -> None:
    try:
        Path(p).write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _folders_cache(cfg) -> Path:
    return Path(cfg.paths.state_db).resolve().parent / ".catalogue-folders.json"


def _scan_subdirs(path: Path) -> list:
    """Subdirectories of `path` via os.scandir, which caches each entry's is_dir() from the
    directory enumeration -- ONE SMB round-trip instead of a stat() per entry. The per-entry
    stat storm of iterdir()+is_dir() (~1 stat per artist x hundreds) is exactly what made the
    top-level listing exceed the stall timeout on the slow Y: drive."""
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir():
                    out.append(Path(e.path))
            except OSError:
                pass
    return out


def _dir_has_audio(path: Path, exts) -> bool:
    """True if `path` directly contains an audio file (cached is_file() via os.scandir)."""
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                    return True
            except OSError:
                pass
    return False


def _audio_files(folder: Path, exts) -> list:
    """Audio files in `folder` plus one level down (multi-disc), via os.scandir (cached
    is_file/is_dir -> no stat() per entry, unlike rglob). Sorted for a stable representative."""
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file():
                        if os.path.splitext(e.name)[1].lower() in exts:
                            out.append(Path(e.path))
                    elif e.is_dir():
                        with os.scandir(e.path) as it2:
                            for e2 in it2:
                                if e2.is_file() and os.path.splitext(e2.name)[1].lower() in exts:
                                    out.append(Path(e2.path))
                except OSError:
                    pass
    except OSError:
        pass
    return sorted(out)


def _albums_in_artist(art: Path, exts) -> list:
    """Immediate sub-folders of one artist = its album folders, via a single scandir. The
    audio-presence test is deferred to build_collection (its per-folder rglob + 'if not files:
    continue' drops any non-album folder and finds multi-disc audio one level down), so the same
    folder isn't scandir'd twice -- roughly halving the SMB ops the walk costs on the slow drive."""
    albums = _with_timeout(lambda: _scan_subdirs(art))
    if albums is None:
        log.warning("  stalled artist folder skipped: %s", art.name)
        return []
    return albums


def discover_albums(cfg, exts, refresh: bool = False, limit: int = 0, workers: int = 12) -> list:
    """Resilient Artist/Album walk: times out stalled folders (skips them) and caches the
    album-folder list, so re-runs skip the slow walk. Pass refresh=True to re-walk.

    Artists are scanned in PARALLEL (one thread each) so the slow Y: SMB drive's per-op latency
    overlaps instead of summing -- a sequential walk was ~10 folders/min (hours for the whole
    library); parallel finishes in minutes even while the watcher imports to the same drive.
    os.scandir avoids a stat() per entry on top of that."""
    library = Path(cfg.paths.library)
    cpath = _folders_cache(cfg)
    cached = _read_json(cpath)
    if not refresh and cached.get("folders") and not cached.get("partial"):
        return [Path(f) for f in cached["folders"]]
    log.info("Walking %s for album folders (per-folder timeout %ds, x%d workers) ...",
             library, _OP_TIMEOUT, workers)
    arts = _with_timeout(lambda: sorted(_scan_subdirs(library))) or []
    folders: list = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        done = 0
        for fut in as_completed({ex.submit(_albums_in_artist, a, exts) for a in arts}):
            folders.extend(fut.result())
            done += 1
            if done % 50 == 0 or done == len(arts):
                _write_json(cpath, {"folders": [str(f) for f in folders], "partial": True})
                log.info("  ...%d/%d artists scanned, %d album folders", done, len(arts), len(folders))
            if limit and len(folders) >= limit:
                break
    if limit:
        folders = folders[:limit]
    _write_json(cpath, {"folders": [str(f) for f in folders], "partial": False})
    log.info("Walk complete: %d album folders cached.", len(folders))
    return folders


def _sidecar_for(folder: Path, sidecar_name: str) -> dict | None:
    for cand in (folder / sidecar_name, folder.parent / sidecar_name):
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


# ── build the collection (lean: one read per album) ────────────────────────────
def build_collection(cfg, name: str = "Audio Vault", limit: int = 0, workers: int = 12) -> dict:
    exts = {e.lower() for e in cfg.judge.audio_extensions}
    sidecar_name = str(getattr(getattr(cfg, "enrich", None), "sidecar", "audiophile.json"))
    library = Path(cfg.paths.library)
    annotations = _read_annotations(cfg)
    if not library.exists():
        log.error("Library folder does not exist: %s", library)
        return _finalize(cfg, name, {}, {}, annotations)

    # 1) discover album folders (resilient + cached walk), then read files/sidecars per album
    folders = discover_albums(cfg, exts, limit=limit)
    if limit:
        folders = folders[:limit]
    log.info("Cataloguing %d albums (1 read/album, ×%d) ...", len(folders), workers)
    def _info_for(folder):
        files = _with_timeout(lambda: _audio_files(folder, exts))
        if not files:
            return None
        side = _with_timeout(lambda: _sidecar_for(folder, sidecar_name)) or {}
        return (folder, files, side.get("album") or {},
                {t.get("file"): t for t in (side.get("tracks") or []) if t.get("file")})

    # Parallel + scandir: the old version was a SEQUENTIAL rglob per folder (1842 metadata ops
    # one-at-a-time on the slow drive) -> a silent ~16-min stall. Overlap it like the walk.
    infos: list = []     # (folder, files, side_album, side_tracks)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        n = 0
        for res in ex.map(_info_for, folders):
            n += 1
            if res is not None:
                infos.append(res)
            if n % 200 == 0:
                log.info("  ...listed %d/%d album folders", n, len(folders))
    reps = [info[1][0] for info in infos]    # representative file per album

    # 2) read each album's representative file in parallel — each read time-limited, and the
    #    per-file cache is saved incrementally so a kill/reboot resumes instead of restarting
    cache = _load_cache(cfg)
    next_cache: dict = dict(cache)
    repdata: dict = {}

    def _rep(r):
        return str(r), _with_timeout(lambda: _parse_file(r, cache), 30)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        done = 0
        for fut in as_completed({ex.submit(_rep, r) for r in reps}):
            fpath, entry = fut.result()
            if entry is not None:
                next_cache[fpath] = entry
                repdata[fpath] = entry["d"]
            done += 1
            if done % 100 == 0:
                _save_cache(cfg, next_cache)
                log.info("  ...read %d/%d rep files", done, len(reps))
    _save_cache(cfg, next_cache)

    # 3) build artists + albums
    artists: dict = {}
    albums: dict = {}
    color_i = [0]

    def ensure_artist(nm: str) -> dict:
        if nm not in artists:
            accent = PALETTE[color_i[0] % len(PALETTE)]
            color_i[0] += 1
            artists[nm] = {"id": "ar-" + _slug(nm), "name": nm, "sortName": nm, "formed": None,
                           "origin": "—", "genres": [], "accent": accent, "bio": "",
                           "albumIds": [], "_genres": set()}
        return artists[nm]

    for folder, files, side_album, side_tracks in infos:
        d = repdata.get(str(files[0]), {})
        folder_album = folder.name
        folder_artist = folder.parent.name if folder.parent != library else "Unknown Artist"
        rep_fmt = _fmt_of(files[0])
        artist_name = d.get("albumartist") or d.get("artist") or folder_artist or "Unknown Artist"
        album_title = d.get("album") or re.sub(r"\s*\(\d{4}\).*$", "", folder_album).strip() or "Singles"
        ym = re.search(r"\((\d{4})\)", folder_album)
        year = d.get("year") or (int(ym.group(1)) if ym else None)
        genre = d.get("genre") or "Uncatalogued"
        artist = ensure_artist(artist_name)
        artist["_genres"].add(genre)

        bd, sr = d.get("bitDepth", 0), d.get("sampleRate", 0)
        brate, ch = d.get("bitrate", 0), d.get("channels", 0)
        dur1 = d.get("duration", 0) or 0
        formats: list = []
        for fp in files:
            ff = _fmt_of(fp)
            if ff not in formats:
                formats.append(ff)
        tracks = []
        for i, fp in enumerate(files):
            title = re.sub(r"^\s*\d+[\s.\-_]+", "", fp.stem).strip() or fp.stem
            tr = {"no": i + 1, "title": title,
                  "length": _clock(dur1) if len(files) == 1 else "—",
                  "bitDepth": bd, "sampleRate": sr, "bitrate": brate,
                  "bpm": 0, "key": None, "resolution": _resolution(rep_fmt, bd, sr, brate)}
            env = (side_tracks.get(fp.name) or {}).get("env")
            if env:
                tr["env"] = env
            tracks.append(tr)

        peak = d.get("albumPeak")
        if d.get("trackPeak") is not None:
            peak = max(peak or 0, d["trackPeak"])
        key = f"{artist_name} — {album_title}"
        al = {
            "id": "al-" + _slug(key), "artistId": artist["id"], "title": album_title,
            "year": year, "era": _decade(year) if year else "Undated", "genre": genre,
            "coverColor": _shade(artist["accent"], -55), "accent": artist["accent"],
            "formats": formats, "pressing_details": "—",
            "audio_resolution": _resolution(rep_fmt, bd, sr, brate),
            "bitDepth": bd, "sampleRate": sr, "bitrate": brate, "channels": ch,
            "durationMs": round(dur1 * 1000) * len(files),     # estimate (rep track × count)
            "bytes": (d.get("size", 0) or 0) * len(files),     # estimate
            "bpm": d.get("bpm", 0) or 0, "key": d.get("key"),
            "dr": None, "drSource": None, "peak": peak,
            "loudness": None, "lra": None, "truePeak": None,
            "composer": d.get("composer"), "label": d.get("label"),
            "catalogNumber": d.get("catalogNumber"), "albumGain": d.get("albumGain"),
            "totalTracks": d.get("totalTracks") or len(files),
            "mbReleaseGroupId": d.get("mbReleaseGroupId"), "mbArtistId": d.get("mbArtistId"),
            "mbAlbumId": d.get("mbAlbumId"), "compilation": bool(d.get("compilation")),
            "purchase_date": None, "gear_synergy": "—",
            "listening_notes": "Imported from local files — add archival notes in scanner/annotations.json.",
            "rating": 0, "tracks": tracks,
        }
        for fld in ("loudness", "lra", "truePeak", "dr", "drSource"):   # measured, from the sidecar
            if side_album.get(fld) is not None:
                al[fld] = side_album[fld]
        if al["dr"] is None and al["albumGain"] is not None and (al["peak"] or 0) > 0:
            al["dr"] = max(1, min(30, round(20 * math.log10(al["peak"]) + 18 + al["albumGain"])))
            al["drSource"] = "estimate"
        if al["peak"] is not None:
            al["peak"] = round(al["peak"], 4)
        albums[key] = al
        artist["albumIds"].append(al["id"])

    return _finalize(cfg, name, artists, albums, annotations)


def _finalize(cfg, name, artists, albums, annotations) -> dict:
    artist_list = []
    for a in artists.values():
        a["genres"] = sorted(a["_genres"]) if a["_genres"] else ["Uncatalogued"]
        del a["_genres"]
        artist_list.append(a)
    album_list = list(albums.values())

    # lossless-only vault policy
    kept = [al for al in album_list if any(fmt in LOSSLESS for fmt in al["formats"])]
    dropped = len(album_list) - len(kept)
    kept_ids = {al["id"] for al in kept}
    for ar in artist_list:
        ar["albumIds"] = [i for i in ar["albumIds"] if i in kept_ids]
    live = {al["artistId"] for al in kept}
    kept_artists = [ar for ar in artist_list if ar["id"] in live]

    merged = _apply_annotations({"artists": kept_artists, "albums": kept}, annotations)
    m_albums, m_artists = merged["albums"], merged["artists"]
    drs = [a["dr"] for a in m_albums if a.get("dr")]
    louds = [a["loudness"] for a in m_albums if a.get("loudness") is not None]
    stats = {
        "albums": len(m_albums), "artists": len(m_artists),
        "tracks": sum(len(a.get("tracks", [])) for a in m_albums),
        "bytes": sum(a.get("bytes", 0) for a in m_albums),
        "durationMs": sum(a.get("durationMs", 0) for a in m_albums),
        "hiRes": sum(1 for a in m_albums if (a.get("bitDepth") or 0) >= 24 or (a.get("sampleRate") or 0) > 48000),
        "maxSampleRate": max([0, *[a.get("sampleRate") or 0 for a in m_albums]]),
        "avgDr": round(sum(drs) / len(drs)) if drs else None,
        "avgLoudness": round(sum(louds) / len(louds), 1) if louds else None,
        "analyzed": sum(1 for a in m_albums if a.get("loudness") is not None),
        "dropped_lossy": dropped,
    }
    now = datetime.now(timezone.utc)
    log.info("Collection: %d artists, %d albums (%d lossy dropped), %d enriched.",
             len(m_artists), len(m_albums), dropped, stats["analyzed"])
    return {
        "meta": {"vaultName": name, "owner": "Resident Audiophile",
                 "generatedAt": now.isoformat(), "lastInventory": now.date().isoformat(),
                 "source": str(cfg.paths.library),
                 "generatedBy": "picardwatch (mutagen + audiophile sidecars)",
                 "schemaVersion": "1.1", "stats": stats},
        "artists": m_artists, "albums": m_albums, "milestones": merged["milestones"],
    }


# ── curated annotations from the repo (parity with lib.applyAnnotations) ────────
def _read_annotations(cfg) -> dict:
    repo = getattr(getattr(cfg, "catalogue", None), "repo_dir", "")
    if not repo:
        return {}
    try:
        return json.loads((Path(repo) / "scanner" / "annotations.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _apply_annotations(coll: dict, ann: dict) -> dict:
    artist_ann = ann.get("artists", {}) or {}
    album_ann = ann.get("albums", {}) or {}
    milestones: list = list(coll.get("milestones", []) or [])
    seen = {f"{m.get('artistId')}|{m.get('year')}|{m.get('title')}" for m in milestones}
    for a in coll["artists"]:
        ov = artist_ann.get(a["name"])
        if not ov:
            continue
        for fld in ("bio", "origin", "formed", "sortName", "accent"):
            if ov.get(fld):
                a[fld] = ov[fld]
        if isinstance(ov.get("genres"), list) and ov["genres"]:
            a["genres"] = ov["genres"]
        for m in ov.get("milestones", []) or []:
            k = f"{a['id']}|{m.get('year')}|{m.get('title')}"
            if k not in seen:
                seen.add(k)
                milestones.append({"artistId": a["id"], **m})
    by_id = {a["id"]: a["name"] for a in coll["artists"]}
    for al in coll["albums"]:
        nm = by_id.get(al["artistId"], "")
        ov = album_ann.get(f"{nm} — {al['title']}") or album_ann.get(al["title"])
        if not ov:
            continue
        for fld in ("pressing_details", "gear_synergy", "listening_notes", "purchase_date",
                    "audio_resolution", "genre", "coverColor"):
            if ov.get(fld):
                al[fld] = ov[fld]
        if isinstance(ov.get("rating"), (int, float)):
            al["rating"] = ov["rating"]
        for fx in ov.get("formats_extra", []) or []:
            if fx not in al["formats"]:
                al["formats"].insert(0, fx)
    return {"artists": coll["artists"], "albums": coll["albums"], "milestones": milestones}


# ── write + publish ────────────────────────────────────────────────────────────
def write_collection(repo_dir, collection: dict, filename: str = "collection.json") -> Path:
    Path(repo_dir).mkdir(parents=True, exist_ok=True)
    out = Path(repo_dir) / filename
    out.write_text(json.dumps(collection, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s", out)
    return out


def publish(repo_dir, filename: str = "collection.json") -> bool:
    repo = str(repo_dir)
    try:
        subprocess.run(["git", "-C", repo, "add", filename], check=True, capture_output=True, text=True)
        c = subprocess.run(["git", "-C", repo, "commit", "-m", f"scanner: update {filename}"],
                           capture_output=True, text=True)
        if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr):
            log.info("Nothing to commit — %s unchanged.", filename)
            return True
        p = subprocess.run(["git", "-C", repo, "push"], capture_output=True, text=True)
        if p.returncode != 0:
            log.error("git push failed: %s", (p.stdout + p.stderr).strip())
            return False
        log.info("Pushed %s to the catalogue repo.", filename)
        return True
    except Exception:
        log.exception("publish failed")
        return False
