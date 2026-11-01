"""LMS source: a fake JSON-RPC server built from answers recorded from a real
Lyrion Music Server 9.1.2, plus the real thing when SAAS_TEST_LMS_URL is set
(e.g. SAAS_TEST_LMS_URL=http://127.0.0.1:9000)."""
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from saas.core.config import ConfigError, validate
from saas.core.events import Event
from saas.net.http import NetError
from saas.net.lms import LmsLibrary, _seconds
from saas.sources.network import make_factory

FILE = "file:///music"
# folder tree: 0 (root) -> 6 "Artist A - First Album" (2 tracks), 7 "Various" -> 8 "Compilation" (2 tracks)
FOLDERS = {
    0: [{"type": "folder", "id": 7, "filename": "Various"},
        {"type": "folder", "id": 6, "filename": "Artist A - First Album"}],
    6: [{"type": "track", "id": 1, "filename": "01 Song One.mp3", "title": "01 Song One.mp3", "duration": "0:20"},
        {"type": "track", "id": 2, "filename": "02 Song Two.mp3", "title": "02 Song Two.mp3", "duration": "0:20"}],
    7: [{"type": "folder", "id": 8, "filename": "Compilation"}],
    8: [{"type": "track", "id": 3, "filename": "01 Comp.flac", "title": "01 Comp.flac", "duration": "0:20"},
        {"type": "track", "id": 4, "filename": "02 Comp2.flac", "title": "02 Comp2.flac", "duration": "0:20"}],
}
SONGS = {
    1: {"id": 1, "title": "Song One", "duration": 20.035, "artist": "Artist A", "tracknum": "1", "album": "First Album"},
    2: {"id": 2, "title": "Song Two", "duration": 20.035, "artist": "Artist A", "tracknum": "2", "album": "First Album"},
    3: {"id": 3, "title": "Comp Track", "duration": 20, "artist": "Artist B", "tracknum": "1", "album": "Compilation"},
    4: {"id": 4, "title": "Comp Track 2", "duration": 20, "artist": "Artist C", "tracknum": "2", "album": "Compilation"},
}
ALBUMS = [{"id": 2, "album": "Compilation", "artist": "Artist C"}, {"id": 1, "album": "First Album", "artist": "Artist A"}]
PAGE_LIMIT = 1     # the fake hands out one item per request to exercise paging


class Lms(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not self.server.auth:
            return True
        want = "Basic " + base64.b64encode(self.server.auth.encode()).decode()
        return self.headers.get("Authorization") == want

    def do_GET(self):
        if not self._authed():
            return self._send(401, b"", "text/html")
        if self.path.startswith("/music/") and self.path.endswith("/download"):
            return self._send(200, b"ID3fake", "audio/mpeg")
        return self._send(404, b"")

    def do_POST(self):
        if not self._authed():
            return self._send(401, b"", "text/html")
        if self.path != "/jsonrpc.js":
            return self._send(404, b"")
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cmd = req["params"][1]
        self.server.commands.append(cmd)
        name, rest = cmd[0], cmd[1:]
        params = dict(p.split(":", 1) for p in rest if ":" in p)
        if name == "version":
            return self.reply(req, {"_version": "9.1.2"})
        if name == "serverstatus":
            return self.reply(req, {"version": "9.1.2", "info total songs": 4})
        start, qty = int(rest[0]), min(int(rest[1]), PAGE_LIMIT)
        if name == "musicfolder":
            items = FOLDERS[int(params.get("folder_id", 0))]
            return self.reply(req, {"count": len(items), "folder_loop": items[start:start + qty]})
        if name == "albums":
            return self.reply(req, {"count": len(ALBUMS), "albums_loop": ALBUMS[start:start + qty]})
        if name == "titles":
            songs = [s for s in SONGS.values() if (s["album"] == "First Album") == (params["album_id"] == "1")]
            songs = list(reversed(songs))       # the client sorts by disc/track itself
            return self.reply(req, {"count": len(songs), "titles_loop": songs[start:start + qty]})
        if name == "songinfo":
            s = SONGS[int(params["track_id"])]
            return self.reply(req, {"songinfo_loop": [{k: v} for k, v in s.items()]})
        return self.reply(req, {})

    def reply(self, req, result):
        return self._send(200, {"id": req["id"], "method": "slim.request", "params": req["params"], "result": result})


@pytest.fixture
def lms():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Lms)
    srv.commands, srv.auth = [], ""
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


def test_folders_view_mirrors_the_disk(lms):
    lib = LmsLibrary("Home LMS", lms.url)
    lib.ping()
    assert lib.scan() == ["folder:6", "folder:8"]
    assert [lib.folder_label(i) for i in range(2)] == ["Artist A - First Album", "Various / Compilation"]
    assert [lib.track_label(0, j) for j in range(2)] == ["01 Song One", "02 Song Two"]   # file names until played
    assert lib.track_path(1, 0) == f"{lms.url}/music/3/download"
    # tags are fetched once, when a track is started
    n = len(lms.commands)
    assert lib.track_tags(1, 0) == {"title": "Comp Track", "artist": "Artist B", "album": "Compilation", "duration": 20.0}
    lib.track_tags(1, 0)
    assert len(lms.commands) == n + 1


def test_albums_view_and_track_order(lms):
    lib = LmsLibrary("Home LMS", lms.url, view="albums")
    assert lib.scan() == ["album:2", "album:1"]
    assert lib.folder_label(1) == "Artist A - First Album"
    assert [lib.track_label(1, j) for j in range(2)] == ["Song One", "Song Two"]
    assert lib.track_tags(1, 1)["artist"] == "Artist A"


def test_start_folder(lms):
    lib = LmsLibrary("x", lms.url, start="/various/")
    assert lib.scan() == ["folder:8"] and lib.folder_label(0) == "Compilation"
    with pytest.raises(NetError, match="no folder 'nope'"):
        LmsLibrary("x", lms.url, start="nope").scan()


def test_password(lms):
    lms.auth = "dan:p@ss:w/rd"
    with pytest.raises(NetError, match="wants a password"):
        LmsLibrary("x", lms.url).ping()
    lib = LmsLibrary("x", lms.url, user="dan", password="p@ss:w/rd")
    lib.ping()
    assert lib.scan()
    url = lib.track_path(0, 0)
    assert url.startswith("http://dan:p%40ss%3Aw%2Frd@127.0.0.1:")
    from saas.backends.mpv_backend import redact
    assert "p%40ss" not in redact(f"cannot open {url}")


def test_config_and_factory(lms):
    cfg = validate({"audio": {"backend": "dummy"}, "library": {"media_root": "/m"},
                    "network": [{"type": "lms", "url": lms.url, "view": "albums"}]})
    spec = cfg["network"][0]
    assert spec["name"] == "Lms"
    lib = make_factory(spec)()
    assert lib.view == "albums" and lib.scan()
    with pytest.raises(ConfigError, match="view"):
        validate({"audio": {"backend": "dummy"}, "library": {"media_root": "/m"},
                  "network": [{"type": "lms", "url": lms.url, "view": "genres"}]})


def test_player_plays_lms(lms, backend):
    from saas.core.events import Bus
    from saas.core.player import Player
    lib = LmsLibrary("Home LMS", lms.url)
    lib.scan()
    p = Player(Bus(), backend, extensions=["mp3"])
    p.handle(Event.MEDIA_ADDED, lib)
    p.handle(Event.PLAY_PAUSE)
    assert p.state.title == "Song One" and p.state.artist == "Artist A"
    assert backend.calls[-1][1] == f"{lms.url}/music/1/download"


def test_seconds():
    assert _seconds("3:25") == 205 and _seconds(20.035) == 20.035 and _seconds("") == 0 and _seconds("x") == 0


@pytest.mark.skipif(not os.environ.get("SAAS_TEST_LMS_URL"), reason="set SAAS_TEST_LMS_URL to test a real LMS")
def test_real_lms():
    for view in ("folders", "albums"):
        lib = LmsLibrary("real", os.environ["SAAS_TEST_LMS_URL"], view=view,
                         user=os.environ.get("SAAS_TEST_LMS_USER", ""),
                         password=os.environ.get("SAAS_TEST_LMS_PASSWORD", ""))
        lib.ping()
        assert lib.scan()
        tags = lib.track_tags(0, 0)
        assert tags["title"] and tags["duration"] > 0
        assert "/music/" in lib.track_path(0, 0)


def test_test_network_probe(lms):
    from saas.net import probe
    lms.auth = "dan:pw"
    lines = []
    specs = [{"type": "lms", "name": "Home", "url": lms.url, "user": "dan", "password": "pw"},
             {"type": "lms", "name": "Wrong", "url": lms.url}]
    assert probe.run(specs, make_factory, lines.append) == 1
    text = "\n".join(lines)
    assert "stream: OK (HTTP 200, audio/mpeg" in text and "   OK" in text
    assert "FAIL: LMS wants a password" in text and "pw@" not in text
