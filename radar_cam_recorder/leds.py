"""Status LEDs: state to pattern mapping, sysfs LED-class output, and the status sink."""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from radar_cam_recorder.clocks import Clock
from radar_cam_recorder.status import State, StatusEvent

log = logging.getLogger(__name__)


class Pattern(Enum):
    OFF = "off"
    ON = "on"
    FAST_BLINK = "fast_blink"
    SLOW_BLINK = "slow_blink"
    SLOW_BLINK_INVERTED = "slow_blink_inverted"
    BLINK_1 = "blink_1"
    BLINK_2 = "blink_2"
    BLINK_1_2 = "blink_1_2"
    PULSE = "pulse"


# Kernel "pattern" trigger strings: "<brightness> <ms> ..." (cycle restarts on every write).
PATTERN_STRINGS = {
    Pattern.FAST_BLINK: "1 100 0 100",  # 5 Hz
    Pattern.SLOW_BLINK: "1 500 0 500",  # 1 Hz
    Pattern.SLOW_BLINK_INVERTED: "0 500 1 500",
    Pattern.BLINK_1: "1 200 0 1300",
    Pattern.BLINK_2: "1 200 0 200 1 200 0 1200",
    Pattern.BLINK_1_2: "1 200 0 700 1 200 0 200 1 200 0 1200",
    # Starts with 0: a finite pattern ends at the level of its first element (dark).
    Pattern.PULSE: "0 200 1 150 0 1650",  # 2.0 s cycle
}
PULSE_REPEAT = 2  # 4 s in total, then dark on its own
PULSE_CYCLE_NS = 2_000_000_000
FLASH_SECONDS = 0.2
RETRY_SECONDS = 1.0
# logind powers off after a 5 s hold; if the process is still alive this long after the red
# "long_armed" blink started, the poweroff did not happen: drop the overlay.
LONG_ARMED_TIMEOUT_SECONDS = 10.0


def patterns_for(event: StatusEvent) -> tuple[Pattern, Pattern]:
    """(green, red) for a state event."""
    s = event.state
    if s is State.STARTING:
        return Pattern.FAST_BLINK, Pattern.OFF
    if s is State.IDLE:
        return Pattern.ON, Pattern.SLOW_BLINK if event.detail.get("problem") else Pattern.OFF
    if s is State.RECORDING:
        return Pattern.PULSE, Pattern.OFF
    if s is State.DEGRADED:
        return (
            Pattern.PULSE,
            Pattern.BLINK_2 if event.reason == "radar missing" else Pattern.BLINK_1,
        )
    if s is State.WAITING_FOR_DEVICES:
        return Pattern.OFF, Pattern.BLINK_1_2
    if s in (State.DISK_FULL, State.STORAGE_MISSING):
        return Pattern.SLOW_BLINK_INVERTED, Pattern.SLOW_BLINK
    if s is State.ERROR:
        return Pattern.OFF, Pattern.ON
    if s in (State.STOPPING, State.STOPPED):
        # STOPPED blinks too: at poweroff it lasts until the kernel halts (the script turns
        # the LEDs off after a plain service stop).
        return Pattern.FAST_BLINK, Pattern.FAST_BLINK
    return Pattern.OFF, Pattern.OFF


class LedOutput(Protocol):
    def show(self, pattern: Pattern) -> None: ...

    def pulse(self) -> None:
        """Arm one finite PULSE cycle."""
        ...


class FakeLed:
    def __init__(self) -> None:
        self.shown: list[Pattern] = []
        self.pulses = 0

    def show(self, pattern: Pattern) -> None:
        self.shown.append(pattern)

    def pulse(self) -> None:
        self.pulses += 1


class SysfsLed:
    """One LED-class device; never raises. Writes only on pattern change (writes restart cycles)."""

    def __init__(
        self,
        name: str,
        root: Path = Path("/sys/class/leds"),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._dir = root / name
        self._sleep = sleep
        self._monotonic = monotonic
        self._switched_at = 0.0
        self._lock = threading.Lock()
        self._last: Pattern | None = None
        self._trigger: str | None = None
        self._warned = False

    def show(self, pattern: Pattern) -> None:
        with self._lock:
            if pattern is self._last:
                return
            try:
                if pattern is Pattern.ON:
                    self._steady("1")
                elif pattern in (Pattern.OFF, Pattern.PULSE):
                    self._steady("0")
                else:
                    self._run_pattern(PATTERN_STRINGS[pattern], "-1")
                self._last = pattern
            except OSError as e:
                self._failed(e)

    def pulse(self) -> None:
        with self._lock:
            try:
                self._run_pattern(PATTERN_STRINGS[Pattern.PULSE], str(PULSE_REPEAT))
                self._last = None  # sysfs now holds a pulse, not the shown state
            except OSError as e:
                self._failed(e)

    def _failed(self, e: OSError) -> None:
        # The device may now be half-switched (e.g. trigger changed, brightness not): forget the
        # last pattern so the next show() writes again instead of being skipped.
        self._last = None
        if not self._warned:
            self._warned = True
            log.warning("LED %s: %s", self._name, e)
        else:
            log.debug("LED %s: %s", self._name, e)

    def _set_trigger(self, value: str) -> None:
        if self._trigger != value:
            self._write("trigger", value)
            self._trigger = value
            self._switched_at = self._monotonic()

    def _steady(self, level: str) -> None:
        self._set_trigger("none")
        self._write("brightness", level)

    def _run_pattern(self, pattern: str, repeat: str) -> None:
        self._set_trigger("pattern")
        # repeat first so a finite pattern never runs forever, even briefly
        self._write_retry("repeat", repeat)
        self._write_retry("pattern", pattern)

    def _write(self, fname: str, value: str) -> None:
        (self._dir / fname).write_text(value)

    def _write_retry(self, fname: str, value: str) -> None:
        # udev makes pattern/repeat group-writable asynchronously after the trigger switch.
        # Only within RETRY_SECONDS of this LED's own trigger switch; later denials fail at once.
        deadline = self._switched_at + RETRY_SECONDS
        while True:
            try:
                self._write(fname, value)
                return
            except PermissionError:
                if self._monotonic() >= deadline:
                    raise
                self._sleep(0.02)


class LedController:
    """Thread-safe: state patterns, green progress pulses and button-feedback overlays."""

    def __init__(
        self,
        green: LedOutput,
        red: LedOutput,
        clock: Clock,
        flash_delay: float = FLASH_SECONDS,
        long_armed_timeout: float = LONG_ARMED_TIMEOUT_SECONDS,
    ) -> None:
        self._leds = {"green": green, "red": red}
        self._clock = clock
        self._flash_delay = flash_delay
        self._long_armed_timeout = long_armed_timeout
        self._lock = threading.Lock()
        self._state = {"green": Pattern.OFF, "red": Pattern.OFF}
        self._overlay: dict[str, Pattern] = {}
        self._last_pulse_ns: int | None = None
        self._timer: threading.Timer | None = None
        self._sticky = False

    def show_state(self, event: StatusEvent) -> None:
        green, red = patterns_for(event)
        with self._lock:
            self._state = {"green": green, "red": red}
            self._last_pulse_ns = None
            self._apply()

    def progress(self) -> None:
        with self._lock:
            if self._state["green"] is not Pattern.PULSE or "green" in self._overlay:
                return
            now = self._clock()
            last = self._last_pulse_ns
            if last is not None and now - last < PULSE_CYCLE_NS:
                return
            self._last_pulse_ns = now
            self._leds["green"].pulse()

    def flash(self, kind: str) -> None:
        with self._lock:
            if self._sticky:
                # long_armed lasts until poweroff (or its timeout); green follows the state.
                return
            hold = self._flash_delay
            if kind == "accepted":
                self._overlay = {"green": Pattern.ON, "red": Pattern.ON}
            elif kind == "rejected":
                self._overlay = {"red": Pattern.BLINK_2}
                hold = 3 * self._flash_delay  # on, off, on: two blinks
            elif kind == "long_armed":
                self._overlay = {"red": Pattern.FAST_BLINK}
                self._sticky = True
            else:
                log.warning("unknown LED flash %r", kind)
                return
            self._apply()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._sticky:
                timer = threading.Timer(self._long_armed_timeout, self._disarm)
            else:
                timer = threading.Timer(hold, self._restore)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _disarm(self) -> None:
        with self._lock:
            if not self._sticky:
                return
            self._sticky = False
            log.warning(
                "long press: still running %.0f s after arming, poweroff did not happen",
                self._long_armed_timeout,
            )
            self._clear_overlay()

    def _restore(self) -> None:
        with self._lock:
            if not self._sticky:
                self._clear_overlay()

    def _clear_overlay(self) -> None:
        self._overlay = {}
        self._timer = None
        self._last_pulse_ns = None
        self._apply()

    def _apply(self) -> None:
        for name, led in self._leds.items():
            led.show(self._overlay.get(name, self._state[name]))


class LedSink:
    def __init__(self, controller: LedController) -> None:
        self._controller = controller

    def handle(self, event: StatusEvent) -> None:
        if event.kind == "state":
            self._controller.show_state(event)
