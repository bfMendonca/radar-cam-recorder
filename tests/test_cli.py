from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from radar_cam_recorder.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "deploy" / "config.example.toml"


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "c.toml"
    (tmp_path / "rec").mkdir()  # the recorder never creates the recordings dir
    cfg.write_text(
        f"""
[recording]
segment_s = 1.0
status_file = "{tmp_path / "status.json"}"

[storage]
recordings_dir = "{tmp_path / "rec"}"
stop_below_mb = 1
resume_above_mb = 2
check_interval_s = 1.0
"""
    )
    return cfg


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("fake-run")
    cfg = _write_config(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "radar_cam_recorder.cli", "run", "--fake", "--config", str(cfg)],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Python start-up alone takes seconds on a Pi Zero: wait for the recorder, not a clock.
        deadline = time.monotonic() + 60
        while _status_state(tmp_path / "status.json") != "recording":
            assert proc.poll() is None, proc.communicate()[1]
            assert time.monotonic() < deadline, "recorder never reported recording"
            time.sleep(0.1)
        time.sleep(1.5)  # record a little
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    return cfg


def _status_state(path: Path) -> str | None:
    try:
        state = json.loads(path.read_text())["state"]
    except (OSError, ValueError, KeyError):
        return None  # not written yet (or replaced mid-read)
    return state if isinstance(state, str) else None


def test_check_config_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--config", str(EXAMPLE)]) == 0
    assert "storage" in json.loads(capsys.readouterr().out)


def test_bad_config_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[storage]\n")
    assert main(["check-config", "--config", str(bad)]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_run_fake_stops_cleanly_on_sigterm(recorded: Path) -> None:
    rec = recorded.parent / "rec"
    sessions = [p for p in rec.iterdir() if p.is_dir()]
    assert len(sessions) == 1
    assert json.loads((sessions[0] / "session.json").read_text())["state"] == "complete"
    status = json.loads((recorded.parent / "status.json").read_text())
    assert status["state"] == "stopped"


def test_inspect_and_status(recorded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    session = next(p for p in (recorded.parent / "rec").iterdir() if p.is_dir())
    assert main(["inspect", str(session)]) == 0
    assert "frames:" in capsys.readouterr().out
    assert main(["inspect", str(session), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["frames"] > 0
    assert main(["status", "--config", str(recorded)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"


def test_status_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path)
    assert main(["status", "--config", str(cfg)]) == 1
    assert "error: no status file at" in capsys.readouterr().err
