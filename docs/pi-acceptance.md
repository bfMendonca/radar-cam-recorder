# Pi acceptance results (2026-10-20)

Target: Raspberry Pi Zero W (ARMv6), Raspbian 12, system Python 3.11.2, OV5647 camera
(`rpicam-vid`), HLK-LD2451 radar on `/dev/serial0` (firmware V1.13.25070114, direction
"both"). Config used `[radar.detection]` UNSET (radar settings untouched) and
`[setup] notes = "acceptance run 2026-10-20, indoor"`. Recordings went to
`/home/bmendonca/recordings`. Branch `feat/recorder-v1`.

Summary (2026-10-20 run; the LEDs and button acceptance is at the end of this file): acceptance 1, 2, 3, 5, 6 and the SIGKILL half of 4 pass. Acceptance 7 and the
power-cut half of 4 are pending (need physical access). Step 2 (Pi test suite, expected 0
failures) was UNMET on the original run (1 failed + 2 errors, timing-only) and is resolved
by the re-run after the R29 fixes (133 passed). The camera-stall finding is fixed in code (R28) and was
verified on hardware with a forced stall.
Camera default 1296x972 is PROVISIONAL (R32).

## Findings (not patched, library code untouched)

1. **Camera stall is not detected (acceptance 2 / robustness). FIXED IN CODE by R28 (commit d3790d5), verified on hardware (below).** Twice out of about eight
   `rpicam-vid` starts the camera process stayed alive but stopped delivering frames, and
   the recorder kept reporting `recording` with no incident:
   - Boot session `s000002`, segment 1 (03:28:09, right after the first rotation): 39
     frames in the first 1.5 s, then nothing for about 298 s. At the next rotation the
     process had to be killed, so `0001.meta.json` is truncated (unparsable JSON) and
     `0001.pts.txt` is empty; `frames.csv` was still recovered (39 rows). Incident logged
     only as `segment gap` (`gap_ns` = 299.77 s) after the fact.
   - Measurement run 1 (1296x972), segment 0: 0 frames for 60 s. `0000.log` shows
     `Dequeue timer ... expired`, `Camera frontend has timed out!`, `Device timeout
     detected, attempting a restart!!!`, so libcamera's own restart did not recover.
   The supervisor has no "alive but no frames" watchdog (only process exit is detected). A
   frame-liveness check (no new meta/frames for N s, then restart as a camera failure with
   an incident) would close this. Related: the unparsable meta of a killed segment is
   handled (the reader still opens the session).
   **Resolution:** R28 added `[camera] stall_timeout_s` (default 10.0): if the segment's
   meta file does not grow for that long (grace of one timeout after a start), the
   recorder logs a `camera stalled` incident, stops the run, and treats it as a camera
   failure (degraded `camera missing`, backoff, new segment if video was left). Covered by
   unit tests (a fake camera run that stays alive without writing frames); deployed to the
   Pi with `stall_timeout_s = 10.0` in `/etc/radar-cam-recorder.toml` (backup
   `/etc/radar-cam-recorder.toml.bak`) and the service recording afterwards. No natural
   stall was re-observed, so the hardware check was forced (below).
   **Order of results:** the Step 3 measurement and the Step 5-9 acceptance results in
   this document were all recorded BEFORE the R28-R30 deploy, on pre-fix code. Only the
   Step 2 re-run and the watchdog check below ran on the fixed code.
   **Hardware check of the watchdog (after the deploy, service recording):**

   ```
   sudo pkill -STOP -x rpicam-vid          # freeze the camera process, 04:29:19
   # poll: radar-cam-recorder status --config /etc/radar-cam-recorder.toml every 3 s
   sudo pkill -CONT -x rpicam-vid          # 04:30:00 (always sent; also in a trap)
   journalctl -u radar-cam-recorder --since "2026-10-21 04:29:14"
   ```

   ```
   04:29:30 WARNING incident: recording (camera stalled) {'segment': 3, 'stalled_s': 10.035829059}
   04:29:36 INFO    state: degraded (camera missing)
   04:29:37 INFO    state: recording (recording)
   04:29:39 WARNING incident: recording (segment gap) {'segment': 4, 'gap_ns': 19390101000}
   ```

   Result: PASS. The stall was detected 10.04 s after the incident clock started, one
   incident, degraded after the frozen process was stopped (it ignores SIGINT while frozen,
   so it is killed after the stop timeout), then a new `rpicam-vid` (new pid) started a new
   segment 4 and the state returned to `recording`. After `SIGCONT` the service stayed
   `recording` (one `rpicam-vid`, pid 10494).
2. **Test suite timing on the Pi (Step 2 FAIL on the original run; fixed by R29):** 125 passed, 1 failed, 2 errors, all timing
   assumptions that are too tight for a Pi Zero (Python start-up of a subprocess takes
   over 1 s):
   - `tests/test_camera.py::test_rpicam_exit1` waits 0.5 s for the fake rpicam
     (a Python script) to exit.
   - `tests/test_cli.py::recorded` fixture (errors in `test_run_fake_stops_cleanly_on_sigterm`
     and `test_inspect_and_status`) sends SIGTERM after 2.5 s, before the child has
     installed its handler, so it dies with -15.
   Manual check that the behaviour is right: `run --fake` with a 20 s delay before SIGTERM
   exits 0 and writes a complete session; `FAKE_RPICAM_MODE=exit1 tests/fake_rpicam.py`
   exits 1 immediately.
3. **Dependency install on ARMv6 (fixed by R29/R30):** `uv sync` as written fails to be practical. The lock
   resolves `hypothesis` 6.170.5 for Python >= 3.11, which ships a Rust extension with no
   ARMv6 wheel; uv starts compiling it from source with a Rust toolchain (it ran for
   minutes and used a large part of the 427 MB RAM; I stopped it). Workaround used:
   `uv sync --python /usr/bin/python3 --no-group test`, then
   `uv pip install pytest "hypothesis==6.141.1"` (pure Python). The README Pi steps
   should say so or the lock should cap hypothesis for the test group. Also, the repo's
   `.python-version` (3.9) makes plain `uv run` refuse the venv on the Pi, so commands were
   run as `.venv/bin/radar-cam-recorder ...` and `.venv/bin/python -m pytest`.
   **Resolution:** hypothesis capped to `>=6.100,<6.142` in the test group (R29, pure
   Python), and the README documents `export UV_PYTHON=/usr/bin/python3` (R30).
4. **Boot-time camera flapping (about 10 s).** Right after boot the camera stack is not
   ready: session `s000002` logged one `camera exited` incident (returncode 255, `Could not
   open any dmaHeap device`, then `no cameras available`) and `degraded / camera missing`
   flipped to `recording` and back three times between 45 s and 54 s of uptime before it
   stabilised (see Step 5). Handled as designed (one incident per streak, backoff, no
   crash), but expect it on every boot; it is noted in the README's known limits.

## Step 1: install

Commands as run on 2026-10-20 (first run of the brief's steps, before the fixes):

```
ssh homegateway 'mkdir -p ~/workspace/radar-cam-recorder ~/recordings ~/.local/bin'
EX='--exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.hypothesis --exclude=.superpowers --exclude=.DS_Store --exclude=*.egg-info'
rsync -a $EX driver-hlk-ld2451/ homegateway:workspace/driver-hlk-ld2451/
rsync -a $EX radar-cam-recorder/ homegateway:workspace/radar-cam-recorder/
ssh homegateway 'curl -sSfL https://github.com/astral-sh/uv/releases/download/0.10.10/uv-arm-unknown-linux-musleabihf.tar.gz | tar xz -C /tmp && install -m 755 /tmp/uv-arm-unknown-linux-musleabihf/uv ~/.local/bin/uv && ~/.local/bin/uv --version'
                                   -> uv 0.10.10
# brief's command: uv sync --python /usr/bin/python3
#   -> started compiling hypothesis 6.170.5 from source (Rust, no ARMv6 wheel); stopped by me.
# deviation actually used (1m25s):
cd ~/workspace/radar-cam-recorder
uv sync --python /usr/bin/python3 --no-group test
uv pip install pytest "hypothesis==6.141.1"
.venv/bin/python -c "import ld2451, radar_cam_recorder, hypothesis"   -> ok (Python 3.11.2)
```

Deviation: `--no-group test` plus a manual pure-Python hypothesis, and `uv run` was not used
(`.python-version` = 3.9). **Current supported procedure** (README, after R29/R30): install
uv as above, `export UV_PYTHON=/usr/bin/python3` (in `~/.profile`), then plain `uv sync`
in `~/workspace/radar-cam-recorder`; the test group now pins a pure-Python hypothesis, so
nothing is compiled. The Pi was re-synced this way after the fixes.

## Step 2: tests on the Pi

**Original run: FAIL (expected 0 failures).** Command and environment:
`cd ~/workspace/radar-cam-recorder && .venv/bin/python -m pytest -q` with the deviating
environment above (hypothesis 6.141.1 installed by hand) ->
`1 failed, 125 passed, 2 errors in 131.74s`. Cause (finding 2): fixed timing assumptions in
the tests (0.5 s wait for the fake camera to exit; SIGTERM after 2.5 s in the CLI fixture,
before the child installed its handler), not recorder behaviour (verified by hand).

**Resolution (re-run after R29, commits af72e6f and d3790d5, per
`task-12-fix-report.md`):** the Pi was re-synced with `UV_PYTHON=/usr/bin/python3
~/.local/bin/uv sync` (pure-Python hypothesis 6.141.1, nothing compiled) and the suite run
there gives `133 passed` (0 failures), including the new stall-watchdog tests. Command
and environment of the re-run (service recording at the time):

```
export UV_PYTHON=/usr/bin/python3
cd ~/workspace/radar-cam-recorder && time timeout 1800 ~/.local/bin/uv run pytest -q
-> 133 passed in 81.6 s
```

## Step 3: camera modes (acceptance 8)

Shortened to 3 minutes per run (instead of 10) and `segment_s = 60`, radar recording, via
`radar-cam-recorder run` with a test config, then `inspect --json`. The 3-minute shortening
was a controller instruction in the Task 12 dispatch (R31), so there is less statistical
confidence in the fps and gap numbers. The runs were stopped
with `timeout --foreground -s TERM 185` (first attempt without `--foreground` also sent
TERM to `rpicam-vid`, which truncated the last segment; that run was discarded for
numbers except for the segment-0 stall in finding 1).

Test configs (`~/accept/m1296.toml`; `m1080.toml` differs in `width = 1920`, `height = 1080`,
the status file and the recordings dir `/home/bmendonca/accept/rec1080`). `[radar.detection]`
unset:

```toml
[recording]
segment_s = 60
status_file = "/tmp/accept-status-m1296.json"
[storage]
recordings_dir = "/home/bmendonca/accept/rec1296"
[camera]
width = 1296
height = 972
framerate = 25.0
[radar]
port = "/dev/serial0"
[setup]
notes = "acceptance run 2026-10-20, indoor"
```

Commands (the 1080 run is identical with `m1080.toml` and `rec1080`):

```
cd ~/workspace/radar-cam-recorder
timeout --foreground -s TERM 185 .venv/bin/radar-cam-recorder run --config ~/accept/m1296.toml
.venv/bin/radar-cam-recorder inspect ~/accept/rec1296/s000002 --json
python3 ~/accept/chk.py ~/accept/rec1296/s000002        # frame-count check, source below
top -b -n1                                               # mid-run CPU
```

`chk.py` (per segment: meta entries, pts lines, frames.csv rows, h264 size; overall median
frame interval):

```python
import json, sys, pathlib, statistics
d = pathlib.Path(sys.argv[1]) / "video"
allts = []
for m in sorted(d.glob("*.meta.json")):
    s = m.stem.split(".")[0]
    try:
        meta = json.loads(m.read_text()); n = len(meta)
    except ValueError as e:
        n = None; print(s, "META UNPARSABLE", e)
    pts = [l for l in (d / (s + ".pts.txt")).read_text().splitlines() if l and not l.startswith("#")]
    csv = len((d / (s + ".frames.csv")).read_text().splitlines()) - 1
    h = d / (s + ".h264"); size = h.stat().st_size if h.exists() else 0
    print(s, "meta", n, "pts", len(pts), "frames.csv", csv, "h264 MB", round(size/1e6, 2))
    if n:
        allts += [x["SensorTimestamp"] for x in meta]
dt = [(b - a) / 1e6 for a, b in zip(allts, allts[1:])]
print("frames", len(allts), "median dt ms", statistics.median(dt), "max", max(dt))
```

| | 1296x972 @ 25 | 1920x1080 @ 25 |
| --- | --- | --- |
| session | `accept/rec1296/s000002` | `accept/rec1080/s000001` |
| duration_s | 182.5 | 180.4 |
| frames | 3847 | 3013 |
| mean_fps | 21.17 | 16.79 |
| gaps (> 2x median dt, includes 2-3 segment-rotation gaps of 2.2-2.7 s) | 239 | 95 |
| radar reports / overlap_s | 130 / 181.1 | 128 / 179.4 |
| video MB per minute (60 s segments) | ~25.3-26.2 | ~20.0-20.2 |
| CPU (`top -b -n1`, mid-run) | rpicam-vid 63-65 %, recorder 4 %, system 54-62 % sy | rpicam-vid 45 %, recorder 3 %, system 37 % sy |

`inspect --json` output, 1296x972: `{"state": "complete", "duration_s": 182.54, "frames": 3847,
"mean_fps": 21.17, "gaps": 239, "radar_reports": 130, "overlap_s": 181.09}`; 1920x1080:
`{"state": "complete", "duration_s": 180.41, "frames": 3013, "mean_fps": 16.79, "gaps": 95,
"radar_reports": 128, "overlap_s": 179.43}`.

Median frame interval is 40 ms (25 fps) in both, so frames are dropped in bursts rather
than the clock running slow; neither mode sustains the full 25 fps on the Pi Zero.
**Default decision (R32): 1296x972@25 is kept as the PROVISIONAL default;
`deploy/config.example.toml` is unchanged.** Weighing all the numbers: 1296x972 gives more
frames per second (21.2 vs 16.8), which matters for the ~50 ms video/radar alignment, but
it has more gaps (239 vs 95 per 3 minutes, mostly burst drops), higher rpicam-vid CPU
(~64 % vs ~45 %) and more storage (~25 vs ~20 MB/min). 1920x1080 has fewer gaps, lower
CPU and less data, and more resolution for identifying cars. Neither is proven better
indoors; this must be revisited with real road footage. The first test run (discarded) also showed
19.5 fps at 1296x972.

Frame counts match exactly, per segment, between `meta.json` entries, `pts.txt` lines and
`frames.csv` rows (checked with `accept/chk.py`):

- 1296x972 run: 1312/1312/1312, 1262/1262/1262, 1265/1265/1265, 8/8/8.
- 1920x1080 run: 1005/1005/1005, 1010/1010/1010, 998/998/998.
- Boot session `s000002`: 5701/5701/5701 (segment 0) and 3263/3263/3263 (segment 2). Segment 1
  (the stalled one, finding 1) has a truncated meta and an empty pts file, so no
  comparison was possible; `frames.csv` has 39 rows.

## Step 4: service

`/etc/radar-cam-recorder.toml` = `deploy/config.example.toml` with the setup note,
`recordings_dir = /home/bmendonca/recordings`; unit copied to `/etc/systemd/system/`;
`systemctl daemon-reload && systemctl enable --now radar-cam-recorder`. Service left
installed, enabled and recording with the normal config.

## Step 5: acceptance 1 (boot without login): PASS

`sudo reboot`; ssh back after about 1 minute. `who` showed 0 users (no login) and
`radar-cam-recorder status` printed state `recording`. Session `s000001` (from before the
reboot) was `complete`, the new `s000002` was `in_progress`. At boot the camera exited
three times (`Could not open any dmaHeap device`, `no cameras available`) while the camera
stack came up: logged as one `camera exited` incident, `degraded / camera missing`
flapping to `recording` for about 10 s, then stable (`recording` at 54 s of uptime).
`ntp_synced` was False in that session's start sample.

## Step 6: acceptance 2 and 3: PASS

`sudo systemctl stop radar-cam-recorder` after the session ran more than 10 minutes:
`session.json` `state = complete`, `stop_reason = stopped by signal`. Copied to the Mac
(`.superpowers/sdd/2026-10-20-radar-cam-recorder/pi-sessions/s000002`, without the .h264
files to save transfer) and `uv run radar-cam-recorder inspect`:

```
state: complete  duration_s: 745.47  frames: 9003  mean_fps: 12.33  gaps: 604
radar_reports: 524  overlap_s: 730.34
```

`overlap_s` ~ duration and `frames > 0`. `mean_fps` and `gaps` are low only because of the
stalled segment (finding 1). Clock samples (`boottime_ns - monotonic_ns`): -18.0, -21.0,
-20.0, -19.0 microseconds, so all are far below 1 ms.

## Step 7: acceptance 4, SIGKILL half: PASS

`sudo systemctl kill -s KILL radar-cam-recorder`: systemd restarted it after 5 s
(`activating (auto-restart)`, no leftover `rpicam-vid`). The killed session `s000003` became
`interrupted` ("found at next start") and `inspect` opens it (also on the Mac: 522 frames,
14 radar reports, overlap 17.8 s; the last radar line ends cleanly). The new session started
`recording`.

## Step 8: acceptance 5, disk full: PASS

Free space 8323 MB, so `stop_below_mb = 8023`, `resume_above_mb = 8123` in the temporary
config; service restarted; `fallocate -l 400M ~/filler` -> within 5 s status
`disk_full / disk full`, session `s000006` `complete`, `stop_reason = disk full`. After
`rm ~/filler` status went to `recording` within 5 s and a new session (`s000007`) started.
Filler removed, `/etc/radar-cam-recorder.toml` restored and the service restarted with the
normal thresholds (1024 / 1536).

A first attempt was aborted because my own tooling truncated `/etc/radar-cam-recorder.toml`
to an empty file (the status command then failed with `[storage] recordings_dir is
required`, and the service could not start). I restored it with:

```
cd ~/workspace/radar-cam-recorder
sed 's|^notes = ""|notes = "acceptance run 2026-10-20, indoor"|' deploy/config.example.toml > ~/accept/normal.toml
sudo cp ~/accept/normal.toml /etc/radar-cam-recorder.toml
```

So the example's `recordings_dir = "/home/bmendonca/recordings"` (already the example's
value) and the `[setup]` note were re-applied. Current `/etc/radar-cam-recorder.toml`
(`cat` over ssh, after the later `stall_timeout_s` deploy; `.bak` is the pre-deploy file):

```toml
[recording]
segment_s = 300.0
retry_initial_s = 1.0
retry_max_s = 60.0
status_file = "/run/radar-cam-recorder/status.json"
[storage]
recordings_dir = "/home/bmendonca/recordings"
require_mountpoint = false
stop_below_mb = 1024
resume_above_mb = 1536
check_interval_s = 5.0
[camera]
enabled = true
binary = "rpicam-vid"
width = 1296
height = 972
framerate = 25.0
bitrate = 4000000
extra_args = []
stop_timeout_s = 5.0
stall_timeout_s = 10.0
[radar]
enabled = true
port = "/dev/serial0"
baudrate = 115200
silence_timeout_s = 5.0
# [radar.detection] left commented out (radar settings untouched)
[setup]
notes = "acceptance run 2026-10-20, indoor"
[setup.geometry]
```

(Comments and blank lines omitted here; the file is the repo example plus the note.)

## Step 9: acceptance 6: PASS; acceptance 7: PENDING

6: `sudo pkill -INT rpicam-vid` -> incident `camera exited` (returncode 0, with the last
camera log lines), state `degraded / camera missing`, back to `recording` about 2 s later
in a new segment (`segment gap` incident, 3.9 s). Only one incident per failure.
Journal around the event (`journalctl -u radar-cam-recorder --since "03:36:50" --until
"03:37:20"`):

```
03:37:05 WARNING incident: recording (camera exited) {'segment': 0, 'returncode': 0, 'log': [...]}
03:37:05 INFO    state: degraded (camera missing)
03:37:06 INFO    state: recording (recording)
03:37:07 WARNING incident: recording (segment gap) {'segment': 1, 'gap_ns': 3918690000}
```

Backoff: in this case the first retry (`retry_initial_s = 1 s`) succeeded, so doubling was
not exercised here. It was observed at the reboot (Step 5, boot of session `s000002`,
uptime in seconds from `events.jsonl`): degraded at 45.2 -> retry at 46.25 (1 s), degraded
47.3 -> retry 49.3 (2 s), degraded 50.3 -> retry 54.4 (4 s), i.e. 1, 2, 4 s doubling, with a
single `camera exited` incident for the streak.

Pending list (summary): 7 and the power-cut half of 4 need physical access; the camera
watchdog hardware check was a remote item and is done (see finding 1).

7 (pending, needs physical access): with the service recording, pull the radar's 5 V
wire. Expected within about 5 s: `radar-cam-recorder status --config
/etc/radar-cam-recorder.toml` shows `degraded` / `radar missing`, and the session's
`events.jsonl` gets a state event (`degraded`, `radar missing`) and one `radar silent`
incident (`silent_s` about 5) for the whole outage. Plug it back in: status returns to
`recording`. Check with `journalctl -u radar-cam-recorder -f`.

4 (power-cut half, pending, needs physical access): while recording, pull the Pi's power.
After boot, `journalctl -u radar-cam-recorder -b` and `radar-cam-recorder status` should
show `recording` in a new session, the previous session `interrupted` in its
`session.json`, and `radar-cam-recorder inspect ~/recordings/<previous>` should open it
(a partial last radar line and truncated `meta.json` tolerated).

## Leftovers on the Pi

`~/accept/` (about 180 MB of test recordings and configs) can be deleted. Service sessions
`s000001`-`s000007` are in `~/recordings` (never deleted).

# LEDs and button: acceptance (2026-10-21, branch `feat/leds-button`)

Pi: homegateway (Zero W, kernel 6.12.25+rpt-rpi-v6, systemd 252). The kernel setup from the
spike (`docs/leds-button-spike.md`) was already installed. No LEDs or button are wired:
LED levels were read with `pinctrl get 17` / `pinctrl get 27` (`hi`/`lo`, sampled every
0.25 s), and the button was driven with the signals `SIGUSR1` (single press) and `SIGUSR2`
(double press), always `sudo systemctl kill --kill-whom=main -s USR1|USR2 radar-cam-recorder`.

## Deploy

```
rsync -a --exclude .venv --exclude .git --exclude recordings/ --exclude .superpowers/ \
  --exclude __pycache__ --exclude '*.egg-info' ./ homegateway:workspace/radar-cam-recorder/
ssh homegateway 'cd ~/workspace/radar-cam-recorder && UV_PYTHON=/usr/bin/python3 uv sync'
  -> Resolved 28 packages / Checked 11 packages
sudo cp deploy/radar-cam-recorder.service /etc/systemd/system/ && sudo systemctl daemon-reload \
  && sudo systemctl restart radar-cam-recorder
systemctl show radar-cam-recorder -p Type,WatchdogUSec,MainPID  -> Type=notify WatchdogUSec=30s
```

`/etc/radar-cam-recorder.toml` has no `[control]`/`[leds]` tables: the defaults apply
(`start_mode = "idle"`, `button_name = "button@3"`). Test suite on the Pi
(`uv run pytest -q`): **229 passed in 68.10 s**, 0 failures.

## Software-only checks

1. Start -> idle: PASS. After the restart: `status` -> `{"state": "idle", "reason": "idle"}`;
   `pinctrl get 17` hi in 8/8 samples (green steady), `pinctrl get 27` lo (red off).
2. `USR2` -> recording: PASS. Journal `command toggle accepted (idle)`, then
   `state: recording`; session `s000019` `in_progress`. Green: mostly lo with short hi pulses
   (`G17: lo hi lo lo lo lo lo lo lo lo lo lo hi lo lo lo`), red lo.
3. `USR1` -> split: PASS. `s000019` -> `complete` (`stop_reason = "split by button"`), a new
   session `s000020` `in_progress`; journal `command split accepted (recording)`. Measured
   gap across the split (`segment gap` incident in `s000020`): **gap_ns = 4 725 070 000 (4.73 s)**,
   from the last frame of `s000019` (sensor ts 3548.334 s) to the first of `s000020`
   (3553.059 s). Slightly above the 3-4 s rotation gap, as the spec predicted.
4. `USR2` -> idle: PASS. `s000020` `complete` (`stop_reason = "stopped by button"`; since renamed
   `"stopped by command"`),
   status `idle`, green steady hi, red lo.
5. Green dark within 5 s of `SIGSTOP`: PASS. Recording (green pulsing), then
   `systemctl kill --kill-whom=main -s STOP` at 23:59:40. G17 was lo in every 1 s sample from
   t+0 to t+15 s and in a further 12 x 0.25 s samples; no pulse after the stop (the pulse is a
   finite kernel pattern, so it ends on its own; the sampling cannot catch a single short pulse
   at t+0).
6. Watchdog restart: PASS. `00:00:10 Watchdog timeout (limit 30s)!` (30 s after the stop),
   SIGABRT, `Failed with result 'watchdog'`, restart after 5 s, new pid 6692 (old 4615), no
   stray `rpicam-vid`. The session in progress, `s000021`, was marked `interrupted`; status:
   `{"state": "idle", "reason": "idle after a problem", "detail": {"problem": true}}`. Levels:
   green hi steady, red alternating (`lo lo lo hi hi lo lo hi hi lo lo hi`, 0.25 s samples: slow blink).
7. `SIGKILL` of the recorder: PASS. `systemctl kill --kill-whom=main -s KILL` at 00:00:54:
   `pinctrl` right after: G17 lo, **R27 hi** for the whole 5 s `activating (auto-restart)` window
   (ExecStopPost: red solid, green off); after the restart `idle after a problem` again with
   green hi and red blinking. `NRestarts=2`.
8. Clean stop (R8): `sudo systemctl stop` -> G17 lo and R27 lo (ExecStopPost turned both LEDs
   off). `systemctl start` afterwards: `idle` (no warning, as the last state was `stopped`).

Final state: service `active`, status `idle`, no recorder session `in_progress`, no stray
`rpicam-vid`.

Observations (not product changes):

- Camera flake: the first `rpicam-vid` start after a period without the camera often times
  out (`Camera frontend has timed out!`, reproduced by hand outside the recorder: first run
  failed, next two worked). In the first two recording runs this produced `camera stalled`
  (stall watchdog, detected at about 10.2 s), then `degraded` / `camera missing` for about
  1-5 s and `recording` again; those sessions (`s000017`, `s000018`) have 0 video frames
  because they were closed within seconds after the recovery. The measured split above
  (`s000019` -> `s000020`) had a healthy camera (854 frames, 109 drops, max 120 ms).
- The earlier service run showed `degraded` / `recording` flapping about every 70 s before the
  redeploy (journal 23:45-23:47): the same camera flake.
- Test sessions `s000017`-`s000021` remain on disk. `s000021` is `interrupted` (watchdog
  test); `s000014` was already `interrupted` before this work.

## Hands-on checks: PENDING (need the parts wired, spec section 7)

Wire as in the README (green GPIO 17 / pin 11, red GPIO 27 / pin 13, button GPIO 3 / pin 5,
GND pins 9, 14, 6). Run `radar-cam-recorder status --config /etc/radar-cam-recorder.toml`
and `journalctl -u radar-cam-recorder -f` over SSH while you test.
These checks describe button-driven use (`[control] start_mode = "idle"`). With the default
`"record"`, a start records at once instead of going idle; the warning after a problem is then
only visible after a double press back to idle.

1. Boot: power the Pi. Green blinks at 1 Hz from kernel start, then fast-blinks while the recorder is `starting`, then steady (idle), red off.
2. Double press: recording starts, green pulses, a new session appears and is `in_progress`.
   Double press again: that session becomes `complete`, back to idle.
3. Single press while recording: the session closes `complete` and a new one opens; run the
   README "Copying sessions" section while recording continues and check the closed session arrives.
4. Long press, 5 s, with the recorder frozen (`sudo systemctl kill --kill-whom=main -s STOP
   radar-cam-recorder`). The watchdog restarts a frozen recorder about 30 s after the STOP, so
   start the hold within about 25 s of sending it. (The red fast blink after 5 s appears only when the recorder runs;
   frozen, it cannot.) Expected: the Pi powers off (logind), the LEDs go dark (to confirm that
   `gpio-leds` turns them off at halt, R8), and then one press on the button powers it back on
   (wake from halt on GPIO 3). Time the hold with a stopwatch from button down to the Pi powering off (green/red off, SSH
   dropped): expect about 5 s (logind's threshold is 5 s from memory).
   Repeat once with the recorder healthy: red fast blink after 5 s, then poweroff.
5. Recording, then `sudo systemctl kill --kill-whom=main -s STOP radar-cam-recorder`: green
   dark within 5 s; after about 30 s the watchdog restarts the recorder; it returns idle with
   the red slow blink; any press clears the warning.
6. `sudo systemctl kill --kill-whom=main -s KILL radar-cam-recorder`: red solid for the 5 s
   restart delay, then idle with the red warning blink.
7. Missing devices. The kernel `gpio-led` / `gpio-key` devices stay registered when a wire is
   unplugged, so exercise the missing-device path through the config instead:
   `sudo cp /etc/radar-cam-recorder.toml /etc/radar-cam-recorder.toml.bak-absent`, then in
   `/etc/radar-cam-recorder.toml` set `green = "absent"` inside the existing `[leds]` table and
   `button_name = "absent"` inside the existing `[control]` table (or add those tables if they
   are missing; never a second copy of a table), run `radar-cam-recorder check-config --config
   /etc/radar-cam-recorder.toml`, then `sudo systemctl restart radar-cam-recorder`. Expected:
   exactly one WARNING for the green LED (`LED absent: ...`) and one for the button
   (`button 'absent' not found; running without it`) in `journalctl -u radar-cam-recorder`;
   recording is unaffected (`USR2` / `USR1` still toggle and split; the red LED still works).
   Revert: `sudo mv /etc/radar-cam-recorder.toml.bak-absent /etc/radar-cam-recorder.toml` and
   restart the service.
8. Unplugged parts (power off first for the wiring): with an LED or the button unplugged the
   recorder keeps working and the signals still toggle recording (no log is expected: the
   kernel devices are still there).

Not to run remotely: `poweroff`, `halt`, `shutdown -h`; nobody can power the Pi back on.
