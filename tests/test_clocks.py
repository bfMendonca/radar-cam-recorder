from __future__ import annotations

from pathlib import Path

from radar_cam_recorder.clocks import ClockSample, boot_clock, boot_id, ntp_synced, sample_clocks


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
