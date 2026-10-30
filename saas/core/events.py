"""Events and the tiny bus everything talks through.

Inputs and sources call ``bus.emit()`` from whatever thread they live in
(GPIO event thread, mpv's event thread, the USB poller). The main loop is
the only consumer, so all player logic runs single-threaded.
"""
from __future__ import annotations

import enum
import queue
from typing import Any, Optional, Tuple


class Event(enum.Enum):
    # User-facing events. Config files refer to them by value ("folder_up").
    PLAY_PAUSE = "play_pause"
    STOP = "stop"
    NEXT = "next"
    PREV = "prev"
    FOLDER_UP = "folder_up"      # next folder
    FOLDER_DOWN = "folder_down"  # previous folder
    VOL_UP = "vol_up"
    VOL_DOWN = "vol_down"
    PLAYLIST_ADD = "playlist_add"    # toggle the current track in the active playlist
    LIKE = "like"                    # toggle the current track in "Liked Songs"
    PLAYLIST_NEXT = "playlist_next"  # cycle the active playlist
    PLAYLIST_NEW = "playlist_new"    # create "Playlist N" (next number) and make it active
    SOURCE_NEXT = "source_next"      # cycle through the media present (sticks, ...)
    VIEW_TOGGLE = "view_toggle"      # switch folder navigation: folders <-> playlists
    UPDATE = "update"                # 1st press: look for DLP6/Update/saas.pub, 2nd: install
    STANDBY = "standby"              # toggle soft standby (any button wakes)
    POWEROFF = "poweroff"            # save state, then shut the system down

    # Internal events.
    TRACK_END = "track_end"          # data: "eof" | "error"
    MEDIA_ADDED = "media_added"      # data: media root path
    MEDIA_REMOVED = "media_removed"  # data: media root path
    TRACK_INFO = "track_info"        # data: {"title", "artist", "album", "duration"}
    QUIT = "quit"
    RESTART = "restart"              # main loop: clean shutdown, then exec ourselves again
    HALT = "halt"                    # main loop: clean shutdown, then run the poweroff command


USER_EVENTS = {
    e.value: e
    for e in (
        Event.PLAY_PAUSE, Event.STOP, Event.NEXT, Event.PREV,
        Event.FOLDER_UP, Event.FOLDER_DOWN, Event.VOL_UP, Event.VOL_DOWN,
        Event.PLAYLIST_ADD, Event.PLAYLIST_NEXT, Event.SOURCE_NEXT, Event.LIKE, Event.VIEW_TOGGLE,
        Event.UPDATE, Event.PLAYLIST_NEW, Event.STANDBY, Event.POWEROFF,
    )
}


def parse_event(name: str) -> Event:
    """Turn a config name like "folder_up" into an Event (user events only)."""
    try:
        return USER_EVENTS[str(name).strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown event {name!r} (valid: {', '.join(sorted(USER_EVENTS))})"
        ) from None


class Bus:
    """Thread-safe FIFO of (event, data) tuples."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Tuple[Event, Any]]" = queue.Queue()

    def emit(self, event: Event, data: Any = None) -> None:
        self._q.put((event, data))

    def get(self, timeout: Optional[float] = None) -> Optional[Tuple[Event, Any]]:
        """Block until an event arrives. Returns None on timeout."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None
