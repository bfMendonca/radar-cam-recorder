from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from radar_cam_recorder.leds import (
    PATTERN_STRINGS,
    FakeLed,
    LedController,
    LedSink,
    Pattern,
    SysfsLed,
    patterns_for,
)
from radar_cam_recorder.status import State, StatusEvent

P = Pattern


def event(state: State, reason: str = "", **detail: Any) -> StatusEvent:
    return StatusEvent("state", state, reason, 0, detail)


@pytest.mark.parametrize(
    "state,reason,detail,expected",
    [
        (State.STARTING, "starting", {}, (P.FAST_BLINK, P.OFF)),
        (State.IDLE, "idle", {}, (P.ON, P.OFF)),
        (State.IDLE, "idle", {"problem": "x"}, (P.ON, P.SLOW_BLINK)),
        (State.RECORDING, "r", {}, (P.PULSE, P.OFF)),
        (State.DEGRADED, "camera missing", {}, (P.PULSE, P.BLINK_1)),
        (State.DEGRADED, "radar missing", {}, (P.PULSE, P.BLINK_2)),
        (State.WAITING_FOR_DEVICES, "w", {}, (P.OFF, P.BLINK_1_2)),
        (State.DISK_FULL, "d", {}, (P.SLOW_BLINK_INVERTED, P.SLOW_BLINK)),
        (State.STORAGE_MISSING, "s", {}, (P.SLOW_BLINK_INVERTED, P.SLOW_BLINK)),
        (State.ERROR, "e", {}, (P.OFF, P.ON)),
        (State.STOPPING, "s", {}, (P.FAST_BLINK, P.FAST_BLINK)),
        (State.STOPPED, "s", {}, (P.FAST_BLINK, P.FAST_BLINK)),
    ],
)
def test_patterns_for(
    state: State, reason: str, detail: dict[str, Any], expected: tuple[Pattern, Pattern]
) -> None:
    assert patterns_for(event(state, reason, **detail)) == expected


class Manual:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def make(delay: float = 0.0) -> tuple[LedController, FakeLed, FakeLed, Manual]:
    green, red, clock = FakeLed(), FakeLed(), Manual()
    return LedController(green, red, clock, flash_delay=delay), green, red, clock


def test_pulse_only_while_recording() -> None:
    ctl, green, _, _ = make()
    ctl.show_state(event(State.IDLE))
    ctl.progress()
    assert green.pulses == 0
    ctl.show_state(event(State.RECORDING))
    ctl.progress()
    assert green.pulses == 1


def test_pulse_at_most_once_per_cycle() -> None:
    ctl, green, _, clock = make()
    ctl.show_state(event(State.RECORDING))
    for t in (0, 500_000_000, 1_000_000_000):
        clock.now = t
        ctl.progress()
    assert green.pulses == 1
    clock.now = 2_500_000_000
    ctl.progress()
    assert green.pulses == 2


def wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> bool:
    end = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        end.wait(0.005)
    return pred()


def test_flash_restores_state() -> None:
    ctl, green, red, _ = make(delay=0.01)
    ctl.show_state(event(State.IDLE))
    ctl.flash("accepted")
    assert P.ON in green.shown
    assert wait_until(lambda: red.shown[-1] == P.OFF and green.shown[-1] == P.ON)
    assert P.ON in red.shown  # the overlay was displayed
    ctl.flash("rejected")
    assert P.BLINK_2 in red.shown
    assert wait_until(lambda: red.shown[-1] == P.OFF)


def test_long_armed_stays() -> None:
    ctl, _, red, _ = make(delay=0.01)
    ctl.show_state(event(State.IDLE))
    ctl.flash("long_armed")
    threading.Event().wait(0.1)
    assert red.shown[-1] == P.FAST_BLINK


def led(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir()
    for f in ("trigger", "brightness", "pattern", "repeat"):
        (d / f).write_text("")
    return d


def test_sysfs_writes(tmp_path: Path) -> None:
    d = led(tmp_path, "rcr-red")
    out = SysfsLed("rcr-red", tmp_path)
    out.show(P.ON)
    assert (d / "trigger").read_text() == "none"
    assert (d / "brightness").read_text() == "1"
    out.show(P.SLOW_BLINK)
    assert (d / "trigger").read_text() == "pattern"
    assert (d / "pattern").read_text() == PATTERN_STRINGS[P.SLOW_BLINK]
    assert (d / "repeat").read_text() == "-1"
    (d / "pattern").write_text("sentinel")
    out.show(P.SLOW_BLINK)  # identical: skipped, no cycle restart
    assert (d / "pattern").read_text() == "sentinel"


def test_sysfs_pulse(tmp_path: Path) -> None:
    d = led(tmp_path, "rcr-green")
    out = SysfsLed("rcr-green", tmp_path)
    out.show(P.PULSE)
    assert (d / "trigger").read_text() == "none"
    assert (d / "brightness").read_text() == "0"
    out.pulse()
    assert (d / "trigger").read_text() == "pattern"
    assert (d / "repeat").read_text() == "2"
    assert (d / "pattern").read_text().startswith("0 ")


def test_pattern_strings_sane() -> None:
    for p, s in PATTERN_STRINGS.items():
        nums = s.split()
        assert len(nums) >= 4 and len(nums) % 2 == 0 and nums[0] in ("0", "1"), p
    assert PATTERN_STRINGS[P.PULSE].startswith("0 ")


def test_sysfs_retries_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = led(tmp_path, "rcr-red")
    out = SysfsLed("rcr-red", tmp_path, sleep=lambda _s: None)
    real = Path.write_text
    fails = {"n": 2}

    def flaky(self: Path, data: str, *a: Any, **k: Any) -> int:
        if self.name == "pattern" and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(13, "denied")
        return real(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", flaky)
    out.show(P.FAST_BLINK)
    assert (d / "pattern").read_text() == PATTERN_STRINGS[P.FAST_BLINK]


def test_sysfs_missing_or_denied(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    out = SysfsLed("rcr-red", tmp_path, sleep=lambda _s: None)
    out.show(P.ON)
    out.show(P.OFF)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    d = led(tmp_path, "rcr-green")
    g = SysfsLed("rcr-green", tmp_path, sleep=lambda _s: None)
    (d / "trigger").chmod(0)
    g.show(P.ON)
    g.pulse()
    (d / "trigger").chmod(0o644)
    assert os.access(d / "trigger", os.R_OK)


def test_led_sink() -> None:
    ctl, green, red, _ = make()
    LedSink(ctl).handle(event(State.ERROR, "e"))
    assert (green.shown[-1], red.shown[-1]) == (P.OFF, P.ON)


def test_flash_ignored_after_long_armed() -> None:
    ctl, green, red, _ = make(delay=0.01)
    ctl.show_state(event(State.RECORDING))
    ctl.flash("long_armed")
    ctl.flash("accepted")
    ctl.flash("rejected")
    threading.Event().wait(0.1)
    assert red.shown[-1] == P.FAST_BLINK
    assert P.ON not in red.shown
    assert P.ON not in green.shown
    ctl.show_state(event(State.IDLE))
    assert (green.shown[-1], red.shown[-1]) == (P.ON, P.FAST_BLINK)


def test_sysfs_denied_retries_only_after_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    led(tmp_path, "rcr-red")
    now = {"t": 100.0}
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now["t"] += s

    out = SysfsLed("rcr-red", tmp_path, sleep=sleep, monotonic=lambda: now["t"])
    real = Path.write_text

    def denied(self: Path, data: str, *a: Any, **k: Any) -> int:
        if self.name in ("pattern", "repeat"):
            raise PermissionError(13, "denied")
        return real(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", denied)
    out.show(P.FAST_BLINK)
    first = len(sleeps)
    assert 0 < first <= 60  # bounded, about 1 s of polling
    out.pulse()
    out.show(P.SLOW_BLINK)
    assert len(sleeps) == first  # later calls fail immediately
