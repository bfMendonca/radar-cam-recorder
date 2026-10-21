from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import ld2451
import pytest

from radar_cam_recorder.config import (
    ConfigError,
    ControlConfig,
    LedsConfig,
    config_to_dict,
    load_config,
    parse_config,
)


def minimal(**tables: Any) -> dict[str, Any]:
    return {"storage": {"recordings_dir": "/x"}, **tables}


def test_minimal_config_uses_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[storage]\nrecordings_dir = "/data/rec"\n')
    cfg = load_config(p)
    assert cfg.storage.recordings_dir == Path("/data/rec")
    assert cfg.storage.stop_below_bytes == 1024 * 2**20
    assert cfg.storage.resume_above_bytes == 1536 * 2**20
    assert cfg.segment_s == 300
    assert cfg.camera.framerate == 25
    assert cfg.camera.stall_timeout_s == 10.0
    assert cfg.radar.silence_timeout_s == 5
    assert cfg.radar.detection is None


def test_detection_and_geometry_parsed(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(
        '[storage]\nrecordings_dir = "/x"\n'
        '[radar.detection]\nmax_distance_m = 50\ndirection = "both"\n'
        "min_speed_kmh = 2\nno_target_delay_s = 3\n"
        '[setup]\nnotes = "pole"\n[setup.geometry]\nradar_height_m = 1.2\n'
    )
    cfg = load_config(p)
    assert cfg.radar.detection == ld2451.DetectionParams(50, ld2451.Direction.BOTH, 2, 3)
    assert cfg.setup.notes == "pole"
    assert dict(cfg.setup.geometry) == {"radar_height_m": 1.2}


@pytest.mark.parametrize(
    "bad",
    [
        {"storage": {}},
        {"storage": {"recordings_dir": "/x", "stop_below_mb": 2000, "resume_above_mb": 1000}},
        {"storage": {"recordings_dir": "/x"}, "recording": {"segment_s": 0}},
        {"storage": {"recordings_dir": "/x"}, "camera": {"framerate": 0}},
        {"storage": {"recordings_dir": "/x"}, "camera": {"stall_timeout_s": 0}},
        {"storage": {"recordings_dir": "/x"}, "camera": {"stall_timeout_s": -1.0}},
        {"storage": {"recordings_dir": "/x"}, "unknown": {}},
        {"storage": {"recordings_dir": ""}},
        {"storage": {"recordings_dir": "relative/rec"}},
        {"storage": {"recordings_dir": "/x"}, "recording": {"segment_s": float("inf")}},
        {"storage": {"recordings_dir": "/x"}, "recording": {"retry_max_s": float("nan")}},
        {"storage": {"recordings_dir": "/x", "check_interval_s": float("nan")}},
        {"storage": {"recordings_dir": "/x"}, "camera": {"framerate": float("inf")}},
        {"storage": {"recordings_dir": "/x"}, "camera": {"stop_timeout_s": float("nan")}},
        {"storage": {"recordings_dir": "/x"}, "radar": {"silence_timeout_s": float("inf")}},
        {"storage": {"recordings_dir": "/x"}, "setup": {"geometry": {"h_m": float("nan")}}},
        {
            "storage": {"recordings_dir": "/x"},
            "radar": {
                "detection": {
                    "max_distance_m": 50,
                    "direction": ["both"],
                    "min_speed_kmh": 1,
                    "no_target_delay_s": 1,
                }
            },
        },
        {"storage": {"recordings_dir": "/x", "typo": 1}},
        {
            "storage": {"recordings_dir": "/x"},
            "radar": {
                "detection": {
                    "max_distance_m": 5,
                    "direction": "both",
                    "min_speed_kmh": 1,
                    "no_target_delay_s": 1,
                }
            },
        },
    ],
)
def test_invalid_config_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_example_config_loads() -> None:
    cfg = load_config(Path("deploy/config.example.toml"))
    assert cfg.camera.stall_timeout_s == 10.0


def test_snapshot_is_json() -> None:
    cfg = parse_config({"storage": {"recordings_dir": "/x"}, "camera": {"stall_timeout_s": 7.5}})
    snapshot = config_to_dict(cfg)
    json.dumps(snapshot)
    assert snapshot["camera"]["stall_timeout_s"] == 7.5


def test_control_defaults() -> None:
    cfg = parse_config(minimal())
    assert cfg.control == ControlConfig("idle", "button@3", 0.4, 1.0, 5.0, 0.03)
    assert cfg.leds == LedsConfig(True, "rcr-green", "rcr-red")


def test_start_mode_record() -> None:
    assert parse_config(minimal(control={"start_mode": "record"})).control.start_mode == "record"


@pytest.mark.parametrize(
    "bad",
    [
        {"start_mode": "always"},
        {"double_press_s": 0},
        {"long_press_s": 1.0, "max_press_s": 1.0},  # long must exceed max short press
        {"debounce_s": 0.5},  # debounce must be below double_press_s
    ],
)
def test_control_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        parse_config(minimal(control=bad))


def test_snapshot_includes_control_and_leds() -> None:
    snapshot = config_to_dict(parse_config(minimal(leds={"enabled": False})))
    json.dumps(snapshot)
    assert snapshot["control"]["button_name"] == "button@3"
    assert snapshot["leds"] == {"enabled": False, "green": "rcr-green", "red": "rcr-red"}
