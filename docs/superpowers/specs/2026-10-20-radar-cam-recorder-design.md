# radar-cam-recorder — design

Date: 2026-10-20. Status: draft for approval.

Marks: **[user]** = decided by the user; **[call]** = my call, open to change;
**[measured]** = observed on the target Pi on 2026-10-20.

## 1. Intent

Record camera video and HLK-LD2451 radar data on a Raspberry Pi Zero W, unattended,
so the two streams can be **synchronised later** (mandatory) **[user]**. The recordings
feed a future "speed trap" pipeline developed off-site on a Mac or server: an overlay
of radar targets on the video and, later, vehicle detection with each car's speed
**[user]**. This project is the data-collection tool for that work; it is for
development, not a product **[user]**.

Success for the first version: after a day pointed at a road, there are sessions of
video plus radar data that a processing tool on the Mac can open and align
**[user]**. It is a failure, even if it "works", if the streams cannot be aligned, if
data goes missing without anyone noticing, or if a session cannot be opened later
**[call, inferred]**.

## 2. Goals, non-goals, anti-goals

Goals:

1. Record video (Raspberry Pi Camera v1.3) and radar (LD2451 over `/dev/serial0`) at
   the same time, with timestamps that allow exact alignment **[user]**.
2. Run as a systemd service that starts recording at boot, with no one logged in
   **[user]**.
3. Monitor free space on the disk holding the recordings; below a configurable
   threshold, stop recording gracefully **[user]**.
4. Shut down gracefully on any stop (low disk, `systemctl stop`, reboot, SIGINT): both
   video and radar data closed cleanly **[user]**.
5. Expose a reliable recorder state, ready for future feedback through a GUI, an LCD
   or an LED; today it has no display **[user]**.
6. Modular, well-written code **[user]**.
7. A reader API that opens a session on the Mac for the processing tools **[call]**.

Non-goals (first version):

- Overlay, detection, speed computation, projection or any other analysis **[user]**.
- Live preview or streaming **[call]**.
- Automatic upload or sync; transfer is manual `rsync` for now **[user]**.
- Any physical LED/LCD/GUI driver; only the hook for one **[user]**.
- Copying data off the Pi while it is still recording; stop the service, then rsync
  **[user]**.
- Real date and time without network: the Pi has no network and no RTC for now; an
  RTC may come later **[user]**. Only stream-to-stream sync is required.
- Calibrating sensor latencies to better than ~50 ms **[user]**.
- Daylight-only recording: wanted, but deferred to a later version (see 11)
  **[user]**. v1 records continuously.
- A larger drive; the SD card is enough for now **[user]**.

Anti-goals:

- **Never delete recordings automatically**, not even when the disk is full **[user]**.
- Never write into an unmounted mount-point directory on the system disk **[call]**.
- Never leave the radar in configuration mode **[call]**.
- Never assume a constant video frame rate **[measured]** (see 3).
- Never hide a gap: every gap in either stream (segment changes, restarts, drops)
  must be visible in the timestamps and logged as an event **[call]**.

## 3. Constraints and measured facts

Hardware **[measured]**:

- Raspberry Pi Zero W Rev 1.1: single-core ARMv6, 427 MB RAM, Raspbian 12
  (bookworm), Python 3.11.2 system, user `bmendonca` in `dialout`/`video`, sudo.
- Camera: OV5647 (Camera v1.3). Modes: 640×480 @58.9, 1296×972 @46.3,
  1920×1080 @32.8, 2592×1944 @15.6 fps.
- Hardware H.264 encoder present (`/dev/video11`). `rpicam-apps-lite` 1.7.0 installed;
  **Picamera2 is not installed**.
- `rpicam-vid -n -t 2000 --width 1296 --height 972 --framerate 30 --codec h264
  --save-pts … --metadata … --metadata-format json` worked: ~160 kB/s on a static
  scene. **It delivered ~21 fps, not 30**: 29 frames in 1.37 s, with gaps visible in
  the PTS file.
- Per-frame metadata includes `SensorTimestamp` (ns) and `FrameWallClock`. The first
  `SensorTimestamp` fell inside both the `CLOCK_BOOTTIME` and the `CLOCK_MONOTONIC`
  windows around the capture. On this Pi the two clocks differed by 24 µs. They
  differ only by time spent suspended, which a Pi does not do.
- Radar: `ld2451` driver (sibling project `~/workspace/driver-hlk-ld2451`), firmware
  V1.13.25070114. It stamps every serial read with `CLOCK_BOOTTIME` ns. With no
  target it sends an empty frame every ~1.39 s.
- Storage: one SD card, 14.3 GB ext4 root partition, 9.2 GB free. No battery-backed
  clock: wall time is wrong until NTP syncs after boot.

Software:

- Python ≥ 3.9 code, typed, same tooling as the driver: ruff, `mypy --strict`, pytest
  **[call]**.
- Must run and be tested on the Mac without a camera or radar **[call]**.

## 4. Project layout and sharing

- Its own project at `~/workspace/radar-cam-recorder` on the Mac and on the Pi, with
  its own `pyproject.toml` and its own venv managed by `uv` **[user]**. No venv or
  `pyproject.toml` in `~/workspace` itself **[user]**.
- It depends on the driver as a local editable path dependency
  (`[tool.uv.sources] ld2451 = { path = "../driver-hlk-ld2451", editable = true }`),
  so driver changes take effect without publishing **[user + call on mechanism]**.
- Shared with the driver as far as possible **[user]**: the same Python range, ruff,
  mypy and pytest settings, and the same layout style. The radar recording format and
  the receive clock are reused from the driver, not reimplemented **[call]**.
- **To verify in planning:** whether `uv` runs on the Pi Zero's ARMv6. If it doesn't,
  the Pi uses a plain `python3 -m venv` installing the same `pyproject.toml`, with the
  driver installed editable from `../driver-hlk-ld2451` **[call]**.
- Camera backend **[call]**: drive `rpicam-vid` as a subprocess (hardware encode, low
  CPU, per-frame metadata for free). Not Picamera2: it is not installed, it would force
  the venv to use system site-packages, and it costs CPU on a single core. The camera
  sits behind an interface, so Picamera2 could be added later.

## 5. Components

Each part has one job, a small interface, and its own tests **[call]**.

| Part | Responsibility |
| --- | --- |
| `config` | Load and validate one TOML file. Defaults suitable for the Pi. |
| `clocks` | Read `CLOCK_BOOTTIME`, `CLOCK_MONOTONIC`, `CLOCK_REALTIME` together; report whether NTP has synced. |
| `storage` | Free space on the filesystem that holds the recordings dir; the "storage missing" check. |
| `session` | Create and close session folders; write metadata and the in-progress/complete marker. |
| `camera` | `CameraSource` interface. `RpicamCamera` runs `rpicam-vid` and collects per-frame timestamps. `FakeCamera` is for the Mac and tests. |
| `radar` | Wraps `ld2451.LD2451`; writes raw reads with the driver's `t_rx_ns,hex` format, split into segments. `FakeRadar` uses the driver's `LoopbackTransport`/`ReplayTransport`. |
| `status` | State machine and event bus. Sinks subscribe; today: a log sink and a status file. |
| `supervisor` | Startup, shutdown, ordering, signal handling, health checks, device recovery. |
| `reader` | Opens a session on any machine; no camera or Pi dependencies. |
| `cli` | `radar-cam-recorder run`, `status`, `check-config`, `inspect <session>`. |

## 6. Behaviour

### 6.1 Lifecycle and states

States **[call]**: `starting`, `waiting_for_devices`, `recording`, `disk_full`,
`storage_missing`, `degraded` (one stream down, the other still recording),
`stopping`, `stopped`, `error`.

- Every transition is an event carrying the state, a reason and a timestamp. It is
  published to all sinks and appended to the session's event log.
- Sinks today: log (journald) and a JSON status file in a runtime dir (for example
  `/run/radar-cam-recorder/status.json`), so `radar-cam-recorder status` or `cat`
  shows the current state. Future LED/LCD/GUI sinks implement the same small
  interface and must not be able to block or crash the recorder.
- One stream down is `degraded`, not a stop: if the camera dies, radar keeps
  recording, and the supervisor retries the camera with backoff. The same holds the
  other way round **[call]**.

### 6.2 Storage policy **[user, thresholds and resume are calls]**

- Free space is checked periodically (for example every 5 s) on the filesystem that
  actually holds the recordings directory.
- Below `stop_below` (configurable, for example 1 GiB): close the current segments
  cleanly, record the reason in the metadata and the event log, mark the session
  complete, and go to `disk_full`. The service keeps running.
- At start (including boot), if free space is below `stop_below`, don't open a session;
  go straight to `disk_full`.
- Resume automatically once free space is above `resume_above` (configurable, higher
  than `stop_below`, for example 1.5 GiB), so it doesn't flap **[call]**. Resuming
  opens a new session **[call]**.
- One service run is one session, except that resuming after `disk_full` starts a new
  one **[call]**.
- At ~0.6–1 GB per hour (estimated with traffic), the 9.2 GB free holds roughly 10–15
  hours. Continuous recording fills the card in under a day, which `disk_full` handles;
  the daylight-only feature (11) will stretch this.
- Nothing is ever deleted.
- Storage missing: if the config says the recordings dir must be a mount point (for a
  future SD card or USB drive) and it isn't mounted, or the dir is missing or not
  writable, don't record; go to `storage_missing`; re-check periodically.

### 6.3 Graceful shutdown **[user, details are calls]**

- SIGTERM (systemd stop or reboot) and SIGINT trigger the same orderly stop:
  1. stop accepting new segments;
  2. stop `rpicam-vid` with SIGINT (so it flushes), and wait for it with a timeout;
  3. stop the radar driver (`LD2451.close()`);
  4. flush and fsync open files;
  5. write the final metadata (stop reason and times) and mark the session complete.
- The whole stop finishes within systemd's `TimeoutStopSec` (for example 15 s).
- After a hard stop (power cut, SIGKILL), a session is left "in progress". The next
  start detects it and marks it "interrupted" (recording what it can, such as the last
  timestamp seen), without modifying data files.

### 6.4 Segments and crash safety **[call]**

- Video and radar are written in segments of configurable length (for example 5
  minutes), so a power cut loses at most the open segment's unflushed tail.
- Video is raw H.264 (`.h264`), not MP4, so a cut-off file stays readable. Each frame's
  timestamp (`SensorTimestamp`) is stored next to it. The per-frame metadata stream is
  parsed incrementally, so a truncated last record is tolerated.
- **A short video gap between segments is acceptable [user]**. So the camera module
  runs one `rpicam-vid` process per segment and restarts it at each segment change
  (about 1–2 s lost per change on the Pi Zero). Each segment's video and frame file
  come from one process, so frames never need matching across files **[call]**.
  Gaps show up in the timestamps and are logged (2, anti-goals). Radar recording
  doesn't stop between segments.
- Radar segments use the driver's format, one line per serial read:
  `t_rx_ns,hex`. A segment switch never loses or duplicates a read.
- Data is flushed at least every few seconds. fsync on segment close and on stop.

### 6.5 Synchronisation (mandatory) **[user requirement; method is a call]**

Target: video and radar aligned to within **~50 ms [user]**. Plain timestamps on a
common clock plus the clock samples below meet that; measuring sensor latencies is
out of scope for v1.

- Common timebase: the Linux boot clock in nanoseconds. Radar: `t_rx_ns` from the
  driver (`CLOCK_BOOTTIME`). Video: `SensorTimestamp` per frame.
- **To verify in planning:** which kernel clock `SensorTimestamp` uses (V4L2 typically
  stamps with `CLOCK_MONOTONIC`). Either way, at session start, at every segment change
  and at stop, the recorder samples BOOTTIME, MONOTONIC and REALTIME together and writes
  them to the metadata, so any offset can be corrected offline.
- Known latencies are documented, not hidden. Radar `t_rx_ns` is the arrival time of
  the read carrying the frame's first byte, so it includes UART and transmit delay. A
  per-session `radar_latency_ns` field (default 0) allows a calibrated correction
  later.
- Wall time: **there is no network and no RTC for now [user]**, so the wall clock is
  unreliable. The recorder never depends on it. Session ids are built from something
  that is always valid, such as a persistent boot counter plus a sequence number,
  so they stay unique and sortable without the wall clock **[call]**. REALTIME
  samples are still written; if NTP or an RTC sync is ever detected, that is noted.

### 6.6 Session folder (format v1, details left to the builder)

```
<recordings_dir>/<session_id>/
  session.json          # format_version, ids, config snapshot, devices, radar
                        # params/firmware, camera mode, setup notes, clock samples,
                        # start/stop reasons, state: in_progress|complete|interrupted
  events.jsonl          # status transitions and incidents
  video/0000.h264 …     # H.264 segments
  video/0000.frames.csv # per frame: sensor_ts_ns, index, other useful metadata
  radar/0000.csv …      # driver format t_rx_ns,hex
```

- Setup notes **[call]**: free-text plus optional structured fields for the mounting
  geometry (radar and camera position and angles relative to the road), filled from the
  config. They are needed later for projection and cosine correction.
- rsync-friendly: plain files; the in-progress/complete state lets a sync skip a live
  session.

### 6.7 Reader **[call]**

- `open_session(path) -> Session`, with access to the metadata, the events, the video
  segments with per-frame timestamps, and radar reports decoded through the driver
  with their original timestamps. It also provides a time-ordered merge of frames and
  radar reports.
- It doesn't decode video (that stays the processing tool's job, for example with
  OpenCV); it maps a timestamp to a segment and frame index.
- Pure Python plus `ld2451`; works on the Mac.

### 6.8 Configuration and service **[call]**

- One TOML file (for example `/etc/radar-cam-recorder.toml`, or a path passed with
  `--config`): recordings dir, require-mountpoint flag, thresholds, check interval,
  segment length, camera (resolution, fps, bitrate, extra `rpicam-vid` args), radar
  (port, baud, whether to apply detection parameters at start), setup notes, status
  file path.
- Radar loss on the GPIO UART shows up as silence, not a port error, so the radar part
  treats no bytes for a configurable time (for example 5 s, above the 1.39 s idle
  frame interval) as `radar missing` **[call]**.
- A systemd unit is shipped. It runs as the normal user with `Restart=always`, starts
  after the local filesystems, sets `TimeoutStopSec`, and logs to journald. Waiting
  for the camera and radar happens inside the app (`waiting_for_devices`), not in the
  unit.
- Python's standard `tomllib` exists only from 3.11; on 3.9/3.10 use `tomli` (a small
  pure-Python dependency) **[call]**.

## 7. Acceptance checks

On the Mac:

- All tests pass without hardware (fake camera, fake radar), ruff and mypy clean.
- The reader opens a session produced by the fakes, and frames and radar reports
  merge in timestamp order.

On the Pi **[call]**:

1. Boot with the service enabled: recording starts with no login.
2. A 10-minute session copied with `rsync` opens with the reader on the Mac. Frame
   timestamps and radar timestamps overlap in time and are on the same clock (verify
   against the metadata clock samples).
3. `systemctl stop` produces a session marked complete, with every segment readable.
4. `kill -9` and a power cut produce a session marked interrupted at the next start,
   with all data written before the cut readable.
5. With `stop_below` set just under the current free space: the recorder stops
   cleanly, goes to `disk_full`, keeps running, and resumes once above `resume_above`.
6. Unplugging the radar, or killing `rpicam-vid`, gives `degraded`, the other stream
   continues, and recovery is automatic.
7. Pulling the radar's 5 V gives `radar missing` within the silence timeout.
8. A camera mode is chosen by measurement: compare frame rate and frame drops at
   1296×972 and 1920×1080 over 10 minutes, with radar recording and the encoder
   running at the same time; record the result in the docs.

## 8. Decided by the user

Section 1 intent and success; short video gap between segments acceptable; ~50 ms
alignment; daylight-only deferred to later; no copying while recording; no network or RTC for now; separate sibling project with its own `uv` and
`pyproject.toml`; import the driver as a library; mandatory sync; free-space
threshold that stops recording gracefully; never auto-delete; graceful shutdown of
both streams; systemd service recording from boot; feedback hook for a future
GUI/LCD/LED; manual rsync now, possible separate SD card later; modular,
well-written code; off-site processing is out of scope.

## 9. Left to the builder

File names and the exact schema of `session.json` and `*.frames.csv` (keep a
`format_version`); default values; module boundaries inside each component; test
strategy for the subprocess camera; CLI details; how to detect NTP sync.

## 10. Open questions

None. Settled as calls after the builder check: automatic resume after `disk_full` with
hysteresis; project name `radar-cam-recorder`; one camera process per segment; radar
silence timeout; session ids that don't need the wall clock.

## 11. Future features (not in v1)

- **Daylight only [user]**: record only in daylight, since the Camera v1.3 has no IR.
  There is no reliable clock without network or RTC, so a time schedule won't work. A
  likely approach is the camera's per-frame `Lux` estimate, with two thresholds and a
  minimum dwell time, and a short light check every few minutes at night. Still to
  decide: whether the radar keeps recording at night.
- **RTC module [user]**, giving real date and time.
- **Larger or removable drive [user]**; the storage-missing guard is already in v1.
- **Feedback devices [user]**: LED, LCD or GUI sinks on the status bus.
- **Latency calibration** to get alignment below 50 ms.
- **Copying while recording**, for example by session rotation.

