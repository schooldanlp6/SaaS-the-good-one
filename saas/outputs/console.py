"""One status line per state change on stdout (ends up in the journal/log
when running as a service)."""
from __future__ import annotations

from ..core.state import PlayerState
from .base import Output

_TAGS = {"idle": "[----]", "stopped": "[stop]", "playing": "[play]", "paused": "[paus]"}


def format_state(s: PlayerState) -> str:
    tag = _TAGS.get(s.status, f"[{s.status}]")
    if not s.media:
        return f"{tag} no media  vol {s.volume}"
    medium = f"<{s.medium_name}> " if s.medium_count > 1 else ""
    letter = "P" if s.view == "playlists" else "F"
    return (f"{tag} {medium}{letter}{s.view_index}/{s.view_count} {s.folder_name}  "
            f"T{s.track_index + 1}/{s.track_count} {s.track_name}  vol {s.volume}")


class ConsoleOutput(Output):
    def __init__(self) -> None:
        self._last = ""
        self._notice_time = 0.0

    def update(self, state: PlayerState) -> None:
        if state.notice and state.notice_time != self._notice_time:
            self._notice_time = state.notice_time
            print(f"  * {state.notice}", flush=True)
        line = format_state(state)
        if line != self._last:
            self._last = line
            print(line, flush=True)
