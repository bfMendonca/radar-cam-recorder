# Pi acceptance results (2026-10-20)

Target: Raspberry Pi Zero W (ARMv6), Raspbian 12, system Python 3.11.2, OV5647 camera
(`rpicam-vid`), HLK-LD2451 radar on `/dev/serial0` (firmware V1.13.25070114, direction
"both"). Config used `[radar.detection]` UNSET (radar settings untouched) and
`[setup] notes = "acceptance run 2026-10-20, indoor"`. Recordings went to
`/home/bmendonca/recordings`. Branch `feat/recorder-v1`.

Summary: acceptance 1, 2, 3, 5, 6 and the SIGKILL half of 4 pass. Acceptance 7 and the
power-cut half of 4 are pending (need physical access). Two real findings, below.

## Findings (not patched, library code untouched)

1. **Camera stall is not detected (acceptance 2 / robustness).** Twice out of about eight
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
2. **Test suite timing on the Pi (Task 2):** 125 passed, 1 failed, 2 errors, all timing
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
3. **Dependency install on ARMv6:** `uv sync` as written fails to be practical. The lock
   resolves `hypothesis` 6.170.5 for Python >= 3.11, which ships a Rust extension with no
   ARMv6 wheel; uv starts compiling it from source with a Rust toolchain (it ran for
   minutes and used a large part of the 427 MB RAM; I stopped it). Workaround used:
   `uv sync --python /usr/bin/python3 --no-group test`, then
   `uv pip install pytest "hypothesis==6.141.1"` (pure Python). The README Pi steps
   should say so or the lock should cap hypothesis for the test group. Also, the repo's
   `.python-version` (3.9) makes plain `uv run` refuse the venv on the Pi, so commands were
   run as `.venv/bin/radar-cam-recorder ...` and `.venv/bin/python -m pytest`.

## Step 1: install

```
curl ... uv-arm-unknown-linux-musleabihf.tar.gz | tar xz -C /tmp ; install -m 755 ... ~/.local/bin/uv
uv --version                       -> uv 0.10.10
rsync -a (excluding .venv, caches, .superpowers, .DS_Store) both projects to ~/workspace/
uv sync --python /usr/bin/python3 --no-group test   (1m25s) + uv pip install pytest hypothesis==6.141.1
.venv/bin/python -c "import ld2451, radar_cam_recorder, hypothesis"   -> ok (Python 3.11.2)
```

## Step 2: tests on the Pi

`.venv/bin/python -m pytest -q` -> `1 failed, 125 passed, 2 errors in 131.74s`
(see finding 2: all three are timing assumptions in the tests, behaviour verified by hand).

## Step 3: camera modes (acceptance 8)

Shortened to 3 minutes per run (instead of 10) and `segment_s = 60`, radar recording, via
`radar-cam-recorder run` with a test config, then `inspect --json`. The runs were stopped
with `timeout --foreground -s TERM 185` (first attempt without `--foreground` also sent
TERM to `rpicam-vid`, which truncated the last segment; that run was discarded for
numbers except for the segment-0 stall in finding 1).

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

Median frame interval is 40 ms (25 fps) in both, so frames are dropped in bursts rather
than the clock running slow; neither mode sustains the full 25 fps on the Pi Zero.
1296x972 delivers more frames (21.2 vs 16.8 fps), so it stays the default and
`deploy/config.example.toml` is unchanged. The first test run (discarded) also showed
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
normal thresholds (1024 / 1536). (A first attempt was aborted because my own tooling
truncated `/etc/radar-cam-recorder.toml`; the file was restored from the repo example.)

## Step 9: acceptance 6: PASS; acceptance 7: PENDING

6: `sudo pkill -INT rpicam-vid` -> incident `camera exited` (returncode 0, with the last
camera log lines), state `degraded / camera missing`, back to `recording` about 2 s later
in a new segment (`segment gap` incident, 3.9 s). Only one incident per failure.

7 (pending, needs physical access): with the service recording, pull the radar's 5 V
wire. Expected within about 5 s: `radar-cam-recorder status --config
/etc/radar-cam-recorder.toml` shows `degraded` / `radar missing`, and an incident in the
session's `events.jsonl`. Plug it back in: status returns to `recording`. Check with
`journalctl -u radar-cam-recorder -f`.

4 (power-cut half, pending, needs physical access): while recording, pull the Pi's power.
After boot, `journalctl -u radar-cam-recorder -b` and `radar-cam-recorder status` should
show `recording` in a new session, the previous session `interrupted` in its
`session.json`, and `radar-cam-recorder inspect ~/recordings/<previous>` should open it
(a partial last radar line and truncated `meta.json` tolerated).

## Leftovers on the Pi

`~/accept/` (about 180 MB of test recordings and configs) can be deleted. Service sessions
`s000001`-`s000007` are in `~/recordings` (never deleted).
