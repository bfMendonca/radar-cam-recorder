"""A fake camera that writes rpicam-shaped files, so everything runs without hardware."""

from __future__ import annotations

import json
import threading
import time
from typing import IO, Any

from radar_cam_recorder.clocks import Clock
from radar_cam_recorder.session import SegmentPaths

_FRAME_BYTES = 1024


class FakeRun:
    def __init__(self, clock: Clock, fps: float, paths: SegmentPaths | None) -> None:
        self._clock = clock
        self._period = 1.0 / fps
        self._paths = paths
        self._stop = threading.Event()
        self._returncode: int | None = None
        self._thread: threading.Thread | None = None
        if paths is None:
            self._returncode = 1
        else:
            self._thread = threading.Thread(target=self._run, name="fake-camera", daemon=True)
            self._thread.start()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def alive(self) -> bool:
        return self._returncode is None

    def stop(self, timeout: float) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        paths = self._paths
        assert paths is not None
        try:
            with (
                open(paths.video, "wb") as video,
                open(paths.pts, "w", encoding="utf-8") as pts,
                open(paths.meta, "w", encoding="utf-8") as meta,
            ):
                self._record(video, pts, meta)
            self._returncode = 0
        except OSError:
            self._returncode = 1

    def _record(self, video: IO[bytes], pts: IO[str], meta: IO[str]) -> None:
        pts.write("# timecode format v2\n")
        meta.write("[\n")
        start = time.monotonic()
        index = 0
        while True:
            frame = {
                "ExposureTime": 10000,
                "Lux": 500.0,
                "FrameWallClock": time.time_ns() // 1000,
                "SensorTimestamp": self._clock(),
            }
            video.write(b"\0" * _FRAME_BYTES)
            pts.write(f"{index * self._period * 1000:.3f}\n")
            meta.write(("" if index == 0 else ",\n") + json.dumps(frame, indent=4))
            for fh in (video, pts, meta):
                fh.flush()
            index += 1
            if self._stop.wait(max(0.0, start + index * self._period - time.monotonic())):
                break
        meta.write("\n]\n")


class FakeCamera:
    def __init__(self, clock: Clock, fps: float = 25.0, fail_starts: int = 0) -> None:
        self._clock = clock
        self._fps = fps
        self._fail_starts = fail_starts

    def start(self, paths: SegmentPaths) -> FakeRun:
        if self._fail_starts > 0:
            self._fail_starts -= 1
            return FakeRun(self._clock, self._fps, None)
        return FakeRun(self._clock, self._fps, paths)

    def describe(self) -> dict[str, Any]:
        return {"backend": "fake", "fps": self._fps}
