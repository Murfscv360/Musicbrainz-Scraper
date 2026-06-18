"""Drive MusicBrainz Picard in batch mode to tag + rename + MOVE one album.

Uses Picard's verified -e/--exec command interface (Picard 2.9+). Because Picard is
only ever invoked on an album the judge already declared PERFECT, SAVE_MATCHED is safe:
there is nothing partial to split.

Command sequence:
    LOAD <folder>          load the files
    [LOAD <release_mbid>]  optionally load the resolved release (skip a 2nd AcoustID round-trip)
    CLUSTER                group files into an album
    LOOKUP clustered       tag-based match
    [SCAN]                 fingerprint match (omitted when the MBID is pre-loaded)
    PAUSE <n>              let async matching settle
    SAVE_MATCHED           tag + rename + MOVE matched files into the library
    PAUSE <n>
    REMOVE_SAVED / REMOVE_EMPTY / WRITE_LOGS / QUIT
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("picardwatch.picard")


class PicardRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exe = cfg.picard.exe
        self.ini = cfg.picard.config_ini
        self.log_path = str(Path(cfg.paths.log_dir) / "picard-last.log")

    def build_args(self, folder: str | Path, release_mbid: str = "") -> list[str]:
        p = self.cfg.picard
        cmds: list[str] = [f"LOAD {folder}"]
        use_mbid = p.use_known_mbid and release_mbid
        if use_mbid:
            cmds.append(f"LOAD {release_mbid}")
        cmds += ["CLUSTER", "LOOKUP clustered"]
        if not use_mbid:
            cmds.append("SCAN")  # fingerprint match only needed when we didn't pre-load the release
        cmds += [
            f"PAUSE {p.pause_after_lookup}",
            "SAVE_MATCHED",
            f"PAUSE {p.pause_after_save}",
            "REMOVE_SAVED",
            "REMOVE_EMPTY",
            f"WRITE_LOGS {self.log_path}",
            "QUIT",
        ]
        args = [self.exe, "-s", "-N", "-M", "-c", self.ini]
        for c in cmds:
            args += ["-e", c]
        return args

    def run(self, folder: str | Path, release_mbid: str = "") -> bool:
        args = self.build_args(folder, release_mbid)
        log.debug("Picard: %s", " ".join(f'"{a}"' if " " in a else a for a in args))
        try:
            proc = subprocess.run(
                args, timeout=self.cfg.picard.timeout_sec,
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            log.error("Picard executable not found: %s", self.exe)
            return False
        except subprocess.TimeoutExpired:
            log.error("Picard timed out after %ss on %s", self.cfg.picard.timeout_sec, folder)
            return False
        if proc.returncode != 0:
            log.error("Picard exited %s: %s", proc.returncode, (proc.stderr or "").strip()[:500])
            return False
        return True
