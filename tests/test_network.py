"""Network media: fake Jellyfin + Subsonic servers that check auth like the real
ones, DIDL parsing, a real minidlna if installed, the source thread, the player."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from saas.core.events import Bus, Event
from saas.core.player import Player
from saas.core.resume import ResumeStore
from saas.core.state import PLAYING
from saas.net import subsonic as subsonic_mod
from saas.net.base import safe_name
from saas.net.dlna import DlnaLibrary, parse_didl, parse_duration
from saas.net.http import NetError
from saas.net.jellyfin import JellyfinLibrary
from saas.net.subsonic import SubsonicLibrary
from saas.sources.network import NetSource, make_factory

KEY, PW = "k3y", "s3cret"
AUDIO = b"ID3fake-audio-bytes"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.server.hits.append(u.path)
        if self.server.down:
            return self._send(503, {})
        if u.path.startswith("/rest/"):
            return self.subsonic(u.path[6:], q)
        return self.jellyfin(u.path, q)

    # --- Jellyfin -----------------------------------------------------------------
    def jellyfin(self, path, q):
        if path == "/System/Info/Public":
            return self._send(200, {"ServerName": "fake"})
        if path.startswith("/Audio/"):
            ok = q.get("ApiKey") == KEY and q.get("static") == "true"
            return self._send(200 if ok else 401, AUDIO if ok else {}, "audio/mpeg")
        if self.headers.get("Authorization") != f'MediaBrowser Token="{KEY}"':
            return self._send(401, {})
        if path == "/Users":
            return self._send(200, [{"Name": "Dan", "Id": "u1"}, {"Name": "other", "Id": "u2"}])
        if path == "/Items":
            assert q["userId"] == "u1"
            if q.get("IncludeItemTypes") == "MusicAlbum":
                return self._send(200, {"Items": [{"Id": "al1", "Name": "First", "AlbumArtist": "Band A"},
                                                  {"Id": "al2", "Name": "Loose"}]})
            if q.get("ParentId") == "al1":
                return self._send(200, {"Items": [
                    {"Id": "t1", "Name": "Song 1", "Artists": ["Band A"], "Album": "First", "RunTimeTicks": 1_850_000_000},
                    {"Id": "t2", "Name": "Song 2", "Artists": [], "AlbumArtist": "Band A", "Album": "First"}]})
            return self._send(200, {"Items": [{"Id": "t3", "Name": "Alone", "Album": "Loose"}]})
        return self._send(404, {})

    # --- Subsonic -----------------------------------------------------------------
    def subsonic(self, method, q):
        ok_auth = q.get("u") == "dan" and (
            ("t" in q and q["t"] == hashlib.md5((PW + q.get("s", "")).encode()).hexdigest())
            or q.get("p") == "enc:" + PW.encode().hex())
        if not ok_auth:
            return self._send(200, {"subsonic-response": {"status": "failed",
                                                          "error": {"code": 40, "message": "Wrong username or password"}}})
        r = {"status": "ok", "version": "1.16.1"}
        if method == "getAlbumList2":
            albums = [{"id": f"a{i}", "name": f"Album {i}", "artist": "Band"} for i in range(5)]
            off, size = int(q["offset"]), int(q["size"])
            r["albumList2"] = {"album": albums[off:off + size]}
        elif method == "getAlbum":
            r["album"] = {"name": "Album 0", "song": [
                {"id": "s2", "title": "Second", "track": 2, "artist": "Band", "duration": 120},
                {"id": "s1", "title": "First", "track": 1, "artist": "Band", "duration": 100}]}
        elif method == "stream":
            return self._send(200, AUDIO, "audio/mpeg")
        return self._send(200, {"subsonic-response": r})


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.hits, srv.down = [], False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


# --- Jellyfin -----------------------------------------------------------------------
def test_jellyfin_albums_tracks_and_stream(server):
    lib = JellyfinLibrary("Jelly", server.url, KEY, user="dan")
    lib.ping()
    assert lib.scan() == ["al1", "al2"] and lib.root == "jellyfin://Jelly"
    assert [lib.folder_label(0), lib.folder_label(1)] == ["Band A - First", "Loose"]
    assert lib.tracks(0) == ["t1", "t2"] and lib.track_label(0, 0) == "Song 1"
    assert lib.track_tags(0, 0) == {"title": "Song 1", "artist": "Band A", "album": "First", "duration": 185.0}
    assert lib.track_tags(0, 1)["artist"] == "Band A"                # falls back to AlbumArtist
    import urllib.request
    assert urllib.request.urlopen(lib.track_path(0, 0)).read() == AUDIO   # ApiKey + static work


def test_jellyfin_errors(server):
    with pytest.raises(NetError, match="HTTP 401"):
        JellyfinLibrary("J", server.url, "wrong", user="dan").scan()
    with pytest.raises(NetError, match="user 'nobody' not found"):
        JellyfinLibrary("J", server.url, KEY, user="nobody").scan()
    with pytest.raises(NetError, match="several users"):
        JellyfinLibrary("J", server.url, KEY).scan()


# --- Subsonic -----------------------------------------------------------------------
@pytest.mark.parametrize("auth", ["token", "password"])
def test_subsonic_paging_order_and_auth(server, monkeypatch, auth):
    monkeypatch.setattr(subsonic_mod, "PAGE", 2)                        # 5 albums -> 3 pages
    lib = SubsonicLibrary("Navi", server.url, "dan", PW, auth)
    lib.ping()
    assert len(lib.scan()) == 5 and lib.folder_label(4) == "Band - Album 4"
    assert [lib.track_label(0, j) for j in range(2)] == ["First", "Second"]   # sorted by track number
    url = lib.track_path(0, 0)
    assert "/rest/stream?" in url and ("t=" in url if auth == "token" else "p=enc%3A" in url)
    import urllib.request
    assert urllib.request.urlopen(url).read() == AUDIO
    if auth == "token":
        assert PW not in url                                            # never the plain password


def test_subsonic_wrong_password(server):
    with pytest.raises(NetError, match="Wrong username or password"):
        SubsonicLibrary("N", server.url, "dan", "nope").ping()


# --- DLNA ---------------------------------------------------------------------------
DIDL = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
 <container id="64$1" parentID="64" childCount="2"><dc:title>Band A - First</dc:title></container>
 <item id="64$2" parentID="64"><dc:title>Song &amp; Dance</dc:title><upnp:artist>Band A</upnp:artist>
  <upnp:album>First</upnp:album><upnp:class>object.item.audioItem.musicTrack</upnp:class>
  <res duration="0:03:25.120" protocolInfo="http-get:*:audio/mpeg:*">http://h:8200/MediaItems/2.mp3</res></item>
 <item id="64$3"><dc:title>A picture</dc:title><upnp:class>object.item.imageItem.photo</upnp:class>
  <res>http://h:8200/x.jpg</res></item>
</DIDL-Lite>"""


def test_didl_parsing():
    containers, tracks = parse_didl(DIDL)
    assert containers == [("64$1", "Band A - First")]
    assert tracks == [{"id": "64$2", "title": "Song & Dance", "artist": "Band A", "album": "First",
                       "duration": 205.12, "url": "http://h:8200/MediaItems/2.mp3"}]   # photo skipped
    assert parse_duration("bogus") == 0.0


@pytest.mark.skipif(not shutil.which("minidlnad") or not shutil.which("ffmpeg"), reason="needs minidlna + ffmpeg")
def test_real_minidlna(tmp_path):
    music = tmp_path / "music" / "Band A - First"
    music.mkdir(parents=True)
    for i in (1, 2):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1",
                        "-metadata", f"title=Song {i}", "-metadata", f"track={i}", str(music / f"0{i}.mp3")], check=True)
    port = 8300 + os.getpid() % 500
    conf = tmp_path / "m.conf"
    conf.write_text(f"media_dir=A,{tmp_path / 'music'}\ndb_dir={tmp_path}/db\nlog_dir={tmp_path}/log\n"
                    f"port={port}\nfriendly_name=SaaS pytest\ninotify=no\n")
    subprocess.run(["minidlnad", "-f", str(conf), "-P", str(tmp_path / "pid"), "-R"], check=True)
    try:
        lib = DlnaLibrary("T", location=f"http://127.0.0.1:{port}/rootDesc.xml", start="Music/Folders")
        for _ in range(40):                      # minidlna scans in the background
            try:
                if lib.scan():
                    break
            except NetError:
                pass
            time.sleep(0.25)
        assert lib.folder_label(0) == "Band A - First"
        assert [lib.track_label(0, j) for j in range(2)] == ["Song 1", "Song 2"]
        with pytest.raises(NetError, match="no folder 'Nope'"):
            DlnaLibrary("T", location=f"http://127.0.0.1:{port}/rootDesc.xml", start="Nope").scan()
    finally:
        pid = int((tmp_path / "pid").read_text())
        os.kill(pid, 15)


# --- source thread + player ------------------------------------------------------------
def test_net_source_add_remove_return(server):
    bus = Bus()
    src = NetSource(bus, make_factory({"type": "jellyfin", "name": "Jelly", "url": server.url,
                                       "api_key": KEY, "user": "dan"}))
    src.check()
    event, lib = bus.get(0)
    assert event is Event.MEDIA_ADDED and lib.folders == ["al1", "al2"]
    server.down = True
    src.check()
    assert bus.get(0) == (Event.MEDIA_REMOVED, "jellyfin://Jelly")
    src.check()                                    # still down: nothing, logged once
    assert bus.get(0) is None
    server.down = False
    src.check()
    assert bus.get(0)[0] is Event.MEDIA_ADDED


def test_player_plays_a_server_like_a_stick(server, backend, tmp_path):
    store = ResumeStore(str(tmp_path / "s.json"))
    p = Player(Bus(), backend, extensions=["mp3"], playlist_dir="DLP6/Playlists", resume=store)
    lib = JellyfinLibrary("Jelly", server.url, KEY, user="dan")
    lib.scan()
    p.handle(Event.MEDIA_ADDED, lib)
    assert p.state.medium_name == "Jelly" and p.state.folder_name == "Band A - First"
    p.handle(Event.PLAY_PAUSE)
    assert backend.calls[-1][1].startswith(server.url + "/Audio/t1/stream?")
    assert (p.state.title, p.state.artist, p.state.duration) == ("Song 1", "Band A", 185.0)
    p.handle(Event.TRACK_INFO, {"title": "", "artist": "", "duration": 185.2})   # mpv knows less
    assert p.state.title == "Song 1" and p.state.duration == 185.2
    p.handle(Event.PLAYLIST_ADD)
    assert p.state.notice == "Playlists only on sticks"
    p.handle(Event.LIKE)
    assert p.state.notice == "Playlists only on sticks"
    p.handle(Event.TRACK_END, "error")             # a stream error skips, it doesn't mean "stick pulled"
    assert p.state.status == PLAYING and p.state.track_name == "Song 2"
    backend.pos = 12.0
    p.close()
    p2 = Player(Bus(), type(backend)(), extensions=["mp3"], resume=store, autoplay=True)
    lib2 = JellyfinLibrary("Jelly", server.url, KEY, user="dan")
    lib2.scan()
    p2.handle(Event.MEDIA_ADDED, lib2)
    assert p2.state.track_name == "Song 2" and p2.backend.calls[-1][2] == 12.0   # resumed per server


def test_redaction_of_stream_credentials():
    from saas.backends.mpv_backend import redact
    s = redact("Failed http://h/Audio/1/stream?static=true&ApiKey=abc123 and /rest/stream?u=dan&t=deadbeef&s=f00&p=enc:41")
    assert "abc123" not in s and "deadbeef" not in s and "f00" not in s and "enc:41" not in s
    assert "static=true" in s and "u=dan" in s


def test_safe_name():
    assert safe_name("My/Server: 1") == "My_Server_ 1" and safe_name("") == "server"
