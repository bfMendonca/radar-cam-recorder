"""The real camera: one ``rpicam-vid`` subprocess per segment."""

from __future__ import annotations

import signal
import subprocess
from typing import IO, Any

from radar_cam_recorder.config import CameraConfig
from radar_cam_recorder.session import SegmentPaths


def build_command(cfg: CameraConfig, paths: SegmentPaths) -> list[str]:
    return [
        cfg.binary,
        "-n",
        "-t",
        "0",
        "--width",
        str(cfg.width),
        "--height",
        str(cfg.height),
        "--framerate",
        str(cfg.framerate),
        "--bitrate",
        str(cfg.bitrate),
        "--codec",
        "h264",
        "--inline",
        "-o",
        str(paths.video),
        "--save-pts",
        str(paths.pts),
        "--metadata",
        str(paths.meta),
        "--metadata-format",
        "json",
        *cfg.extra_args,
    ]


class RpicamRun:
    def __init__(self, proc: subprocess.Popen[bytes], log_file: IO[bytes]) -> None:
        self._proc = proc
        self._log = log_file

    @property
    def returncode(self) -> int | None:
        code = self._proc.poll()
        if code is not None:
            self._log.close()
        return code

    def alive(self) -> bool:
        return self.returncode is None

    def stop(self, timeout: float) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.send_signal(signal.SIGINT)
                try:
                    self._proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
        finally:
            self._log.close()


class RpicamCamera:
    def __init__(self, cfg: CameraConfig) -> None:
        self._cfg = cfg

    def start(self, paths: SegmentPaths) -> RpicamRun:
        log_file = open(paths.camera_log, "ab")  # noqa: SIM115 - closed by RpicamRun
        try:
            proc = subprocess.Popen(
                build_command(self._cfg, paths),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            log_file.close()
            raise
        return RpicamRun(proc, log_file)

    def describe(self) -> dict[str, Any]:
        cfg = self._cfg
        return {
            "backend": "rpicam-vid",
            "binary": cfg.binary,
            "width": cfg.width,
            "height": cfg.height,
            "framerate": cfg.framerate,
            "bitrate": cfg.bitrate,
            "extra_args": list(cfg.extra_args),
        }
