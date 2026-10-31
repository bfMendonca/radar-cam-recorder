"""Clock sampling: the common boot clock plus monotonic and wall-clock anchors."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ld2451.clock import default_clock

Clock = Callable[[], int]


@dataclass(frozen=True)
class ClockSample:
    boottime_ns: int
    monotonic_ns: int
    realtime_ns: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def boot_clock() -> Clock:
    """The driver's receive-time clock (CLOCK_BOOTTIME on Linux)."""
    return default_clock()[0]


def sample_clocks(boot: Clock | None = None) -> ClockSample:
    """Read boot, monotonic and wall clocks back to back."""
    boot_fn = boot if boot is not None else boot_clock()
    boottime = boot_fn()
    monotonic = time.monotonic_ns()
    realtime = time.time_ns()
    return ClockSample(boottime, monotonic, realtime)


def boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return "unknown"


def ntp_synced(marker: Path = Path("/run/systemd/timesync/synchronized")) -> bool:
    return marker.exists()


RTC_MAX_OFFSET_MS = 1000


def rtc_info(
    sysfs: Path = Path("/sys/class/rtc/rtc0"), realtime_ns: int | None = None
) -> dict[str, Any]:
    """The hardware RTC's driver name, time and offset from the system clock, if there is one."""
    try:
        name = (sysfs / "name").read_text().split()[0]
        since_epoch = int((sysfs / "since_epoch").read_text())
    except (OSError, ValueError, IndexError):
        return {"present": False}
    now_ns = time.time_ns() if realtime_ns is None else realtime_ns
    return {
        "present": True,
        "name": name,
        "since_epoch": since_epoch,
        "offset_ms": since_epoch * 1000 - now_ns // 1_000_000,
    }


def wall_clock_source(ntp: bool, rtc: dict[str, Any]) -> str:
    """ "ntp", "rtc" (RTC present and agreeing with the system clock) or "unsynced"."""
    if ntp:
        return "ntp"
    if rtc.get("present") and abs(rtc["offset_ms"]) <= RTC_MAX_OFFSET_MS:
        return "rtc"
    return "unsynced"
