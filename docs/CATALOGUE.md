# Catalogue → Audio Vault (`collection.json`)

PicardWatch can publish a **catalogue of the organized library** as the Audio Vault web
app's `collection.json`. It walks the Plex library, reads each album's audio-stream header
+ tags, **merges the `audiophile.json` enrichment** (loudness / DR / waveform — see
[ENRICHMENT.md](ENRICHMENT.md)), and writes the exact Audio Vault schema, then commits +
pushes it to the catalogue repo.

> **Data flow:** PicardWatch (the scanner) → `collection.json` at the repo root → the
> Audio Vault app reads it. The scanner lives here; only its output file lands in the
> [Home-audio-catalogue](https://github.com/Murfscv360/Home-audio-catalogue) repo.

## Commands

```bash
python run.py --catalogue                 # build collection.json into the repo (no push)
python run.py --catalogue --push          # build + git add/commit/push to the catalogue repo
python run.py --catalogue --limit 20      # first 20 albums (quick test)
python run.py --catalogue --out "DIR"     # override the output repo dir
```

Logs go to `logs/catalogue.log`.

## Config (`config.yaml`)

```yaml
catalogue:
  repo_dir: "C:/Users/.../Home-audio-catalogue"   # local clone of the catalogue repo
  file: "collection.json"                         # written to the repo root; the app reads this
  name: "Audio Vault"
```

## What it reads / produces

| Source | Fields |
|---|---|
| Folder layout `Artist/Album (Year) (Type)/` | artist, album, year, era, track titles |
| Audio header (Mutagen) | bit depth, sample rate, bitrate, channels, format, `audio_resolution` |
| File tags | label, catalogue #, MusicBrainz IDs, ISRC, ReplayGain, compilation |
| `audiophile.json` sidecar | **measured** loudness, LRA, true-peak, DR (`drSource`), per-track waveform `env` |
| `scanner/annotations.json` (in the repo) | curated bios, origins, ratings, listening notes (merged on top, never overwritten) |

Output is the locked Audio Vault schema: `meta` (+ `stats`), `artists[]`, `albums[]`,
`milestones[]`. **Lossless-only**: lossy albums (no FLAC/ALAC/WAV/AIFF/DSD/APE) are dropped.

## Lean scan (why it's fast)

The library lives on a slow SMB network drive, so reading every track (~18k files) would
take hours. Album audio quality is **homogeneous across an album**, so the catalogue reads
**one representative file per album** for the quality + tags, and builds the track list from
filenames + the sidecar. This cuts reads ~12×.

- **Only loss:** exact per-track *length* (shown as `—`); bit depth / sample rate / format /
  loudness / DR / waveform are all exact.
- A **`.catalogue-cache.json`** (path + mtime + size) makes re-runs instant — only changed
  files are re-read.

## Performance on a slow drive

The whole build is **network I/O on the library drive** — on this setup a ~1 MB/s SMB share.
Two things keep it from stalling (early versions timed out or stalled for tens of minutes):

- **`os.scandir` + parallel everywhere** — the artist walk, the per-album file/sidecar
  listing, and the one-file-per-album read all run on a thread pool so the drive's per-op
  latency overlaps instead of summing, and use `scandir` (one enumeration per folder) instead
  of `iterdir() + is_dir()` (a `stat()` per entry — hundreds of round-trips just to list the
  top-level artists).
- **Caches** — `.catalogue-folders.json` (the album-folder list) and `.catalogue-cache.json`
  (path + mtime + size per read) make re-runs near-instant; only changed files are re-read.

It now **completes even while the watcher is importing** to the same drive — the import writes
just slow it (a full ~1,800-album build took ~50 min under live imports vs a few minutes on an
idle drive). For the fastest run, do it in a quiet window (`run.py --stop-enrich`, let the
watcher idle). `PICARDWATCH_CAT_TIMEOUT=<seconds>` raises the per-op stall timeout (default 45)
for an exceptionally slow drive. Run `run.py` from the **project directory** so the caches and
`collection.json` resolve to the right place.
