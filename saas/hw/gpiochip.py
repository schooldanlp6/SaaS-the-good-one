"""GPIO via the kernel's character device (/dev/gpiochipN), uAPI v2.

Pure standard library (ctypes + fcntl), no root needed: the installer adds a
udev rule so the "gpio" group can open /dev/gpiochip*. Needs Linux >= 5.10.

Pull-ups, edge detection and debouncing are all done by the kernel. On a Pi
the line offsets on the main chip are the BCM GPIO numbers.

The struct layouts below mirror <linux/gpio.h>; tests/test_gpiochip.py checks
them against the real header with a C compiler.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import logging
import os
import select
import struct
import threading
from typing import Callable, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# --- <linux/gpio.h> ------------------------------------------------------------
GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINES_MAX = 64
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

FLAG_ACTIVE_LOW = 1 << 1
FLAG_INPUT = 1 << 2
FLAG_OUTPUT = 1 << 3
FLAG_EDGE_RISING = 1 << 4
FLAG_EDGE_FALLING = 1 << 5
FLAG_BIAS_PULL_UP = 1 << 8

ATTR_ID_FLAGS = 1
ATTR_ID_OUTPUT_VALUES = 2
ATTR_ID_DEBOUNCE = 3

EVENT_RISING_EDGE = 1
EVENT_FALLING_EDGE = 2


class gpiochip_info(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * GPIO_MAX_NAME_SIZE),
                ("label", ctypes.c_char * GPIO_MAX_NAME_SIZE),
                ("lines", ctypes.c_uint32)]


class _attr_union(ctypes.Union):
    _fields_ = [("flags", ctypes.c_uint64),
                ("values", ctypes.c_uint64),
                ("debounce_period_us", ctypes.c_uint32)]


class gpio_v2_line_attribute(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("id", ctypes.c_uint32),
                ("padding", ctypes.c_uint32),
                ("u", _attr_union)]


class gpio_v2_line_config_attribute(ctypes.Structure):
    _fields_ = [("attr", gpio_v2_line_attribute),
                ("mask", ctypes.c_uint64)]


class gpio_v2_line_config(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64),
                ("num_attrs", ctypes.c_uint32),
                ("padding", ctypes.c_uint32 * 5),
                ("attrs", gpio_v2_line_config_attribute * GPIO_V2_LINE_NUM_ATTRS_MAX)]


class gpio_v2_line_request(ctypes.Structure):
    _fields_ = [("offsets", ctypes.c_uint32 * GPIO_V2_LINES_MAX),
                ("consumer", ctypes.c_char * GPIO_MAX_NAME_SIZE),
                ("config", gpio_v2_line_config),
                ("num_lines", ctypes.c_uint32),
                ("event_buffer_size", ctypes.c_uint32),
                ("padding", ctypes.c_uint32 * 5),
                ("fd", ctypes.c_int32)]


class gpio_v2_line_values(ctypes.Structure):
    _fields_ = [("bits", ctypes.c_uint64),
                ("mask", ctypes.c_uint64)]


EVENT_FORMAT = "=QIIII24x"  # timestamp_ns, id, offset, seqno, line_seqno, padding[6]
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (0xB4 << 8) | nr


_READ, _READ_WRITE = 2, 3
GPIO_GET_CHIPINFO_IOCTL = _ioc(_READ, 0x01, ctypes.sizeof(gpiochip_info))
GPIO_V2_GET_LINE_IOCTL = _ioc(_READ_WRITE, 0x07, ctypes.sizeof(gpio_v2_line_request))
GPIO_V2_LINE_SET_VALUES_IOCTL = _ioc(_READ_WRITE, 0x0F, ctypes.sizeof(gpio_v2_line_values))

# Sizes fixed by the kernel ABI (identical on every architecture). If ctypes
# lays something out differently here, refuse instead of sending garbage.
_ABI_SIZES = {gpiochip_info: 68, gpio_v2_line_request: 592, gpio_v2_line_config: 272,
              gpio_v2_line_values: 16}
_ABI_OK = all(ctypes.sizeof(t) == n for t, n in _ABI_SIZES.items()) and EVENT_SIZE == 48

# Callback for inputs: fn(pin, pressed). Buttons are wired pin -> GND with the
# pull-up on, so "pressed" means the line went low.
InputCallback = Callable[[int, bool], None]


class GpioError(Exception):
    pass


# --- chip discovery -----------------------------------------------------------
def chip_info(path: str) -> gpiochip_info:
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        info = gpiochip_info()
        fcntl.ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, info, True)
        return info
    finally:
        os.close(fd)


def find_chip() -> str:
    """The Pi's main GPIO chip (label pinctrl-bcm2835 / -bcm2711 / ...)."""
    paths = sorted(glob.glob("/dev/gpiochip*"))
    if not paths:
        raise GpioError("no /dev/gpiochip* found (kernel without GPIO chardev?)")
    seen = []
    for path in paths:
        try:
            info = chip_info(path)
        except OSError as exc:
            seen.append(f"{path}: {exc.strerror}")
            continue
        label = info.label.decode(errors="replace")
        seen.append(f"{path}: {label} ({info.lines} lines)")
        if label.startswith("pinctrl-bcm") or label.startswith("pinctrl-rp1"):
            return path
    raise GpioError("no Raspberry Pi GPIO chip found; set [gpio] chip explicitly. Found: " + "; ".join(seen))


# --- real chip ------------------------------------------------------------------
class OutputLine:
    def __init__(self, fd: int, pin: int) -> None:
        self._fd = fd
        self.pin = pin
        self._lock = threading.Lock()

    def set(self, value: bool) -> None:
        vals = gpio_v2_line_values(bits=1 if value else 0, mask=1)
        with self._lock:
            if self._fd >= 0:
                fcntl.ioctl(self._fd, GPIO_V2_LINE_SET_VALUES_IOCTL, vals, True)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


class Chip:
    def __init__(self, path: str) -> None:
        self.path = path
        if not _ABI_OK:
            raise GpioError("ctypes struct layout doesn't match the kernel GPIO ABI on this platform")
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except OSError as exc:
            hint = " (is the user in the 'gpio' group? see init/60-saas-gpio.rules)" \
                if exc.errno in (13, 1) else ""
            raise GpioError(f"cannot open {path}: {exc.strerror}{hint}") from None
        self._threads: List[threading.Thread] = []
        self._line_fds: List[int] = []
        self._stop_r, self._stop_w = os.pipe()

    def _request(self, pins: Sequence[int], flags: int, debounce_ms: int = 0,
                 initial: Optional[bool] = None) -> int:
        if not 0 < len(pins) <= GPIO_V2_LINES_MAX:
            raise GpioError(f"bad number of lines: {len(pins)}")
        req = gpio_v2_line_request()
        for i, pin in enumerate(pins):
            req.offsets[i] = pin
        req.num_lines = len(pins)
        req.consumer = b"saas"
        req.config.flags = flags
        mask_all = (1 << len(pins)) - 1
        n = 0
        if debounce_ms:
            a = req.config.attrs[n]
            a.attr.id = ATTR_ID_DEBOUNCE
            a.attr.debounce_period_us = debounce_ms * 1000
            a.mask = mask_all
            n += 1
        if initial is not None:
            a = req.config.attrs[n]
            a.attr.id = ATTR_ID_OUTPUT_VALUES
            a.attr.values = mask_all if initial else 0
            a.mask = mask_all
            n += 1
        req.config.num_attrs = n
        try:
            fcntl.ioctl(self._fd, GPIO_V2_GET_LINE_IOCTL, req, True)
        except OSError as exc:
            raise GpioError(f"cannot request GPIO {list(pins)} on {self.path}: {exc.strerror}"
                            " (pin in use by another driver/program?)") from None
        self._line_fds.append(req.fd)
        return req.fd

    def output(self, pin: int, initial: bool = False) -> OutputLine:
        return OutputLine(self._request([pin], FLAG_OUTPUT, initial=initial), pin)

    def watch_inputs(self, pins: Sequence[int], debounce_ms: int, callback: InputCallback) -> None:
        flags = FLAG_INPUT | FLAG_BIAS_PULL_UP | FLAG_EDGE_RISING | FLAG_EDGE_FALLING
        fd = self._request(list(pins), flags, debounce_ms)
        t = threading.Thread(target=self._event_loop, args=(fd, callback), name="gpio-events", daemon=True)
        t.start()
        self._threads.append(t)

    def _event_loop(self, fd: int, callback: InputCallback) -> None:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        poller.register(self._stop_r, select.POLLIN)
        while True:
            for ready_fd, _ in poller.poll():
                if ready_fd == self._stop_r:
                    return
                try:
                    data = os.read(fd, EVENT_SIZE * 16)
                except OSError:
                    return
                for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _, event_id, offset, _, _ = struct.unpack_from(EVENT_FORMAT, data, i)
                    try:
                        callback(offset, event_id == EVENT_FALLING_EDGE)
                    except Exception:
                        log.exception("gpio callback failed")

    def close(self) -> None:
        os.write(self._stop_w, b"x")
        for t in self._threads:
            t.join(timeout=1)
        for fd in self._line_fds + [self._fd, self._stop_r, self._stop_w]:
            try:
                os.close(fd)
            except OSError:
                pass
        self._line_fds.clear()


# --- mock chip (laptop dev + tests) ---------------------------------------------------
class MockOutput:
    def __init__(self, chip: "MockChip", pin: int) -> None:
        self.chip, self.pin = chip, pin

    def set(self, value: bool) -> None:
        self.chip.outputs[self.pin] = bool(value)

    def close(self) -> None:
        pass


class MockChip:
    path = "mock"

    def __init__(self) -> None:
        self.outputs: Dict[int, bool] = {}
        self._callbacks: Dict[int, InputCallback] = {}

    def output(self, pin: int, initial: bool = False) -> MockOutput:
        self.outputs[pin] = initial
        return MockOutput(self, pin)

    def watch_inputs(self, pins: Sequence[int], debounce_ms: int, callback: InputCallback) -> None:
        for pin in pins:
            self._callbacks[pin] = callback

    def drive(self, pin: int, pressed: bool) -> None:
        """Simulate a button edge."""
        self._callbacks[pin](pin, pressed)

    def close(self) -> None:
        pass


def open_chip(spec: str):
    """spec: "auto", a path like "/dev/gpiochip0", or "mock"."""
    if spec == "mock":
        return MockChip()
    path = find_chip() if spec in ("", "auto") else spec
    chip = Chip(path)
    log.info("using GPIO chip %s", path)
    return chip
