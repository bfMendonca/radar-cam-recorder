from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import ld2451
import pytest

from radar_cam_recorder.camera.base import CameraRun, CameraSource
from radar_cam_recorder.camera.fake import FakeCamera
from radar_cam_recorder.camera.metadata import parse_metadata
from radar_cam_recorder.camera.rpicam import RpicamCamera
from radar_cam_recorder.clocks import ClockSample
from radar_cam_recorder.config import Config, parse_config
from radar_cam_recorder.fakes import fake_radar_transport
from radar_cam_recorder.radar import RadarStream
from radar_cam_recorder.session import SegmentPaths, SessionStore
from radar_cam_recorder.status import State, StatusBus, StatusEvent
from radar_cam_recorder.storage import StorageCheck
from radar_cam_recorder.supervisor import Supervisor

FAKE_BIN = Path(__file__).parent / "fake_rpicam.py"
FRAME = bytes.fromhex(
    "F4F3F2F1 1100 03 01 8A28003C15 8A1E013C0F 765F003C0F F8F7F6F5".replace(" ", "")
)
S = 1_000_000_000


def wait_until(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


def frame_count(meta: Path) -> int:
    return len(parse_metadata(meta.read_text())) if meta.exists() else 0


def radar_data(path: Path) -> bytes:
    return b"".join(data for _, data in ld2451.read_records(path))


def make_cfg(root: Path, **camera: Any) -> Config:
    return parse_config(
        {
            "recording": {"segment_s": 1.0},
            "storage": {"recordings_dir": str(root), "check_interval_s": 0.5},
            "camera": {"binary": str(FAKE_BIN), **camera},
            "setup": {"notes": "pole at the gate", "geometry": {"height_m": 2.5}},
        }
    )


class ListSink:
    def __init__(self) -> None:
        self.events: list[StatusEvent] = []

    def handle(self, event: StatusEvent) -> None:
        self.events.append(event)


@dataclass
class Rig:
    sup: Supervisor
    now: list[int]
    transport: ld2451.LoopbackTransport
    bus: StatusBus
    sink: ListSink
    root: Path

    def session_json(self) -> dict[str, Any]:
        assert self.sup.session is not None
        return read_json(self.sup.session.path / "session.json")

    def events_text(self) -> str:
        assert self.sup.session is not None
        return (self.sup.session.path / "events.jsonl").read_text()

    def segment(self, index: int) -> SegmentPaths:
        assert self.sup.session is not None
        return self.sup.session.segment(index)


def build(
    tmp_path: Path,
    camera: Callable[[Callable[[], int]], CameraSource | None] = lambda clock: FakeCamera(clock),
    cfg: Config | None = None,
    storage_check: Callable[[Path, bool], StorageCheck] | None = None,
) -> Rig:
    now = [0]

    def clock() -> int:
        return now[0]

    cfg = cfg or make_cfg(tmp_path)
    transport = fake_radar_transport()
    bus = StatusBus(clock)
    sink = ListSink()
    bus.add_sink(sink)
    kwargs: dict[str, Any] = {}
    if storage_check is not None:
        kwargs["storage_check"] = storage_check
    sup = Supervisor(
        cfg,
        bus=bus,
        camera=camera(clock),
        radar=RadarStream(cfg.radar, lambda: transport),
        clock=clock,
        **kwargs,
    )
    return Rig(sup, now, transport, bus, sink, tmp_path)


def inject_and_wait(rig: Rig) -> None:
    reads = rig.sup._writer.reads
    rig.transport.inject(FRAME)
    wait_until(lambda: rig.sup._writer.reads > reads)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = build(tmp_path)
    yield r
    r.sup.shutdown()


def test_first_tick_opens_session_and_records(rig: Rig) -> None:
    rig.sup.tick()
    assert rig.bus.current.state is State.RECORDING
    assert rig.bus.current.reason == "recording"
    data = rig.session_json()
    assert data["state"] == "in_progress"
    assert data["devices"]["radar"]["firmware"]
    assert data["devices"]["camera"] == {"backend": "fake", "fps": 25.0}
    assert data["setup"] == {"notes": "pole at the gate", "geometry": {"height_m": 2.5}}
    assert data["ntp_synced"] in (True, False)
    assert [s["label"] for s in data["clock_samples"]] == ["start"]
    assert data["config"]["recording"]["segment_s"] == 1.0
    wait_until(lambda: frame_count(rig.segment(0).meta) >= 1)


def test_radar_flushed_periodically(rig: Rig) -> None:
    rig.sup.tick()
    radar_file = rig.segment(0).radar
    inject_and_wait(rig)
    assert not radar_data(radar_file).endswith(FRAME)  # still buffered
    rig.now[0] = int(0.6 * S)  # past check_interval_s, before segment_s
    rig.sup.tick()
    assert radar_data(radar_file).endswith(FRAME)  # after the start-up command ACKs
    assert not (rig.segment(1).radar).exists()


def test_rotation_creates_next_segment(rig: Rig) -> None:
    rig.sup.tick()
    seg0 = rig.segment(0)
    wait_until(lambda: frame_count(seg0.meta) >= 2)
    rig.now[0] = int(1.1 * S)
    rig.sup.tick()
    assert seg0.frames.exists()
    seg1 = rig.segment(1)
    wait_until(lambda: seg1.video.exists() and seg1.video.stat().st_size > 0)
    size = seg1.video.stat().st_size
    wait_until(lambda: seg1.video.stat().st_size > size)
    data = rig.session_json()
    first = data["segments"][0]
    assert first["index"] == 0
    assert first["start_boottime_ns"] == 0
    assert first["end_boottime_ns"] == int(1.1 * S)
    assert first["frames"] == len(seg0.frames.read_text().splitlines()) - 1 >= 2
    assert [s["label"] for s in data["clock_samples"]] == ["start", "segment 1"]


def test_radar_not_interrupted_by_rotation(rig: Rig) -> None:
    rig.sup.tick()
    inject_and_wait(rig)
    rig.now[0] = int(1.1 * S)
    rig.sup.tick()
    inject_and_wait(rig)
    rig.sup.shutdown()
    assert radar_data(rig.segment(0).radar).endswith(FRAME)  # after the start-up command ACKs
    assert radar_data(rig.segment(1).radar) == FRAME


class StampedCamera:
    """FakeCamera whose frames in segment N all carry SensorTimestamp (N + 1) s."""

    def __init__(self) -> None:
        self.stamp = [0]
        self._cam = FakeCamera(lambda: self.stamp[0], fps=100.0)

    def start(self, paths: SegmentPaths) -> CameraRun:
        self.stamp[0] = (paths.index + 1) * S
        return self._cam.start(paths)

    def describe(self) -> dict[str, Any]:
        return self._cam.describe()


def test_segment_gap_incident(tmp_path: Path) -> None:
    rig = build(tmp_path, camera=lambda clock: StampedCamera())
    try:
        rig.sup.tick()
        wait_until(lambda: frame_count(rig.segment(0).meta) >= 1)
        rig.now[0] = int(1.1 * S)
        rig.sup.tick()
        wait_until(lambda: frame_count(rig.segment(1).meta) >= 1)
        rig.sup.tick()
        rig.sup.tick()  # reported once only
        wait_until(lambda: any(e.reason == "segment gap" for e in rig.sink.events))
    finally:
        rig.sup.shutdown()
    gaps = [e for e in rig.sink.events if e.reason == "segment gap"]
    assert [dict(e.detail) for e in gaps] == [{"segment": 1, "gap_ns": S}]
    events = rig.events_text()
    assert '"segment gap"' in events


def test_shutdown_completes_session(rig: Rig) -> None:
    rig.sup.tick()
    wait_until(lambda: frame_count(rig.segment(0).meta) >= 1)
    rig.now[0] = int(0.4 * S)
    rig.sup.shutdown()
    rig.sup.shutdown()  # idempotent
    data = rig.session_json()
    assert data["state"] == "complete"
    assert data["stop_reason"] == "stopped by signal"
    assert [s["label"] for s in data["clock_samples"]] == ["start", "stop"]
    assert data["clock_samples"][-1]["boottime_ns"] == int(0.4 * S)
    assert rig.segment(0).frames.exists()
    assert data["segments"] == [
        {
            "index": 0,
            "start_boottime_ns": 0,
            "end_boottime_ns": int(0.4 * S),
            "frames": len(rig.segment(0).frames.read_text().splitlines()) - 1,
        }
    ]
    assert rig.bus.current.state is State.STOPPED
    events = [json.loads(line) for line in rig.events_text().splitlines()]
    states = [e["state"] for e in events if e["kind"] == "state"]
    assert states == ["recording", "stopping", "stopped"]


def test_shutdown_before_any_session(tmp_path: Path) -> None:
    rig = build(
        tmp_path, storage_check=lambda p, m: StorageCheck(False, 0, "recordings dir missing")
    )
    rig.sup.tick()
    rig.sup.shutdown()
    assert rig.sup.session is None
    assert list(tmp_path.iterdir()) == []
    assert rig.bus.current.state is State.STOPPED


def test_shutdown_with_camera_ignoring_sigint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RPICAM_MODE", "ignore_sigint")
    cfg = make_cfg(tmp_path, stop_timeout_s=0.5)
    rig = build(tmp_path, camera=lambda clock: RpicamCamera(cfg.camera), cfg=cfg)
    rig.sup.tick()
    wait_until(lambda: frame_count(rig.segment(0).meta) >= 1)
    t0 = time.monotonic()
    rig.sup.shutdown()
    assert time.monotonic() - t0 < 3.0
    assert rig.session_json()["state"] == "complete"


def test_wall_clock_jump_does_not_rotate(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.sup.tick()
    real = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: real() + 3600 * S)
    rig.now[0] = int(0.5 * S)
    for _ in range(3):
        rig.sup.tick()
    assert rig.session_json()["segments"] == []
    assert not rig.segment(1).video.exists()


def test_run_stops_on_event(rig: Rig) -> None:
    stop = threading.Event()
    thread = threading.Thread(target=rig.sup.run, args=(stop, 0.05))
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert rig.session_json()["state"] == "complete"
    assert rig.bus.current.state is State.STOPPED


def test_run_recovers_interrupted_sessions(tmp_path: Path) -> None:
    left = SessionStore(tmp_path).create({}, "old-boot", ClockSample(0, 0, 0))
    rig = build(tmp_path)
    stop = threading.Event()
    stop.set()
    rig.sup.run(stop, 0.01)
    assert read_json(left.path / "session.json")["state"] == "interrupted"
