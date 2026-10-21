from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from radar_cam_recorder.camera.fake import FakeCamera
from radar_cam_recorder.camera.metadata import parse_metadata
from radar_cam_recorder.camera.rpicam import RpicamCamera, build_command
from radar_cam_recorder.config import CameraConfig
from radar_cam_recorder.session import SegmentPaths

FAKE_BIN = Path(__file__).parent / "fake_rpicam.py"


def make_paths(root: Path) -> SegmentPaths:
    return SegmentPaths(
        index=0,
        video=root / "0000.h264",
        meta=root / "0000.meta.json",
        pts=root / "0000.pts.txt",
        frames=root / "0000.frames.csv",
        camera_log=root / "0000.log",
        radar=root / "0000.csv",
    )


def make_cfg(binary: str = str(FAKE_BIN), extra: tuple[str, ...] = ()) -> CameraConfig:
    return CameraConfig(
        enabled=True,
        binary=binary,
        width=1296,
        height=972,
        framerate=25.0,
        bitrate=4_000_000,
        extra_args=extra,
        stop_timeout_s=5.0,
        stall_timeout_s=10.0,
    )


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_build_command_exact(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    cfg = make_cfg("rpicam-vid", ("--awb", "auto"))
    assert build_command(cfg, paths) == [
        "rpicam-vid",
        "-n",
        "-t",
        "0",
        "--width",
        "1296",
        "--height",
        "972",
        "--framerate",
        "25.0",
        "--bitrate",
        "4000000",
        "--codec",
        "h264",
        "--inline",
        "-o",
        str(paths.video),
        "--save-pts",
        str(paths.pts),
        "--metadata",
        str(paths.meta),
        "--metadata-format",
        "json",
        "--awb",
        "auto",
    ]


def test_rpicam_records_and_stops_cleanly(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    run = RpicamCamera(make_cfg()).start(paths)
    assert wait_for(lambda: paths.meta.exists() and paths.meta.stat().st_size > 0)
    assert wait_for(lambda: len(parse_metadata(paths.meta.read_text())) >= 5)
    assert run.alive()
    assert run.returncode is None
    run.stop(5)
    assert run.returncode == 0
    assert not run.alive()
    assert len(parse_metadata(paths.meta.read_text())) >= 5
    run.stop(5)  # idempotent


def test_rpicam_exit1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RPICAM_MODE", "exit1")
    run = RpicamCamera(make_cfg()).start(make_paths(tmp_path))
    assert wait_for(lambda: not run.alive(), 0.5)
    assert run.returncode == 1
    assert "no cameras" in make_paths(tmp_path).camera_log.read_text()
    run.stop(1)  # safe on a dead run


def test_rpicam_ignored_sigint_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RPICAM_MODE", "ignore_sigint")
    paths = make_paths(tmp_path)
    run = RpicamCamera(make_cfg()).start(paths)
    assert wait_for(lambda: paths.meta.exists() and paths.meta.stat().st_size > 0)
    t0 = time.monotonic()
    run.stop(0.5)
    assert time.monotonic() - t0 < 1.5
    assert not run.alive()
    assert run.returncode is not None
    assert run.returncode != 0


def test_rpicam_closes_log_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RPICAM_MODE", "exit1")
    run = RpicamCamera(make_cfg()).start(make_paths(tmp_path))
    assert wait_for(lambda: not run.alive(), 2)
    assert run._log.closed


def test_rpicam_describe() -> None:
    cfg = replace(make_cfg("rpicam-vid"), extra_args=("--awb", "auto"))
    assert RpicamCamera(cfg).describe() == {
        "backend": "rpicam-vid",
        "binary": "rpicam-vid",
        "width": 1296,
        "height": 972,
        "framerate": 25.0,
        "bitrate": 4_000_000,
        "extra_args": ["--awb", "auto"],
    }


def test_fake_camera_records(tmp_path: Path) -> None:
    ticks = iter(range(1000, 10**9, 7))
    cam = FakeCamera(lambda: next(ticks), fps=200.0)
    paths = make_paths(tmp_path)
    run = cam.start(paths)
    assert wait_for(
        lambda: paths.meta.exists() and len(parse_metadata(paths.meta.read_text())) >= 5
    )
    run.stop(5)
    assert run.returncode == 0
    frames = parse_metadata(paths.meta.read_text())
    assert paths.video.stat().st_size == 1024 * len(frames)
    assert [f["SensorTimestamp"] for f in frames] == sorted(f["SensorTimestamp"] for f in frames)
    assert cam.describe() == {"backend": "fake", "fps": 200.0}
    run.stop(5)


def test_fake_camera_fail_starts(tmp_path: Path) -> None:
    cam = FakeCamera(lambda: 1, fps=100.0, fail_starts=1)
    first = cam.start(make_paths(tmp_path))
    assert not first.alive()
    assert first.returncode == 1
    first.stop(1)
    second = cam.start(make_paths(tmp_path))
    assert second.alive()
    second.stop(5)
    assert second.returncode == 0
