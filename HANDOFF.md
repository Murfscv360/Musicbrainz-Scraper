# PicardWatch — Handoff

_Last updated: 2026-06-18_

Automated, Plex-ready music importer for Windows. It watches a folder of (messily-named)
music downloads, identifies complete, high-confidence albums via **AcoustID fingerprints +
MusicBrainz**, then natively tags + organizes the perfect matches into a Plex library with
rich metadata. Imperfect albums are left in place; duplicates are skipped.

- **Repo:** https://github.com/Murfscv360/Musicbrainz-Scraper (branch `main`)
- **Architecture / design:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/DESIGN.md](docs/DESIGN.md)
- **Runtime config:** `config.yaml` (git-ignored — holds the AcoustID key; template is `config.example.yaml`)

## Status snapshot (2026-06-18)

| | |
|---|---|
| Watcher | **running** (and set to auto-start at logon) |
| Imported | ~76 albums (~778 tracks) |
| In review | ~38 |
| Duplicates skipped | ~76 |
| Failed | 0 |
| Processed | ~190 of ~4,531 |
| Pace / ETA | ~0.4 albums/min → **multi-day** (≈ a week) for the full backlog |

> Numbers above are a point-in-time snapshot. **For live progress run `.\status.ps1`.**

## Deployment on this machine

- **Input (watched):** `Y:\SABNZDB\APPLE AUDIO` — completed SABnzbd music downloads.
- **Library (destination):** `Y:\SABNZDB\TEMP OUTPUT` — Point Plex here (or move to a final library later).
- **Engine:** native tagging (Mutagen). Picard is optional and not required.
- **Autostart:** a Startup-folder shortcut `…\Startup\PicardWatch.lnk` runs `run.py --watch` at every logon (no admin). Delete that shortcut to disable.
- **Key settings (`config.yaml`):** `importer.dedupe: true`, `importer.delete_source: true`, `importer.embed_art: false` (cover.jpg is always saved; embedding is the slow-on-hi-res option), `musicbrainz.ssl_mode: relaxed`.

## How to operate it

Run from the project folder (`C:\Users\murfs\Documents\Claude Projects\picardwatch`):

| Command | What it does |
|---|---|
| `.\status.ps1` | Progress snapshot (albums/tracks moved, review/dup counts, ETA). Safe while running. |
| `.\start-watch.ps1` | Start the watcher (backlog + ongoing). Already auto-starts at logon. |
| `.\install.ps1` | Re-provision (venv/deps/fpcalc), enable autostart, launch the scanner. Prompt-free when configured. |
| `python run.py --once [--limit N] [--dry-run]` | One-shot scan of the input folder (optionally first N / preview only). |
| `python run.py --folder "PATH" [--dry-run] [--force]` | Process a single album folder. |
| `python run.py --retag [--dry-run]` | Re-tag + re-organize the **existing library** to the current standard. **Stop the watcher first** (it takes the single-instance lock). |
| `--dry-run` | Judge + report only; never moves/edits files. |
| `--force` | Re-judge a folder even if it was decided before. |

**Stop the watcher** (e.g. before `--retag`):
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*run.py*--watch*' } | Stop-Process -Force
```

## Output layout (what Plex sees)

```
Y:\SABNZDB\TEMP OUTPUT\<Album Artist>\<Album> (Year) (Type)\
    01 - Title.flac                 # single disc: flat
    cover.jpg
…\<Album> (Year) (Type)\Disc 1\01 - Title.flac   # multi-disc: one subfolder per disc
```
`(Type)` = MusicBrainz release type (Album / EP / Single / Compilation / Live / …). Embedded
tags include: album/artist/album-artist + sort names, track/disc, dates, **genre, label,
catalog #, barcode, country, ASIN, media format**, MusicBrainz artist/album/release-group/track
IDs, ISRC, release type, compilation flag — plus cover art. A single `album_metadata()` builder
feeds both the live importer and `--retag`, so tags + naming are identical however an album enters.

## Known issues & caveats

- **Multi-day backlog.** ~4,500 hi-res albums at MusicBrainz's 1 req/sec + fingerprinting ≈ a week. **Not yet built:** a *tag-first fast path* (skip fingerprinting when existing tags already match a release) — the main available speed-up.
- **Transient HTTPS drops.** This PC's antivirus/proxy intercepts HTTPS; under load it occasionally resets a connection (`WinError 10054`), which can push an album to `review` on a blip. Safe (left in place). A re-sweep would reclaim these — **note:** there's no dedicated "review re-sweep" mode yet; `--once --force` re-judges everything (slow). Possible future `--review-resweep`.
- **`--status` on the busy network drive.** Listing the input folder over SMB while the drive is busy is slow; the input-folder scan is now **time-boxed** (returns in a few seconds; shows "n/a (drive busy)" for remaining/ETA when it can't finish). Run when the drive is idle for an exact ETA.
- **Plex auto-scan is off** (`plex.enabled: false`). Set `plex.host/token/music_section_id` and `enabled: true` to trigger a Plex scan after each import.
- **Mapped-drive constraint.** `Y:` is a per-user mapped drive, so the watcher must run in the logon session (hence the Startup shortcut, not a Session-0 service).

## Open items / next steps

1. (Optional) **Tag-first fast path** to cut the backlog time substantially.
2. (Optional) **Review re-sweep** tool to reclaim albums that failed only on a transient network error.
3. (Optional) **Enable Plex auto-scan** (token + section id).
4. Decide the final library location (currently `TEMP OUTPUT`); point Plex at it or relocate.

## Resuming / accessing

- **The code:** browse anywhere via the GitHub repo (incl. the GitHub mobile app).
- **This chat session on mobile:** Claude Code **Remote Control** — run `/remote-control` in the terminal, then open the session from the Claude mobile app or `claude.ai/code`. (The session stays on this PC; keep it running.)
- **After a reboot:** the watcher relaunches itself via the logon Startup shortcut and **resumes** (state in `state.sqlite3` is the source of truth: imported folders are gone, reviewed/duplicate ones are skipped).

## Repo map

```
run.py                  CLI entry (--watch/--once/--folder/--status/--retag, --dry-run/--force/--limit)
install.ps1 / .bat      one-shot installer (venv, deps, fpcalc, config, autostart, launch)
start-watch.ps1         start the watcher
status.ps1              progress snapshot
config.example.yaml     config template (copy to config.yaml)
picard-batch.ini        optional Picard-engine config
picardwatch/            package — see docs/ARCHITECTURE.md for module responsibilities:
  config, state, musicbrainz, judge, coverart, tagger, organizer, importer,
  watcher, report, status, retag  (+ verifier, picard_runner for the optional Picard engine)
docs/                   ARCHITECTURE.md, DESIGN.md
```
