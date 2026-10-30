"""What a network server looks like to the player - the same interface as the
stick Library (saas/core/library.py), minus playlists (those live on sticks)."""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

Track = Dict[str, object]   # {"id", "title", "artist", "album", "duration", "url"}


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip() or "server"


class NetLibrary:
    kind = "net"
    is_local = False
    playlists = None            # save/like only work on sticks

    def __init__(self, name: str, identity: str) -> None:
        self.name = safe_name(name)
        self.root = f"{self.kind}://{self.name}"     # the medium's key
        self.fingerprint = hashlib.sha1(f"{self.kind}|{identity}".encode()).hexdigest()[:16]
        self.folders: List[str] = []
        self._labels: Dict[str, str] = {}
        self._tracks: Dict[str, List[Track]] = {}

    # to implement -------------------------------------------------------------
    def ping(self) -> None:
        """Raise NetError if the server is gone."""
        raise NotImplementedError

    def list_folders(self) -> List[Tuple[str, str]]:
        """[(key, label)] in display order."""
        raise NotImplementedError

    def list_tracks(self, key: str) -> List[Track]:
        raise NotImplementedError

    # the Library interface the player uses ---------------------------------------
    def scan(self) -> List[str]:
        folders = self.list_folders()
        self.folders = [k for k, _ in folders]
        self._labels = dict(folders)
        log.info("%s: %d folder(s)", self.root, len(self.folders))
        return self.folders

    def refresh_playlists(self) -> None:
        pass

    def is_playlist(self, folder_index: int) -> bool:
        return False

    def tracks_for_key(self, key: str) -> List[Track]:
        if key not in self._tracks:
            try:
                self._tracks[key] = [t for t in self.list_tracks(key) if t.get("url")]
            except Exception as exc:  # network trouble: an empty folder, not a crash
                log.warning("%s: cannot list %s: %s", self.root, self._labels.get(key, key), exc)
                return []
        return self._tracks[key]

    def tracks(self, folder_index: int) -> List[str]:
        return [str(t["id"]) for t in self.tracks_for_key(self.folders[folder_index])]

    def _track(self, folder_index: int, track_index: int) -> Track:
        return self.tracks_for_key(self.folders[folder_index])[track_index]

    def track_path(self, folder_index: int, track_index: int) -> str:
        return str(self._track(folder_index, track_index)["url"])

    def track_label(self, folder_index: int, track_index: int) -> str:
        return str(self._track(folder_index, track_index).get("title") or "?")

    def track_tags(self, folder_index: int, track_index: int) -> Optional[dict]:
        t = self._track(folder_index, track_index)
        return {k: t.get(k) for k in ("title", "artist", "album", "duration")}

    def folder_label(self, folder_index: int) -> str:
        return self._labels.get(self.folders[folder_index], "?")

    def rel_to_root(self, path: str) -> str:
        return path
