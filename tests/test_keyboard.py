"""Keyboard input through a real pty: '?' reprints the keys, mapping 'help' in config works."""
import os
import pty
import select
import sys
import time

from saas.core.config import validate
from saas.core.events import Bus
from saas.inputs.keyboard import KeyboardInput

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_help_text_lists_every_key():
    text = KeyboardInput(Bus()).help_text()
    for piece in ("space: play/pause", "?: this help", "q: quit", "*: next stick", "U: update"):
        assert piece in text
    assert all(len(line) <= 78 for line in text.splitlines())
    assert "=:" not in text                               # '=' no longer means volume up


def test_help_can_be_remapped_in_config():
    cfg = validate({"audio": {"backend": "dummy"}, "library": {"media_root": "/m"},
                    "inputs": {"keyboard": {"enabled": True, "keys": {"h": "help"}}}})
    assert cfg["inputs"]["keyboard"]["keys"] == {"h": "help"}


def test_question_mark_reprints_keys(tmp_path):
    (tmp_path / "m" / "A").mkdir(parents=True)
    (tmp_path / "m" / "A" / "1.mp3").write_bytes(b"")
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[audio]\nbackend = "dummy"\n[library]\nmedia_root = "{tmp_path}/m"\n'
                   f'[sources.usb]\nlayout = "single"\nrequire_mountpoint = false\n'
                   f'[inputs.keyboard]\nenabled = true\n[outputs.status]\ndir = "{tmp_path}/run"\n'
                   f'[update]\nenabled = false\n')
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "main.py"), "-c", str(cfg),
                                  "--log-level", "WARNING"])

    def read(t):
        buf, end = b"", time.time() + t
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    break
        return buf.decode(errors="replace")

    first = read(1.5)
    os.write(fd, b"?")
    again = read(0.6)
    os.write(fd, b"q")
    read(1.0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert first.count("keys:") == 1 and "keys:" in again and "?: this help" in again


def test_arrow_keys_dont_trigger_folder_down(tmp_path):
    """0.6.4 fix: ESC [ A used to be read byte by byte and the '[' meant folder_down."""
    import threading
    from saas.core.events import Event
    r, w = os.pipe()
    kb = KeyboardInput(Bus())
    kb._fd = r
    got = []
    kb.bus.emit = lambda e, data=None: got.append(e)
    t = threading.Thread(target=kb._run, daemon=True)
    t.start()
    for seq in (b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D", b"\x1bOA", b"\x1b[3~", b"\x1b[1;5C", b"["):
        os.write(w, seq)
        time.sleep(0.08)
    os.close(w)
    t.join(2)
    assert got == [Event.VOL_UP, Event.VOL_DOWN, Event.NEXT, Event.PREV, Event.VOL_UP, Event.FOLDER_DOWN]
