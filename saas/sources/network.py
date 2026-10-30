"""One background thread per configured server: connect + load the album list
OFF the main thread (a slow server never freezes the buttons), then announce
the server as a medium. Checks every `poll` seconds whether it's still there;
gone -> MEDIA_REMOVED, back -> loaded and announced again."""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from ..core.events import Bus, Event
from ..net.base import NetLibrary
from .base import Source

log = logging.getLogger(__name__)


class NetSource(Source):
    def __init__(self, bus: Bus, factory: Callable[[], NetLibrary], poll: float = 60.0) -> None:
        super().__init__(bus)
        self.factory = factory
        self.poll = poll
        self.library: Optional[NetLibrary] = None
        self._last_error = ""
        self._stop = threading.Event()

    def check(self) -> None:
        if self.library is None:
            lib = self.factory()
            try:
                lib.scan()
            except Exception as exc:
                self._report(f"{lib.root}: {exc}")
                return
            if not lib.folders:
                self._report(f"{lib.root}: connected, but no music found")
                return
            self._last_error = ""
            self.library = lib
            self.bus.emit(Event.MEDIA_ADDED, lib)
        else:
            try:
                self.library.ping()
            except Exception as exc:
                log.warning("%s is gone: %s", self.library.root, exc)
                self.bus.emit(Event.MEDIA_REMOVED, self.library.root)
                self.library = None

    def _report(self, msg: str) -> None:
        if msg != self._last_error:  # log each new problem once, not every poll
            log.warning("%s", msg)
            self._last_error = msg

    def _run(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("network source failed")
            if self._stop.wait(self.poll):
                return

    def start(self) -> None:
        threading.Thread(target=self._run, name="net-source", daemon=True).start()

    def close(self) -> None:
        self._stop.set()


def make_factory(spec: dict) -> Callable[[], NetLibrary]:
    """Config entry -> a function that builds a fresh (unscanned) library."""
    kind = spec["type"]
    if kind == "jellyfin":
        from ..net.jellyfin import JellyfinLibrary
        return lambda: JellyfinLibrary(spec["name"], spec["url"], spec["api_key"], spec.get("user", ""))
    if kind == "subsonic":
        from ..net.subsonic import SubsonicLibrary
        return lambda: SubsonicLibrary(spec["name"], spec["url"], spec["user"], spec["password"],
                                       spec.get("auth", "token"))
    if kind == "dlna":
        from ..net.dlna import DlnaLibrary
        return lambda: DlnaLibrary(spec["name"], spec.get("location", ""), spec.get("server", ""),
                                   spec.get("start", ""))
    raise ValueError(f"unknown network type {kind!r}")
