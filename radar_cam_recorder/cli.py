"""Command line: ``run``, ``status``, ``check-config``, ``inspect``."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from types import FrameType

from radar_cam_recorder.camera.base import CameraSource
from radar_cam_recorder.clocks import boot_clock
from radar_cam_recorder.config import Config, ConfigError, config_to_dict, load_config
from radar_cam_recorder.radar import RadarStream
from radar_cam_recorder.reader import open_session
from radar_cam_recorder.status import LogSink, StatusBus, StatusFileSink
from radar_cam_recorder.supervisor import Supervisor

DEFAULT_CONFIG = "/etc/radar-cam-recorder.toml"

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-cam-recorder")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config", default=DEFAULT_CONFIG, help="config file (default: %(default)s)"
        )

    run = sub.add_parser("run", help="record until stopped by SIGTERM/SIGINT")
    add_config(run)
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    run.add_argument("--fake", action="store_true", help=argparse.SUPPRESS)
    add_config(sub.add_parser("status", help="print the live status file"))
    add_config(sub.add_parser("check-config", help="print the parsed config as JSON"))
    inspect = sub.add_parser("inspect", help="summarise a recorded session")
    inspect.add_argument("session_dir", type=Path)
    inspect.add_argument("--json", action="store_true", help="print JSON")
    return parser


def _run(cfg: Config, fake: bool) -> int:
    clock = boot_clock()
    camera: CameraSource | None = None
    radar: RadarStream | None = None
    if fake:
        from radar_cam_recorder.camera.fake import FakeCamera
        from radar_cam_recorder.fakes import fake_radar_transport

        if cfg.camera.enabled:
            camera = FakeCamera(clock)
        if cfg.radar.enabled:
            radar = RadarStream(cfg.radar, transport_factory=fake_radar_transport)
    else:
        from radar_cam_recorder.camera.rpicam import RpicamCamera

        if cfg.camera.enabled:
            camera = RpicamCamera(cfg.camera)
        if cfg.radar.enabled:
            radar = RadarStream(cfg.radar)

    bus = StatusBus(clock)
    bus.add_sink(LogSink())
    bus.add_sink(StatusFileSink(cfg.status_file))
    supervisor = Supervisor(cfg, bus=bus, camera=camera, radar=radar, clock=clock)

    stop = threading.Event()

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        stop.set()  # the supervisor shuts down after its current tick

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        supervisor.run(stop)
    finally:
        bus.close()
    return 0


def _status(cfg: Config) -> int:
    try:
        text = cfg.status_file.read_text()
    except FileNotFoundError:
        print(f"error: no status file at {cfg.status_file}", file=sys.stderr)
        return 1
    print(text)
    return 0


def _inspect(session_dir: Path, as_json: bool) -> int:
    summary = open_session(session_dir).summary()
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
        )
    try:
        if args.command == "inspect":
            return _inspect(args.session_dir, args.json)
        cfg = load_config(Path(args.config))
        if args.command == "check-config":
            print(json.dumps(config_to_dict(cfg), indent=2))
            return 0
        if args.command == "status":
            return _status(cfg)
        return _run(cfg, args.fake)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        if args.command != "run":
            raise
        log.exception("recorder crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
