from __future__ import annotations

from pathlib import Path

from radar_cam_recorder.clocks import (
    ClockSample,
    boot_clock,
    boot_id,
    ntp_synced,
    rtc_info,
    sample_clocks,
    wall_clock_source,
)


def test_sample_uses_given_boot_clock() -> None:
    s = sample_clocks(lambda: 5)
    assert s.boottime_ns == 5
    assert s.monotonic_ns > 0
    assert s.realtime_ns > 0


def test_successive_samples_non_decreasing() -> None:
    boot = boot_clock()
    a = sample_clocks(boot)
    b = sample_clocks(boot)
    assert b.boottime_ns >= a.boottime_ns
    assert b.monotonic_ns >= a.monotonic_ns


def test_as_dict_keys() -> None:
    assert ClockSample(1, 2, 3).as_dict() == {
        "boottime_ns": 1,
        "monotonic_ns": 2,
        "realtime_ns": 3,
    }


def test_boot_id_reads_and_strips(tmp_path: Path) -> None:
    p = tmp_path / "boot_id"
    p.write_text("abc\n")
    assert boot_id(p) == "abc"


def test_boot_id_missing(tmp_path: Path) -> None:
    assert boot_id(tmp_path / "nope") == "unknown"


def test_ntp_synced(tmp_path: Path) -> None:
    marker = tmp_path / "synchronized"
    assert ntp_synced(marker) is False
    marker.write_text("")
    assert ntp_synced(marker) is True


def _rtc(tmp_path: Path, since_epoch: int) -> Path:
    (tmp_path / "name").write_text("rtc-ds3231 1-0068\n")
    (tmp_path / "since_epoch").write_text(f"{since_epoch}\n")
    return tmp_path


def test_rtc_info_missing(tmp_path: Path) -> None:
    assert rtc_info(tmp_path / "nope") == {"present": False}


def test_rtc_info_unreadable_time(tmp_path: Path) -> None:
    (tmp_path / "name").write_text("rtc-ds3231\n")
    (tmp_path / "since_epoch").write_text("garbage\n")
    assert rtc_info(tmp_path) == {"present": False}


def test_rtc_info_reports_offset(tmp_path: Path) -> None:
    info = rtc_info(_rtc(tmp_path, 1_000), realtime_ns=999_500_000_000)
    assert info == {"present": True, "name": "rtc-ds3231", "since_epoch": 1_000, "offset_ms": 500}


def test_wall_clock_source() -> None:
    near = {"present": True, "offset_ms": -400}
    far = {"present": True, "offset_ms": 90_000}
    assert wall_clock_source(True, far) == "ntp"
    assert wall_clock_source(False, near) == "rtc"
    assert wall_clock_source(False, far) == "unsynced"
    assert wall_clock_source(False, {"present": False}) == "unsynced"
