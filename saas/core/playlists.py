"""Playlists on the stick: <media root>/DLP6/Playlists/<name>.toml

    # SaaS playlist
    tracks = [
        "01 Album A/1 a.flac",
        "Singles/x.mp3",
    ]

Paths are relative to the stick root with "/" separators, so the same file
works on the Pi and on a PC. The file name is the playlist name. Shared by the
player and pctool.py.
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from typing import List, Optional

log = logging.getLogger(__name__)

DEFAULT_DIR = "DLP6/Playlists"
_BAD_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')  # not allowed in FAT names


def sanitize_name(name: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip().strip(".")
    return name[:100]


def _toml_str(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def dump(tracks: List[str]) -> str:
    lines = ["# SaaS playlist - paths relative to the stick root", "tracks = ["]
    lines += [f"    {_toml_str(t)}," for t in tracks]
    lines.append("]")
    return "\n".join(lines) + "\n"


def clean_rel(path: str) -> Optional[str]:
    """Normalize a stick-relative path; None if it's absolute or escapes the stick."""
    p = path.replace("\\", "/").strip()
    if not p or p.startswith("/"):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


class PlaylistStore:
    def __init__(self, root: str, subdir: str = DEFAULT_DIR) -> None:
        self.root = root
        self.dir = os.path.join(root, *subdir.split("/"))

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name + ".toml")

    def names(self) -> List[str]:
        try:
            with os.scandir(self.dir) as it:
                names = [e.name[:-5] for e in it
                         if e.name.lower().endswith(".toml") and not e.name.startswith(".") and e.is_file()]
        except OSError:
            return []
        from .library import natural_key
        return sorted(names, key=natural_key)

    def load(self, name: str) -> List[str]:
        try:
            with open(self.path(name), "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            return []
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("playlist %s unreadable: %s", name, exc)
            return []
        tracks = data.get("tracks", [])
        if not isinstance(tracks, list):
            log.warning("playlist %s: 'tracks' is not a list", name)
            return []
        out = []
        for t in tracks:
            rel = clean_rel(t) if isinstance(t, str) else None
            if rel is None:
                log.warning("playlist %s: ignoring bad entry %r", name, t)
            else:
                out.append(rel)
        return out

    def save(self, name: str, tracks: List[str]) -> None:
        """Atomic write (temp file + rename). Raises OSError (e.g. read-only stick)."""
        os.makedirs(self.dir, exist_ok=True)
        final = self.path(name)
        tmp = os.path.join(self.dir, f".{name}.toml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(dump(tracks))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)

    def remove(self, name: str, rel: str) -> bool:
        """Remove a track. False if it wasn't in the playlist."""
        tracks = self.load(name)
        if rel not in tracks:
            return False
        self.save(name, [t for t in tracks if t != rel])
        return True

    def contains(self, name: str, rel: str) -> bool:
        return rel in self.load(name)

    def add(self, name: str, rel: str) -> bool:
        """Append a track. False if it's already in the playlist."""
        tracks = self.load(name)
        if rel in tracks:
            return False
        tracks.append(rel)
        self.save(name, tracks)
        return True
