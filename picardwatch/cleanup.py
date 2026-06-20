"""Tidy up source locations after an album's audio has been moved out.

When an album imports, its audio files are moved into the Plex library and the leftover
source album folder is deleted. For nested input libraries (Genre/Artist/Album) that
leaves empty parent folders behind, so we also prune the now-empty parents up to — but
never including — the input root. Housekeeping junk (Thumbs.db, desktop.ini, ...) and
stale shortcuts / broken links never block a removal.

Safety: a folder is only removed when it holds nothing but junk/links/empty subdirs.
Anything with real files (audio, art, sidecars we kept) stops the prune immediately, and
input roots themselves are never deleted.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from . import discovery

log = logging.getLogger("picardwatch.cleanup")

# Files that should never keep a folder "alive" for cleanup purposes.
_JUNK_NAMES = {
    "thumbs.db", "desktop.ini", ".ds_store", "albumart.ini", "folder.ini",
    "albumartsmall.jpg", "zbthumbnail.info",
}
_JUNK_EXT = {".lnk", ".url", ".ini"}          # Windows / internet shortcuts, stray .ini


def _is_junk(p: Path) -> bool:
    """A file that doesn't count as real content (junk, a shortcut, or a dead link)."""
    try:
        if p.is_symlink() and not p.exists():
            return True                        # broken symlink / junction target gone
    except OSError:
        return True
    name = p.name.lower()
    return name in _JUNK_NAMES or p.suffix.lower() in _JUNK_EXT


def _effectively_empty(folder: Path) -> bool:
    """True if `folder` contains only junk, links, and (recursively) empty subfolders."""
    try:
        for child in folder.iterdir():
            if child.is_symlink():
                # a symlink/junction to real content elsewhere is NOT ours to delete
                if child.exists():
                    return False
                continue                       # dead link -> ignorable
            if child.is_dir():
                if not _effectively_empty(child):
                    return False
            elif not _is_junk(child):
                return False
        return True
    except OSError:
        return False


def remove_source_tree(folder: Path) -> bool:
    """Delete an imported source folder and any junk/links left in it. True if it's gone."""
    folder = Path(folder)
    if not folder.exists():
        return True
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def prune_empty_parents(start, input_root) -> int:
    """Walk up from `start`, removing empty/junk-only folders, stopping below `input_root`
    (which is never removed). Returns how many folders were pruned."""
    try:
        root = Path(input_root).resolve()
    except OSError:
        return 0
    removed = 0
    try:
        cur = Path(start).resolve().parent
    except OSError:
        return 0
    while cur != root and root in cur.parents:   # strictly inside the input root
        if not _effectively_empty(cur):
            break
        if not remove_source_tree(cur):
            break
        log.debug("Pruned empty source folder: %s", cur)
        removed += 1
        cur = cur.parent
    return removed


def cleanup_after_import(source_folder, cfg) -> int:
    """Remove the imported source folder and prune the empty parents above it (up to the
    matching input root). Returns the number of parent folders pruned."""
    root = _matching_input_root(source_folder, cfg)
    if root is None:
        return 0
    return prune_empty_parents(source_folder, root)


def sweep_empty_dirs(root) -> int:
    """One-shot bottom-up sweep: remove every empty/junk-only directory under `root`
    (but not `root` itself). For tidying leftover husks 'once done'."""
    root = Path(root)
    if not root.exists():
        return 0
    removed = 0
    for dirpath, dirnames, _ in os.walk(str(root), topdown=False):
        d = Path(dirpath)
        if d == root:
            continue
        if _effectively_empty(d) and remove_source_tree(d):
            removed += 1
    if removed:
        log.info("Cleanup: pruned %d empty folder(s) under %s", removed, root)
    return removed


def _matching_input_root(path, cfg):
    try:
        p = Path(path).resolve()
    except OSError:
        return None
    best = None
    for r in discovery.input_roots(cfg):
        try:
            rr = Path(r).resolve()
        except OSError:
            continue
        if rr == p or rr in p.parents:
            # prefer the deepest matching root (in case roots are nested)
            if best is None or len(str(rr)) > len(str(best)):
                best = rr
    return best
