"""Camera source protocols."""

from __future__ import annotations

from typing import Any, Protocol

from radar_cam_recorder.session import SegmentPaths


class CameraRun(Protocol):
    """One running segment recording."""

    @property
    def returncode(self) -> int | None:
        """``None`` while running, the exit status afterwards."""
        ...

    def alive(self) -> bool: ...

    def stop(self, timeout: float) -> None:
        """Stop gracefully, escalating after ``timeout`` s. Idempotent."""
        ...


class CameraSource(Protocol):
    def start(self, paths: SegmentPaths) -> CameraRun: ...

    def describe(self) -> dict[str, Any]:
        """JSON-serialisable description for ``session.json``."""
        ...
