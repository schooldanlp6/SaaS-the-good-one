"""python-mpv backend: audio only, straight to ALSA (e.g. the Scarlett)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import mpv  # python-mpv; needs the system libmpv

from .base import AudioBackend

log = logging.getLogger(__name__)

# mpv_end_file_reason values from libmpv's client.h
_EOF, _ERROR = 0, 4
_REASON_NAMES = {"eof": 0, "stop": 2, "quit": 3, "error": 4, "redirect": 5}

_MPV_LOG = {"fatal": logging.CRITICAL, "error": logging.ERROR, "warn": logging.WARNING,
            "info": logging.INFO}


def _end_reason(event: Any) -> Optional[int]:
    """Pull the end-file reason out of the event. python-mpv 1.x hands over
    objects, 0.5.x hands over dicts."""
    data = getattr(event, "data", None)
    if data is not None:
        reason = getattr(data, "reason", None)
    elif isinstance(event, dict):
        reason = (event.get("event") or {}).get("reason")
    else:
        reason = None
    if isinstance(reason, str):
        return _REASON_NAMES.get(reason.lower())
    try:
        return int(reason)
    except (TypeError, ValueError):
        return None


def _log_handler(level: str, prefix: str, text: str) -> None:
    log.log(_MPV_LOG.get(level, logging.DEBUG), "[%s] %s", prefix, text.rstrip())


def _base_options() -> Dict[str, Any]:
    return dict(
        video=False,                  # never decode cover art / video
        ytdl=False,
        input_default_bindings=False,
        input_vo_keyboard=False,
        idle=True,                    # stay alive between files
    )


class MpvBackend(AudioBackend):
    def __init__(self, device: str = "auto", options: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        opts = _base_options()
        if device and device != "auto":
            opts["audio_device"] = device
        opts.update(options or {})
        self._mpv = mpv.MPV(log_handler=_log_handler, loglevel="warn", **opts)
        self._mpv.event_callback("end-file")(self._on_end_file)
        self._mpv.event_callback("file-loaded")(self._on_file_loaded)

    def _on_file_loaded(self, event: Any) -> None:
        try:
            meta = {str(k).lower(): v for k, v in (self._mpv.metadata or {}).items()}
            duration = self._mpv.duration
        except Exception:
            log.debug("reading metadata failed", exc_info=True)
            return
        self._info({
            "title": str(meta.get("title", "")),
            "artist": str(meta.get("artist") or meta.get("album_artist") or meta.get("albumartist") or ""),
            "album": str(meta.get("album", "")),
            "duration": float(duration or 0.0),
        })

    def _on_end_file(self, event: Any) -> None:
        reason = _end_reason(event)
        if reason == _EOF:
            self._ended("eof")
        elif reason == _ERROR:
            self._ended("error")
        # stop / quit / redirect: we caused it, ignore

    def play(self, path: str, start: float = 0.0, paused: bool = False) -> None:
        self._mpv.pause = paused
        if start and start > 0:
            self._mpv.loadfile(path, start=f"{start:.2f}")
        else:
            self._mpv.loadfile(path)

    def set_paused(self, paused: bool) -> None:
        self._mpv.pause = paused

    def stop(self) -> None:
        self._mpv.command("stop")

    def seek(self, seconds: float) -> None:
        self._mpv.seek(seconds, reference="absolute", precision="exact")

    def position(self) -> Optional[float]:
        try:
            return self._mpv.time_pos
        except Exception:  # property unavailable while nothing is loaded
            return None

    def set_volume(self, volume: int) -> None:
        self._mpv.volume = volume

    def close(self) -> None:
        try:
            self._mpv.terminate()
        except Exception:
            log.debug("mpv terminate failed", exc_info=True)


def list_devices() -> List[Dict[str, str]]:
    player = mpv.MPV(video=False, idle=True)
    try:
        return list(player.audio_device_list or [])
    finally:
        player.terminate()
