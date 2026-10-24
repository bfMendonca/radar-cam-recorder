# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Status LEDs (green GPIO 17, red GPIO 27) and one button (GPIO 3) through kernel devices:
  `gpio-led` / `gpio-key` overlays, udev rule, logind long-press poweroff.
- `[control] start_mode`: `"record"` (default, as before: record at start) or `"idle"`
  (start idle and wait for a double press or `USR2`). Single press splits the session,
  double press starts or stops.
- `SIGUSR1` (split) and `SIGUSR2` (toggle) for remote control.
- Systemd `Type=notify` with a 30 s watchdog, and `ExecStopPost` script showing a crash
  on the red LED. A crash ends in the `error` state (session closed `complete`), so the
  next start warns.
- `[control]` and `[leds]` config tables.
- Pi acceptance for the LEDs and button in `docs/pi-acceptance.md`.
- GitHub CI (lint, `mypy --strict`, tests on Python 3.9-3.13, x86-64 and ARM64), issue and
  pull request templates, `CONTRIBUTING.md`, `SECURITY.md` and the MIT `LICENSE` file.

### Changed

- The `ld2451` driver is installed from <https://github.com/bfMendonca/ld2451> instead of a
  sibling checkout.
- The example systemd unit and config use user `pi` and `/home/pi/...`: adjust them to your install.

## [0.1.0] - 2026-10-20

### Added

- Supervisor that records camera video and HLK-LD2451 radar data into numbered sessions,
  rotating both into time-aligned segments.
- Camera capture through `rpicam-vid` with per-frame sensor timestamps, plus a fake camera.
- Radar stream recording raw frames with receive timestamps, optional detection parameters
  applied at start, and a simulated radar for development.
- Session format (`session.json`, `events.jsonl`, per-segment video, frames CSV and radar files).
- Disk-space policy with stop/resume thresholds and recovery of sessions left `in_progress`.
- Retry with exponential backoff for camera and radar failures, reported as incidents.
- Live status file and log output.
- Session reader with `summary()`, frame lookup and a merged frame/radar timeline.
- CLI: `run`, `status`, `check-config`, `inspect`.
- systemd unit and example configuration under `deploy/`.
