# Car Audio → CarPlay (`--carplay`)

A **self-contained, local-only, DJ-grade** in-car music experience built straight from your
**M4A** drive (e.g. `G:\CAR AUDIO`). One command studies every track, checks it plays, scores it
for a noisy cabin, curates driving-themed playlists, sequences them like a DJ set, **organises
the drive so play order is guaranteed**, and writes a sleek offline browser — all **into the
drive itself**. Nothing leaves the drive, nothing is shared with Plex, and (by default) nothing
touches the network.

```
G:\CAR AUDIO\                       ← the M4A source (ALAC / AAC)
  Artist\Album (Year)\NN - Title.m4a
  _CarPlay\                         ← everything --carplay produces, ON the drive
    index.html                      ← sleek, self-contained offline browser (double-click)
    all-car-audio.m3u8  bass-thump.m3u8  night-drive.m3u8 ...   ← .m3u8 playlists
    carplay-manifest.json           ← machine-readable index of every playlist + its tracks
    Playlists\                      ← numbered folders for guaranteed play order (hardlinks)
      02 - Bass & Thump\
        001 - Justice - Genesis.m4a
        002 - Kavinsky - Nightcall.m4a  ...
      03 - Highway Cruise\ ...
```

## Run it

```powershell
# Recommended first run — measure loudness/DR/bass (ffmpeg), verify every file, embed missing
# art, and organise the drive for guaranteed play order:
python run.py --carplay --analyze --verify --art

# Fast pass — curate from existing tags/sidecars only (no ffmpeg), still organises the drive:
python run.py --carplay

# Just the playlists + browser, no folder reorganisation:
python run.py --carplay --no-organize

# First 20 albums (quick test); or write somewhere other than <source>\_CarPlay:
python run.py --carplay --limit 20
python run.py --carplay --out "G:/CAR AUDIO/Drive Mixes"

# OPTIONAL online extra — backfill missing genres from MusicBrainz (cached; off by default):
python run.py --carplay --reference
```

Logs go to `logs/carplay.log`. It is **non-destructive to your music** by default (reads audio;
writes only into `_CarPlay/`). The one exception is **`--art`**, which embeds artwork into M4As
that lack it (rewriting those files, on the drive). Materialisation only **hardlinks** into
`_CarPlay/` — it never moves or alters your originals.

## Why a car needs its own curation

A moving cabin is a hostile listening room: road/wind noise raises the floor to **~65–75 dB**, so
quiet passages and wide dynamic range vanish and you ride the volume knob. So the curation is
built on **measured loudness, dynamic range and bass**, not just genre and vibe:

| Car problem | What `--carplay` does |
|---|---|
| Quiet detail lost under road noise | **Cabin score** rewards loudness near a car target (`target_lufs`, default **−13 LUFS**). |
| Reaching for the volume between tracks | Playlists are **sequenced to hold perceived volume steady** (the master list is loudness-banded). |
| Wide dynamic range washes out | High-DR tracks are de-prioritised for driving, but collected into **Quiet-Cabin Audiophile** for parked listening. |
| Wanting the bass to hit | A measured **bass "thump" score** (ffmpeg low-band vs full-band RMS) powers the **Bass & Thump** theme. |
| Jarring key changes between tracks | **Harmonic sequencing** prefers Camelot-compatible transitions, like a DJ mixing in key. |
| Same artist three times in a row | **Artist-spread** guarantees no repeat within `min_artist_gap` tracks. |
| A corrupt/half-downloaded file killing playback | The **integrity gate** excludes unplayable tracks (and lists them). |
| Head units that ignore `.m3u8` | The drive is **reorganised into numbered folders** so folder-order playback = intended order. |

## What it studies (all local)

Per album it reads **one representative M4A** (quality is homogeneous across an album) plus the
album's **`audiophile.json` sidecar**:

- **From the M4A tags** (Mutagen `MP4`): artist / album / year / genre / key, and — by reading
  the codec — **ALAC vs AAC** (the `.m4a` extension can't tell them apart), plus bit depth /
  sample rate.
- **From the sidecar** (or measured live with `--analyze`): **loudness (LUFS)**, **dynamic
  range (DR)**, **LRA**, per-track **BPM**, and a **bass score** (low-frequency prominence).

`--analyze` runs the ffmpeg measurement (loudness/DR from `enrich.py`, plus a bass pass) and
**caches it in the sidecar on the drive**, so it's a one-time cost. Without measurements a track
still scores from a proxy (louder + more compressed + higher tempo reads as bassier), so an
un-analysed library still produces good playlists.

### Derived numbers
- **Energy** (0–1): BPM (tempo) blended with loudness.
- **Cabin score** (0–100): suitability for the noisy cabin.
- **Bass** (0–100): measured low-end prominence, or the proxy.
- **Mood** (major/minor) and **Camelot** code from the musical key → mood themes + harmonic mixing.

## The driving themes

15 themes grouped by category (a track lands in every theme whose rule it fits):

| Category | Themes |
|---|---|
| **Pace** | Fast Lane · **Bass & Thump** · Highway Cruise · City Commute · Open Roads |
| **Time of Day** | Sunrise Launch · Golden Hour · Night Drive · Midnight Run |
| **Mood & Scenery** | Coastal Cruise · Feel-Good · Rainy Day |
| **Journey** | Road Trip (long-haul) · Wind Down (end of drive) |
| **Audiophile** | Quiet Cabin Audiophile (parked) |
| **Collection** | All Car Audio (master) · per-decade · per-genre |

Each is sequenced along an **energy arc** (`up` / `down` / `flat` / `peak` / `wave`) then refined
with harmonic + artist-spread. Themes thinner than `min_playlist_tracks` are dropped; lists over
`max_per_playlist` keep the most cabin-suitable tracks.

## Guaranteed play order on the drive

Many car USB/head units ignore `.m3u8` and just play a folder's files in filename order. So each
theme is also **materialised** as a numbered folder:

```
_CarPlay\Playlists\02 - Bass & Thump\001 - Artist - Title.m4a
                                     \002 - Artist - Title.m4a  ...
```

- **`materialize: link`** (default) — **hardlinks**: a track in 8 playlists still stores **once**,
  so the whole tree adds **~zero disk space** on an NTFS drive. Any file that can't be hardlinked
  (e.g. exFAT USB) is copied instead.
- **`materialize: copy`** — real copies (works on any FAT/exFAT USB, but **duplicates** data).
- **`materialize: off`** / `--no-organize` — `.m3u8` + browser only.

The `Playlists\` tree is **rebuilt each run** (safe — it only ever touches its own folder). The
master "All Car Audio" list stays `.m3u8`-only (materialising the whole library in one order adds
little for a lot of entries).

## Cover art

Artwork drives the CarPlay Now-Playing screen. `--carplay` **audits** each album (embedded `covr`
atom or a folder `cover.jpg`/`front.png`/…) and shows thumbnails in the browser (real downscaled
covers when [Pillow](https://python-pillow.org/) is installed, else a generated colour tile).
**`--art`** additionally **embeds** the best available art into any M4A missing it, so CarPlay
always shows a cover. `--art` rewrites those files (on the drive).

## Track integrity

- **Always on (cheap):** a size/truncation check catches empty or half-downloaded files.
- **`--verify` (deep):** opens every file and confirms a decodable audio stream with a real
  length. Slower on a big SMB drive (it reads each file), so it's opt-in.

Unplayable tracks are **excluded from every playlist** and listed in the browser stat line and
`carplay-manifest.json` (`meta.integrityIssues`).

## The offline browser (`index.html`)

A single **self-contained** page — inline CSS + JS, **no server, no CDN, no external assets** —
with all data embedded as JSON. Dark, sleek: playlists grouped by category in the nav (each with
a mini energy sparkline + cover tile); the main panel shows the **energy-journey sparkline** and a
track table with **cabin-score bar, energy, LUFS, DR, BPM, key + mood dot, a bass "thump" meter,
ALAC/AAC + Hi-Res badges, and the integrity ✓**. Opens straight off the drive.

## Config (`config.yaml`)

```yaml
carplay:
  source: "G:/CAR AUDIO"    # the M4A drive to curate ("" = paths.library)
  out_dir: ""               # "" = <source>/_CarPlay (kept ON the drive)
  relative_paths: true      # portable .m3u8 paths for a USB stick / phone
  analyze: false            # measure loudness/DR/bass with ffmpeg (caches sidecars); also --analyze
  verify: false             # deep per-track integrity (opens every file); also --verify
  art: false                # embed cover art into M4As missing it; also --art
  materialize: link         # link | copy | off — organise the drive for guaranteed play order
  target_lufs: -13.0        # cabin loudness target — drives scoring + sequencing
  min_artist_gap: 2         # no repeat artist within this many tracks
  max_per_playlist: 500     # cap a themed list (0 = no cap)
  min_playlist_tracks: 8    # drop thinner themed lists
  top_genres: 12            # how many genre lists to emit
  reference: false          # OPTIONAL online: backfill missing genres from MusicBrainz; also --reference
```

## Outputs

| File / folder | What it is |
|---|---|
| `_CarPlay/index.html` | self-contained offline browser |
| `_CarPlay/*.m3u8` | `#EXTM3U` playlists, relative paths, `#EXTINF` = `Artist - Title` |
| `_CarPlay/Playlists/NN - Theme/NNN - Artist - Title.m4a` | numbered folders (hardlinks) for guaranteed play order |
| `_CarPlay/carplay-manifest.json` | every playlist (category, arc, cabin score, folder, ordered tracks) + integrity issues + materialise stats |

## Performance & resilience

Built on the catalogue's slow-drive machinery: a **layout-agnostic** walk that **times out and
skips** a stalled folder, `os.scandir` (no `stat()` storm), a **path+mtime+size cache** for the
representative reads, and album-level parallelism. The output tree is **excluded from discovery**
so materialised files are never re-ingested. `--analyze` (ffmpeg) and `--verify` (open every file)
are the slow, opt-in parts; both cache/short-circuit so re-runs are cheap.
