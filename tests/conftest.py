import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from saas.backends.base import AudioBackend  # noqa: E402


class FakeBackend(AudioBackend):
    """Records calls; position is whatever the test sets."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.current = None
        self.paused = False
        self.pos = None
        self.volume = None

    def play(self, path, start=0.0, paused=False):
        self.calls.append(("play", path, start, paused))
        self.current, self.paused, self.pos = path, paused, start

    def set_paused(self, paused):
        self.calls.append(("pause", paused))
        self.paused = paused

    def stop(self):
        self.calls.append(("stop",))
        self.current, self.pos = None, None

    def seek(self, seconds):
        self.calls.append(("seek", seconds))
        self.pos = seconds

    def position(self):
        return self.pos

    def set_volume(self, volume):
        self.volume = volume


def make_tree(root, spec):
    for rel in spec:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "music"
    make_tree(str(root), [
        "01 Album A/1 a.flac",
        "01 Album A/2 b.flac",
        "01 Album A/10 c.flac",
        "01 Album A/cover.jpg",
        "02 Album B/x.mp3",
        "Empty/readme.txt",
    ])
    return str(root)
