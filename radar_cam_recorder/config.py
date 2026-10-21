"""Configuration loading and validation (TOML -> frozen dataclasses)."""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import ld2451

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

__all__ = [
    "CameraConfig",
    "Config",
    "ConfigError",
    "ControlConfig",
    "LedsConfig",
    "RadarConfig",
    "SetupConfig",
    "StorageConfig",
    "config_to_dict",
    "load_config",
    "parse_config",
]

_MB = 2**20
_DIRECTIONS = {
    "away": ld2451.Direction.AWAY_ONLY,
    "approach": ld2451.Direction.APPROACH_ONLY,
    "both": ld2451.Direction.BOTH,
}

T = TypeVar("T")


class ConfigError(ValueError):
    """The configuration is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class StorageConfig:
    recordings_dir: Path
    require_mountpoint: bool
    stop_below_bytes: int
    resume_above_bytes: int
    check_interval_s: float


@dataclass(frozen=True)
class CameraConfig:
    enabled: bool
    binary: str
    width: int
    height: int
    framerate: float
    bitrate: int
    extra_args: tuple[str, ...]
    stop_timeout_s: float
    stall_timeout_s: float


@dataclass(frozen=True)
class RadarConfig:
    enabled: bool
    port: str
    baudrate: int
    silence_timeout_s: float
    detection: ld2451.DetectionParams | None


@dataclass(frozen=True)
class SetupConfig:
    notes: str
    geometry: Mapping[str, float]


@dataclass(frozen=True)
class ControlConfig:
    start_mode: str
    button_name: str
    double_press_s: float
    max_press_s: float
    long_press_s: float
    debounce_s: float


@dataclass(frozen=True)
class LedsConfig:
    enabled: bool
    green: str
    red: str


@dataclass(frozen=True)
class Config:
    storage: StorageConfig
    camera: CameraConfig
    radar: RadarConfig
    setup: SetupConfig
    control: ControlConfig
    leds: LedsConfig
    segment_s: float
    retry_initial_s: float
    retry_max_s: float
    status_file: Path


def _table(data: Mapping[str, Any], name: str, allowed: set[str]) -> Mapping[str, Any]:
    raw = data.get(name, {})
    if not isinstance(raw, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    for key in raw:
        if key not in allowed:
            raise ConfigError(f"unknown key {key!r} in [{name}]")
    return raw


def _get(table: Mapping[str, Any], name: str, key: str, default: T, kind: type) -> T:
    """Return table[key] (or default), checked against ``kind``."""
    if key not in table:
        return default
    value = table[key]
    ok = isinstance(value, kind) and not (kind is not bool and isinstance(value, bool))
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)  # type: ignore[return-value]
    if not ok:
        raise ConfigError(f"[{name}] {key} must be of type {kind.__name__}")
    if kind is float and not math.isfinite(value):
        raise ConfigError(f"[{name}] {key} must be a finite number")
    return value  # type: ignore[no-any-return]


def _positive(name: str, key: str, value: float) -> None:
    if value <= 0:
        raise ConfigError(f"[{name}] {key} must be > 0")


def _parse_detection(raw: Any) -> ld2451.DetectionParams:
    keys = {"max_distance_m", "direction", "min_speed_kmh", "no_target_delay_s"}
    if not isinstance(raw, Mapping):
        raise ConfigError("[radar.detection] must be a table")
    for key in raw:
        if key not in keys:
            raise ConfigError(f"unknown key {key!r} in [radar.detection]")
    for key in sorted(keys - set(raw)):
        raise ConfigError(f"[radar.detection] {key} is required")
    direction = raw["direction"]
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise ConfigError(f"[radar.detection] direction must be one of {sorted(_DIRECTIONS)}")
    ints: dict[str, int] = {}
    for key in ("max_distance_m", "min_speed_kmh", "no_target_delay_s"):
        ints[key] = _get(raw, "radar.detection", key, 0, int)
    try:
        return ld2451.DetectionParams(
            ints["max_distance_m"],
            _DIRECTIONS[direction],
            ints["min_speed_kmh"],
            ints["no_target_delay_s"],
        )
    except ValueError as exc:
        raise ConfigError(f"[radar.detection] {exc}") from exc


def parse_config(data: Mapping[str, Any]) -> Config:
    """Validate a parsed TOML mapping and build a :class:`Config`."""
    for table in data:
        if table not in {"recording", "storage", "camera", "radar", "setup", "control", "leds"}:
            raise ConfigError(f"unknown table [{table}]")

    rec = _table(data, "recording", {"segment_s", "retry_initial_s", "retry_max_s", "status_file"})
    segment_s = _get(rec, "recording", "segment_s", 300.0, float)
    retry_initial_s = _get(rec, "recording", "retry_initial_s", 1.0, float)
    retry_max_s = _get(rec, "recording", "retry_max_s", 60.0, float)
    status_file = Path(
        _get(rec, "recording", "status_file", "/run/radar-cam-recorder/status.json", str)
    )
    for key, val in (("segment_s", segment_s), ("retry_initial_s", retry_initial_s)):
        _positive("recording", key, val)
    if retry_max_s < retry_initial_s:
        raise ConfigError("[recording] retry_max_s must be >= retry_initial_s")

    st = _table(
        data,
        "storage",
        {
            "recordings_dir",
            "require_mountpoint",
            "stop_below_mb",
            "resume_above_mb",
            "check_interval_s",
        },
    )
    if "recordings_dir" not in st:
        raise ConfigError("[storage] recordings_dir is required")
    recordings_dir = _get(st, "storage", "recordings_dir", "", str)
    if not recordings_dir or not Path(recordings_dir).is_absolute():
        raise ConfigError("[storage] recordings_dir must be an absolute path")
    stop_mb = _get(st, "storage", "stop_below_mb", 1024, int)
    resume_mb = _get(st, "storage", "resume_above_mb", 1536, int)
    check_s = _get(st, "storage", "check_interval_s", 5.0, float)
    if stop_mb < 0:
        raise ConfigError("[storage] stop_below_mb must be >= 0")
    if resume_mb <= stop_mb:
        raise ConfigError("[storage] resume_above_mb must be > stop_below_mb")
    _positive("storage", "check_interval_s", check_s)
    storage = StorageConfig(
        recordings_dir=Path(recordings_dir),
        require_mountpoint=_get(st, "storage", "require_mountpoint", False, bool),
        stop_below_bytes=stop_mb * _MB,
        resume_above_bytes=resume_mb * _MB,
        check_interval_s=check_s,
    )

    cam = _table(
        data,
        "camera",
        {
            "enabled",
            "binary",
            "width",
            "height",
            "framerate",
            "bitrate",
            "extra_args",
            "stop_timeout_s",
            "stall_timeout_s",
        },
    )
    extra = cam.get("extra_args", [])
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        raise ConfigError("[camera] extra_args must be a list of strings")
    camera = CameraConfig(
        enabled=_get(cam, "camera", "enabled", True, bool),
        binary=_get(cam, "camera", "binary", "rpicam-vid", str),
        width=_get(cam, "camera", "width", 1296, int),
        height=_get(cam, "camera", "height", 972, int),
        framerate=_get(cam, "camera", "framerate", 25.0, float),
        bitrate=_get(cam, "camera", "bitrate", 4_000_000, int),
        extra_args=tuple(extra),
        stop_timeout_s=_get(cam, "camera", "stop_timeout_s", 5.0, float),
        stall_timeout_s=_get(cam, "camera", "stall_timeout_s", 10.0, float),
    )
    for key in ("width", "height", "framerate", "bitrate", "stop_timeout_s", "stall_timeout_s"):
        _positive("camera", key, getattr(camera, key))

    rad = _table(data, "radar", {"enabled", "port", "baudrate", "silence_timeout_s", "detection"})
    radar = RadarConfig(
        enabled=_get(rad, "radar", "enabled", True, bool),
        port=_get(rad, "radar", "port", "/dev/serial0", str),
        baudrate=_get(rad, "radar", "baudrate", 115200, int),
        silence_timeout_s=_get(rad, "radar", "silence_timeout_s", 5.0, float),
        detection=_parse_detection(rad["detection"]) if "detection" in rad else None,
    )
    _positive("radar", "baudrate", radar.baudrate)
    _positive("radar", "silence_timeout_s", radar.silence_timeout_s)

    su = _table(data, "setup", {"notes", "geometry"})
    geometry_raw = su.get("geometry", {})
    if not isinstance(geometry_raw, Mapping):
        raise ConfigError("[setup.geometry] must be a table")
    geometry: dict[str, float] = {}
    for key, val in geometry_raw.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
            raise ConfigError(f"[setup.geometry] {key} must be a finite number")
        geometry[str(key)] = float(val)
    setup = SetupConfig(notes=_get(su, "setup", "notes", "", str), geometry=geometry)

    ctl = _table(
        data,
        "control",
        {
            "start_mode",
            "button_name",
            "double_press_s",
            "max_press_s",
            "long_press_s",
            "debounce_s",
        },
    )
    control = ControlConfig(
        start_mode=_get(ctl, "control", "start_mode", "idle", str),
        button_name=_get(ctl, "control", "button_name", "button@3", str),
        double_press_s=_get(ctl, "control", "double_press_s", 0.4, float),
        max_press_s=_get(ctl, "control", "max_press_s", 1.0, float),
        long_press_s=_get(ctl, "control", "long_press_s", 5.0, float),
        debounce_s=_get(ctl, "control", "debounce_s", 0.03, float),
    )
    if control.start_mode not in {"idle", "record"}:
        raise ConfigError("[control] start_mode must be one of ['idle', 'record']")
    if not control.button_name:
        raise ConfigError("[control] button_name must not be empty")
    for key in ("double_press_s", "max_press_s", "long_press_s", "debounce_s"):
        _positive("control", key, getattr(control, key))
    if control.long_press_s <= control.max_press_s:
        raise ConfigError("[control] long_press_s must be > max_press_s")
    if control.debounce_s >= control.double_press_s:
        raise ConfigError("[control] debounce_s must be < double_press_s")

    led = _table(data, "leds", {"enabled", "green", "red"})
    leds = LedsConfig(
        enabled=_get(led, "leds", "enabled", True, bool),
        green=_get(led, "leds", "green", "rcr-green", str),
        red=_get(led, "leds", "red", "rcr-red", str),
    )

    return Config(
        storage=storage,
        camera=camera,
        radar=radar,
        setup=setup,
        control=control,
        leds=leds,
        segment_s=segment_s,
        retry_initial_s=retry_initial_s,
        retry_max_s=retry_max_s,
        status_file=status_file,
    )


def load_config(path: Path) -> Config:
    """Read and validate a TOML config file."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(data)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """JSON-serialisable snapshot of ``cfg`` (stored in ``session.json``)."""
    det = cfg.radar.detection
    detection: dict[str, Any] | None = None
    if det is not None:
        detection = {
            "max_distance_m": det.max_distance_m,
            "direction": ld2451.Direction(det.direction).name,
            "min_speed_kmh": det.min_speed_kmh,
            "no_target_delay_s": det.no_target_delay_s,
        }
    return {
        "recording": {
            "segment_s": cfg.segment_s,
            "retry_initial_s": cfg.retry_initial_s,
            "retry_max_s": cfg.retry_max_s,
            "status_file": str(cfg.status_file),
        },
        "storage": {
            "recordings_dir": str(cfg.storage.recordings_dir),
            "require_mountpoint": cfg.storage.require_mountpoint,
            "stop_below_bytes": cfg.storage.stop_below_bytes,
            "resume_above_bytes": cfg.storage.resume_above_bytes,
            "check_interval_s": cfg.storage.check_interval_s,
        },
        "camera": {
            "enabled": cfg.camera.enabled,
            "binary": cfg.camera.binary,
            "width": cfg.camera.width,
            "height": cfg.camera.height,
            "framerate": cfg.camera.framerate,
            "bitrate": cfg.camera.bitrate,
            "extra_args": list(cfg.camera.extra_args),
            "stop_timeout_s": cfg.camera.stop_timeout_s,
            "stall_timeout_s": cfg.camera.stall_timeout_s,
        },
        "radar": {
            "enabled": cfg.radar.enabled,
            "port": cfg.radar.port,
            "baudrate": cfg.radar.baudrate,
            "silence_timeout_s": cfg.radar.silence_timeout_s,
            "detection": detection,
        },
        "setup": {"notes": cfg.setup.notes, "geometry": dict(cfg.setup.geometry)},
        "control": {
            "start_mode": cfg.control.start_mode,
            "button_name": cfg.control.button_name,
            "double_press_s": cfg.control.double_press_s,
            "max_press_s": cfg.control.max_press_s,
            "long_press_s": cfg.control.long_press_s,
            "debounce_s": cfg.control.debounce_s,
        },
        "leds": {"enabled": cfg.leds.enabled, "green": cfg.leds.green, "red": cfg.leds.red},
    }
