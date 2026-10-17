"""Terminal keys -> events, for developing on a laptop."""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Dict, Optional

from ..core.events import Bus, Event, parse_event
from .base import Input

log = logging.getLogger(__name__)

DEFAULT_KEYS: Dict[str, str] = {
    " ": "play_pause",
    "s": "stop",
    "n": "next",
    "p": "prev",
    "]": "folder_up",
    "[": "folder_down",
    "+": "vol_up",
    "-": "vol_down",
    "a": "playlist_add",
    "l": "playlist_next",
    "c": "playlist_new",
    "f": "like",
    "v": "view_toggle",
    "U": "update",
    "*": "source_next",
    "?": "help",
    "q": "quit",
}

_LABELS = {
    "play_pause": "play/pause", "stop": "stop", "next": "next", "prev": "prev",
    "folder_up": "folder +", "folder_down": "folder -", "vol_up": "vol +", "vol_down": "vol -",
    "playlist_add": "save/remove", "playlist_next": "next playlist", "playlist_new": "new playlist",
    "like": "like", "view_toggle": "folders/playlists", "source_next": "next stick",
    "update": "update", "help": "this help", "quit": "quit",
}


class KeyboardInput(Input):
    def __init__(self, bus: Bus, keys: Optional[Dict[str, str]] = None) -> None:
        super().__init__(bus)
        self.keys = dict(DEFAULT_KEYS)
        self.keys.update(keys or {})
        self._fd: Optional[int] = None
        self._saved = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            log.warning("keyboard input enabled but stdin is not a terminal - disabled")
            return
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)  # unbuffered keys, output processing stays normal
        threading.Thread(target=self._run, name="keyboard", daemon=True).start()
        self.print_help()

    def help_text(self) -> str:
        items = [f"{'space' if k == ' ' else k}: {_LABELS.get(v, v)}" for k, v in self.keys.items()]
        lines, line = [], "keys:"
        for item in items:  # wrap at ~78 columns
            if len(line) + 3 + len(item) > 78:
                lines.append(line)
                line = "     "
            line += "   " + item
        lines.append(line)
        return "\n".join(lines)

    def print_help(self) -> None:
        print(self.help_text(), flush=True)

    def _run(self) -> None:
        while True:
            try:
                data = os.read(self._fd, 1)
            except OSError:
                return
            if not data:
                return
            name = self.keys.get(data.decode("utf-8", "ignore"))
            if name == "help":
                self.print_help()
            elif name == "quit":
                self.bus.emit(Event.QUIT)
            elif name:
                self.bus.emit(parse_event(name))

    def close(self) -> None:
        if self._fd is not None and self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None
