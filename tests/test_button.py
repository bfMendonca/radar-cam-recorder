from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from typing import Callable

from radar_cam_recorder.button import ButtonReader, Gesture, GestureClassifier, find_button
from radar_cam_recorder.config import ControlConfig

ms = 1_000_000
EV_SYN, EV_KEY = 0, 1
KEY_POWER = 116


def make() -> GestureClassifier:
    return GestureClassifier(ControlConfig("idle", "button@3", 0.4, 1.0, 5.0, 0.03))


def wait_until(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.002)


def test_single() -> None:
    c = make()
    assert c.feed(True, 0) == [] and c.feed(False, 100 * ms) == []
    assert c.poll(499 * ms) == [] and c.poll(500 * ms) == [Gesture.SINGLE]


def test_double_emitted_on_second_down() -> None:
    c = make()
    c.feed(True, 0)
    c.feed(False, 100 * ms)
    assert c.feed(True, 300 * ms) == [Gesture.DOUBLE]
    c.feed(False, 400 * ms)
    assert c.poll(2000 * ms) == []


def test_third_press_ignored() -> None:
    c = make()
    for t in (0, 100, 200, 300):
        c.feed(t % 200 == 0, t * ms)
    assert c.feed(True, 400 * ms) == [] and c.poll(3000 * ms) == []


def test_bounce_is_not_double() -> None:
    c = make()
    c.feed(True, 0)
    c.feed(False, 5 * ms)
    c.feed(True, 10 * ms)
    c.feed(False, 120 * ms)
    assert c.poll(600 * ms) == [Gesture.SINGLE]


def test_medium_hold_ignored() -> None:
    c = make()
    c.feed(True, 0)
    c.feed(False, 2000 * ms)
    assert c.poll(5000 * ms) == []


def test_long_armed_once() -> None:
    c = make()
    c.feed(True, 0)
    assert c.poll(4999 * ms) == [] and c.poll(5000 * ms) == [Gesture.LONG_ARMED]
    assert c.poll(6000 * ms) == [] and c.feed(False, 7000 * ms) == []


def test_next_deadline() -> None:
    c = make()
    assert c.next_deadline_ns() is None
    c.feed(True, 0)
    assert c.next_deadline_ns() == 5000 * ms
    c.feed(False, 100 * ms)
    assert c.next_deadline_ns() == 500 * ms


def test_late_second_press_yields_single_then_new_sequence() -> None:
    c = make()
    c.feed(True, 0)
    c.feed(False, 100 * ms)
    assert c.feed(True, 700 * ms) == [Gesture.SINGLE]
    c.feed(False, 800 * ms)
    assert c.poll(1200 * ms) == [Gesture.SINGLE]


def test_release_inside_debounce_is_not_lost() -> None:
    c = make()
    c.feed(True, 0)
    c.feed(False, 20 * ms)
    assert c.next_deadline_ns() == 50 * ms
    assert c.poll(6000 * ms) == [Gesture.SINGLE]
    assert c.poll(12000 * ms) == []


def ev(etype: int, code: int, value: int) -> bytes:
    return struct.pack("llHHi", 0, 0, etype, code, value)


def test_reader_parses_events() -> None:
    r, w = os.pipe()
    now = [0]
    got: list[Gesture] = []
    clf = make()
    reader = ButtonReader(Path("unused"), clf, got.append, lambda: now[0], fd=r)
    reader.start()
    try:
        os.write(w, ev(EV_KEY, KEY_POWER, 1) + ev(EV_SYN, 0, 0) + ev(EV_KEY, KEY_POWER, 2))
        wait_until(lambda: clf.next_deadline_ns() == 5000 * ms)
        now[0] = 100 * ms
        os.write(w, ev(EV_KEY, KEY_POWER, 0) + ev(EV_SYN, 0, 0))
        wait_until(lambda: clf.next_deadline_ns() == 500 * ms)
        now[0] = 600 * ms
        os.write(w, ev(EV_SYN, 0, 0))  # wake the select; poll sees the clock
        wait_until(lambda: got == [Gesture.SINGLE])
    finally:
        reader.stop()
        os.close(w)
    assert not reader.is_alive()


def test_reader_handles_split_event_and_stops_promptly() -> None:
    r, w = os.pipe()
    now = [0]
    got: list[Gesture] = []
    clf = make()
    reader = ButtonReader(Path("unused"), clf, got.append, lambda: now[0], fd=r)
    reader.start()
    data = ev(EV_KEY, KEY_POWER, 1)
    os.write(w, data[:5])
    time.sleep(0.02)
    os.write(w, data[5:])
    wait_until(lambda: clf.next_deadline_ns() == 5000 * ms)
    now[0] = 5000 * ms
    os.write(w, ev(EV_SYN, 0, 0))
    wait_until(lambda: got == [Gesture.LONG_ARMED])
    t0 = time.monotonic()
    reader.stop()
    assert time.monotonic() - t0 < 1.0
    os.close(w)


def test_reader_exits_when_device_gone() -> None:
    r, w = os.pipe()
    reader = ButtonReader(Path("unused"), make(), lambda g: None, lambda: 0, fd=r)
    reader.start()
    os.close(w)
    wait_until(lambda: not reader.is_alive())
    reader.stop()


def test_find_button(tmp_path: Path) -> None:
    (tmp_path / "event3" / "device").mkdir(parents=True)
    (tmp_path / "event3" / "device" / "name").write_text("rcr-button\n")
    (tmp_path / "event4" / "device").mkdir(parents=True)
    (tmp_path / "event4" / "device" / "name").write_text("other\n")
    (tmp_path / "mouse0").mkdir()
    assert find_button("rcr-button", tmp_path) == Path("/dev/input/event3")
    assert find_button("nope", tmp_path) is None
