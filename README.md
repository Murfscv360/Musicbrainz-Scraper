# PicardWatch

Automated, **Plex-ready** album importer. It watches an input folder, and for every
album that lands it decides — using **AcoustID fingerprints + existing tags + the
MusicBrainz database** — whether the folder is a *complete, high-confidence* match to a
single MusicBrainz release. **Only "perfect" albums** are imported: PicardWatch writes the
tags, embeds **and** saves the cover art, builds the `Artist/Album (Year)` folders, and
**moves** them into your Plex library. Everything imperfect is **left exactly where it is**
and listed in `review_report.html`. (Tagging is done natively via Mutagen; MusicBrainz
Picard is optional.)

```
Input\Album\ ─► watcher ─► judge (fpcalc+AcoustID+MB) ─► perfect? ─► native import (tag + art + move) ─► Library\Artist\Album (Year)\01 - Title.flac ─► Plex scan
                                                         └─ not perfect ─► leave in place + review report
```

**Docs:** [Handoff](HANDOFF.md) · [Architecture](docs/ARCHITECTURE.md) · [Design](docs/DESIGN.md) · [Enrichment](docs/ENRICHMENT.md) · [Catalogue → Audio Vault](docs/CATALOGUE.md) · [Car Audio → CarPlay](docs/CARPLAY.md)

## Project status — _2026-06-18_

**Deployed and actively importing.** The watcher runs through the backlog and then watches for new drops; it auto-starts at logon and resumes across reboots.

| | |
|---|---|
| Stage | Bulk import in progress (multi-day run) |
| Imported | **77 albums** (788 tracks) |
| In review | 38 |
| Duplicates skipped | 78 |
| Failed | 0 |
| Processed | 193 of ~4,531 |
| Pace / ETA | ~0.4 albums/min → multi-day (≈ a week) for the full backlog |

**Most recent:** fixed a watcher freeze caused by an antivirus-dropped HTTPS connection (added a network timeout + MusicBrainz retry). Full state, operating guide, and next steps are in **[HANDOFF.md](HANDOFF.md)**. For live numbers run `.\status.ps1`.

## How "perfect" is decided
An album is imported only when **all** of these hold (thresholds in `config.yaml`):
- every release track is matched by a file (no missing tracks),
- every audio file matched a track on the release (no stray files),
- file count == the release's track count (correct edition),
- minimum per-track fingerprint confidence ≥ `acoustid.min_score`,
- exactly one MusicBrainz release was chosen.

Matching is primarily by **recording MBID** (AcoustID → file recordings ∩ release tracklist);
files AcoustID can't identify fall back to duration + fuzzy-title matching unless
`judge.require_full_acoustid: true`.

## Prerequisites
1. **Python 3.10+**.
2. **`fpcalc`** (Chromaprint) — `install.ps1` downloads it into `bin\` automatically (no PATH change needed).
3. **AcoustID application API key** (free) — https://acoustid.org/new-application
4. A **Plex** music library pointed at your destination folder (optional auto-scan).
5. *(Optional)* **MusicBrainz Picard** — only if you switch the tagging engine back to Picard.

## Install
**Easiest:** double-click **`install.bat`** (or run `.\install.ps1`). It creates the venv,
installs dependencies, downloads `fpcalc` into `bin\`, creates `config.yaml` from the
template, and prompts for your input/library folders + AcoustID key.

Manual:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.yaml config.yaml   # then edit it
```

## Configure
Edit **`config.yaml`** (copied from `config.example.yaml`):
- `paths.input` / `paths.library` — the watched folder and the destination library
- `acoustid.api_key` — free key from https://acoustid.org/new-application
- `musicbrainz.contact` — your email (identifies your API client; required by MusicBrainz)
- *(optional)* `importer.*` to tune dedupe / art embedding / source cleanup, `plex.*` for auto-scan

Tagging is **native** by default (no Picard needed); `picard-batch.ini` only matters if you
switch the engine back to Picard.

## Layout produced
```
Library/<Album Artist>/<Album> (Year) (Type)/
    01 - Title.flac ...                 # single-disc: flat
    cover.jpg
Library/<Album Artist>/<Album> (Year) (Type)/
    Disc 1/01 - Title.flac ...          # multi-disc: one subfolder per disc
    Disc 2/01 - Title.flac ...
    cover.jpg
```
`(Type)` is the MusicBrainz release type — `Album`, `EP`, `Single`, `Compilation`, etc.

## Run
```powershell
# 1) Safe first pass — judges + writes the review report, moves NOTHING:
python run.py --once --dry-run -v

# 2) One folder end-to-end (real move) once you trust the matching:
python run.py --folder "D:\Music\Input\Some Album"

# 3) Whole input folder once:
python run.py --once

# 4) The daemon (event-driven + periodic rescan):
python run.py --watch

# 5) Kept-alive daemon — restarts the watcher whenever it stops or hangs (what autostart runs):
python run.py --supervise          #   stop cleanly with:  python run.py --stop

# 6) Audiophile enrichment sidecars (opt-in, ffmpeg; see docs/ENRICHMENT.md):
python run.py --enrich --analyze   #   managed + AV-resilient:  --start-enrich / --stop-enrich

# 7) Publish the Audio Vault catalogue collection.json (see docs/CATALOGUE.md):
python run.py --catalogue --push

# 7b) Self-contained, DJ-grade Car Audio / CarPlay experience onto the M4A drive (see docs/CARPLAY.md):
python run.py --carplay --analyze --verify --art
#     driving-themed playlists + numbered play-order folders + sleek offline browser, all in <source>/_CarPlay/

# 8) Re-tag + re-organize the existing library to the current standard:
python run.py --retag

# Progress snapshot any time:
python run.py --status
```
Open `review_report.html` to see what was left behind and why. Fix a folder's contents
(e.g. add the missing track) and it's **re-evaluated automatically** next scan — the SQLite
state keys off a content signature, not just the path.

## Keep it running (autostart + self-heal)
Native tagging needs no GUI, but the watcher still runs in your **logon session** (the
library is on a per-user mapped drive). It stays alive three ways:
- **Supervisor** — `run.py --supervise` (re)starts the watcher whenever it stops or hangs.
- **Startup shortcut** — a `pythonw run.py --supervise` shortcut in the Startup folder
  relaunches it at logon (no admin needed).
- **Keepalive task** — `install-keepalive.ps1` (run once **as admin**) registers a scheduled
  task that relaunches the supervisor *and* the enrichment worker within ~10 min if they're
  ever killed (e.g. by antivirus) and after resume-from-sleep.

Stop cleanly with `python run.py --stop` (finishes the current album, then exits).

## Notes / environment
- **Native tagging by default** (Mutagen) — Picard is optional and unused; the `picard.*`
  config only matters if you switch the engine back.
- **HTTPS interception** — if antivirus/proxy scans HTTPS, Python 3.13+ strict TLS rejects the
  injected CA. Set `musicbrainz.ssl_mode: relaxed` (verifies chain + host, tolerates the
  non-RFC-strict cert); `network_timeout` + retries stop a dropped connection from freezing
  the watcher.
- **Keep awake** — the supervisor holds `ES_SYSTEM_REQUIRED` (display may still sleep).
  Disabling OS **sleep + hibernate** is recommended — they can kill the daemon.
- **Disk guard** — imports pause (or stop) if the **library** drive drops below
  `space.min_free_gb`.

## Layout
```
picardwatch/
  config.yaml            run config (copied from config.example.yaml; git-ignored)
  requirements.txt
  run.py                 CLI: --once / --watch / --supervise / --folder / --status / --retag /
                         --enrich / --start-enrich / --stop-enrich / --catalogue [--push] /
                         --carplay [--analyze --verify --art --no-organize] / --stop
  keepalive.pyw          self-heal launcher (scheduled task revives the supervisor + enrich worker)
  install.ps1 / .bat     one-shot setup;  install-keepalive.ps1 = admin step to register the task
  picardwatch/
    config.py            yaml -> namespace
    models.py            Track, FileAnalysis, AlbumDecision
    state.py             SQLite decisions + cache + folder signature
    discovery.py         find album folders (flat OR nested Genre/Artist/Album; multi-disc)
    watcher.py           watchdog + folder-stability gate (multiple input roots)
    judge.py             THE "perfect album" decision (parallel fingerprinting)
    musicbrainz.py       rate-limited MB client (+cache) and release helpers
    importer.py          native import: fetch art, move, tag, cover.jpg, then cleanup
    tagger.py            write Plex-friendly tags + embed art (FLAC/MP3/MP4/Ogg)
    organizer.py         Artist/Album (Year) (Type)/[Disc N/] naming
    cleanup.py           remove emptied source folders + stale links after a move
    diskspace.py         pause/stop imports if the library drive runs low
    supervisor.py        keep run.py --watch alive (restart on exit/hang)
    control.py           cooperative stop flags;  power.py  keep the PC awake
    winutil.py           keep child processes (fpcalc/ffmpeg) windowless
    enrich.py            audiophile enrichment -> audiophile.json   (docs/ENRICHMENT.md)
    catalogue.py         Audio Vault collection.json                (docs/CATALOGUE.md)
    carplay.py           self-contained Car Audio/CarPlay playlists (docs/CARPLAY.md)
    retag.py             re-tag/re-organize the existing library
    plex.py              Plex section scan;  report.py  review report;  status.py  progress
```
