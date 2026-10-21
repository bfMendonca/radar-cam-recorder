from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from radar_cam_recorder.camera.metadata import parse_metadata, write_frames_csv

PI_PROBE = """[
{
    "ExposureTime": 10812,
    "Lux": 823.283630,
    "FrameWallClock": 1792535726211187,
    "SensorTimestamp": 2208365156000
},
{
    "ExposureTime": 11029,
    "SensorTimestamp": 2208405133000,
    "FrameWallClock": 1792535726251164,
    "Lux": 801.1
}
]
"""

FIFTY = json.dumps([{"SensorTimestamp": i, "ExposureTime": i * 2} for i in range(50)], indent=4)


def test_parse_full_array() -> None:
    frames = parse_metadata(PI_PROBE)
    assert [f["SensorTimestamp"] for f in frames] == [2208365156000, 2208405133000]


def test_parse_truncated_tail() -> None:
    assert len(parse_metadata(PI_PROBE[:-40])) == 1


def test_parse_missing_closing_bracket() -> None:
    assert len(parse_metadata(PI_PROBE.rstrip().removesuffix("]"))) == 2


def test_parse_empty() -> None:
    assert parse_metadata("[") == []
    assert parse_metadata("") == []
    assert parse_metadata("garbage") == []


@given(st.integers(min_value=0, max_value=len(FIFTY)))
def test_any_prefix_yields_prefix_of_objects(cut: int) -> None:
    expected = json.loads(FIFTY)
    got = parse_metadata(FIFTY[:cut])
    assert got == expected[: len(got)]


def test_write_frames_csv(tmp_path: Path) -> None:
    meta = tmp_path / "m.json"
    frames = tmp_path / "f.csv"
    meta.write_text(
        '[{"SensorTimestamp": 1, "FrameWallClock": 2, "ExposureTime": 3, "Lux": 4.5},'
        '{"Foo": 1},{"SensorTimestamp": 6}]'
    )
    assert write_frames_csv(meta, frames) == 2
    assert frames.read_text().splitlines() == [
        "index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux",
        "0,1,2,3,4.5",
        "1,6,,,",
    ]
    assert not list(tmp_path.glob("*.tmp"))
