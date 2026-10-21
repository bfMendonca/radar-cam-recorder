"""Push-button gestures: a pure classifier plus a Linux evdev reader thread."""

from __future__ import annotations

import contextlib
import logging
import os
import select
import struct
import threading
from enum import Enum
from pathlib import Path
from typing import Callable

from .clocks import Clock
from .config import ControlConfig

log = logging.getLogger(__name__)

_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_EV_KEY = 1
_MAX_WAIT_S = 1.0


class Gesture(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"
    LONG_ARMED = "long_armed"


_IDLE = "idle"
_DOWN = "down"  # first press held, below the long threshold
_LONG = "long"  # long gesture already emitted (or press ended the sequence); wait for release
_WAIT = "wait"  # short press released; waiting for a second press
_LOCK = "lock"  # after DOUBLE (or an ignored hold): ignore until quiet


class GestureClassifier:
    def __init__(self, cfg: ControlConfig) -> None:
        self._double_ns = int(cfg.double_press_s * 1e9)
        self._max_ns = int(cfg.max_press_s * 1e9)
        self._long_ns = int(cfg.long_press_s * 1e9)
        self._debounce_ns = int(cfg.debounce_s * 1e9)
        self._state = _IDLE
        self._pressed = False
        self._last_ns: int | None = None  # last accepted transition
        self._down_ns = 0
        self._deadline: int | None = None  # deadline for _WAIT / _LOCK (button up)

    def feed(self, pressed: bool, t_ns: int) -> list[Gesture]:
        out = self.poll(t_ns)
        if pressed == self._pressed:
            return out
        if self._last_ns is not None and t_ns - self._last_ns < self._debounce_ns:
            return out
        self._last_ns = t_ns
        self._pressed = pressed
        if pressed:
            if self._state == _IDLE:
                self._state = _DOWN
                self._down_ns = t_ns
            elif self._state == _WAIT:
                self._state = _LOCK
                self._deadline = None
                out.append(Gesture.DOUBLE)
            else:  # _LOCK
                self._deadline = None
        else:
            if self._state == _DOWN:
                if t_ns - self._down_ns >= self._max_ns:
                    self._state = _IDLE
                else:
                    self._state = _WAIT
                    self._deadline = t_ns + self._double_ns
            elif self._state == _LONG:
                self._state = _IDLE
            elif self._state == _LOCK:
                self._deadline = t_ns + self._double_ns
        return out

    def poll(self, t_ns: int) -> list[Gesture]:
        out: list[Gesture] = []
        if self._state == _DOWN and t_ns - self._down_ns >= self._long_ns:
            self._state = _LONG
            out.append(Gesture.LONG_ARMED)
        elif self._state == _WAIT and self._deadline is not None and t_ns >= self._deadline:
            self._state = _IDLE
            self._deadline = None
            out.append(Gesture.SINGLE)
        elif self._state == _LOCK and self._deadline is not None and t_ns >= self._deadline:
            self._state = _IDLE
            self._deadline = None
        return out

    def next_deadline_ns(self) -> int | None:
        if self._state == _DOWN:
            return self._down_ns + self._long_ns
        if self._state in (_WAIT, _LOCK):
            return self._deadline
        return None


def find_button(name: str, sys_input: Path = Path("/sys/class/input")) -> Path | None:
    """Return /dev/input/eventN for the input device whose name matches."""
    try:
        entries = sorted(sys_input.glob("event*"))
    except OSError:
        return None
    for entry in entries:
        try:
            dev_name = (entry / "device" / "name").read_text().strip()
        except OSError:
            continue
        if dev_name == name:
            return Path("/dev/input") / entry.name
    return None


class ButtonReader:
    def __init__(
        self,
        path: Path,
        classifier: GestureClassifier,
        on_gesture: Callable[[Gesture], None],
        clock: Clock,
        fd: int | None = None,
    ) -> None:
        self._path = path
        self._classifier = classifier
        self._on_gesture = on_gesture
        self._clock = clock
        self._fd = fd  # pre-opened device (tests); otherwise opened from path
        self._wake_r, self._wake_w = os.pipe()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="button-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"x")
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if not self._thread.is_alive():
            for f in (self._wake_r, self._wake_w):
                with contextlib.suppress(OSError):
                    os.close(f)
            self._wake_r = self._wake_w = -1

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _emit(self, gestures: list[Gesture]) -> None:
        for g in gestures:
            try:
                self._on_gesture(g)
            except Exception:
                log.exception("button gesture handler failed")

    def _run(self) -> None:
        fd = self._fd
        try:
            if fd is None:
                fd = os.open(str(self._path), os.O_RDONLY | os.O_NONBLOCK)
            else:
                os.set_blocking(fd, False)
            self._loop(fd)
        except OSError as exc:
            log.error("button device %s unavailable: %s", self._path, exc)
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _loop(self, fd: int) -> None:
        buf = b""
        while not self._stop.is_set():
            deadline = self._classifier.next_deadline_ns()
            timeout = _MAX_WAIT_S
            if deadline is not None:
                timeout = min(_MAX_WAIT_S, max(0.0, (deadline - self._clock()) / 1e9))
            ready, _, _ = select.select([fd, self._wake_r], [], [], timeout)
            if self._stop.is_set():
                return
            if fd in ready:
                try:
                    chunk = os.read(fd, _EVENT_SIZE * 16)
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    log.error("button device %s closed", self._path)
                    return
                if chunk:
                    buf += chunk
                    while len(buf) >= _EVENT_SIZE:
                        _, _, etype, _code, value = struct.unpack(_EVENT_FMT, buf[:_EVENT_SIZE])
                        buf = buf[_EVENT_SIZE:]
                        if etype == _EV_KEY and value in (0, 1):
                            self._emit(self._classifier.feed(value == 1, self._clock()))
            self._emit(self._classifier.poll(self._clock()))
