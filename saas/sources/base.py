"""Sources announce media appearing/disappearing (MEDIA_ADDED/MEDIA_REMOVED)."""
from __future__ import annotations

from ..inputs.base import Input


class Source(Input):
    """Same lifecycle as an input: start(), emits to the bus, close()."""
