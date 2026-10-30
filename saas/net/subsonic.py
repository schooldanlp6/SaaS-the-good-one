"""Subsonic API servers (Navidrome, Airsonic, Gonic, ...). Folders = albums.

Auth: token (t = md5(password + salt), API 1.13+) by default; `auth = "password"`
sends the password hex-encoded instead, for servers without token support."""
from __future__ import annotations

import hashlib
import secrets
from typing import Dict, List, Tuple

from . import http
from .base import NetLibrary, Track

API_VERSION = "1.16.1"
PAGE = 500


class SubsonicLibrary(NetLibrary):
    kind = "subsonic"

    def __init__(self, name: str, url: str, user: str, password: str, auth: str = "token") -> None:
        super().__init__(name, f"{url}|{user}")
        self.base = url.rstrip("/")
        self.user, self.password, self.auth = user, password, auth

    def _auth(self) -> Dict[str, str]:
        p = {"u": self.user, "v": API_VERSION, "c": "saas", "f": "json"}
        if self.auth == "password":
            p["p"] = "enc:" + self.password.encode().hex()
        else:
            salt = secrets.token_hex(6)
            p["s"] = salt
            p["t"] = hashlib.md5((self.password + salt).encode()).hexdigest()
        return p

    def _url(self, method: str, **params) -> str:
        return f"{self.base}/rest/{method}?" + http.query({**self._auth(), **params})

    def _call(self, method: str, **params) -> dict:
        data = http.get_json(self._url(method, **params)).get("subsonic-response", {})
        if data.get("status") != "ok":
            err = data.get("error", {})
            raise http.NetError(f"Subsonic {method}: {err.get('message', 'failed')} (code {err.get('code', '?')})")
        return data

    def ping(self) -> None:
        self._call("ping")

    def list_folders(self) -> List[Tuple[str, str]]:
        out, offset = [], 0
        while True:
            albums = self._call("getAlbumList2", type="alphabeticalByArtist", size=PAGE,
                                offset=offset).get("albumList2", {}).get("album", [])
            for a in albums:
                artist = a.get("artist") or ""
                out.append((str(a["id"]), f"{artist} - {a.get('name', '?')}" if artist else str(a.get("name", "?"))))
            if len(albums) < PAGE:
                return out
            offset += PAGE

    def list_tracks(self, key: str) -> List[Track]:
        album = self._call("getAlbum", id=key).get("album", {})
        songs = sorted(album.get("song", []), key=lambda s: (s.get("discNumber", 0) or 0, s.get("track", 0) or 0))
        return [{
            "id": s["id"], "title": s.get("title", ""), "artist": s.get("artist", ""),
            "album": s.get("album", album.get("name", "")), "duration": float(s.get("duration") or 0),
            "url": self._url("stream", id=s["id"]),
        } for s in songs]
