"""Inputs turn the outside world (buttons, keys, ...) into bus events."""
from __future__ import annotations

from ..core.events import Bus


class Input:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass
