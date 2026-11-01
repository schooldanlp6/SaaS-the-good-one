"""Terminal keys -> events, for developing on a laptop.

Holding next/prev (n/p, arrow right/left) scrubs through the song instead of
skipping a pile of tracks. A terminal only sends key *presses* - holding a key
just makes it auto-repeat - so a hold is recognised by its rhythm: press,
pause (the auto-repeat delay), then fast repeats. That means a single tap
of next/prev acts once the auto-repeat delay has passed without a repeat
(~0.3-0.7 s; the delay is measured on the first hold and used from then on).
"""
from __future__ import annotations

import logging
import os
import select
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

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

# Keys whose hold scrubs: tap event -> hold event
SCRUB_HOLDS: Dict[str, Event] = {"next": Event.SEEK_FWD, "prev": Event.SEEK_BACK}
SCRUB_INTERVAL = 0.25   # one seek step per 0.25 s while held


class HoldDetector:
    """Tells taps from holds of the scrub keys by the timing of their auto-repeats.

    feed(name, now) for every key and poll(now) whenever nothing arrived; both
    return the events to emit as [(event name, data)]. timeout(now) says how
    long the caller may wait for the next key before calling poll().
    """
    REPEAT_GAP = 0.15     # auto-repeats come every ~30-40 ms; a human can't tap that fast
    RELEASE_GAP = 0.35    # no repeat for this long while scrubbing = key released
    DEFAULT_WINDOW = 0.7  # wait this long for a first repeat before calling it a tap

    def __init__(self) -> None:
        self.window = self.DEFAULT_WINDOW
        self._key: Optional[str] = None
        self._arrivals: List[float] = []
        self._scrubbing = False
        self._tick = 0
        self._next_tick = 0.0

    def feed(self, name: Optional[str], now: float) -> List[Tuple[str, object]]:
        out: List[Tuple[str, object]] = []
        if name != self._key:
            out += self._flush()
        if name not in SCRUB_HOLDS:
            if name:
                out.append((name, None))
            return out
        if self._key is None:
            self._key, self._arrivals = name, [now]
            return out
        if self._scrubbing:
            self._arrivals = [now]     # still held
            return out + self.poll(now)
        gap = now - self._arrivals[-1]
        self._arrivals.append(now)
        if gap < self.REPEAT_GAP and len(self._arrivals) >= 3:
            # press, first repeat, fast repeat: a hold. Earlier arrivals were taps.
            delay = self._arrivals[-2] - self._arrivals[-3]
            if delay >= self.REPEAT_GAP:                       # learn the auto-repeat delay
                self.window = min(0.8, max(0.3, delay + 0.12))
            out += [(name, None)] * (len(self._arrivals) - 3)
            self._scrubbing, self._tick, self._next_tick = True, 0, now
            self._arrivals = [now]
            out += self.poll(now)
        return out

    def poll(self, now: float) -> List[Tuple[str, object]]:
        if self._key is None:
            return []
        last = self._arrivals[-1]
        if self._scrubbing:
            if now - last > self.RELEASE_GAP:
                return self._flush()
            out: List[Tuple[str, object]] = []
            if now >= self._next_tick:
                out.append((SCRUB_HOLDS[self._key].value, self._tick))
                self._tick += 1
                self._next_tick = now + SCRUB_INTERVAL
            return out
        if now - last >= self.window:
            return self._flush()
        return []

    def timeout(self, now: float) -> Optional[float]:
        if self._key is None:
            return None
        if self._scrubbing:
            return max(0.0, min(self._next_tick, self._arrivals[-1] + self.RELEASE_GAP + 0.01) - now)
        return max(0.0, self._arrivals[-1] + self.window - now)

    def _flush(self) -> List[Tuple[str, object]]:
        """End whatever is pending: taps count once each, a scrub just stops."""
        key, arrivals, scrubbing = self._key, self._arrivals, self._scrubbing
        self._key, self._arrivals, self._scrubbing = None, [], False
        if key is None or scrubbing:
            return []
        return [(key, None)] * len(arrivals)


_LABELS = {
    "play_pause": "play/pause", "stop": "stop", "next": "next", "prev": "prev",
    "folder_up": "folder +", "folder_down": "folder -", "vol_up": "vol +", "vol_down": "vol -",
    "playlist_add": "save/remove", "playlist_next": "next playlist", "playlist_new": "new playlist",
    "like": "like", "view_toggle": "folders/playlists", "source_next": "next stick",
    "update": "update", "standby": "standby", "poweroff": "power off",
    "help": "this help", "quit": "quit", "seek_fwd": "seek +", "seek_back": "seek -",
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
        items.append("hold prev/next: scrub")
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
        holds = HoldDetector()
        while True:
            wait = holds.timeout(time.monotonic())
            try:
                ready = select.select([self._fd], [], [], wait)[0]
            except (OSError, ValueError):
                return
            now = time.monotonic()
            if not ready:
                self._dispatch(holds.poll(now))
                continue
            try:
                data = os.read(self._fd, 1)
            except OSError:
                return
            if not data:
                self._dispatch(holds.poll(now + 10))   # stdin closed: a pending tap still counts
                return
            if data == b"\x1b":
                name = ESCAPE_KEYS.get(self._read_escape())
            else:
                name = self.keys.get(data.decode("utf-8", "ignore"))
            self._dispatch(holds.feed(name, now))

    def _dispatch(self, items: List[Tuple[str, object]]) -> None:
        for name, data in items:
            if name == "help":
                self.print_help()
            elif name == "quit":
                self.bus.emit(Event.QUIT)
            else:
                self.bus.emit(parse_event(name), data)

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
