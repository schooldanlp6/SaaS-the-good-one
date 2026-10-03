"""Silent fake backend: every track "lasts" N seconds. For dev boxes without
libmpv and for the smoke test. Missing files fail like mpv would."""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from .base import AudioBackend


class DummyBackend(AudioBackend):
    def __init__(self, track_seconds: float = 10.0) -> None:
        super().__init__()
        self.track_seconds = float(track_seconds)
        self.current: Optional[str] = None
        self.paused = False
        self.volume = 0
        self._elapsed = 0.0
        self._since = 0.0
        self._gen = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    # timer plumbing -------------------------------------------------------
    def _cancel(self) -> None:
        self._gen += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self, delay: float, reason: str) -> None:
        gen = self._gen
        self._since = time.monotonic()
        self._timer = threading.Timer(max(0.0, delay), self._fire, args=(gen, reason))
        self._timer.daemon = True
        self._timer.start()

    def _fire(self, gen: int, reason: str) -> None:
        with self._lock:
            if gen != self._gen or self.current is None:
                return
            self.current = None
            self._timer = None
        self._ended(reason)

    # AudioBackend ---------------------------------------------------------
    def play(self, path: str, start: float = 0.0, paused: bool = False) -> None:
        with self._lock:
            self._cancel()
            self.current = path
            self._elapsed = float(start or 0.0)
            self.paused = paused
            if not os.path.isfile(path):
                self._arm(0.0, "error")
                return
            if not paused:
                self._arm(self.track_seconds - self._elapsed, "eof")
        name = os.path.splitext(os.path.basename(path))[0]
        self._info({"title": name, "artist": "", "album": "", "duration": self.track_seconds})

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if self.current is None or paused == self.paused:
                return
            if paused:
                self._elapsed += time.monotonic() - self._since
                self._cancel()
            else:
                self._arm(self.track_seconds - self._elapsed, "eof")
            self.paused = paused

    def stop(self) -> None:
        with self._lock:
            self._cancel()
            self.current = None

    def seek(self, seconds: float) -> None:
        with self._lock:
            if self.current is None:
                return
            self._elapsed = float(seconds)
            if not self.paused:
                self._cancel()
                self._arm(self.track_seconds - self._elapsed, "eof")

    def position(self) -> Optional[float]:
        with self._lock:
            if self.current is None:
                return None
            if self.paused:
                return self._elapsed
            return self._elapsed + time.monotonic() - self._since

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def close(self) -> None:
        self.stop()
