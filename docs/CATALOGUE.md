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

## Performance note

The build's cost is almost entirely **network I/O on the library drive**, and it competes
with the live watcher + the enrichment pass for that drive. For a fast, reliable run, do it
in a **quiet window** — pause the enrichment (`run.py --stop-enrich`) and ideally let the
watcher idle — then `run.py --catalogue --push`. After the first full build the cache makes
refreshes cheap, so a periodic re-run keeps `collection.json` current as new albums import
and more sidecars are enriched.
