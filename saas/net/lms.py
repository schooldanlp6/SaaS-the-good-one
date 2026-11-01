"""Lyrion Music Server (LMS, formerly Logitech Media Server).

Talks to LMS's JSON-RPC interface (POST JSON to <url>/jsonrpc.js - that's
only the URL's name, there's no JavaScript involved) and plays the original
files from <url>/music/<track id>/download, so the Pi decodes them like files
from a stick. No Squeezebox player is involved; SaaS is just a client.

Folders:
  view = "folders"  the music folder as it is on the server's disk (default):
                    every folder that directly holds tracks is one folder,
                    like on a stick. `start` limits it to a sub-folder
                    ("Rock/Live").
  view = "albums"   every album, sorted by artist.

Password protection (LMS Settings -> Advanced -> Security): set user and
password; they go in a Basic auth header and, for mpv, into the stream URL.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from . import http
from .base import NetLibrary, Track

log = logging.getLogger(__name__)

PAGE = 500
MAX_FOLDERS = 5000    # safety net for huge libraries


class LmsLibrary(NetLibrary):
    kind = "lms"

    def __init__(self, name: str, url: str, view: str = "folders", start: str = "",
                 user: str = "", password: str = "") -> None:
        super().__init__(name, f"{url}|{view}|{start}")
        self.base = url.rstrip("/")
        self.view = view
        self.start = start.strip("/")
        self.user, self.password = user, password

    # JSON-RPC -------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.user:
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {token}"
        return h

    def call(self, *command: Any, timeout: float = 8.0) -> Dict[str, Any]:
        """One LMS command, e.g. call("albums", 0, 100, "tags:la") -> result dict."""
        body = json.dumps({"id": 1, "method": "slim.request",
                           "params": ["", [str(c) for c in command]]}).encode()
        try:
            raw = http.request(f"{self.base}/jsonrpc.js", self._headers(), data=body, timeout=timeout)
        except http.NetError as exc:
            if "HTTP 401" in str(exc):
                raise http.NetError("LMS wants a password: set user/password (or they are wrong)") from None
            raise
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise http.NetError(f"{self.base}: not an LMS JSON-RPC answer") from None
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise http.NetError(f"LMS {command[0]}: no result")
        return result

    def _loop(self, command: str, loop: str, *params: Any) -> List[Dict[str, Any]]:
        """All items of a paged query."""
        out: List[Dict[str, Any]] = []
        while True:
            res = self.call(command, len(out), PAGE, *params)
            items = res.get(loop) or []
            out += items
            if not items or len(out) >= int(res.get("count") or 0):
                return out

    def stream_url(self, track_id: Any) -> str:
        netloc = urllib.parse.urlsplit(self.base)
        if self.user:
            cred = urllib.parse.quote(self.user, safe="") + ":" + urllib.parse.quote(self.password, safe="")
            base = urllib.parse.urlunsplit((netloc.scheme, f"{cred}@{netloc.netloc}", netloc.path, "", ""))
        else:
            base = self.base
        return f"{base}/music/{int(track_id)}/download"

    # NetLibrary -------------------------------------------------------------------
    def ping(self) -> None:
        self.call("version", "?", timeout=5)

    def list_folders(self) -> List[Tuple[str, str]]:
        res = self.call("serverstatus", 0, 0)
        if res.get("rescan"):
            log.info("LMS is scanning its library - folders may be incomplete until it's done")
        if self.view == "albums":
            return self._album_folders()
        return self._disk_folders()

    def _album_folders(self) -> List[Tuple[str, str]]:
        out = []
        for a in self._loop("albums", "albums_loop", "tags:la", "sort:artistalbum"):
            artist = str(a.get("artist") or "")
            name = str(a.get("album") or "?")
            out.append((f"album:{a['id']}", f"{artist} - {name}" if artist else name))
        return out

    def _children(self, folder_id: Optional[int]) -> List[Dict[str, Any]]:
        params = ["tags:dt"] + ([f"folder_id:{folder_id}"] if folder_id is not None else [])
        return self._loop("musicfolder", "folder_loop", *params)

    def _start_id(self) -> Optional[int]:
        node: Optional[int] = None
        for part in [p for p in self.start.split("/") if p]:
            match = [c for c in self._children(node)
                     if c.get("type") == "folder" and str(c.get("filename", "")).lower() == part.lower()]
            if not match:
                raise http.NetError(f"LMS: no folder {part!r} in the music folder (start = {self.start!r})")
            node = int(match[0]["id"])
        return node

    def _disk_folders(self) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        queue = deque([(self._start_id(), "")])
        seen = 0
        while queue and seen < MAX_FOLDERS:
            fid, path = queue.popleft()
            seen += 1
            children = self._children(fid)
            tracks = [c for c in children if c.get("type") == "track"]
            if tracks:
                key = f"folder:{fid if fid is not None else 0}"
                out.append((key, path or self.name))
                self._tracks[key] = [self._folder_track(c) for c in tracks]   # no second request later
            subs = sorted((c for c in children if c.get("type") == "folder"),
                          key=lambda c: str(c.get("filename", "")).lower())
            for c in subs:
                name = str(c.get("filename", "?"))
                queue.append((int(c["id"]), f"{path} / {name}" if path else name))
        if queue:
            log.warning("LMS: stopped after %d folders - set `start` to a sub-folder", MAX_FOLDERS)
        out.sort(key=lambda f: f[1].lower())
        return out

    def _folder_track(self, item: Dict[str, Any]) -> Track:
        # The folder listing only has file names; real tags come from songinfo when played.
        title = os.path.splitext(str(item.get("title") or item.get("filename") or ""))[0]
        return {"id": item["id"], "title": title, "artist": "", "album": "",
                "duration": _seconds(item.get("duration")), "url": self.stream_url(item["id"]),
                "tagged": False}

    def list_tracks(self, key: str) -> List[Track]:
        kind, _, ident = key.partition(":")
        if kind == "folder":
            fid = int(ident)
            return [self._folder_track(c) for c in self._children(fid or None) if c.get("type") == "track"]
        songs = self._loop("titles", "titles_loop", f"album_id:{int(ident)}", "tags:adtli", "sort:tracknum")
        songs.sort(key=lambda s: (_int(s.get("disc")), _int(s.get("tracknum"))))
        return [{"id": s["id"], "title": str(s.get("title") or ""), "artist": str(s.get("artist") or ""),
                 "album": str(s.get("album") or ""), "duration": _seconds(s.get("duration")),
                 "url": self.stream_url(s["id"]), "tagged": True} for s in songs]

    def track_tags(self, folder_index: int, track_index: int) -> Optional[dict]:
        t = self._track(folder_index, track_index)
        if not t.get("tagged"):
            t["tagged"] = True       # one try per track, even if it fails
            try:
                res = self.call("songinfo", 0, 100, f"track_id:{int(t['id'])}", "tags:adtl", timeout=4)
                info: Dict[str, Any] = {}
                for entry in res.get("songinfo_loop") or []:
                    info.update(entry)
                t["title"] = str(info.get("title") or t["title"])
                t["artist"] = str(info.get("artist") or "")
                t["album"] = str(info.get("album") or "")
                t["duration"] = _seconds(info.get("duration")) or t["duration"]
            except http.NetError as exc:
                log.debug("LMS songinfo failed: %s", exc)
        return {k: t.get(k) for k in ("title", "artist", "album", "duration")}


def _seconds(value: Any) -> float:
    """LMS gives 20.035, "20" or (in folder listings) "3:25"."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        total = 0.0
        for part in str(value).split(":"):
            total = total * 60 + float(part)
        return total
    except ValueError:
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(str(value).split("/")[0])
    except (TypeError, ValueError):
        return 0
