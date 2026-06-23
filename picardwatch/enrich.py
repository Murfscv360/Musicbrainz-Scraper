"""Audiophile enrichment — Python port of audio-enrich.js (opt-in, non-destructive).

Tier 1 (free, no ffmpeg): per-track fields read straight from the file's tags via Mutagen
(bpm, key, isrc, ReplayGain, MusicBrainz ids, label, catalog, disc, compilation, DR tag).

Tier 2 (--analyze, needs ffmpeg on PATH): loudness (LUFS), LRA, true peak, and a 40-bucket
waveform envelope. Memory-safe — the envelope STREAMS ffmpeg's 2 kHz mono u8 PCM and keeps
only a running peak per ~1000-sample frame (a few thousand small ints), never the whole
decoded track; the loudness pass reads only ffmpeg's small R128 summary.

Album aggregation folds each file in, then finalises once (median bpm, top key, mean
loudness, max LRA/true-peak, best-effort DR with `drSource`: "measured" → "tag" → "estimate").

Output: a per-album `audiophile.json` sidecar (the locked schema the app reads). Nothing here
runs unless called — it never moves, retags, or deletes audio, so a live scan is unaffected.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

log = logging.getLogger("picardwatch.enrich")

# ffmpeg children must never flash a console window (run.py also patches Popen globally).
_CF = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ─────────────────────────────────────────────────────────────────────────────
# small parse helpers
# ─────────────────────────────────────────────────────────────────────────────
def _jsround(x: float) -> int:
    return math.floor(x + 0.5)          # match JS Math.round (half-up), not banker's


def _num(s):
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    return float(m.group()) if m else None


def _to_int(s):
    n = _num(s)
    return int(_jsround(n)) if n is not None else None


def _peak(s):
    """ReplayGain peak tags are stored as a linear ratio string (e.g. '0.988')."""
    n = _num(s)
    return round(n, 6) if n is not None else None


def _dr(s):
    n = _num(s)
    if n is None:
        return None
    n = int(n)
    return n if 0 < n < 40 else None


def _truthy(s):
    if s is None:
        return False
    if isinstance(s, bool):
        return s
    return str(s).strip().lower() in ("1", "true", "yes")


def _first(v):
    if isinstance(v, (list, tuple)):
        return _first(v[0]) if v else None
    if isinstance(v, bytes):
        return v.decode("utf-8", "ignore")
    return v if v is None else str(v)


# ─────────────────────────────────────────────────────────────────────────────
# TIER 1 — read tags into a normalized {lowercase-key: str} map, then extract fields.
# ─────────────────────────────────────────────────────────────────────────────
def _vorbis_map(mf) -> dict:
    return {str(k).lower(): _first(v) for k, v in (mf.tags or {}).items()}


def _id3_map(path) -> dict:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        return {}
    out = {}

    def txt(fid):
        fr = id3.getall(fid)
        return str(fr[0].text[0]) if fr and getattr(fr[0], "text", None) else None

    for fid, key in (("TBPM", "bpm"), ("TKEY", "key"), ("TSRC", "isrc"),
                     ("TPUB", "label"), ("TCOM", "composer")):
        v = txt(fid)
        if v:
            out[key] = v
    trck = txt("TRCK")
    if trck:
        out["tracknumber"] = trck.split("/")[0]
        if "/" in trck:
            out["totaltracks"] = trck.split("/")[1]
    tpos = txt("TPOS")
    if tpos:
        out["discnumber"] = tpos.split("/")[0]
    if id3.getall("TCMP"):
        out["compilation"] = "1"
    for fr in id3.getall("TXXX"):
        out["txxx:" + str(fr.desc).lower()] = str(fr.text[0]) if fr.text else ""
    return out


def _mp4_map(mp4) -> dict:
    t = mp4.tags or {}
    out = {}
    if t.get("tmpo"):
        out["bpm"] = str(t["tmpo"][0])
    if "cpil" in t:
        out["compilation"] = "1" if t["cpil"] else ""
    if t.get("disk"):
        out["discnumber"] = str(t["disk"][0][0])
    if t.get("trkn"):
        out["tracknumber"] = str(t["trkn"][0][0])
        if len(t["trkn"][0]) > 1 and t["trkn"][0][1]:
            out["totaltracks"] = str(t["trkn"][0][1])
    for k, v in t.items():
        if k.startswith("----:") and v:                      # iTunes freeform atom
            out[k.split(":")[-1].lower()] = _first(v)
    return out


def _tagmap(path) -> dict:
    ext = Path(path).suffix.lower()
    try:
        if ext == ".flac":
            return _vorbis_map(FLAC(path))
        if ext in (".ogg", ".oga"):
            return _vorbis_map(OggVorbis(path))
        if ext == ".opus":
            return _vorbis_map(OggOpus(path))
        if ext in (".m4a", ".mp4", ".m4b", ".alac"):
            return _mp4_map(MP4(path))
        if ext == ".mp3":
            return _id3_map(path)
        mf = mutagen.File(path)
        if mf is not None and mf.tags:
            return {str(k).lower(): _first(v) for k, v in mf.tags.items()}
    except Exception as exc:                                 # never let one file break a run
        log.debug("tag read failed for %s: %s", path, exc)
    return {}


def tag_fields(path) -> dict:
    """Tier-1 fields for one file (mirrors audio-enrich.js tagFields)."""
    tg = _tagmap(path)

    def pick(*keys):
        for k in keys:
            v = tg.get(k)
            if v not in (None, ""):
                return v
        return None

    return {
        "bpm": _to_int(pick("bpm", "tempo", "txxx:bpm")) or 0,
        "key": (pick("key", "initialkey", "tkey", "txxx:initialkey", "txxx:key") or "").strip() or None,
        "composer": pick("composer") or None,
        "label": pick("label", "organization", "publisher") or None,
        "catalogNumber": pick("catalognumber", "txxx:catalognumber") or None,
        "isrc": pick("isrc", "txxx:isrc") or None,
        "mbReleaseGroupId": pick("musicbrainz_releasegroupid", "musicbrainz release group id",
                                 "txxx:musicbrainz release group id") or None,
        "mbAlbumId": pick("musicbrainz_albumid", "musicbrainz album id",
                          "txxx:musicbrainz album id") or None,
        "mbArtistId": pick("musicbrainz_artistid", "musicbrainz artist id",
                           "txxx:musicbrainz artist id") or None,
        "albumGain": (lambda n: round(n, 1) if n is not None else None)(
            _num(pick("replaygain_album_gain", "txxx:replaygain_album_gain"))),
        "albumPeak": _peak(pick("replaygain_album_peak", "txxx:replaygain_album_peak")),
        "trackPeak": _peak(pick("replaygain_track_peak", "txxx:replaygain_track_peak")),
        "drTag": _dr(pick("dynamic range", "album dynamic range", "dr",
                          "txxx:dynamic range", "txxx:album dynamic range", "txxx:dr")),
        "totalTracks": _to_int(pick("totaltracks", "tracktotal")) or None,
        "discNo": _to_int(pick("discnumber")) or None,
        "compilation": _truthy(pick("compilation")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TIER 2 — ffmpeg measurement (opt-in, slow). Graceful: returns None if anything fails.
# ─────────────────────────────────────────────────────────────────────────────
_FFMPEG_OK = None


def ffmpeg_available() -> bool:
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15, creationflags=_CF)
            _FFMPEG_OK = True
        except Exception:
            _FFMPEG_OK = False
    return _FFMPEG_OK


def _loudness(path):
    # -f null + -nostats: no audio is muxed, only the small R128 summary hits stderr.
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180, creationflags=_CF)
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (p.stderr or "") + (p.stdout or "")
    # ebur128 prints per-frame progress lines (I:/LRA:) BEFORE a final "Summary:" block.
    # Parse inside the Summary so we don't grab an early -70 LUFS gating frame; take the
    # LAST "Peak: ... dBFS" so true peak wins over sample peak (peak=true prints both).
    seg = out.rsplit("Summary:", 1)[-1] if "Summary:" in out else out

    def _last(pat):
        ms = re.findall(pat, seg)
        return ms[-1] if ms else None

    si = _last(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
    if si is None:
        return None
    loud = float(si)
    if not math.isfinite(loud) or loud <= -70:   # -70 = the EBU floor -> no real measurement
        return None
    slra = _last(r"\bLRA:\s*(-?\d+(?:\.\d+)?)\s*LU")
    stp = _last(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")
    return {"loudness": round(loud, 1),
            "lra": round(float(slra), 1) if slra else None,
            "truePeak": round(float(stp), 1) if stp else None}


def _envelope(path, buckets: int = 40):
    FRAME = 1000                          # samples per frame (~0.5 s at 2000 Hz mono)
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-map", "0:a:0", "-ac", "1", "-ar", "2000", "-f", "u8", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_CF)
    except OSError:
        return None
    frames, cur, cnt = [], 0, 0
    deadline = time.monotonic() + 180
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            for b in chunk:                       # b is 0..255
                a = b - 128
                m = -a if a < 0 else a
                if m > cur:
                    cur = m
                cnt += 1
                if cnt >= FRAME:
                    frames.append(cur)
                    cur = 0
                    cnt = 0
            if time.monotonic() > deadline:
                proc.kill()
                return None
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if cnt:
        frames.append(cur)
    if len(frames) < buckets:
        return None
    per = len(frames) / buckets
    env = []
    for bk in range(buckets):
        pk = 0
        s, e = int(bk * per), int((bk + 1) * per)
        for i in range(s, min(e, len(frames))):
            if frames[i] > pk:
                pk = frames[i]
        env.append(min(99, _jsround(pk / 128 * 99)))
    if not any(v > 2 for v in env):
        return None
    return "".join(str(v).zfill(2) for v in env)


def analyze_file(path):
    """{loudness, lra, truePeak, env} (any may be None) or None if ffmpeg/decode fails."""
    if not ffmpeg_available():
        return None
    loud = _loudness(path)
    env = _envelope(path)
    if not loud and not env:
        return None
    return {"loudness": loud["loudness"] if loud else None,
            "lra": loud["lra"] if loud else None,
            "truePeak": loud["truePeak"] if loud else None,
            "env": env}


# ─────────────────────────────────────────────────────────────────────────────
# Album aggregation (mirrors foldFile / finalizeAlbum).
# ─────────────────────────────────────────────────────────────────────────────
def fold_file(album: dict, f: dict) -> None:
    for k in ("composer", "label", "catalogNumber", "mbReleaseGroupId", "mbArtistId", "mbAlbumId"):
        if not album.get(k) and f.get(k):
            album[k] = f[k]
    if album.get("albumGain") is None and f.get("albumGain") is not None:
        album["albumGain"] = f["albumGain"]
    if album.get("peak") is None and f.get("albumPeak") is not None:
        album["peak"] = f["albumPeak"]
    if f.get("trackPeak") is not None:
        album["peak"] = max(album.get("peak") or 0, f["trackPeak"])
    if f.get("drTag"):
        album.setdefault("_drTags", []).append(f["drTag"])
    if f.get("loudness") is not None:
        album["_loudSum"] = album.get("_loudSum", 0) + f["loudness"]
        album["_loudN"] = album.get("_loudN", 0) + 1
    if f.get("lra") is not None:
        album["lra"] = max(album["lra"] if album.get("lra") is not None else 0, f["lra"])
    if f.get("truePeak") is not None:
        album["truePeak"] = max(album["truePeak"] if album.get("truePeak") is not None else -120, f["truePeak"])
    if f.get("totalTracks") and f["totalTracks"] > (album.get("totalTracks") or 0):
        album["totalTracks"] = f["totalTracks"]
    if f.get("discNo") and f["discNo"] > (album.get("discs") or 0):
        album["discs"] = f["discNo"]
    if f.get("compilation"):
        album["compilation"] = True


def finalize_album(album: dict, tracks: list) -> None:
    bpms = sorted(t["bpm"] for t in tracks if t.get("bpm", 0) > 0)
    album["bpm"] = bpms[len(bpms) // 2] if bpms else 0
    kc = {}
    for t in tracks:
        if t.get("key"):
            kc[t["key"]] = kc.get(t["key"], 0) + 1
    album["key"] = max(kc.items(), key=lambda kv: kv[1])[0] if kc else None
    if album.get("_loudN"):
        album["loudness"] = round(album["_loudSum"] / album["_loudN"], 1)
        if album.get("truePeak") is not None:
            album["peak"] = round(10 ** (album["truePeak"] / 20), 4)
    if album.get("loudness") is not None and album.get("truePeak") is not None:
        album["dr"] = max(1, min(30, _jsround(album["truePeak"] - album["loudness"])))
        album["drSource"] = "measured"
    elif album.get("_drTags"):
        album["dr"] = _jsround(sum(album["_drTags"]) / len(album["_drTags"]))
        album["drSource"] = "tag"
    elif album.get("albumGain") is not None and (album.get("peak") or 0) > 0:
        album["dr"] = max(1, min(30, _jsround(20 * math.log10(album["peak"]) + 18 + album["albumGain"])))
        album["drSource"] = "estimate"
    if album.get("peak") is not None:
        album["peak"] = round(album["peak"], 4)
    for k in ("_drTags", "_loudSum", "_loudN"):
        album.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# Driver: enrich one album folder -> the locked schema; and a whole library pass.
# ─────────────────────────────────────────────────────────────────────────────
_ALBUM_KEYS = ["bpm", "key", "composer", "label", "catalogNumber", "mbReleaseGroupId",
               "mbArtistId", "mbAlbumId", "albumGain", "peak", "totalTracks", "discs",
               "compilation", "dr", "drSource", "loudness", "lra", "truePeak"]


def _album_schema(album: dict, ntracks: int) -> dict:
    out = {k: album.get(k) for k in _ALBUM_KEYS}
    out["bpm"] = album.get("bpm") or 0
    out["compilation"] = bool(album.get("compilation"))
    out["discs"] = album.get("discs") or 1
    out["totalTracks"] = album.get("totalTracks") or ntracks
    return out


def _analyze_all(files, workers):
    results = {}
    workers = max(1, int(workers))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, a in zip(files, ex.map(lambda fp: analyze_file(fp), files)):
            results[str(p)] = a
    return results


def enrich_album(folder, exts, analyze: bool = False, analyze_workers: int = 2) -> dict | None:
    folder = Path(folder)
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts)
    if not files:
        return None
    analyses = _analyze_all(files, analyze_workers) if analyze else {}
    album: dict = {}
    tracks = []
    for p in files:
        tf = tag_fields(p)
        a = analyses.get(str(p))
        f = dict(tf)
        if a:
            f.update(loudness=a["loudness"], lra=a["lra"], truePeak=a["truePeak"], env=a["env"])
        track = {"file": p.name, "bpm": tf["bpm"], "key": tf["key"],
                 "isrc": tf["isrc"], "discNo": tf["discNo"]}
        if a and a.get("env"):
            track["env"] = a["env"]
        tracks.append(track)
        fold_file(album, f)
    finalize_album(album, tracks)
    return {"album": _album_schema(album, len(tracks)), "tracks": tracks}


def write_sidecar(folder, data: dict, name: str) -> None:
    (Path(folder) / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def enrich_library(cfg, analyze: bool = False, force: bool = False, limit: int = 0) -> None:
    """Walk the library and write an audiophile sidecar per album. Reads audio + writes
    sidecars only — never moves/retags/deletes — so it's safe to run alongside the watcher."""
    from . import discovery
    exts = {e.lower() for e in cfg.judge.audio_extensions}
    enr = getattr(cfg, "enrich", None)
    name = str(getattr(enr, "sidecar", "audiophile.json"))
    workers = int(getattr(enr, "analyze_workers", 2))
    lib = Path(cfg.paths.library)
    if not lib.exists():
        log.error("Library folder does not exist: %s", lib)
        return
    if analyze and not ffmpeg_available():
        log.warning("ffmpeg not found on PATH — Tier-2 (loudness/LRA/true-peak/waveform) will be "
                    "skipped. Install ffmpeg and re-run with --analyze to enable it.")
    log.info("Enriching library %s  (analyze=%s, workers=%d)%s",
             lib, analyze, workers, "" if not limit else f", limit={limit}")
    scanned = written = 0
    for folder in discovery.find_albums(lib, exts):
        scanned += 1
        sidecar = folder / name
        if not force and sidecar.exists():
            try:
                newest = max(p.stat().st_mtime for p in folder.rglob("*")
                             if p.is_file() and p.suffix.lower() in exts)
                if sidecar.stat().st_mtime >= newest:
                    continue                                  # up to date
            except (ValueError, OSError):
                pass
        try:
            data = enrich_album(folder, exts, analyze=analyze, analyze_workers=workers)
        except Exception:
            log.exception("Enrichment failed for %s (skipping)", folder)
            continue
        if not data:
            continue
        write_sidecar(folder, data, name)
        written += 1
        if written % 25 == 0:
            log.info("  ...%d sidecars written (%d albums scanned)", written, scanned)
        if limit and written >= limit:
            log.info("Reached --limit %d; stopping.", limit)
            break
    log.info("Enrichment complete: %d sidecar(s) written, %d albums scanned under %s",
             written, scanned, lib)
