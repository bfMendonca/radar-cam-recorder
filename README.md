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

```sh
uv sync --python /usr/bin/python3
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

## Service

```sh
sudo cp deploy/radar-cam-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radar-cam-recorder
journalctl -u radar-cam-recorder -f
radar-cam-recorder status --config /etc/radar-cam-recorder.toml
```

Adjust `User=`, `WorkingDirectory=` and `ExecStart=` to your install path.

`KillMode=mixed` matters: with the default (control-group) mode systemd would send
SIGTERM to `rpicam-vid` as well, preempting the recorder's own clean stop (SIGINT, so
the video file is finalised). With `mixed` only the recorder gets SIGTERM; any leftover
process is SIGKILLed after `TimeoutStopSec`.

## Commands

| Command | Purpose |
| --- | --- |
| `run [--config PATH] [-v]` | Record until SIGTERM/SIGINT; exit 0 after a signal stop, 1 on config error or crash. |
| `status [--config PATH]` | Print the live status file (`recording.status_file`); exit 1 if missing. |
| `check-config [--config PATH]` | Print the parsed configuration as JSON. |
| `inspect SESSION_DIR [--json]` | Print the session summary (frames, fps, gaps, radar reports, overlap). |

The default config path is `/etc/radar-cam-recorder.toml`.

## Session format

Each run creates `recordings_dir/sNNNNNN/` containing:

- `session.json`: metadata, devices, clock samples and `state` (`in_progress`,
  `complete`, `interrupted`, ...);
- `events.jsonl`: state changes and incidents;
- per segment: the H.264 video, a CSV of per-frame sensor timestamps, and the raw radar
  capture (`t_rx_ns,hex` lines).

All timestamps are on the boot-relative monotonic clock. A session left `in_progress`
by a crash or power cut is closed on the next start.

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

- Each segment rotation leaves a 1-2 s gap in the video.
- Video and radar are aligned to about 50 ms.
- The Pi Zero has no RTC: wall-clock times are unreliable until NTP syncs, so rely on
  the monotonic timestamps and the recorded clock samples.
