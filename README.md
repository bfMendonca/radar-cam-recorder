# radar-cam-recorder

Unattended recorder for a Raspberry Pi Zero with a camera (`rpicam-vid`) and an
HLK-LD2451 24 GHz radar on the serial port. It starts at boot, writes video and radar
data into numbered sessions with a shared monotonic clock, and survives device
failures, full disks and power cuts. The radar is read through the
[`ld2451`](../driver-hlk-ld2451) driver.

## Install

### On the Mac (development)

```sh
uv sync --all-groups
uv run pytest -q
```

The repo's `.python-version` is 3.9 for development. Try the whole pipeline without
hardware using the hidden `--fake` option (fake camera and loopback radar):

```sh
uv run radar-cam-recorder run --fake --config my-dev.toml
```

### On the Pi

Install uv (release 0.10.10, `arm-unknown-linux-musleabihf` build) into `~/.local/bin`,
then build the environment against the system Python (do not use the development pin):

```sh
mkdir -p ~/.local/bin
curl -sSfL https://github.com/astral-sh/uv/releases/download/0.10.10/uv-arm-unknown-linux-musleabihf.tar.gz | tar xz -C /tmp
install -m 755 /tmp/uv-arm-unknown-linux-musleabihf/uv ~/.local/bin/uv
uv --version
```

The repo's `.python-version` (3.9) is for the Mac. On the Pi, make uv use the system
Python 3.11 for every command by adding this to `~/.profile` (then log in again):

```sh
export UV_PYTHON=/usr/bin/python3
```

```sh
cd ~/workspace/radar-cam-recorder
uv sync               # the test group pins a pure-Python hypothesis: nothing to compile
uv run pytest -q      # slow on a Pi Zero: allow several minutes
```

The service must run from this environment (`.venv/bin/radar-cam-recorder`).

## Configuration

Copy `deploy/config.example.toml` to `/etc/radar-cam-recorder.toml` and edit it; every key
is documented inline. Only `storage.recordings_dir` is required, and the directory must
exist. Check what the recorder will use:

```sh
radar-cam-recorder check-config --config /etc/radar-cam-recorder.toml
```

Set `camera.enabled = false` or `radar.enabled = false` to record one device only.

`camera.stall_timeout_s` (default 10 s) is the frame watchdog: if `rpicam-vid` stays alive
but its frame metadata stops growing for that long, the recorder logs a `camera stalled`
incident, stops the camera and restarts it as a camera failure (state `degraded`,
`camera missing`, retried with backoff). A new run gets the same time to deliver its
first frame.

## Service

```sh
sudo cp deploy/radar-cam-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radar-cam-recorder
journalctl -u radar-cam-recorder -f
radar-cam-recorder status --config /etc/radar-cam-recorder.toml
```

The unit is `Type=notify` with a 30 s watchdog and an `ExecStopPost` LED script; see
"Status LEDs and button". Adjust `User=`, `WorkingDirectory=` and `ExecStart=` to your install path.

`KillMode=mixed` matters: with the default (control-group) mode systemd would send
SIGTERM to `rpicam-vid` as well, preempting the recorder's own clean stop (SIGINT, so
the video file is finalised). With `mixed` only the recorder gets SIGTERM; any leftover
process is SIGKILLed after `TimeoutStopSec`.

## Status LEDs and button

A green and a red LED and one push button on the Pi's GPIO header show the recorder state
and control it without a laptop. All are kernel devices (no Python GPIO library).

### Wiring

| Part | GPIO | Header pin | Wiring |
| --- | --- | --- | --- |
| Green LED | 17 | 11 | GPIO -> 330 ohm -> LED anode, cathode -> GND (pin 9) |
| Red LED | 27 | 13 | GPIO -> 330 ohm -> LED anode, cathode -> GND (pin 14) |
| Button | 3 | 5 | GPIO 3 -> button -> GND (pin 6) |

GPIO 3 has a fixed on-board pull-up, and grounding it wakes a halted Pi, so the same
button powers the Pi back on after a long-press shutdown (to be confirmed by the hands-on
check in `docs/pi-acceptance.md`). Consequence: keep `i2c_arm` disabled in `config.txt`.

### Kernel setup (once, on the Pi)

Files under `deploy/`; the logind drop-in goes first:

```sh
sudo install -D -m 644 deploy/logind-rcr-button.conf /etc/systemd/logind.conf.d/logind-rcr-button.conf
sudo systemctl restart systemd-logind
sudo install -m 644 deploy/rcr-leds.conf /etc/modules-load.d/rcr-leds.conf
sudo install -m 644 deploy/99-rcr-leds.rules /etc/udev/rules.d/99-rcr-leds.rules
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak-rcr
grep -q rcr-green /boot/firmware/config.txt || \
  sudo sh -c 'cat deploy/config.txt.snippet >> /boot/firmware/config.txt'
sudo reboot
```

- `config.txt.snippet`: two `gpio-led` overlays (`rcr-green` on GPIO 17 with a 1 Hz `timer`
  blink from kernel start, `rcr-red` on GPIO 27) and a `gpio-key` overlay on GPIO 3
  (active low, pull-up, `KEY_POWER`). The input device is named `button@3`.
- `rcr-leds.conf` loads the `ledtrig-pattern` module.
- `99-rcr-leds.rules` lets group `gpio` write the `rcr-*` LED attributes (the service user
  must be in groups `gpio` and `input`).
- `logind-rcr-button.conf`: `HandlePowerKey=ignore`, `HandlePowerKeyLongPress=poweroff`.
  logind powers the Pi off after a 5 s hold by itself, even if the recorder is hung.
- `rcr-leds-stop.sh` is the unit's `ExecStopPost`: red solid after a crash or kill
  (`$SERVICE_RESULT` not `success`); after a clean stop both LEDs go off, except during
  system poweroff, when the "shutting down" blink is left running.

The unit is `Type=notify` with `WatchdogSec=30`: the recorder sends `READY=1` and a
watchdog ping each tick, and a hung main loop is killed and restarted by systemd. Copy the
updated unit as in "Service" above.

### Modes and the button

The recorder starts **idle** (running, not recording) on every start; set
`[control] start_mode = "record"` to record at start for unattended use.

**Behaviour change:** earlier versions recorded as soon as the service started. After
upgrading, an unattended install stays idle and records nothing until a double press (or
`USR2`). To record at start again, add this new table to `/etc/radar-cam-recorder.toml`
(the file has no `[control]` table unless you added one) and restart the service:

```toml
[control]
start_mode = "record"
```

```sh
sudo systemctl restart radar-cam-recorder
```

| Press | In idle | While recording or degraded | Waiting for devices, disk full, storage missing |
| --- | --- | --- | --- |
| Single | rejected | close the session (`complete`, safe to copy) and open a new one | rejected |
| Double | start recording | stop, back to idle | stop, back to idle |
| Hold 5 s | Pi powers off (done by logind) | same | same |

In `starting`, `stopping` and `error`, or while a split, start or stop is still running,
presses are rejected. Single < 1 s with no second press within 400 ms; double = a second
press within 400 ms; holds between 1 s and 5 s are ignored. Timings are in `[control]`.

The same commands as signals, for SSH and testing (`SIGUSR1` = single, `SIGUSR2` =
double). Always use `--kill-whom=main`: a plain `systemctl kill` also signals
`rpicam-vid`, and SIGUSR2 makes it quit.

```sh
sudo systemctl kill --kill-whom=main -s USR1 radar-cam-recorder   # split
sudo systemctl kill --kill-whom=main -s USR2 radar-cam-recorder   # start / stop
```

### LED meaning

| Meaning (state) | Green | Red |
| --- | --- | --- |
| Kernel boot (before the recorder runs) | 1 Hz blink (timer) | off |
| Recorder `starting` | fast blink | off |
| Idle, ready | steady on | off |
| Idle after a problem (crash, kill, watchdog, interrupted session) until the next press | steady on | slow blink |
| Recording, all OK | pulses while data reaches the disk | off |
| Recording, a device missing (`degraded`) | pulses while data arrives | 1 blink per cycle = camera missing, 2 = radar missing |
| Waiting for both devices | off | 1 + 2 blinks alternating |
| Storage full or missing | slow blink, alternating with red | slow blink |
| Broken, needs a laptop (`error`, crash) | off | solid |
| Shutting down (`stopping`, and during poweroff; also shown when the recorder is `stopped`) | fast blink | fast blink |
| Off: safe to unplug | off | off |

Green never claims "recording" without data: the recorder re-arms a finite kernel pulse
whenever the camera or radar makes progress, so green goes dark within 5 s if the recorder
freezes or dies. Brief overlays: press accepted = both LEDs flash once; rejected = red
flashes twice; hold past 5 s = red fast blink until power-off. LEDs and button are optional:
a missing device is logged once and ignored.

## Commands

| Command | Purpose |
| --- | --- |
| `run [--config PATH] [-v]` | Idle until a command (or record at start with `start_mode = "record"`); runs until SIGTERM/SIGINT; exit 0 after a signal stop, 1 on config error or crash. |
| `status [--config PATH]` | Print the live status file (`recording.status_file`); exit 1 if missing. |
| `check-config [--config PATH]` | Print the parsed configuration as JSON. |
| `inspect SESSION_DIR [--json]` | Print the session summary (frames, fps, gaps, radar reports, overlap). |

The default config path is `/etc/radar-cam-recorder.toml`.

## Session format

Each run creates `recordings_dir/sNNNNNN/` containing:

- `session.json`: metadata, devices, clock samples and `state` (`in_progress`,
  `complete`, `interrupted`, ...);
- `events.jsonl`: state changes and incidents (`camera exited`, `camera stalled`,
  `segment gap`, `frame drops`, `radar silent`, ...);
- per segment: the H.264 video, a CSV of per-frame sensor timestamps, and the raw radar
  capture (`t_rx_ns,hex` lines).

Each closed segment in `session.json` also records `drops` (frame intervals longer than
1.5 frame periods) and `max_gap_ns` (its longest frame interval).

Timestamps: the radar's `t_rx_ns` is `CLOCK_BOOTTIME`, read by the recorder when the
bytes arrive. The camera's `SensorTimestamp` is the kernel's frame timestamp; on a Pi,
which never suspends, it equals `CLOCK_BOOTTIME`, and the clock samples in
`session.json` (boot, monotonic and wall clock read together) allow a correction if
that ever differs. A session left `in_progress` by a crash or power cut is marked
`interrupted` once the recordings dir is usable again (before the next session opens).

## Reading sessions

```python
from pathlib import Path
from radar_cam_recorder.reader import open_session

session = open_session(Path("/home/bmendonca/recordings/s000001"))
print(session.summary())
for item in session.timeline():   # video frames and radar reports in time order
    ...
frame = session.frame_at(ts_ns)   # nearest video frame to a timestamp, or None
```

`timeline()` already shifts radar reports by `radar_latency_ns`; `frame_at()` does not,
so for a radar report pass
`report.t_rx_ns - session.metadata["radar_latency_ns"]`.

## Copying sessions

Skip sessions that are still being written (`state` is `in_progress`) or whose
`session.json` is unreadable. The exclude list is built on the Pi by parsing the JSON:

```sh
PI=homegateway
skip=$(mktemp)
ssh "$PI" 'python3 -c "import json,pathlib
for p in sorted(pathlib.Path.home().joinpath(\"recordings\").glob(\"*/session.json\")):
    try:
        if json.loads(p.read_text()).get(\"state\") == \"in_progress\": print(p.parent.name + \"/\")
    except (OSError, ValueError): print(p.parent.name + \"/\")"' > "$skip"
rsync -av --exclude-from="$skip" "$PI":recordings/ ./recordings/
rm -f "$skip"
```

## Known limits

- Each segment rotation leaves a gap of about 3 s in the video (`segment gap` incident):
  measured on the Pi over 26 rotations of 300 s segments, 2.5-3.8 s, median 3.0 s. It is
  the time `rpicam-vid` takes to stop and to deliver the first frame of the next run.
- In-segment frame drops are normal at the default 1296x972@25 (about 300 per 300 s
  segment, longest usually 0.1-1 s): one `frame drops` incident per segment.
- Right after boot the camera stack needs about 10 s: expect one `camera exited` (or
  `camera stalled`) incident and a short `degraded` spell before recording settles.
- Video and radar are aligned to about 50 ms.
- The Pi Zero has no RTC: wall-clock times are unreliable until NTP syncs, so rely on
  the monotonic timestamps and the recorded clock samples.
