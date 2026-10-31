from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from radar_cam_recorder.clocks import ClockSample
from radar_cam_recorder.session import FORMAT_VERSION, SessionState, SessionStore
from radar_cam_recorder.status import State, StatusEvent

SAMPLE = ClockSample(1, 2, 3)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


def test_ids_increment_and_continue_across_stores(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.next_id() == "s000001"
    assert store.next_id() == "s000002"
    assert SessionStore(tmp_path).next_id() == "s000003"


def test_init_does_not_touch_disk(tmp_path: Path) -> None:
    SessionStore(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("text", ["garbage", "-5", "\xff\xfe"])
def test_corrupt_counter_heals_from_session_dirs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    for name in ("s000001", "s000002", "s000003", "notes"):
        (tmp_path / name).mkdir()
    (tmp_path / ".session_counter").write_bytes(text.encode("latin-1"))
    with caplog.at_level(logging.WARNING, logger="radar_cam_recorder"):
        assert SessionStore(tmp_path).next_id() == "s000004"
    assert (tmp_path / ".session_counter").read_text() == "4\n"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ".session_counter" in warnings[0].getMessage()
    assert SessionStore(tmp_path).next_id() == "s000005"


def test_missing_counter_continues_after_session_dirs(tmp_path: Path) -> None:
    (tmp_path / "s000007").mkdir()
    assert SessionStore(tmp_path).next_id() == "s000008"


def test_valid_counter_is_never_lowered(tmp_path: Path) -> None:
    (tmp_path / "s000002").mkdir()
    (tmp_path / ".session_counter").write_text("9\n")
    assert SessionStore(tmp_path).next_id() == "s000010"


def test_create_writes_session_json(tmp_path: Path) -> None:
    session = SessionStore(tmp_path).create({"a": 1}, "boot-x", SAMPLE)
    data = read_json(session.path / "session.json")
    assert session.id == "s000001"
    assert data["state"] == "in_progress"
    assert data["format_version"] == FORMAT_VERSION == 1
    assert data["session_id"] == "s000001"
    assert data["boot_id"] == "boot-x"
    assert data["config"] == {"a": 1}
    assert data["setup"] == {}
    assert data["devices"] == {}
    assert data["radar_latency_ns"] == 0
    assert data["ntp_synced"] is False
    assert data["rtc"] == {"present": False}
    assert data["wall_clock_source"] == "unsynced"
    assert data["segments"] == []
    assert data["stop_reason"] is None
    assert data["last_radar_t_rx_ns"] is None
    assert data["clock_samples"] == [{"label": "start", **SAMPLE.as_dict()}]


def test_create_never_reuses_existing_dir(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    (tmp_path / "s000001").mkdir()
    (tmp_path / ".session_counter").write_text("0\n")  # a valid counter that lags the dirs
    with pytest.raises(FileExistsError):
        store.create({}, "b", SAMPLE)


def test_segment_paths(tmp_path: Path) -> None:
    session = SessionStore(tmp_path).create({}, "b", SAMPLE)
    seg = session.segment(3)
    assert seg.index == 3
    assert seg.video.name == "0003.h264"
    assert seg.meta.name == "0003.meta.json"
    assert seg.pts.name == "0003.pts.txt"
    assert seg.frames.name == "0003.frames.csv"
    assert seg.camera_log.name == "0003.log"
    assert seg.radar == session.path / "radar" / "0003.csv"
    assert seg.video.parent == session.path / "video"
    assert seg.video.parent.is_dir()
    assert seg.radar.parent.is_dir()


def test_update_add_sample_and_add_segment(tmp_path: Path) -> None:
    session = SessionStore(tmp_path).create({}, "b", SAMPLE)
    session.update(setup={"h": 2}, ntp_synced=True)
    session.add_clock_sample("mid", ClockSample(4, 5, 6))
    session.add_segment(0, 10, 20, 250)
    data = read_json(session.path / "session.json")
    assert data["setup"] == {"h": 2}
    assert data["ntp_synced"] is True
    assert [s["label"] for s in data["clock_samples"]] == ["start", "mid"]
    assert data["segments"] == [
        {"index": 0, "start_boottime_ns": 10, "end_boottime_ns": 20, "frames": 250}
    ]
    assert not list(session.path.glob("*.tmp"))


def test_append_event_writes_json_lines(tmp_path: Path) -> None:
    session = SessionStore(tmp_path).create({}, "b", SAMPLE)
    session.append_event(StatusEvent("state", State.RECORDING, "ok", 1, {}))
    session.append_event(StatusEvent("incident", State.DEGRADED, "x", 2, {"k": 1}))
    lines = (session.path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["detail"] == {"k": 1}


def test_close_sets_state_reason_and_stop_sample(tmp_path: Path) -> None:
    session = SessionStore(tmp_path).create({}, "b", SAMPLE)
    session.close(SessionState.COMPLETE, "stopped by signal", ClockSample(7, 8, 9))
    data = read_json(session.path / "session.json")
    assert data["state"] == "complete"
    assert data["stop_reason"] == "stopped by signal"
    assert data["clock_samples"][-1] == {
        "label": "stop",
        "boottime_ns": 7,
        "monotonic_ns": 8,
        "realtime_ns": 9,
    }


def test_recovery_marks_interrupted_and_finds_last_radar_line(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    done = store.create({}, "b", SAMPLE)
    done.close(SessionState.COMPLETE, "stopped by signal", SAMPLE)
    (crashed.path / "radar").mkdir()
    (crashed.path / "radar" / "0000.csv").write_bytes(b"t_rx_ns,hex\n1,aa\n")
    (crashed.path / "radar" / "0001.csv").write_bytes(b"t_rx_ns,hex\n100,f4\n123,f4f3\n456,f2")
    done_before = (done.path / "session.json").read_bytes()

    assert SessionStore(tmp_path).recover_interrupted() == [crashed.id]

    data = read_json(crashed.path / "session.json")
    assert data["state"] == "interrupted"
    assert data["stop_reason"] == "interrupted (found at next start)"
    assert data["last_radar_t_rx_ns"] == 123
    assert (done.path / "session.json").read_bytes() == done_before
    assert (crashed.path / "radar" / "0001.csv").read_bytes().endswith(b"456,f2")


def test_recovery_without_radar_data_gives_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    (crashed.path / "radar").mkdir()
    (crashed.path / "radar" / "0000.csv").write_bytes(b"t_rx_ns,hex\n12,a")
    store.recover_interrupted()
    assert read_json(crashed.path / "session.json")["last_radar_t_rx_ns"] is None


def test_recovery_generates_missing_frames_csv(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    s0, s1 = crashed.segment(0), crashed.segment(1)
    s0.meta.write_text("{}")
    s1.meta.write_text("{}")
    s1.frames.write_text("existing")
    calls: list[tuple[Path, Path]] = []

    def writer(meta: Path, frames: Path) -> int:
        calls.append((meta, frames))
        frames.write_text("derived")
        return 0

    store.recover_interrupted(writer)
    assert calls == [(s0.meta, s0.frames)]
    assert s1.frames.read_text() == "existing"


def test_recovery_default_writer_derives_frames_csv(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    seg = crashed.segment(0)
    seg.meta.write_text('[\n{"SensorTimestamp": 5, "ExposureTime": 7},\n{"SensorTimestamp": 9, "Lu')
    store.recover_interrupted()
    assert seg.frames.read_text().splitlines() == [
        "index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux",
        "0,5,,7,",
    ]


def test_recovery_skips_unreadable_session_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SessionStore(tmp_path)
    good = store.create({}, "b", SAMPLE)
    bad = tmp_path / "s000099"
    bad.mkdir()
    (bad / "session.json").write_text("{trunc")
    (tmp_path / "not-a-session").mkdir()
    with caplog.at_level(logging.WARNING):
        assert store.recover_interrupted() == [good.id]
    assert "s000099" in caplog.text
    assert (bad / "session.json").read_text() == "{trunc"


def test_recovery_survives_failing_frames_writer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SessionStore(tmp_path)
    first = store.create({}, "b", SAMPLE)
    second = store.create({}, "b", SAMPLE)
    first.segment(0).meta.write_text("{trunc")
    second.segment(0).meta.write_text("{}")

    def writer(meta: Path, frames: Path) -> int:
        if meta.parent.parent == first.path:
            raise ValueError("truncated meta")
        frames.write_text("derived")
        return 0

    with caplog.at_level(logging.WARNING):
        assert store.recover_interrupted(writer) == [first.id, second.id]
    assert read_json(first.path / "session.json")["state"] == "interrupted"
    assert read_json(second.path / "session.json")["state"] == "interrupted"
    assert second.segment(0).frames.exists()
    assert "truncated meta" in caplog.text


def test_recovery_survives_unreadable_radar_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    (crashed.path / "radar").mkdir()
    (crashed.path / "radar" / "0000.csv").write_bytes(b"t_rx_ns,hex\n1,aa\n")

    def boom(self: Path) -> bytes:
        raise OSError("io error")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert store.recover_interrupted() == [crashed.id]
    data = read_json(crashed.path / "session.json")
    assert data["state"] == "interrupted"
    assert data["last_radar_t_rx_ns"] is None


def test_recovery_regenerates_stale_frames_csv(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    stale, current = crashed.segment(0), crashed.segment(1)
    header = "index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux\n"
    stale.frames.write_text(header)  # written by an earlier run of the reused segment
    stale.meta.write_text('[\n{"SensorTimestamp": 5},\n{"SensorTimestamp": 9},\n{"Sens')
    current.meta.write_text('[\n{"SensorTimestamp": 20}\n]\n')
    current.frames.write_text(header + "0,20,,,\n")
    current_before = current.frames.read_bytes()
    store.recover_interrupted()
    assert stale.frames.read_text().splitlines() == [header.strip(), "0,5,,,", "1,9,,,"]
    assert current.frames.read_bytes() == current_before


def test_recovery_heartbeats_per_segment_and_survives_failure(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    crashed = store.create({}, "b", SAMPLE)
    for n in range(3):
        crashed.segment(n).meta.write_text("{}")
        crashed.segment(n).frames.write_text("existing")  # skipped by the writer, still counted
    beats: list[int] = []

    def heartbeat() -> None:
        beats.append(1)
        raise RuntimeError("watchdog socket gone")

    assert store.recover_interrupted(heartbeat=heartbeat) == [crashed.id]
    assert len(beats) >= 3 + 1  # every segment, plus the session
