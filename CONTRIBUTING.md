# Contributing

Thanks for your interest in `radar-cam-recorder`! Bug reports, field results from real
installations and pull requests are all welcome.

## Setup

The project uses [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/bfMendonca/radar-cam-recorder.git
cd radar-cam-recorder
uv sync --all-groups
```

The [`ld2451`](https://github.com/bfMendonca/ld2451) driver is installed from GitHub. To
work on both at once, clone the driver next to this repo and install it over the locked
copy (the next `uv sync` puts the GitHub version back):

```sh
uv pip install -e ../ld2451
```

## Before opening a pull request

```sh
uv run pytest                      # no hardware needed
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Try the whole pipeline without hardware with the hidden `--fake` option (fake camera and
loopback radar): `uv run radar-cam-recorder run --fake --config my-dev.toml`.

- Keep the runtime dependencies to `ld2451` (and `tomli` on Python < 3.11).
- No Python GPIO library: LEDs and the button go through kernel devices.
- Changes to the session format must stay readable by `radar_cam_recorder.reader`.
- Add an entry under *Unreleased* in `CHANGELOG.md`.

## Hardware checks

The test suite needs no hardware (fake camera, loopback radar, fake sysfs LEDs and input
device). Changes that touch the camera, radar, LEDs, button or the systemd unit should also
be checked on a Pi; the hands-on checklist and past results are in
[`docs/pi-acceptance.md`](docs/pi-acceptance.md).
