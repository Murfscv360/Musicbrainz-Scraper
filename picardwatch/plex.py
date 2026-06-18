"""Tell Plex to scan after a successful import.

Does a refresh of the configured music section. A full section refresh is simplest
and reliable; Plex only ingests new/changed files. (A *targeted* path scan is possible
via &path=<dir> but requires replicating Picard's naming script to know the exact moved
folder — left as an enhancement.)
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("picardwatch.plex")


def scan(plex_cfg) -> bool:
    base = plex_cfg.host.rstrip("/")
    url = f"{base}/library/sections/{plex_cfg.music_section_id}/refresh"
    try:
        r = requests.get(url, params={"X-Plex-Token": plex_cfg.token}, timeout=10)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Plex scan request failed: %s", exc)
        return False
    log.info("Triggered Plex scan of section %s", plex_cfg.music_section_id)
    return True
