"""saas-now.sh (bash) against now.vars written by the real StatusOutput."""
import os
import shutil
import subprocess
import time

import pytest

from saas.core.state import PlayerState
from saas.outputs.status import StatusOutput, build_status, build_vars

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HERE, "saas-now.sh")
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")

BASE = dict(status="playing", media=True, folder_index=1, folder_count=20, view_index=2, view_count=20,
            folder_name="Other",
            track_index=2, track_count=12, track_name="03 x.mp3", title="Beta", artist="Band B",
            album="Album", duration=296, active_playlist="Favorites", volume=70, can_save=True)


def lines(tmp_path, state, **env):
    out = StatusOutput(str(tmp_path / "run"), lambda: 83.4, interval=60)
    out.update(state)
    out._stop.set()
    e = {**os.environ, "SAAS_VARS": str(tmp_path / "run" / "now.vars"), "LC_ALL": "C.UTF-8", **env}
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=e, check=True)
    return r.stdout.split("\n")[:2]


def test_two_lines(tmp_path):
    l1, l2 = lines(tmp_path, PlayerState(**BASE, notice="Saved to Favorites", notice_time=time.time(),
                                         in_active_playlist=True))
    assert l1 == "Playing: Beta, Band B - Album - (4:56), Track 3/12 Folder 2/20"
    assert l2 == "Saved to Favorites"


def test_line2_states_and_like_icon(tmp_path):
    old = time.time() - 60
    assert lines(tmp_path, PlayerState(**BASE, notice="x", notice_time=old))[1] == "Save to: Favorites"
    assert lines(tmp_path, PlayerState(**BASE, in_active_playlist=True, liked=True))[1] == "Saved in: Favorites  <3"
    l1, l2 = lines(tmp_path, PlayerState(**BASE, liked=True), SAAS_WIDTH="20", SAAS_LIKE_ICON="♥")
    assert l1 == "Playing: Beta, Band " and l2 == "Save to: Favorites ♥" and len(l2) == 20


def test_untagged_idle_and_not_running(tmp_path):
    st = PlayerState(status="paused", media=True, folder_count=3, view_index=1, view_count=3,
                     track_count=5, track_name="07 x.flac")
    assert lines(tmp_path, st) == ["Paused: 07 x, Track 1/5 Folder 1/3", ""]
    assert lines(tmp_path, PlayerState()) == ["No media", ""]
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                       env={**os.environ, "SAAS_VARS": str(tmp_path / "nope")})
    assert r.stdout == "SaaS: not running\n\n"


def test_newlines_in_tags_cant_break_the_format():
    d = build_status(PlayerState(**{**BASE, "title": "evil\nSAAS_LIKED=1"}), 1.0)
    v = build_vars(d)
    assert "SAAS_TITLE=evil SAAS_LIKED=1\n" in v and "\nSAAS_LIKED=0\n" in v


def test_hostile_vars_file_executes_nothing(tmp_path):
    marker = tmp_path / "pwned"
    f = tmp_path / "evil.vars"
    f.write_text("\n".join([
        "SAAS_MEDIA=1", "SAAS_STATUS=playing",
        f"SAAS_DURATION=x[$(touch {marker}1)]", f"SAAS_NOTICE_TIME=a[$(touch {marker}2)]",
        f"SAAS_WIDTH=a[$(touch {marker}3)]", "SAAS_VARS=/etc/shadow", "PATH=/nonexistent",
        f"SAAS_TITLE=$(touch {marker}4)", f"SAAS_NOTICE=`touch {marker}5`", "SAAS_MEDIUM_COUNT=08",
    ]) + "\n")
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                       env={**os.environ, "SAAS_VARS": str(f)})
    assert r.returncode == 0
    assert not list(tmp_path.glob("pwned*"))
    assert "$(touch" in r.stdout  # shown as text, not run


def test_playlist_view_label(tmp_path):
    st = PlayerState(**{**BASE, "view": "playlists", "view_index": 1, "view_count": 3, "in_playlist": True})
    assert lines(tmp_path, st)[0].endswith("Track 3/12 Playlist 1/3")


def test_no_save_hint_on_servers(tmp_path):
    st = PlayerState(**{**BASE, "can_save": False})
    assert lines(tmp_path, st)[1] == ""                    # a server: nothing to save to
    st = PlayerState(**{**BASE, "can_save": True})
    assert lines(tmp_path, st)[1] == "Save to: Favorites"


def test_standby_line(tmp_path):
    assert lines(tmp_path, PlayerState(**{**BASE, "standby": True})) == ["Standby", ""]
