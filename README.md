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
```
Open `review_report.html` to see what was left behind and why. Fix a folder's contents
(e.g. add the missing track) and it's **re-evaluated automatically** next scan — the SQLite
state keys off a content signature, not just the path.

## Autostart on Windows login (important)
Picard is a GUI app with **no true headless mode**, so PicardWatch must run in your
**interactive desktop session**, not as a Session-0 service. Use a **logon-triggered**
Task Scheduler task (not "run whether logged on or not"):
- Trigger: *At log on* (your user)
- Action: `…\.venv\Scripts\pythonw.exe`  argument `run.py --watch`
- Start in: the project folder

## Known caveats (by design, see the chat design doc)
- **GUI session required** — see autostart note above.
- **Async timing** — Picard's lookups are asynchronous; `picard.pause_after_lookup`
  in `config.yaml` is the wait before `SAVE_MATCHED`. Increase it for big albums / slow
  networks. Post-move verification is the real safety net.
- **Double fingerprinting** — the judge fingerprints to decide; Picard fingerprints again
  to tag. Keep `picard.use_known_mbid: true` so Picard loads the resolved release MBID and
  skips its own `SCAN`.

## Layout
```
picardwatch/
  config.yaml            run config
  picard-batch.ini       dedicated Picard config (move/rename/Plex naming)
  requirements.txt
  run.py                 CLI: --once / --watch / --folder, --dry-run
  picardwatch/
    config.py            yaml -> namespace
    models.py            Track, FileAnalysis, AlbumDecision
    state.py             SQLite decisions + cache + folder signature
    musicbrainz.py       rate-limited MB client (+cache) and release helpers
    judge.py             THE "perfect album" decision
    picard_runner.py     builds + runs the Picard -e command sequence
    verifier.py          confirms the move happened
    plex.py              triggers a Plex section scan
    watcher.py           watchdog + folder-stability gate
    report.py            review_report.html
```
