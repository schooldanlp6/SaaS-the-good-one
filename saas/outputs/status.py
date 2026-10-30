"""Now-playing + status for the rest of the system (dashboards, scripts).

Writes the files into a tmpfs directory (default /dev/shm/saas: tmpfs on every
Linux, and SaaS can create it itself - no root, no init system needed; the SD
card is never touched), atomically (temp file + rename), on every state change
and every `interval` seconds while playing (to keep the position fresh):

  now.json   everything, machine-readable
  now.txt    one line for humans:  > Artist - Title  [1:23/4:56]  Album
  now.vars   KEY=value lines for shell scripts (see saas-now.sh). Read it with
             `while IFS='=' read -r k v` - do NOT `source` it from a root shell,
             it's written by the unprivileged saas user.

Readers: `cat /dev/shm/saas/now.txt`, `/opt/saas/saas-now.sh`, or poll
the file's mtime. If position isn't fresh enough, extrapolate it yourself from
"position" + "position_at" while "status" == "playing".
"""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from typing import Callable, Optional

from ..core.state import PAUSED, PLAYING, PlayerState
from .base import Output

log = logging.getLogger(__name__)

_ICON = {"playing": ">", "paused": "||", "stopped": "[]", "idle": "--"}


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def build_status(s: PlayerState, position: Optional[float]) -> dict:
    return {
        "status": "standby" if s.standby else s.status,
        "standby": s.standby,
        "media": s.media,
        "volume": s.volume,
        "medium": {"name": s.medium_name, "index": s.medium_index, "count": s.medium_count},
        "view": s.view,
        # index/count are within the current view (folders or playlists)
        "folder": {"index": s.view_index if s.media else 0, "count": s.view_count if s.media else 0,
                   "name": s.folder_name, "is_playlist": s.in_playlist},
        "track": {"index": s.track_index + 1 if s.media else 0, "count": s.track_count,
                  "file": s.track_name, "title": s.title or os.path.splitext(s.track_name)[0],
                  "artist": s.artist, "album": s.album, "duration": round(s.duration, 1),
                  "position": round(position, 1) if position is not None else None,
                  "in_active_playlist": s.in_active_playlist, "liked": s.liked},
        "position_at": time.time(),
        "active_playlist": s.active_playlist,
        "can_save": s.can_save,
        "notice": s.notice,
        "notice_time": s.notice_time,
    }


def _flat(v) -> str:
    """One-line value: no newlines/control chars can break the KEY=value format."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if v is None:
        return ""
    if isinstance(v, float):
        v = int(v)
    return "".join(ch if ch >= " " else " " for ch in str(v))


def build_vars(d: dict) -> str:
    t, fo, m = d["track"], d["folder"], d["medium"]
    pairs = [
        ("STATUS", d["status"]), ("MEDIA", d["media"]), ("VOLUME", d["volume"]), ("VIEW", d["view"]),
        ("MEDIUM", m["name"]), ("MEDIUM_INDEX", m["index"]), ("MEDIUM_COUNT", m["count"]),
        ("FOLDER", fo["name"]), ("FOLDER_INDEX", fo["index"]), ("FOLDER_COUNT", fo["count"]),
        ("FOLDER_IS_PLAYLIST", fo["is_playlist"]),
        ("TRACK_INDEX", t["index"]), ("TRACK_COUNT", t["count"]), ("FILE", t["file"]),
        ("TITLE", t["title"]), ("ARTIST", t["artist"]), ("ALBUM", t["album"]),
        ("DURATION", t["duration"]), ("POSITION", t["position"]), ("POSITION_AT", d["position_at"]),
        ("PLAYLIST", d["active_playlist"]), ("CAN_SAVE", d["can_save"]),
        ("IN_PLAYLIST", t["in_active_playlist"]), ("LIKED", t["liked"]),
        ("NOTICE", d["notice"]), ("NOTICE_TIME", d["notice_time"]),
    ]
    return "".join(f"SAAS_{k}={_flat(v)}\n" for k, v in pairs)


def build_line(d: dict) -> str:
    icon = _ICON.get(d["status"], d["status"])
    if not d["media"]:
        line = f"{icon} no media  vol {d['volume']}"
    else:
        t = d["track"]
        who = f"{t['artist']} - {t['title']}" if t["artist"] else t["title"]
        times = ""
        if t["position"] is not None:
            times = f"  [{_mmss(t['position'])}/{_mmss(t['duration'])}]" if t["duration"] else f"  [{_mmss(t['position'])}]"
        album = f"  {t['album']}" if t["album"] else ""
        line = f"{icon} {who}{times}{album}"
    if d["active_playlist"] and d["can_save"]:
        line += f"  (pl: {d['active_playlist']})"
    return line


def ensure_private_dir(path: str) -> None:
    """Create `path` (mode 0755) or accept it only if it's a real directory owned
    by us and not writable by others. /dev/shm is world-writable, so someone
    could plant a directory or symlink there first - we won't write into that."""
    try:
        os.mkdir(path, 0o755)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"{path} is not a directory (symlink?) - remove it")
    if st.st_uid != os.geteuid():
        raise PermissionError(f"{path} belongs to uid {st.st_uid}, not to us (uid {os.geteuid()}) - remove it")
    if st.st_mode & 0o022:
        os.chmod(path, 0o755)


class StatusOutput(Output):
    def __init__(self, directory: str, position: Callable[[], Optional[float]], interval: float = 1.0) -> None:
        self.dir = directory
        self.position = position
        self.interval = interval
        self._state: Optional[PlayerState] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._warned: Optional[bool] = None  # None = nothing written yet
        threading.Thread(target=self._run, name="status-file", daemon=True).start()

    def update(self, state: PlayerState) -> None:
        self._state = state
        self._write()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            st = self._state
            if st is not None and st.status == PLAYING:
                self._write()

    def _write(self) -> None:
        st = self._state
        if st is None:
            return
        pos = None
        if st.status in (PLAYING, PAUSED):
            try:
                pos = self.position()
            except Exception:
                pos = None
        data = build_status(st, pos)
        with self._lock:
            try:
                parent = os.path.dirname(os.path.abspath(self.dir))
                os.makedirs(parent, exist_ok=True)
                ensure_private_dir(self.dir)
                self._atomic("now.json", json.dumps(data, ensure_ascii=False, indent=1) + "\n")
                self._atomic("now.txt", build_line(data) + "\n")
                self._atomic("now.vars", build_vars(data))
                if self._warned is not False:
                    log.info("status files: %s (now.json, now.txt, now.vars)", self.dir)
                self._warned = False
            except OSError as exc:
                if not self._warned:  # don't spam the log every second
                    log.warning("cannot write status files to %s: %s", self.dir, exc)
                    self._warned = True

    def _atomic(self, name: str, text: str) -> None:
        final = os.path.join(self.dir, name)
        tmp = final + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, final)

    def close(self) -> None:
        self._stop.set()
        st = self._state
        if st is not None:  # leave a final "not running" snapshot behind
            self._state = PlayerState(volume=st.volume, active_playlist=st.active_playlist,
                                      notice="player stopped", notice_time=time.time())
            self._write()
