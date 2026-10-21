"""Parsing rpicam-vid's JSON frame metadata, including files cut short by a power loss."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

HEADER = ("index", "sensor_ts_ns", "frame_wall_clock_us", "exposure_us", "lux")
_COLUMNS = ("SensorTimestamp", "FrameWallClock", "ExposureTime", "Lux")


def parse_metadata(text: str) -> list[dict[str, Any]]:
    """Return the complete frame objects of an rpicam metadata array.

    A truncated tail (or a missing closing ``]``) is ignored.
    """
    decoder = json.JSONDecoder()
    pos = _skip_ws(text, 0)
    if not text.startswith("[", pos):
        return []
    pos += 1
    frames: list[dict[str, Any]] = []
    while True:
        pos = _skip_ws(text, pos)
        if pos >= len(text) or text[pos] == "]":
            break
        if text[pos] == ",":
            pos += 1
            continue
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except ValueError:
            break
        if isinstance(obj, dict):
            frames.append(obj)
    return frames


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def write_frames_csv(meta_path: Path, frames_path: Path) -> int:
    """Write ``frames_path`` atomically from ``meta_path``; return the number of rows."""
    text = meta_path.read_text(encoding="utf-8", errors="replace")
    tmp = frames_path.with_name(frames_path.name + ".tmp")
    count = 0
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for obj in parse_metadata(text):
            if "SensorTimestamp" not in obj:
                continue
            writer.writerow([count, *(obj.get(key, "") for key in _COLUMNS)])
            count += 1
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, frames_path)
    return count
