"""Write Plex-friendly tags and embed cover art into an audio file (format-agnostic).

We write a rich tag set so Plex (and other tools) have as much to work with as possible:
artist/album/title, album-artist, track/disc, dates, genre, label + catalog number,
barcode, country, ASIN, media format, sort names, MusicBrainz artist/album/track ids,
ISRC, release type, the compilation flag — plus the front cover.

`track`: {title, artist, artist_sort, artist_mbid, isrc, position, disc, recording_id}
`album`: {album, albumartist, albumartist_sort, albumartist_mbid, year, date, originaldate,
          originalyear, type, genre, label, catalognumber, barcode, country, asin, media,
          mbid, release_group_id, is_compilation, total_tracks, total_discs}
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TCMP, TCON, TDOR, TDRC,
                         TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TSO2, TSOP, TSRC, TXXX)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
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


def _flac_picture(cover: bytes) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = coverart.mime(cover)
    pic.desc = "front"
    pic.data = cover
    return pic


def _vorbis_fields(t: dict, a: dict) -> dict:
    """The vorbis-comment tag map shared by FLAC, Ogg Vorbis and Opus."""
    fields = {
        "title": t["title"],
        "artist": t["artist"],
        "albumartist": a["albumartist"],
        "album": a["album"],
        "tracknumber": str(t["position"]),
        "tracktotal": str(a["total_tracks"]),
        "totaltracks": str(a["total_tracks"]),
        "discnumber": str(t["disc"]),
        "disctotal": str(a["total_discs"]),
        "totaldiscs": str(a["total_discs"]),
    }
    optional = {
        "date": a.get("date"),
        "originaldate": a.get("originaldate"),
        "originalyear": a.get("originalyear"),
        "year": a.get("year"),
        "genre": a.get("genre"),
        "label": a.get("label"),
        "catalognumber": a.get("catalognumber"),
        "barcode": a.get("barcode"),
        "releasecountry": a.get("country"),
        "asin": a.get("asin"),
        "media": a.get("media"),
        "releasetype": a.get("type"),
        "albumartistsort": a.get("albumartist_sort"),
        "artistsort": t.get("artist_sort"),
        "isrc": t.get("isrc"),
        "musicbrainz_albumid": a.get("mbid"),
        "musicbrainz_releasegroupid": a.get("release_group_id"),
        "musicbrainz_albumartistid": a.get("albumartist_mbid"),
        "musicbrainz_artistid": t.get("artist_mbid"),
        "musicbrainz_trackid": t.get("recording_id"),
    }
    for k, v in optional.items():
        if v:
            fields[k] = str(v)
    if a.get("is_compilation"):
        fields["compilation"] = "1"
    return fields


def _flac(path, t, a, cover):
    audio = FLAC(path)
    for k, v in _vorbis_fields(t, a).items():
        audio[k] = v
    if cover:
        audio.clear_pictures()
        audio.add_picture(_flac_picture(cover))
    audio.save()


def _vorbis(audio, t, a, cover):
    for k, v in _vorbis_fields(t, a).items():
        audio[k] = v
    if cover:
        audio["metadata_block_picture"] = [base64.b64encode(_flac_picture(cover).write()).decode("ascii")]
    audio.save()


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
    if a.get("originalyear"):
        id3.setall("TDOR", [TDOR(encoding=3, text=a["originalyear"])])
    if a.get("genre"):
        id3.setall("TCON", [TCON(encoding=3, text=a["genre"])])
    if a.get("label"):
        id3.setall("TPUB", [TPUB(encoding=3, text=a["label"])])
    if a.get("albumartist_sort"):
        id3.setall("TSO2", [TSO2(encoding=3, text=a["albumartist_sort"])])
    if t.get("artist_sort"):
        id3.setall("TSOP", [TSOP(encoding=3, text=t["artist_sort"])])
    if t.get("isrc"):
        id3.setall("TSRC", [TSRC(encoding=3, text=t["isrc"])])
    if a.get("is_compilation"):
        id3.setall("TCMP", [TCMP(encoding=3, text="1")])
    for desc, val in (("MusicBrainz Album Id", a.get("mbid")),
                      ("MusicBrainz Release Group Id", a.get("release_group_id")),
                      ("MusicBrainz Album Artist Id", a.get("albumartist_mbid")),
                      ("MusicBrainz Artist Id", t.get("artist_mbid")),
                      ("MusicBrainz Album Type", a.get("type")),
                      ("CATALOGNUMBER", a.get("catalognumber")),
                      ("BARCODE", a.get("barcode")),
                      ("ASIN", a.get("asin"))):
        if val:
            id3.delall(f"TXXX:{desc}")
            id3.add(TXXX(encoding=3, desc=desc, text=val))
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
    if a.get("genre"):
        m["\xa9gen"] = [a["genre"]]
    if a.get("albumartist_sort"):
        m["soaa"] = [a["albumartist_sort"]]
    if t.get("artist_sort"):
        m["soar"] = [t["artist_sort"]]
    m["cpil"] = bool(a.get("is_compilation"))

    def freeform(name, val):
        if val:
            m[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(str(val).encode("utf-8"))]

    freeform("MusicBrainz Album Id", a.get("mbid"))
    freeform("MusicBrainz Album Artist Id", a.get("albumartist_mbid"))
    freeform("MusicBrainz Artist Id", t.get("artist_mbid"))
    freeform("LABEL", a.get("label"))
    freeform("CATALOGNUMBER", a.get("catalognumber"))
    freeform("ISRC", t.get("isrc"))
    if cover:
        fmt = MP4Cover.FORMAT_PNG if coverart.png(cover) else MP4Cover.FORMAT_JPEG
        m["covr"] = [MP4Cover(cover, imageformat=fmt)]
    m.save()


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
