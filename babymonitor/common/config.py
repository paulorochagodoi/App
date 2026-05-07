from __future__ import annotations
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass
class APConfig:
    ssid: str = "BabyMonitor-AP"
    password: str = "babymonitor123"
    ip: str = "10.42.0.1"


@dataclass
class WifiCredentials:
    ssid: str = ""
    password: str = ""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class StreamingConfig:
    width: int = 1920
    height: int = 1080
    framerate: int = 24
    hls_dir: str = "/tmp/hls"
    hls_target_duration: int = 1
    hls_max_files: int = 5
    video_device: str = ""   # empty = auto-detect; e.g. "/dev/video0"
    audio_device: str = ""   # empty = auto-detect; e.g. "hw:1,0"
    timestamp_overlay: bool = True


@dataclass
class RecordingsConfig:
    output_dir: str = "/opt/babymonitor/recordings"
    max_recordings: int = 50       # oldest are deleted when exceeded
    min_free_mb: int = 500         # refuse recording below this free space


@dataclass
class CryDetectorConfig:
    sample_rate: int = 16000
    chunk_size: int = 2048
    threshold: float = 0.65
    silence_timeout: int = 10
    calibrate_on_start: bool = True   # sample ambient noise before detecting


@dataclass
class SecurityConfig:
    api_token: str = ""   # if non-empty, POST endpoints require X-Api-Token header


@dataclass
class CameraConfig:
    ap: APConfig = field(default_factory=APConfig)
    fallback_wifi: WifiCredentials = field(default_factory=WifiCredentials)
    server: ServerConfig = field(default_factory=ServerConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    recordings: RecordingsConfig = field(default_factory=RecordingsConfig)
    cry_detector: CryDetectorConfig = field(default_factory=CryDetectorConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


@dataclass
class MonitorConfig:
    fallback_wifi: WifiCredentials = field(default_factory=WifiCredentials)
    camera_ap: APConfig = field(default_factory=APConfig)
    kiosk_url_file: str = "/etc/babymonitor/kiosk_url"
    default_camera_url: str = "http://10.42.0.1:8080"


def _merge(instance, data: dict) -> None:
    """Recursively overlay dict values onto a dataclass instance."""
    for key, value in data.items():
        if not hasattr(instance, key):
            continue
        attr = getattr(instance, key)
        if dataclasses.is_dataclass(attr) and isinstance(value, dict):
            _merge(attr, value)
        else:
            setattr(instance, key, value)


def load_camera_config(path: str) -> CameraConfig:
    cfg = CameraConfig()
    raw = yaml.safe_load(Path(path).read_text())
    if raw:
        _merge(cfg, raw)
    return cfg


def load_monitor_config(path: str) -> MonitorConfig:
    cfg = MonitorConfig()
    raw = yaml.safe_load(Path(path).read_text())
    if raw:
        _merge(cfg, raw)
    return cfg


def save_camera_config(cfg: CameraConfig, path: str) -> None:
    data = {
        "node_role": "camera",
        "ap": {"ssid": cfg.ap.ssid, "password": cfg.ap.password, "ip": cfg.ap.ip},
        "fallback_wifi": {"ssid": cfg.fallback_wifi.ssid, "password": cfg.fallback_wifi.password},
        "server": {"host": cfg.server.host, "port": cfg.server.port},
        "streaming": {
            "width": cfg.streaming.width, "height": cfg.streaming.height,
            "framerate": cfg.streaming.framerate, "hls_dir": cfg.streaming.hls_dir,
            "hls_target_duration": cfg.streaming.hls_target_duration,
            "hls_max_files": cfg.streaming.hls_max_files,
            "video_device": cfg.streaming.video_device,
            "audio_device": cfg.streaming.audio_device,
            "timestamp_overlay": cfg.streaming.timestamp_overlay,
        },
        "recordings": {
            "output_dir": cfg.recordings.output_dir,
            "max_recordings": cfg.recordings.max_recordings,
            "min_free_mb": cfg.recordings.min_free_mb,
        },
        "cry_detector": {
            "sample_rate": cfg.cry_detector.sample_rate,
            "chunk_size": cfg.cry_detector.chunk_size,
            "threshold": cfg.cry_detector.threshold,
            "silence_timeout": cfg.cry_detector.silence_timeout,
            "calibrate_on_start": cfg.cry_detector.calibrate_on_start,
        },
        "security": {"api_token": cfg.security.api_token},
    }
    Path(path).write_text(yaml.safe_dump(data, allow_unicode=True))
