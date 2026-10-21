"""Status bus: state changes and incidents, fanned out to sinks off the caller's thread."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from radar_cam_recorder.clocks import Clock

log = logging.getLogger(__name__)


class State(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    WAITING_FOR_DEVICES = "waiting_for_devices"
    RECORDING = "recording"
    DEGRADED = "degraded"
    DISK_FULL = "disk_full"
    STORAGE_MISSING = "storage_missing"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True)
class StatusEvent:
    kind: str  # "state" or "incident"
    state: State
    reason: str
    boottime_ns: int
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state.value,
            "reason": self.reason,
            "boottime_ns": self.boottime_ns,
            "detail": dict(self.detail),
        }


class StatusSink(Protocol):
    def handle(self, event: StatusEvent) -> None: ...


class StatusBus:
    """Never blocks producers: events go to a bounded queue read by one daemon thread."""

    def __init__(self, clock: Clock, queue_size: int = 256) -> None:
        self._clock = clock
        self._queue: queue.Queue[StatusEvent] = queue.Queue(maxsize=queue_size)
        self._sinks: list[StatusSink] = []
        self._lock = threading.Lock()
        self._dropped = 0
        self._closing = threading.Event()
        self._current = StatusEvent("state", State.STARTING, "starting", clock(), {})
        self._thread = threading.Thread(target=self._run, name="status-bus", daemon=True)
        self._thread.start()

    @property
    def current(self) -> StatusEvent:
        with self._lock:
            return self._current

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def add_sink(self, sink: StatusSink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def set_state(self, state: State, reason: str, **detail: Any) -> bool:
        with self._lock:
            cur = self._current
            if (cur.state, cur.reason) == (state, reason):
                return False
            event = StatusEvent("state", state, reason, self._clock(), detail)
            self._current = event
            self._enqueue(event)
        return True

    def announce(self) -> None:
        """Publish the current state again, e.g. the initial ``starting`` for the sinks."""
        with self._lock:
            self._enqueue(self._current)

    def incident(self, reason: str, **detail: Any) -> None:
        with self._lock:
            event = StatusEvent("incident", self._current.state, reason, self._clock(), detail)
            self._enqueue(event)

    def close(self, timeout: float = 2.0) -> None:
        """Deliver queued events (bounded by ``timeout``) and stop the dispatcher."""
        self._closing.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _enqueue(self, event: StatusEvent) -> None:
        # Caller holds the lock.
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def _run(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            with self._lock:
                sinks = list(self._sinks)
            for sink in sinks:
                try:
                    sink.handle(event)
                except Exception:
                    log.exception("status sink %r failed", sink)


class LogSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or log

    def handle(self, event: StatusEvent) -> None:
        level = logging.WARNING if event.kind == "incident" else logging.INFO
        self._log.log(
            level, "%s: %s (%s) %s", event.kind, event.state.value, event.reason, dict(event.detail)
        )


class StatusFileSink:
    """Writes the latest event as JSON, atomically; I/O errors are logged and ignored."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def handle(self, event: StatusEvent) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(event.as_dict()))
            os.replace(tmp, self._path)
        except OSError:
            log.exception("cannot write status file %s", self._path)
