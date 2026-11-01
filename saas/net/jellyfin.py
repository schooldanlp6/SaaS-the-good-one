"""Jellyfin: API key from Dashboard -> API Keys. Folders = music albums.

Auth: `Authorization: MediaBrowser Token="<key>"` on API calls, `ApiKey=` in
stream URLs (mpv fetches those itself). Streams are the original files
(`static=true`), so the Pi decodes them like files from a stick."""
from __future__ import annotations

from typing import List, Tuple

from . import http
from .base import NetLibrary, Track


class JellyfinLibrary(NetLibrary):
    kind = "jellyfin"

    def __init__(self, name: str, url: str, api_key: str, user: str = "") -> None:
        super().__init__(name, f"{url}|{user}")
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.user = user
        self._user_id = ""

    def _get(self, path: str, **params):
        sep = "&" if "?" in path else "?"
        url = f"{self.base}{path}{sep + http.query(params) if params else ''}"
        return http.get_json(url, {"Authorization": self._authorization()})

    def _authorization(self) -> str:
        # The full MediaBrowser form (Jellyfin 12 dropped X-Emby-Token & co., this one stays)
        from ..version import version
        return (f'MediaBrowser Token="{self.api_key}", Client="SaaS", Device="SaaS player", '
                f'DeviceId="{self.fingerprint}", Version="{version()}"')

    def ping(self) -> None:
        http.get_json(f"{self.base}/System/Info/Public", timeout=5)

    def user_id(self) -> str:
        if not self._user_id:
            users = self._get("/Users")
            if self.user:
                match = [u for u in users if str(u.get("Name", "")).lower() == self.user.lower()]
                if not match:
                    raise http.NetError(f"Jellyfin user {self.user!r} not found")
            elif len(users) == 1:
                match = users
            else:
                raise http.NetError("Jellyfin has several users - set `user` in the config")
            self._user_id = str(match[0]["Id"])
        return self._user_id

    def list_folders(self) -> List[Tuple[str, str]]:
        data = self._get("/Items", userId=self.user_id(), IncludeItemTypes="MusicAlbum", Recursive="true",
                         SortBy="AlbumArtist,SortName", SortOrder="Ascending")
        out = []
        for it in data.get("Items", []):
            artist = it.get("AlbumArtist") or ""
            out.append((str(it["Id"]), f"{artist} - {it.get('Name', '?')}" if artist else str(it.get("Name", "?"))))
        return out

    def list_tracks(self, key: str) -> List[Track]:
        data = self._get("/Items", userId=self.user_id(), ParentId=key, IncludeItemTypes="Audio",
                         Recursive="true", SortBy="ParentIndexNumber,IndexNumber,SortName")
        out = []
        for it in data.get("Items", []):
            artists = it.get("Artists") or []
            ticks = it.get("RunTimeTicks") or 0
            out.append({
                "id": it["Id"], "title": it.get("Name", ""),
                "artist": ", ".join(artists) if artists else it.get("AlbumArtist", ""),
                "album": it.get("Album", ""), "duration": ticks / 10_000_000,
                "url": f"{self.base}/Audio/{it['Id']}/stream?" + http.query({"static": "true", "ApiKey": self.api_key}),
            })
        return out
