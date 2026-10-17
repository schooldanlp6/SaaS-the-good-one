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
