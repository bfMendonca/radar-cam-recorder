from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import ld2451
import pytest

from radar_cam_recorder.config import ConfigError, config_to_dict, load_config, parse_config


def test_minimal_config_uses_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[storage]\nrecordings_dir = "/data/rec"\n')
    cfg = load_config(p)
    assert cfg.storage.recordings_dir == Path("/data/rec")
    assert cfg.storage.stop_below_bytes == 1024 * 2**20
    assert cfg.storage.resume_above_bytes == 1536 * 2**20
    assert cfg.segment_s == 300
    assert cfg.camera.framerate == 25
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
        {"storage": {"recordings_dir": "/x"}, "unknown": {}},
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
    load_config(Path("deploy/config.example.toml"))


def test_snapshot_is_json() -> None:
    json.dumps(config_to_dict(parse_config({"storage": {"recordings_dir": "/x"}})))
