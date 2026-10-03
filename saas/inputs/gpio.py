"""Physical buttons. Wiring: GPIO pin -> button -> GND (the kernel turns the
internal pull-up on, no resistors needed). Debouncing is done by the kernel.

Per button, one of:
  * plain:   event on press
  * hold:    short press -> event (on release), long press -> hold event
  * repeat:  event on press, then repeated every hold_time while held (volume)
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Set, Tuple

from ..core.events import Bus, Event, parse_event
from .base import Input

log = logging.getLogger(__name__)


class GpioInput(Input):
    def __init__(self, bus: Bus, chip: Any, buttons: Dict[str, Dict[str, Any]], bounce_ms: int = 30) -> None:
        super().__init__(bus)
        self._buttons: Dict[int, Tuple[Event, Dict[str, Any]]] = {
            spec["pin"]: (parse_event(name), spec) for name, spec in buttons.items()
        }
        self._down: Set[int] = set()
        self._held: Set[int] = set()
        self._gen: Dict[int, int] = {}
        self._lock = threading.Lock()
        chip.watch_inputs(sorted(self._buttons), bounce_ms, self._edge)
        for pin, (event, spec) in sorted(self._buttons.items()):
            extra = f" (hold: {spec['hold']})" if spec["hold"] else " (repeat)" if spec["repeat"] else ""
            log.info("button %s on GPIO%d%s", event.value, pin, extra)

    def _edge(self, pin: int, pressed: bool) -> None:
        if pin not in self._buttons:
            return
        event, spec = self._buttons[pin]
        with self._lock:
            self._gen[pin] = self._gen.get(pin, 0) + 1  # invalidates pending timers
            if pressed:
                if pin in self._down:
                    return
                self._down.add(pin)
                self._held.discard(pin)
                if spec["repeat"]:
                    self.bus.emit(event)
                    self._arm(pin, spec["hold_time"])
                elif spec["hold"]:
                    self._arm(pin, spec["hold_time"])
                else:
                    self.bus.emit(event)
            else:
                if pin not in self._down:
                    return
                self._down.discard(pin)
                if spec["hold"] and pin not in self._held:
                    self.bus.emit(event)

    def _arm(self, pin: int, delay: float) -> None:
        t = threading.Timer(delay, self._timer_fired, args=(pin, self._gen[pin]))
        t.daemon = True
        t.start()

    def _timer_fired(self, pin: int, gen: int) -> None:
        event, spec = self._buttons[pin]
        with self._lock:
            if self._gen.get(pin) != gen or pin not in self._down:
                return
            if spec["repeat"]:
                self.bus.emit(event)
                self._arm(pin, spec["hold_time"])
            else:
                self._held.add(pin)
                self.bus.emit(parse_event(spec["hold"]))

    def close(self) -> None:
        with self._lock:
            for pin in self._gen:
                self._gen[pin] += 1
            self._down.clear()
