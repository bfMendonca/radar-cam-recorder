"""Idle mode, button commands (split/toggle) and the progress signal (spec 4)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

from radar_cam_recorder.camera.base import CameraSource
from radar_cam_recorder.camera.fake import FakeCamera, FakeRun
from radar_cam_recorder.cli import previous_run_unclean
from radar_cam_recorder.clocks import ClockSample
from radar_cam_recorder.config import Config
from radar_cam_recorder.session import SegmentPaths, SessionStore
from radar_cam_recorder.status import State, StatusFileSink
from radar_cam_recorder.storage import StorageCheck
from tests.test_supervisor import (
    Rig,
    S,
    build,
    frame_count,
    inject_and_wait,
    make_cfg,
    read_json,
    wait_until,
)

GIB = 2**30


def idle_cfg(root: Path) -> Config:
    cfg = make_cfg(root)
    return replace(cfg, control=replace(cfg.control, start_mode="idle"))


def session_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir())


class Calls:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool]] = []
        self.progress = 0

    def on_command(self, command: str, accepted: bool) -> None:
        self.commands.append((command, accepted))

    def on_progress(self) -> None:
        self.progress += 1


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., tuple[Rig, Calls]]]:
    rigs: list[Rig] = []

    def factory(cfg: Config | None = None, **kwargs: Any) -> tuple[Rig, Calls]:
        calls = Calls()
        rig = build(
            tmp_path,
            cfg=cfg or idle_cfg(tmp_path),
            on_command=calls.on_command,
            on_progress=calls.on_progress,
            **kwargs,
        )
        rigs.append(rig)
        return rig, calls

    yield factory
    for rig in rigs:
        rig.sup.shutdown()


def state_of(rig: Rig) -> State:
    return rig.bus.current.state  # a call, so mypy does not narrow it across ticks


def states(rig: Rig) -> list[tuple[State, str]]:
    return [(e.state, e.reason) for e in rig.sink.events if e.kind == "state"]


def test_starts_idle_and_records_nothing(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, _ = make()
    rig.sup.tick()
    rig.now[0] += 5 * S
    rig.sup.tick()
    assert state_of(rig) is State.IDLE
    assert rig.bus.current.reason == "idle"
    assert rig.sup.session is None
    assert session_dirs(rig.root) == []


def test_start_mode_record(make: Callable[..., tuple[Rig, Calls]], tmp_path: Path) -> None:
    rig, _ = make(make_cfg(tmp_path))
    rig.sup.tick()
    assert rig.sup.session is not None
    assert state_of(rig) is State.RECORDING


def test_toggle_starts_and_stops(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, calls = make()
    rig.sup.tick()
    rig.now[0] += S // 10  # before the next storage check: the toggle forces it
    rig.sup.request("toggle")
    rig.sup.tick()
    session = rig.sup.session
    assert session is not None
    assert state_of(rig) is State.RECORDING
    rig.now[0] += S // 10
    rig.sup.request("toggle")
    rig.sup.tick()
    assert rig.sup.session is None
    assert state_of(rig) is State.IDLE
    data = read_json(session.path / "session.json")
    assert data["state"] == "complete"
    rig.now[0] += 2 * S  # storage checks while idle never reopen
    rig.sup.tick()
    assert rig.sup.session is None
    assert calls.commands == [("toggle", True), ("toggle", True)]
    rig.sup.shutdown()
    seen = states(rig)
    i = seen.index((State.RECORDING, "recording"))
    assert seen[i + 1 :][:2] == [(State.STOPPING, "stopping recording"), (State.IDLE, "idle")]


def test_split_closes_and_reopens(make: Callable[..., tuple[Rig, Calls]], tmp_path: Path) -> None:
    rig, calls = make(make_cfg(tmp_path))
    rig.sup.tick()
    old = rig.sup.session
    assert old is not None
    wait_until(lambda: frame_count(old.segment(0).meta) >= 2)
    rig.now[0] += S // 10
    rig.sup.request("split")
    rig.sup.tick()
    new = rig.sup.session
    assert new is not None and new.id != old.id
    data = read_json(old.path / "session.json")
    assert data["state"] == "complete"
    assert data["stop_reason"] == "split by button"
    assert state_of(rig) is State.RECORDING
    assert calls.commands == [("split", True)]
    wait_until(lambda: frame_count(new.segment(0).meta) >= 1)
    rig.sup.tick()
    wait_until(lambda: any(e.reason == "segment gap" for e in rig.sink.events))
    gap = [e for e in rig.sink.events if e.reason == "segment gap"]
    assert gap[0].detail["segment"] == 0
    rig.sup.shutdown()  # delivers the queued events to the session's events.jsonl
    assert '"segment gap"' in (new.path / "events.jsonl").read_text()


def test_split_rejected_when_idle(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, calls = make()
    rig.sup.tick()
    rig.sup.request("split")
    rig.sup.tick()
    assert calls.commands == [("split", False)]
    assert rig.sup.session is None
    assert session_dirs(rig.root) == []


def test_command_while_starting_rejected(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, calls = make()
    rig.sup.request("toggle")  # before the first tick: state is still STARTING
    rig.sup.tick()
    assert calls.commands == [("toggle", False)]
    assert rig.sup.session is None
    assert state_of(rig) is State.IDLE


def test_unknown_command_raises(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, _ = make()
    with pytest.raises(ValueError):
        rig.sup.request("reboot")


class SlowStopCamera:
    """FakeCamera whose run stop takes 3 s on the manual clock, calling ``hook`` once at 1 s."""

    def __init__(self, clock: Callable[[], int]) -> None:
        self._camera = FakeCamera(clock)
        self.now: list[int] = []  # the rig's manual clock, set once the rig exists
        self.hook: Callable[[], None] | None = None

    def start(self, paths: SegmentPaths) -> FakeRun:
        run = self._camera.start(paths)
        stop = run.stop

        def slow_stop(timeout: float) -> None:
            stop(timeout)
            self.now[0] += S
            hook, self.hook = self.hook, None
            if hook is not None:
                hook()  # in the middle of the stop
            self.now[0] += 2 * S

        run.stop = slow_stop  # type: ignore[method-assign]
        return run

    def describe(self) -> dict[str, Any]:
        return self._camera.describe()


def test_command_during_long_command_rejected(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    cameras: list[SlowStopCamera] = []

    def camera(clock: Callable[[], int]) -> CameraSource:
        cameras.append(SlowStopCamera(clock))
        return cameras[-1]

    rig, calls = make(make_cfg(tmp_path), camera=camera)
    cameras[0].now = rig.now
    rig.sup.tick()
    first = rig.sup.session
    assert first is not None
    cameras[0].hook = lambda: rig.sup.request("toggle")  # arrives during the split
    rig.sup.request("split")
    rig.sup.tick()
    rig.sup.tick()
    assert calls.commands == [("split", True), ("toggle", False)]
    assert rig.sup.session is not None and rig.sup.session.id != first.id
    assert state_of(rig) is State.RECORDING
    rig.sup.request("split")  # after the split ended: accepted again
    rig.sup.tick()
    assert calls.commands[-1] == ("split", True)


class MutableStorage:
    def __init__(self, check: StorageCheck) -> None:
        self.check = check

    def __call__(self, path: Path, require_mountpoint: bool) -> StorageCheck:
        return self.check


def test_toggle_in_disk_full_goes_idle_and_stays(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    storage = MutableStorage(StorageCheck(True, 0))
    rig, calls = make(make_cfg(tmp_path), storage_check=storage)
    rig.sup.tick()
    assert state_of(rig) is State.DISK_FULL
    rig.sup.request("toggle")
    rig.sup.tick()
    assert state_of(rig) is State.IDLE
    storage.check = StorageCheck(True, 10 * GIB)
    for _ in range(3):
        rig.now[0] += S
        rig.sup.tick()
    assert state_of(rig) is State.IDLE
    assert rig.sup.session is None
    assert calls.commands == [("toggle", True)]


def test_toggle_in_storage_missing_goes_idle(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    storage = MutableStorage(StorageCheck(False, 0, "not mounted"))
    rig, calls = make(make_cfg(tmp_path), storage_check=storage)
    rig.sup.tick()
    assert state_of(rig) is State.STORAGE_MISSING
    rig.sup.request("split")  # no session open
    rig.sup.tick()
    rig.now[0] += S // 10
    rig.sup.request("toggle")
    rig.sup.tick()
    assert calls.commands == [("split", False), ("toggle", True)]
    assert state_of(rig) is State.IDLE


def test_idle_never_reports_storage_states(make: Callable[..., tuple[Rig, Calls]]) -> None:
    storage = MutableStorage(StorageCheck(False, 0, "not mounted"))
    rig, _ = make(storage_check=storage)
    for _ in range(3):
        rig.sup.tick()
        rig.now[0] += S
    assert state_of(rig) is State.IDLE
    rig.sup.shutdown()
    assert all(s not in (State.STORAGE_MISSING, State.DISK_FULL) for s, _ in states(rig))


def test_problem_flag_from_recovery(make: Callable[..., tuple[Rig, Calls]], tmp_path: Path) -> None:
    left = SessionStore(tmp_path).create({}, "old-boot", ClockSample(0, 0, 0))
    rig, calls = make()
    rig.sup.tick()
    assert read_json(left.path / "session.json")["state"] == "interrupted"
    current = rig.bus.current
    assert (current.state, current.reason) == (State.IDLE, "idle after a problem")
    assert current.detail == {"problem": True}
    rig.sup.request("split")  # rejected, but any press clears the warning
    rig.sup.tick()
    assert calls.commands == [("split", False)]
    assert (rig.bus.current.state, rig.bus.current.reason) == (State.IDLE, "idle")


def test_warn_at_start(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, _ = make(warn_at_start=True)
    rig.sup.tick()
    assert (rig.bus.current.state, rig.bus.current.reason) == (State.IDLE, "idle after a problem")


def test_progress_from_camera(make: Callable[..., tuple[Rig, Calls]], tmp_path: Path) -> None:
    cfg = idle_cfg(tmp_path)
    cfg = replace(cfg, radar=replace(cfg.radar, enabled=False))
    rig, calls = make(cfg)
    for _ in range(3):
        rig.sup.tick()
    assert calls.progress == 0  # idle: never
    rig.sup.request("toggle")
    rig.sup.tick()
    session = rig.sup.session
    assert session is not None
    size = session.segment(0).meta.stat().st_size if session.segment(0).meta.exists() else 0
    wait_until(
        lambda: session.segment(0).meta.exists() and session.segment(0).meta.stat().st_size > size
    )
    before = calls.progress
    rig.sup.tick()
    assert calls.progress == before + 1


def test_progress_from_radar_only(make: Callable[..., tuple[Rig, Calls]], tmp_path: Path) -> None:
    rig, calls = make(make_cfg(tmp_path), camera=lambda clock: None)
    rig.sup.tick()
    rig.sup.tick()  # baseline after the start-up command ACKs
    calls.progress = 0
    rig.sup.tick()
    rig.sup.tick()
    assert calls.progress == 0  # silent radar, no camera
    inject_and_wait(rig)
    rig.sup.tick()
    assert calls.progress == 1


def test_heartbeat_called_each_tick(make: Callable[..., tuple[Rig, Calls]]) -> None:
    rig, _ = make()
    stop = threading.Event()
    beats: list[int] = []

    def heartbeat() -> None:
        beats.append(1)
        if len(beats) >= 3:
            stop.set()

    rig.sup.run(stop, 0.0, heartbeat=heartbeat)
    assert len(beats) == 3


class BeatingStore(SessionStore):
    """Beats the heartbeat it is given three times, as a long recovery would."""

    def recover_interrupted(self, frames_writer: Any = None, heartbeat: Any = None) -> list[str]:
        for _ in range(3):
            heartbeat()
        return []


def test_run_heartbeat_reaches_recovery(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    rig, _ = make(store=BeatingStore(tmp_path))
    stop = threading.Event()
    beats: list[int] = []

    def heartbeat() -> None:
        beats.append(1)
        stop.set()  # stop after the first tick

    rig.sup.run(stop, 0.0, heartbeat=heartbeat)
    assert len(beats) == 4  # three during recovery, one for the tick


def test_crash_in_run_ends_in_error_not_stopped(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    status = tmp_path / "status.json"
    checks = {"n": 0}

    def storage(path: Path, require_mountpoint: bool) -> StorageCheck:
        checks["n"] += 1
        if checks["n"] > 1:
            raise RuntimeError("boom")
        return StorageCheck(True, 10 * GIB)

    rig, _ = make(make_cfg(tmp_path), storage_check=storage)
    rig.bus.add_sink(StatusFileSink(status))
    stop = threading.Event()

    def heartbeat() -> None:
        rig.now[0] += S  # the next tick runs the (crashing) storage check

    with pytest.raises(RuntimeError, match="boom"):
        rig.sup.run(stop, 0.0, heartbeat=heartbeat)
    sessions = session_dirs(rig.root)
    assert len(sessions) == 1
    data = read_json(sessions[0] / "session.json")
    assert data["state"] == "complete"  # the data is safe
    assert data["stop_reason"] == "crashed: RuntimeError: boom"
    final = rig.bus.current
    assert (final.state, final.reason) == (State.ERROR, "crashed: RuntimeError: boom")
    assert states(rig)[-2:] == [
        (State.STOPPING, "crashed: RuntimeError: boom"),
        (State.ERROR, "crashed: RuntimeError: boom"),
    ]
    assert read_json(status)["state"] == "error"
    assert previous_run_unclean(status) is True


def test_signal_stop_still_ends_stopped(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    status = tmp_path / "status.json"
    rig, _ = make(make_cfg(tmp_path))
    rig.bus.add_sink(StatusFileSink(status))
    stop = threading.Event()
    rig.sup.run(stop, 0.0, heartbeat=stop.set)
    assert rig.bus.current.state is State.STOPPED
    assert previous_run_unclean(status) is False


class FlakyCreateStore(SessionStore):
    """``create`` raises ``error`` while it is set."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.error: Exception | None = None

    def create(self, *args: Any, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        return super().create(*args, **kwargs)


def test_split_whose_open_fails_retries(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    store = FlakyCreateStore(tmp_path)
    rig, calls = make(make_cfg(tmp_path), store=store)
    rig.sup.tick()
    old = rig.sup.session
    assert old is not None
    store.error = RuntimeError("no luck")
    rig.now[0] += S // 10
    rig.sup.request("split")
    rig.sup.tick()
    assert calls.commands == [("split", True)]
    assert rig.sup.session is None
    assert state_of(rig) is State.ERROR
    assert read_json(old.path / "session.json")["state"] == "complete"
    rig.now[0] += S  # the next storage check retries, still failing
    rig.sup.tick()
    assert rig.sup.session is None
    assert state_of(rig) is State.ERROR
    store.error = None
    rig.now[0] += S
    rig.sup.tick()
    new = rig.sup.session
    assert new is not None and new.id != old.id
    assert state_of(rig) is State.RECORDING
    rig.now[0] += S
    rig.sup.tick()
    assert rig.sup.session is new  # opened once
    assert len(session_dirs(rig.root)) == 2


def test_split_whose_open_hits_storage_error(
    make: Callable[..., tuple[Rig, Calls]], tmp_path: Path
) -> None:
    store = FlakyCreateStore(tmp_path)
    rig, _ = make(make_cfg(tmp_path), store=store)
    rig.sup.tick()
    old = rig.sup.session
    assert old is not None
    store.error = OSError(5, "Input/output error")
    rig.now[0] += S // 10
    rig.sup.request("split")
    rig.sup.tick()
    assert rig.sup.session is None
    assert state_of(rig) is State.STORAGE_MISSING
    assert read_json(old.path / "session.json")["state"] == "complete"
    store.error = None
    rig.now[0] += S
    rig.sup.tick()
    assert rig.sup.session is not None and rig.sup.session.id != old.id
    assert state_of(rig) is State.RECORDING
    assert len(session_dirs(rig.root)) == 2


def test_toggle_stop_reason(make: Callable[..., tuple[Rig, Calls]], tmp_path: Path) -> None:
    rig, _ = make(make_cfg(tmp_path))
    rig.sup.tick()
    session = rig.sup.session
    assert session is not None
    rig.now[0] += S // 10
    rig.sup.request("toggle")
    rig.sup.tick()
    assert read_json(session.path / "session.json")["stop_reason"] == "stopped by command"
