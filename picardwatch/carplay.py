"""Car Audio / CarPlay experience — a self-contained, on-drive in-car music release (opt-in).

`python run.py --carplay` turns a folder of **M4A** albums (ALAC / AAC, e.g. `G:/CAR AUDIO`)
into a professional, CarPlay-ready listening experience that lives **entirely on that drive** —
nothing is written outside it, nothing is shared with Plex, and (by default) nothing touches
the network. It:

1. **Studies** every track from its M4A tags plus, when present, the measured `audiophile.json`
   sidecars (loudness / dynamic range / BPM / key). With `--analyze` it measures loudness/DR
   itself (ffmpeg) and caches a sidecar next to the album — all on the drive.
2. **Scores** each track for how well it plays in a **noisy moving cabin** and buckets it into
   *drive profiles* (Highway Cruise, City Commute, Night Drive, Open Roads, Quiet-Cabin
   Audiophile) plus era and genre lists.
3. **Sequences** each playlist so perceived volume stays steady and the same artist never
   repeats back-to-back, then writes portable **`.m3u8`** playlists + a **sleek offline
   browser** (`index.html`, no server, no external assets) into `<drive>/_CarPlay/`.

It is non-destructive: it **reads audio and writes into `_CarPlay/` (plus optional sidecars)**
only — it never moves, retags, or deletes your music, takes no lock, and touches no state DB,
so it is safe to run anytime.

Why the loudness/DR study matters in a car: road/wind noise raises the cabin floor to
~65–75 dB, so quiet passages and very wide dynamic range vanish and you keep reaching for the
volume knob. The measurements let us (a) score each track for the cabin, (b) sequence so the
volume stays put, and (c) keep a separate *Quiet-Cabin Audiophile* list that celebrates wide
dynamics for when the car is parked.

Delivery to CarPlay (all local, no cloud): copy the drive (or its `_CarPlay/` folder) to a USB
stick / the phone — head units and the iPhone Music / VLC / Doppler apps read the `.m3u8`
playlists directly. Portable relative paths by default (`carplay.relative_paths`).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from mutagen.mp4 import MP4

from . import catalogue, enrich

log = logging.getLogger("picardwatch.carplay")

# ── drive profiles ──────────────────────────────────────────────────────────────
# Each profile is an energy band (with soft overlaps) tuned to a driving context. A track
# lands in every profile whose band it falls in, so a mid-tempo track can be both an
# "Open Roads" and a "Highway Cruise" pick. Energy is a 0..1 blend of BPM and loudness
# (see _energy) so tracks with no BPM tag still classify by loudness alone.
PROFILES = [
    # name                    lo     hi    needs_cabin_cut  arc         blurb
    ("Night Drive",          0.00, 0.38, False, "down",
     "Mellow, low-energy tracks for a calm night cruise — winds down as it plays."),
    ("Open Roads",           0.30, 0.66, False, "up",
     "Relaxed mid-tempo flow for cruising open highway and Sunday roads."),
    ("Highway Cruise",       0.52, 0.82, True,  "up",
     "Steady, cabin-cutting energy that holds attention at speed without fatigue."),
    ("City Commute",         0.68, 1.01, True,  "flat",
     "Punchy, upbeat tracks that stay audible over stop-start city traffic."),
]

# BPM/loudness normalisation ranges for the energy blend.
_BPM_LO, _BPM_HI = 60.0, 165.0
_LUFS_LO, _LUFS_HI = -30.0, -6.0        # quiet → loud master, mapped to 0..1


def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def _norm(x, lo, hi):
    return _clamp((float(x) - lo) / (hi - lo)) if x is not None else None


def _energy(bpm, loudness):
    """0..1 energy from BPM (tempo) and loudness (drive). Uses whichever is present; if both
    are, BPM dominates (tempo drives the felt energy in a car), loudness nudges it."""
    b = _norm(bpm, _BPM_LO, _BPM_HI) if bpm else None
    l = _norm(loudness, _LUFS_LO, _LUFS_HI)
    if b is not None and l is not None:
        return round(0.65 * b + 0.35 * l, 4)
    if b is not None:
        return round(b, 4)
    if l is not None:
        return round(l, 4)
    return 0.5                            # unknown → neutral middle so it still gets placed


def _car_score(loudness, dr, target_lufs):
    """0..100 suitability for a NOISY cabin. Rewards loudness near the cabin target and a
    moderate dynamic range; penalises very wide DR (quiet detail lost to road noise) and
    brick-walled DR (fatiguing on a long drive). Missing measurements score neutrally so an
    un-analysed library still produces usable playlists."""
    score = 100.0
    if loudness is not None:
        score -= min(60.0, abs(loudness - target_lufs) * 6.0)   # up to -60 for far-off loudness
    else:
        score -= 12.0                                            # unknown loudness: mild neutral
    d = dr if dr else 10
    if d > 13:
        score -= min(30.0, (d - 13) * 4.0)                       # too dynamic for a moving car
    elif d < 5:
        score -= (5 - d) * 3.0                                   # brick-walled → listener fatigue
    return int(_clamp(round(score), 0, 100))


# ── M4A-aware format detection ───────────────────────────────────────────────────
_MP4_EXTS = {".m4a", ".mp4", ".m4b", ".alac", ".aac"}


def _fmt_and_lossless(path: Path) -> tuple[str, bool]:
    """(format label, lossless?) — resolves ALAC vs AAC *inside* an .m4a container by reading
    the codec (extension alone can't tell them apart). Non-MP4 files fall back to the
    catalogue's extension map."""
    if path.suffix.lower() in _MP4_EXTS:
        try:
            codec = (getattr(MP4(str(path)).info, "codec", "") or "").lower()
        except Exception:
            codec = "alac"                # unreadable m4a → assume the common ALAC case
        if "alac" in codec:
            return "ALAC", True
        return "AAC", False
    fmt = catalogue._fmt_of(path)
    return fmt, fmt in catalogue.LOSSLESS


# ── source discovery (resilient, layout-agnostic) ────────────────────────────────
_DISC_RE = re.compile(r"(?i)^(disc|disk|cd|vol|volume)\b")


def _find_album_folders(root: Path, exts, max_depth: int = 5) -> list:
    """Every folder under `root` that IS an album (directly holds audio, or is the parent of
    `Disc N`/`CD N` subfolders that do). Layout-agnostic — works whether the car source is
    flat album folders, Artist/Album, or Genre/Artist/Album. Resilient: a stalled folder on a
    slow/flaky drive is skipped (via catalogue._with_timeout) instead of hanging the build."""
    found: list = []

    def walk(path: Path, depth: int):
        if catalogue._with_timeout(lambda: catalogue._dir_has_audio(path, exts)):
            found.append(path)                              # album leaf (multi-disc read one level down)
            return
        subs = catalogue._with_timeout(lambda: catalogue._scan_subdirs(path))
        if not subs:
            return
        if any(_DISC_RE.match(s.name) and catalogue._dir_has_audio(s, exts) for s in subs):
            found.append(path)                              # multi-disc album: parent is the album
            return
        if depth >= max_depth:
            return
        for s in subs:
            walk(s, depth + 1)

    walk(Path(root), 0)
    return sorted(found)


# ── per-album read (one representative file + the sidecar, optional local analysis) ──
def _album_feature(folder: Path, exts, sidecar_name: str, cache: dict,
                   analyze: bool, workers: int) -> dict | None:
    """Read one album: its file list (scandir), its audiophile sidecar, and one representative
    file for quality/tags. Loudness/DR/format are album-wide (homogeneous mastering) — the same
    lean assumption the catalogue uses. With `analyze` and no measured sidecar, it measures
    loudness/DR itself (ffmpeg) and writes the sidecar next to the album, all on the drive."""
    files = catalogue._audio_files(folder, exts)
    if not files:
        return None
    side = catalogue._sidecar_for(folder, sidecar_name) or {}
    side_album = side.get("album") or {}
    side_tracks = {t.get("file"): t for t in (side.get("tracks") or []) if t.get("file")}

    # Study loudness/DR locally when asked and not already measured — cache it on the drive.
    if analyze and side_album.get("loudness") is None:
        try:
            data = enrich.enrich_album(folder, exts, analyze=True, analyze_workers=workers)
        except Exception:
            data = None
        if data:
            side_album = data.get("album") or side_album
            side_tracks = {t.get("file"): t for t in (data.get("tracks") or []) if t.get("file")}
            try:
                enrich.write_sidecar(folder, data, sidecar_name)   # persist on the drive (self-contained)
            except OSError:
                pass

    entry = catalogue._parse_file(files[0], cache)
    if entry is not None:
        cache[str(files[0])] = entry
    d = (entry or {}).get("d", {}) if entry else {}
    fmt, lossless = _fmt_and_lossless(files[0])
    ym = re.search(r"\((\d{4})\)", folder.name)
    year = d.get("year") or (int(ym.group(1)) if ym else None)
    bd, sr, br = d.get("bitDepth", 0) or 0, d.get("sampleRate", 0) or 0, d.get("bitrate", 0) or 0
    return {
        "folder": folder,
        "files": files,
        "artist": d.get("albumartist") or d.get("artist")
                  or (folder.parent.name if folder.parent != folder else "Unknown Artist")
                  or "Unknown Artist",
        "album": d.get("album") or re.sub(r"\s*\(\d{4}\).*$", "", folder.name).strip() or folder.name,
        "year": year,
        "genre": (d.get("genre") or "").strip(),
        "bitDepth": bd, "sampleRate": sr, "bitrate": br,
        "format": fmt, "lossless": lossless,
        "resolution": catalogue._resolution(fmt, bd, sr, br),
        "loudness": side_album.get("loudness"),
        "dr": side_album.get("dr"),
        "lra": side_album.get("lra"),
        "album_bpm": side_album.get("bpm") or d.get("bpm") or 0,
        "mbAlbumId": d.get("mbAlbumId"),
        "side_tracks": side_tracks,
    }


def _title_of(path: Path) -> str:
    return re.sub(r"^\s*\d+[\s.\-_]+", "", path.stem).strip() or path.stem


def _tracks_from_album(a: dict, target_lufs: float) -> list:
    """Expand an album into per-track feature dicts. Loudness/DR/format are album-wide;
    BPM is per-track from the sidecar when present, else the album median. Every track is
    scored + given an energy so the profile pass can bucket it."""
    out = []
    for fp in a["files"]:
        st = a["side_tracks"].get(fp.name) or {}
        bpm = st.get("bpm") or a["album_bpm"] or 0
        loud, dr = a["loudness"], a["dr"]
        out.append({
            "path": fp,
            "title": st.get("title") or _title_of(fp),
            "artist": a["artist"],
            "album": a["album"],
            "year": a["year"],
            "genre": a["genre"] or "Uncatalogued",
            "bpm": int(bpm) if bpm else 0,
            "loudness": loud,
            "dr": dr,
            "lra": a["lra"],
            "bitDepth": a["bitDepth"],
            "sampleRate": a["sampleRate"],
            "format": a["format"],
            "lossless": a["lossless"],
            "resolution": a["resolution"],
            "energy": _energy(bpm, loud),
            "car_score": _car_score(loud, dr, target_lufs),
        })
    return out


# ── curation ─────────────────────────────────────────────────────────────────────
def _profiles_for(t: dict) -> list:
    e = t["energy"]
    names = []
    for name, lo, hi, needs_cut, _arc, _blurb in PROFILES:
        if lo <= e < hi:
            if needs_cut and t["car_score"] < 50:
                continue                       # too swamped by cabin noise for a "cut through" list
            names.append(name)
    if not names:                              # everything gets at least one drive home
        names.append("Open Roads")
    if (t["dr"] or 0) >= 12 or t["bitDepth"] >= 24 or t["sampleRate"] > 48000:
        names.append("Quiet Cabin Audiophile")
    return names


_ARC = {p[0]: p[3] for p in PROFILES}
_BLURB = {p[0]: p[4] for p in PROFILES}


def _arc_sort(tracks: list, arc: str) -> list:
    """Order a playlist along an energy arc: 'up' ramps up, 'down' winds down, 'flat' keeps a
    steady mid-energy plateau (least jarring in stop-start traffic)."""
    if arc == "down":
        return sorted(tracks, key=lambda t: -t["energy"])
    if arc == "flat":
        return sorted(tracks, key=lambda t: abs(t["energy"] - 0.5))
    return sorted(tracks, key=lambda t: t["energy"])


def _spread_artists(tracks: list, gap: int) -> list:
    """Greedy reorder so the same artist isn't repeated within `gap` tracks, preserving the
    incoming (arc) order as much as possible — no reaching for skip because three tracks by
    the same act just came up."""
    if gap <= 0:
        return list(tracks)
    remaining = list(tracks)
    out, recent = [], deque(maxlen=gap)
    while remaining:
        pick = next((i for i, t in enumerate(remaining) if t["artist"] not in recent), 0)
        t = remaining.pop(pick)
        out.append(t)
        recent.append(t["artist"])
    return out


def _sequence(tracks: list, arc: str, gap: int, cap: int) -> list:
    """Cap to the most cabin-suitable tracks (by car_score), then arc-sort + artist-spread."""
    picked = tracks
    if cap and len(picked) > cap:
        picked = sorted(picked, key=lambda t: -t["car_score"])[:cap]
    return _spread_artists(_arc_sort(picked, arc), gap)


def _loudness_bands(tracks: list, gap: int, cap: int) -> list:
    """Master 'All Car Audio' order: group by perceived-loudness band so the volume stays put
    across the whole drive, ramp energy gently inside each band, then spread artists."""
    def band(t):
        return round((t["loudness"] if t["loudness"] is not None else -14) / 3)   # ~3 LU buckets
    ordered = sorted(tracks, key=lambda t: (band(t), t["energy"]))
    if cap and len(ordered) > cap:
        ordered = ordered[:cap]
    return _spread_artists(ordered, gap)


# ── outputs ────────────────────────────────────────────────────────────────────
def _m3u_path(t: dict, out_dir: Path, relative: bool) -> str:
    if relative:
        try:
            p = os.path.relpath(t["path"], out_dir)
        except ValueError:                     # different drive on Windows → fall back to absolute
            p = str(t["path"])
    else:
        p = str(t["path"])
    return p.replace(os.sep, "/")              # forward slashes: portable across head units + apps


def write_m3u8(out_dir: Path, name: str, tracks: list, relative: bool) -> Path:
    """A UTF-8 `#EXTM3U` playlist. `#EXTINF` carries `Artist - Title` for the CarPlay Now-Playing
    line; duration is `-1` (per-track length isn't read on the lean scan — players ignore it)."""
    fn = out_dir / (catalogue._slug(name) + ".m3u8")
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for t in tracks:
        lines.append(f'#EXTINF:-1,{t["artist"]} - {t["title"]}')
        lines.append(_m3u_path(t, out_dir, relative))
    fn.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fn


def _write_browser(out_dir: Path, emitted: list, stats: dict, source: str, target_lufs: float) -> Path:
    """A single self-contained `index.html` — dark, sleek, no server, no external assets. All
    playlist + track data is embedded inline as JSON, so it opens straight off the drive."""
    data = {
        "meta": {"source": source, "generated": stats["generated"], "targetLufs": target_lufs,
                 "albums": stats["albums"], "tracks": stats["tracks"],
                 "enriched": stats["enriched"], "playlists": len(emitted)},
        "playlists": [{
            "name": e["name"], "file": e["file"], "blurb": e["blurb"],
            "avg": e["avg_car_score"], "count": len(e["seq"]),
            "tracks": [{
                "t": t["title"], "a": t["artist"],
                "al": (t["album"] + (f" · {t['year']}" if t["year"] else "")),
                "s": t["car_score"], "e": round(t["energy"] * 100),
                "l": t["loudness"], "d": t["dr"], "b": t["bpm"],
                "r": t["resolution"], "f": t["format"],
            } for t in e["seq"]],
        } for e in emitted],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    doc = _BROWSER_HTML.replace("/*__DATA__*/", payload)
    p = out_dir / "index.html"
    p.write_text(doc, encoding="utf-8")
    return p


# Self-contained: inline CSS + JS, no CDN/font/asset requests, no server. Data injected at build.
_BROWSER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Car Audio — CarPlay</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2430;--line:#26303c;--txt:#e6edf3;--dim:#8b98a5;--acc:#4cc2ff;--good:#3fb950;--warn:#d29922;--bad:#f85149}
 *{box-sizing:border-box} html,body{margin:0;height:100%}
 body{background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;display:flex;flex-direction:column}
 header{padding:1rem 1.4rem;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#111823,#0d1117)}
 header h1{margin:0;font-size:1.25rem;letter-spacing:.2px} header .sub{color:var(--dim);font-size:.82rem;margin-top:.15rem}
 .stats{display:flex;gap:1.4rem;margin-top:.7rem;flex-wrap:wrap}
 .stat b{font-size:1.3rem} .stat span{color:var(--dim);font-size:.72rem;display:block;text-transform:uppercase;letter-spacing:.5px}
 .wrap{flex:1;display:flex;min-height:0}
 nav{width:290px;border-right:1px solid var(--line);overflow:auto;background:var(--panel)}
 nav .pl{padding:.7rem 1rem;border-bottom:1px solid var(--line);cursor:pointer}
 nav .pl:hover{background:var(--panel2)} nav .pl.on{background:var(--panel2);border-left:3px solid var(--acc);padding-left:calc(1rem - 3px)}
 nav .pl .n{font-weight:600} nav .pl .m{color:var(--dim);font-size:.76rem;margin-top:.1rem;display:flex;gap:.6rem}
 main{flex:1;overflow:auto;padding:1.1rem 1.4rem}
 main h2{margin:.1rem 0 .1rem;font-size:1.15rem} main .blurb{color:var(--dim);margin-bottom:.9rem;font-size:.9rem}
 table{width:100%;border-collapse:collapse;font-size:.86rem}
 th,td{text-align:left;padding:.42rem .55rem;border-bottom:1px solid var(--line);white-space:nowrap}
 th{color:var(--dim);font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0;background:var(--bg)}
 td.t{white-space:normal} td .al{color:var(--dim);font-size:.78rem}
 .num{text-align:right;font-variant-numeric:tabular-nums} .idx{color:var(--dim);text-align:right}
 .bar{display:inline-block;width:60px;height:7px;border-radius:4px;background:var(--panel2);vertical-align:middle;overflow:hidden}
 .bar>i{display:block;height:100%} .pill{font-size:.7rem;padding:.05rem .4rem;border:1px solid var(--line);border-radius:20px;color:var(--dim)}
 .lossless{color:var(--good);border-color:#1c3a24}
 @media(max-width:720px){nav{width:190px} .stats{gap:.9rem}}
</style></head><body>
<header>
 <h1>🚗 Car Audio <span style="color:var(--dim);font-weight:400">· CarPlay playlists</span></h1>
 <div class="sub" id="sub"></div>
 <div class="stats" id="stats"></div>
</header>
<div class="wrap"><nav id="nav"></nav><main id="main"></main></div>
<script>
const DATA=/*__DATA__*/;
const $=s=>document.querySelector(s);
const col=v=>v>=75?'var(--good)':v>=50?'var(--warn)':'var(--bad)';
function fmtL(l){return l==null?'—':l.toFixed(1)}
$('#sub').textContent='Source '+DATA.meta.source+' · generated '+DATA.meta.generated;
$('#stats').innerHTML=[['albums',DATA.meta.albums],['tracks',DATA.meta.tracks],
 ['measured',DATA.meta.enriched],['playlists',DATA.meta.playlists],['cabin target',DATA.meta.targetLufs+' LUFS']]
 .map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
function bar(v){return `<span class="bar"><i style="width:${v}%;background:${col(v)}"></i></span>`}
function renderPlaylist(i){
 document.querySelectorAll('nav .pl').forEach((e,j)=>e.classList.toggle('on',j===i));
 const p=DATA.playlists[i];
 const rows=p.tracks.map((t,n)=>`<tr>
  <td class="idx">${n+1}</td>
  <td class="t"><div>${esc(t.a)} — ${esc(t.t)}</div><div class="al">${esc(t.al)}</div></td>
  <td class="num">${bar(t.s)} ${t.s}</td>
  <td class="num">${t.e}</td>
  <td class="num">${fmtL(t.l)}</td>
  <td class="num">${t.d==null?'—':t.d}</td>
  <td class="num">${t.b||'—'}</td>
  <td><span class="pill ${t.f==='ALAC'||t.f==='FLAC'?'lossless':''}">${esc(t.f)}</span> <span class="al">${esc(t.r)}</span></td>
 </tr>`).join('');
 $('#main').innerHTML=`<h2>${esc(p.name)}</h2><div class="blurb">${esc(p.blurb)} · ${p.count} tracks · avg cabin ${p.avg} · <code>${esc(p.file)}</code></div>
  <table><thead><tr><th></th><th>Track</th><th>Cabin</th><th>Energy</th><th>LUFS</th><th>DR</th><th>BPM</th><th>Format</th></tr></thead><tbody>${rows}</tbody></table>`;
 $('#main').scrollTop=0;
}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
$('#nav').innerHTML=DATA.playlists.map((p,i)=>`<div class="pl" onclick="renderPlaylist(${i})">
  <div class="n">${esc(p.name)}</div>
  <div class="m"><span>${p.count} tracks</span><span>avg cabin ${p.avg}</span></div></div>`).join('');
if(DATA.playlists.length)renderPlaylist(0);
</script></body></html>
"""


# ── optional: MusicBrainz genre backfill (the one online extra; off by default) ───
def _backfill_genres(cfg, albums: list) -> int:
    """OPTIONAL online step (only with --reference): fill an album's missing genre from its
    MusicBrainz release (by MB album id) so genre playlists cover tracks whose local M4A tags
    lack a genre. Cached + rate-limited by the existing MB client; best-effort. The rest of the
    build is fully offline and self-contained."""
    todo = [a for a in albums if not a["genre"] and a.get("mbAlbumId")]
    if not todo:
        return 0
    try:
        from .state import State
        from .musicbrainz import MBClient, release_genre
        client = MBClient(cfg, State(cfg.paths.state_db))
    except Exception:
        log.warning("MusicBrainz reference backfill unavailable — skipping (playlists still build).")
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
            log.debug("genre backfill failed for %s", a["album"])
    log.info("Reference: filled %d missing genre(s) from MusicBrainz.", filled)
    return filled


# ── driver ────────────────────────────────────────────────────────────────────
def build_car_experience(cfg, out_dir: str | None = None, limit: int = 0,
                         reference: bool = False, analyze: bool = False,
                         workers: int = 12) -> dict:
    """Read the car-audio source and curate the CarPlay playlist set + offline browser INTO the
    drive (default `<source>/_CarPlay/`). Non-destructive: reads audio + writes playlists (and,
    with --analyze, audiophile sidecars) only. Returns a summary dict."""
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

    if not source.exists():
        log.error("Car-audio source does not exist: %s", source)
        return {"error": f"source not found: {source}"}
    if analyze and not enrich.ffmpeg_available():
        log.warning("--analyze requested but ffmpeg is not on PATH — loudness/DR will be "
                    "estimated from tags where possible. Install ffmpeg for measured scoring.")
        analyze = False
    out.mkdir(parents=True, exist_ok=True)

    # 1) discover album folders, then read one rep file + sidecar per album (parallel)
    log.info("Scanning car-audio source %s ...", source)
    folders = _find_album_folders(source, exts)
    if limit:
        folders = folders[:limit]
    log.info("Found %d album folder(s); studying tags + loudness/DR (×%d%s) ...",
             len(folders), workers, ", analyze" if analyze else "")
    cache = catalogue._load_cache(cfg)
    albums: list = []
    # ffmpeg analysis is serialised per album inside enrich; keep album-level parallelism modest
    # when analysing so we don't spawn a swarm of ffmpeg processes on the drive.
    aworkers = 3 if analyze else workers
    with ThreadPoolExecutor(max_workers=max(1, aworkers)) as ex:
        futs = {ex.submit(_album_feature, f, exts, sidecar_name, cache, analyze, 2) for f in folders}
        for fut in as_completed(futs):
            a = fut.result()
            if a:
                albums.append(a)
    catalogue._save_cache(cfg, cache)

    if reference:
        _backfill_genres(cfg, albums)

    # 2) expand to per-track features
    tracks: list = []
    for a in albums:
        tracks.extend(_tracks_from_album(a, target_lufs))
    enriched = sum(1 for a in albums if a["loudness"] is not None)
    log.info("Curating %d track(s) across %d album(s) (%d with measured loudness) ...",
             len(tracks), len(albums), enriched)

    # 3) bucket into playlists
    buckets: dict = {name: [] for name, *_ in PROFILES}
    buckets["Quiet Cabin Audiophile"] = []
    for t in tracks:
        for name in _profiles_for(t):
            buckets.setdefault(name, []).append(t)
    era: dict = {}
    genre: dict = {}
    for t in tracks:
        if t["year"]:
            era.setdefault(f"{(t['year'] // 10) * 10}s", []).append(t)
        genre.setdefault(t["genre"], []).append(t)

    # 4) sequence + write
    emitted: list = []

    def emit(name: str, seq: list, blurb: str):
        if len(seq) < min_tracks:
            return
        fn = write_m3u8(out, name, seq, relative)
        avg = round(sum(t["car_score"] for t in seq) / len(seq))
        emitted.append({"name": name, "file": fn.name, "path": str(fn),
                        "tracks": len(seq), "avg_car_score": avg, "blurb": blurb, "seq": seq})
        log.info("  playlist %-26s %4d tracks  (avg cabin %d)", name, len(seq), avg)

    emit("All Car Audio", _loudness_bands(tracks, gap, cap or 0),
         "Every track, ordered so perceived volume stays steady across the whole drive.")
    for name, *_rest in PROFILES:
        emit(name, _sequence(buckets.get(name, []), _ARC[name], gap, cap), _BLURB[name])
    emit("Quiet Cabin Audiophile",
         _spread_artists(sorted(buckets.get("Quiet Cabin Audiophile", []),
                                key=lambda t: -(t["dr"] or 0)), gap)[: cap or None],
         "Wide-dynamic-range & hi-res tracks — save these for a parked, quiet cabin.")
    for label, coll, blurb in (
        ("decade", sorted(era.items(), key=lambda kv: kv[0]),
         "Everything from the {k} — a decade to cruise to."),
        ("genre", sorted(genre.items(), key=lambda kv: -len(kv[1]))[:top_genres],
         "All the {k} in the collection."),
    ):
        for k, seq in coll:
            if k and k != "Uncatalogued":
                emit(k if label == "decade" else f"Genre — {k}",
                     _sequence(seq, "up", gap, cap), blurb.format(k=k))

    stats = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "albums": len(albums), "tracks": len(tracks), "enriched": enriched,
        "playlists": len(emitted),
    }
    manifest = {"meta": {"source": str(source), "outDir": str(out), "targetLufs": target_lufs,
                         "relativePaths": relative, **stats},
                "playlists": [{"name": e["name"], "file": e["file"], "tracks": e["tracks"],
                               "avgCabinScore": e["avg_car_score"], "blurb": e["blurb"],
                               "items": [_m3u_path(t, out, relative) for t in e["seq"]]}
                              for e in emitted]}
    (out / "carplay-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
    browser = _write_browser(out, emitted, stats, str(source), target_lufs)
    log.info("Wrote %d playlist(s) + manifest + %s to %s", len(emitted), browser.name, out)

    return {"out": str(out), "browser": str(browser), **stats}
