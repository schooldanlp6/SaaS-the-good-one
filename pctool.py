#!/usr/bin/env python3
"""pctool - minimal playlist editor for a SaaS USB stick (runs on your PC).

    python3 pctool.py                 # finds a mounted stick by itself
    python3 pctool.py /run/media/dan/STICK

Playlists are saved on the stick as DLP6/Playlists/<name>.toml right away on
every change. Standard library only (curses).

Keys  (Tab switches between the two panes)
  browser   up/down j/k  PgUp/PgDn   Enter/right/l open   left/h/Backspace up
            a  add song (or a whole folder, recursively) to the playlist
  playlist  up/down j/k   d/Del remove   K/J move entry up/down
  anywhere  [ ]  previous/next playlist    n  new playlist    X  delete playlist
            q  quit
"""
from __future__ import annotations

import argparse
import curses
import locale
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from saas.core.config import DEFAULTS  # noqa: E402
from saas.core.library import natural_key  # noqa: E402
from saas.core.playlists import DEFAULT_DIR, PlaylistStore, sanitize_name  # noqa: E402

EXTENSIONS = {e.lower() for e in DEFAULTS["library"]["extensions"]}
_MOUNT_PREFIXES = ("/media/", "/run/media/", "/mnt/")


# --- finding the stick ----------------------------------------------------------
def _unescape_mount(path: str) -> str:
    # /proc/mounts escapes spaces etc. as \040 style octal
    out, i = [], 0
    while i < len(path):
        if path[i] == "\\" and i + 3 < len(path) and path[i + 1:i + 4].isdigit():
            out.append(chr(int(path[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def find_sticks(mounts_file: str = "/proc/mounts") -> List[str]:
    """Mounted removable-looking filesystems; ones that already have DLP6/ first."""
    found = []
    try:
        with open(mounts_file) as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1:
                    mp = _unescape_mount(parts[1])
                    if mp.startswith(_MOUNT_PREFIXES) and os.path.isdir(mp):
                        found.append(mp)
    except OSError:
        pass
    found.sort(key=lambda m: (not os.path.isdir(os.path.join(m, "DLP6")), m))
    return found


# --- model (no curses, testable) -----------------------------------------------------
def is_track(name: str) -> bool:
    stem, dot, ext = name.rpartition(".")
    return bool(dot and stem) and not name.startswith(".") and ext.lower() in EXTENSIONS


class Model:
    def __init__(self, root: str, subdir: str = DEFAULT_DIR) -> None:
        self.root = os.path.abspath(root)
        self.store = PlaylistStore(self.root, subdir)
        self._hidden = subdir.replace("\\", "/").strip("/").split("/")[0]  # e.g. "DLP6"
        self.cwd = ""                      # relative to root, "/" separated
        self.names = self.store.names()
        self.current: Optional[str] = self.names[0] if self.names else None
        self.tracks: List[str] = self.store.load(self.current) if self.current else []

    # browsing
    def entries(self) -> List[Tuple[str, bool]]:
        """(name, is_dir) of the current directory: folders first, then songs."""
        path = os.path.join(self.root, *self.cwd.split("/")) if self.cwd else self.root
        dirs, songs = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith(".") or (not self.cwd and e.name == self._hidden):
                        continue
                    try:
                        if e.is_dir():
                            dirs.append(e.name)
                        elif is_track(e.name):
                            songs.append(e.name)
                    except OSError:
                        continue
        except OSError:
            pass
        dirs.sort(key=natural_key)
        songs.sort(key=natural_key)
        return [(d, True) for d in dirs] + [(s, False) for s in songs]

    def rel(self, name: str) -> str:
        return f"{self.cwd}/{name}" if self.cwd else name

    def enter(self, name: str) -> None:
        self.cwd = self.rel(name)

    def up(self) -> Optional[str]:
        """Go to the parent; returns the folder we came from (to put the cursor on it)."""
        if not self.cwd:
            return None
        head, _, tail = self.cwd.rpartition("/")
        self.cwd = head
        return tail

    # playlists
    def select(self, delta: int) -> None:
        if self.names:
            i = self.names.index(self.current) if self.current in self.names else 0
            self.current = self.names[(i + delta) % len(self.names)]
            self.tracks = self.store.load(self.current)

    def create(self, raw: str) -> Optional[str]:
        name = sanitize_name(raw)
        if not name:
            return None
        if name not in self.names:
            self.store.save(name, [])
            self.names = self.store.names()
        self.current = name
        self.tracks = self.store.load(name)
        return name

    def delete_current(self) -> None:
        if self.current:
            os.remove(self.store.path(self.current))
            self.names = self.store.names()
            self.current = self.names[0] if self.names else None
            self.tracks = self.store.load(self.current) if self.current else []

    def _save(self) -> None:
        self.store.save(self.current, self.tracks)

    def add(self, name: str, is_dir: bool) -> int:
        """Add a song, or every song below a folder (in natural order). Returns count added."""
        if self.current is None:
            raise RuntimeError("no playlist - press n to create one")
        new = []
        if is_dir:
            base = os.path.join(self.root, *self.rel(name).split("/"))
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=natural_key)
                for f in sorted(filter(is_track, filenames), key=natural_key):
                    new.append(os.path.relpath(os.path.join(dirpath, f), self.root).replace(os.sep, "/"))
        else:
            new.append(self.rel(name))
        added = [t for t in dict.fromkeys(new) if t not in self.tracks]
        if added:
            self.tracks.extend(added)
            self._save()
        return len(added)

    def remove(self, index: int) -> None:
        del self.tracks[index]
        self._save()

    def move(self, index: int, delta: int) -> int:
        j = index + delta
        if 0 <= j < len(self.tracks):
            self.tracks[index], self.tracks[j] = self.tracks[j], self.tracks[index]
            self._save()
            return j
        return index

    def exists(self, rel: str) -> bool:
        return os.path.isfile(os.path.join(self.root, *rel.split("/")))


# --- curses UI ---------------------------------------------------------------------
class UI:
    def __init__(self, scr, model: Model) -> None:
        self.scr, self.m = scr, model
        self.focus = 0               # 0 = browser, 1 = playlist
        self.cur = [0, 0]
        self.top = [0, 0]
        self.msg = "Tab: switch pane   a: add   n: new playlist   q: quit"

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w - 1:
            try:
                self.scr.addnstr(y, x, text, w - 1 - x, attr)
            except curses.error:
                pass

    def draw_list(self, pane: int, x: int, width: int, rows: List[str], marks: List[int]) -> None:
        h = self.scr.getmaxyx()[0]
        height = h - 4
        n = len(rows)
        self.cur[pane] = max(0, min(self.cur[pane], n - 1))
        if self.cur[pane] < self.top[pane]:
            self.top[pane] = self.cur[pane]
        if self.cur[pane] >= self.top[pane] + height:
            self.top[pane] = self.cur[pane] - height + 1
        for i in range(height):
            idx = self.top[pane] + i
            if idx >= n:
                break
            attr = curses.A_DIM if idx in marks else 0
            if idx == self.cur[pane]:
                attr |= curses.A_REVERSE if self.focus == pane else curses.A_UNDERLINE
            self.put(2 + i, x, rows[idx][:width - 1].ljust(width - 1), attr)

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        half = w // 2
        m = self.m
        pl = f"{m.current} ({m.names.index(m.current) + 1}/{len(m.names)})" if m.current else "(none - press n)"
        self.put(0, 0, f" SaaS pctool  {m.root}", curses.A_BOLD)
        self.put(1, 0, f" /{m.cwd}"[:half - 1], curses.A_BOLD if self.focus == 0 else 0)
        self.put(1, half, f" Playlist: {pl}", curses.A_BOLD if self.focus == 1 else 0)
        entries = m.entries()
        self.draw_list(0, 0, half, [f"{n}/" if d else f"  {n}" for n, d in entries], [])
        rows = [f"{i + 1:3} {t}" + ("" if m.exists(t) else "  (missing)") for i, t in enumerate(m.tracks)]
        missing = [i for i, t in enumerate(m.tracks) if not m.exists(t)]
        self.draw_list(1, half, w - half, rows, missing)
        self.put(h - 2, 0, "-" * (w - 1))
        self.put(h - 1, 0, " " + self.msg)
        self.scr.refresh()

    def prompt(self, label: str) -> str:
        h = self.scr.getmaxyx()[0]
        self.put(h - 1, 0, " " * (self.scr.getmaxyx()[1] - 1))
        self.put(h - 1, 0, f" {label}")
        curses.echo()
        curses.curs_set(1)
        try:
            raw = self.scr.getstr(h - 1, len(label) + 1, 100)
        finally:
            curses.noecho()
            curses.curs_set(0)
        return raw.decode(locale.getpreferredencoding(False) or "utf-8", "replace").strip()

    def run(self) -> None:
        curses.curs_set(0)
        self.scr.keypad(True)
        while True:
            self.draw()
            k = self.scr.get_wch()
            if k in ("q", "Q"):
                return
            try:
                self.key(k)
            except Exception as exc:  # e.g. stick read-only / removed
                self.msg = f"error: {exc}"

    def key(self, k) -> None:
        m, p = self.m, self.focus
        page = max(1, self.scr.getmaxyx()[0] - 5)
        if k == "\t":
            self.focus = 1 - self.focus
        elif k in (curses.KEY_UP, "k"):
            self.cur[p] -= 1
        elif k in (curses.KEY_DOWN, "j"):
            self.cur[p] += 1
        elif k == curses.KEY_PPAGE:
            self.cur[p] -= page
        elif k == curses.KEY_NPAGE:
            self.cur[p] += page
        elif k == "]":
            m.select(+1); self.cur[1] = 0; self.msg = f"playlist: {m.current}"
        elif k == "[":
            m.select(-1); self.cur[1] = 0; self.msg = f"playlist: {m.current}"
        elif k == "n":
            name = m.create(self.prompt("New playlist name: "))
            self.msg = f"playlist: {name}" if name else "cancelled"
            self.cur[1] = 0
        elif k == "X" and m.current:
            if self.prompt(f"Delete playlist '{m.current}'? type yes: ") == "yes":
                gone = m.current
                m.delete_current()
                self.msg = f"deleted {gone}"
        elif p == 0:
            self.browser_key(k)
        else:
            self.playlist_key(k)
        self.cur[p] = max(0, self.cur[p])

    def browser_key(self, k) -> None:
        m = self.m
        entries = m.entries()
        sel = entries[self.cur[0]] if 0 <= self.cur[0] < len(entries) else None
        if k in ("\n", curses.KEY_ENTER, curses.KEY_RIGHT, "l") and sel and sel[1]:
            m.enter(sel[0])
            self.cur[0] = self.top[0] = 0
        elif k in (curses.KEY_LEFT, "h", curses.KEY_BACKSPACE, "\x7f", "\b"):
            came_from = m.up()
            if came_from is not None:
                names = [n for n, _ in m.entries()]
                self.cur[0] = names.index(came_from) if came_from in names else 0
        elif k == "a" and sel:
            n = m.add(*sel)
            self.msg = f"added {n} song(s) to {m.current}" if n else "already in the playlist"
            self.cur[1] = len(m.tracks) - 1
            if not sel[1]:
                self.cur[0] += 1  # next song, for adding sequentially

    def playlist_key(self, k) -> None:
        m, i = self.m, self.cur[1]
        if not m.tracks:
            return
        if k in ("d", curses.KEY_DC):
            gone = m.tracks[i]
            m.remove(i)
            self.msg = f"removed {gone.rsplit('/', 1)[-1]}"
        elif k == "K":
            self.cur[1] = m.move(i, -1)
        elif k == "J":
            self.cur[1] = m.move(i, +1)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Edit SaaS playlists on a USB stick")
    ap.add_argument("stick", nargs="?", help="mount point of the stick (default: find it)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help=f"playlist dir on the stick (default {DEFAULT_DIR})")
    args = ap.parse_args(argv)

    root = args.stick
    if root is None:
        sticks = find_sticks()
        if not sticks:
            print("no mounted stick found under /media, /run/media or /mnt - pass its path", file=sys.stderr)
            return 1
        if len(sticks) == 1:
            root = sticks[0]
        else:
            for i, s in enumerate(sticks, 1):
                print(f"  {i}) {s}")
            try:
                root = sticks[int(input("which one? ")) - 1]
            except (ValueError, IndexError, EOFError):
                return 1
    if not os.path.isdir(root):
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    locale.setlocale(locale.LC_ALL, "")
    model = Model(root, args.dir)
    curses.wrapper(lambda scr: UI(scr, model).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
