import os
import shutil

import pytest

from saas.core.events import Bus, Event
from saas.core.player import Player
from saas.core.resume import ResumeStore
from saas.core.state import IDLE, PAUSED, PLAYING, STOPPED


def make(backend, **kw):
    kw.setdefault("extensions", ["flac", "mp3"])
    return Player(Bus(), backend, **kw)


def plays(backend):
    return [c for c in backend.calls if c[0] == "play"]


def last_play(backend):
    return os.path.relpath(plays(backend)[-1][1], os.path.dirname(os.path.dirname(plays(backend)[-1][1])))


def test_no_media_is_ignored(backend):
    p = make(backend)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    p.handle(Event.FOLDER_UP)
    assert p.state.status == IDLE and plays(backend) == []


def test_play_pause_cycle(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    assert p.state.status == STOPPED and p.state.track_name == "1 a.flac"
    assert plays(backend) == []
    p.handle(Event.PLAY_PAUSE)
    assert p.state.status == PLAYING
    assert last_play(backend) == os.path.join("01 Album A", "1 a.flac")
    p.handle(Event.PLAY_PAUSE)
    assert p.state.status == PAUSED and backend.paused
    p.handle(Event.PLAY_PAUSE)
    assert p.state.status == PLAYING and not backend.paused


def test_next_prev_and_restart(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    assert p.state.track_name == "2 b.flac"
    backend.pos = 5.0
    p.handle(Event.PREV)            # > 3 s in: restart the track
    assert backend.calls[-1] == ("seek", 0) and p.state.track_name == "2 b.flac"
    backend.pos = 1.0
    p.handle(Event.PREV)            # near the start: previous track
    assert p.state.track_name == "1 a.flac"
    p.handle(Event.PREV)            # before the first track -> last of previous folder (wraps)
    assert p.state.folder_name == "02 Album B" and p.state.track_name == "x.mp3"


def test_folder_change_keeps_paused_and_wraps(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    p.handle(Event.PLAY_PAUSE)      # pause
    p.handle(Event.FOLDER_UP)
    assert p.state.status == PAUSED and p.state.folder_name == "02 Album B"
    assert plays(backend)[-1][3] is True   # loaded paused
    assert p.state.track_index == 0
    p.handle(Event.FOLDER_UP)
    assert p.state.folder_name == "01 Album A"
    p.handle(Event.FOLDER_DOWN)
    assert p.state.folder_name == "02 Album B"


def test_folder_change_while_stopped_doesnt_play(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.FOLDER_UP)
    assert p.state.status == STOPPED and plays(backend) == []
    p.handle(Event.PLAY_PAUSE)
    assert last_play(backend) == os.path.join("02 Album B", "x.mp3")


@pytest.mark.parametrize("mode,folder,status", [
    ("continue", "02 Album B", PLAYING),
    ("repeat", "01 Album A", PLAYING),
    ("stop", "01 Album A", STOPPED),
])
def test_end_of_folder(backend, music, mode, folder, status):
    p = make(backend, end_of_folder=mode)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.TRACK_END, "eof")
    p.handle(Event.TRACK_END, "eof")
    assert p.state.track_name == "10 c.flac"
    p.handle(Event.TRACK_END, "eof")
    assert p.state.folder_name == folder and p.state.track_index == 0 and p.state.status == status


def test_manual_next_wraps_in_stop_mode(backend, music):
    p = make(backend, end_of_folder="stop")
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    for _ in range(3):
        p.handle(Event.NEXT)
    assert p.state.track_index == 0 and p.state.status == PLAYING


def test_error_skips_but_vanished_file_stops(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.TRACK_END, "error")
    assert p.state.track_name == "2 b.flac" and p.state.status == PLAYING
    shutil.rmtree(music)
    p.handle(Event.TRACK_END, "error")
    assert p.state.status == STOPPED


def test_too_many_errors_stops(backend, music):
    p = make(backend, max_consecutive_errors=3, end_of_folder="repeat")
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    for _ in range(3):
        p.handle(Event.TRACK_END, "error")
    assert p.state.status == STOPPED


def test_stale_track_end_ignored(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.STOP)
    p.handle(Event.TRACK_END, "eof")
    assert p.state.status == STOPPED and p.state.track_index == 0


def test_volume_clamped(backend):
    p = make(backend, volume=90, volume_step=5, volume_max=100)
    assert backend.volume == 90
    for _ in range(5):
        p.handle(Event.VOL_UP)
    assert p.state.volume == 100 and backend.volume == 100
    for _ in range(30):
        p.handle(Event.VOL_DOWN)
    assert p.state.volume == 0


def test_media_removed(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.MEDIA_REMOVED)
    assert p.state.status == IDLE and not p.state.media and backend.calls[-1] == ("stop",)
    p.handle(Event.PLAY_PAUSE)
    assert p.state.status == IDLE


def test_empty_media_stays_idle(backend, tmp_path):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, str(tmp_path))
    assert p.state.status == IDLE and not p.state.media


def test_listeners_only_get_changes(backend, music):
    seen = []
    p = make(backend)
    p.add_listener(seen.append)
    p.publish(force=True)
    p.handle(Event.NEXT)          # no media: no change
    assert len(seen) == 1
    p.handle(Event.MEDIA_ADDED, music)
    assert len(seen) == 2 and seen[-1].media


def test_broken_listener_doesnt_crash(backend, music):
    p = make(backend)
    p.add_listener(lambda s: 1 / 0)
    p.handle(Event.MEDIA_ADDED, music)
    assert p.state.media


def test_resume_roundtrip(backend, music, tmp_path):
    store = ResumeStore(str(tmp_path / "state" / "s.json"))
    p = make(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    p.handle(Event.VOL_DOWN)
    backend.pos = 42.5
    p.close()

    b2 = type(backend)()
    p2 = make(b2, resume=store)
    assert p2.state.volume == 75 and b2.volume == 75
    p2.handle(Event.MEDIA_ADDED, music)
    assert p2.state.track_name == "2 b.flac" and plays(b2) == []
    p2.handle(Event.PLAY_PAUSE)
    assert plays(b2)[-1][2] == 42.5
    p2.handle(Event.NEXT)
    assert plays(b2)[-1][2] == 0.0   # resume position only applies once

    b3 = type(backend)()
    p3 = make(b3, resume=store, autoplay=True)
    p3.handle(Event.MEDIA_ADDED, music)
    assert p3.state.status == PLAYING and plays(b3)[-1][2] == 42.5


def test_resume_survives_unplug(backend, music, tmp_path):
    store = ResumeStore(str(tmp_path / "s.json"))
    p = make(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.FOLDER_UP)
    backend.pos = 12.0
    p.handle(Event.MEDIA_REMOVED)
    p.handle(Event.MEDIA_ADDED, music)
    assert p.state.folder_name == "02 Album B"
    p.handle(Event.PLAY_PAUSE)
    assert plays(backend)[-1][2] == 12.0


def test_resume_ignored_for_other_media(backend, music, tmp_path):
    store = ResumeStore(str(tmp_path / "s.json"))
    store.save({"fingerprint": "nope", "folder": "02 Album B", "track": "x.mp3", "position": 9, "volume": 50})
    p = make(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, music)
    assert p.state.folder_index == 0 and p.state.volume == 50


def test_state_only_written_on_close(backend, music, tmp_path):
    store = ResumeStore(str(tmp_path / "s.json"))
    p = make(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    p.handle(Event.MEDIA_REMOVED, music)
    p.handle(Event.MEDIA_ADDED, music)
    assert not os.path.exists(store.path)          # nothing hit the disk while running
    p.handle(Event.PLAY_PAUSE)                     # resumes 2 b.flac
    p.handle(Event.TRACK_INFO, {"title": "Bee", "duration": 3})
    backend.pos = 33.3
    p.close()
    data = store.load()
    entry = data["media"][data["active"]]
    assert entry["track"] == "2 b.flac" and entry["title"] == "Bee" and entry["seconds"] == 33.3


def test_old_state_format_is_migrated(backend, music, tmp_path):
    from saas.core.library import Library
    lib = Library(music, ["flac", "mp3"]); lib.scan()
    store = ResumeStore(str(tmp_path / "s.json"))
    store.save({"volume": 40, "fingerprint": lib.fingerprint, "folder": "02 Album B", "track": "x.mp3", "position": 9})
    p = make(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, music)
    assert p.state.volume == 40 and p.state.folder_name == "02 Album B"
    p.handle(Event.PLAY_PAUSE)
    assert plays(backend)[-1][2] == 9


def two_sticks(tmp_path):
    from conftest import make_tree
    a, b = str(tmp_path / "A_STICK"), str(tmp_path / "B_STICK")
    make_tree(a, ["Rock/1.mp3", "Rock/2.mp3"])
    make_tree(b, ["Jazz/1.mp3", "Pop/1.mp3"])
    return a, b


def test_source_cycle_keeps_place_per_medium(backend, tmp_path):
    a, b = two_sticks(tmp_path)
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, a)
    p.handle(Event.MEDIA_ADDED, b)
    assert p.state.medium_name == "A_STICK" and p.state.medium_count == 2
    assert p.state.notice == "Medium added: B_STICK"
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    backend.pos = 20.0
    p.handle(Event.SOURCE_NEXT)                     # playing -> keeps playing on B
    assert p.state.medium_name == "B_STICK" and p.state.status == PLAYING and p.state.medium_index == 2
    assert plays(backend)[-1][1] == os.path.join(b, "Jazz", "1.mp3")
    p.handle(Event.FOLDER_UP)
    p.handle(Event.SOURCE_NEXT)                     # back to A where we left it
    assert p.state.medium_name == "A_STICK" and p.state.track_name == "2.mp3"
    assert plays(backend)[-1][2] == 20.0
    p.handle(Event.SOURCE_NEXT)
    assert p.state.folder_name == "Pop"             # B remembered too


def test_source_cycle_single_and_none(backend, tmp_path):
    a, _ = two_sticks(tmp_path)
    p = make(backend)
    p.handle(Event.SOURCE_NEXT)
    assert p.state.notice == "No media"
    p.handle(Event.MEDIA_ADDED, a)
    p.handle(Event.SOURCE_NEXT)
    assert p.state.notice == "Only medium: A_STICK"


def test_removing_active_medium_falls_back(backend, tmp_path):
    a, b = two_sticks(tmp_path)
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, a)
    p.handle(Event.MEDIA_ADDED, b)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.MEDIA_REMOVED, a)
    assert p.state.medium_name == "B_STICK" and p.state.status == STOPPED and p.state.medium_count == 1
    assert p.state.notice == "Medium removed: A_STICK"
    p.handle(Event.MEDIA_REMOVED, b)
    assert p.state.status == IDLE and p.state.medium_count == 0


def test_removing_inactive_medium(backend, tmp_path):
    a, b = two_sticks(tmp_path)
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, a)
    p.handle(Event.MEDIA_ADDED, b)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.MEDIA_REMOVED, b)
    assert p.state.medium_name == "A_STICK" and p.state.status == PLAYING


def test_boot_prefers_last_active_medium(backend, tmp_path):
    a, b = two_sticks(tmp_path)
    store = ResumeStore(str(tmp_path / "s.json"))
    p = make(backend, resume=store)
    p.handle(Event.MEDIA_ADDED, a)
    p.handle(Event.MEDIA_ADDED, b)
    p.handle(Event.SOURCE_NEXT)                     # B active
    p.close()
    b2 = type(backend)()
    p2 = make(b2, resume=store, autoplay=True)
    p2.handle(Event.MEDIA_ADDED, a)                 # A shows up first at boot...
    p2.handle(Event.MEDIA_ADDED, b)                 # ...but B was active last time
    assert p2.state.medium_name == "B_STICK" and p2.state.status == PLAYING
    # once the user did something, a late stick doesn't take over
    p3 = make(type(backend)(), resume=store, autoplay=True)
    p3.handle(Event.MEDIA_ADDED, a)
    p3.handle(Event.NEXT)
    p3.handle(Event.MEDIA_ADDED, b)
    assert p3.state.medium_name == "A_STICK"


def test_standby_stops_and_wakes_where_it_was(backend, music):
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.NEXT)
    backend.pos = 31.0
    p.handle(Event.STANDBY)
    assert p.state.standby and p.state.status == STOPPED and backend.calls[-1] == ("stop",)
    assert p.state.notice == "Standby"
    n = len(plays(backend))
    p.handle(Event.NEXT)                      # any button only wakes - it does NOT skip
    assert not p.state.standby and p.state.notice == "Awake"
    assert plays(backend)[n:] == [("play", plays(backend)[-1][1], 31.0, False)]
    assert p.state.track_name == "2 b.flac"


def test_standby_while_stopped_stays_stopped_and_blocks_autoplay(backend, music):
    p = make(backend, autoplay=True)
    p.handle(Event.STANDBY)
    p.handle(Event.MEDIA_ADDED, music)        # a stick arrives during standby
    assert p.state.standby and p.state.media and plays(backend) == []
    p.handle(Event.STANDBY)
    assert not p.state.standby and plays(backend) == []   # wasn't playing before


def test_poweroff_asks_main_to_halt(backend, music):
    bus = Bus()
    p = Player(bus, backend, extensions=["flac", "mp3"])
    p.handle(Event.STANDBY)
    p.handle(Event.POWEROFF)                  # works from standby too (no wake first)
    assert p.state.notice == "Powering off"
    assert bus.get(0) == (Event.HALT, None)


def test_scrub_seeks_without_skipping(backend, music):
    """0.6.5: seek_fwd/seek_back (held next/prev) move inside the song and speed up."""
    p = make(backend)
    p.handle(Event.MEDIA_ADDED, music)
    p.handle(Event.SEEK_FWD, 0)                 # stopped: nothing happens
    assert not [c for c in backend.calls if c[0] == "seek"]
    p.handle(Event.PLAY_PAUSE)
    p.handle(Event.TRACK_INFO, {"duration": 100.0})
    backend.pos = 10.0
    p.handle(Event.SEEK_FWD, 0)
    assert backend.calls[-1] == ("seek", 12.0) and "» 0:12 / 1:40" == p.state.notice
    p.handle(Event.SEEK_FWD, 1)                 # mpv may not have caught up: count from the target
    assert backend.calls[-1] == ("seek", 14.0)
    p.handle(Event.SEEK_FWD, 30)                # held long: 15 s steps, but never past the end
    p.handle(Event.SEEK_FWD, 31)
    p.handle(Event.SEEK_FWD, 32)
    p.handle(Event.SEEK_FWD, 33)
    p.handle(Event.SEEK_FWD, 34)
    p.handle(Event.SEEK_FWD, 35)
    assert backend.calls[-1] == ("seek", 99.0) and p.state.track_name == "1 a.flac"
    p.handle(Event.SEEK_BACK, 30)
    assert backend.calls[-1] == ("seek", 84.0)
    p._scrub = None
    backend.pos = 1.0
    p.handle(Event.SEEK_BACK, 0)
    assert backend.calls[-1] == ("seek", 0.0)   # clamped at the start
    assert p.state.status == PLAYING and p.state.track_name == "1 a.flac"
