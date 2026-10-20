# radar-cam-recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an unattended Raspberry Pi Zero W recorder that saves Camera v1.3 video
and LD2451 radar data in segmented, crash-safe session folders on a common clock, plus a
reader that opens those sessions on a Mac.

**Architecture:** Small single-purpose modules (config, clocks, storage, status,
session, radar, camera, supervisor, reader, cli). A `Supervisor` owns the session
lifecycle and is driven by `tick()`, so every behaviour is testable with fakes and a
manual clock. Video comes from one `rpicam-vid` subprocess per segment. Radar comes
from the sibling `ld2451` driver through its raw-read listener.

**Tech Stack:** Python ≥ 3.9, `uv`, `ld2451` (editable path dependency), `tomli` on
3.9/3.10, pytest, hypothesis, ruff, mypy `--strict`, systemd, `rpicam-vid` 1.7.

**Spec:** `docs/superpowers/specs/2026-10-20-radar-cam-recorder-design.md`

## Global Constraints

- Project root `~/workspace/radar-cam-recorder` (Mac and Pi); package `radar_cam_recorder/` (flat layout, like the driver); no files in `~/workspace` itself.
- `requires-python = ">=3.9"`; every module starts with `from __future__ import annotations`; no runtime use of 3.10+ features.
- Runtime dependencies only: `ld2451`, `tomli; python_version < "3.11"`. Driver dependency: `[tool.uv.sources] ld2451 = { path = "../driver-hlk-ld2451", editable = true }`.
- Dependency groups: `test` = pytest, hypothesis (default group; installs on ARMv6). `dev` = ruff, mypy, pytest-cov, types-pyserial (Mac only). `[tool.uv] default-groups = ["test"]`.
- Tooling settings copied from the driver: ruff `target-version = "py39"`, `line-length = 100`, `extend-exclude = ["*.md"]`, lint `select = ["E","F","W","I","B","UP","SIM","RUF"]`; mypy `python_version = "3.9"`, `strict = true`, `files = ["radar_cam_recorder", "tests"]`.
- Timebase: boot clock ns, via `ld2451.clock.default_clock()` (CLOCK_BOOTTIME on Linux, monotonic elsewhere). Never the wall clock for ordering, rotation or ids.
- Never delete recordings. Never write into a missing or unmounted recordings dir. Never leave the radar in configuration mode (use only the driver's high-level methods).
- Every gap or incident goes to the status bus and the session's `events.jsonl`.
- Library code never calls `print` or `sys.exit`, and never configures logging (logger `radar_cam_recorder` with a `NullHandler`); only `cli.py` does.
- Session format: `FORMAT_VERSION = 1`; layout per spec 6.6.
- Defaults: `segment_s = 300`, `stop_below_mb = 1024`, `resume_above_mb = 1536`, `check_interval_s = 5`, radar `silence_timeout_s = 5`, camera 1296×972 @ 25 fps, `bitrate = 4_000_000`, camera stop timeout 5 s, systemd `TimeoutStopSec=15`.

## Review Focus

1. Disk fills between two free-space checks, so a file write raises `OSError(ENOSPC)` → the recorder goes to `disk_full` and closes the session; it does not crash or restart-loop (Task 9).
2. `rpicam-vid` exits immediately at every start (camera missing or busy) → `degraded`, with backoff growing up to `retry_max_s`, one event per incident, not one per retry (no log flood on the SD card) (Task 9).
3. SIGTERM arrives during `waiting_for_devices`, during segment rotation, or before any session exists → a clean stop within 15 s, with no `in_progress` session left behind (Task 8).
4. A session left by a power cut (truncated `meta.json`, a partial last radar line) → the next start marks it `interrupted`, and the reader still opens it (Tasks 5, 7, 10).
5. The wall clock jumps (NTP, or a manual `date`) mid-session → segment rotation and session ids are unaffected, because both use the boot clock and the counter (Tasks 5, 8).

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `radar_cam_recorder/__init__.py`, `radar_cam_recorder/py.typed`, `radar_cam_recorder/config.py`, `deploy/config.example.toml`, `tests/__init__.py`, `tests/test_config.py`

**Interfaces:**
- Produces (`radar_cam_recorder/config.py`):
  - `class ConfigError(ValueError)`
  - frozen dataclasses:
    - `StorageConfig(recordings_dir: Path, require_mountpoint: bool, stop_below_bytes: int, resume_above_bytes: int, check_interval_s: float)`
    - `CameraConfig(enabled: bool, binary: str, width: int, height: int, framerate: float, bitrate: int, extra_args: tuple[str, ...], stop_timeout_s: float)`
    - `RadarConfig(enabled: bool, port: str, baudrate: int, silence_timeout_s: float, detection: ld2451.DetectionParams | None)`
    - `SetupConfig(notes: str, geometry: Mapping[str, float])`
    - `Config(storage, camera, radar, setup, segment_s: float, retry_initial_s: float, retry_max_s: float, status_file: Path)`
  - `parse_config(data: Mapping[str, Any]) -> Config`
  - `load_config(path: Path) -> Config`
  - `config_to_dict(cfg: Config) -> dict[str, Any]` (JSON-serialisable snapshot, for `session.json`).
- TOML layout:
  - `[recording] segment_s, retry_initial_s (1.0), retry_max_s (60.0), status_file ("/run/radar-cam-recorder/status.json")`
  - `[storage] recordings_dir (required), require_mountpoint (false), stop_below_mb, resume_above_mb, check_interval_s`
  - `[camera] enabled, binary ("rpicam-vid"), width, height, framerate, bitrate, extra_args ([]), stop_timeout_s`
  - `[radar] enabled, port ("/dev/serial0"), baudrate (115200), silence_timeout_s`
  - optional `[radar.detection] max_distance_m, direction ("away"|"approach"|"both"), min_speed_kmh, no_target_delay_s`
  - `[setup] notes (""), [setup.geometry] <name> = <float>`

- [ ] **Step 1: Write `pyproject.toml` and `.gitignore`.** Build backend setuptools, `packages = ["radar_cam_recorder"]`, package data `py.typed`, script `radar-cam-recorder = "radar_cam_recorder.cli:main"` (`cli.py` arrives in Task 11), plus the dependency groups, uv source and tooling from Global Constraints. pytest `testpaths = ["tests"]`, `addopts = "-ra"`, marker `pi: needs the Raspberry Pi camera/radar (skipped unless --pi)`. `.gitignore` is the same as the driver's.
- [ ] **Step 2: Run `uv sync --all-groups` and check that `uv run python -c "import ld2451"` exits 0.**
- [ ] **Step 3: Write the failing tests in `tests/test_config.py`:**

```python
def test_minimal_config_uses_defaults(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[storage]\nrecordings_dir = "/data/rec"\n')
    cfg = load_config(p)
    assert cfg.storage.recordings_dir == Path("/data/rec")
    assert cfg.storage.stop_below_bytes == 1024 * 2**20
    assert cfg.storage.resume_above_bytes == 1536 * 2**20
    assert cfg.segment_s == 300 and cfg.camera.framerate == 25 and cfg.radar.silence_timeout_s == 5
    assert cfg.radar.detection is None

def test_detection_and_geometry_parsed():  # [radar.detection] direction="both" -> Direction.BOTH; geometry {"radar_height_m": 1.2}

@pytest.mark.parametrize("bad", [
    {"storage": {}},                                                        # recordings_dir missing
    {"storage": {"recordings_dir": "/x", "stop_below_mb": 2000, "resume_above_mb": 1000}},
    {"storage": {"recordings_dir": "/x"}, "recording": {"segment_s": 0}},
    {"storage": {"recordings_dir": "/x"}, "camera": {"framerate": 0}},
    {"storage": {"recordings_dir": "/x"}, "unknown": {}},                   # unknown table
    {"storage": {"recordings_dir": "/x", "typo": 1}},                       # unknown key
    {"storage": {"recordings_dir": "/x"}, "radar": {"detection": {"max_distance_m": 5, "direction": "both", "min_speed_kmh": 1, "no_target_delay_s": 1}}},
])
def test_invalid_config_rejected(bad): with pytest.raises(ConfigError): parse_config(bad)

def test_example_config_loads(): load_config(Path("deploy/config.example.toml"))
def test_snapshot_is_json(): json.dumps(config_to_dict(parse_config({"storage": {"recordings_dir": "/x"}})))
```

- [ ] **Step 4: Run `uv run pytest tests/test_config.py -v`.** Expected: FAIL (module missing).
- [ ] **Step 5: Implement `config.py` and write `deploy/config.example.toml`** (every key, with its default and a one-line comment; `recordings_dir = "/home/bmendonca/recordings"`). Use `tomllib` on 3.11+, `tomli` otherwise. Unknown tables and keys raise `ConfigError` naming the key. Wrap the driver's `DetectionParams` `ValueError` in a `ConfigError`. `__init__.py` adds the `NullHandler` and `__version__ = "0.1.0"`.
- [ ] **Step 6: Run `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`.** Expected: all pass.
- [ ] **Step 7: Commit:** `git add -A && git commit -m "feat: project scaffold and config loading"`

### Task 2: Clocks

**Files:** Create `radar_cam_recorder/clocks.py`, `tests/test_clocks.py`

**Interfaces:**
- Produces:
  - `ClockSample(boottime_ns: int, monotonic_ns: int, realtime_ns: int)` (frozen), with `as_dict() -> dict[str, int]`
  - `Clock = Callable[[], int]`
  - `boot_clock() -> Clock` (returns `ld2451.clock.default_clock()[0]`)
  - `sample_clocks(boot: Clock | None = None) -> ClockSample`
  - `boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str` (`"unknown"` if unreadable)
  - `ntp_synced(marker: Path = Path("/run/systemd/timesync/synchronized")) -> bool`

- [ ] **Step 1: Write the failing tests:**
  - `sample_clocks(lambda: 5)` returns `boottime_ns == 5` and `monotonic_ns > 0`;
  - successive samples are non-decreasing;
  - `boot_id(tmp file "abc\n") == "abc"`; a missing path gives `"unknown"`;
  - `ntp_synced(missing) is False`; an existing file gives `True`.
- [ ] **Step 2: Run `uv run pytest tests/test_clocks.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement.** Read the three clocks back to back: boot first, then `time.monotonic_ns()`, then `time.time_ns()`.
- [ ] **Step 4: Run `uv run pytest tests/test_clocks.py -v`.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: clock sampling`

### Task 3: Storage checks and disk policy

**Files:** Create `radar_cam_recorder/storage.py`, `tests/test_storage.py`

**Interfaces:**
- Produces:
  - `StorageCheck(usable: bool, free_bytes: int, reason: str)` (frozen; `reason == ""` when usable)
  - `check_storage(path: Path, require_mountpoint: bool) -> StorageCheck`
  - `class DiskPolicy(stop_below: int, resume_above: int)` with `may_record(free_bytes: int) -> bool`
- Reasons are exact strings: `"recordings dir missing"`, `"recordings dir not writable"`, `"recordings dir is not a mount point"`.

- [ ] **Step 1: Write the failing tests:**
  - an existing writable tmp dir is usable with `free_bytes > 0`;
  - a missing dir gives the "missing" reason;
  - a chmod 0o500 dir gives the "not writable" reason (skip when running as root);
  - `require_mountpoint=True` with `os.path.ismount` monkeypatched to False gives the "not a mount point" reason;
  - DiskPolicy(100, 150):
    ```python
    assert [p.may_record(f) for f in (200, 120, 99, 120, 149, 150, 120, 99)] == [True, True, False, False, False, True, True, False]
    ```
  - `DiskPolicy(150, 100)` raises `ValueError`.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Use `shutil.disk_usage(path).free` and `os.access(path, os.W_OK)`. The policy starts in the "may record" state when the first value is ≥ `stop_below`.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: storage checks and disk policy`

### Task 4: Status bus and sinks

**Files:** Create `radar_cam_recorder/status.py`, `tests/test_status.py`

**Interfaces:**
- Produces:
  - `class State(str, Enum)`: `STARTING="starting"`, `WAITING_FOR_DEVICES="waiting_for_devices"`, `RECORDING="recording"`, `DEGRADED="degraded"`, `DISK_FULL="disk_full"`, `STORAGE_MISSING="storage_missing"`, `STOPPING="stopping"`, `STOPPED="stopped"`, `ERROR="error"`
  - `StatusEvent(kind: str, state: State, reason: str, boottime_ns: int, detail: Mapping[str, Any])`, where `kind` is `"state"` or `"incident"`; with `as_dict()`
  - `class StatusSink(Protocol): def handle(self, event: StatusEvent) -> None`
  - `class StatusBus(clock: Clock, queue_size: int = 256)` with:
    - `add_sink(sink)`
    - `set_state(state, reason, **detail) -> bool`: publishes only when `(state, reason)` changes; returns True if published
    - `incident(reason, **detail)`: always published, with the current state
    - `current -> StatusEvent`
    - `dropped -> int`
    - `close(timeout: float = 2.0)`
  - `LogSink(logger: logging.Logger | None = None)`
  - `StatusFileSink(path: Path)`: atomic JSON write (tmp + `os.replace`); creates parent dirs; errors are logged and ignored.

- [ ] **Step 1: Write the failing tests:**
  - `set_state` twice with the same state and reason publishes once, and with a new reason publishes again;
  - after `close()`, sinks received events in order;
  - a sink that sleeps 1 s doesn't block: 10 `set_state` calls return in < 0.1 s;
  - a sink raising `RuntimeError` doesn't stop the other sinks;
  - with `queue_size=2` and a blocked sink, `dropped > 0`;
  - `StatusFileSink` writes `{"state": "recording", "reason": ..., "boottime_ns": ..., "detail": {...}}`;
  - when its directory can't be created, `StatusFileSink` doesn't raise.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** One daemon dispatcher thread reads a bounded `queue.Queue`. `put_nowait` and counting drops keep producers from ever blocking. Each sink call is wrapped in try/except with `log.exception`.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: status bus with log and file sinks`

### Task 5: Session store

**Files:** Create `radar_cam_recorder/session.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: `ClockSample` (Task 2), `StatusEvent` (Task 4)
- Produces:
  - `FORMAT_VERSION = 1`
  - `class SessionState(str, Enum)`: `IN_PROGRESS="in_progress"`, `COMPLETE="complete"`, `INTERRUPTED="interrupted"`
  - `SegmentPaths(index: int, video: Path, meta: Path, pts: Path, frames: Path, camera_log: Path, radar: Path)` (frozen). File names are `video/{i:04d}.h264`, `.meta.json`, `.pts.txt`, `.frames.csv`, `.log`, and `radar/{i:04d}.csv`.
  - `class SessionStore(root: Path)`:
    - `next_id() -> str`: `"s000001"`, …; counter in `root/.session_counter`, written tmp + fsync + `os.replace`
    - `create(config: Mapping[str, Any], boot_id: str, start: ClockSample) -> Session`
    - `recover_interrupted() -> list[str]`
  - `class Session`:
    - attributes `id: str`, `path: Path`
    - `segment(index: int) -> SegmentPaths` (creates the `video/` and `radar/` dirs)
    - `update(**fields: Any)`: merge and atomic-rewrite `session.json`
    - `add_clock_sample(label: str, sample: ClockSample)`
    - `add_segment(index: int, start_boottime_ns: int, end_boottime_ns: int, frames: int)`
    - `append_event(event: StatusEvent)`: one JSON line, flush + fsync
    - `close(state: SessionState, reason: str, end: ClockSample)`
- `session.json` keys:
  - `format_version`, `session_id`, `boot_id`, `state`
  - `config` (snapshot), `setup`, `devices` (dict)
  - `radar_latency_ns` (0), `ntp_synced`
  - `clock_samples` (list of `{label, boottime_ns, monotonic_ns, realtime_ns}`), `segments` (list)
  - `stop_reason`, `last_radar_t_rx_ns`
- Recovery: every `in_progress` session becomes `interrupted`, with `stop_reason = "interrupted (found at next start)"`. `last_radar_t_rx_ns` comes from the last complete line of the highest `radar/*.csv`, and every `video/*.meta.json` without a `.frames.csv` gets one through the injected `frames_writer` (signature `recover_interrupted(frames_writer: Callable[[Path, Path], int] | None = None)`; `None` skips the step, and Task 7 makes `write_frames_csv` the default). Data files are never modified; the frames CSV is a new derived file.

- [ ] **Step 1: Write the failing tests:**
  - ids are `s000001`, `s000002`, and a new `SessionStore` on the same root continues at `s000003`;
  - a corrupt counter file raises a clear `ValueError` naming the file (it never resets to 1, which could overwrite sessions);
  - after `create`, `session.json` has `state == "in_progress"`, `format_version == 1` and one clock sample labelled `"start"`;
  - `segment(3).video.name == "0003.h264"`;
  - `append_event` twice gives 2 lines of valid JSON;
  - `close(COMPLETE, "stopped by signal", sample)` sets the state, the reason and a `"stop"` clock sample;
  - recovery: a crashed session (state left `in_progress`) whose radar file ends with `"123,f4f3\n456,f2"` gets state `interrupted` and `last_radar_t_rx_ns == 123`, while a `complete` session is untouched;
  - a session dir is never reused when it already exists (create raises `FileExistsError`).
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** All JSON writes are atomic (tmp in the same dir, fsync, `os.replace`).
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: session store with crash recovery`

### Task 6: Radar stream

**Files:** Create `radar_cam_recorder/radar.py`, `tests/test_radar.py`

**Interfaces:**
- Consumes: `RadarConfig` (Task 1); `ld2451.LD2451`, `ld2451.Recorder`, `ld2451.Transport`, `ld2451.SerialTransport`, `ld2451.LoopbackTransport`
- Produces:
  - `class SegmentedRadarWriter`:
    - `open(path: Path)` and `rotate(path: Path)`: switch files under a lock, closing the old one with flush + fsync
    - `write(t_rx_ns: int, data: bytes)`: the raw-listener signature
    - `flush()`, `close()`
    - `last_rx_ns -> int | None`
    - `reads -> int`
  - `RadarInfo(firmware: str | None, params: dict[str, Any] | None)` (frozen)
  - `class RadarStream(cfg: RadarConfig, transport_factory: Callable[[], Transport] | None = None)`. The default factory is `SerialTransport(cfg.port, cfg.baudrate)`. Methods:
    - `start(writer: SegmentedRadarWriter) -> RadarInfo`: build the `LD2451`, add the writer as a raw listener before `open()`/`start()`, apply `cfg.detection` with `set_params(verify=True)` if set, read `get_firmware()` and `get_params()`, return the info. Raises `ld2451.LD2451Error` and `OSError` unchanged.
    - `stop()`: `LD2451.close()`
    - `running -> bool`
    - `silent_for_ns(now_ns: int, writer: SegmentedRadarWriter) -> int`: `now - last_rx_ns`, or `now - started_ns` if nothing was received yet.

- [ ] **Step 1: Write the failing tests**, using the driver's test idea: a `LoopbackTransport` with a responder that ACKs the commands (copy `FakeRadar` from `driver-hlk-ld2451/tests/conftest.py` into `tests/fake_radar.py`).
  - Injected frames end up in the segment file in `t_rx_ns,hex` format, with the driver's header line.
  - `rotate` while a writer thread calls `write` 10 000 times: the lines of both files add up to exactly 10 000 data lines, with no duplicates.
  - `start` returns `firmware == "V1.01.24051510"`.
  - With `detection` set, the fake's params bytes equal the encoded params.
  - `silent_for_ns` grows while nothing arrives and drops after an inject.
  - `start` with a factory whose `open` raises `ld2451.TransportError` propagates it.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** The writer wraps one `ld2451.Recorder` per file, so the format stays shared with the driver.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: segmented radar stream`

### Task 7: Camera sources

**Files:**
- Create: `radar_cam_recorder/camera/__init__.py`, `camera/base.py`, `camera/metadata.py`, `camera/rpicam.py`, `camera/fake.py`
- Tests: `tests/fake_rpicam.py` (executable test double), `tests/test_camera_metadata.py`, `tests/test_camera.py`

**Interfaces:**
- Consumes: `CameraConfig` (Task 1), `SegmentPaths` (Task 5)
- Produces:
  - `metadata.parse_metadata(text: str) -> list[dict[str, Any]]`. It tolerates a truncated tail and a missing closing `]`; it uses `json.JSONDecoder().raw_decode` after `[` and `,`.
  - `metadata.write_frames_csv(meta_path: Path, frames_path: Path) -> int`. Columns: `index,sensor_ts_ns,frame_wall_clock_us,exposure_us,lux`. Returns the frame count.
  - `class CameraRun(Protocol)`:
    - `alive() -> bool`
    - `returncode -> int | None`
    - `stop(timeout: float) -> None`
  - `class CameraSource(Protocol)`:
    - `start(paths: SegmentPaths) -> CameraRun`
    - `describe() -> dict[str, Any]`
  - `rpicam.build_command(cfg: CameraConfig, paths: SegmentPaths) -> list[str]`, giving exactly: `[cfg.binary, "-n", "-t", "0", "--width", W, "--height", H, "--framerate", F, "--bitrate", B, "--codec", "h264", "--inline", "-o", video, "--save-pts", pts, "--metadata", meta, "--metadata-format", "json", *cfg.extra_args]`
  - `class RpicamCamera(cfg: CameraConfig)`: implements `CameraSource`. It uses `subprocess.Popen` with stdout and stderr going to `paths.camera_log`. `stop` sends SIGINT, waits up to the timeout, then sends SIGKILL.
  - `class FakeCamera(clock: Clock, fps: float = 25.0, fail_starts: int = 0)`: implements `CameraSource`. A thread writes 1 kB of fake video per frame plus the metadata JSON in rpicam's format, with `SensorTimestamp = clock()`. The first `fail_starts` runs die immediately with returncode 1.

- [ ] **Step 1: Write the failing metadata tests.**
  - `parse_metadata` handles a full array (fixture copied from the Pi probe: 2 objects with `SensorTimestamp` 2208365156000 and 2208405133000).
  - The same array with its last 40 characters cut off returns 1 object.
  - `"["` and `""` return `[]`.
  - Hypothesis: any prefix of a valid 50-object array returns a prefix of those objects and never raises.
  - `write_frames_csv` writes a header and one row per frame, and returns the count.
- [ ] **Step 2: Write the failing camera tests.**
  - `build_command` gives the exact list above.
  - `RpicamCamera` with `binary = sys.executable` and `extra_args` pointing at `tests/fake_rpicam.py` (or `binary` = that script, made executable): after 0.5 s, `alive()` is true and `meta` is non-empty. `stop(5)` returns, the returncode is 0, and `parse_metadata` gives ≥ 5 frames.
  - The `FAKE_RPICAM_MODE=exit1` environment makes `alive()` false within 0.5 s, with returncode 1.
  - `FAKE_RPICAM_MODE=ignore_sigint` makes `stop(0.5)` fall back to a kill and return within 1.5 s.
  - `FakeCamera(fail_starts=1)`: the first run dies and the second runs.
- [ ] **Step 3: Run `uv run pytest tests/test_camera_metadata.py tests/test_camera.py -v`.** Expected: FAIL.
- [ ] **Step 4: Implement.** `fake_rpicam.py` parses the same flags, appends a frame every 40 ms until SIGINT, then closes the JSON array and exits 0.
- [ ] **Step 5: Run the tests.** Expected: PASS. Wire `write_frames_csv` into `SessionStore.recover_interrupted(frames_writer=...)` (the default becomes `write_frames_csv`), and extend the Task 5 recovery test to assert that the frames CSV now exists.
- [ ] **Step 6: Commit:** `feat: rpicam and fake camera sources`

### Task 8: Supervisor — session lifecycle, segments, shutdown

**Files:** Create `radar_cam_recorder/supervisor.py`, `tests/test_supervisor.py`

**Interfaces:**
- Consumes: Tasks 1–7
- Produces:
  - `class Supervisor` with constructor `(cfg: Config, *, bus: StatusBus, camera: CameraSource | None, radar: RadarStream | None, store: SessionStore | None = None, clock: Clock | None = None, storage_check: Callable[[Path, bool], StorageCheck] = check_storage)`
    - `tick() -> None`: one iteration; never blocks for long
    - `run(stop: threading.Event, tick_s: float = 0.5) -> None`: recover interrupted sessions, then loop `tick()` until `stop` is set, then `shutdown()`
    - `shutdown(reason: str = "stopped by signal") -> None`: idempotent
    - `session -> Session | None`
  - It is also a `StatusSink`: `handle(event)` appends to the current session's `events.jsonl`.

Behaviour for this task (storage and recovery are Task 9):
- First tick, with storage usable: create a session and record `devices` (radar `RadarInfo`, `camera.describe()`), `setup` (`cfg.setup.notes` and `geometry`), a `"start"` clock sample, and `ntp_synced`.
- Every `check_interval_s`, call `writer.flush()` (spec 6.4: data flushed at least every few seconds). Start the radar writer on segment 0, then the camera on segment 0. State `RECORDING`, reason `"recording"`.
- Rotation: when `clock() - segment_start_ns >= segment_s * 1e9`:
  1. stop the camera run;
  2. `write_frames_csv`;
  3. `writer.rotate(next.radar)`;
  4. start the camera on the next segment;
  5. `add_segment(...)`, plus a `"segment N"` clock sample;
  6. incident `"segment gap"` with `gap_ns` = first new frame time minus last old frame time, filled on the next tick when it's known.
- Shutdown order (spec 6.3): `STOPPING`, then camera stop (`cfg.camera.stop_timeout_s`), frames CSV, `radar.stop()`, `writer.close()`, `session.close(COMPLETE, reason, sample)`, then `STOPPED` and `bus.close()`. The total time stays below 15 s, even when the camera ignores SIGINT.

- [ ] **Step 1: Write the failing tests** (manual clock `[0]`; `FakeCamera`; `RadarStream` over the loopback fake; `cfg` with `segment_s=1`):
  - `test_first_tick_opens_session_and_records`: state `recording`; `session.json` state is `in_progress`; `devices.radar.firmware` is set; `setup.notes` equals the config's notes.
  - `test_radar_flushed_periodically`: after an inject and a tick past `check_interval_s`, the radar segment file contains the line without closing the writer.
  - `test_rotation_creates_next_segment`: after the clock advances past 1 s, `0000.frames.csv` exists, `0001.h264` grows, and `segments[0]` is recorded.
  - `test_radar_not_interrupted_by_rotation`: frames injected before and after the rotation land in `radar/0000.csv` and `radar/0001.csv`, none lost.
  - `test_shutdown_completes_session`: state `complete`, a `"stop"` clock sample, and `events.jsonl` containing `stopping`.
  - `test_shutdown_before_any_session`: storage check unusable on the first tick, then `shutdown()`. It doesn't raise, and the store has no sessions.
  - `test_shutdown_with_camera_ignoring_sigint` (`RpicamCamera` + fake script `ignore_sigint`, `stop_timeout_s=0.5`): `shutdown()` takes < 3 s.
  - `test_wall_clock_jump_does_not_rotate`: monkeypatch `time.time_ns` to jump forward 1 h. No rotation happens while the boot clock stays put (Review Focus 5).
  - `test_run_stops_on_event`: `run()` in a thread, `stop.set()` after 0.3 s, and the thread joins within 5 s.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement** `tick`, `run` and `shutdown` as specified. Keep camera and radar handling in small private methods (`_start_camera_segment`, `_rotate`, `_close_session`) that Task 9 extends.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: supervisor session lifecycle and segments`

### Task 9: Supervisor — storage policy, degraded mode, recovery

**Files:** Modify `radar_cam_recorder/supervisor.py`; Test: `tests/test_supervisor_failures.py`

**Interfaces:**
- Consumes: `DiskPolicy` and `StorageCheck` (Task 3), plus Task 8's private methods
- Produces: no new public names. Reason strings (exact, used in tests and docs): `"disk full"`, `"storage missing: <StorageCheck.reason>"`, `"camera missing"`, `"radar missing"`, `"camera and radar missing"`.

Behaviour (spec 6.1, 6.2):
- Every `check_interval_s`, run `storage_check`.
  - Unusable: close the session `COMPLETE` with the reason; state `STORAGE_MISSING`.
  - `DiskPolicy` refuses: close the session `COMPLETE` with reason `"disk full"`; state `DISK_FULL`.
  - Recovery: the next tick that allows recording opens a **new** session.
- `OSError` with `errno.ENOSPC` from any write: treated as disk full immediately (Review Focus 1). Any other `OSError` from the recordings dir: treated as storage missing.
- Camera run dead (`not alive()`): incident `"camera exited"` with the returncode and the last 20 lines of `camera_log`, then frames CSV. Retry at `retry_initial_s`, doubling to `retry_max_s`; the backoff resets after a run survives one full segment. The incident is emitted once per failure streak, not once per retry (Review Focus 2).
- Radar: `start()` failing gets the same backoff. While running, `silent_for_ns > silence_timeout_s` means radar missing. When reads resume, the radar is OK again with no restart.
- State:
  - both devices OK: `RECORDING`;
  - one missing: `DEGRADED` with that reason;
  - both missing: `WAITING_FOR_DEVICES`, reason `"camera and radar missing"`.
  - `cfg.camera.enabled = False` or `cfg.radar.enabled = False` means that device isn't expected and doesn't count as missing.

- [ ] **Step 1: Write the failing tests:**
  - `test_disk_full_stops_and_resumes`: the storage-check stub returns free = 2 GiB, then 0.9 GiB, then 1.2 GiB, then 2 GiB. The states are `recording`, then `disk_full`, then `disk_full` (hysteresis), then `recording`. There are two sessions in total, the first `complete` with `stop_reason == "disk full"`, and nothing deleted.
  - `test_disk_full_at_start_opens_nothing`.
  - `test_enospc_on_write_goes_disk_full`: the writer is monkeypatched to raise `OSError(errno.ENOSPC, ...)`. The state becomes `disk_full`, with no exception out of `tick()`.
  - `test_storage_missing_then_back`: reason `"storage missing: recordings dir missing"`, then recording again once the dir exists.
  - `test_camera_crash_degraded_and_backoff`: `FakeCamera(fail_starts=3)`. The state is `degraded` / `"camera missing"`. The retry times follow 1, 2, 4 s on the manual clock. One `"camera exited"` incident is emitted for the streak, and the state is `recording` once a run survives.
  - `test_radar_silence_degraded_then_recovers`: no injects for over 5 s gives `degraded` / `"radar missing"`; an inject brings back `recording`.
  - `test_both_missing_waiting`.
  - `test_camera_disabled_is_not_degraded`.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run all tests:** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat: disk policy, degraded mode and device recovery`

### Task 10: Session reader

**Files:** Create `radar_cam_recorder/reader.py`, `tests/test_reader.py`

**Interfaces:**
- Consumes: the session format (Task 5), `parse_metadata` and `write_frames_csv` (Task 7), and `ld2451.FrameDecoder`, `ld2451.protocol.decode_report`, `ld2451.read_records`, `ld2451.RadarReport`
- Produces:
  - `Frame(segment: int, index: int, sensor_ts_ns: int, video: Path)` (frozen)
  - `open_session(path: Path) -> RecordedSession`. Raises `ValueError` when `format_version` isn't 1.
  - `class RecordedSession`:
    - `metadata: dict[str, Any]`
    - `events() -> list[dict[str, Any]]`
    - `frames() -> Iterator[Frame]`: reads `frames.csv`, or parses `meta.json` when the CSV is missing
    - `radar_reports() -> Iterator[RadarReport]`: decodes every radar segment in order with the recorded timestamps; a partial last line is ignored
    - `timeline() -> Iterator[Frame | RadarReport]`: merged by `sensor_ts_ns` and by `t_rx_ns - radar_latency_ns`
    - `frame_at(ts_ns: int) -> Frame | None`: the nearest frame within one frame interval, else None
    - `summary() -> dict[str, Any]`, with keys `state`, `duration_s`, `frames`, `mean_fps`, `gaps` (count of frame intervals > 2× the median), `radar_reports`, `overlap_s`

- [ ] **Step 1: Write the failing tests.** They build a session with a `Supervisor` + `FakeCamera` + loopback radar for 2 segments (that is the integration test), inject the driver's manual example frame (`F4 F3 F2 F1 11 00 03 01 8A 28 00 3C 15 8A 1E 01 3C 0F 76 5F 00 3C 0F F8 F7 F6 F5`), then open the session with `open_session`.
  - Frames come in increasing `sensor_ts_ns` order across segments.
  - The injected report decodes to 3 targets.
  - The `timeline()` timestamps are non-decreasing.
  - `frame_at` returns the frame closest to a report.
  - `summary()["overlap_s"] > 0`.
  - A crashed copy (meta truncated, no frames CSV, partial radar line) still opens.
  - `format_version` 2 raises `ValueError`.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit:** `feat: session reader`

### Task 11: CLI, systemd unit, README

**Files:** Create `radar_cam_recorder/cli.py`, `deploy/radar-cam-recorder.service`, `README.md`, `CHANGELOG.md`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above
- Produces: commands
  - `radar-cam-recorder run [--config PATH] [-v]`: installs SIGTERM/SIGINT handlers that set the stop event; uses `LogSink` + `StatusFileSink(cfg.status_file)`; exit 0 on a signal stop, 1 on a config error
  - `status [--config PATH]`: prints the status file JSON; exit 1 if it's missing
  - `check-config [--config PATH]`: prints the parsed config as JSON
  - `inspect SESSION_DIR [--json]`: prints `summary()`
  - Default `--config` is `/etc/radar-cam-recorder.toml`.
  - `--fake` (hidden, dev only) uses `FakeCamera` and a loopback radar, so the whole thing runs on a Mac.

- [ ] **Step 1: Write the failing tests:**
  - `check-config` on the example config exits 0 and prints JSON;
  - a bad config exits 1 with `error:` on stderr;
  - `run --fake` in a subprocess with a tmp config: after 2 s, SIGTERM gives exit 0 within 15 s, one `complete` session, and a status file with state `stopped`;
  - `inspect` on that session prints `frames`;
  - `status` prints the state.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement** the CLI.
- [ ] **Step 4: Write the unit file:**

```ini
[Unit]
Description=Camera + LD2451 radar recorder
After=local-fs.target
[Service]
Type=simple
User=bmendonca
WorkingDirectory=/home/bmendonca/workspace/radar-cam-recorder
ExecStart=/home/bmendonca/workspace/radar-cam-recorder/.venv/bin/radar-cam-recorder run --config /etc/radar-cam-recorder.toml
RuntimeDirectory=radar-cam-recorder
KillSignal=SIGTERM
TimeoutStopSec=15
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Write `README.md`** (English): what the project does; installing on the Mac (`uv sync --all-groups`) and on the Pi (uv via the `arm-unknown-linux-musleabihf` release into `~/.local/bin`, then `uv sync --python /usr/bin/python3`); configuration; service install and enable; the session format; reader usage; copying sessions with `rsync -av --exclude-from` on sessions still `in_progress`; the known limits (segment gaps of 1–2 s, ~50 ms alignment, the clock without RTC).
- [ ] **Step 6: Run** `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run mypy --platform linux`. Expected: all pass.
- [ ] **Step 7: Commit:** `feat: CLI, systemd unit and docs`

### Task 12: Deploy to the Pi and run the acceptance checks

**Files:** Create `docs/pi-acceptance.md` (the results), and modify `deploy/config.example.toml` (the measured camera default).

Manual task on `ssh homegateway`; record every command's result in `docs/pi-acceptance.md`.

- [ ] **Step 1: Install uv and sync.**
  - Install uv 0.10.10 (`uv-arm-unknown-linux-musleabihf.tar.gz`) into `~/.local/bin`.
  - `rsync` both projects to `~/workspace/` (excluding `.venv` and the caches).
  - In `~/workspace/radar-cam-recorder`, run `uv sync --python /usr/bin/python3`.
  - Expected: `uv run python -c "import ld2451, radar_cam_recorder"` exits 0.
- [ ] **Step 2: Run the tests on the Pi:** `uv run pytest -q`. Expected: 0 failures.
- [ ] **Step 3: Measure camera modes** (spec acceptance 8). Two 10-minute runs, at 1296×972@25 and 1920×1080@25, each with radar recording, using `run` with a test config and then `inspect --json`. Record `mean_fps`, `gaps`, MB/min and CPU (`top -b -n1`). Check that the frame counts in `meta.json` match the lines in `pts.txt` for each segment; a mismatch means metadata isn't one-to-one with encoded frames, so stop and report it. Set the better mode as the example default.
- [ ] **Step 4: Install the service.**
  - Copy the config to `/etc/radar-cam-recorder.toml` (`recordings_dir = /home/bmendonca/recordings`).
  - Install the unit to `/etc/systemd/system/`, then run `sudo systemctl daemon-reload && sudo systemctl enable --now radar-cam-recorder`.
- [ ] **Step 5: Acceptance 1:** `sudo reboot`; after boot, without logging in, `radar-cam-recorder status` shows `recording`.
- [ ] **Step 6: Acceptance 2 and 3.**
  - After 10 minutes, `sudo systemctl stop radar-cam-recorder`; `session.json` shows `complete`.
  - `rsync` the session to the Mac and run `uv run radar-cam-recorder inspect <dir>`: `overlap_s` ≈ the duration and `frames > 0`.
  - Check the clock samples: for every sample, `|boottime − monotonic| < 1 ms`.
- [ ] **Step 7: Acceptance 4.** `sudo systemctl kill -s KILL radar-cam-recorder`. Systemd restarts it; the previous session shows `interrupted`, and `inspect` opens it.
- [ ] **Step 8: Acceptance 5.** Set `stop_below_mb` just under the current free space and restart. Then fill the gap with `fallocate -l <size> ~/filler`: the status goes to `disk_full` and the session is `complete`. `rm ~/filler`: a new session starts.
- [ ] **Step 9: Acceptance 6 and 7.**
  - `pkill -INT rpicam-vid`: an incident and backoff are logged, then recording continues.
  - Pull the radar's 5 V wire: `degraded` / `radar missing` within about 5 s. Plug it back in: `recording`.
- [ ] **Step 10: Commit** `docs/pi-acceptance.md` and the example config: `docs: Pi acceptance results`
