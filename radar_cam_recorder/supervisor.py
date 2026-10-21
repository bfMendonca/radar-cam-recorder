"""Supervisor: owns the session, rotates segments on the boot clock and stops in order.

It also applies the storage policy (spec 6.1) and keeps recording with whatever devices work,
retrying the others with backoff (spec 6.2). Nothing it does deletes a recording.
"""

from __future__ import annotations

import csv
import errno
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import ld2451

from radar_cam_recorder.camera.base import CameraRun, CameraSource
from radar_cam_recorder.camera.metadata import parse_metadata, write_frames_csv
from radar_cam_recorder.clocks import Clock, boot_clock, boot_id, ntp_synced, sample_clocks
from radar_cam_recorder.config import Config, config_to_dict
from radar_cam_recorder.radar import RadarStream, SegmentedRadarWriter
from radar_cam_recorder.session import SegmentPaths, Session, SessionState, SessionStore
from radar_cam_recorder.status import State, StatusBus, StatusEvent
from radar_cam_recorder.storage import DiskPolicy, StorageCheck, check_storage

__all__ = ["Supervisor"]

log = logging.getLogger(__name__)

T = TypeVar("T")

_CAMERA_LOG_LINES = 20


@dataclass
class _PendingGap:
    """A rotation whose video gap is reported once the new segment has a frame."""

    segment: SegmentPaths
    last_old_ts: int | None


class _Backoff:
    """Retry schedule for one device: ``initial``, doubling up to ``maximum`` (boot-clock ns)."""

    def __init__(self, initial_s: float, maximum_s: float) -> None:
        self._initial = int(initial_s * 1e9)
        self._maximum = int(maximum_s * 1e9)
        self._delay = self._initial
        self._next_ns = 0
        self.failing = False

    def fail(self, now: int) -> bool:
        """Record a failure; return True if it starts a new failure streak."""
        first = not self.failing
        self.failing = True
        self._next_ns = now + self._delay
        self._delay = min(self._delay * 2, self._maximum)
        return first

    def due(self, now: int) -> bool:
        return self.failing and now >= self._next_ns

    def reset(self) -> None:
        self.failing = False
        self._delay = self._initial


class Supervisor:
    def __init__(
        self,
        cfg: Config,
        *,
        bus: StatusBus,
        camera: CameraSource | None,
        radar: RadarStream | None,
        store: SessionStore | None = None,
        clock: Clock | None = None,
        storage_check: Callable[[Path, bool], StorageCheck] = check_storage,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._camera = camera if camera is not None and cfg.camera.enabled else None
        self._radar = radar if radar is not None and cfg.radar.enabled else None
        self._store = store if store is not None else SessionStore(cfg.storage.recordings_dir)
        self._clock = clock if clock is not None else boot_clock()
        self._storage_check = storage_check
        self._writer = SegmentedRadarWriter()
        self._session: Session | None = None
        self._segment: SegmentPaths | None = None
        self._segment_start_ns = 0
        self._next_check_ns = 0
        self._camera_run: CameraRun | None = None
        self._pending_gap: _PendingGap | None = None
        self._devices: dict[str, Any] = {}
        self._policy = DiskPolicy(cfg.storage.stop_below_bytes, cfg.storage.resume_above_bytes)
        self._out_of_space = False  # ENOSPC seen: wait for resume_above before recording again
        self._camera_retry = _Backoff(cfg.retry_initial_s, cfg.retry_max_s)
        self._radar_retry = _Backoff(cfg.retry_initial_s, cfg.retry_max_s)
        self._radar_started = False
        self._camera_up_since_ns: int | None = None  # start of the current run of good runs
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        bus.add_sink(self)

    @property
    def session(self) -> Session | None:
        return self._session

    def handle(self, event: StatusEvent) -> None:
        session = self._session
        if session is not None:
            session.append_event(event)

    def run(self, stop: threading.Event, tick_s: float = 0.5) -> None:
        try:
            self._recover()
            while not stop.is_set():
                self.tick()
                stop.wait(tick_s)
        finally:
            self.shutdown()

    def tick(self) -> None:
        """One supervision step; storage errors become states, never exceptions."""
        if self._shut_down:
            return
        now = self._clock()
        try:
            self._step(now)
        except OSError as exc:
            self._storage_failed(exc)

    def _step(self, now: int) -> None:
        if now >= self._next_check_ns:
            self._next_check_ns = now + int(self._cfg.storage.check_interval_s * 1e9)
            if not self._storage_allows_recording():
                return
            if self._session is None:
                self._try_open_session(now)
                return
            self._writer.flush()
        elif self._session is None:
            return
        error = self._writer.take_error()
        if error is not None:
            raise error
        if now - self._segment_start_ns >= self._segment_ns():
            self._rotate(now)
        elif self._pending_gap is not None:
            self._report_gap()
        self._check_camera(now)
        self._check_radar(now)
        self._update_state(now)

    def shutdown(self, reason: str = "stopped by signal") -> None:
        """Stop in order (spec 6.3); idempotent, bounded and never raises.

        Must not run concurrently with :meth:`tick`: signal handlers set the stop event and
        :meth:`run` calls this after the current tick.
        """
        with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
        try:
            self._bus.set_state(State.STOPPING, reason)
            if self._session is not None:
                self._close_session(SessionState.COMPLETE, reason)
        except Exception:
            log.exception("error during shutdown")
        finally:
            self._bus.set_state(State.STOPPED, reason)
            self._bus.close()

    # -- storage policy -------------------------------------------------------------------

    def _storage_allows_recording(self) -> bool:
        """Run the storage check; if recording must stop, close the session and say so."""
        check = self._check_storage()
        if not check.usable:
            self._stop_recording(State.STORAGE_MISSING, f"storage missing: {check.reason}")
            return False
        allowed = self._policy.may_record(check.free_bytes)
        if self._out_of_space:
            allowed = check.free_bytes >= self._cfg.storage.resume_above_bytes
            self._out_of_space = not allowed
        if not allowed:
            self._stop_recording(State.DISK_FULL, "disk full")
        return allowed

    def _storage_failed(self, exc: OSError) -> None:
        if exc.errno == errno.ENOSPC:
            self._out_of_space = True
            reason = "disk full"
            state = State.DISK_FULL
        else:
            reason = f"storage missing: recordings dir unavailable: {exc.strerror or exc}"
            state = State.STORAGE_MISSING
        changed = self._stop_recording(state, reason)
        # Once per streak: a write that keeps failing must not flood the SD card's log.
        log.log(
            logging.WARNING if changed else logging.DEBUG, "storage error (%s): %s", reason, exc
        )

    def _stop_recording(self, state: State, reason: str) -> bool:
        """Close the session (if any); return True if this changed the state."""
        changed = self._bus.set_state(state, reason)
        if self._session is None:
            return changed
        self._close_session(SessionState.COMPLETE, reason)
        self._session = None
        self._segment = None
        self._pending_gap = None
        self._radar_started = False
        self._camera_up_since_ns = None
        self._camera_retry.reset()
        self._radar_retry.reset()
        self._writer.take_error()  # belongs to the closed session
        return True

    # -- session and segments -------------------------------------------------------------

    def _recover(self) -> None:
        check = self._check_storage()
        if not check.usable:
            log.warning("skipping recovery: %s", check.reason)
            return
        for session_id in self._store.recover_interrupted():
            log.warning("session %s marked interrupted", session_id)

    def _check_storage(self) -> StorageCheck:
        storage = self._cfg.storage
        return self._storage_check(storage.recordings_dir, storage.require_mountpoint)

    def _try_open_session(self, now: int) -> None:
        """Open a session; an unexpected error is a state, retried at the next check."""
        try:
            self._open_session(now)
        except OSError:
            raise
        except Exception as exc:
            reason = f"error: {type(exc).__name__}: {exc}"
            changed = self._stop_recording(State.ERROR, reason)
            if changed:
                log.warning("cannot open a session: %s", reason, exc_info=True)
            else:
                log.debug("cannot open a session: %s", reason)

    def _open_session(self, now: int) -> None:
        session = self._store.create(
            config_to_dict(self._cfg), boot_id(), sample_clocks(boot=self._clock)
        )
        self._session = session
        segment = session.segment(0)
        # Set before any device starts, so a failed start still leaves a closable session.
        self._segment = segment
        self._segment_start_ns = now
        self._devices = {"radar": None, "camera": None}
        if self._camera is not None:
            self._devices["camera"] = self._camera.describe()
        if self._radar is not None:
            self._writer.open(segment.radar)
            self._start_radar(now, save=False)
        self._start_camera_segment(segment, now)
        session.update(
            devices=self._devices,
            setup={"notes": self._cfg.setup.notes, "geometry": dict(self._cfg.setup.geometry)},
            ntp_synced=ntp_synced(),
        )
        self._check_camera(now)
        self._update_state(now)

    def _start_camera_segment(self, segment: SegmentPaths, now: int) -> None:
        self._segment = segment
        self._segment_start_ns = now
        retry = self._camera_retry
        if not retry.failing or retry.due(now):
            self._start_camera(segment, now)

    def _start_camera(self, segment: SegmentPaths, now: int) -> None:
        retry = self._camera_retry
        if self._camera is None:
            return
        try:
            self._camera_run = self._camera.start(segment)
        except OSError as exc:  # e.g. the rpicam-vid binary is missing
            if exc.errno == errno.ENOSPC:
                raise  # creating the camera log: a storage error, not a camera one
            if retry.fail(now):
                self._bus.incident("camera start failed", segment=segment.index, error=str(exc))
            else:
                log.debug("camera start failed again: %s", exc)
            self._camera_up_since_ns = None
            return
        if self._camera_up_since_ns is None:
            self._camera_up_since_ns = now

    def _stop_camera(self) -> int:
        """Stop the camera run and write the segment's frames CSV; return its frame count."""
        run, self._camera_run = self._camera_run, None
        if run is not None:
            run.stop(self._cfg.camera.stop_timeout_s)
        segment = self._segment
        if segment is None or not segment.meta.exists():
            return 0
        return write_frames_csv(segment.meta, segment.frames)

    def _rotate(self, now: int) -> None:
        session, old = self._session, self._segment
        assert session is not None and old is not None
        frames = self._stop_camera()
        new = session.segment(old.index + 1)
        if self._radar is not None:
            self._writer.rotate(new.radar)
        start_ns = self._segment_start_ns
        self._start_camera_segment(new, now)
        session.add_segment(old.index, start_ns, now, frames)
        session.add_clock_sample(f"segment {new.index}", sample_clocks(boot=self._clock))
        self._pending_gap = _PendingGap(new, _last_sensor_ts(old.frames))

    def _report_gap(self) -> None:
        pending = self._pending_gap
        assert pending is not None
        if pending.last_old_ts is None:
            self._pending_gap = None
            return
        try:
            text = pending.segment.meta.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return  # no frame yet
        except OSError as exc:
            log.warning("cannot read %s, gap not reported: %s", pending.segment.meta, exc)
            self._pending_gap = None
            return
        frames = parse_metadata(text)
        first = next((f["SensorTimestamp"] for f in frames if "SensorTimestamp" in f), None)
        if first is None:
            return
        self._pending_gap = None
        self._bus.incident(
            "segment gap", segment=pending.segment.index, gap_ns=int(first) - pending.last_old_ts
        )

    # -- devices --------------------------------------------------------------------------

    def _check_camera(self, now: int) -> None:
        if self._camera is None:
            return
        retry, segment = self._camera_retry, self._segment
        if self._camera_run is None and retry.due(now) and segment is not None:
            if _has_recording(segment):
                self._rotate(now)  # never overwrite recorded video or frames
            else:
                self._start_camera(segment, now)  # only empty camera files are replaced
        run, segment = self._camera_run, self._segment
        if run is None or segment is None:
            return
        if run.alive():
            up_since = self._camera_up_since_ns
            if retry.failing and up_since is not None and now - up_since >= self._segment_ns():
                retry.reset()  # the camera survived a full segment
            return
        self._camera_up_since_ns = None
        if retry.fail(now):
            self._bus.incident(
                "camera exited",
                segment=segment.index,
                returncode=run.returncode,
                log=_tail(segment.camera_log, _CAMERA_LOG_LINES),
            )
        else:
            log.debug("camera exited again with %s", run.returncode)
        self._stop_camera()

    def _check_radar(self, now: int) -> None:
        if self._radar is None:
            return
        if not self._radar_started:
            if self._radar_retry.due(now):
                self._start_radar(now, save=True)
        elif not self._radar_silent(now):
            self._radar_retry.reset()

    def _start_radar(self, now: int, *, save: bool) -> None:
        assert self._radar is not None
        try:
            info = self._radar.start(self._writer)
        except (ld2451.LD2451Error, OSError) as exc:
            if self._radar_retry.fail(now):
                self._bus.incident("radar start failed", error=str(exc))
            else:
                log.debug("radar start failed again: %s", exc)
            return
        self._radar_started = True
        self._devices["radar"] = {"firmware": info.firmware, "params": info.params}
        if save and self._session is not None:
            self._session.update(devices=self._devices)

    def _radar_silent(self, now: int) -> bool:
        assert self._radar is not None
        timeout_ns = self._cfg.radar.silence_timeout_s * 1e9
        return self._radar.silent_for_ns(now, self._writer) > timeout_ns

    def _segment_ns(self) -> float:
        return self._cfg.segment_s * 1e9

    def _update_state(self, now: int) -> None:
        run = self._camera_run
        camera_missing = self._camera is not None and (run is None or not run.alive())
        radar_missing = self._radar is not None and (
            not self._radar_started or self._radar_silent(now)
        )
        if camera_missing and radar_missing:
            self._bus.set_state(State.WAITING_FOR_DEVICES, "camera and radar missing")
        elif camera_missing:
            self._bus.set_state(State.DEGRADED, "camera missing")
        elif radar_missing:
            self._bus.set_state(State.DEGRADED, "radar missing")
        else:
            self._bus.set_state(State.RECORDING, "recording")

    def _close_session(self, state: SessionState, reason: str) -> None:
        """Close everything in spec 6.3 order; a failing step is logged and the rest still run."""
        session, segment = self._session, self._segment
        if session is None:
            return
        frames = _attempt("stop camera", self._stop_camera, 0)
        if self._radar is not None:
            _attempt("stop radar", self._radar.stop, None)
        _attempt("close radar file", self._writer.close, None)
        end = self._clock()
        if segment is not None:
            _attempt(
                "record last segment",
                lambda: session.add_segment(segment.index, self._segment_start_ns, end, frames),
                None,
            )
        _attempt(
            "close session",
            lambda: session.close(state, reason, sample_clocks(boot=self._clock)),
            None,
        )


def _attempt(what: str, fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception:
        log.exception("cannot %s", what)
        return default


def _has_recording(segment: SegmentPaths) -> bool:
    """True unless the segment's video is absent or empty and its meta has no frame.

    Anything that cannot be checked counts as recorded, so it is never overwritten.
    """
    try:
        if segment.video.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    try:
        text = segment.meta.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(parse_metadata(text))


def _tail(path: Path, lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def _last_sensor_ts(frames_csv: Path) -> int | None:
    try:
        with open(frames_csv, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    if not rows:
        return None
    try:
        return int(rows[-1]["sensor_ts_ns"])
    except (KeyError, ValueError):
        return None
