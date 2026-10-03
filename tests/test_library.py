import os

from conftest import make_tree
from saas.core.library import Library, natural_key


def test_natural_sort():
    names = ["10 c", "2 b", "1 a", "Track 9", "track 10"]
    assert sorted(names, key=natural_key) == ["1 a", "2 b", "10 c", "Track 9", "track 10"]


def test_scan_finds_only_folders_with_tracks(music):
    lib = Library(music, ["flac", "mp3"])
    assert lib.scan() == ["01 Album A", "02 Album B"]
    assert lib.tracks(0) == ["1 a.flac", "2 b.flac", "10 c.flac"]
    assert lib.track_path(1, 0) == os.path.join(music, "02 Album B", "x.mp3")


def test_root_junk_and_case(tmp_path):
    root = str(tmp_path)
    make_tree(root, [
        "loose.MP3",
        "._loose.mp3",
        ".hidden/a.mp3",
        "System Volume Information/x.mp3",
        "Disc 10/a.mp3",
        "Disc 2/a.mp3",
        "Disc 2/sub/b.ogg",
    ])
    lib = Library(root, ["mp3", "ogg"])
    assert lib.scan() == ["", "Disc 2", os.path.join("Disc 2", "sub"), "Disc 10"]
    assert lib.tracks(0) == ["loose.MP3"]
    assert lib.folder_label(0) == os.path.basename(root)


def test_fingerprint_changes_with_content(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    make_tree(a, ["X/1.mp3"])
    make_tree(b, ["Y/1.mp3"])
    la, lb = Library(a, ["mp3"]), Library(b, ["mp3"])
    la.scan(); lb.scan()
    assert la.fingerprint and la.fingerprint != lb.fingerprint
    la2 = Library(a, ["mp3"]); la2.scan()
    assert la.fingerprint == la2.fingerprint
