"""Buttons + LEDs against the mock chip, USB watcher on a temp dir."""
import shutil
import time

import pytest

from saas.core.events import Bus, Event
from saas.core.state import PAUSED, PLAYING, STOPPED, PlayerState
from saas.hw.gpiochip import MockChip
from saas.inputs.gpio import GpioInput
from saas.outputs.leds import LedOutput
from saas.sources.usb import UsbSource


def drain(bus, timeout=0.3):
    out, deadline = [], time.monotonic() + timeout
    while True:
        item = bus.get(timeout=max(0.0, deadline - time.monotonic()))
        if item is None:
            return out
        out.append(item[0])


def btn(pin, hold=None, hold_time=1.0, repeat=False):
    return {"pin": pin, "hold": hold, "hold_time": hold_time, "repeat": repeat}


@pytest.fixture
def chip():
    return MockChip()


def test_plain_button(chip):
    bus = Bus()
    GpioInput(bus, chip, {"next": btn(27)})
    chip.drive(27, True)
    chip.drive(27, False)
    assert drain(bus) == [Event.NEXT]


def test_duplicate_edges_ignored(chip):
    bus = Bus()
    GpioInput(bus, chip, {"next": btn(27)})
    chip.drive(27, True); chip.drive(27, True)
    chip.drive(27, False); chip.drive(27, False)
    assert drain(bus) == [Event.NEXT]


def test_short_vs_long_press(chip):
    bus = Bus()
    GpioInput(bus, chip, {"play_pause": btn(17, hold="stop", hold_time=0.1)})
    chip.drive(17, True); chip.drive(17, False)
    assert drain(bus) == [Event.PLAY_PAUSE]
    chip.drive(17, True); time.sleep(0.25); chip.drive(17, False)
    assert drain(bus) == [Event.STOP]


def test_repeat_button(chip):
    bus = Bus()
    GpioInput(bus, chip, {"vol_up": btn(25, hold_time=0.1, repeat=True)})
    chip.drive(25, True); time.sleep(0.35); chip.drive(25, False)
    n = len(drain(bus))
    assert 3 <= n <= 5
    time.sleep(0.3)
    assert drain(bus) == []          # stops repeating after release


def test_unknown_pin_ignored(chip):
    bus = Bus()
    gi = GpioInput(bus, chip, {"next": btn(27)})
    gi._edge(4, True)
    assert drain(bus) == []


def test_leds(chip):
    leds = LedOutput(chip, playing=6, media=13, folder_bits=[19, 26])
    out = chip.outputs
    leds.update(PlayerState())
    assert not out[6] and not out[13]
    s = PlayerState(status=PLAYING, media=True, folder_index=2, folder_count=5, view_index=3)   # folder 3 = 0b11
    leds.update(s)
    assert out[6] and out[13] and out[19] and out[26]
    s.folder_index, s.view_index = 3, 4                                                           # folder 4 wraps to 0b00
    leds.update(s.snapshot())
    assert not out[19] and not out[26]
    s.status = STOPPED
    leds.update(s.snapshot())
    assert not out[6]
    s.status = PAUSED
    leds.update(s.snapshot())
    seen = set()
    for _ in range(12):
        seen.add(out[6]); time.sleep(0.1)
    assert seen == {True, False}       # blinking
    leds.close()
    assert not out[6] and not out[13]


def test_led_blink_count_then_on(chip):
    leds = LedOutput(chip, media=13, blink_on=0.03, blink_off=0.03)
    leds.update(PlayerState(status=STOPPED, media=True, folder_index=0, folder_count=3, view_index=1))
    time.sleep(0.2)
    leds.update(PlayerState(status=STOPPED, media=True, folder_index=1, folder_count=3, view_index=2))
    time.sleep(0.4)
    assert chip.outputs[13]   # back on after blinking "2"
    leds.update(PlayerState())
    assert not chip.outputs[13]
    leds.close()


def test_usb_watch(tmp_path):
    bus = Bus()
    path = tmp_path / "usb"
    src = UsbSource(bus, str(path), require_mountpoint=False, poll_interval=1, layout="single")
    src.check()
    assert bus.get(0) is None
    path.mkdir()
    src.check()
    assert bus.get(0) is None
    (path / "a.mp3").write_bytes(b"")
    src.check()
    assert bus.get(0) == (Event.MEDIA_ADDED, str(path))
    src.check()
    assert bus.get(0) is None
    shutil.rmtree(path)
    src.check()
    assert bus.get(0) == (Event.MEDIA_REMOVED, str(path))


def test_usb_requires_mountpoint(tmp_path):
    bus = Bus()
    (tmp_path / "a.mp3").write_bytes(b"")
    UsbSource(bus, str(tmp_path), require_mountpoint=True, layout="single").check()
    assert bus.get(0) is None


def test_usb_multi_layout(tmp_path):
    bus = Bus()
    src = UsbSource(bus, str(tmp_path), require_mountpoint=False, layout="multi")
    (tmp_path / "STICK_A").mkdir()
    (tmp_path / "STICK_A" / "a.mp3").write_bytes(b"")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.mp3").write_bytes(b"")
    src.check()
    assert drain(bus, 0.05) == [Event.MEDIA_ADDED]
    (tmp_path / "STICK_B").mkdir()
    (tmp_path / "STICK_B" / "b.mp3").write_bytes(b"")
    shutil.rmtree(tmp_path / "STICK_A")
    src.check()
    assert bus.get(0) == (Event.MEDIA_REMOVED, str(tmp_path / "STICK_A"))
    assert bus.get(0) == (Event.MEDIA_ADDED, str(tmp_path / "STICK_B"))
    assert UsbSource(Bus(), str(tmp_path), require_mountpoint=True, layout="multi").scan() == set()


def test_led_folder_number_counts_inside_the_view(chip):
    """0.6.4 fix: in the playlists view the 1st playlist is 1, not 'number of folders + 1'."""
    leds = LedOutput(chip, media=13, folder_bits=[19, 26])
    leds.update(PlayerState(status=PLAYING, media=True, folder_index=7, view="playlists",
                            view_index=1, view_count=2))
    assert chip.outputs[19] and not chip.outputs[26]          # 1 = 0b01
    leds.close()
