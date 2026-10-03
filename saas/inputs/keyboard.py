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
    "=": "vol_up",
    "-": "vol_down",
    "a": "playlist_add",
    "l": "playlist_next",
    "f": "like",
    "v": "view_toggle",
    "*": "source_next",
    "q": "quit",
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
        help_line = "  ".join(f"[{'space' if k == ' ' else k}] {v}" for k, v in self.keys.items())
        print(f"keys: {help_line}", flush=True)

    def _run(self) -> None:
        while True:
            try:
                data = os.read(self._fd, 1)
            except OSError:
                return
            if not data:
                return
            name = self.keys.get(data.decode("utf-8", "ignore"))
            if name == "quit":
                self.bus.emit(Event.QUIT)
            elif name:
                self.bus.emit(parse_event(name))

    def close(self) -> None:
        if self._fd is not None and self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None
