"""Fetch front cover art from the Cover Art Archive by MusicBrainz release MBID.

Uses urllib (not requests) on purpose: musicbrainz.py installs a relaxed default
HTTPS context (see install_ssl_mode), so urllib works behind this machine's HTTPS
interception, whereas requests+certifi would not. CAA 307-redirects to archive.org;
urllib follows redirects automatically.
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Optional

log = logging.getLogger("picardwatch.coverart")


def png(data: bytes) -> bool:
    return bool(data) and data[:8] == b"\x89PNG\r\n\x1a\n"


def cover_filename(data: bytes) -> str:
    return "cover.png" if png(data) else "cover.jpg"


def mime(data: bytes) -> str:
    return "image/png" if png(data) else "image/jpeg"


def fetch_front(release_mbid: str, contact: str = "") -> Optional[bytes]:
    """Return front-cover image bytes for a release, or None if unavailable."""
    if not release_mbid:
        return None
    ua = f"PicardWatch/0.1 ( {contact or 'https://musicbrainz.org'} )"
    url = f"https://coverartarchive.org/release/{release_mbid}/front"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=8) as resp:   # short: a flaky CAA/SSL
            data = resp.read()                                  # handshake mustn't stall the import
        return data or None
    except Exception as exc:  # 404 (no art) is common and fine
        log.info("No Cover Art Archive front image for %s (%s)", release_mbid, exc)
        return None
