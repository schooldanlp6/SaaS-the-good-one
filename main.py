#!/usr/bin/env python3
"""SaaS - ScriptAsASystem. A tiny headless music player core.

    python3 main.py --config config/dev.toml
    python3 main.py --list-devices
    python3 main.py --config /etc/saas/config.toml --check
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import Any, Dict, List

from saas.core import config as config_mod
from saas.core.events import Bus, Event
from saas.core.player import Player
from saas.core.resume import ResumeStore

log = logging.getLogger("saas")


class Shutdown(BaseException):
    """Raised from the SIGTERM handler; BaseException so nothing swallows it."""


def _on_sigterm(signum: int, frame: Any) -> None:
    raise Shutdown()


def make_backend(audio: Dict[str, Any]):
    if audio["backend"] == "dummy":
        from saas.backends.dummy import DummyBackend
        return DummyBackend(audio["dummy_track_seconds"])
    from saas.backends.mpv_backend import MpvBackend
    return MpvBackend(audio["device"], audio["mpv_options"])


def build(cfg: Dict[str, Any], bus: Bus, config_path: str = ""):
    audio, lib, st = cfg["audio"], cfg["library"], cfg["state"]
    updater = None
    if cfg["update"]["enabled"]:
        from saas.core.updater import Updater
        updater = Updater(os.path.dirname(os.path.abspath(__file__)), os.path.abspath(config_path),
                          cfg["update"]["file"], cfg["update"]["confirm_seconds"])
    player = Player(
        bus, make_backend(audio),
        extensions=lib["extensions"],
        end_of_folder=lib["end_of_folder"],
        autoplay=lib["autoplay"],
        volume=audio["volume"],
        volume_step=audio["volume_step"],
        volume_max=audio["volume_max"],
        resume=ResumeStore(st["file"]) if st["file"] else None,
        playlist_dir=cfg["playlists"]["dir"] if cfg["playlists"]["enabled"] else None,
        default_playlist=cfg["playlists"]["default"],
        liked_playlist=cfg["playlists"]["liked"],
        updater=updater,
    )

    # GPIO is optional hardware: if the chip can't be opened, log it and run
    # without buttons/LEDs instead of crash-looping the service.
    chip = None
    leds, gpio = cfg["outputs"]["leds"], cfg["inputs"]["gpio"]
    if leds["enabled"] or gpio["enabled"]:
        from saas.hw.gpiochip import open_chip
        try:
            chip = open_chip(cfg["gpio"]["chip"])
        except Exception as exc:
            log.error("GPIO disabled: %s", exc)

    outputs: List[Any] = []
    if cfg["outputs"]["console"]["enabled"]:
        from saas.outputs.console import ConsoleOutput
        outputs.append(ConsoleOutput())
    if leds["enabled"] and chip is not None:
        from saas.outputs.leds import LedOutput
        try:
            outputs.append(LedOutput(chip, leds["playing"], leds["media"], leds["folder_bits"],
                                     leds["blink_on"], leds["blink_off"]))
        except Exception as exc:
            log.error("LEDs disabled: %s", exc)
    status = cfg["outputs"]["status"]
    if status["enabled"]:
        from saas.outputs.status import StatusOutput
        outputs.append(StatusOutput(status["dir"], player.backend.position, status["interval"]))
    for out in outputs:
        player.add_listener(out.update)

    inputs: List[Any] = []
    kb = cfg["inputs"]["keyboard"]
    if kb["enabled"]:
        from saas.inputs.keyboard import KeyboardInput
        inputs.append(KeyboardInput(bus, kb["keys"]))
    if gpio["enabled"] and chip is not None:
        from saas.inputs.gpio import GpioInput
        try:
            inputs.append(GpioInput(bus, chip, gpio["buttons"], gpio["bounce_ms"]))
        except Exception as exc:
            log.error("buttons disabled: %s", exc)

    usb = cfg["sources"]["usb"]
    if usb["enabled"]:
        from saas.sources.usb import UsbSource
        inputs.append(UsbSource(bus, lib["media_root"], usb["require_mountpoint"], usb["poll_interval"],
                                layout=usb["layout"]))

    return player, inputs, outputs, chip


def run(cfg: Dict[str, Any], config_path: str = "") -> int:
    bus = Bus()
    player, inputs, outputs, chip = build(cfg, bus, config_path)
    restart = False
    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        player.publish(force=True)
        for plugin in inputs:
            plugin.start()
        while True:
            event, data = bus.get()
            if event is Event.QUIT:
                break
            if event is Event.RESTART:
                restart = True
                break
            player.handle(event, data)
    except (KeyboardInterrupt, Shutdown):
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        log.info("shutting down")
        for plugin in reversed(inputs):
            _safe(plugin.close)
        _safe(player.close)
        _safe(player.backend.close)
        for out in outputs:
            _safe(out.close)
        if chip is not None:
            _safe(chip.close)
    if restart:
        # After an OTA update: start the new code in place of this process. Works the
        # same under any init system or when started by hand (same PID, same terminal).
        log.info("restarting")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
    return 0


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        log.exception("error during shutdown in %r", fn)


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="SaaS - ScriptAsASystem music player")
    ap.add_argument("-c", "--config", default="/etc/saas/config.toml")
    ap.add_argument("--check", action="store_true", help="validate the config and exit")
    ap.add_argument("--list-devices", action="store_true", help="list mpv audio devices and exit")
    ap.add_argument("--log-level", default=os.environ.get("SAAS_LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.list_devices:
        from saas.backends.mpv_backend import list_devices
        for dev in list_devices():
            print(f"{dev.get('name', ''):45} {dev.get('description', '')}")
        print('\nPut the "name" of your DAC into [audio] device. For ALSA prefer the '
              '"alsa/plughw:..." entry (it converts sample formats if needed).')
        return 0

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"config error in {args.config}:\n{exc}", file=sys.stderr)
        return 2
    if args.check:
        print("config OK")
        return 0
    return run(cfg, args.config)


if __name__ == "__main__":
    sys.exit(main())
