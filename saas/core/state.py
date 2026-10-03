"""The player state that outputs (LEDs, console, later an OLED) render."""
from __future__ import annotations

import dataclasses

IDLE = "idle"        # no media
STOPPED = "stopped"  # media present, nothing loaded
PLAYING = "playing"
PAUSED = "paused"


@dataclasses.dataclass
class PlayerState:
    status: str = IDLE
    media: bool = False
    folder_index: int = 0
    folder_count: int = 0
    folder_name: str = ""
    track_index: int = 0
    track_count: int = 0
    track_name: str = ""
    volume: int = 80
    medium_name: str = ""       # e.g. the stick's label
    medium_index: int = 0       # 1-based, 0 = none
    medium_count: int = 0
    in_playlist: bool = False   # current "folder" is a playlist
    # navigation view: folder up/down only moves within it
    view: str = "folders"       # "folders" | "playlists"
    view_index: int = 0         # 1-based position of the current folder/playlist in its view
    view_count: int = 0
    # tags of the current track (from the backend; "" / 0 if unknown)
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    # status: which playlist "add" goes to, and the last thing that happened
    active_playlist: str = ""
    in_active_playlist: bool = False   # current track is already in active_playlist
    liked: bool = False                # current track is in the liked playlist
    notice: str = ""
    notice_time: float = 0.0

    def snapshot(self) -> "PlayerState":
        return dataclasses.replace(self)
