"""LEDs as a poor man's display. Wiring: GPIO pin -> resistor (330R-1k) ->
LED anode, LED cathode -> GND. Every LED is optional.

  playing      on = playing, slow blink = paused, off = stopped / no media
  media        on = media mounted, off = none
  folder_bits  folder number (1-based) in binary, wraps at 2^n
               (no folder_bits: the media LED blinks the folder number instead)
"""
from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from ..core.state import PAUSED, PLAYING, PlayerState
from .base import Output

log = logging.getLogger(__name__)


class Led:
    """One output line with on/off/blink. Blinking runs in its own thread."""

    def __init__(self, line: Any) -> None:
        self.line = line
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    def _stop_blink(self) -> None:
        if self._stop is not None:
            self._stop.set()
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=1)
            self._stop = self._thread = None

    def set(self, value: bool) -> None:
        self._stop_blink()
        self.line.set(value)

    def blink(self, on_time: float, off_time: float, n: Optional[int] = None,
              then: Optional[bool] = None) -> None:
        """Blink forever, or n times and then stay at `then`."""
        self._stop_blink()
        stop = threading.Event()

        def run() -> None:
            count = 0
            while n is None or count < n:
                self.line.set(True)
                if stop.wait(on_time):
                    return
                self.line.set(False)
                if stop.wait(off_time):
                    return
                count += 1
            if then is not None:
                self.line.set(then)

        self._stop = stop
        self._thread = threading.Thread(target=run, name="led-blink", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_blink()
        try:
            self.line.set(False)
        finally:
            self.line.close()


class LedOutput(Output):
    def __init__(self, chip: Any, playing: Optional[int] = None, media: Optional[int] = None,
                 folder_bits: Optional[List[int]] = None,
                 blink_on: float = 0.15, blink_off: float = 0.3) -> None:
        self.playing = Led(chip.output(playing)) if playing is not None else None
        self.media = Led(chip.output(media)) if media is not None else None
        self.bits = [Led(chip.output(p)) for p in (folder_bits or [])]
        self.blink_on, self.blink_off = blink_on, blink_off
        self._play_mode: Optional[str] = None
        self._has_media: Optional[bool] = None
        self._folder: Optional[int] = None

    def update(self, s: PlayerState) -> None:
        mode = "on" if s.status == PLAYING else "blink" if s.status == PAUSED else "off"
        if self.playing and mode != self._play_mode:
            if mode == "blink":
                self.playing.blink(0.5, 0.5)
            else:
                self.playing.set(mode == "on")
        self._play_mode = mode

        media_changed = s.media != self._has_media
        self._has_media = s.media
        if self.media and media_changed:
            self.media.set(s.media)

        folder = s.folder_index if s.media else None
        if folder != self._folder:
            self._folder = folder
            if self.bits:
                value = 0 if folder is None else (folder + 1) % (1 << len(self.bits))
                for i, led in enumerate(self.bits):
                    led.set(bool(value >> i & 1))
            elif self.media and folder is not None:
                # blink the folder number, then back to solid on
                self.media.blink(self.blink_on, self.blink_off, n=folder + 1, then=True)

    def close(self) -> None:
        for led in [self.playing, self.media, *self.bits]:
            if led is not None:
                try:
                    led.close()
                except Exception:
                    log.debug("led close failed", exc_info=True)
