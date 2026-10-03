"""Tiny JSON file that remembers folder/track/position/volume across reboots."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

log = logging.getLogger(__name__)


class ResumeStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable resume file %s: %s", self.path, exc)
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        # Write to a temp file, fsync, then rename: a power cut leaves either
        # the old or the new file, never a half-written one.
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save resume file %s: %s", self.path, exc)
