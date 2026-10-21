from __future__ import annotations

import os
from pathlib import Path

import pytest

from radar_cam_recorder.storage import DiskPolicy, StorageCheck, check_storage


def test_existing_writable_dir_is_usable(tmp_path: Path) -> None:
    result = check_storage(tmp_path, require_mountpoint=False)
    assert result.usable
    assert result.free_bytes > 0
    assert result.reason == ""


def test_missing_dir(tmp_path: Path) -> None:
    result = check_storage(tmp_path / "nope", require_mountpoint=False)
    assert result == StorageCheck(False, 0, "recordings dir missing")


def test_file_instead_of_dir_is_missing(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    result = check_storage(f, require_mountpoint=False)
    assert not result.usable
    assert result.reason == "recordings dir missing"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_dir(tmp_path: Path) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        result = check_storage(d, require_mountpoint=False)
    finally:
        d.chmod(0o700)
    assert not result.usable
    assert result.reason == "recordings dir not writable"


def test_not_a_mount_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "ismount", lambda _p: False)
    result = check_storage(tmp_path, require_mountpoint=True)
    assert not result.usable
    assert result.reason == "recordings dir is not a mount point"


def test_mount_point_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    assert check_storage(tmp_path, require_mountpoint=True).usable


def test_disk_policy_hysteresis() -> None:
    p = DiskPolicy(100, 150)
    seq = (200, 120, 99, 120, 149, 150, 120, 99)
    assert [p.may_record(f) for f in seq] == [True, True, False, False, False, True, True, False]


def test_disk_policy_starts_refused_when_low() -> None:
    p = DiskPolicy(100, 150)
    assert [p.may_record(f) for f in (50, 120, 150)] == [False, False, True]


@pytest.mark.parametrize(("stop", "resume"), [(150, 100), (100, 100)])
def test_disk_policy_rejects_no_hysteresis(stop: int, resume: int) -> None:
    with pytest.raises(ValueError):
        DiskPolicy(stop, resume)
