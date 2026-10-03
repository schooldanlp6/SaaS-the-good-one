"""Load and validate config.toml.

Hardware details (audio device, pins) are never guessed: if something is
missing or wrong, load() raises ConfigError listing every problem at once.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List

import tomllib  # stdlib, Python >= 3.11

from .events import USER_EVENTS


class ConfigError(Exception):
    pass


END_MODES = ("continue", "repeat", "stop")
_FREEFORM = {"mpv_options", "keys", "buttons"}  # tables whose keys we don't predefine

DEFAULTS: Dict[str, Any] = {
    "audio": {
        "backend": "mpv",          # mpv | dummy
        "device": "",              # required for mpv; "auto" lets mpv pick
        "volume": 80,
        "volume_step": 5,
        "volume_max": 100,
        "mpv_options": {},         # passed straight to mpv, e.g. replaygain = "album"
        "dummy_track_seconds": 10.0,
    },
    "library": {
        "media_root": "",
        "extensions": ["flac", "mp3", "ogg", "opus", "m4a", "wav", "aiff", "wv", "ape"],
        "end_of_folder": "continue",
        "autoplay": False,
    },
    "playlists": {
        "enabled": True,
        "dir": "DLP6/Playlists",   # on the stick, relative to media_root
        "default": "Favorites",    # where "add" goes until you cycle
        "liked": "Liked Songs",    # the like button's playlist (not part of the cycle)
    },
    "state": {
        "file": "",                # empty = don't persist; written only on a clean exit
        "save_interval": None,     # obsolete (state is only saved on exit), still accepted
    },
    "sources": {
        "usb": {
            "enabled": True,
            # "multi": every mount point directly inside media_root is one medium
            #          (the udev helper mounts each stick at /media/usb/<label>)
            # "single": media_root itself is the one medium (old behaviour / laptop folder)
            "layout": "multi",
            "require_mountpoint": True,
            "poll_interval": 2.0,
        },
    },
    "inputs": {
        "keyboard": {"enabled": False, "keys": {}},
        "gpio": {"enabled": False, "bounce_ms": 30, "buttons": {}},
    },
    "gpio": {
        "chip": "auto",            # "auto" (find the Pi's chip), "/dev/gpiochipN", or "mock"
    },
    "outputs": {
        "console": {"enabled": True},
        "status": {
            "enabled": True,
            "dir": "/dev/shm/saas",  # tmpfs on every Linux, no root/init needed; never hits the SD card
            "interval": 1.0,       # position refresh while playing
        },
        "leds": {
            "enabled": False,
            "playing": None,
            "media": None,
            "folder_bits": [],
            "blink_on": 0.15,
            "blink_off": 0.3,
        },
    },
}


def _merge(defaults: Dict[str, Any], user: Dict[str, Any], path: str, errors: List[str]) -> Dict[str, Any]:
    out = copy.deepcopy(defaults)
    for key, value in user.items():
        where = f"{path}.{key}" if path else key
        if key not in defaults:
            errors.append(f"unknown option '{where}'")
            continue
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                errors.append(f"'{where}' must be a table")
                continue
            out[key] = dict(value) if key in _FREEFORM else _merge(defaults[key], value, where, errors)
        else:
            out[key] = value
    return out


def _num(section: Dict[str, Any], key: str, lo: float, hi: float, where: str, errors: List[str], integer: bool = False) -> None:
    v = section[key]
    ok_type = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok_type or not lo <= v <= hi:
        kind = "an integer" if integer else "a number"
        errors.append(f"'{where}.{key}' must be {kind} between {lo} and {hi} (got {v!r})")


def _pin(value: Any, where: str, errors: List[str]) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 27:
        errors.append(f"'{where}' must be a BCM GPIO number 2-27 (got {value!r}; 0/1 are reserved for the HAT EEPROM)")
        return False
    return True


def _parse_buttons(raw: Dict[str, Any], errors: List[str]) -> Dict[str, Dict[str, Any]]:
    buttons: Dict[str, Dict[str, Any]] = {}
    for name, spec in raw.items():
        where = f"inputs.gpio.buttons.{name}"
        if name not in USER_EVENTS:
            errors.append(f"'{where}': unknown event (valid: {', '.join(sorted(USER_EVENTS))})")
            continue
        if not isinstance(spec, dict):
            spec = {"pin": spec}
        extra = set(spec) - {"pin", "hold", "hold_time", "repeat"}
        if extra:
            errors.append(f"'{where}': unknown key(s) {', '.join(sorted(extra))}")
        if "pin" not in spec:
            errors.append(f"'{where}': missing 'pin'")
            continue
        if not _pin(spec["pin"], where, errors):
            continue
        hold = spec.get("hold")
        if hold is not None and hold not in USER_EVENTS:
            errors.append(f"'{where}.hold': unknown event {hold!r}")
            continue
        repeat = bool(spec.get("repeat", False))
        if repeat and hold is not None:
            errors.append(f"'{where}': 'repeat' and 'hold' can't be combined")
            continue
        hold_time = spec.get("hold_time", 0.4 if repeat else 1.0)
        if isinstance(hold_time, bool) or not isinstance(hold_time, (int, float)) or not 0.05 <= hold_time <= 10:
            errors.append(f"'{where}.hold_time' must be 0.05-10 seconds")
            continue
        buttons[name] = {"pin": spec["pin"], "hold": hold, "hold_time": float(hold_time), "repeat": repeat}
    return buttons


def validate(raw: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    cfg = _merge(DEFAULTS, raw, "", errors)

    a = cfg["audio"]
    if a["backend"] not in ("mpv", "dummy"):
        errors.append(f"'audio.backend' must be \"mpv\" or \"dummy\" (got {a['backend']!r})")
    if a["backend"] == "mpv" and (not isinstance(a["device"], str) or a["device"] in ("", "CHANGE_ME")):
        errors.append(
            "'audio.device' is not set. List devices with `python3 main.py --list-devices` and "
            "put your DAC's entry there (or \"auto\" to let mpv choose)"
        )
    _num(a, "volume_max", 1, 150, "audio", errors, integer=True)
    _num(a, "volume", 0, 150, "audio", errors, integer=True)
    _num(a, "volume_step", 1, 50, "audio", errors, integer=True)
    _num(a, "dummy_track_seconds", 0.1, 3600, "audio", errors)
    if not errors and a["volume"] > a["volume_max"]:
        errors.append("'audio.volume' is above 'audio.volume_max'")

    lib = cfg["library"]
    if not isinstance(lib["media_root"], str) or not lib["media_root"]:
        errors.append("'library.media_root' is required (e.g. \"/media/usb\")")
    else:
        lib["media_root"] = os.path.expanduser(lib["media_root"])
    if not isinstance(lib["extensions"], list) or not lib["extensions"] or \
            not all(isinstance(e, str) and e for e in lib["extensions"]):
        errors.append("'library.extensions' must be a non-empty list of strings")
    if lib["end_of_folder"] not in END_MODES:
        errors.append(f"'library.end_of_folder' must be one of {', '.join(END_MODES)}")

    pl = cfg["playlists"]
    if pl["enabled"]:
        from .playlists import clean_rel, sanitize_name
        if not isinstance(pl["dir"], str) or clean_rel(pl["dir"]) is None:
            errors.append("'playlists.dir' must be a relative path on the stick, e.g. \"DLP6/Playlists\"")
        else:
            pl["dir"] = clean_rel(pl["dir"])
        for key in ("default", "liked"):
            v = pl[key]
            if not isinstance(v, str) or not v or sanitize_name(v) != v:
                errors.append(f"'playlists.{key}' must be a plain name (no / \\ : * ? \" < > |)")
        if pl["default"] == pl["liked"]:
            errors.append("'playlists.default' and 'playlists.liked' must differ")

    status = cfg["outputs"]["status"]
    if status["enabled"]:
        if not isinstance(status["dir"], str) or not status["dir"]:
            errors.append("'outputs.status.dir' must be a directory path")
        else:
            status["dir"] = os.path.expanduser(status["dir"])
        _num(status, "interval", 0.2, 60, "outputs.status", errors)

    st = cfg["state"]
    if not isinstance(st["file"], str):
        errors.append("'state.file' must be a string (empty = disabled)")
    elif st["file"]:
        st["file"] = os.path.expanduser(st["file"])

    usb = cfg["sources"]["usb"]
    if usb["layout"] not in ("multi", "single"):
        errors.append("'sources.usb.layout' must be \"multi\" or \"single\"")
    _num(usb, "poll_interval", 1, 60, "sources.usb", errors)

    kb = cfg["inputs"]["keyboard"]
    for key, name in kb["keys"].items():
        if len(key) != 1:
            errors.append(f"'inputs.keyboard.keys': key {key!r} must be a single character")
        elif name != "quit" and name not in USER_EVENTS:
            errors.append(f"'inputs.keyboard.keys.{key}': unknown event {name!r}")

    if not isinstance(cfg["gpio"]["chip"], str):
        errors.append("'gpio.chip' must be a string (\"auto\", \"/dev/gpiochip0\" or \"mock\")")

    used: Dict[int, str] = {}

    def claim(pin: int, who: str) -> None:
        if pin in used:
            errors.append(f"GPIO {pin} is used twice ({used[pin]} and {who})")
        used[pin] = who

    gpio = cfg["inputs"]["gpio"]
    if gpio["enabled"]:
        _num(gpio, "bounce_ms", 0, 500, "inputs.gpio", errors, integer=True)
        gpio["buttons"] = _parse_buttons(gpio["buttons"], errors)
        if not gpio["buttons"] and not any("inputs.gpio.buttons" in e for e in errors):
            errors.append("'inputs.gpio' is enabled but no buttons are configured")
        for name, spec in gpio["buttons"].items():
            claim(spec["pin"], f"button {name}")

    leds = cfg["outputs"]["leds"]
    if leds["enabled"]:
        for key in ("playing", "media"):
            if leds[key] is not None and _pin(leds[key], f"outputs.leds.{key}", errors):
                claim(leds[key], f"led {key}")
        bits = leds["folder_bits"]
        if not isinstance(bits, list):
            errors.append("'outputs.leds.folder_bits' must be a list of pins")
        else:
            for i, p in enumerate(bits):
                if _pin(p, f"outputs.leds.folder_bits[{i}]", errors):
                    claim(p, f"led folder bit {i}")
        if leds["playing"] is None and leds["media"] is None and not bits:
            errors.append("'outputs.leds' is enabled but no LED pins are configured")
        _num(leds, "blink_on", 0.02, 2, "outputs.leds", errors)
        _num(leds, "blink_off", 0.02, 2, "outputs.leds", errors)

    if errors:
        raise ConfigError("\n".join(f"  - {e}" for e in errors))
    return cfg


def load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"  - config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"  - {path}: {exc}") from None
    return validate(raw)
