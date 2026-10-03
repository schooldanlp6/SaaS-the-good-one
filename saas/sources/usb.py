"""Watches for media. Mounting itself is the OS's job (see init/saas-automount);
this only notices what is (or isn't) there anymore.

layout "multi":  every mount point directly inside `path` is one medium
                 (the udev helper mounts each stick at /media/usb/<label>)
layout "single": `path` itself is the one medium

With require_mountpoint = false (laptop dev), any non-empty directory counts,
so in "multi" a folder of folders simulates several sticks.

A cheap poll (a stat or two and one directory read every couple of seconds)
works the same on every distro and init system, which inotify/udev hooks don't.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Set

from ..core.events import Bus, Event
from .base import Source

log = logging.getLogger(__name__)


class UsbSource(Source):
    def __init__(self, bus: Bus, path: str, require_mountpoint: bool = True,
                 poll_interval: float = 2.0, layout: str = "single") -> None:
        super().__init__(bus)
        self.path = os.path.abspath(path)
        self.require_mountpoint = require_mountpoint
        self.poll_interval = poll_interval
        self.layout = layout
        self._present: Set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _usable(self, path: str) -> bool:
        if self.require_mountpoint and not os.path.ismount(path):
            return False
        try:
            with os.scandir(path) as it:  # also catches a yanked-but-still-mounted stick (EIO)
                return next(it, None) is not None
        except OSError:
            return False

    def scan(self) -> Set[str]:
        if self.layout == "single":
            return {self.path} if self._usable(self.path) else set()
        found = set()
        try:
            with os.scandir(self.path) as it:
                for e in it:
                    if not e.name.startswith(".") and e.is_dir(follow_symlinks=False) and self._usable(e.path):
                        found.add(e.path)
        except OSError:
            pass
        return found

    def check(self) -> None:
        now = self.scan()
        for gone in sorted(self._present - now):
            log.info("medium gone: %s", gone)
            self.bus.emit(Event.MEDIA_REMOVED, gone)
        for new in sorted(now - self._present):
            log.info("medium found: %s", new)
            self.bus.emit(Event.MEDIA_ADDED, new)
        self._present = now

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.check()
            except Exception:
                log.exception("usb watcher failed")

    def start(self) -> None:
        self.check()
        if not self._present:
            log.info("waiting for media in %s", self.path)
        self._thread = threading.Thread(target=self._run, name="usb-watch", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
