"""Outputs render PlayerState snapshots (LEDs, console, later an OLED).
update() is only called when the state actually changed."""
from __future__ import annotations

from ..core.state import PlayerState


class Output:
    def update(self, state: PlayerState) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass
