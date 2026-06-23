# Audiophile enrichment

Optional, **non-destructive** enrichment that writes a per-album `audiophile.json` sidecar
into the Plex library. Ported to Python (`picardwatch/enrich.py`) from the reference
`audio-enrich.js`. It **only reads audio and writes the sidecar** — it never moves, retags,
or deletes anything, takes no lock, and touches no state DB — so it is safe to run **while
the watcher is live**. Nothing runs unless you invoke `--enrich`.

## Run it

```bash
python run.py --enrich              # Tier 1 only (tags) — fast, no ffmpeg
python run.py --enrich --analyze    # + Tier 2 (ffmpeg: loudness / LRA / true-peak / waveform)
python run.py --enrich --limit 20   # only the first 20 albums (handy for a test run)
python run.py --enrich --force      # rebuild sidecars even if they already exist
```

- A sidecar is **skipped if up to date** (its mtime ≥ the newest track), so re-runs are cheap.
- **Tier 2 needs `ffmpeg` on PATH.** Without it, Tier 1 still works and Tier 2 fields stay `null`.
- Logs go to `logs/enrich.log` (separate from the watcher's `picardwatch.log`).
- Tune in `config.yaml` → `enrich:` (`sidecar` filename, `analyze_workers` for ffmpeg concurrency).

## Run it continuously (auto-restart) and stop it

For a long Tier-2 pass that survives an **antivirus kill**, use the *managed* commands
instead of a bare `--enrich`:

```bash
python run.py --start-enrich   # clear flags + launch the Tier-2 worker in the background
python run.py --stop-enrich    # finish the current album, stop, and stay stopped
```

- **Auto-restart:** the `PicardWatch-Keepalive` scheduled task (the same one that revives
  the watcher) relaunches the worker within ~10 min if it dies — **unless** it has finished
  or you stopped it. State is two flag files next to `state.sqlite3`:
  - `picardwatch-enrich.done` — written after a clean full pass → keepalive leaves it down.
  - `picardwatch-enrich.stop` — written by `--stop-enrich` → keepalive leaves it down until
    you `--start-enrich` again (which clears both flags).
- It holds `picardwatch-enrich.lock` while running, so the keepalive never starts a second copy.
- **Resumable:** a relaunch skips up-to-date sidecars, so an AV kill costs only the
  in-progress album. It also **stops itself** when the whole library is done (the `.done`
  marker). Re-running later picks up newly-imported albums (Tier-1 sidecars auto-upgrade to Tier-2).
- ffmpeg runs at **below-normal priority** with `analyze_workers: 1`, so it never starves the watcher.

## Tiers

| | Source | Cost |
|---|---|---|
| **Tier 1** | file tags via Mutagen | free, every run |
| **Tier 2** | ffmpeg `ebur128` + streamed PCM | slow, only with `--analyze` |

Tier 2 is memory-safe: the waveform **streams** ffmpeg's 2 kHz mono PCM and keeps only a
running peak per ~1000-sample frame (a few thousand ints), never the whole decoded track.

## `audiophile.json` schema

```jsonc
{
  "album": {
    "bpm": 0, "key": null, "composer": null,
    "label": "Elektra", "catalogNumber": "60376-2",
    "mbReleaseGroupId": "...", "mbArtistId": "...", "mbAlbumId": "...",
    "albumGain": null, "peak": null, "totalTracks": 10, "discs": 1, "compilation": false,
    "dr": 9, "drSource": "measured",        // measured (ffmpeg) | tag (DR-meter) | estimate (ReplayGain)
    "loudness": -13.6, "lra": 9.2, "truePeak": -1.8   // Tier 2 only (null without --analyze)
  },
  "tracks": [
    { "file": "01 - Without Warning.flac", "bpm": 0, "key": null,
      "isrc": "USEE19901147", "discNo": 1, "env": "0103101722..." }   // env: Tier 2 only
  ]
}
```

`dr` is always best-effort; `drSource` says how it was derived so a reader can de-emphasise
estimates: **measured** (true-peak − loudness) → **tag** (DR-meter tag) → **estimate** (ReplayGain).
