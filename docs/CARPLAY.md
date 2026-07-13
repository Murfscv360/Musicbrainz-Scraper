# Car Audio → CarPlay (`--carplay`)

A **self-contained, local-only** in-car music experience built straight from your **M4A**
car-audio drive (e.g. `G:\CAR AUDIO`). One command studies every track, scores it for a noisy
moving cabin, curates CarPlay-ready **`.m3u8` playlists**, and writes a **sleek offline
browser** — all **into the drive itself**. Nothing leaves the drive, nothing is shared with
Plex, and (by default) nothing touches the network.

```
G:\CAR AUDIO\                       ← the M4A source (ALAC / AAC)
  Artist\Album (Year)\NN - Title.m4a
  ...
  _CarPlay\                         ← everything --carplay produces, ON the drive
    index.html                      ← sleek, self-contained offline browser (double-click)
    all-car-audio.m3u8              ← master list (volume-steady across the whole drive)
    highway-cruise.m3u8  night-drive.m3u8  city-commute.m3u8  open-roads.m3u8
    quiet-cabin-audiophile.m3u8
    1990s.m3u8  2000s.m3u8 ...       ← per-decade
    genre-*.m3u8                     ← per-genre (top N)
    carplay-manifest.json            ← machine-readable index of every playlist + its tracks
```

> **Design intent:** a portable *release* that travels with the drive. Copy the drive (or just
> its `_CarPlay\` folder) to a USB stick or the phone and the head unit / iPhone Music / VLC /
> Doppler read the `.m3u8` playlists directly, with correct relative paths.

## Run it

```powershell
# Fast pass — curate from existing tags (+ any audiophile.json sidecars already on the drive):
python run.py --carplay

# Recommended once — measure loudness/DR with ffmpeg and cache sidecars on the drive, then curate:
python run.py --carplay --analyze

# Only the first 20 albums (quick test):
python run.py --carplay --limit 20

# Write somewhere other than <source>\_CarPlay:
python run.py --carplay --out "G:/CAR AUDIO/Playlists"

# OPTIONAL online extra — backfill missing genres from MusicBrainz (cached; off by default):
python run.py --carplay --reference
```

Logs go to `logs/carplay.log`. When it finishes it prints the path to `index.html` — open it
straight off the drive. It is **non-destructive**: it reads audio and writes into `_CarPlay\`
only (and, with `--analyze`, an `audiophile.json` next to each album). It never moves, retags,
or deletes your music, takes no lock, and touches no state DB — safe to run anytime.

## Why a car needs its own curation

A moving cabin is a hostile listening room: road and wind noise raise the noise floor to
**~65–75 dB**, so quiet passages and very wide dynamic range simply disappear, and you end up
riding the volume knob. So the curation is built around **measured loudness + dynamic range**,
not just genre and vibe:

| Car problem | What `--carplay` does about it |
|---|---|
| Quiet detail lost under road noise | **Cabin score** rewards tracks whose integrated loudness sits near a car target (`target_lufs`, default **−13 LUFS** — a touch hotter than the −14 streaming norm). |
| Reaching for the volume between tracks | Every playlist is **sequenced to hold perceived volume steady** (the master list is ordered in ~3 LU loudness bands). |
| Very wide dynamic range washes out | High-DR tracks are **de-prioritised for driving** — but collected into a separate **Quiet-Cabin Audiophile** list for when you're parked and can actually hear them. |
| Brick-walled masters fatigue on a long drive | A mild penalty for DR < 5 keeps the drive lists from being a wall of loudness. |
| Three tracks by the same act in a row | **Artist-spread** guarantees no repeat within `min_artist_gap` tracks. |

## What it studies (all local)

Per album it reads **one representative M4A** (audio quality is homogeneous across an album —
the same lean assumption the catalogue uses) plus the album's **`audiophile.json` sidecar**:

- **From the M4A tags** (Mutagen `MP4`): artist / album / year / genre, and — by reading the
  codec — **ALAC vs AAC** (the `.m4a` extension alone can't tell them apart), plus bit depth /
  sample rate for the resolution label.
- **From the sidecar** (or measured live with `--analyze`): **integrated loudness (LUFS)**,
  **dynamic range (DR)**, **LRA**, and per-track **BPM** / titles.

`--analyze` runs the same ffmpeg measurement as `run.py --enrich --analyze`, per album, and
**caches the `audiophile.json` on the drive**, so it's a one-time cost — later runs reuse it.
Without any measurements a track still scores neutrally, so an un-analysed library still
produces usable playlists (just less finely tuned).

### The two derived numbers

- **Energy** (0–1): a blend of BPM (tempo) and loudness — BPM leads, loudness nudges. Works
  from either signal alone, so tracks with no BPM tag still classify.
- **Cabin score** (0–100): suitability for the noisy cabin — loudness proximity to the target,
  minus penalties for extreme DR (too wide) or brick-walling (too flat).

## The playlists

| Playlist | Who's in it | How it's ordered |
|---|---|---|
| **All Car Audio** | every track | loudness-banded so the volume stays put, energy ramps gently inside each band, artists spread |
| **Night Drive** | low-energy | energy **winds down** as it plays |
| **Open Roads** | relaxed mid-tempo | energy ramps up |
| **Highway Cruise** | mid-high energy **that cuts through the cabin** (score ≥ 50) | energy ramps up |
| **City Commute** | punchy / upbeat **that cuts through** | steady mid-high plateau (least jarring in stop-start traffic) |
| **Quiet Cabin Audiophile** | wide-DR (≥ 12) or hi-res | most-dynamic first — for a *parked* cabin |
| **`<decade>`** | by release decade | energy ramp |
| **Genre — `<name>`** | top-N most-populous genres | energy ramp |

A track appears in **every** profile whose energy band it falls in (bands overlap softly), so a
mid-tempo cut can be both an *Open Roads* and a *Highway Cruise* pick. Themed lists thinner
than `min_playlist_tracks` are dropped; lists longer than `max_per_playlist` keep the most
cabin-suitable tracks.

## The offline browser (`index.html`)

A single **self-contained** page — inline CSS + JS, **no server, no CDN, no external fonts or
assets** — with all playlist and track data embedded as JSON. Double-click it on the drive and
you get a dark, sleek console: playlist nav on the left; on the right each track with a
**cabin-score bar**, energy, LUFS, DR, BPM, and an **ALAC/AAC + resolution** badge. Because it's
one file with the data baked in, it works from a USB stick or the phone with no setup.

## Delivery to CarPlay

All local — pick whichever your car supports:

1. **USB / SD in the head unit** — copy the drive (or `_CarPlay\`). Relative `.m3u8` paths
   resolve on the stick. Most modern head units read `.m3u8` and show the `Artist - Title` from
   each `#EXTINF` on the Now-Playing screen.
2. **iPhone → CarPlay** — import the tracks + `.m3u8` files into the Music app (or a local-file
   player like VLC / Doppler that supports playlists); CarPlay then surfaces them.

Keep `relative_paths: true` and `out_dir` **on the same drive as the audio** so the paths stay
valid wherever the drive is mounted.

## Config (`config.yaml`)

```yaml
carplay:
  source: "G:/CAR AUDIO"    # the M4A drive to curate ("" = use paths.library)
  out_dir: ""               # "" = <source>/_CarPlay (kept ON the drive)
  relative_paths: true      # portable paths for a USB stick / phone
  analyze: false            # measure loudness/DR with ffmpeg (caches sidecars on the drive); also --analyze
  target_lufs: -13.0        # cabin loudness target — drives scoring + sequencing
  min_artist_gap: 2         # no repeat artist within this many tracks
  max_per_playlist: 500     # cap a themed list (0 = no cap)
  min_playlist_tracks: 8    # drop thinner themed lists
  top_genres: 12            # how many genre lists to emit
  reference: false          # OPTIONAL online: backfill missing genres from MusicBrainz; also --reference
```

## Outputs

| File | What it is |
|---|---|
| `_CarPlay/index.html` | self-contained offline browser (double-click) |
| `_CarPlay/*.m3u8` | UTF-8 `#EXTM3U` playlists, relative paths, `#EXTINF` = `Artist - Title` |
| `_CarPlay/carplay-manifest.json` | machine-readable: every playlist, its cabin-score, and its ordered track paths |

## Performance & resilience

Built on the catalogue's slow-drive machinery: a **layout-agnostic** walk (flat, `Artist/Album`,
or `Genre/Artist/Album`, multi-disc aware) that **times out and skips** a stalled folder instead
of hanging, `os.scandir` (no `stat()` storm), a **path+mtime+size cache** for the representative
reads, and album-level parallelism. `--analyze` is the only slow part (one ffmpeg pass per
album); it's serialised modestly so it never swarms the drive, and it's cached so you pay it
once.

## Design notes

- **Local & self-contained by construction.** The only code path that touches the network is
  the opt-in `--reference` genre backfill; everything else — discovery, measurement, curation,
  the browser — is offline and writes only inside the drive.
- **M4A-first.** ALAC/AAC are resolved by reading the codec, not the extension, so lossless
  ALAC is correctly flagged (and celebrated in the audiophile list) while AAC isn't mislabelled.
- **Reuses the enrichment.** The loudness/DR study is the exact `picardwatch/enrich.py`
  measurement, so the numbers match `--enrich` / the catalogue, and existing sidecars are
  reused for free.
- **Non-destructive, no lock.** Like `--enrich` and `--catalogue`, it only reads audio and
  writes its own outputs, so it can run anytime without coordinating with the importer.
