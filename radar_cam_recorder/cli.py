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
from typing import Any

from radar_cam_recorder.button import ButtonReader, Gesture, GestureClassifier, find_button
from radar_cam_recorder.camera.base import CameraSource
from radar_cam_recorder.clocks import boot_clock
from radar_cam_recorder.config import Config, ConfigError, config_to_dict, load_config
from radar_cam_recorder.leds import LedController, LedSink, SysfsLed
from radar_cam_recorder.notify import notify
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

    # Must be read before anything (the bus announce, StatusFileSink) rewrites the file.
    unclean = previous_run_unclean(cfg.status_file)
    bus = StatusBus(clock)
    bus.add_sink(LogSink())
    controller: LedController | None = None
    if cfg.leds.enabled and not fake:
        controller = LedController(SysfsLed(cfg.leds.green), SysfsLed(cfg.leds.red), clock)
        bus.add_sink(LedSink(controller))
    bus.add_sink(StatusFileSink(cfg.status_file))

    def on_command(command: str, accepted: bool) -> None:
        if controller is not None:
            controller.flash("accepted" if accepted else "rejected")

    supervisor = Supervisor(
        cfg,
        bus=bus,
        camera=camera,
        radar=radar,
        clock=clock,
        warn_at_start=unclean,
        on_progress=controller.progress if controller is not None else None,
        on_command=on_command,
    )

    stop = threading.Event()

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        stop.set()  # the supervisor shuts down after its current tick

    def on_usr1(signum: int, _frame: FrameType | None) -> None:
        supervisor.request("split")

    def on_usr2(signum: int, _frame: FrameType | None) -> None:
        supervisor.request("toggle")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGUSR1, on_usr1)
    signal.signal(signal.SIGUSR2, on_usr2)

    def on_gesture(gesture: Gesture) -> None:
        if gesture is Gesture.SINGLE:
            supervisor.request("split")
        elif gesture is Gesture.DOUBLE:
            supervisor.request("toggle")
        elif gesture is Gesture.LONG_ARMED and controller is not None:
            controller.flash("long_armed")

    reader: ButtonReader | None = None
    if not fake:
        device = find_button(cfg.control.button_name)
        if device is None:
            log.warning("button %r not found; running without it", cfg.control.button_name)
        else:
            reader = ButtonReader(device, GestureClassifier(cfg.control), on_gesture, clock)
            reader.start()

    def heartbeat() -> None:
        notify("WATCHDOG=1")

    try:
        notify("READY=1")
        supervisor.run(stop, heartbeat=heartbeat)
    finally:
        if reader is not None:
            reader.stop()
        bus.close()
    return 0


def previous_run_unclean(status_file: Path) -> bool:
    """True when the last run left a status file whose state is not ``stopped``."""
    try:
        data: Any = json.loads(status_file.read_text())
    except (OSError, ValueError):
        return False
    state = data.get("state") if isinstance(data, dict) else None
    return isinstance(state, str) and state != "stopped"


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
