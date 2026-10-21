"""Recordings-dir checks and the free-space policy (with hysteresis)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageCheck:
    usable: bool
    free_bytes: int
    reason: str = ""


def check_storage(path: Path, require_mountpoint: bool) -> StorageCheck:
    """Say whether ``path`` may be written to; never creates or touches it."""
    if not path.is_dir():
        return StorageCheck(False, 0, "recordings dir missing")
    free = shutil.disk_usage(path).free
    if require_mountpoint and not os.path.ismount(path):
        return StorageCheck(False, free, "recordings dir is not a mount point")
    if not os.access(path, os.W_OK):
        return StorageCheck(False, free, "recordings dir not writable")
    return StorageCheck(True, free, "")


class DiskPolicy:
    """Stop recording below ``stop_below``; resume only at ``resume_above`` or more."""

    def __init__(self, stop_below: int, resume_above: int) -> None:
        if resume_above <= stop_below:
            raise ValueError("resume_above must be greater than stop_below")
        self._stop_below = stop_below
        self._resume_above = resume_above
        self._allowed = True  # first value is judged against stop_below

    def may_record(self, free_bytes: int) -> bool:
        threshold = self._stop_below if self._allowed else self._resume_above
        self._allowed = free_bytes >= threshold
        return self._allowed
