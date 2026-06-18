# PicardWatch — Design

## Goal

Turn a folder of messily-named music downloads into a clean, **Plex-ready** library
automatically: scan an input folder, identify each album against MusicBrainz, and move
only *perfect* matches into `Artist/Album (Year) (Type)/…` with correct tags and cover
art. Leave anything uncertain untouched. Keep doing it for new downloads as they arrive.

## Requirements

**Functional**
- Scan an input folder; group files into album candidates (one album per subfolder).
- Identify albums even when tags are wrong/missing → acoustic fingerprinting + tags.
- Import only **perfect** albums (complete tracklist, high confidence, correct edition).
- Move + rename into a Plex layout; write full metadata; store cover art with the files.
- Multi-disc releases get `Disc N` subfolders; single-disc stay flat.
- Folder names carry the release **type**: `(Album)`, `(EP)`, `(Single)`, `(Compilation)`, …
- De-duplicate re-posted downloads.
- Remove the emptied source folder after a successful move.
- Watch the folder continuously and process new arrivals.

**Plex compatibility**
- `Artist/Album (Year)/Track` structure; embedded tags are authoritative for Plex.
- `Album Artist` for grouping; `Compilation` (TCMP) flag so Various-Artists albums group as one.
- `cover.jpg` per album folder; multi-disc grouped via Disc Number tags.
- Windows-illegal filename characters sanitised.

## Key design decisions

| Decision | Choice | Why |
|---|---|---|
| **Tagging engine** | **Native (Mutagen)** — *pivoted from MusicBrainz Picard* | The judge already resolves the exact MusicBrainz release, so PicardWatch can write tags/art/folders itself. This removed Picard's two problems on the target machine: a fragile GUI-config export step and Picard's own HTTPS lookups breaking behind the antivirus TLS interception. Picard remains available as an optional engine. |
| **Matching** | AcoustID fingerprint **+** tags | Robust to the wrong/missing tags common in scene releases; recording-MBID intersection gives precise file→track mapping, with duration+title fallback. |
| **Imperfect albums** | Leave in place, log to `review_report.html` | Keep the library pristine; whatever remains in the input folder afterwards is exactly the "needs a look" pile. |
| **Trigger** | Watch-folder daemon (`--watch`) | Its startup scan clears the backlog, then it watches for new SABnzbd drops — one process does both. |
| **"Perfect" gate** | coverage 1.0 + 0 orphans + exact track count | Strict, so a wrong match never silently mis-files an album. Worst case is "left in review," never "lost." |
| **De-dupe** | content-signature (pre-judge) + release-MBID (post-judge) | Identical re-posts are skipped without re-fingerprinting; different-encoding copies of an imported release are flagged, not duplicated. |
| **Cover art** | always `cover.jpg`; embedding opt-in | `cover.jpg` satisfies Plex; embedding into every hi-res FLAC forces a full-file rewrite (~minutes/album), so it's `importer.embed_art` (default off). |
| **Throughput** | parallel fingerprinting + early-exit + caching | A backlog of thousands of hi-res albums is otherwise multi-day; these cut per-album time and make the whole run resumable. |

## Environment specifics (target machine)

- **Windows 11**, Python 3.14, input/library on a mapped drive `Y:` (SABnzbd).
- **SABnzbd folder topology:** `Y:\SABNZDB\APPLE AUDIO` = completed music downloads (the **input**); `Y:\SABNZDB\TEMP OUTPUT` = the chosen library destination; `Y:\SABNZDB\TEMP MUSIC` = SABnzbd's *incomplete/working* dir (never use as input — it holds partial `SABnzbd_nzf_*` chunks).
- **HTTPS interception:** antivirus/proxy TLS scanning injects a CA cert that Python 3.13+ strict X.509 rejects (`Basic Constraints of CA cert not marked critical`). Handled by `musicbrainz.ssl_mode: relaxed`. The same interception can affect any HTTPS client; AcoustID is fine because pyacoustid uses HTTP.
- **Mapped drive + headless:** because tagging is native (no Picard GUI), the watcher can run headless, but it runs in the user's **logon session** so the `Y:` mapped drive is visible (a Session-0 service typically can't see per-user mapped drives). Hence a logon-triggered autostart task rather than a service.

## External dependencies & credentials

- **AcoustID application API key** (free) — the only secret the user must supply; lives in `config.yaml` (git-ignored).
- **MusicBrainz** — no authentication for read-only lookups; only a descriptive User-Agent (built from `musicbrainz.contact`) and the 1 req/s limit.
- **`fpcalc`** (Chromaprint) — downloaded into `bin/` by the installer.
- **Plex** (optional) — token + music section id to trigger a scan after import.

## Known limitations & future work

- **Backlog duration:** fresh hi-res albums take ~tens of seconds each; a multi-thousand-album backlog is a multi-day (resumable) run. Could be sped up with a tag-first fast path that skips fingerprinting when existing tags are trustworthy.
- **Quality-aware de-dupe:** current de-dupe keeps the first-imported copy of a release; it does not yet prefer the highest-bitrate duplicate.
- **Transient TLS drops:** under sustained load the AV occasionally resets an HTTPS connection, which can push an album to `review` on a network blip. A `--force` re-sweep of the review pile reclaims these.
- **Legacy imports:** albums imported before the `(Type)` / multi-disc-subfolder features use the older flat layout; a one-off normalize pass (read tags → MB type → rename/restructure in place) would reconcile them.
- **Persistence gap:** the logon autostart resumes the watcher after a reboot, but if the watcher process dies while the user stays logged in, nothing restarts it until the next logon (or a manual `start-watch.ps1`). A supervised service is possible but complicated by the mapped-drive requirement.

## Repository layout

```
run.py                 # entry point / CLI
install.ps1 / .bat     # one-shot installer (venv, deps, fpcalc, config, autostart)
start-watch.ps1        # convenience: run the watcher
config.example.yaml    # template → copy to config.yaml (git-ignored)
picard-batch.ini       # optional Picard-engine config
picardwatch/           # the package (see ARCHITECTURE.md for module responsibilities)
docs/                  # this design doc + ARCHITECTURE.md
```
