"""Version + codename. The number lives in VERSION (read by OTA and make-pub.sh),
the codename here (so OTA-updated installs always carry it)."""
from __future__ import annotations

import os

CODENAME = "janky-jellyfish"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version() -> str:
    try:
        with open(os.path.join(_ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


def full() -> str:
    return f"{version()} {CODENAME}"
