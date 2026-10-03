"""The ioctl layer: struct layouts vs. the real <linux/gpio.h>, and the event
reader fed with real event bytes through a pipe. (No GPIO hardware needed.)"""
import ctypes
import os
import shutil
import struct
import subprocess
import threading
import time

import pytest

from saas.hw import gpiochip as g

C_SRC = r"""
#include <stdio.h>
#include <stddef.h>
#include <linux/gpio.h>
int main(void){
  printf("%zu %zu %zu %zu %zu %zu %zu\n", sizeof(struct gpiochip_info), sizeof(struct gpio_v2_line_attribute),
         sizeof(struct gpio_v2_line_config_attribute), sizeof(struct gpio_v2_line_config),
         sizeof(struct gpio_v2_line_request), sizeof(struct gpio_v2_line_values), sizeof(struct gpio_v2_line_event));
  printf("%zu %zu %zu %zu %zu\n", offsetof(struct gpio_v2_line_request, consumer), offsetof(struct gpio_v2_line_request, config),
         offsetof(struct gpio_v2_line_request, num_lines), offsetof(struct gpio_v2_line_request, fd),
         offsetof(struct gpio_v2_line_config, attrs));
  printf("%lu %lu %lu\n", (unsigned long)GPIO_GET_CHIPINFO_IOCTL, (unsigned long)GPIO_V2_GET_LINE_IOCTL,
         (unsigned long)GPIO_V2_LINE_SET_VALUES_IOCTL);
  printf("%d %d %d %d %d %d %d %d %d %d\n", GPIO_V2_LINE_FLAG_INPUT, GPIO_V2_LINE_FLAG_OUTPUT, GPIO_V2_LINE_FLAG_EDGE_RISING,
         GPIO_V2_LINE_FLAG_EDGE_FALLING, GPIO_V2_LINE_FLAG_BIAS_PULL_UP, GPIO_V2_LINE_ATTR_ID_FLAGS,
         GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES, GPIO_V2_LINE_ATTR_ID_DEBOUNCE,
         GPIO_V2_LINE_EVENT_RISING_EDGE, GPIO_V2_LINE_EVENT_FALLING_EDGE);
  return 0;
}
"""


@pytest.mark.skipif(not (shutil.which("cc") and os.path.exists("/usr/include/linux/gpio.h")),
                    reason="needs a C compiler and kernel headers")
def test_layout_matches_kernel_header(tmp_path):
    src, exe = tmp_path / "c.c", tmp_path / "c"
    src.write_text(C_SRC)
    subprocess.run(["cc", "-o", str(exe), str(src)], check=True)
    lines = [list(map(int, l.split())) for l in subprocess.run([str(exe)], capture_output=True, text=True,
                                                               check=True).stdout.splitlines()]
    sizes, offsets, ioctls, consts = lines
    S = ctypes.sizeof
    assert sizes == [S(g.gpiochip_info), S(g.gpio_v2_line_attribute), S(g.gpio_v2_line_config_attribute),
                     S(g.gpio_v2_line_config), S(g.gpio_v2_line_request), S(g.gpio_v2_line_values), g.EVENT_SIZE]
    R = g.gpio_v2_line_request
    assert offsets == [R.consumer.offset, R.config.offset, R.num_lines.offset, R.fd.offset,
                       g.gpio_v2_line_config.attrs.offset]
    assert ioctls == [g.GPIO_GET_CHIPINFO_IOCTL, g.GPIO_V2_GET_LINE_IOCTL, g.GPIO_V2_LINE_SET_VALUES_IOCTL]
    assert consts == [g.FLAG_INPUT, g.FLAG_OUTPUT, g.FLAG_EDGE_RISING, g.FLAG_EDGE_FALLING, g.FLAG_BIAS_PULL_UP,
                      g.ATTR_ID_FLAGS, g.ATTR_ID_OUTPUT_VALUES, g.ATTR_ID_DEBOUNCE,
                      g.EVENT_RISING_EDGE, g.EVENT_FALLING_EDGE]


def test_debounce_lands_in_the_union():
    a = g.gpio_v2_line_attribute()
    a.id = g.ATTR_ID_DEBOUNCE
    a.debounce_period_us = 30000
    raw = bytes(a)
    assert struct.unpack_from("=II", raw, 0) == (3, 0)
    assert struct.unpack_from("=I", raw, 8) == (30000,)


def test_event_loop_parses_kernel_events():
    """Feed the reader packed gpio_v2_line_event records through a pipe."""
    chip = object.__new__(g.Chip)
    chip._stop_r, chip._stop_w = os.pipe()
    r, w = os.pipe()
    got = []
    t = threading.Thread(target=chip._event_loop, args=(r, lambda pin, pressed: got.append((pin, pressed))))
    t.start()
    ev = lambda kind, off: struct.pack(g.EVENT_FORMAT, 123, kind, off, 1, 1)
    os.write(w, ev(g.EVENT_FALLING_EDGE, 17) + ev(g.EVENT_RISING_EDGE, 17))   # two in one read
    os.write(w, ev(g.EVENT_FALLING_EDGE, 27))
    deadline = time.time() + 2
    while len(got) < 3 and time.time() < deadline:
        time.sleep(0.01)
    os.write(chip._stop_w, b"x")
    t.join(2)
    assert not t.is_alive()
    assert got == [(17, True), (17, False), (27, True)]


def test_open_chip_mock_and_missing():
    assert isinstance(g.open_chip("mock"), g.MockChip)
    with pytest.raises(g.GpioError, match="cannot open"):
        g.open_chip("/nonexistent/gpiochip9")
