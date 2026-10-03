import json
import os
import time
import tomllib

import pytest

from conftest import make_tree
from saas.core.events import Bus, Event
from saas.core.library import Library
from saas.core.player import Player
from saas.core.playlists import PlaylistStore, clean_rel, dump, sanitize_name
from saas.core.resume import ResumeStore
from saas.core.state import PLAYING, PlayerState  # noqa: F401
from saas.outputs.status import StatusOutput, build_line, build_status

PL = "DLP6/Playlists"


def test_dump_roundtrips_weird_names():
    tracks = ['a/b "quoted".mp3', "back\\slash/x.flac", "Ümlaut ♪/ü.ogg", "tab\there.mp3"]
    assert tomllib.loads(dump(tracks))["tracks"] == tracks


def test_clean_rel_and_sanitize():
    assert clean_rel("a\\b/./c.mp3") == "a/b/c.mp3"
    assert clean_rel("../etc/passwd") is None and clean_rel("/abs.mp3") is None and clean_rel("") is None
    assert sanitize_name('Road/trip: "best"?') == "Road_trip_ _best__"


def test_store_add_save_load(tmp_path):
    st = PlaylistStore(str(tmp_path), PL)
    assert st.names() == [] and st.load("x") == []
    assert st.add("Road", "A/1.mp3") is True
    assert st.add("Road", "A/1.mp3") is False      # no duplicates
    st.add("Road", "B/2.mp3")
    st.add("2 Chill", "B/2.mp3")
    st.add("10 Late", "B/2.mp3")
    assert st.load("Road") == ["A/1.mp3", "B/2.mp3"]
    assert st.names() == ["2 Chill", "10 Late", "Road"]
    assert os.path.isfile(tmp_path / "DLP6" / "Playlists" / "Road.toml")
    assert not any(n.endswith(".tmp") for n in os.listdir(tmp_path / "DLP6" / "Playlists"))


def test_store_ignores_garbage(tmp_path):
    d = tmp_path / "DLP6" / "Playlists"
    d.mkdir(parents=True)
    (d / "bad.toml").write_text("this is [not toml")
    (d / "evil.toml").write_text('tracks = ["../../etc/passwd", 5, "ok/x.mp3"]')
    st = PlaylistStore(str(tmp_path), PL)
    assert st.load("bad") == []
    assert st.load("evil") == ["ok/x.mp3"]


@pytest.fixture
def stick(tmp_path):
    root = tmp_path / "stick"
    make_tree(str(root), ["A/1.mp3", "A/2.mp3", "B/x.mp3"])
    return str(root)


def test_library_playlists_as_virtual_folders(stick):
    st = PlaylistStore(stick, PL)
    st.save("Mix", ["B/x.mp3", "gone/missing.mp3", "A/1.mp3"])
    st.save("Empty", ["gone/nothing.mp3"])
    lib = Library(stick, ["mp3"], st)
    lib.scan()
    assert lib.folders == ["A", "B", "playlist:Mix"]      # empty/missing-only playlists hidden
    assert lib.tracks(2) == ["B/x.mp3", "A/1.mp3"]        # missing entries skipped
    assert lib.folder_label(2) == "[PL] Mix" and lib.is_playlist(2)
    assert lib.track_label(2, 0) == "x.mp3"
    assert lib.track_path(2, 1) == os.path.join(stick, "A", "1.mp3")
    plain = Library(stick, ["mp3"]); plain.scan()
    assert plain.fingerprint == lib.fingerprint          # playlists don't change media identity


def make_player(backend, **kw):
    kw.setdefault("extensions", ["mp3"])
    kw.setdefault("playlist_dir", PL)
    return Player(Bus(), backend, **kw)


def test_toggle_cycle_and_play_playlist(backend, stick):
    p = make_player(backend)
    assert p.state.active_playlist == "Favorites"
    p.handle(Event.MEDIA_ADDED, stick)
    assert p.state.folder_count == 2
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)                              # A/2.mp3
    assert not p.state.in_active_playlist
    p.handle(Event.PLAYLIST_ADD)
    assert p.state.notice == "Saved to Favorites" and p.state.in_active_playlist
    assert PlaylistStore(stick, PL).load("Favorites") == ["A/2.mp3"]
    assert p.state.folder_count == 3                  # Favorites appeared as a folder
    p.handle(Event.PLAYLIST_ADD)                      # same button again = remove
    assert p.state.notice == "Removed from Favorites" and not p.state.in_active_playlist
    assert PlaylistStore(stick, PL).load("Favorites") == [] and p.state.folder_count == 2
    p.handle(Event.PLAYLIST_ADD)

    PlaylistStore(stick, PL).save("Road", ["B/x.mp3"])
    p.handle(Event.PLAYLIST_NEXT)
    assert p.state.active_playlist == "Road" and p.state.notice == "Playlist: Road"
    assert not p.state.in_active_playlist             # 2.mp3 isn't in Road
    p.handle(Event.PLAYLIST_NEXT)
    assert p.state.active_playlist == "Favorites" and p.state.in_active_playlist

    p.handle(Event.VIEW_TOGGLE)
    assert p.state.folder_name == "[PL] Favorites" and p.state.in_playlist
    assert backend.calls[-1][1] == os.path.join(stick, "A", "2.mp3")


def test_flags_follow_the_track(backend, stick):
    PlaylistStore(stick, PL).save("Favorites", ["A/2.mp3"])
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.PLAY_PAUSE)
    assert not p.state.in_active_playlist
    p.handle(Event.NEXT)
    assert p.state.in_active_playlist
    p.handle(Event.NEXT)                              # B/x.mp3
    assert not p.state.in_active_playlist


def test_like_toggle_is_separate_and_not_cycled(backend, stick):
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.LIKE)
    assert p.state.notice == "Liked" and p.state.liked and not p.state.in_active_playlist
    assert PlaylistStore(stick, PL).load("Liked Songs") == ["A/1.mp3"]
    p.handle(Event.NEXT)
    assert not p.state.liked
    p.handle(Event.PREV)
    assert p.state.liked
    for _ in range(3):
        p.handle(Event.PLAYLIST_NEXT)
        assert p.state.active_playlist != "Liked Songs"
    p.handle(Event.LIKE)
    assert p.state.notice == "Unliked" and not p.state.liked
    # Liked Songs is still playable like any playlist while it has songs
    p.handle(Event.LIKE)
    assert "playlist:Liked Songs" in p.library.folders


def test_removing_playing_song_from_played_playlist(backend, stick):
    PlaylistStore(stick, PL).save("Favorites", ["A/1.mp3", "B/x.mp3"])
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.VIEW_TOGGLE)                       # [PL] Favorites
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)                              # B/x.mp3
    p.handle(Event.PLAYLIST_ADD)                      # remove it while playing it
    assert p.state.status == PLAYING and p.state.folder_name == "[PL] Favorites"
    assert p.state.track_count == 1


def test_emptied_playlist_vanishes_safely(backend, stick):
    PlaylistStore(stick, PL).save("Favorites", ["A/1.mp3"])
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.VIEW_TOGGLE)                       # [PL] Favorites
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.PLAYLIST_ADD)                      # removes its only song
    assert "playlist:Favorites" not in p.library.folders
    assert p.state.view == "folders" and p.state.folder_name == "A"   # no playlists left
    assert p.state.status == PLAYING                  # the song keeps playing


def test_add_without_media_and_readonly(backend, stick):
    p = make_player(backend)
    p.handle(Event.PLAYLIST_ADD)
    assert p.state.notice == "nothing playing"
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.PLAY_PAUSE)
    os.chmod(stick, 0o555)
    try:
        if os.geteuid() == 0:
            pytest.skip("root ignores permissions")
        p.handle(Event.PLAYLIST_ADD)
        assert p.state.notice.startswith("cannot write Favorites")
        assert p.state.status == PLAYING
    finally:
        os.chmod(stick, 0o755)


def test_playlists_disabled(backend, stick):
    p = make_player(backend, playlist_dir=None)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.PLAYLIST_ADD)
    assert p.state.active_playlist == "" and not os.path.exists(os.path.join(stick, "DLP6"))


def test_resume_active_playlist_and_playlist_position(backend, stick, tmp_path):
    PlaylistStore(stick, PL).save("Road", ["B/x.mp3", "A/2.mp3"])
    store = ResumeStore(str(tmp_path / "s.json"))
    p = make_player(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.PLAYLIST_NEXT)                     # Favorites -> ... cycles to Road? names = [Road, Favorites]
    while p.state.active_playlist != "Road":
        p.handle(Event.PLAYLIST_NEXT)
    p.handle(Event.VIEW_TOGGLE)                       # [PL] Road
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    backend.pos = 7.0
    p.close()
    b2 = type(backend)()
    p2 = make_player(b2, resume=store)
    assert p2.state.active_playlist == "Road"
    p2.handle(Event.MEDIA_ADDED, stick)
    assert p2.state.folder_name == "[PL] Road" and p2.state.track_name == "2.mp3"


def test_track_info_and_reset(backend, stick):
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.TRACK_INFO, {"title": "Song", "artist": "Band", "album": "LP", "duration": 201.5})
    assert (p.state.title, p.state.artist, p.state.duration) == ("Song", "Band", 201.5)
    p.handle(Event.NEXT)
    assert p.state.title == "" and p.state.duration == 0.0   # cleared until the new tags arrive


def test_status_line_and_json():
    s = PlayerState(status="playing", media=True, folder_count=2, view_index=1, view_count=2,
                    folder_name="A", track_count=3,
                    track_name="01 x.flac", title="Song", artist="Band", album="LP", duration=125,
                    active_playlist="Road", volume=70)
    d = build_status(s, 61.0)
    assert d["track"]["title"] == "Song" and d["folder"]["index"] == 1 and d["track"]["position"] == 61.0
    assert build_line(d) == "> Band - Song  [1:01/2:05]  LP  (pl: Road)"
    s2 = PlayerState(status="playing", media=True, track_name="02 untagged.mp3", track_count=1, folder_count=1)
    assert build_line(build_status(s2, None)) == "> 02 untagged"
    assert build_line(build_status(PlayerState(), None)) == "-- no media  vol 80"


def test_status_files_written_and_refreshed(tmp_path):
    pos = {"v": 1.0}
    out = StatusOutput(str(tmp_path / "run"), lambda: pos["v"], interval=0.2)
    out.update(PlayerState(status="playing", media=True, folder_count=1, track_count=1, track_name="a.mp3"))
    d = json.loads((tmp_path / "run" / "now.json").read_text())
    assert d["status"] == "playing" and d["track"]["position"] == 1.0
    pos["v"] = 5.0
    time.sleep(0.5)                                         # periodic refresh while playing
    assert json.loads((tmp_path / "run" / "now.json").read_text())["track"]["position"] == 5.0
    assert (tmp_path / "run" / "now.txt").read_text().startswith("> a")
    out.close()
    d = json.loads((tmp_path / "run" / "now.json").read_text())
    assert d["status"] == "idle" and d["notice"] == "player stopped"


def test_view_toggle(backend, stick):
    st = PlaylistStore(stick, PL)
    st.save("Road", ["B/x.mp3", "A/1.mp3"])
    st.save("Chill", ["A/2.mp3"])
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    assert p.state.view == "folders" and (p.state.view_index, p.state.view_count) == (1, 2)
    # folder up/down stays inside the folders
    p.handle(Event.FOLDER_UP); p.handle(Event.FOLDER_UP)
    assert p.state.folder_name == "A"
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)                                   # A/2.mp3
    backend.pos = 42.0
    p.handle(Event.VIEW_TOGGLE)
    assert p.state.view == "playlists" and p.state.notice == "Playlists: Chill"
    assert (p.state.view_index, p.state.view_count) == (1, 2) and p.state.status == PLAYING
    # folder up/down stays inside the playlists
    p.handle(Event.FOLDER_UP)
    assert p.state.folder_name == "[PL] Road"
    p.handle(Event.FOLDER_UP)
    assert p.state.folder_name == "[PL] Chill"
    p.handle(Event.FOLDER_UP)
    p.handle(Event.NEXT)                                   # Road, 2nd song
    backend.pos = 7.0
    # back to the folders: same folder, same song, same second
    p.handle(Event.VIEW_TOGGLE)
    assert p.state.notice == "Folders: A" and p.state.track_name == "2.mp3"
    assert plays(backend)[-1][1:3] == (os.path.join(stick, "A", "2.mp3"), 42.0)
    # and back to the playlists where we left them
    p.handle(Event.VIEW_TOGGLE)
    assert p.state.folder_name == "[PL] Road" and p.state.track_index == 1
    assert plays(backend)[-1][2] == 7.0


def test_view_toggle_keeps_pause_and_stop(backend, stick):
    PlaylistStore(stick, PL).save("Road", ["B/x.mp3"])
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.VIEW_TOGGLE)                            # stopped: nothing starts
    assert p.state.status == "stopped" and plays(backend) == []
    p.handle(Event.PLAY_PAUSE); p.handle(Event.PLAY_PAUSE)  # paused
    p.handle(Event.VIEW_TOGGLE)
    assert p.state.status == "paused" and plays(backend)[-1][3] is True


def test_view_toggle_without_playlists(backend, stick):
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.VIEW_TOGGLE)
    assert p.state.notice == "No playlists" and p.state.view == "folders"


def test_end_of_playlist_continues_with_next_playlist(backend, stick):
    st = PlaylistStore(stick, PL)
    st.save("A1", ["B/x.mp3"])
    st.save("A2", ["A/1.mp3"])
    p = make_player(backend)
    p.handle(Event.MEDIA_ADDED, stick)
    p.handle(Event.VIEW_TOGGLE)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.TRACK_END, "eof")
    assert p.state.folder_name == "[PL] A2"                 # not back into the folders
    p.handle(Event.TRACK_END, "eof")
    assert p.state.folder_name == "[PL] A1"


def plays(backend):
    return [c for c in backend.calls if c[0] == "play"]


def test_status_dir_refuses_foreign_or_symlink(tmp_path):
    from saas.outputs.status import ensure_private_dir
    d = tmp_path / "saas"
    ensure_private_dir(str(d))                        # creates it
    assert d.is_dir() and (d.stat().st_mode & 0o777) == 0o755
    os.chmod(d, 0o777)
    ensure_private_dir(str(d))                        # fixes a too-open mode on our own dir
    assert (d.stat().st_mode & 0o777) == 0o755
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(PermissionError, match="not a directory"):
        ensure_private_dir(str(link))
    if os.geteuid() == 0:                             # can only fake a foreign owner as root
        other = tmp_path / "other"
        other.mkdir()
        os.chown(other, 12345, 12345)
        with pytest.raises(PermissionError, match="belongs to uid 12345"):
            ensure_private_dir(str(other))


def test_status_output_creates_its_dir_and_survives_refusal(tmp_path):
    out = StatusOutput(str(tmp_path / "shm" / "saas"), lambda: None, interval=60)
    out.update(PlayerState())
    assert (tmp_path / "shm" / "saas" / "now.vars").exists()
    (tmp_path / "bad").symlink_to(tmp_path)
    bad = StatusOutput(str(tmp_path / "bad"), lambda: None, interval=60)
    bad.update(PlayerState())                         # logs a warning, doesn't raise
    assert not (tmp_path / "now.json").exists()
