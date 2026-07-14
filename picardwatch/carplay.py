"""Car Audio / CarPlay experience — a self-contained, on-drive, DJ-grade in-car music release.

`python run.py --carplay` turns a folder of **M4A** albums (ALAC / AAC, e.g. `G:/CAR AUDIO`)
into a professionally-curated in-car listening experience that lives **entirely on that drive** —
nothing is written outside it, nothing is shared with Plex, and (by default) nothing touches
the network. It:

1. **Studies** every track from its M4A tags + the measured `audiophile.json` sidecars
   (loudness / dynamic range / BPM / key). With `--analyze` it measures loudness, DR **and a
   bass "thump" score** itself (ffmpeg) and caches them next to the album.
2. **Checks integrity** — a cheap truncation check always, and a full decodable check with
   `--verify`; unplayable tracks are excluded from every playlist and reported.
3. **Scores** each track for a **noisy moving cabin** and buckets it into 15 driving *themes*
   grouped by Pace / Time of Day / Mood & Scenery / Journey / Audiophile, plus era & genre.
4. **Sequences** each playlist like a DJ set — an energy arc, harmonic (key-aware) transitions,
   and artist-spread so nothing repeats back-to-back.
5. **Writes** portable `.m3u8` playlists, a `carplay-manifest.json`, and a sleek offline browser
   (`index.html`, no server/assets) into `<drive>/_CarPlay/`.
6. **Organises the drive for guaranteed play order** — materialises each theme as a numbered
   folder of tracks (`_CarPlay/Playlists/NN - Theme/NNN - Artist - Title.m4a`) via **hardlinks**
   (zero extra space on NTFS; per-file copy fallback), so even a dumb USB head unit that ignores
   `.m3u8` and plays folders in filename order plays the *intended* order.
7. **Enhances cover art** — audits and shows artwork in the browser, and with `--art` embeds
   artwork into M4As missing it so CarPlay's Now-Playing screen always shows a cover.

Non-destructive to your music by default (reads audio; writes only into `_CarPlay/`). `--art`
is the one flag that rewrites the audio files (to embed artwork); materialisation only links/
copies into `_CarPlay/`. Safe to run anytime — no lock, no state DB.
"""

from __future__ import annotations

import base64
import colorsys
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import mutagen
from mutagen.mp4 import MP4, MP4Cover

from . import catalogue, enrich

try:                                        # optional: real downscaled cover thumbnails
    from PIL import Image as _PILImage       # noqa: N812
except Exception:
    _PILImage = None

log = logging.getLogger("picardwatch.carplay")

_CF = getattr(enrich, "_CF", 0)             # ffmpeg: no console window + below-normal priority


# ── energy / cabin scoring ───────────────────────────────────────────────────────
_BPM_LO, _BPM_HI = 60.0, 165.0
_LUFS_LO, _LUFS_HI = -30.0, -6.0


def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def _norm(x, lo, hi):
    return _clamp((float(x) - lo) / (hi - lo)) if x is not None else None


def _energy(bpm, loudness):
    """0..1 energy from BPM (tempo) + loudness (drive). Uses whichever is present."""
    b = _norm(bpm, _BPM_LO, _BPM_HI) if bpm else None
    l = _norm(loudness, _LUFS_LO, _LUFS_HI)
    if b is not None and l is not None:
        return round(0.65 * b + 0.35 * l, 4)
    if b is not None:
        return round(b, 4)
    if l is not None:
        return round(l, 4)
    return 0.5


def _car_score(loudness, dr, target_lufs):
    """0..100 suitability for a noisy cabin: loudness near the car target, moderate DR."""
    score = 100.0
    if loudness is not None:
        score -= min(60.0, abs(loudness - target_lufs) * 6.0)
    else:
        score -= 12.0
    d = dr if dr else 10
    if d > 13:
        score -= min(30.0, (d - 13) * 4.0)
    elif d < 5:
        score -= (5 - d) * 3.0
    return int(_clamp(round(score), 0, 100))


def _bass_score(measured, loudness, dr, energy):
    """0..100 'thump'. Uses the measured low-end prominence when present (--analyze); otherwise a
    proxy: louder + more compressed + higher-energy tracks feel bassier in a cabin."""
    if measured is not None:
        return int(_clamp(measured, 0, 100))
    l = _norm(loudness, -24.0, -6.0) if loudness is not None else 0.5
    comp = _clamp((12 - (dr or 10)) / 10.0)
    return int(_clamp(0.45 * l + 0.25 * comp + 0.30 * energy) * 100)


# ── musical key → mood + Camelot (for harmonic sequencing) ───────────────────────
_KEY_RE = re.compile(r"^\s*([a-gA-G])([#b♯♭]?)\s*(.*)$")
_CAMELOT_RE = re.compile(r"^\s*(\d{1,2})\s*([ab])\s*$", re.I)
_NOTE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_MAJ = {0: "8B", 7: "9B", 2: "10B", 9: "11B", 4: "12B", 11: "1B",
        6: "2B", 1: "3B", 8: "4B", 3: "5B", 10: "6B", 5: "7B"}
_MIN = {9: "8A", 4: "9A", 11: "10A", 6: "11A", 1: "12A", 8: "1A",
        3: "2A", 10: "3A", 5: "4A", 0: "5A", 7: "6A", 2: "7A"}


def _key_parse(key):
    """(mood, camelot) from a key string: 'Am', 'F# minor', 'Bb maj', Camelot '8A'/'8B'.
    mood ∈ {'major','minor',None}; camelot like '8A' or None."""
    if not key:
        return None, None
    s = str(key).strip()
    cam = _CAMELOT_RE.match(s)
    if cam:
        mode = "minor" if cam.group(2).lower() == "a" else "major"
        return mode, f"{int(cam.group(1))}{cam.group(2).upper()}"
    m = _KEY_RE.match(s)
    if not m:
        return None, None
    pc = _NOTE[m.group(1).lower()]
    acc = m.group(2)
    if acc in ("#", "♯"):
        pc = (pc + 1) % 12
    elif acc in ("b", "♭"):
        pc = (pc - 1) % 12
    minor = "min" in m.group(3).lower() or m.group(3).strip().lower() in ("m", "-")
    mode = "minor" if minor else "major"
    return mode, (_MIN if minor else _MAJ).get(pc)


def _camelot_compat(a, b):
    """True if two Camelot codes mix smoothly: identical, relative major/minor (same number),
    or ±1 on the wheel (same letter). The bread-and-butter of harmonic mixing."""
    if not a or not b or a == b:
        return bool(a and b)
    ma, mb = _CAMELOT_RE.match(a), _CAMELOT_RE.match(b)
    if not (ma and mb):
        return False
    na, la = int(ma.group(1)), ma.group(2).upper()
    nb, lb = int(mb.group(1)), mb.group(2).upper()
    if na == nb:
        return True                                   # relative major/minor
    if la == lb and (abs(na - nb) == 1 or abs(na - nb) == 11):
        return True                                   # ±1 around the 12-hour wheel
    return False


# ── M4A-aware format + embedded-art probe (one MP4 open) ─────────────────────────
_MP4_EXTS = {".m4a", ".mp4", ".m4b", ".alac", ".aac"}


def _probe(path: Path):
    """(fmt, lossless, has_embedded_art, art_bytes|None, art_mime|None) from ONE open. Resolves
    ALAC vs AAC by codec (not extension) and grabs any embedded cover for a thumbnail."""
    ext = path.suffix.lower()
    if ext in _MP4_EXTS:
        try:
            mp4 = MP4(str(path))
            codec = (getattr(mp4.info, "codec", "") or "").lower()
            fmt, loss = ("ALAC", True) if "alac" in codec else ("AAC", False)
            covr = (mp4.tags or {}).get("covr") if mp4.tags else None
            if covr:
                c0 = covr[0]
                mime = "image/png" if c0.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                return fmt, loss, True, bytes(c0), mime
            return fmt, loss, False, None, None
        except Exception:
            return "ALAC", True, False, None, None
    fmt = catalogue._fmt_of(path)
    return fmt, fmt in catalogue.LOSSLESS, False, None, None


# ── cover art ────────────────────────────────────────────────────────────────────
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_ART_PREF = ("cover", "folder", "front", "albumart", "album", "art", "thumb")


def _find_folder_art(folder: Path):
    try:
        with os.scandir(folder) as it:
            imgs = [Path(e.path) for e in it
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in _IMG_EXT]
    except OSError:
        return None
    if not imgs:
        return None
    for pref in _ART_PREF:
        for im in imgs:
            if im.stem.lower().startswith(pref):
                return im
    return sorted(imgs)[0]


def _mime_of(path: Path):
    e = path.suffix.lower()
    return "image/png" if e == ".png" else "image/webp" if e == ".webp" else "image/jpeg"


def _thumb_uri(data: bytes, mime: str):
    """A small data-URI thumbnail for the browser. With Pillow it's a 96px JPEG; without, only
    already-small images are inlined as-is (so the page never bloats with full-res covers)."""
    if not data:
        return None
    if _PILImage is not None:
        try:
            from io import BytesIO
            im = _PILImage.open(BytesIO(data))
            im.thumbnail((96, 96))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = BytesIO()
            im.save(buf, "JPEG", quality=72)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None
    if len(data) <= 45000:
        return f"data:{mime};base64," + base64.b64encode(data).decode()
    return None


def _accent(name: str):
    """Two deterministic hex shades from a name — the browser's fallback cover tile when there's
    no real artwork, so every album still gets a distinct, stable look."""
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:6], 16)
    hue = (h % 360) / 360.0

    def hexof(lig, sat):
        r, g, b = colorsys.hls_to_rgb(hue, lig, sat)
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))

    return [hexof(0.60, 0.55), hexof(0.26, 0.5)]


def _embed_album_art(files, art_bytes, mime) -> int:
    """--art: embed `art_bytes` into every M4A that has no cover, so CarPlay shows artwork.
    Rewrites those files in place (on the drive). Returns how many were updated."""
    if not art_bytes:
        return 0
    fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
    cover = MP4Cover(art_bytes, imageformat=fmt)
    n = 0
    for fp in files:
        if fp.suffix.lower() not in _MP4_EXTS:
            continue
        try:
            mp4 = MP4(str(fp))
            if mp4.tags is None:
                mp4.add_tags()
            if mp4.tags.get("covr"):
                continue
            mp4.tags["covr"] = [cover]
            mp4.save()
            n += 1
        except Exception:
            log.debug("art embed failed for %s", fp.name)
    return n


# ── integrity ────────────────────────────────────────────────────────────────────
def _integrity(path: Path, deep: bool):
    """(ok, issue). Cheap size/truncation check always; a full decodable check when `deep`."""
    try:
        size = path.stat().st_size
    except OSError:
        return False, "unreadable (stat failed)"
    if size < 2048:
        return False, f"empty or truncated ({size} bytes)"
    if not deep:
        return True, None
    try:
        mf = mutagen.File(str(path))
        if mf is None or getattr(mf, "info", None) is None:
            return False, "unreadable audio stream"
        if (getattr(mf.info, "length", 0) or 0) <= 0:
            return False, "zero-length stream"
    except Exception:
        return False, "decode error"
    return True, None


# ── source discovery (resilient, layout-agnostic) ────────────────────────────────
_DISC_RE = re.compile(r"(?i)^(disc|disk|cd|vol|volume)\b")


def _find_album_folders(root: Path, exts, exclude=None, max_depth: int = 5) -> list:
    """Every folder under `root` that IS an album (directly holds audio, or parents Disc N/CD N
    folders). Layout-agnostic; a stalled folder on a slow drive is skipped, not hung on. The
    output tree (`exclude`, e.g. `<source>/_CarPlay`) is pruned so materialised playlist files
    are never re-ingested as albums."""
    found: list = []
    ex = os.path.normcase(os.path.abspath(str(exclude))) if exclude else None

    def _skip(path: Path) -> bool:
        if path.name == "_CarPlay":                     # our output folder name, wherever it sits
            return True
        return ex is not None and os.path.normcase(os.path.abspath(str(path))) == ex

    def walk(path: Path, depth: int):
        if _skip(path):
            return
        if catalogue._with_timeout(lambda: catalogue._dir_has_audio(path, exts)):
            found.append(path)
            return
        subs = catalogue._with_timeout(lambda: catalogue._scan_subdirs(path))
        if not subs:
            return
        if any(_DISC_RE.match(s.name) and catalogue._dir_has_audio(s, exts) for s in subs):
            found.append(path)
            return
        if depth >= max_depth:
            return
        for s in subs:
            walk(s, depth + 1)

    walk(Path(root), 0)
    return sorted(found)


# ── driving themes ────────────────────────────────────────────────────────────────
CATEGORY_ORDER = ["Pace", "Time of Day", "Mood & Scenery", "Journey", "Audiophile",
                  "Collection", "Eras", "Genres"]


def _t(name, cat, arc, blurb, test):
    return {"name": name, "cat": cat, "arc": arc, "blurb": blurb, "test": test}


THEMES = [
    _t("Fast Lane", "Pace", "peak",
       "Spirited, high-energy tracks for when the road opens up and you want to press on.",
       lambda t: t["energy"] >= 0.72 and t["car_score"] >= 55),
    _t("Bass & Thump", "Pace", "flat",
       "Heart-thumping, bass-forward cuts that fill the cabin — subwoofer weather.",
       lambda t: t["bass"] >= 70 and t["car_score"] >= 50),
    _t("Highway Cruise", "Pace", "up",
       "Steady, cabin-cutting energy that holds attention at speed without fatigue.",
       lambda t: 0.52 <= t["energy"] < 0.84 and t["car_score"] >= 50),
    _t("City Commute", "Pace", "flat",
       "Punchy tracks that stay audible over stop-start city traffic.",
       lambda t: t["energy"] >= 0.60 and t["car_score"] >= 50),
    _t("Open Roads", "Pace", "up",
       "Relaxed mid-tempo flow for cruising open highway and Sunday back-roads.",
       lambda t: 0.32 <= t["energy"] < 0.66),
    _t("Sunrise Launch", "Time of Day", "up",
       "Bright, building energy to ease onto the road and start the day's drive.",
       lambda t: 0.42 <= t["energy"] < 0.78 and t["mood"] != "minor" and t["car_score"] >= 45),
    _t("Golden Hour", "Time of Day", "down",
       "Warm, easy tracks for the sunset drive home as the light goes gold.",
       lambda t: 0.30 <= t["energy"] < 0.62 and t["mood"] != "minor"),
    _t("Night Drive", "Time of Day", "down",
       "Mellow, atmospheric tracks for a calm, quiet night cruise.",
       lambda t: t["energy"] < 0.40),
    _t("Midnight Run", "Time of Day", "flat",
       "Darker, hypnotic energy for empty midnight roads.",
       lambda t: 0.40 <= t["energy"] < 0.74 and t["mood"] == "minor"),
    _t("Coastal Cruise", "Mood & Scenery", "flat",
       "Breezy, spacious tracks for a scenic coastal or lakeside run.",
       lambda t: 0.35 <= t["energy"] < 0.68 and t["mood"] != "minor"
                 and (t["loudness"] is None or t["loudness"] <= -9)),
    _t("Feel-Good", "Mood & Scenery", "up",
       "Upbeat, bright tracks to lift the drive and sing along to.",
       lambda t: t["energy"] >= 0.50 and t["mood"] != "minor" and t["car_score"] >= 55),
    _t("Rainy Day", "Mood & Scenery", "down",
       "Moody, introspective tracks for grey-sky miles and wet windscreens.",
       lambda t: t["energy"] < 0.52 and t["mood"] == "minor"),
    _t("Road Trip", "Journey", "wave",
       "A long-haul companion — hours of varied, sustained driving that never flatlines.",
       lambda t: 0.28 <= t["energy"] < 0.86),
    _t("Wind Down", "Journey", "down",
       "Ease off the throttle and settle as you near home at the end of the drive.",
       lambda t: t["energy"] < 0.55 and t["car_score"] >= 40),
    _t("Quiet Cabin Audiophile", "Audiophile", "audiophile",
       "Wide-dynamic-range & hi-res tracks — save these for a parked, quiet cabin.",
       lambda t: (t["dr"] or 0) >= 12 or (t["bitDepth"] >= 24 and t["sampleRate"] > 48000)),
]


def _themes_for(t: dict) -> list:
    return [th["name"] for th in THEMES if th["test"](t)]


# ── per-album study ────────────────────────────────────────────────────────────
def _measure_bass(path: Path):
    """0..100 low-end prominence via ffmpeg: RMS of the sub-150 Hz band vs the full-band RMS
    (first 90 s). Heavy, so only under --analyze; best-effort (None on any failure)."""
    if not enrich.ffmpeg_available():
        return None

    def rms(af):
        try:
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats", "-t", "90", "-i", str(path),
                 "-map", "0:a:0", "-af", f"{af},astats=metadata=1:reset=0", "-f", "null", "-"],
                capture_output=True, text=True, timeout=150, creationflags=_CF)
        except Exception:
            return None
        ms = re.findall(r"RMS level dB:\s*(-?\d+(?:\.\d+)?)", p.stderr)
        return float(ms[-1]) if ms else None

    full, low = rms("anull"), rms("lowpass=f=150")
    if full is None or low is None:
        return None
    diff = low - full                                 # ~-3 (bass-heavy) .. -30 (bass-light)
    return int(_clamp((diff + 30) / 27.0) * 100)


def _album_feature(folder: Path, exts, sidecar_name: str, cache: dict,
                   analyze: bool, verify: bool, art: bool, workers: int) -> dict | None:
    files = catalogue._audio_files(folder, exts)
    if not files:
        return None
    side = catalogue._sidecar_for(folder, sidecar_name) or {}
    side_album = side.get("album") or {}
    side_tracks = {t.get("file"): t for t in (side.get("tracks") or []) if t.get("file")}

    # local loudness/DR + bass study (self-contained; cached on the drive)
    if analyze and side_album.get("loudness") is None:
        try:
            data = enrich.enrich_album(folder, exts, analyze=True, analyze_workers=workers)
        except Exception:
            data = None
        if data:
            side_album = data.get("album") or side_album
            side_tracks = {t.get("file"): t for t in (data.get("tracks") or []) if t.get("file")}
            if side_album.get("bass") is None:
                side_album["bass"] = _measure_bass(files[0])
            data["album"] = side_album
            try:
                enrich.write_sidecar(folder, data, sidecar_name)
            except OSError:
                pass
    elif analyze and side_album.get("bass") is None:
        side_album["bass"] = _measure_bass(files[0])

    entry = catalogue._parse_file(files[0], cache)
    if entry is not None:
        cache[str(files[0])] = entry
    d = (entry or {}).get("d", {}) if entry else {}
    fmt, lossless, has_art, art_bytes, art_mime = _probe(files[0])

    # cover art: folder image wins over embedded (usually higher-res); build a browser thumb
    folder_art = _find_folder_art(folder)
    if folder_art:
        try:
            art_bytes, art_mime = folder_art.read_bytes(), _mime_of(folder_art)
        except OSError:
            pass
    has_any_art = bool(has_art or folder_art)
    if art and art_bytes and not (has_art and all(  # embed where missing
            _probe(f)[2] for f in files if f.suffix.lower() in _MP4_EXTS)):
        _embed_album_art(files, art_bytes, art_mime)
        has_any_art = True

    # integrity (cheap always; deep opens under --verify), overlapped across albums
    integ = {}
    for fp in files:
        integ[fp.name] = _integrity(fp, verify)

    ym = re.search(r"\((\d{4})\)", folder.name)
    year = d.get("year") or (int(ym.group(1)) if ym else None)
    bd, sr, br = d.get("bitDepth", 0) or 0, d.get("sampleRate", 0) or 0, d.get("bitrate", 0) or 0
    akey = f"{d.get('albumartist') or d.get('artist') or folder.parent.name}||{folder.name}"
    return {
        "folder": folder, "files": files,
        "artist": d.get("albumartist") or d.get("artist")
                  or (folder.parent.name if folder.parent != folder else "Unknown Artist")
                  or "Unknown Artist",
        "album": d.get("album") or re.sub(r"\s*\(\d{4}\).*$", "", folder.name).strip() or folder.name,
        "year": year, "genre": (d.get("genre") or "").strip(),
        "bitDepth": bd, "sampleRate": sr, "bitrate": br,
        "format": fmt, "lossless": lossless,
        "resolution": catalogue._resolution(fmt, bd, sr, br),
        "loudness": side_album.get("loudness"), "dr": side_album.get("dr"),
        "lra": side_album.get("lra"), "bass_measured": side_album.get("bass"),
        "album_bpm": side_album.get("bpm") or d.get("bpm") or 0,
        "mbAlbumId": d.get("mbAlbumId"),
        "side_tracks": side_tracks, "integrity": integ,
        "akey": akey, "hasArt": has_any_art,
        "thumb": _thumb_uri(art_bytes, art_mime) if art_bytes else None,
        "accent": _accent(akey),
    }


def _title_of(path: Path) -> str:
    return re.sub(r"^\s*\d+[\s.\-_]+", "", path.stem).strip() or path.stem


def _tracks_from_album(a: dict, target_lufs: float, issues: list) -> list:
    out = []
    for fp in a["files"]:
        ok, issue = a["integrity"].get(fp.name, (True, None))
        if not ok:
            issues.append({"file": str(fp), "album": a["album"], "artist": a["artist"], "issue": issue})
            continue
        st = a["side_tracks"].get(fp.name) or {}
        bpm = st.get("bpm") or a["album_bpm"] or 0
        loud, dr = a["loudness"], a["dr"]
        e = _energy(bpm, loud)
        mood, camelot = _key_parse(st.get("key"))
        out.append({
            "path": fp, "title": st.get("title") or _title_of(fp),
            "artist": a["artist"], "album": a["album"], "year": a["year"],
            "genre": a["genre"] or "Uncatalogued",
            "bpm": int(bpm) if bpm else 0, "loudness": loud, "dr": dr, "lra": a["lra"],
            "bitDepth": a["bitDepth"], "sampleRate": a["sampleRate"],
            "format": a["format"], "lossless": a["lossless"], "resolution": a["resolution"],
            "key": st.get("key"), "mood": mood, "camelot": camelot,
            "energy": e, "car_score": _car_score(loud, dr, target_lufs),
            "bass": _bass_score(a["bass_measured"], loud, dr, e),
            "akey": a["akey"],
        })
    return out


# ── DJ sequencing ────────────────────────────────────────────────────────────────
def _arc_sort(tracks: list, arc: str) -> list:
    if arc == "down":
        return sorted(tracks, key=lambda t: -t["energy"])
    if arc == "flat":
        return sorted(tracks, key=lambda t: abs(t["energy"] - 0.5))
    if arc == "audiophile":
        return sorted(tracks, key=lambda t: -(t["dr"] or 0))
    a = sorted(tracks, key=lambda t: t["energy"])
    if arc == "peak":                                  # rise to a climax, then ease off
        return a[0::2] + a[1::2][::-1]
    if arc == "wave":                                  # gentle swells, never flatlines
        n = max(2, round(len(a) / 5))
        out = []
        for i in range(0, len(a), n):
            chunk = a[i:i + n]
            out.extend(chunk[::-1] if (i // n) % 2 else chunk)
        return out
    return a


def _dj_sequence(tracks: list, arc: str, gap: int) -> list:
    """Order along the energy arc, then greedily refine with a DJ's ear: avoid repeating an
    artist within `gap`, prefer harmonically-compatible (Camelot) transitions, and keep
    energy jumps small — all while staying close to the arc."""
    ordered = _arc_sort(tracks, arc)
    remaining = list(ordered)
    out, recent, prev = [], deque(maxlen=max(1, gap)), None
    while remaining:
        window = remaining[:12]                        # local window keeps the arc mostly intact
        best_i, best_s = 0, -1e9
        for i, t in enumerate(window):
            s = -i * 0.5                                # bias toward preserving arc order
            if t["artist"] in recent:
                s -= 100
            if prev is not None:
                s -= abs(t["energy"] - prev["energy"]) * 8.0
                if _camelot_compat(prev.get("camelot"), t.get("camelot")):
                    s += 6.0
            if s > best_s:
                best_s, best_i = s, i
        t = window[best_i]
        remaining.remove(t)
        out.append(t)
        recent.append(t["artist"])
        prev = t
    return out


def _spread_artists(tracks: list, gap: int) -> list:
    if gap <= 0:
        return list(tracks)
    remaining, out, recent = list(tracks), [], deque(maxlen=gap)
    while remaining:
        pick = next((i for i, t in enumerate(remaining) if t["artist"] not in recent), 0)
        out.append(remaining.pop(pick))
        recent.append(out[-1]["artist"])
    return out


def _loudness_bands(tracks: list, gap: int, cap: int) -> list:
    def band(t):
        return round((t["loudness"] if t["loudness"] is not None else -14) / 3)
    ordered = sorted(tracks, key=lambda t: (band(t), t["energy"]))
    if cap and len(ordered) > cap:
        ordered = ordered[:cap]
    return _spread_artists(ordered, gap)


def _sequence(tracks: list, arc: str, gap: int, cap: int) -> list:
    picked = tracks
    if cap and len(picked) > cap:
        picked = sorted(picked, key=lambda t: -t["car_score"])[:cap]
    return _dj_sequence(picked, arc, gap)


# ── outputs: m3u8 ──────────────────────────────────────────────────────────────
def _m3u_path(path: Path, out_dir: Path, relative: bool) -> str:
    if relative:
        try:
            p = os.path.relpath(path, out_dir)
        except ValueError:
            p = str(path)
    else:
        p = str(path)
    return p.replace(os.sep, "/")


def write_m3u8(out_dir: Path, name: str, tracks: list, relative: bool) -> Path:
    fn = out_dir / (catalogue._slug(name) + ".m3u8")
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for t in tracks:
        lines.append(f'#EXTINF:-1,{t["artist"]} - {t["title"]}')
        lines.append(_m3u_path(t["path"], out_dir, relative))
    fn.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fn


# ── outputs: materialise numbered folders (guaranteed play order) ────────────────
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str, maxlen: int = 80) -> str:
    s = _ILLEGAL.sub("_", str(name)).strip().rstrip(". ")
    return (s[:maxlen].rstrip() or "untitled")


def materialise(out_dir: Path, playlists: list, mode: str) -> dict:
    """Write each playlist as `Playlists/NN - Name/NNN - Artist - Title.ext` so a folder-order
    head unit plays the intended sequence. `mode`: 'link' (hardlink, per-file copy fallback) |
    'copy'. Rebuilds the Playlists tree each run. Returns counts + bytes copied."""
    root = out_dir / "Playlists"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)         # our own tree — safe to rebuild
    root.mkdir(parents=True, exist_ok=True)
    linked = copied = failed = 0
    copied_bytes = 0
    for pi, pl in enumerate(playlists, 1):
        pdir = root / f"{pi:02d} - {_safe(pl['name'])}"
        pdir.mkdir(parents=True, exist_ok=True)
        for ti, t in enumerate(pl["seq"], 1):
            src = t["path"]
            dst = pdir / f"{ti:03d} - {_safe(t['artist'])} - {_safe(t['title'])}{src.suffix.lower()}"
            try:
                if mode == "link":
                    try:
                        os.link(src, dst)
                        linked += 1
                        continue
                    except OSError:
                        pass                              # cross-device / exFAT → copy this one
                shutil.copy2(src, dst)
                copied += 1
                try:
                    copied_bytes += dst.stat().st_size
                except OSError:
                    pass
            except OSError:
                failed += 1
    log.info("Materialised %d playlist folder(s): %d linked, %d copied (%.1f GB), %d failed -> %s",
             len(playlists), linked, copied, copied_bytes / 1e9, failed, root)
    return {"root": str(root), "playlists": len(playlists), "linked": linked,
            "copied": copied, "failed": failed, "copiedGB": round(copied_bytes / 1e9, 2)}


# ── outputs: sleek offline browser ───────────────────────────────────────────────
def _write_browser(out_dir: Path, emitted: list, stats: dict, source: str,
                   target_lufs: float, issues: list, covers: dict, accents: dict) -> Path:
    data = {
        "meta": {"source": source, "targetLufs": target_lufs, "issues": len(issues), **stats},
        "covers": covers, "accents": accents,
        "catOrder": CATEGORY_ORDER,
        "playlists": [{
            "name": e["name"], "cat": e["cat"], "arc": e["arc"], "file": e["file"],
            "blurb": e["blurb"], "avg": e["avg_car_score"], "count": len(e["seq"]),
            "folder": e.get("folder"),
            "tracks": [{
                "ar": t["artist"], "title": t["title"],
                "al": t["album"] + (f" · {t['year']}" if t["year"] else ""),
                "g": t["genre"], "s": t["car_score"], "e": round(t["energy"] * 100),
                "loud": t["loudness"], "dr": t["dr"], "bpm": t["bpm"],
                "key": t["key"] or "—", "mood": t["mood"], "bass": t["bass"],
                "f": t["format"], "bd": t["bitDepth"], "ak": t["akey"],
            } for t in e["seq"]],
        } for e in emitted],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    p = out_dir / "index.html"
    p.write_text(_BROWSER_HTML.replace("/*__DATA__*/", payload), encoding="utf-8")
    return p


_BROWSER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Car Audio — CarPlay</title>
<style>
 :root{--bg:#0a0e14;--panel:#121826;--panel2:#1a2233;--line:#232d40;--txt:#e9eef6;--dim:#8592a6;
  --acc:#5cc8ff;--acc2:#a679ff;--good:#43d17a;--warn:#f0c250;--bad:#ff6b6b;--bass:#ff5a3c}
 *{box-sizing:border-box}html,body{margin:0;height:100%}
 body{background:radial-gradient(1200px 600px at 80% -10%,#16233d 0%,var(--bg) 55%);color:var(--txt);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;display:flex;flex-direction:column;height:100vh;overflow:hidden}
 header{padding:.9rem 1.4rem;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(20,30,52,.7),rgba(10,14,20,.2))}
 .brand{display:flex;align-items:baseline;gap:.6rem}.brand h1{margin:0;font-size:1.35rem;font-weight:700}
 .brand .tag{color:var(--dim);font-size:.85rem}.sub{color:var(--dim);font-size:.78rem;margin-top:.15rem}
 .stats{display:flex;gap:1.6rem;margin-top:.7rem;flex-wrap:wrap}
 .stat b{font-size:1.15rem;font-variant-numeric:tabular-nums}.stat b small{font-size:.7rem;color:var(--dim);font-weight:600}
 .stat span{color:var(--dim);font-size:.66rem;display:block;text-transform:uppercase;letter-spacing:.6px;margin-top:1px}
 .wrap{flex:1;display:flex;min-height:0}
 nav{width:312px;border-right:1px solid var(--line);overflow:auto;background:linear-gradient(180deg,var(--panel),#0c111c)}
 .cat{padding:.7rem 1rem .25rem;color:var(--dim);font-size:.66rem;text-transform:uppercase;letter-spacing:1px;font-weight:700}
 .pl{display:flex;gap:.65rem;align-items:center;padding:.5rem 1rem;cursor:pointer;border-left:3px solid transparent}
 .pl:hover{background:var(--panel2)}.pl.on{background:linear-gradient(90deg,rgba(92,200,255,.12),transparent);border-left-color:var(--acc)}
 .mos,.cv,.cover{overflow:hidden;background-size:cover;background-position:center;display:grid;grid-template-columns:1fr 1fr}
 .mos{width:34px;height:34px;border-radius:7px;flex:none;box-shadow:0 1px 3px #0007}.mos i,.cv i,.cover i{display:block}
 .pl .txt{min-width:0;flex:1}.pl .n{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .pl .m{color:var(--dim);font-size:.72rem;display:flex;gap:.5rem;align-items:center}
 main{flex:1;overflow:auto;padding:1.1rem 1.5rem 3rem}
 .phead{display:flex;gap:1.1rem;align-items:flex-start;margin-bottom:1rem}
 .cover{width:118px;height:118px;border-radius:12px;flex:none;box-shadow:0 8px 26px #000a}
 .phead h2{margin:.1rem 0 .15rem;font-size:1.5rem}.chip{display:inline-block;font-size:.66rem;letter-spacing:.5px;text-transform:uppercase;
  color:var(--acc);border:1px solid #1d3a4d;background:#0e1c26;padding:.12rem .5rem;border-radius:20px;margin-bottom:.4rem}
 .blurb{color:#c3ccd9;max-width:54ch}.pmeta{color:var(--dim);font-size:.8rem;margin-top:.5rem;display:flex;gap:1.1rem;flex-wrap:wrap;align-items:center}
 .arc{margin-left:auto;text-align:right}.arc .lbl{color:var(--dim);font-size:.62rem;text-transform:uppercase;letter-spacing:.8px;margin-bottom:2px}
 table{width:100%;border-collapse:collapse;font-size:.85rem}
 th,td{text-align:left;padding:.46rem .55rem;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:middle}
 th{color:var(--dim);font-weight:600;font-size:.66rem;text-transform:uppercase;letter-spacing:.6px;position:sticky;top:0;background:#0b0f18;z-index:2}
 td.idx{color:var(--dim);text-align:right;font-variant-numeric:tabular-nums;width:1.6rem}td.tk{white-space:normal;min-width:14rem}
 .tkrow{display:flex;gap:.6rem;align-items:center}.tkrow .cv{width:32px;height:32px;border-radius:6px;flex:none}
 .tkrow .tt b{font-weight:600}.tkrow .tt .al{color:var(--dim);font-size:.75rem}
 .num{text-align:right;font-variant-numeric:tabular-nums}
 .bar{display:inline-block;width:58px;height:7px;border-radius:5px;background:#1b2334;overflow:hidden;vertical-align:middle}.bar>i{display:block;height:100%}
 .thump{display:inline-flex;align-items:center;gap:.4rem}.thump .bb{width:52px;height:8px;border-radius:5px;background:#1b2334;overflow:hidden}
 .thump .bb>i{display:block;height:100%;background:linear-gradient(90deg,#ff9d3c,var(--bass))}
 .kdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:.35rem;vertical-align:middle}
 .pill{font-size:.66rem;padding:.06rem .42rem;border:1px solid var(--line);border-radius:20px;color:var(--dim)}
 .pill.loss{color:var(--good);border-color:#1c3a24;background:#0d1c13}.pill.hr{color:var(--warn);border-color:#463a17;background:#1c1708}
 .ok{color:var(--good)}.foot{color:var(--dim);font-size:.72rem;margin-top:1.2rem;border-top:1px solid var(--line);padding-top:.7rem}
 @media(max-width:820px){nav{width:220px}.cover{width:84px;height:84px}.stats{gap:1rem}}
</style></head><body>
<header><div class="brand"><h1>🚗 Car Audio</h1><span class="tag">· curated for CarPlay</span></div>
 <div class="sub" id="sub"></div><div class="stats" id="stats"></div></header>
<div class="wrap"><nav id="nav"></nav><main id="main"></main></div>
<script>
const D=/*__DATA__*/;const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const scol=v=>v>=75?"var(--good)":v>=50?"var(--warn)":"var(--bad)";
function tile(ak,cls){const th=D.covers[ak];if(th)return `<span class="${cls}" style="background-image:url('${th}')"></span>`;
 const a=D.accents[ak]||["#333","#111"];return `<span class="${cls}"><i style="background:${a[0]}"></i><i style="background:${a[1]}"></i><i style="background:${a[1]}"></i><i style="background:${a[0]}"></i></span>`;}
function spark(tr,w=210,h=40){const es=tr.map(t=>t.e/100);if(!es.length)return"";const st=w/Math.max(1,es.length-1);let d="",ar="M0,"+h;
 es.forEach((e,i)=>{const x=i*st,y=h-4-e*(h-8);d+=(i?" L":"M")+x.toFixed(1)+","+y.toFixed(1);ar+=" L"+x.toFixed(1)+","+y.toFixed(1);});ar+=" L"+w+","+h+" Z";
 return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5cc8ff" stop-opacity=".45"/><stop offset="1" stop-color="#5cc8ff" stop-opacity="0"/></linearGradient></defs><path d="${ar}" fill="url(#g)"/><path d="${d}" fill="none" stroke="#7ad4ff" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;}
function mini(tr){const es=tr.map(t=>t.e/100),w=46,h=16,st=w/Math.max(1,es.length-1);let d="";es.forEach((e,i)=>{d+=(i?" L":"M")+(i*st).toFixed(1)+","+(h-2-e*(h-4)).toFixed(1);});
 return `<svg width="${w}" height="${h}"><path d="${d}" fill="none" stroke="#5cc8ff" stroke-width="1.5"/></svg>`;}
$("#sub").innerHTML="Source <b>"+esc(D.meta.source)+"</b> · generated "+esc(D.meta.generated);
$("#stats").innerHTML=[["albums",D.meta.albums],["tracks",D.meta.tracks],["playlists",D.meta.playlists],
 ["measured",D.meta.enriched],["cabin target",D.meta.targetLufs+"<small> LUFS</small>"],
 ["integrity",`<span class="ok">${D.meta.tracks}/${D.meta.tracks+D.meta.issues}</span>`]]
 .map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
let nav="";D.catOrder.forEach(cat=>{const it=D.playlists.map((p,i)=>[p,i]).filter(([p])=>p.cat===cat);if(!it.length)return;
 nav+=`<div class="cat">${esc(cat)}</div>`;it.forEach(([p,i])=>{nav+=`<div class="pl" data-i="${i}" onclick="pick(${i})">${tile(p.tracks[0]&&p.tracks[0].ak,"mos")}
  <div class="txt"><div class="n">${esc(p.name)}</div><div class="m"><span>${p.count} tracks</span><span>cabin ${p.avg}</span>${mini(p.tracks)}</div></div></div>`;});});
$("#nav").innerHTML=nav;
function kdot(m){return `<span class="kdot" style="background:${m=="minor"?"#7c86ff":m=="major"?"#f0b64d":"#556"}"></span>`;}
function fmtPill(t){let h=`<span class="pill ${t.f=="ALAC"||t.f=="FLAC"?"loss":""}">${esc(t.f)}</span>`;if(t.bd>=24)h+=` <span class="pill hr">Hi-Res</span>`;return h;}
window.pick=function(i){document.querySelectorAll(".pl").forEach(e=>e.classList.toggle("on",+e.dataset.i===i));const p=D.playlists[i];
 const ab=Math.round(p.tracks.reduce((a,t)=>a+t.bass,0)/p.tracks.length);
 const rows=p.tracks.map((t,n)=>`<tr><td class="idx">${n+1}</td>
  <td class="tk"><div class="tkrow">${tile(t.ak,"cv")}<div class="tt"><b>${esc(t.ar)} — ${esc(t.title)}</b><div class="al">${esc(t.al)} · ${esc(t.g)}</div></div></div></td>
  <td class="num"><span class="bar"><i style="width:${t.s}%;background:${scol(t.s)}"></i></span> ${t.s}</td>
  <td class="num">${t.e}</td><td class="num">${t.loud==null?"—":t.loud.toFixed(1)}</td><td class="num">${t.dr==null?"—":t.dr}</td><td class="num">${t.bpm||"—"}</td>
  <td>${kdot(t.mood)}${esc(t.key)}</td><td><span class="thump"><span class="bb"><i style="width:${t.bass}%"></i></span>${t.bass}</span></td>
  <td>${fmtPill(t)}</td><td class="num ok">✓</td></tr>`).join("");
 $("#main").innerHTML=`<div class="phead">${tile(p.tracks[0]&&p.tracks[0].ak,"cover")}
  <div><div class="chip">${esc(p.cat)}</div><h2>${esc(p.name)}</h2><div class="blurb">${esc(p.blurb)}</div>
   <div class="pmeta"><span>${p.count} tracks</span><span>avg cabin <b class="ok">${p.avg}</b></span><span>avg thump <b style="color:var(--bass)">${ab}</b></span><span>${esc(p.arc)} arc</span>${p.folder?`<span>▶ ${esc(p.folder)}</span>`:""}</div></div>
  <div class="arc"><div class="lbl">energy journey</div>${spark(p.tracks)}</div></div>
  <table><thead><tr><th></th><th>Track</th><th>Cabin</th><th>Energy</th><th>LUFS</th><th>DR</th><th>BPM</th><th>Key</th><th>Bass · Thump</th><th>Format</th><th>OK</th></tr></thead><tbody>${rows}</tbody></table>
  <div class="foot">Sequenced as a <b>${esc(p.arc)}</b> energy arc with harmonic (key-aware) transitions and artist-spread · cabin scores from measured loudness/DR.</div>`;
 $("#main").scrollTop=0;};
if(D.playlists.length)pick(0);
</script></body></html>
"""


# ── optional online: MusicBrainz genre backfill ──────────────────────────────────
def _backfill_genres(cfg, albums: list) -> int:
    todo = [a for a in albums if not a["genre"] and a.get("mbAlbumId")]
    if not todo:
        return 0
    try:
        from .state import State
        from .musicbrainz import MBClient, release_genre
        client = MBClient(cfg, State(cfg.paths.state_db))
    except Exception:
        log.warning("MusicBrainz reference backfill unavailable — skipping.")
        return 0
    filled = 0
    for a in todo:
        try:
            rel = client.get_release(a["mbAlbumId"])
            g = release_genre(rel) if rel else ""
            if g:
                a["genre"] = g
                filled += 1
        except Exception:
            pass
    log.info("Reference: filled %d missing genre(s) from MusicBrainz.", filled)
    return filled


# ── driver ────────────────────────────────────────────────────────────────────
def build_car_experience(cfg, out_dir: str | None = None, limit: int = 0,
                         reference: bool = False, analyze: bool = False,
                         verify: bool = False, art: bool = False,
                         organize: bool | None = None, workers: int = 12) -> dict:
    cc = getattr(cfg, "carplay", None)
    exts = {e.lower() for e in cfg.judge.audio_extensions}
    sidecar_name = str(getattr(getattr(cfg, "enrich", None), "sidecar", "audiophile.json"))
    source = Path(str(getattr(cc, "source", "") or "") or cfg.paths.library)
    out = Path(out_dir or str(getattr(cc, "out_dir", "") or "") or (source / "_CarPlay"))
    relative = bool(getattr(cc, "relative_paths", True))
    target_lufs = float(getattr(cc, "target_lufs", -13.0))
    gap = int(getattr(cc, "min_artist_gap", 2))
    cap = int(getattr(cc, "max_per_playlist", 500))
    min_tracks = int(getattr(cc, "min_playlist_tracks", 8))
    top_genres = int(getattr(cc, "top_genres", 12))
    reference = reference or bool(getattr(cc, "reference", False))
    analyze = analyze or bool(getattr(cc, "analyze", False))
    verify = verify or bool(getattr(cc, "verify", False))
    art = art or bool(getattr(cc, "art", False))
    mat_mode = str(getattr(cc, "materialize", "link")).lower()
    if organize is False:
        mat_mode = "off"
    elif organize is True and mat_mode == "off":
        mat_mode = "link"

    if not source.exists():
        log.error("Car-audio source does not exist: %s", source)
        return {"error": f"source not found: {source}"}
    if analyze and not enrich.ffmpeg_available():
        log.warning("--analyze requested but ffmpeg is not on PATH — loudness/DR/bass will be "
                    "estimated from tags. Install ffmpeg for measured scoring.")
        analyze = False
    out.mkdir(parents=True, exist_ok=True)

    # 1) discover + study albums (parallel; ffmpeg analysis serialised modestly)
    log.info("Scanning car-audio source %s ...", source)
    folders = _find_album_folders(source, exts, exclude=out)
    if limit:
        folders = folders[:limit]
    log.info("Found %d album folder(s); studying (analyze=%s verify=%s art=%s) ...",
             len(folders), analyze, verify, art)
    cache = catalogue._load_cache(cfg)
    aworkers = 3 if analyze else workers
    albums: list = []
    with ThreadPoolExecutor(max_workers=max(1, aworkers)) as ex:
        futs = {ex.submit(_album_feature, f, exts, sidecar_name, cache, analyze, verify, art, 2)
                for f in folders}
        for fut in as_completed(futs):
            a = fut.result()
            if a:
                albums.append(a)
    catalogue._save_cache(cfg, cache)
    if reference:
        _backfill_genres(cfg, albums)

    # 2) expand to per-track features (excluding integrity failures)
    tracks: list = []
    issues: list = []
    covers: dict = {}
    accents: dict = {}
    for a in albums:
        tracks.extend(_tracks_from_album(a, target_lufs, issues))
        accents[a["akey"]] = a["accent"]
        if a["thumb"]:
            covers[a["akey"]] = a["thumb"]
    enriched = sum(1 for a in albums if a["loudness"] is not None)
    if issues:
        log.warning("Integrity: excluded %d unplayable track(s).", len(issues))
    log.info("Curating %d track(s) across %d album(s) (%d measured, %d with cover art) ...",
             len(tracks), len(albums), enriched, len(covers))

    # 3) bucket into themes + eras + genres
    buckets: dict = {th["name"]: [] for th in THEMES}
    theme_meta = {th["name"]: th for th in THEMES}
    for t in tracks:
        for name in _themes_for(t):
            buckets[name].append(t)
    era: dict = {}
    genre: dict = {}
    for t in tracks:
        if t["year"]:
            era.setdefault(f"{(t['year'] // 10) * 10}s", []).append(t)
        genre.setdefault(t["genre"], []).append(t)

    # 4) sequence + write .m3u8
    emitted: list = []

    def emit(name, seq, cat, arc, blurb, mat=True):
        if len(seq) < min_tracks:
            return
        fn = write_m3u8(out, name, seq, relative)
        avg = round(sum(t["car_score"] for t in seq) / len(seq))
        emitted.append({"name": name, "cat": cat, "arc": arc, "blurb": blurb, "file": fn.name,
                        "path": str(fn), "avg_car_score": avg, "seq": seq, "mat": mat})
        log.info("  %-26s %4d tracks  (cabin %d)", name, len(seq), avg)

    emit("All Car Audio", _loudness_bands(tracks, gap, cap or 0), "Collection", "master",
         "Every track, ordered so perceived volume stays steady across the whole drive.", mat=False)
    for th in THEMES:
        seq = _sequence(buckets[th["name"]], th["arc"], gap, cap)
        emit(th["name"], seq, th["cat"], th["arc"], th["blurb"])
    for k, seq in sorted(era.items(), key=lambda kv: kv[0]):
        emit(k, _sequence(seq, "up", gap, cap), "Eras", "up", f"Everything from the {k}.")
    for k, seq in sorted(genre.items(), key=lambda kv: -len(kv[1]))[:top_genres]:
        if k and k != "Uncatalogued":
            emit(f"Genre — {k}", _sequence(seq, "up", gap, cap), "Genres", "up",
                 f"All the {k} in the collection.")

    # 5) materialise numbered folders for guaranteed play order (themes + eras + genres)
    mat_result = None
    if mat_mode in ("link", "copy"):
        to_mat = [e for e in emitted if e["mat"]]
        mat_result = materialise(out, to_mat, mat_mode)
        rel_root = os.path.relpath(mat_result["root"], out).replace(os.sep, "/")
        for pi, e in enumerate(to_mat, 1):
            e["folder"] = f"{rel_root}/{pi:02d} - {_safe(e['name'])}"

    # 6) manifest + browser
    stats = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "albums": len(albums), "tracks": len(tracks), "enriched": enriched,
             "playlists": len(emitted)}
    manifest = {
        "meta": {"source": str(source), "outDir": str(out), "targetLufs": target_lufs,
                 "relativePaths": relative, "materialize": mat_mode, **stats,
                 "materialised": mat_result, "integrityIssues": issues},
        "playlists": [{"name": e["name"], "category": e["cat"], "arc": e["arc"], "file": e["file"],
                       "tracks": len(e["seq"]), "avgCabinScore": e["avg_car_score"],
                       "folder": e.get("folder"), "blurb": e["blurb"],
                       "items": [_m3u_path(t["path"], out, relative) for t in e["seq"]]}
                      for e in emitted]}
    (out / "carplay-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
    browser = _write_browser(out, emitted, stats, str(source), target_lufs, issues, covers, accents)
    log.info("Wrote %d playlist(s) + manifest + %s to %s", len(emitted), browser.name, out)

    return {"out": str(out), "browser": str(browser), "issues": len(issues),
            "materialised": mat_result, **stats}
