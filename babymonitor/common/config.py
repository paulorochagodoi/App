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
    video_device: str = ""
    audio_device: str = ""
    timestamp_overlay: bool = True
    video_bitrate: int = 4000


@dataclass
class SecurityConfig:
    api_token: str = ""


@dataclass
class CameraConfig:
    ap: APConfig = field(default_factory=APConfig)
    fallback_wifi: WifiCredentials = field(default_factory=WifiCredentials)
    server: ServerConfig = field(default_factory=ServerConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


@dataclass
class MonitorConfig:
    fallback_wifi: WifiCredentials = field(default_factory=WifiCredentials)
    camera_ap: APConfig = field(default_factory=APConfig)
    kiosk_url_file: str = "/etc/babymonitor/kiosk_url"
    default_camera_url: str = "http://10.42.0.1:8080"


def _merge(instance, data: dict) -> None:
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
            "video_bitrate": cfg.streaming.video_bitrate,
        },
        "security": {"api_token": cfg.security.api_token},
    }
    Path(path).write_text(yaml.safe_dump(data, allow_unicode=True))
