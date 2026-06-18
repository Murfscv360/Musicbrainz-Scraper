"""Write Plex-friendly tags and embed cover art into an audio file (format-agnostic).

Plex reads music metadata primarily from embedded tags, so we write the canonical
set per format: Album Artist (grouping), Artist, Album, Title, Track#/Total,
Disc#/Total, Year, Compilation flag (for Various-Artists comps), MusicBrainz IDs,
and an embedded front cover.

`track` is a dict: {title, artist, position, disc, recording_id}
`album` is a dict: {album, albumartist, year, date, originaldate, mbid,
                    release_group_id, is_compilation, total_tracks, total_discs}
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, TALB, TCMP, TCON, TDRC, TIT2, TPE1, TPE2,
                         TPOS, TRCK, TXXX)
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import coverart

log = logging.getLogger("picardwatch.tagger")


def tag_file(path: str, track: dict, album: dict, cover: Optional[bytes]) -> None:
    ext = Path(path).suffix.lower()
    if ext == ".flac":
        _flac(path, track, album, cover)
    elif ext == ".mp3":
        _mp3(path, track, album, cover)
    elif ext in (".m4a", ".mp4", ".m4b", ".alac"):
        _mp4(path, track, album, cover)
    elif ext in (".ogg", ".oga"):
        _vorbis(OggVorbis(path), track, album, cover)
    elif ext == ".opus":
        _vorbis(OggOpus(path), track, album, cover)
    else:
        _easy(path, track, album)


def _flac(path, t, a, cover):
    f = FLAC(path)
    f["title"] = t["title"]
    f["artist"] = t["artist"]
    f["albumartist"] = a["albumartist"]
    f["album"] = a["album"]
    f["tracknumber"] = str(t["position"])
    f["totaltracks"] = f["tracktotal"] = str(a["total_tracks"])
    f["discnumber"] = str(t["disc"])
    f["totaldiscs"] = f["disctotal"] = str(a["total_discs"])
    if a.get("date"):
        f["date"] = a["date"]
    if a.get("originaldate"):
        f["originaldate"] = a["originaldate"]
    if a.get("is_compilation"):
        f["compilation"] = "1"
    if a.get("mbid"):
        f["musicbrainz_albumid"] = a["mbid"]
    if a.get("release_group_id"):
        f["musicbrainz_releasegroupid"] = a["release_group_id"]
    if t.get("recording_id"):
        f["musicbrainz_trackid"] = t["recording_id"]
    if a.get("type"):
        f["releasetype"] = a["type"]
    if cover:
        f.clear_pictures()
        f.add_picture(_flac_picture(cover))
    f.save()


def _flac_picture(cover: bytes) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = coverart.mime(cover)
    pic.desc = "front"
    pic.data = cover
    return pic


def _mp3(path, t, a, cover):
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    id3.setall("TIT2", [TIT2(encoding=3, text=t["title"])])
    id3.setall("TPE1", [TPE1(encoding=3, text=t["artist"])])
    id3.setall("TPE2", [TPE2(encoding=3, text=a["albumartist"])])
    id3.setall("TALB", [TALB(encoding=3, text=a["album"])])
    id3.setall("TRCK", [TRCK(encoding=3, text=f'{t["position"]}/{a["total_tracks"]}')])
    id3.setall("TPOS", [TPOS(encoding=3, text=f'{t["disc"]}/{a["total_discs"]}')])
    if a.get("year"):
        id3.setall("TDRC", [TDRC(encoding=3, text=a["year"])])
    if a.get("is_compilation"):
        id3.setall("TCMP", [TCMP(encoding=3, text="1")])
    if a.get("mbid"):
        id3.delall("TXXX:MusicBrainz Album Id")
        id3.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=a["mbid"]))
    if cover:
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=coverart.mime(cover), type=3, desc="front", data=cover))
    id3.save(path, v2_version=3)  # ID3v2.3 = broadest Plex compatibility


def _mp4(path, t, a, cover):
    m = MP4(path)
    m["\xa9nam"] = [t["title"]]
    m["\xa9ART"] = [t["artist"]]
    m["aART"] = [a["albumartist"]]
    m["\xa9alb"] = [a["album"]]
    m["trkn"] = [(int(t["position"]), int(a["total_tracks"]))]
    m["disk"] = [(int(t["disc"]), int(a["total_discs"]))]
    if a.get("year"):
        m["\xa9day"] = [a["year"]]
    m["cpil"] = bool(a.get("is_compilation"))
    if a.get("mbid"):
        m["----:com.apple.iTunes:MusicBrainz Album Id"] = [a["mbid"].encode()]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if coverart.png(cover) else MP4Cover.FORMAT_JPEG
        m["covr"] = [MP4Cover(cover, imageformat=fmt)]
    m.save()


def _vorbis(audio, t, a, cover):
    audio["title"] = t["title"]
    audio["artist"] = t["artist"]
    audio["albumartist"] = a["albumartist"]
    audio["album"] = a["album"]
    audio["tracknumber"] = str(t["position"])
    audio["tracktotal"] = str(a["total_tracks"])
    audio["discnumber"] = str(t["disc"])
    audio["disctotal"] = str(a["total_discs"])
    if a.get("date"):
        audio["date"] = a["date"]
    if a.get("is_compilation"):
        audio["compilation"] = "1"
    if a.get("mbid"):
        audio["musicbrainz_albumid"] = a["mbid"]
    if a.get("type"):
        audio["releasetype"] = a["type"]
    if cover:
        audio["metadata_block_picture"] = [
            base64.b64encode(_flac_picture(cover).write()).decode("ascii")
        ]
    audio.save()


def _easy(path, t, a):
    """Last-resort text-only tagging for unrecognised formats."""
    mf = mutagen.File(path, easy=True)
    if mf is None:
        log.warning("Cannot tag (unknown format): %s", path)
        return
    mf["title"] = t["title"]
    mf["artist"] = t["artist"]
    mf["album"] = a["album"]
    mf["albumartist"] = a["albumartist"]
    mf["tracknumber"] = str(t["position"])
    mf.save()
