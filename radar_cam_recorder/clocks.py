"""Clock sampling: the common boot clock plus monotonic and wall-clock anchors."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

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
