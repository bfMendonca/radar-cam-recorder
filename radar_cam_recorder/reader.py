"""Open a recorded session: frames, radar reports and events on the common boot clock."""

from __future__ import annotations

import bisect
import csv
import heapq
import json
import logging
import statistics
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ld2451
from ld2451.protocol import decode_report
from ld2451.recorder import parse_line

from radar_cam_recorder.camera.metadata import parse_metadata

__all__ = ["Frame", "RecordedSession", "open_session"]

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
DEFAULT_FRAME_INTERVAL_NS = 40_000_000
NS = 1_000_000_000


@dataclass(frozen=True)
class Frame:
    segment: int
    index: int
    sensor_ts_ns: int
    video: Path


def open_session(path: Path) -> RecordedSession:
    path = Path(path)
    metadata = json.loads((path / "session.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: session.json is not an object")
    if metadata.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"{path}: unsupported format_version {metadata.get('format_version')!r}")
    return RecordedSession(path, metadata)


def _segment_indexes(directory: Path, suffix: str) -> list[int]:
    indexes = []
    if directory.is_dir():
        for file in directory.iterdir():
            stem = file.name[: -len(suffix)] if file.name.endswith(suffix) else ""
            if stem.isdigit():
                indexes.append(int(stem))
    return sorted(indexes)


class RecordedSession:
    def __init__(self, path: Path, metadata: dict[str, Any]) -> None:
        self.path = path
        self.metadata = metadata
        self._frame_cache: list[Frame] | None = None
        self._lookup: tuple[list[int], list[int]] | None = None  # sorted ts, matching tolerances

    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        file = self.path / "events.jsonl"
        if not file.exists():
            return out
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # partial last line
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def frames(self) -> Iterator[Frame]:
        return iter(self._frames())

    def radar_reports(self) -> Iterator[ld2451.RadarReport]:
        radar_dir = self.path / "radar"
        for segment in _segment_indexes(radar_dir, ".csv"):
            yield from self._segment_reports(radar_dir / f"{segment:04d}.csv")

    def timeline(self) -> Iterator[Frame | ld2451.RadarReport]:
        latency = self._latency_ns()
        frames: Iterator[tuple[int, int, int, Frame | ld2451.RadarReport]] = (
            (f.sensor_ts_ns, 0, i, f) for i, f in enumerate(self._frames())
        )
        reports = ((r.t_rx_ns - latency, 1, i, r) for i, r in enumerate(self.radar_reports()))
        for _, _, _, item in heapq.merge(frames, reports, key=lambda e: e[:3]):
            yield item

    def frame_at(self, ts_ns: int) -> Frame | None:
        stamps, tolerances = self._frame_lookup()
        pos = bisect.bisect_left(stamps, ts_ns)
        best: int | None = None
        for i in (pos - 1, pos):
            if 0 <= i < len(stamps) and abs(stamps[i] - ts_ns) <= tolerances[i]:  # noqa: SIM102
                if best is None or abs(stamps[i] - ts_ns) < abs(stamps[best] - ts_ns):
                    best = i
        return self._sorted_frames()[best] if best is not None else None

    def summary(self) -> dict[str, Any]:
        frames = self._sorted_frames()
        frame_ts = [f.sensor_ts_ns for f in frames]
        latency = self._latency_ns()
        radar_ts = [r.t_rx_ns - latency for r in self.radar_reports()]
        both = frame_ts + radar_ts
        duration = (max(both) - min(both)) / NS if both else 0.0
        span = (frame_ts[-1] - frame_ts[0]) / NS if len(frame_ts) >= 2 else 0.0
        mean_fps = (len(frame_ts) - 1) / span if span > 0 else 0.0
        deltas = [b - a for a, b in zip(frame_ts, frame_ts[1:])]
        gaps = 0
        if deltas:
            limit = 2 * statistics.median(deltas)
            gaps = sum(1 for d in deltas if d > limit)
        overlap = 0.0
        if frame_ts and radar_ts:
            lo = max(frame_ts[0], min(radar_ts))
            hi = min(frame_ts[-1], max(radar_ts))
            overlap = max(0, hi - lo) / NS
        return {
            "state": self.metadata.get("state"),
            "duration_s": duration,
            "frames": len(frame_ts),
            "mean_fps": mean_fps,
            "gaps": gaps,
            "radar_reports": len(radar_ts),
            "overlap_s": overlap,
        }

    def _latency_ns(self) -> int:
        return int(self.metadata.get("radar_latency_ns") or 0)

    def _frames(self) -> list[Frame]:
        if self._frame_cache is None:
            video_dir = self.path / "video"
            segments = sorted(
                set(_segment_indexes(video_dir, ".frames.csv"))
                | set(_segment_indexes(video_dir, ".meta.json"))
                | set(_segment_indexes(video_dir, ".h264"))
            )
            frames: list[Frame] = []
            for segment in segments:
                frames.extend(self._segment_frames(segment))
            self._frame_cache = frames
        return self._frame_cache

    def _segment_frames(self, segment: int) -> list[Frame]:
        stem = self.path / "video" / f"{segment:04d}"
        video = stem.with_name(stem.name + ".h264")
        csv_path = stem.with_name(stem.name + ".frames.csv")
        meta_path = stem.with_name(stem.name + ".meta.json")
        stamps: list[tuple[int, int]] = []
        if csv_path.exists():
            with open(csv_path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        stamps.append((int(row["index"]), int(row["sensor_ts_ns"])))
                    except (KeyError, TypeError, ValueError):
                        continue  # damaged row
        elif meta_path.exists():
            text = meta_path.read_text(encoding="utf-8", errors="replace")
            ts = [o["SensorTimestamp"] for o in parse_metadata(text) if "SensorTimestamp" in o]
            stamps = list(enumerate(ts))
        return [Frame(segment, i, t, video) for i, t in stamps]

    def _sorted_frames(self) -> list[Frame]:
        self._frame_lookup()
        return self._sorted

    def _frame_lookup(self) -> tuple[list[int], list[int]]:
        if self._lookup is None:
            frames = sorted(self._frames(), key=lambda f: (f.sensor_ts_ns, f.segment, f.index))
            default = self._default_interval_ns()
            by_segment: dict[int, list[int]] = {}
            for f in frames:
                by_segment.setdefault(f.segment, []).append(f.sensor_ts_ns)
            interval: dict[int, int] = {}
            for segment, ts in by_segment.items():
                deltas = [b - a for a, b in zip(ts, ts[1:])]
                interval[segment] = int(statistics.median(deltas)) if deltas else default
            self._sorted = frames
            self._lookup = (
                [f.sensor_ts_ns for f in frames],
                [interval[f.segment] for f in frames],
            )
        return self._lookup

    def _default_interval_ns(self) -> int:
        devices = self.metadata.get("devices")
        camera = devices.get("camera") if isinstance(devices, dict) else None
        if isinstance(camera, dict):
            fps = camera.get("framerate", camera.get("fps"))
            if isinstance(fps, (int, float)) and fps > 0:
                return int(NS / fps)
        return DEFAULT_FRAME_INTERVAL_NS

    @staticmethod
    def _segment_reports(path: Path) -> Iterator[ld2451.RadarReport]:
        decoder = ld2451.FrameDecoder()
        with open(path, encoding="ascii", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                try:
                    record = parse_line(line)
                except ValueError:
                    log.warning("%s:%d: garbled radar line, ignoring the rest", path, lineno)
                    return
                if record is None:
                    continue
                t_rx_ns, data = record
                for frame in decoder.feed(data, t_rx_ns):
                    if frame.kind is not ld2451.FrameKind.DATA:
                        continue
                    try:
                        yield decode_report(frame.payload, frame.t_rx_ns, frame.raw)
                    except ld2451.FrameError:
                        continue
