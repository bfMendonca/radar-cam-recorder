from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import ld2451
import pytest
from ld2451.clock import default_clock

from radar_cam_recorder.camera.fake import FakeCamera
from radar_cam_recorder.fakes import fake_radar_transport
from radar_cam_recorder.radar import RadarStream
from radar_cam_recorder.reader import Frame, open_session
from radar_cam_recorder.status import StatusBus
from radar_cam_recorder.storage import StorageCheck
from radar_cam_recorder.supervisor import Supervisor
from tests.test_supervisor import FRAME, S, make_cfg, wait_until


def record_session(root: Path) -> Path:
    """Two segments from a real Supervisor + FakeCamera + loopback radar."""
    boot = default_clock()[0]
    now = [0]
    cfg = make_cfg(root)
    transport = fake_radar_transport()
    sup = Supervisor(
        cfg,
        bus=StatusBus(lambda: now[0]),
        camera=FakeCamera(boot),
        radar=RadarStream(cfg.radar, lambda: transport),
        clock=lambda: now[0],
        storage_check=lambda p, m: StorageCheck(True, 10**12, ""),
    )

    def settle(index: int) -> None:
        seg = sup.session.segment(index)  # type: ignore[union-attr]
        wait_until(lambda: seg.meta.exists() and seg.meta.read_text().count("Sensor") >= 3)
        reads = sup._writer.reads
        transport.inject(FRAME)
        wait_until(lambda: sup._writer.reads > reads)

    try:
        sup.tick()
        settle(0)
        now[0] = int(1.1 * S)
        sup.tick()
        settle(1)
    finally:
        sup.shutdown()
    assert sup.session is not None
    return sup.session.path


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return record_session(tmp_path_factory.mktemp("rec"))


@pytest.fixture
def session_dir(recorded: Path, tmp_path: Path) -> Iterator[Path]:
    dest = tmp_path / "copy"
    shutil.copytree(recorded, dest)
    yield dest


def test_frames_increase_across_segments(session_dir: Path) -> None:
    frames = list(open_session(session_dir).frames())
    assert {f.segment for f in frames} == {0, 1}
    stamps = [f.sensor_ts_ns for f in frames]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert frames[0].video.name == "0000.h264"


def test_injected_report_decodes_to_three_targets(session_dir: Path) -> None:
    reports = [r for r in open_session(session_dir).radar_reports() if r.targets]
    assert len(reports) == 2  # one per segment; config ACKs are skipped
    assert all(len(r.targets) == 3 for r in reports)


def test_timeline_non_decreasing_and_complete(session_dir: Path) -> None:
    session = open_session(session_dir)
    items = list(session.timeline())
    stamps = [i.sensor_ts_ns if isinstance(i, Frame) else i.t_rx_ns for i in items]
    assert stamps == sorted(stamps)
    assert len(items) == len(list(session.frames())) + len(list(session.radar_reports()))


def test_radar_latency_shifts_timeline_key(session_dir: Path) -> None:
    path = session_dir / "session.json"
    data = json.loads(path.read_text())
    data["radar_latency_ns"] = 10 * S
    path.write_text(json.dumps(data))
    items = list(open_session(session_dir).timeline())
    assert isinstance(items[0], ld2451.RadarReport)  # shifted 10 s earlier than any frame
    keys = [i.sensor_ts_ns if isinstance(i, Frame) else i.t_rx_ns - 10 * S for i in items]
    assert keys == sorted(keys)


def test_frame_at_returns_closest_frame(session_dir: Path) -> None:
    session = open_session(session_dir)
    report = next(r for r in session.radar_reports() if r.targets)
    frames = list(session.frames())
    found = session.frame_at(report.t_rx_ns)
    assert found is not None
    best = min(frames, key=lambda f: abs(f.sensor_ts_ns - report.t_rx_ns))
    assert found == best
    assert session.frame_at(frames[0].sensor_ts_ns - 10 * S) is None
    assert session.frame_at(frames[-1].sensor_ts_ns + 10 * S) is None


def test_summary(session_dir: Path) -> None:
    summary = open_session(session_dir).summary()
    assert summary["state"] == "complete"
    assert summary["overlap_s"] > 0
    assert summary["duration_s"] >= summary["overlap_s"]
    assert summary["frames"] >= 6
    assert summary["radar_reports"] >= 2
    assert summary["mean_fps"] > 0


def test_events(session_dir: Path) -> None:
    events_file = session_dir / "events.jsonl"
    with open(events_file, "a") as fh:
        fh.write('{"kind": "sta')
    events = open_session(session_dir).events()
    assert events and all(isinstance(e, dict) for e in events)


def test_crashed_copy_still_opens(session_dir: Path) -> None:
    for frames_csv in (session_dir / "video").glob("*.frames.csv"):
        frames_csv.unlink()
    meta = session_dir / "video" / "0001.meta.json"
    meta.write_text(meta.read_text()[:-40])  # truncated mid-object
    radar = session_dir / "radar" / "0001.csv"
    radar.write_text(radar.read_text() + "123456,f4f3f")  # power cut mid-line
    (session_dir / "video" / "0002.h264").write_bytes(b"")  # empty video, no metadata
    data = json.loads((session_dir / "session.json").read_text())
    data["segments"] = data["segments"][:1]
    (session_dir / "session.json").write_text(json.dumps(data))
    session = open_session(session_dir)
    frames = list(session.frames())
    assert {f.segment for f in frames} == {0, 1}
    assert len([r for r in session.radar_reports() if r.targets]) == 2
    assert session.summary()["frames"] == len(frames)


def test_unsupported_format_version(session_dir: Path) -> None:
    path = session_dir / "session.json"
    data = json.loads(path.read_text())
    data["format_version"] = 2
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="format_version"):
        open_session(session_dir)


def test_gaps_and_frame_at_tolerance_from_synthetic_session(tmp_path: Path) -> None:
    (tmp_path / "video").mkdir()
    (tmp_path / "session.json").write_text(
        json.dumps({"format_version": 1, "state": "complete", "devices": {"camera": None}})
    )
    ms = 1_000_000
    stamps = [0, 40 * ms, 80 * ms, 400 * ms, 440 * ms]
    rows = "".join(f"{i},{t},,,\n" for i, t in enumerate(stamps))
    (tmp_path / "video" / "0000.frames.csv").write_text(
        "index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux\n" + rows
    )
    session = open_session(tmp_path)
    summary = session.summary()
    assert summary["gaps"] == 1
    assert summary["frames"] == 5
    assert summary["mean_fps"] == pytest.approx(4 / 0.44)
    assert set(summary) == {
        "state",
        "duration_s",
        "frames",
        "mean_fps",
        "gaps",
        "radar_reports",
        "overlap_s",
    }
    assert summary["overlap_s"] == 0
    found = session.frame_at(60 * ms)
    assert found is not None and found.index in (1, 2)
    assert session.frame_at(200 * ms) is None


def test_mean_fps_zero_with_fewer_than_two_frames(tmp_path: Path) -> None:
    (tmp_path / "video").mkdir()
    (tmp_path / "session.json").write_text(json.dumps({"format_version": 1, "state": "complete"}))
    (tmp_path / "video" / "0000.frames.csv").write_text(
        "index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux\n0,5,,,\n"
    )
    summary = open_session(tmp_path).summary()
    assert summary["frames"] == 1
    assert summary["mean_fps"] == 0


EMPTY_REPORT = "f4f3f2f10000f8f7f6f5"
FRAMES_HEADER = "index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux\n"


def synthetic_session(root: Path) -> Path:
    (root / "radar").mkdir()
    (root / "video").mkdir()
    (root / "session.json").write_text(json.dumps({"format_version": 1, "state": "complete"}))
    return root


def test_report_split_across_segments_is_kept(tmp_path: Path) -> None:
    root = synthetic_session(tmp_path)
    (root / "radar" / "0000.csv").write_text(f"t_rx_ns,hex\n1000,{EMPTY_REPORT}\n2000,f4\n")
    (root / "radar" / "0001.csv").write_text(
        f"t_rx_ns,hex\n2001,{EMPTY_REPORT[2:]}\n3000,{EMPTY_REPORT}\n"
    )
    stamps = [r.t_rx_ns for r in open_session(root).radar_reports()]
    assert len(stamps) == 3
    assert stamps[0] == 1000 and stamps[-1] == 3000


def test_garbled_radar_line_skips_only_that_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = synthetic_session(tmp_path)
    (root / "radar" / "0000.csv").write_text(
        f"t_rx_ns,hex\n1000,{EMPTY_REPORT}\n1500,zz-not-hex\n3000,{EMPTY_REPORT}\n"
    )
    stamps = [r.t_rx_ns for r in open_session(root).radar_reports()]
    assert stamps == [1000, 3000]
    assert "0000.csv:3: garbled radar line" in caplog.text


def test_meta_with_more_frames_than_stale_csv_wins(tmp_path: Path) -> None:
    root = synthetic_session(tmp_path)
    (root / "video" / "0000.frames.csv").write_text(FRAMES_HEADER)  # stale, 0 rows
    (root / "video" / "0000.meta.json").write_text(
        '[\n{"SensorTimestamp": 10},\n{"SensorTimestamp": 50},\n{"SensorTimestamp": 9'
    )
    (root / "video" / "0001.frames.csv").write_text(FRAMES_HEADER + "0,100,,,\n1,140,,,\n")
    (root / "video" / "0001.meta.json").write_text('[\n{"SensorTimestamp": 100}')  # CSV wins
    frames = list(open_session(root).frames())
    assert [(f.segment, f.index, f.sensor_ts_ns) for f in frames] == [
        (0, 0, 10),
        (0, 1, 50),
        (1, 0, 100),
        (1, 1, 140),
    ]
