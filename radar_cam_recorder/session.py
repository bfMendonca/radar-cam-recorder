"""Session store: one self-describing folder per recording, safe against power cuts."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from radar_cam_recorder.clocks import ClockSample
from radar_cam_recorder.status import StatusEvent

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
SESSION_FILE = "session.json"
EVENTS_FILE = "events.jsonl"
COUNTER_FILE = ".session_counter"
INTERRUPTED_REASON = "interrupted (found at next start)"
_SESSION_DIR = re.compile(r"s(\d{6,})")

FramesWriter = Callable[[Path, Path], int]


class SessionState(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class SegmentPaths:
    index: int
    video: Path
    meta: Path
    pts: Path
    frames: Path
    camera_log: Path
    radar: Path


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a tmp file in the same dir, fsync and os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


class Session:
    def __init__(self, session_id: str, path: Path, data: dict[str, Any]) -> None:
        self.id = session_id
        self.path = path
        self._data = data
        self._lock = threading.Lock()

    def segment(self, index: int) -> SegmentPaths:
        video_dir = self.path / "video"
        radar_dir = self.path / "radar"
        video_dir.mkdir(exist_ok=True)
        radar_dir.mkdir(exist_ok=True)
        stem = f"{index:04d}"
        return SegmentPaths(
            index=index,
            video=video_dir / f"{stem}.h264",
            meta=video_dir / f"{stem}.meta.json",
            pts=video_dir / f"{stem}.pts.txt",
            frames=video_dir / f"{stem}.frames.csv",
            camera_log=video_dir / f"{stem}.log",
            radar=radar_dir / f"{stem}.csv",
        )

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._data.update(fields)
            self._save()

    def add_clock_sample(self, label: str, sample: ClockSample) -> None:
        with self._lock:
            self._data["clock_samples"].append({"label": label, **sample.as_dict()})
            self._save()

    def add_segment(
        self,
        index: int,
        start_boottime_ns: int,
        end_boottime_ns: int,
        frames: int,
        *,
        drops: int | None = None,
        max_gap_ns: int | None = None,
    ) -> None:
        """Record a closed segment; ``drops``/``max_gap_ns`` (camera only) are optional keys."""
        record: dict[str, Any] = {
            "index": index,
            "start_boottime_ns": start_boottime_ns,
            "end_boottime_ns": end_boottime_ns,
            "frames": frames,
        }
        if drops is not None:
            record["drops"] = drops
            record["max_gap_ns"] = max_gap_ns
        with self._lock:
            self._data["segments"].append(record)
            self._save()

    def append_event(self, event: StatusEvent) -> None:
        line = json.dumps(event.as_dict()) + "\n"
        with self._lock, open(self.path / EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def close(self, state: SessionState, reason: str, end: ClockSample) -> None:
        with self._lock:
            self._data["state"] = state.value
            self._data["stop_reason"] = reason
            self._data["clock_samples"].append({"label": "stop", **end.as_dict()})
            self._save()

    def _save(self) -> None:
        # Caller holds the lock.
        _write_json(self.path / SESSION_FILE, self._data)


class SessionStore:
    """Creates and recovers sessions under ``root``. Construction never touches the disk."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def next_id(self) -> str:
        """Return the next id and advance the counter; never reuses or lowers an id.

        A missing, unreadable or corrupt counter is healed from the highest session dir.
        """
        counter = self.root / COUNTER_FILE
        last, problem = _read_counter(counter)
        if last is None:
            last = _highest_session_number(self.root)
            if problem is not None or last > 0:
                log.warning(
                    "session counter %s %s; continuing after s%06d",
                    counter,
                    problem or "missing",
                    last,
                )
        _atomic_write(counter, f"{last + 1}\n")
        return f"s{last + 1:06d}"

    def create(self, config: Mapping[str, Any], boot_id: str, start: ClockSample) -> Session:
        session_id = self.next_id()
        path = self.root / session_id
        path.mkdir(exist_ok=False)
        data: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "session_id": session_id,
            "boot_id": boot_id,
            "state": SessionState.IN_PROGRESS.value,
            "config": dict(config),
            "setup": {},
            "devices": {},
            "radar_latency_ns": 0,
            "ntp_synced": False,
            "rtc": {"present": False},
            "wall_clock_source": "unsynced",
            "clock_samples": [{"label": "start", **start.as_dict()}],
            "segments": [],
            "stop_reason": None,
            "last_radar_t_rx_ns": None,
        }
        session = Session(session_id, path, data)
        with session._lock:
            session._save()
        return session

    def recover_interrupted(
        self,
        frames_writer: FramesWriter | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> list[str]:
        """Mark every ``in_progress`` session ``interrupted``.

        Missing ``frames.csv`` files, and stale ones with fewer rows than the metadata has
        frames, are derived from the metadata (default: ``write_frames_csv``); recorded data
        files are never modified. ``heartbeat`` (e.g. the systemd watchdog) is called once per
        session and once per segment; its failure never affects recovery.
        """
        if frames_writer is None:
            from radar_cam_recorder.camera.metadata import write_frames_csv

            frames_writer = write_frames_csv
        marked: list[str] = []
        for child in sorted(self.root.iterdir()):
            session_file = child / SESSION_FILE
            if not session_file.is_file():
                continue
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                log.warning("skipping unreadable session %s: %s", child.name, exc)
                continue
            if data.get("state") != SessionState.IN_PROGRESS.value:
                continue
            _beat(heartbeat)
            _write_missing_frames(child, frames_writer, heartbeat)
            data["state"] = SessionState.INTERRUPTED.value
            data["stop_reason"] = INTERRUPTED_REASON
            data["last_radar_t_rx_ns"] = _last_radar_t_rx_ns(child)
            _write_json(session_file, data)
            marked.append(child.name)
        return marked


def _read_counter(counter: Path) -> tuple[int | None, str | None]:
    """Return (value, None), or (None, problem); (None, None) if there is no counter yet."""
    try:
        text = counter.read_text()
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"unreadable ({exc})"
    try:
        value = int(text.strip())
    except ValueError:
        value = -1
    if value < 0:
        return None, f"corrupt ({text[:40]!r})"
    return value, None


def _highest_session_number(root: Path) -> int:
    numbers = [0]
    for child in root.iterdir():
        match = _SESSION_DIR.fullmatch(child.name)
        if match and child.is_dir():
            numbers.append(int(match.group(1)))
    return max(numbers)


def _beat(heartbeat: Callable[[], None] | None) -> None:
    if heartbeat is None:
        return
    try:
        heartbeat()
    except Exception:
        log.exception("recovery heartbeat failed")


def _write_missing_frames(
    session_dir: Path,
    frames_writer: FramesWriter,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    suffix = ".meta.json"
    for meta in sorted((session_dir / "video").glob(f"*{suffix}")):
        _beat(heartbeat)
        frames = meta.with_name(meta.name[: -len(suffix)] + ".frames.csv")
        try:
            if frames.exists() and not _frames_csv_stale(meta, frames):
                continue
            frames_writer(meta, frames)
        except Exception as exc:
            log.warning("cannot write frames for %s: %s", meta, exc)


def _frames_csv_stale(meta: Path, frames: Path) -> bool:
    """True if ``frames`` has fewer rows than ``meta`` has frames (e.g. a reused segment)."""
    from radar_cam_recorder.camera.metadata import parse_metadata

    with open(frames, encoding="utf-8", errors="replace") as fh:
        rows = max(0, sum(1 for line in fh if line.strip()) - 1)  # minus the header
    text = meta.read_text(encoding="utf-8", errors="replace")
    return sum(1 for obj in parse_metadata(text) if "SensorTimestamp" in obj) > rows


def _last_radar_t_rx_ns(session_dir: Path) -> int | None:
    files = sorted((session_dir / "radar").glob("*.csv"))
    if not files:
        return None
    try:
        raw = files[-1].read_bytes()
    except OSError as exc:
        log.warning("cannot read radar file %s: %s", files[-1], exc)
        return None
    # Everything after the last newline is a partial line; the first line is the header.
    lines = raw.split(b"\n")[:-1]
    for line in reversed(lines[1:]):
        try:
            return int(line.split(b",", 1)[0])
        except ValueError:
            continue
    return None
