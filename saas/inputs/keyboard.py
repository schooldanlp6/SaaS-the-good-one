"""Terminal keys -> events, for developing on a laptop."""
from __future__ import annotations

import logging
import os
import select
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
    "z": "standby",
    "?": "help",
    "q": "quit",
}

# Escape sequences of special keys. Read as ONE key - byte by byte, the "["
# inside "ESC [ A" (arrow up) used to trigger folder_down.
ESCAPE_KEYS: Dict[str, str] = {
    "\x1b[A": "vol_up", "\x1bOA": "vol_up",        # arrow up
    "\x1b[B": "vol_down", "\x1bOB": "vol_down",    # arrow down
    "\x1b[C": "next", "\x1bOC": "next",            # arrow right
    "\x1b[D": "prev", "\x1bOD": "prev",            # arrow left
}

_LABELS = {
    "play_pause": "play/pause", "stop": "stop", "next": "next", "prev": "prev",
    "folder_up": "folder +", "folder_down": "folder -", "vol_up": "vol +", "vol_down": "vol -",
    "playlist_add": "save/remove", "playlist_next": "next playlist", "playlist_new": "new playlist",
    "like": "like", "view_toggle": "folders/playlists", "source_next": "next stick",
    "update": "update", "standby": "standby", "poweroff": "power off",
    "help": "this help", "quit": "quit",
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
        from ..version import full
        items = [f"{'space' if k == ' ' else k}: {_LABELS.get(v, v)}" for k, v in self.keys.items()]
        items.append("arrows: prev/next, vol")
        lines, line = [], "keys:"
        for item in items:  # wrap at ~78 columns
            if len(line) + 3 + len(item) > 78:
                lines.append(line)
                line = "     "
            line += "   " + item
        lines.append(line)
        return f"SaaS {full()}\n" + "\n".join(lines)

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
            if data == b"\x1b":
                name = ESCAPE_KEYS.get(self._read_escape())
            else:
                name = self.keys.get(data.decode("utf-8", "ignore"))
            if name == "help":
                self.print_help()
            elif name == "quit":
                self.bus.emit(Event.QUIT)
            elif name:
                self.bus.emit(parse_event(name))

    def _read_escape(self) -> str:
        """Collect the rest of an escape sequence (they arrive within a few ms).
        Unknown sequences (Home, Del, F-keys, a lone Esc) map to nothing."""
        seq = b"\x1b"
        while len(seq) < 8 and select.select([self._fd], [], [], 0.03)[0]:
            b = os.read(self._fd, 1)
            if not b:
                break
            seq += b
            if len(seq) >= 3 and (0x40 <= b[0] <= 0x7E) and seq[1:2] in (b"[", b"O"):
                break  # final byte of a CSI / SS3 sequence
        return seq.decode("ascii", "ignore")

    def close(self) -> None:
        if self._fd is not None and self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None
