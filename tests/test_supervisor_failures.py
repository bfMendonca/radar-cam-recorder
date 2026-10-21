from __future__ import annotations

import errno
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import ld2451
import pytest

from radar_cam_recorder import radar as radar_module
from radar_cam_recorder.camera.base import CameraRun
from radar_cam_recorder.camera.fake import FakeCamera
from radar_cam_recorder.camera.rpicam import RpicamCamera
from radar_cam_recorder.config import Config, parse_config
from radar_cam_recorder.fakes import fake_radar_transport
from radar_cam_recorder.radar import RadarInfo, RadarStream, SegmentedRadarWriter
from radar_cam_recorder.session import SegmentPaths, SessionStore
from radar_cam_recorder.status import StatusBus
from radar_cam_recorder.storage import StorageCheck, check_storage
from radar_cam_recorder.supervisor import Supervisor
from tests.test_supervisor import FAKE_BIN, FRAME, ListSink, read_json, wait_until

S = 1_000_000_000
GIB = 2**30
PLENTY = StorageCheck(True, 10 * GIB)


def make_cfg(root: Path, *, camera: dict[str, Any] | None = None, **recording: Any) -> Config:
    return parse_config(
        {
            "recording": {"segment_s": 60.0, "retry_initial_s": 1.0, **recording},
            "storage": {"recordings_dir": str(root), "check_interval_s": 0.5},
            # FakeCamera writes on the real clock while these tests jump the manual one:
            # keep the stall watchdog out of the way unless a test sets it.
            "camera": {"binary": str(FAKE_BIN), "stall_timeout_s": 1000.0, **(camera or {})},
        }
    )


class ScriptedStorage:
    """Returns the scripted checks in order, then repeats the last one."""

    def __init__(self, *checks: StorageCheck) -> None:
        self.checks = list(checks)
        self.calls = 0

    def __call__(self, path: Path, require_mountpoint: bool) -> StorageCheck:
        check = self.checks[min(self.calls, len(self.checks) - 1)]
        self.calls += 1
        return check


def free(gib: float) -> StorageCheck:
    return StorageCheck(True, int(gib * GIB))


class CountingCamera:
    """FakeCamera that records the (manual) time and segment of every start."""

    def __init__(self, clock: Callable[[], int], fail_starts: int = 0) -> None:
        self._clock = clock
        self._cam = FakeCamera(clock, fail_starts=fail_starts)
        self.starts: list[int] = []
        self.segments: list[int] = []

    def start(self, paths: SegmentPaths) -> CameraRun:
        self.starts.append(self._clock())
        self.segments.append(paths.index)
        return self._cam.start(paths)

    def describe(self) -> dict[str, Any]:
        return self._cam.describe()


class FlakyRadar(RadarStream):
    """Fails ``fail_starts`` starts with a driver error, then works; records start times."""

    def __init__(self, clock: Callable[[], int], fail_starts: int) -> None:
        self.transport = fake_radar_transport()
        super().__init__(make_cfg(Path("unused")).radar, lambda: self.transport)
        self._clock = clock
        self._fail_starts = fail_starts
        self.starts: list[int] = []

    def start(self, writer: SegmentedRadarWriter) -> RadarInfo:
        self.starts.append(self._clock())
        if self._fail_starts > 0:
            self._fail_starts -= 1
            raise ld2451.LD2451Error("no answer from radar")
        return super().start(writer)


@dataclass
class Rig:
    sup: Supervisor
    now: list[int]
    bus: StatusBus
    sink: ListSink
    root: Path
    camera: Any
    radar: Any
    transport: ld2451.LoopbackTransport

    def tick_at(self, t_s: float) -> None:
        self.now[0] = int(t_s * S)
        self.sup.tick()

    def feed_radar(self) -> None:
        reads = self.sup._writer.reads
        self.transport.inject(FRAME)
        wait_until(lambda: self.sup._writer.reads > reads)

    def state(self) -> tuple[str, str]:
        cur = self.bus.current
        return cur.state.value, cur.reason

    def sessions(self) -> list[dict[str, Any]]:
        return [read_json(p) for p in sorted(self.root.glob("s*/session.json"))]

    def incidents(self, reason: str) -> list[dict[str, Any]]:
        return [
            dict(e.detail) for e in self.sink.events if e.kind == "incident" and e.reason == reason
        ]


Factory = Callable[[Callable[[], int]], Any]


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Rig]]:
    rigs: list[Rig] = []
    now = [0]

    def clock() -> int:
        return now[0]

    def manual_clock() -> tuple[Callable[[], int], str]:
        return clock, "manual"

    # The driver and the stream timestamp reads on the supervisor's manual clock.
    monkeypatch.setattr(radar_module, "default_clock", manual_clock)
    monkeypatch.setattr(ld2451.driver, "default_clock", manual_clock)

    def build(
        cfg: Config | None = None,
        *,
        camera: Factory = lambda clock: FakeCamera(clock),
        radar: Factory | None = None,
        storage: Callable[[Path, bool], StorageCheck] = lambda p, m: PLENTY,
        store: SessionStore | None = None,
    ) -> Rig:
        cfg = cfg or make_cfg(tmp_path)
        transport = fake_radar_transport()
        bus = StatusBus(clock)
        sink = ListSink()
        bus.add_sink(sink)
        cam = camera(clock)
        rad = radar(clock) if radar else RadarStream(cfg.radar, lambda: transport)
        transport = getattr(rad, "transport", transport)
        sup = Supervisor(
            cfg, bus=bus, camera=cam, radar=rad, store=store, clock=clock, storage_check=storage
        )
        rig = Rig(sup, now, bus, sink, tmp_path, cam, rad, transport)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.sup.shutdown()


def all_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".tmp")}


def test_disk_full_stops_and_resumes(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = make_rig(storage=ScriptedStorage(free(2), free(0.9), free(1.2), free(2)))
    states = []
    for t in (0.0, 0.5, 1.0, 1.5):
        rig.tick_at(t)
        states.append(rig.state()[0])
        if t == 0.0:
            before = all_files(tmp_path)
    assert states == ["recording", "disk_full", "disk_full", "recording"]
    assert rig.bus.current.reason == "recording"
    sessions = rig.sessions()
    assert len(sessions) == 2
    assert sessions[0]["state"] == "complete"
    assert sessions[0]["stop_reason"] == "disk full"
    assert sessions[1]["state"] == "in_progress"
    assert before <= all_files(tmp_path)  # nothing deleted


def test_disk_full_reason(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(storage=ScriptedStorage(free(2), free(0.5)))
    rig.tick_at(0)
    rig.tick_at(0.5)
    assert rig.state() == ("disk_full", "disk full")
    assert rig.sup.session is None


def test_disk_full_at_start_opens_nothing(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = make_rig(storage=ScriptedStorage(free(0.5)))
    rig.tick_at(0)
    rig.tick_at(0.2)
    rig.tick_at(0.6)
    assert rig.state() == ("disk_full", "disk full")
    assert rig.sup.session is None
    assert list(tmp_path.iterdir()) == []


def test_enospc_on_write_goes_disk_full(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(storage=ScriptedStorage(free(2), free(1.2), free(2)))
    rig.tick_at(0)
    session = rig.sup.session
    assert session is not None
    recorder = rig.sup._writer._recorder
    assert recorder is not None

    def full(t_rx_ns: int, data: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(recorder, "write", full)
    errors = rig.sup._writer.write_errors
    rig.transport.inject(FRAME)
    wait_until(lambda: rig.sup._writer.write_errors > errors)
    rig.tick_at(0.1)  # no exception
    assert rig.state() == ("disk_full", "disk full")
    assert read_json(session.path / "session.json")["stop_reason"] == "disk full"
    rig.tick_at(0.5)  # 1.2 GiB: ENOSPC needs resume_above before recording again
    assert rig.state() == ("disk_full", "disk full")
    rig.tick_at(1.0)
    assert rig.state() == ("recording", "recording")
    assert len(rig.sessions()) == 2


def test_enospc_on_flush_goes_disk_full(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.tick_at(0)

    def full() -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(rig.sup._writer, "flush", full)
    monkeypatch.setattr(rig.sup._writer, "close", full)
    rig.tick_at(0.5)
    assert rig.state() == ("disk_full", "disk full")
    assert rig.sessions()[0]["stop_reason"] == "disk full"


def test_other_write_error_is_storage_missing(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.tick_at(0)

    def broken() -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(rig.sup._writer, "flush", broken)
    rig.tick_at(0.5)
    assert rig.state() == (
        "storage_missing",
        "storage missing: recordings dir unavailable: Input/output error",
    )
    assert rig.sup.session is None


def test_storage_missing_then_back(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    root = tmp_path / "rec"
    rig = make_rig(make_cfg(root), storage=check_storage)
    rig.tick_at(0)
    assert rig.state() == ("storage_missing", "storage missing: recordings dir missing")
    assert not root.exists()  # never created
    root.mkdir()
    rig.tick_at(0.2)  # before the next check
    assert rig.state()[0] == "storage_missing"
    rig.tick_at(0.5)
    assert rig.state() == ("recording", "recording")
    assert rig.sup.session is not None
    assert rig.sup.session.path.parent == root


def run_feeding_radar(rig: Rig, start_s: float, stop_s: float, check: Callable[[], None]) -> None:
    """Tick every 0.25 s over [start_s, stop_s), feeding the radar each second."""
    t = start_s
    while t < stop_s - 1e-9:
        rig.tick_at(t)
        check()
        if round(t * 4) % 4 == 0:
            rig.feed_radar()  # keeps the radar from going silent
        t += 0.25


def kill_camera(rig: Rig) -> None:
    run = rig.sup._camera_run
    assert run is not None and run.alive()
    run.stop(2.0)


def test_camera_crash_degraded_and_backoff(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = make_rig(
        make_cfg(tmp_path, segment_s=10.0),  # segments longer than the retries
        camera=lambda clock: CountingCamera(clock, fail_starts=3),
    )

    def missing() -> None:
        assert rig.state() == ("degraded", "camera missing")

    def recording() -> None:
        assert rig.state() == ("recording", "recording")

    run_feeding_radar(rig, 0, 7, missing)
    assert [s / S for s in rig.camera.starts] == [0, 1, 3]
    assert rig.camera.segments == [0, 0, 0]  # runs that left nothing: no new segment
    run_feeding_radar(rig, 7, 17.25, recording)  # alive from 7 s: recording again at once
    assert [s / S for s in rig.camera.starts] == [0, 1, 3, 7, 10]  # 10 s: regular rotation
    assert rig.camera.segments == [0, 0, 0, 0, 1]
    # The run has been up since 7 s, a full segment: a new failure retries after retry_initial_s.
    kill_camera(rig)
    run_feeding_radar(rig, 17.25, 18.25, missing)
    rig.tick_at(18.25)
    assert [s / S for s in rig.camera.starts][-1] == 18.25
    assert rig.camera.segments[-1] == 2  # segment 1 has frames: the retry gets a new one
    assert rig.state() == ("recording", "recording")
    rig.sup.shutdown()
    assert rig.incidents("camera exited") == [
        {"segment": 0, "returncode": 1, "log": []},
        {"segment": 1, "returncode": 0, "log": []},
    ]


def test_camera_backoff_resets_only_after_full_segment(
    make_rig: Callable[..., Rig], tmp_path: Path
) -> None:
    rig = make_rig(
        make_cfg(tmp_path, segment_s=10.0),
        camera=lambda clock: CountingCamera(clock, fail_starts=3),
    )
    run_feeding_radar(rig, 0, 11, lambda: None)  # starts at 0, 1, 3, 7; rotation at 10
    kill_camera(rig)  # up 4 s only: the streak goes on, next delay 8 s
    run_feeding_radar(rig, 11, 19.25, lambda: None)
    assert [s / S for s in rig.camera.starts] == [0, 1, 3, 7, 10, 19]
    assert rig.camera.segments[-1] == 2
    rig.sup.shutdown()
    assert len(rig.incidents("camera exited")) == 1  # still the same streak


def test_camera_backoff_capped(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = make_rig(
        make_cfg(tmp_path, segment_s=30.0, retry_max_s=2.0),
        camera=lambda clock: CountingCamera(clock, fail_starts=5),
    )
    for step in range(0, 40):
        rig.tick_at(step * 0.25)
    assert [s / S for s in rig.camera.starts] == [0, 1, 3, 5, 7, 9]


def test_camera_binary_missing_is_degraded(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, camera={"binary": str(tmp_path / "no-such-rpicam")})
    rig = make_rig(cfg, camera=lambda clock: RpicamCamera(cfg.camera))
    rig.tick_at(0)
    assert rig.state() == ("degraded", "camera missing")
    rig.tick_at(1.0)  # retry fails the same way, without an exception
    assert rig.state() == ("degraded", "camera missing")
    rig.sup.shutdown()
    assert len(rig.incidents("camera start failed")) == 1


def test_radar_silence_degraded_then_recovers(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.tick_at(0)
    assert rig.state() == ("recording", "recording")
    rig.tick_at(4.5)
    assert rig.state() == ("recording", "recording")
    rig.tick_at(5.5)
    assert rig.state() == ("degraded", "radar missing")
    rig.feed_radar()
    rig.tick_at(5.6)
    assert rig.state() == ("recording", "recording")


def test_radar_start_failure_retries(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(radar=lambda clock: FlakyRadar(clock, fail_starts=2))
    rig.tick_at(0)
    assert rig.state() == ("degraded", "radar missing")
    assert rig.sup.session is not None  # the camera keeps recording
    t = 0.25
    while t <= 3.0:
        rig.tick_at(t)
        t += 0.25
    assert [s / S for s in rig.radar.starts] == [0, 1, 3]
    assert rig.state() == ("recording", "recording")
    devices = read_json(rig.sup.session.path / "session.json")["devices"]
    assert devices["radar"]["firmware"]
    rig.sup.shutdown()
    assert len(rig.incidents("radar start failed")) == 1


def test_both_missing_waiting(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(
        camera=lambda clock: FakeCamera(clock, fail_starts=100),
        radar=lambda clock: FlakyRadar(clock, fail_starts=100),
    )
    for step in range(8):
        rig.tick_at(step * 0.5)
        assert rig.state() == ("waiting_for_devices", "camera and radar missing")


def test_camera_disabled_is_not_degraded(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = make_rig(make_cfg(tmp_path, camera={"enabled": False}))
    rig.tick_at(0)
    rig.tick_at(0.5)
    assert rig.state() == ("recording", "recording")
    assert rig.sessions()[0]["devices"]["camera"] is None


def test_no_camera_is_not_degraded(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(camera=lambda clock: None)
    rig.tick_at(0)
    assert rig.state() == ("recording", "recording")


def test_unreadable_meta_skips_gap(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(make_cfg(tmp_path, segment_s=1.0))
    rig.tick_at(0)
    session = rig.sup.session
    assert session is not None
    seg0, seg1 = session.segment(0), session.segment(1)
    wait_until(lambda: seg0.meta.exists() and seg0.meta.stat().st_size > 50)
    rig.tick_at(1.1)
    wait_until(lambda: seg1.meta.exists())
    real = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == seg1.meta:
            raise OSError(errno.EIO, "Input/output error")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    rig.tick_at(1.2)  # no exception, gap skipped
    assert rig.sup._pending_gap is None
    assert rig.state() == ("recording", "recording")


class BrokenStore(SessionStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.broken = True
        self.creates = 0

    def create(self, config: Any, boot_id: str, start: Any) -> Any:
        self.creates += 1
        if self.broken:
            raise RuntimeError("boom")
        return super().create(config, boot_id, start)


def test_unexpected_error_opening_session_is_error_state(
    make_rig: Callable[..., Rig], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = BrokenStore(tmp_path)
    rig = make_rig(store=store)
    with caplog.at_level(logging.DEBUG, logger="radar_cam_recorder"):
        rig.tick_at(0)  # no exception
        assert rig.state() == ("error", "error: RuntimeError: boom")
        assert rig.sup.session is None
        rig.tick_at(0.2)  # before the next check: no retry
        assert store.creates == 1
        rig.tick_at(0.5)
        rig.tick_at(1.0)
        assert store.creates == 3
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1  # once per streak
    assert list(tmp_path.iterdir()) == []
    store.broken = False
    rig.tick_at(1.5)
    assert rig.state() == ("recording", "recording")


class BrokenDescribeCamera:
    def __init__(self, clock: Callable[[], int]) -> None:
        self._cam = FakeCamera(clock)

    def start(self, paths: SegmentPaths) -> CameraRun:
        return self._cam.start(paths)

    def describe(self) -> dict[str, Any]:
        raise RuntimeError("bad camera description")


def test_error_after_session_created_leaves_no_session_open(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(camera=BrokenDescribeCamera)
    rig.tick_at(0)
    assert rig.state() == ("error", "error: RuntimeError: bad camera description")
    assert rig.sup.session is None
    [data] = rig.sessions()
    assert data["state"] == "complete"
    assert data["stop_reason"] == "error: RuntimeError: bad camera description"


class VideoOnlyCamera:
    """First run writes video bytes and an empty meta, then dies; later runs are fakes."""

    def __init__(self, clock: Callable[[], int]) -> None:
        self._cam = CountingCamera(clock, fail_starts=1)
        self.segments = self._cam.segments
        self.first = True

    def start(self, paths: SegmentPaths) -> CameraRun:
        if self.first:
            self.first = False
            paths.video.write_bytes(b"\x00\x00\x00\x01video")
            paths.meta.write_text("")
        return self._cam.start(paths)

    def describe(self) -> dict[str, Any]:
        return self._cam.describe()


@pytest.mark.parametrize("meta", ["empty", "missing"])
def test_camera_retry_never_overwrites_video(
    make_rig: Callable[..., Rig], tmp_path: Path, meta: str
) -> None:
    rig = make_rig(camera=VideoOnlyCamera)
    rig.tick_at(0)  # the first run dies at once, leaving video but no frame
    session = rig.sup.session
    assert session is not None
    seg0 = session.segment(0)
    if meta == "missing":
        seg0.meta.unlink()
    rig.tick_at(1.0)  # retry due
    assert rig.camera.segments == [0, 1]
    assert seg0.video.read_bytes() == b"\x00\x00\x00\x01video"
    assert rig.state() == ("recording", "recording")


class ManualRun:
    def __init__(self, paths: SegmentPaths) -> None:
        self.paths = paths
        self.frames = 0
        self.stopped = False
        paths.meta.write_text("[\n")

    @property
    def returncode(self) -> int | None:
        return 0 if self.stopped else None

    def alive(self) -> bool:
        return not self.stopped

    def stop(self, timeout: float) -> None:
        self.stopped = True

    def write_frame(self, sensor_ts: int) -> None:
        with open(self.paths.meta, "a", encoding="utf-8") as fh:
            fh.write(("" if self.frames == 0 else ",\n") + f'{{"SensorTimestamp": {sensor_ts}}}')
        self.frames += 1


class ManualCamera:
    """Runs stay alive until stopped and write a frame only when the test says so."""

    def __init__(self, clock: Callable[[], int]) -> None:
        self._clock = clock
        self.runs: list[ManualRun] = []
        self.starts: list[int] = []

    def start(self, paths: SegmentPaths) -> CameraRun:
        self.starts.append(self._clock())
        self.runs.append(ManualRun(paths))
        return self.runs[-1]

    def describe(self) -> dict[str, Any]:
        return {"backend": "manual"}


def stall_rig(make_rig: Callable[..., Rig], tmp_path: Path) -> Rig:
    return make_rig(make_cfg(tmp_path, camera={"stall_timeout_s": 2.0}), camera=ManualCamera)


def test_no_false_stall_while_frames_arrive(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = stall_rig(make_rig, tmp_path)
    for step in range(40):  # 0 to 9.75 s, one frame per tick
        rig.tick_at(step * 0.25)
        rig.camera.runs[-1].write_frame(step)
        if step % 4 == 0:
            rig.feed_radar()
        assert rig.state() == ("recording", "recording")
    assert len(rig.camera.runs) == 1
    rig.sup.shutdown()
    assert rig.incidents("camera stalled") == []


def test_camera_stall_without_frames(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = stall_rig(make_rig, tmp_path)

    def tick(t: float) -> None:
        rig.tick_at(t)
        if round(t * 4) % 4 == 0:
            rig.feed_radar()

    for step in range(8):  # 0 to 1.75 s: grace after start
        tick(step * 0.25)
        assert rig.state() == ("recording", "recording")
    tick(2.0)
    first = rig.camera.runs[0]
    assert first.stopped
    assert rig.state() == ("degraded", "camera missing")
    for step in range(9, 33):  # to 8 s
        tick(step * 0.25)
    assert [s / S for s in rig.camera.starts] == [0, 3, 7]  # stalls at 2 and 5: backoff 1, 2
    assert [r.paths.index for r in rig.camera.runs] == [0, 0, 0]  # nothing recorded, same segment
    rig.sup.shutdown()
    assert rig.incidents("camera stalled") == [{"segment": 0, "stalled_s": 2.0}]


def test_camera_stall_after_frames(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    rig = stall_rig(make_rig, tmp_path)
    rig.tick_at(0)
    for step in range(1, 5):  # frames until 1 s, then nothing
        rig.camera.runs[-1].write_frame(step)
        rig.tick_at(step * 0.25)
    rig.feed_radar()
    rig.tick_at(2.75)
    assert rig.state() == ("recording", "recording")
    rig.tick_at(3.0)  # 2 s since the meta last grew
    assert rig.state() == ("degraded", "camera missing")
    seg0 = rig.camera.runs[0].paths
    assert len(seg0.frames.read_text().splitlines()) == 1 + 4  # frames CSV written
    rig.tick_at(4.0)  # retry: segment 0 has frames, so a new segment
    assert [r.paths.index for r in rig.camera.runs] == [0, 1]
    assert rig.state() == ("recording", "recording")
    rig.sup.shutdown()
    assert rig.incidents("camera stalled") == [{"segment": 0, "stalled_s": 2.0}]
