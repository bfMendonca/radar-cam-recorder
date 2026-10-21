from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import ld2451
import pytest
from ld2451.clock import default_clock

from radar_cam_recorder.config import RadarConfig
from radar_cam_recorder.fakes import FakeRadarResponder, fake_radar_transport
from radar_cam_recorder.radar import RadarStream, SegmentedRadarWriter

FRAME = bytes.fromhex(
    "F4F3F2F1 1100 03 01 8A28003C15 8A1E013C0F 765F003C0F F8F7F6F5".replace(" ", "")
)


def make_cfg(detection: ld2451.DetectionParams | None = None) -> RadarConfig:
    return RadarConfig(
        enabled=True, port="/dev/null", baudrate=115200, silence_timeout_s=5, detection=detection
    )


def wait_until(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def data_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()[1:]


def test_injected_frames_are_recorded(tmp_path: Path) -> None:
    transport = fake_radar_transport()
    stream = RadarStream(make_cfg(), lambda: transport)
    writer = SegmentedRadarWriter()
    path = tmp_path / "radar.csv"
    writer.open(path)
    stream.start(writer)
    try:
        transport.inject(FRAME)
        wait_until(lambda: writer.reads >= 1)
    finally:
        stream.stop()
        writer.close()
    lines = path.read_text().splitlines()
    assert lines[0] == "t_rx_ns,hex"
    records = list(ld2451.read_records(path))
    assert b"".join(d for _, d in records).endswith(FRAME)
    assert all(len(line.split(",")) == 2 for line in lines[1:])


def test_rotate_during_writes_loses_and_duplicates_nothing(tmp_path: Path) -> None:
    writer = SegmentedRadarWriter()
    paths = [tmp_path / "a.csv"]
    writer.open(paths[0])
    started = threading.Event()

    def produce() -> None:
        for i in range(10_000):
            writer.write(i + 1, b"\x01")
            if i == 50:
                started.set()

    t = threading.Thread(target=produce)
    t.start()
    started.wait(2.0)
    while t.is_alive():  # keep rotating while the producer runs
        paths.append(tmp_path / f"{len(paths)}.csv")
        writer.rotate(paths[-1])
    t.join()
    writer.close()
    assert len(paths) >= 2
    stamps = [int(x.split(",")[0]) for p in paths for x in data_lines(p)]
    assert stamps == list(range(1, 10_001))
    assert writer.reads == 10_000
    assert writer.last_rx_ns == 10_000


def test_start_returns_firmware_and_params() -> None:
    stream = RadarStream(make_cfg(), fake_radar_transport)
    writer = SegmentedRadarWriter()
    try:
        info = stream.start(writer)
        assert stream.running
    finally:
        stream.stop()
    assert not stream.running
    assert info.firmware == "V1.01.24051510"
    assert info.params == {
        "max_distance_m": 100,
        "direction": 1,
        "min_speed_kmh": 5,
        "no_target_delay_s": 2,
    }


def test_detection_is_applied() -> None:
    transport = fake_radar_transport()
    params = ld2451.DetectionParams(
        max_distance_m=50,
        direction=ld2451.Direction.BOTH,
        min_speed_kmh=7,
        no_target_delay_s=3,
    )
    stream = RadarStream(make_cfg(params), lambda: transport)
    try:
        info = stream.start(SegmentedRadarWriter())
    finally:
        stream.stop()
    fake = transport.responder
    assert isinstance(fake, FakeRadarResponder)
    assert fake.params == bytes((50, 2, 7, 3))
    assert info.params == {
        "max_distance_m": 50,
        "direction": 2,
        "min_speed_kmh": 7,
        "no_target_delay_s": 3,
    }


def test_silent_for_grows_and_resets_on_inject(tmp_path: Path) -> None:
    transport = fake_radar_transport()
    stream = RadarStream(make_cfg(), lambda: transport)
    writer = SegmentedRadarWriter()
    writer.open(tmp_path / "r.csv")
    stream.start(writer)
    try:
        # command replies are reads too; wait for them to settle
        wait_until(lambda: writer.last_rx_ns is not None)
        time.sleep(0.05)
        now = default_clock()[0]
        first = stream.silent_for_ns(now(), writer)
        time.sleep(0.02)
        second = stream.silent_for_ns(now(), writer)
        assert second > first
        before = writer.reads
        transport.inject(FRAME)
        wait_until(lambda: writer.reads > before)
        assert stream.silent_for_ns(now(), writer) < second
    finally:
        stream.stop()
        writer.close()


def test_silent_for_without_any_read_counts_from_start() -> None:
    stream = RadarStream(make_cfg(), fake_radar_transport)
    writer = SegmentedRadarWriter()
    started = default_clock()[0]()
    assert stream.silent_for_ns(started + 1_000, writer) >= 1_000


def test_open_failure_propagates() -> None:
    class Broken(ld2451.LoopbackTransport):
        def open(self) -> None:
            raise ld2451.TransportError("no such port")

    stream = RadarStream(make_cfg(), Broken)
    with pytest.raises(ld2451.TransportError):
        stream.start(SegmentedRadarWriter())
    assert not stream.running
