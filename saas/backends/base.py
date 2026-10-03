"""Audio backend interface. The controller manages the playlist itself and
hands the backend one file at a time, so a backend stays very small."""
from __future__ import annotations

import abc
from typing import Callable, Optional


class AudioBackend(abc.ABC):
    def __init__(self) -> None:
        self._on_end: Optional[Callable[[str], None]] = None
        self._on_info: Optional[Callable[[dict], None]] = None

    def set_end_callback(self, fn: Callable[[str], None]) -> None:
        """fn("eof" | "error") is called (from any thread) when a file stops on
        its own. It must NOT be called for stop() or for replacing the file."""
        self._on_end = fn

    def set_info_callback(self, fn: Callable[[dict], None]) -> None:
        """fn({"title", "artist", "album", "duration"}) once a file's tags are known."""
        self._on_info = fn

    def _info(self, info: dict) -> None:
        if self._on_info is not None:
            self._on_info(info)

    def _ended(self, reason: str) -> None:
        if self._on_end is not None:
            self._on_end(reason)

    @abc.abstractmethod
    def play(self, path: str, start: float = 0.0, paused: bool = False) -> None: ...

    @abc.abstractmethod
    def set_paused(self, paused: bool) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def seek(self, seconds: float) -> None: ...

    @abc.abstractmethod
    def position(self) -> Optional[float]: ...

    @abc.abstractmethod
    def set_volume(self, volume: int) -> None: ...

    def close(self) -> None:
        pass
