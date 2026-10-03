import os
import pty
import select
import sys
import time

from conftest import make_tree
import pctool
from saas.core.playlists import PlaylistStore

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stick(tmp_path):
    root = tmp_path / "stick"
    make_tree(str(root), ["Rock/10 c.mp3", "Rock/2 b.mp3", "Rock/Live/1 x.flac", "Rock/cover.jpg",
                          "Pop/a.ogg", ".Trash/z.mp3"])
    return str(root)


def test_model_browse_add_reorder(tmp_path):
    m = pctool.Model(stick(tmp_path))
    assert m.current is None and m.entries() == [("Pop", True), ("Rock", True)]
    m.create("Road: trip")
    assert m.current == "Road_ trip"
    m.enter("Rock")
    assert m.entries() == [("Live", True), ("2 b.mp3", False), ("10 c.mp3", False)]
    assert m.add("2 b.mp3", False) == 1 and m.add("2 b.mp3", False) == 0
    m.up()
    assert m.add("Rock", True) == 2          # recursive, natural order, skips the dup
    assert m.tracks == ["Rock/2 b.mp3", "Rock/10 c.mp3", "Rock/Live/1 x.flac"]
    assert m.move(2, -1) == 1
    m.remove(0)
    assert PlaylistStore(m.root).load("Road_ trip") == ["Rock/Live/1 x.flac", "Rock/10 c.mp3"]
    m.create("Chill")
    m.select(+1)
    assert m.current == "Road_ trip"
    m.delete_current()
    assert m.names == ["Chill"]


def test_find_sticks(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda1 /tmp/not-media vfat rw 0 0\n/dev/sdb1 /media/does\\040not\\040exist vfat rw 0 0\n"
                      "proc /proc proc rw 0 0\n")
    assert pctool.find_sticks(str(mounts)) == []          # wrong prefix / not existing
    assert pctool._unescape_mount("/media/My\\040Stick") == "/media/My Stick"


def test_tui_smoke(tmp_path):
    """Drive the real curses UI through a pty: new playlist, add a folder, quit."""
    root = stick(tmp_path)
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm"
        os.environ.setdefault("LANG", "C.UTF-8")
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "pctool.py"), root])

    def pump(t):
        end = time.time() + t
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    os.read(fd, 65536)
                except OSError:
                    return

    pump(1.0)
    for keys in ("n", "Party\n", "j", "a", "q"):      # j -> "Rock/", a -> add whole folder
        os.write(fd, keys.encode())
        pump(0.4)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert PlaylistStore(root).load("Party") == ["Rock/2 b.mp3", "Rock/10 c.mp3", "Rock/Live/1 x.flac"]
