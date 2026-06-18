"""Rate-limited MusicBrainz client with an on-disk response cache.

MusicBrainz enforces ~1 request/second for anonymous clients and REQUIRES a
descriptive User-Agent. musicbrainzngs handles the throttling; the cache keeps us
from re-fetching the same release on every scan.
"""

from __future__ import annotations

import logging
import ssl
from typing import Optional

import musicbrainzngs

from .models import Track
from .state import State

log = logging.getLogger("picardwatch.mb")

# Rich metadata for Plex. 'genres' may be unsupported on older musicbrainzngs, so we
# fall back to 'tags'.
_INCLUDES = ["recordings", "media", "artists", "artist-credits", "labels",
             "release-groups", "isrcs", "genres"]
_INCLUDES_FALLBACK = ["recordings", "media", "artists", "artist-credits", "labels",
                      "release-groups", "isrcs", "tags"]


def install_ssl_mode(mode: str) -> None:
    """musicbrainzngs uses stdlib urllib, which verifies against the Windows trust
    store. Python 3.13+ enables strict X.509 checks by default, which reject the
    non-RFC-strict CA certs that antivirus/proxy HTTPS-scanning injects. Relax just
    that check (still verifying chain + hostname) so MB stays reachable on such PCs.

      strict  -> Python 3.13+ default (rejects intercepted / non-strict chains)
      relaxed -> verify chain + hostname, but clear VERIFY_X509_STRICT (default)
      none    -> no verification at all (last resort)
    """
    mode = (mode or "relaxed").lower()
    if mode == "strict":
        return

    def _factory(*args, **kwargs):
        if mode == "none":
            return ssl._create_unverified_context()
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    ssl._create_default_https_context = _factory
    if mode == "none":
        log.warning("musicbrainz.ssl_mode=none -> TLS verification DISABLED for urllib")


class MBClient:
    def __init__(self, cfg, state: State):
        install_ssl_mode(getattr(cfg.musicbrainz, "ssl_mode", "relaxed"))
        musicbrainzngs.set_useragent(
            cfg.musicbrainz.app_name, cfg.musicbrainz.app_version, cfg.musicbrainz.contact
        )
        musicbrainzngs.set_rate_limit(1.0, 1)  # 1 request/second
        self.state = state

    def get_release(self, mbid: str) -> Optional[dict]:
        if not mbid:
            return None
        key = f"mb:release:v2:{mbid}"   # v2 = richer includes (labels/genres/isrcs/...)
        cached = self.state.cache_get(key)
        if cached is not None:
            return cached
        rel = None
        for includes in (_INCLUDES, _INCLUDES_FALLBACK):
            try:
                data = musicbrainzngs.get_release_by_id(mbid, includes=includes)
                rel = data.get("release")
                break
            except musicbrainzngs.WebServiceError as exc:
                log.warning("MB get_release(%s) failed: %s", mbid, exc)
                return None
            except Exception as exc:  # e.g. InvalidIncludeError -> retry with the fallback set
                log.debug("MB include set rejected (%s); trying fallback", exc)
                continue
        if rel is not None:
            self.state.cache_set(key, rel)
        return rel

    def search_releases(self, artist: str, album: str, tracks: Optional[int] = None,
                        limit: int = 5) -> list[str]:
        """Tag-based fallback: return candidate release MBIDs from a text search."""
        if not (artist and album):
            return []
        key = f"mb:search:{artist}|{album}|{tracks}|{limit}".lower()
        cached = self.state.cache_get(key)
        if cached is not None:
            return cached
        kwargs = {"artist": artist, "release": album, "limit": limit}
        if tracks:
            kwargs["tracks"] = tracks
        try:
            res = musicbrainzngs.search_releases(**kwargs)
        except musicbrainzngs.WebServiceError as exc:
            log.warning("MB search_releases failed: %s", exc)
            return []
        ids = [r["id"] for r in res.get("release-list", [])]
        self.state.cache_set(key, ids)
        return ids


def release_tracks(rel: dict) -> list[Track]:
    """Flatten a MB release dict into an ordered list of Track objects."""
    tracks: list[Track] = []
    for medium in rel.get("medium-list", []):
        try:
            disc = int(medium.get("position", 1))
        except (TypeError, ValueError):
            disc = 1
        for t in medium.get("track-list", []):
            rec = t.get("recording", {})
            length = t.get("length") or rec.get("length")
            try:
                pos = int(t.get("position", 0))
            except (TypeError, ValueError):
                pos = 0
            a_mbid, a_sort = _credit_artist(t)
            if not a_mbid:
                a_mbid, a_sort = _credit_artist(rec)
            isrcs = rec.get("isrc-list") or []
            isrc = ""
            if isrcs:
                first = isrcs[0]
                isrc = first if isinstance(first, str) else first.get("id", "")
            tracks.append(
                Track(
                    position=pos,
                    disc=disc,
                    recording_id=rec.get("id", ""),
                    title=rec.get("title", t.get("title", "")),
                    length_ms=int(length) if length else None,
                    artist=(t.get("artist-credit-phrase") or rec.get("artist-credit-phrase") or ""),
                    artist_sort=a_sort,
                    artist_mbid=a_mbid,
                    isrc=isrc,
                )
            )
    return tracks


def release_artist(rel: dict) -> str:
    return rel.get("artist-credit-phrase") or "Unknown Artist"


def release_year(rel: dict) -> str:
    date = rel.get("date") or ""
    return date[:4]


def release_type(rel: dict) -> str:
    """A single human label for the release: a notable secondary type (Compilation,
    Soundtrack, Live, ...) wins, otherwise the primary type (Album / EP / Single)."""
    rg = rel.get("release-group", {}) or {}
    secondary = rg.get("secondary-type-list") or []
    for t in ("Compilation", "Soundtrack", "Live", "Remix", "Mixtape/Street", "Demo"):
        if t in secondary:
            return t
    return rg.get("primary-type") or rg.get("type") or "Album"


def _credit_artist(entity: dict):
    """(artist_mbid, artist_sort_name) for the first credited artist of an entity."""
    for part in (entity.get("artist-credit") or []):
        if isinstance(part, dict) and part.get("artist"):
            a = part["artist"]
            return a.get("id", ""), a.get("sort-name", "")
    return "", ""


def album_artist(rel: dict):
    """(album_artist_mbid, album_artist_sort_name) for the release."""
    return _credit_artist(rel)


def release_genre(rel: dict) -> str:
    """Most-voted genre (release-group preferred), else a folksonomy tag."""
    def top(items):
        if not items:
            return ""
        best = max(items, key=lambda g: int(g.get("count", 0) or 0))
        return best.get("name", "")
    rg = rel.get("release-group", {}) or {}
    for src in (rg.get("genre-list"), rel.get("genre-list"), rg.get("tag-list"), rel.get("tag-list")):
        name = top(src)
        if name:
            return name.title()
    return ""


def release_label_catalog(rel: dict):
    """(label name, catalog number) from the first label-info entry."""
    for info in (rel.get("label-info-list") or []):
        label = (info.get("label") or {}).get("name", "")
        catno = info.get("catalog-number", "")
        if label or catno:
            return label, catno
    return "", ""


def release_media(rel: dict) -> str:
    """Format of the release medium (CD / Digital Media / Vinyl / ...)."""
    for medium in (rel.get("medium-list") or []):
        fmt = medium.get("format")
        if fmt:
            return fmt
    return ""
