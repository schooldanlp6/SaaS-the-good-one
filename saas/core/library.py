"""Folder walker: every folder that directly contains tracks is a playlist.
Real playlists (DLP6/Playlists/*.toml on the stick) are appended after the
folders as virtual folders, so folder up/down reaches them too.

The folder list is built once when media appears (idle time). Track lists are
read lazily, the first time a folder is entered, so nothing hammers the shared
USB bus during playback.

Folder keys: real folders are paths relative to the root ("" = root itself);
playlists are "playlist:<name>".
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Dict, Iterable, List, Optional

from .playlists import PlaylistStore

log = logging.getLogger(__name__)

_DIGITS = re.compile(r"(\d+)")
_SKIP_DIRS = {"system volume information", "$recycle.bin", "lost+found"}
PLAYLIST_PREFIX = "playlist:"


def natural_key(name: str) -> list:
    """'2 b' < '10 a' (numbers compare as numbers, text case-insensitively)."""
    return [int(p) if p.isdigit() else p.casefold() for p in _DIGITS.split(name)]


def _path_key(rel: str) -> list:
    return [natural_key(part) for part in rel.split(os.sep)] if rel else [[""]]


class Library:
    is_local = True

    def __init__(self, root: str, extensions: Iterable[str],
                 playlists: Optional[PlaylistStore] = None) -> None:
        self.root = os.path.abspath(root)
        self.extensions = {e.lower().lstrip(".") for e in extensions}
        self.playlists = playlists
        self.folders: List[str] = []  # folder keys, see module docstring
        self.fingerprint = ""
        self._tracks: Dict[str, List[str]] = {}

    def is_track(self, name: str) -> bool:
        if name.startswith("."):  # hidden files, macOS "._foo.mp3" junk
            return False
        stem, dot, ext = name.rpartition(".")
        return bool(dot and stem) and ext.lower() in self.extensions

    # scanning -----------------------------------------------------------------
    def scan(self) -> List[str]:
        folders: List[str] = []
        stack = [""]
        while stack:
            rel = stack.pop()
            path = os.path.join(self.root, rel)
            has_tracks = False
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        name = entry.name
                        if name.startswith(".") or name.lower() in _SKIP_DIRS:
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(os.path.join(rel, name))
                            elif not has_tracks and self.is_track(name) and entry.is_file():
                                has_tracks = True
                        except OSError:
                            continue
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
                continue
            if has_tracks:
                folders.append(rel)
        folders.sort(key=_path_key)
        self.fingerprint = self._fingerprint(folders)  # real folders only
        self.folders = folders
        self._tracks.clear()
        self.refresh_playlists()
        log.info("library %s: %d folder(s), %d playlist(s)", self.root,
                 len(folders), sum(map(self.is_playlist, range(len(self.folders)))))
        return self.folders

    def refresh_playlists(self) -> None:
        """Re-read the playlist directory (after adding a track, a new playlist...)."""
        real = [k for k in self.folders if not k.startswith(PLAYLIST_PREFIX)]
        for key in list(self._tracks):
            if key.startswith(PLAYLIST_PREFIX):
                del self._tracks[key]
        keys = []
        if self.playlists is not None:
            for name in self.playlists.names():
                key = PLAYLIST_PREFIX + name
                if self.tracks_for_key(key):  # only playlists with playable tracks
                    keys.append(key)
        self.folders = real + keys

    # access -------------------------------------------------------------------
    def is_playlist(self, folder_index: int) -> bool:
        return self.folders[folder_index].startswith(PLAYLIST_PREFIX)

    def tracks(self, folder_index: int) -> List[str]:
        """Track ids: file names for folders, stick-relative paths for playlists."""
        return self.tracks_for_key(self.folders[folder_index])

    def tracks_for_key(self, key: str) -> List[str]:
        if key not in self._tracks:
            if key.startswith(PLAYLIST_PREFIX):
                names = self._playlist_tracks(key[len(PLAYLIST_PREFIX):])
            else:
                names = self._folder_tracks(key)
            self._tracks[key] = names
        return self._tracks[key]

    def _folder_tracks(self, rel: str) -> List[str]:
        path = os.path.join(self.root, rel)
        try:
            with os.scandir(path) as it:
                names = [e.name for e in it if self.is_track(e.name) and e.is_file()]
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            names = []
        names.sort(key=natural_key)
        return names

    def _playlist_tracks(self, name: str) -> List[str]:
        out = []
        for rel in self.playlists.load(name) if self.playlists else []:
            full = os.path.join(self.root, *rel.split("/"))
            if self.is_track(os.path.basename(rel)) and os.path.isfile(full):
                out.append(rel)
            else:
                log.info("playlist %s: skipping missing %s", name, rel)
        return out

    def track_path(self, folder_index: int, track_index: int) -> str:
        key = self.folders[folder_index]
        track = self.tracks(folder_index)[track_index]
        if key.startswith(PLAYLIST_PREFIX):
            return os.path.join(self.root, *track.split("/"))
        return os.path.join(self.root, key, track)

    def track_tags(self, folder_index: int, track_index: int):
        return None  # tags come from the file itself (mpv)

    def track_label(self, folder_index: int, track_index: int) -> str:
        return self.tracks(folder_index)[track_index].rsplit("/", 1)[-1]

    def folder_label(self, folder_index: int) -> str:
        key = self.folders[folder_index]
        if key.startswith(PLAYLIST_PREFIX):
            return "[PL] " + key[len(PLAYLIST_PREFIX):]
        return key or os.path.basename(self.root) or "/"

    def rel_to_root(self, full_path: str) -> str:
        return os.path.relpath(full_path, self.root).replace(os.sep, "/")

    @staticmethod
    def _fingerprint(folders: List[str]) -> str:
        """Cheap identity of the media, used to decide whether resume state applies."""
        h = hashlib.sha1(str(len(folders)).encode())
        for rel in folders[:64]:
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        return h.hexdigest()[:16]
