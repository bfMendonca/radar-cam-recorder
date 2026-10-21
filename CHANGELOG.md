# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
