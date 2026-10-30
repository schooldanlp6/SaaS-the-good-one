"""The controller: consumes events, drives the backend, publishes state.

Everything here runs on the main thread (see main.py), so no locks.

Media: every stick (or, in dev, folder) is a "medium" with its own Library and
its own resume point. `*` (SOURCE_NEXT) cycles through the media present.

Resume state is kept in memory and only written to disk on a clean exit
(close()), to keep SD card writes to a minimum.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from ..backends.base import AudioBackend
from .events import USER_EVENTS, Bus, Event
from .library import Library
from .playlists import PlaylistStore
from .resume import ResumeStore
from .state import PAUSED, PLAYING, STOPPED, PlayerState

log = logging.getLogger(__name__)

Listener = Callable[[PlayerState], None]
MAX_REMEMBERED_MEDIA = 20


def _migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Old single-medium format -> per-medium format."""
    if "fingerprint" in data and "media" not in data:
        fp = data.pop("fingerprint")
        entry = {"folder": data.pop("folder", ""), "track": data.pop("track", ""),
                 "seconds": data.pop("position", 0.0), "title": ""}
        data["media"] = {fp: entry} if isinstance(fp, str) else {}
        data["active"] = fp
    if not isinstance(data.get("media"), dict):
        data["media"] = {}
    return data


class Player:
    def __init__(
        self,
        bus: Bus,
        backend: AudioBackend,
        *,
        extensions: List[str],
        end_of_folder: str = "continue",   # continue | repeat | stop
        autoplay: bool = False,
        volume: int = 80,
        volume_step: int = 5,
        volume_max: int = 100,
        resume: Optional[ResumeStore] = None,
        prev_restart_seconds: float = 3.0,
        max_consecutive_errors: int = 10,
        playlist_dir: Optional[str] = None,   # e.g. "DLP6/Playlists"; None = no playlists
        default_playlist: str = "Favorites",
        liked_playlist: str = "Liked Songs",
        updater: Any = None,                 # saas.core.updater.Updater, None = OTA off
    ) -> None:
        self.bus = bus
        self.backend = backend
        self.extensions = extensions
        self.end_of_folder = end_of_folder
        self.autoplay = autoplay
        self.volume_step = volume_step
        self.volume_max = volume_max
        self.resume = resume
        self.prev_restart_seconds = prev_restart_seconds
        self.max_consecutive_errors = max_consecutive_errors
        self.playlist_dir = playlist_dir
        self.default_playlist = default_playlist
        self.liked_playlist = liked_playlist
        self.updater = updater

        self.media: Dict[str, Library] = {}      # root path -> library, in arrival order
        self.active_root: Optional[str] = None
        self.library: Optional[Library] = None   # the active medium's library
        self.state = PlayerState(volume=volume)
        self._listeners: List[Listener] = []
        self._published: Optional[PlayerState] = None
        self._current_path: Optional[str] = None
        self._start_position = 0.0  # where the next "play" starts (resume)
        self._errors = 0
        self._auto_activated = False  # active medium was picked automatically, not by the user
        # last place per view on the active medium: view -> (folder key, track index, seconds)
        self._view_mem: Dict[str, tuple] = {}
        self._wake_resumes = False    # standby interrupted playback -> resume on wake

        self._resume_data: Dict[str, Any] = _migrate(resume.load() if resume else {})
        saved_volume = self._resume_data.get("volume")
        if isinstance(saved_volume, int) and 0 <= saved_volume <= volume_max:
            self.state.volume = saved_volume
        if playlist_dir:
            saved = self._resume_data.get("playlist")
            self.state.active_playlist = saved if isinstance(saved, str) and saved else default_playlist

        backend.set_end_callback(lambda reason: bus.emit(Event.TRACK_END, reason))
        backend.set_info_callback(lambda info: bus.emit(Event.TRACK_INFO, info))
        backend.set_volume(self.state.volume)
        if updater is not None:
            msg = updater.consume_marker()   # "Updated to X" after an OTA restart
            if msg:
                self._notice(msg)

        self._handlers: Dict[Event, Callable[[Any], None]] = {
            Event.PLAY_PAUSE: self._play_pause,
            Event.STOP: self._stop,
            Event.NEXT: lambda _: self._manual_step(+1),
            Event.PREV: self._prev,
            Event.FOLDER_UP: lambda _: self._folder(+1),
            Event.FOLDER_DOWN: lambda _: self._folder(-1),
            Event.VOL_UP: lambda _: self._volume(+self.volume_step),
            Event.VOL_DOWN: lambda _: self._volume(-self.volume_step),
            Event.SOURCE_NEXT: self._source_next,
            Event.TRACK_END: self._track_end,
            Event.MEDIA_ADDED: self._media_added,
            Event.MEDIA_REMOVED: self._media_removed,
            Event.TRACK_INFO: self._track_info,
            Event.PLAYLIST_ADD: self._playlist_add,
            Event.PLAYLIST_NEXT: self._playlist_next,
            Event.PLAYLIST_NEW: self._playlist_new,
            Event.LIKE: self._like,
            Event.VIEW_TOGGLE: self._view_toggle,
            Event.UPDATE: self._update,
            Event.STANDBY: self._standby,
            Event.POWEROFF: self._poweroff,
        }

    # public API used by main.py -------------------------------------------
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def handle(self, event: Event, data: Any = None) -> None:
        handler = self._handlers.get(event)
        if handler is None:
            log.debug("ignoring event %s", event)
            return
        if event.value in USER_EVENTS:
            self._auto_activated = False
            if self.state.standby and event not in (Event.STANDBY, Event.POWEROFF):
                event, handler = Event.STANDBY, self._standby   # any button wakes; the press is used up
        try:
            handler(data)
        except Exception:
            log.exception("error while handling %s", event.value)
        self.publish()

    def publish(self, force: bool = False) -> None:
        s = self.state
        roots = list(self.media)
        s.medium_count = len(roots)
        s.medium_index = roots.index(self.active_root) + 1 if self.active_root in roots else 0
        s.medium_name = self._medium_name(self.active_root) if self.active_root else ""
        snap = s.snapshot()
        if not force and snap == self._published:
            return
        self._published = snap
        for fn in self._listeners:
            try:
                fn(snap)
            except Exception:
                log.exception("output %r failed", fn)

    def save(self) -> None:
        if self.resume is not None:
            self.resume.save(self._remember())

    def close(self) -> None:
        """Clean exit: the only time resume state goes to disk."""
        self.save()

    # resume (in memory) ----------------------------------------------------------
    def _remember(self, position: Optional[float] = None) -> Dict[str, Any]:
        """Update the in-memory resume data with the active medium's place."""
        s, lib = self.state, self.library
        data = self._resume_data
        data["volume"] = s.volume
        if s.active_playlist:
            data["playlist"] = s.active_playlist
        if lib is not None and s.media and s.track_count:
            if position is None:
                if s.status in (PLAYING, PAUSED):
                    position = self.backend.position() or 0.0
                else:
                    position = self._start_position
            media = data["media"]
            media.pop(lib.fingerprint, None)  # re-insert = most recent last
            media[lib.fingerprint] = {
                "name": self._medium_name(self.active_root),
                "folder": lib.folders[s.folder_index],
                "track": lib.tracks(s.folder_index)[s.track_index],
                "title": s.title,
                "seconds": round(float(position), 1),
            }
            while len(media) > MAX_REMEMBERED_MEDIA:
                media.pop(next(iter(media)))
            data["active"] = lib.fingerprint
        return data

    # helpers ----------------------------------------------------------------
    @property
    def _has_media(self) -> bool:
        return self.library is not None and self.state.media

    @staticmethod
    def _medium_name(root: Optional[str]) -> str:
        return os.path.basename(os.path.normpath(root)) if root else ""

    def _refresh_labels(self) -> None:
        s, lib = self.state, self.library
        if lib is None or not s.media or not lib.folders:
            s.folder_name = s.track_name = ""
            s.track_count = 0
            return
        tracks = lib.tracks(s.folder_index)
        s.folder_count = len(lib.folders)
        s.folder_name = lib.folder_label(s.folder_index)
        s.track_count = len(tracks)
        s.in_playlist = lib.is_playlist(s.folder_index)
        s.can_save = lib.playlists is not None
        s.view = "playlists" if s.in_playlist else "folders"
        idxs = self._view_indices(s.view)
        s.view_count = len(idxs)
        s.view_index = idxs.index(s.folder_index) + 1 if s.folder_index in idxs else 0
        s.track_name = lib.track_label(s.folder_index, s.track_index) if 0 <= s.track_index < len(tracks) else ""
        self._refresh_playlist_flags()

    def _view_indices(self, view: str) -> List[int]:
        lib = self.library
        want = view == "playlists"
        return [i for i in range(len(lib.folders)) if lib.is_playlist(i) == want]

    def _current_rel(self) -> Optional[str]:
        s, lib = self.state, self.library
        if lib is None or not s.media or not (0 <= s.track_index < s.track_count):
            return None
        return lib.rel_to_root(lib.track_path(s.folder_index, s.track_index))

    def _refresh_playlist_flags(self) -> None:
        s, lib = self.state, self.library
        rel = self._current_rel()
        if rel is None or lib.playlists is None:
            s.in_active_playlist = s.liked = False
            return
        s.in_active_playlist = lib.playlists.contains(s.active_playlist, rel)
        s.liked = lib.playlists.contains(self.liked_playlist, rel)

    def _start(self, position: float = 0.0, paused: bool = False) -> None:
        s = self.state
        path = self.library.track_path(s.folder_index, s.track_index)
        self._current_path = path
        s.title = s.artist = s.album = ""
        s.duration = 0.0
        tags = self.library.track_tags(s.folder_index, s.track_index)
        if tags:  # network media know their tags up front
            self._track_info(tags)
        shown = path if self.library.is_local else f"{self.library.root} / {s.track_name}"
        log.info("%s %s", "loading" if paused else "playing", shown)
        self.backend.play(path, start=position, paused=paused)
        s.status = PAUSED if paused else PLAYING

    def _load_current(self) -> None:
        """After an index change: reload according to the current status."""
        self._start_position = 0.0
        self._refresh_labels()
        s = self.state
        if s.track_count == 0:
            log.warning("folder %r has no playable tracks (anymore)", s.folder_name)
            if s.status in (PLAYING, PAUSED):
                self.backend.stop()
                s.status = STOPPED
            return
        if s.status == PLAYING:
            self._start()
        elif s.status == PAUSED:
            self._start(paused=True)

    def _move_folder(self, delta: int, last_track: bool = False) -> None:
        """Next/previous folder within the current view (folders or playlists)."""
        s = self.state
        idxs = self._view_indices(s.view) or list(range(len(self.library.folders)))
        pos = idxs.index(s.folder_index) if s.folder_index in idxs else 0
        s.folder_index = idxs[(pos + delta) % len(idxs)]
        s.track_index = max(0, len(self.library.tracks(s.folder_index)) - 1) if last_track else 0
        self._load_current()

    def _step(self, delta: int, manual: bool) -> None:
        """Move one track; handles running off either end of the folder."""
        s = self.state
        target = s.track_index + delta
        if 0 <= target < s.track_count:
            s.track_index = target
            self._load_current()
            return
        mode = self.end_of_folder
        if mode == "continue":
            self._move_folder(delta, last_track=delta < 0)
        elif delta < 0:
            s.track_index = max(0, s.track_count - 1)
            self._load_current()
        elif mode == "repeat" or manual:
            s.track_index = 0
            self._load_current()
        else:  # "stop" and the folder ended by itself
            s.track_index = 0
            s.status = STOPPED
            self._load_current()

    def _idle_state(self) -> PlayerState:
        s = self.state
        return PlayerState(volume=s.volume, active_playlist=s.active_playlist,
                           notice=s.notice, notice_time=s.notice_time, standby=s.standby)

    def _notice(self, text: str) -> None:
        log.info("%s", text)
        self.state.notice = text
        self.state.notice_time = time.time()

    # media ------------------------------------------------------------------
    def _activate(self, root: str, play: bool) -> None:
        """Make `root` the active medium, at its remembered place."""
        if self._has_media:
            self._remember()
            if self.state.status in (PLAYING, PAUSED):
                self.backend.stop()
        lib = self.media[root]
        self.active_root, self.library = root, lib
        self._view_mem = {}
        self._errors = 0
        self._current_path = None
        self.state = s = self._idle_state()
        s.media, s.status = True, STOPPED
        s.folder_count = len(lib.folders)
        self._start_position = 0.0

        r = self._resume_data["media"].get(lib.fingerprint)
        if isinstance(r, dict) and r.get("folder") in lib.folders:
            fi = lib.folders.index(r["folder"])
            tracks = lib.tracks(fi)
            if r.get("track") in tracks:
                s.folder_index, s.track_index = fi, tracks.index(r["track"])
                sec = r.get("seconds")
                self._start_position = float(sec) if isinstance(sec, (int, float)) and sec > 0 else 0.0
                log.info("resuming %s: %s / %s at %.0fs", self._medium_name(root),
                         r["folder"] or "/", r.get("title") or r["track"], self._start_position)
        self._refresh_labels()
        if play and s.track_count:
            self._start(self._start_position)
            self._start_position = 0.0

    def _media_added(self, root: Any) -> None:
        if isinstance(root, str):            # a stick / folder: scan it here
            if root in self.media:
                return
            store = PlaylistStore(root, self.playlist_dir) if self.playlist_dir else None
            lib = Library(root, self.extensions, store)
            lib.scan()
        else:                                # a network library, already loaded by its source
            lib, root = root, root.root
            if root in self.media:
                return
        name = self._medium_name(root)
        if not lib.folders:
            log.warning("no playable files under %s", root)
            self._notice(f"No music on {name}")
            return
        self.media[root] = lib
        autoplay = self.autoplay and not self.state.standby
        if self.library is None:
            self._auto_activated = True
            self._activate(root, play=autoplay)
        elif self._auto_activated and lib.fingerprint == self._resume_data.get("active") \
                and self.library.fingerprint != lib.fingerprint:
            # Several sticks at boot: prefer the one that was active last time.
            log.info("switching to %s (active last time)", name)
            self._activate(root, play=autoplay)
        else:
            self._notice(f"Medium added: {name}")

    def _media_removed(self, root: Any) -> None:
        root = str(root) if root else self.active_root
        if root not in self.media:
            return
        name = self._medium_name(root)
        if root == self.active_root:
            if self.state.status in (PLAYING, PAUSED):
                # remember where we were so a re-insert resumes here
                self._remember(self.backend.position())
            elif self._has_media:
                self._remember()
            try:
                self.backend.stop()
            except Exception:
                log.debug("backend stop failed", exc_info=True)
            del self.media[root]
            self.library, self.active_root, self._current_path = None, None, None
            self.state = self._idle_state()
            if self.media:  # fall back to another medium, stopped
                self._activate(next(iter(self.media)), play=False)
        else:
            del self.media[root]
        self._notice(f"Medium removed: {name}")

    def _source_next(self, _: Any) -> None:
        roots = list(self.media)
        if not roots:
            self._notice("No media")
            return
        if len(roots) == 1:
            self._notice(f"Only medium: {self._medium_name(roots[0])}")
            return
        i = roots.index(self.active_root) if self.active_root in roots else -1
        nxt = roots[(i + 1) % len(roots)]
        was_playing = self.state.status == PLAYING
        self._activate(nxt, play=was_playing)
        self._notice(f"Medium: {self._medium_name(nxt)}")

    # playback event handlers --------------------------------------------------------
    def _play_pause(self, _: Any) -> None:
        if not self._has_media:
            log.info("play/pause: no media")
            return
        s = self.state
        if s.status == PLAYING:
            self.backend.set_paused(True)
            s.status = PAUSED
        elif s.status == PAUSED:
            self.backend.set_paused(False)
            s.status = PLAYING
        elif s.track_count:
            self._start(self._start_position)
            self._start_position = 0.0

    def _stop(self, _: Any) -> None:
        if not self._has_media:
            return
        self.backend.stop()
        self.state.status = STOPPED
        self._start_position = 0.0

    def _manual_step(self, delta: int) -> None:
        if self._has_media and self.state.track_count:
            self._step(delta, manual=True)

    def _prev(self, _: Any) -> None:
        if not self._has_media or not self.state.track_count:
            return
        if self.state.status in (PLAYING, PAUSED):
            pos = self.backend.position() or 0.0
            if pos > self.prev_restart_seconds:
                self.backend.seek(0)
                return
        self._step(-1, manual=True)

    def _folder(self, delta: int) -> None:
        if self._has_media and self.library.folders:
            self._move_folder(delta)

    def _volume(self, delta: int) -> None:
        s = self.state
        v = max(0, min(self.volume_max, s.volume + delta))
        if v != s.volume:
            s.volume = v
            self.backend.set_volume(v)

    def _track_end(self, reason: Any) -> None:
        s = self.state
        if not self._has_media or s.status not in (PLAYING, PAUSED):
            return  # stale event after stop/removal
        if reason == "error":
            self._errors += 1
            path = self._current_path or "?"
            if self.library.is_local and not os.path.exists(path):
                log.warning("%s vanished (media pulled?) - stopping", path)
                self.backend.stop()
                s.status = STOPPED
                return
            if self._errors >= self.max_consecutive_errors:
                log.error("%d unplayable files in a row - stopping", self._errors)
                self._errors = 0
                self.backend.stop()
                s.status = STOPPED
                return
            log.warning("cannot play %s - skipping", path)
        else:
            self._errors = 0
        # A load error while paused also lands here; _step keeps the status.
        self._step(+1, manual=False)

    # tags + playlists ---------------------------------------------------------
    def _track_info(self, info: Any) -> None:
        if not self._has_media or not isinstance(info, dict):
            return
        s = self.state
        # keep what we already know (network tags) when mpv reports nothing
        s.title = str(info.get("title") or "") or s.title
        s.artist = str(info.get("artist") or "") or s.artist
        s.album = str(info.get("album") or "") or s.album
        try:
            s.duration = float(info.get("duration") or 0.0) or s.duration
        except (TypeError, ValueError):
            pass

    def _toggle_in(self, name: str) -> Optional[bool]:
        """Add the current track to playlist `name`, or remove it if it's there.
        Returns True = added, False = removed, None = nothing happened."""
        if not self.playlist_dir:
            log.warning("playlists are disabled")
            return None
        s, lib = self.state, self.library
        rel = self._current_rel()
        if rel is None:
            self._notice("Nothing playing")
            return None
        if lib.playlists is None:
            self._notice("Playlists only on sticks")
            return None
        try:
            if lib.playlists.contains(name, rel):
                lib.playlists.remove(name, rel)
                added = False
            else:
                lib.playlists.add(name, rel)
                added = True
        except OSError as exc:
            self._notice(f"Cannot write {name}: {exc.strerror}")
            return None
        # playlists may have appeared / vanished / shrunk: re-find our place
        key = lib.folders[s.folder_index]
        old_index = s.folder_index
        lib.refresh_playlists()
        if key in lib.folders:
            s.folder_index = lib.folders.index(key)
            tracks = lib.tracks(s.folder_index)
            if key.startswith("playlist:") and self._current_path:
                cur = lib.rel_to_root(self._current_path)
                if cur in tracks:
                    s.track_index = tracks.index(cur)
                else:  # removed the playing song from the playlist being played
                    s.track_index = min(s.track_index, max(0, len(tracks) - 1))
        else:  # the playlist we're in became empty (the song keeps playing)
            playlists = self._view_indices("playlists")
            if playlists:  # next playlist along
                later = [i for i in playlists if i >= old_index]
                s.folder_index = later[0] if later else playlists[-1]
            else:          # no playlists left: back to the folders view
                mem = self._view_mem.get("folders")
                s.folder_index = lib.folders.index(mem[0]) if mem and mem[0] in lib.folders else 0
            s.track_index = 0
        s.folder_count = len(lib.folders)
        self._refresh_labels()
        return added

    # standby + poweroff --------------------------------------------------------
    def _standby(self, _: Any) -> None:
        s = self.state
        if s.standby:  # wake up
            s.standby = False
            if self._wake_resumes and self._has_media and s.track_count:
                self._start(self._start_position)
                self._start_position = 0.0
            self._wake_resumes = False
            self._notice("Awake")
            return
        self._wake_resumes = s.status == PLAYING
        if s.status in (PLAYING, PAUSED):
            self._start_position = self.backend.position() or 0.0
            self.backend.stop()  # mpv closes the audio device when idle
            s.status = STOPPED
        s.standby = True
        self._remember()
        self._notice("Standby")

    def _poweroff(self, _: Any) -> None:
        self._notice("Powering off")
        self.publish()
        self.bus.emit(Event.HALT)

    def _update(self, _: Any) -> None:
        if self.updater is None:
            self._notice("Updates are disabled")
            return

        def announce(msg: str) -> None:  # show "Updating..." before the slow part
            self._notice(msg)
            self.publish()

        msg, restart = self.updater.press(list(self.media), announce)
        self._notice(msg)
        if restart:
            self.bus.emit(Event.RESTART)

    def _view_toggle(self, _: Any) -> None:
        """Jump between the folders and the playlists, each at its last place."""
        s, lib = self.state, self.library
        if not self._has_media:
            self._notice("No media")
            return
        other = "playlists" if s.view == "folders" else "folders"
        targets = self._view_indices(other)
        if not targets:
            self._notice("No playlists" if other == "playlists" else "No folders")
            return
        # remember where we are in this view
        if s.status in (PLAYING, PAUSED):
            here = self.backend.position() or 0.0
        else:
            here = self._start_position
        self._view_mem[s.view] = (lib.folders[s.folder_index], s.track_index, here)
        # go to the last place in the other view (or its first entry)
        key, track, seconds = lib.folders[targets[0]], 0, 0.0
        mem = self._view_mem.get(other)
        if mem and mem[0] in lib.folders and lib.folders.index(mem[0]) in targets:
            fi = lib.folders.index(mem[0])
            if mem[1] < len(lib.tracks(fi)):
                key, track, seconds = mem
        s.folder_index, s.track_index = lib.folders.index(key), track
        self._refresh_labels()
        if s.track_count:
            if s.status == PLAYING:
                self._start(seconds)
            elif s.status == PAUSED:
                self._start(seconds, paused=True)
            else:
                self._start_position = seconds
        name = s.folder_name[len("[PL] "):] if s.in_playlist else s.folder_name
        self._notice(f"{other.capitalize()}: {name}")

    def _playlist_add(self, _: Any) -> None:
        name = self.state.active_playlist
        added = self._toggle_in(name)
        if added is not None:
            self._notice(f"Saved to {name}" if added else f"Removed from {name}")

    def _playlist_new(self, _: Any) -> None:
        if not self.playlist_dir:
            log.warning("playlist new: playlists are disabled")
            return
        if not self._has_media or self.library.playlists is None:
            self._notice("No stick for playlists")
            return
        try:
            name = self.library.playlists.create_numbered()
        except OSError as exc:
            self._notice(f"Cannot create playlist: {exc.strerror}")
            return
        self.state.active_playlist = name   # "save" goes there from now on
        self._refresh_playlist_flags()
        self._notice(f"Created {name}")

    def _like(self, _: Any) -> None:
        added = self._toggle_in(self.liked_playlist)
        if added is not None:
            self._notice("Liked" if added else "Unliked")

    def _playlist_next(self, _: Any) -> None:
        if not self.playlist_dir:
            log.warning("playlist next: playlists are disabled")
            return
        store = self.library.playlists if self._has_media else None
        names = [n for n in (store.names() if store else [])
                 if n != self.liked_playlist]   # liked has its own button
        if self.default_playlist not in names:
            names.append(self.default_playlist)
        cur = self.state.active_playlist
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self.state.active_playlist = nxt
        self._refresh_playlist_flags()
        self._notice(f"Playlist: {nxt}")
