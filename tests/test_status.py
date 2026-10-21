from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from radar_cam_recorder.status import State, StatusBus, StatusEvent, StatusFileSink


class Collect:
    def __init__(self) -> None:
        self.events: list[StatusEvent] = []

    def handle(self, event: StatusEvent) -> None:
        self.events.append(event)


class Slow:
    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def handle(self, event: StatusEvent) -> None:
        self.release.wait(1.0)


class Boom:
    def handle(self, event: StatusEvent) -> None:
        raise RuntimeError("boom")


def make_bus(queue_size: int = 256) -> StatusBus:
    ticks = iter(range(1, 10_000))
    return StatusBus(lambda: next(ticks), queue_size=queue_size)


def test_initial_state_is_starting() -> None:
    bus = make_bus()
    assert bus.current.state is State.STARTING
    assert bus.current.reason == "starting"
    bus.close()


def test_set_state_publishes_only_on_change() -> None:
    bus = make_bus()
    sink = Collect()
    bus.add_sink(sink)
    assert bus.set_state(State.RECORDING, "ok") is True
    assert bus.set_state(State.RECORDING, "ok") is False
    assert bus.set_state(State.RECORDING, "other") is True
    bus.close()
    assert [e.reason for e in sink.events] == ["ok", "other"]


def test_events_delivered_in_order_after_close() -> None:
    bus = make_bus()
    sink = Collect()
    bus.add_sink(sink)
    bus.set_state(State.WAITING_FOR_DEVICES, "a")
    bus.incident("gap", n=1)
    bus.set_state(State.RECORDING, "b")
    bus.close()
    bus.close()
    assert [(e.kind, e.reason) for e in sink.events] == [
        ("state", "a"),
        ("incident", "gap"),
        ("state", "b"),
    ]
    assert sink.events[1].state is State.WAITING_FOR_DEVICES
    assert sink.events[1].detail == {"n": 1}
    assert [e.boottime_ns for e in sink.events] == sorted(e.boottime_ns for e in sink.events)


def test_slow_sink_does_not_block_producers() -> None:
    release = threading.Event()
    bus = make_bus()
    bus.add_sink(Slow(release))
    t0 = time.monotonic()
    for i in range(10):
        bus.set_state(State.DEGRADED, f"r{i}")
    assert time.monotonic() - t0 < 0.1
    release.set()
    bus.close()


def test_failing_sink_does_not_stop_others() -> None:
    bus = make_bus()
    sink = Collect()
    bus.add_sink(Boom())
    bus.add_sink(sink)
    bus.set_state(State.RECORDING, "x")
    bus.close()
    assert len(sink.events) == 1


def test_full_queue_counts_drops() -> None:
    release = threading.Event()
    bus = make_bus(queue_size=2)
    bus.add_sink(Slow(release))
    for i in range(10):
        bus.set_state(State.DEGRADED, f"r{i}")
    assert bus.dropped > 0
    release.set()
    bus.close()


def test_as_dict() -> None:
    ev = StatusEvent("state", State.RECORDING, "ok", 5, {"a": 1})
    assert ev.as_dict() == {
        "kind": "state",
        "state": "recording",
        "reason": "ok",
        "boottime_ns": 5,
        "detail": {"a": 1},
    }


def test_status_file_sink_writes_json(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "status.json"
    StatusFileSink(path).handle(StatusEvent("state", State.RECORDING, "ok", 7, {"x": 2}))
    data = json.loads(path.read_text())
    assert data["state"] == "recording"
    assert data["reason"] == "ok"
    assert data["boottime_ns"] == 7
    assert data["detail"] == {"x": 2}
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]


def test_status_file_sink_ignores_unwritable_dir(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    StatusFileSink(blocker / "sub" / "status.json").handle(
        StatusEvent("state", State.RECORDING, "ok", 7, {})
    )
